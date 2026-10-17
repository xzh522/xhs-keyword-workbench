"""Owner-scoped, ephemeral browser sessions; Playwright never crosses threads."""
import base64
import queue
import sys
import threading
import time
import traceback
from pathlib import Path

from web_sampling import SamplingError

MESSAGES = {
    'disconnected': '未连接小红书。授权后可在本页扫码。',
    'starting': '正在打开小红书官方登录页面…',
    'waiting_scan': '请用小红书 App 扫码，并在手机上确认登录部署电脑。',
    'awaiting_confirmation': '已扫码，请在手机上的小红书 App 确认登录；本页会自动更新。',
    'authenticated': '小红书已连接，仅用于此浏览器发起的检索。',
    'busy': '正在使用你的独立会话采样。',
    'expired': '二维码或会话已过期，已关闭登录会话；请重新连接。',
    'verification_required': '小红书要求额外验证，已停止并关闭会话。请在官方页面人工处理后重试；不会绕过验证。',
    'failed': '小红书登录页面加载失败或结构变化，请稍后重试。未生成登录凭据或研究结果。',
    'disconnecting': '正在停止本人采样并关闭登录会话…',
}


def log_unexpected(error):
    """Record only an exception class and source line, never browser URLs or credentials."""
    frames = traceback.extract_tb(error.__traceback__)
    location = f'{Path(frames[-1].filename).name}:{frames[-1].lineno}' if frames else 'unknown'
    print(f'login_session_unexpected {type(error).__name__} at {location}', file=sys.stderr, flush=True)


class CombinedCancel:
    def __init__(self, *events): self.events = events
    def is_set(self): return any(e.is_set() for e in self.events)
    def wait(self, seconds):
        end = time.monotonic() + seconds
        while not self.is_set():
            left = end - time.monotonic()
            if left <= 0: return False
            self.events[0].wait(min(.05, left))
        return True


