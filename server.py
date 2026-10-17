#!/usr/bin/env python3
"""Trusted-LAN deployment. Python standard library only; no ChatGPT account or CDN."""
import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from engine import ROOT, CATALOG, ROUTES, research, validate_input, date_window
from sampling_jobs import JobManager, JobBusy
from web_sampling import readiness
from login_sessions import LoginSessions
from tls_support import is_local_request, wrap_server_tls

MAX_BODY = 2_000_000


class ProviderUnavailable(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderUnavailable('数据服务返回了重定向；请管理员检查接口地址。')


def fetch_notes(settings, input_value):
    url = settings.get('provider_url', '')
    if not url:
        raise ProviderUnavailable('尚未接入小红书数据服务。当前可使用词包拓展或导入笔记；不能自动读取小红书。')
    parsed = urlparse(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ProviderUnavailable('管理员配置的数据接口必须是 HTTPS 地址，且不能包含账号密码。')
    i, category = validate_input(input_value)
    request_data = {'version': 1, 'platform': 'xiaohongshu', 'input': i, 'window': date_window(),
                    'seedKeywords': category['words'][:30], 'maxNotes': 3000}
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
    if settings.get('provider_token'):
        headers['Authorization'] = 'Bearer ' + settings['provider_token']
    req = urllib.request.Request(url, data=json.dumps(request_data, ensure_ascii=False).encode(), headers=headers, method='POST')
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=35) as response:
            blob = response.read(MAX_BODY + 1)
        if len(blob) > MAX_BODY:
            raise ProviderUnavailable('数据服务返回内容过大，请减少单次笔记数量。')
        payload = json.loads(blob)
        if not isinstance(payload, dict) or not isinstance(payload.get('notes'), list):
            raise ProviderUnavailable('数据服务格式不符合要求，需返回 notes 数组。')
        return payload['notes'], str(payload.get('provider', '已配置数据服务'))[:100]
    except ProviderUnavailable:
        raise
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise ProviderUnavailable('数据服务调用失败或超时，请管理员检查连接与授权；没有生成热度数据。') from None


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 20

    def server_close(self):
        if hasattr(self, 'sessions'):
            self.sessions.close()
        if hasattr(self, 'jobs'):
            self.jobs.cancelled.set()
            if self.jobs.thread: self.jobs.thread.join(timeout=35)
        super().server_close()


def create_server(host, port, data_dir, settings):
    sampling_provider = settings.get('sampling_provider', 'legacy')
    if sampling_provider not in ('legacy', 'socai'):
        raise ValueError('未识别的采样驱动；请选择 legacy 或 socai。')
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    key_file = data_dir / 'session.key'
    if not key_file.exists():
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(secrets.token_bytes(32))
    key = key_file.read_bytes()
    database = data_dir / 'history.sqlite3'
    connection = sqlite3.connect(database)
    try:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, owner TEXT NOT NULL, created TEXT NOT NULL, result TEXT NOT NULL)')
        connection.execute('CREATE INDEX IF NOT EXISTS owner_created ON runs(owner,created DESC)')
        connection.commit()
    finally:
        connection.close()
    os.chmod(database, 0o600)
    adapter_file = ROOT / 'web-adapter.json'
    web_status = readiness(settings.get('web_enabled', False), adapter_file)
    lock = threading.Lock()
    hits = defaultdict(deque)
    allowed = {'127.0.0.1', 'localhost', '::1', *settings.get('allowed_hosts', [])}
    if host not in ('0.0.0.0', '::'):
        allowed.add(host)

    class Handler(BaseHTTPRequestHandler):
        server_version = 'CathayWorkbench/1.0'

        def setup(self):
            super().setup()
            self.connection.settimeout(40)

        def log_message(self, format, *args):
            # Do not log queries, imported text, cookie values, provider URLs or tokens.
            pass

        def send(self, status, data, kind='application/json; charset=utf-8'):
            content = json.dumps(data, ensure_ascii=False).encode() if kind.startswith('application/json') else data
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            if getattr(self, 'new_cookie', None):
                self.send_header('Set-Cookie', self.new_cookie)
            self.end_headers()
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def prepare(self):
            try:
                request_host = urlparse('http://' + self.headers.get('Host', '')).hostname
            except ValueError:
                request_host = None
            if request_host not in allowed:
                self.send(403, {'error': '不在允许的访问地址内，请使用启动窗口显示的地址。'})
                return False
            self.new_cookie = None
            self.secure = isinstance(self.connection, ssl.SSLSocket)
            self.login_transport = self.secure or is_local_request(request_host, self.client_address[0])
            cookie_name = '__Host-cx_secure' if self.secure else 'cx_visitor'
            signing_prefix = 'https:' if self.secure else ''
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get('Cookie', ''))
                token = cookie[cookie_name].value if cookie_name in cookie else ''
            except Exception:
                token = ''
            uid, _, signature = token.partition('.')
            expected = hmac.new(key, (signing_prefix + uid).encode(), hashlib.sha256).hexdigest()
            if not re.fullmatch(r'[a-f0-9]{32}', uid) or not hmac.compare_digest(signature, expected):
                uid = secrets.token_hex(16)
                signature = hmac.new(key, (signing_prefix + uid).encode(), hashlib.sha256).hexdigest()
                self.new_cookie = f'{cookie_name}={uid}.{signature}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000' + ('; Secure' if self.secure else '')
            self.owner = uid
            return True

        def require_login_transport(self):
            if self.login_transport: return True
            self.send(403, {'error': '远程扫码及在线检索必须使用 HTTPS 安全入口（启动安全扫码版.command）。HTTP 仅支持词包和导入。'})
            return False

        def db(self, sql, params=(), write=False):
            connection = sqlite3.connect(database, timeout=10)
            try:
                cursor = connection.execute(sql, params)
                rows = cursor.fetchall()
                if write:
                    connection.commit()
                return rows
            finally:
                connection.close()

        def do_GET(self):
            if not self.prepare():
                return
            parsed = urlparse(self.path)
            path = parsed.path
            if path in ('/', '/index.html'):
                try:
                    self.send(200, (ROOT / 'index.html').read_bytes(), 'text/html; charset=utf-8')
                except OSError:
                    self.send(503, {'error': 'HTML 文件不存在，请检查部署文件是否完整。'})
            elif path == '/healthz':
                self.send(200, {'ok': True, 'onlineConfigured': bool(settings.get('provider_url')), 'webSampling': web_status})
            elif path == '/api/bootstrap':
                projects = [{**p, 'categories': [{k: v for k, v in c.items() if k != 'words'} for c in p['categories']]} for p in CATALOG['projects']]
                self.send(200, {'projects': projects, 'records': CATALOG['records'], 'uniqueWords': CATALOG['uniqueWords'],
                                'source': CATALOG['source'], 'notes': CATALOG['notes'], 'routes': ROUTES, 'window': date_window(),
                                'onlineConfigured': bool(settings.get('provider_url')), 'webSampling': web_status, 'shareUrl': settings.get('share_url', ''),
                                'loginTransport': self.login_transport, 'https': self.secure,
                                'storage': '匿名浏览器身份；历史保存在部署电脑。清除 Cookie 或更换浏览器后不会自动恢复，请导出备份。'})
            elif path == '/api/xhs/session':
                if self.require_login_transport(): self.send(200, sessions.status(self.owner))
            elif path == '/api/jobs/latest':
                self.send(200, {'job': jobs.latest(self.owner)})
            elif path.startswith('/api/jobs/'):
                found = jobs.get(path.removeprefix('/api/jobs/'), self.owner)
                self.send(200, found) if found else self.send(404, {'error': '任务不存在或不属于此浏览器。'})
            elif path == '/api/history':
                query = parse_qs(parsed.query)
                try:
                    offset = max(0, int(query.get('offset', ['0'])[0]))
                except ValueError:
                    self.send(400, {'error': '历史分页参数无效。'}); return
                items = self.db('SELECT result FROM runs WHERE owner=? ORDER BY created DESC,id DESC LIMIT 31 OFFSET ?', (self.owner, offset))
                data = [json.loads(row[0]) for row in items]
                self.send(200, {'items': [{k: r[k] for k in ('id', 'createdAt', 'input', 'status')} for r in data[:30]], 'more': len(data) > 30})
            elif path == '/api/history-export':
                data = self.db('SELECT result FROM runs WHERE owner=? ORDER BY created DESC,id DESC', (self.owner,))
                self.send(200, {'version': 1, 'runs': [json.loads(r[0]) for r in data]})
            elif path.startswith('/api/history/'):
                run_id = path.removeprefix('/api/history/')
                found = self.db('SELECT result FROM runs WHERE id=? AND owner=?', (run_id, self.owner))
                self.send(200, json.loads(found[0][0])) if found else self.send(404, {'error': '该记录不存在或不属于此浏览器。'})
            else:
                self.send(404, {'error': '页面不存在。'})

        def do_POST(self):
            if not self.prepare():
                return
            origin = self.headers.get('Origin')
            if origin and origin != ('https://' if self.secure else 'http://') + self.headers.get('Host', ''):
                self.send(403, {'error': '已拒绝跨站请求。'}); return
            if self.headers.get('X-Requested-With') != 'CathayWorkbench' or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                self.send(415, {'error': '请通过本工具提交 JSON 请求。'}); return
            if self.path not in ('/api/research', '/api/jobs', '/api/xhs/connect', '/api/xhs/disconnect') and not re.fullmatch(r'/api/jobs/[a-f0-9]{32}/cancel', self.path):
                self.send(404, {'error': '接口不存在。'}); return
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                length = 0
            if not 0 < length <= MAX_BODY:
                self.send(413, {'error': '请求为空或超过 2 MB。请减少导入笔记数量。'}); return
            now = time.monotonic()
            with lock:
                for who in (self.client_address[0], '_global'):
                    while hits[who] and hits[who][0] < now - 60:
                        hits[who].popleft()
                if len(hits[self.client_address[0]]) >= 20 or len(hits['_global']) >= 60:
                    self.send(429, {'error': '查询较频繁，请一分钟后重试。'}); return
                hits[self.client_address[0]].append(now); hits['_global'].append(now)
            try:
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError('请求格式错误。')
                if self.path.startswith('/api/xhs/'):
                    if not self.require_login_transport(): return
                    if self.path.endswith('/disconnect'):
                        jobs.cancel_owner(self.owner)
                        self.send(200, sessions.disconnect(self.owner))
                    elif not web_status['enabled']:
                        self.send(503, {'error': '请管理员运行“启动安全扫码版.command”开启独立扫码入口。'})
                    else:
                        self.send(202, sessions.start(self.owner, payload.get('consent')))
                    return
                if self.path.endswith('/cancel'):
                    cancelled = jobs.cancel(self.path.split('/')[3], self.owner)
                    self.send(200 if cancelled else 404, {'cancelled': cancelled, 'error': '' if cancelled else '任务不存在、已结束或不属于此浏览器。'})
                    return
                i, _ = validate_input(payload.get('input', {}))
                if self.path == '/api/jobs':
                    if not self.require_login_transport(): return
                    if not web_status['enabled']:
                        self.send(503, {'error': '请管理员运行“启动安全扫码版.command”，再在本页扫码连接小红书。'})
                        return
                    if not settings.get('collector') and sessions.status(self.owner)['status'] != 'authenticated':
                        self.send(409, {'error': '请先在本页连接小红书并扫码登录，再提交在线检索。'})
                        return
                    self.send(202, jobs.submit(self.owner, i))
                    return
                provider = None
                if i['mode'] == 'online':
                    if not self.require_login_transport(): return
                    notes, provider = fetch_notes(settings, i)
                else:
                    notes = payload.get('notes', [])
                result = research(i, notes)
                result.update(id=secrets.token_hex(16), createdAt=datetime.now(timezone.utc).isoformat(), provider=provider)
                # Bounded LAN deployment; never delete old user history to make room silently.
                with lock:
                    if self.db('SELECT COUNT(*) FROM runs')[0][0] >= 5000:
                        self.send(507, {'error': '已达到 5000 次历史记录上限，请管理员先备份再扩容。'}); return
                    self.db('INSERT INTO runs VALUES (?,?,?,?)', (result['id'], self.owner, result['createdAt'], json.dumps(result, ensure_ascii=False)), True)
                self.send(200, result)
            except ProviderUnavailable as e:
                self.send(503, {'error': str(e)})
            except JobBusy as e:
                self.send(429, {'error': str(e)})
            except (ValueError, TypeError, KeyError, OverflowError):
                # Validation messages are controlled by this application, not upstream secrets.
                try:
                    raise
                except ValueError as e:
                    self.send(400, {'error': str(e)[:300]})
                except Exception:
                    self.send(400, {'error': '数据格式不正确，请检查导入字段。'})
            except (sqlite3.Error, OSError):
                self.send(500, {'error': '历史保存失败，请检查部署电脑的磁盘空间和目录权限。'})

    app = AppServer((host, port), Handler)
    try:
        if settings.get('tls_cert') or settings.get('tls_key'):
            if not (settings.get('tls_cert') and settings.get('tls_key')): raise ValueError('HTTPS 证书和私钥必须同时提供。')
            wrap_server_tls(app, settings['tls_cert'], settings['tls_key'])
    except Exception:
        app.server_close()
        raise
    def driver_factory():
        if sampling_provider == 'socai':
            from socai_login_browser import SocaiLoginBrowser
            return SocaiLoginBrowser(socai_executable=settings.get('socai_executable'),
                                     edge_executable=settings.get('edge_executable'))
        from login_browser import LoginBrowser
        return LoginBrowser(adapter_file)
    sessions = LoginSessions(settings.get('login_driver_factory') or driver_factory)
    app.sessions = sessions
    # Bind first: a second launch on the same port must not invalidate running jobs.
    jobs = JobManager(database, data_dir, adapter_file, settings.get('collector'), sessions=None if settings.get('collector') else sessions)
    app.jobs = jobs
    return app