class LoginSessions:
    def __init__(self, driver_factory, *, max_sessions=4, login_timeout=180, idle_timeout=1800, poll_interval=1):
        self.factory = driver_factory
        self.max_sessions, self.login_timeout = max_sessions, login_timeout
        self.idle_timeout, self.poll_interval = idle_timeout, poll_interval
        self.lock = threading.RLock()
        self.sessions = {}
        self.closed = False

    def status(self, owner):
        with self.lock:
            s = self.sessions.get(owner)
            if not s: return {'status': 'disconnected', 'message': MESSAGES['disconnected']}
            state = s['status']
            result = {'status': state, 'message': MESSAGES[state]}
            if state == 'waiting_scan' and s.get('qr'):
                result['qrDataUrl'] = 'data:image/png;base64,' + base64.b64encode(s['qr']).decode('ascii')
                result['expiresIn'] = max(0, int(s['login_deadline'] - time.monotonic()))
            return result

    def _set(self, s, status, qr=None):
        with self.lock:
            if s['stop'].is_set() and status not in ('disconnecting', 'disconnected'):
                return
            s['status'], s['qr'] = status, qr if status == 'waiting_scan' else None

    def start(self, owner, consent):
        if consent is not True: raise ValueError('请先明确同意将本人账号登录到部署电脑，仅供本人检索。')
        with self.lock:
            if self.closed: raise ValueError('服务正在关闭。')
            old = self.sessions.get(owner)
            if old and old['thread'].is_alive():
                raise ValueError('当前会话尚未关闭，请先断开后再连接。')
            if sum(s['thread'].is_alive() for s in self.sessions.values()) >= self.max_sessions:
                raise ValueError('部署电脑同时最多支持4个登录会话，请稍后重试。')
            # Retain no unbounded anonymous session registry.
            self.sessions = {k:s for k,s in self.sessions.items() if s['thread'].is_alive() or k == owner}
            s = {'status': 'starting', 'qr': None, 'stop': threading.Event(), 'requests': queue.Queue(maxsize=1),
                 'login_deadline': time.monotonic() + self.login_timeout, 'last_use': time.monotonic()}
            s['thread'] = threading.Thread(target=self._worker, args=(s,), daemon=True)
            self.sessions[owner] = s
            s['thread'].start()
            return self.status(owner)

    def disconnect(self, owner):
        with self.lock:
            s = self.sessions.get(owner)
            if s:
                s['stop'].set()
                self._set(s, 'disconnecting' if s['thread'].is_alive() else 'disconnected')
            return self.status(owner)

    def sample(self, owner, value, progress, cancelled):
        with self.lock:
            s = self.sessions.get(owner)
            if not s or s['status'] != 'authenticated' or s['stop'].is_set():
                raise SamplingError('login_required', '请先在本页连接小红书并扫码登录，再提交在线检索。')
            request = {'value': value, 'progress': progress, 'cancelled': cancelled, 'done': threading.Event()}
            self._set(s, 'busy')
            s['requests'].put_nowait(request)
        while not request['done'].wait(.2):
            if not s['thread'].is_alive():
                raise SamplingError('session_closed', '登录会话已关闭，请重新连接。')
        if 'error' in request: raise request['error']
        if cancelled.is_set() or s['stop'].is_set():
            raise SamplingError('cancelled', '会话或任务已取消。')
        return request['result']

    def _worker(self, s):
        driver, request, authenticated = None, None, False
        try:
            driver = self.factory()
            driver.open()
            while not s['stop'].is_set():
                now = time.monotonic()
                if not authenticated:
                    if now >= s['login_deadline']:
                        self._set(s, 'expired'); break
                    visible = driver.observe()
                    state = visible.get('status')
                    if state not in ('starting', 'waiting_scan', 'awaiting_confirmation', 'authenticated', 'expired', 'verification_required', 'failed'):
                        state = 'failed'
                    self._set(s, state, visible.get('qr'))
                    if state in ('expired', 'verification_required', 'failed'): break
                    if state == 'authenticated':
                        authenticated = True
                        s['last_use'] = now
                elif s['status'] != 'busy' and now - s['last_use'] >= self.idle_timeout:
                    self._set(s, 'expired'); break
                try:
                    request = s['requests'].get(timeout=self.poll_interval)
                except queue.Empty:
                    continue
                try:
                    cancel = CombinedCancel(s['stop'], request['cancelled'])
                    if cancel.is_set(): raise SamplingError('cancelled', '任务已取消。')
                    request['result'] = driver.collect(request['value'], request['progress'], cancel)
                    s['last_use'] = time.monotonic()
                    self._set(s, 'authenticated')
                except SamplingError as error:
                    request['error'] = error
                    if error.code in ('page_timeout', 'results_unavailable') and not cancel.is_set():
                        # Do not confuse a sampling timeout with expired authentication.
                        # Recheck visible official UI; never infer login from old state.
                        try:
                            still_authenticated = driver.observe().get('status') == 'authenticated'
                        except Exception:
                            still_authenticated = False
                        if still_authenticated:
                            s['last_use'] = time.monotonic()
                            self._set(s, 'authenticated')
                            continue
                    state = 'verification_required' if error.code == 'access_restricted' else 'failed'
                    self._set(s, state)
                    break
                except Exception as error:
                    log_unexpected(error)
                    request['error'] = SamplingError('sampling_failed', '网页采样失败，登录会话已关闭。')
                    self._set(s, 'failed'); break
                finally:
                    request['done'].set()
                    request = None
        except Exception as error:
            log_unexpected(error)
            self._set(s, 'failed')
        finally:
            # Close on its owning thread, even if launch/navigation failed partway.
            if driver:
                try: driver.close()
                except Exception: self._set(s, 'failed')
            while True:
                try: pending = s['requests'].get_nowait()
                except queue.Empty: break
                pending['error'] = SamplingError('session_closed', '登录会话已关闭。')
                pending['done'].set()
            if s['stop'].is_set(): self._set(s, 'disconnected')

    def close(self):
        with self.lock:
            self.closed = True
            sessions = list(self.sessions.values())
            for s in sessions: s['stop'].set(); self._set(s, 'disconnecting')
        for s in sessions: s['thread'].join(timeout=45)