def main():
    parser = argparse.ArgumentParser(description='国泰航空 · 小红书拓词工作台（可信局域网）')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--advertise', default='')
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data')
    parser.add_argument('--enable-web', action='store_true', help='启用部署电脑的专用小红书采样会话，需先手动登录并完成网页适配验证')
    parser.add_argument('--sampling-provider', choices=('legacy', 'socai'), default='legacy',
                        help='采样驱动；Socai 独立验收通过前不修改正式启动配置')
    parser.add_argument('--tls-cert', type=Path)
    parser.add_argument('--tls-key', type=Path)
    args = parser.parse_args()
    settings = {'provider_url': os.environ.get('XHS_PROVIDER_URL', ''), 'provider_token': os.environ.get('XHS_PROVIDER_TOKEN', ''), 'allowed_hosts': []}
    settings['web_enabled'] = args.enable_web
    settings['sampling_provider'] = args.sampling_provider
    settings.update(tls_cert=args.tls_cert, tls_key=args.tls_key)
    scheme = 'https' if args.tls_cert and args.tls_key else 'http'
    if args.advertise:
        ipaddress.ip_address(args.advertise)
        settings['allowed_hosts'].append(args.advertise)
        settings['share_url'] = f'{scheme}://{args.advertise}:{args.port}'
    try:
        server = create_server(args.host, args.port, args.data_dir, settings)
    except (OSError, ValueError) as e:
        print(f'启动失败：{e}\n如果端口已被占用，请先打开 http://localhost:{args.port} 检查已有服务。', flush=True)
        return 1
    print(f'本机地址：{scheme}://localhost:{args.port}', flush=True)
    if args.advertise:
        print(f'同事访问：{settings["share_url"]}\n仅限可信的同一局域网；关闭服务或电脑休眠后无法访问。', flush=True)
    print('小红书在线数据：' + ('接口已配置，尚需实际查询验证。' if settings['provider_url'] else '尚未接入；可使用词包拓展和导入笔记分析。'), flush=True)
    if args.enable_web:
        print('独立扫码采样：已启用；每位用户在工具内授权扫码，进程退出会清除登录会话。非全平台统计。', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n服务已停止，历史已保存在 data 目录。', flush=True)
    finally:
        server.jobs.cancelled.set()
        if server.jobs.thread and server.jobs.thread.is_alive():
            server.jobs.thread.join(timeout=35)
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
