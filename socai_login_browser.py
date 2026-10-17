"""Same page-internal QR contract, using an owned normal Edge + Socai reader."""
import os
import subprocess
import sys
import time
from pathlib import Path

from login_browser import LoginBrowser, wait_login_ui
from socai_runtime import SocaiReader
from socai_sampling import collect_socai
from publication_verifier import verify_publication
from web_sampling import SamplingError

from portable_runtime import discover, executable_path, owned_temporary_directory

EDGE = '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge'


class SocaiLoginBrowser(LoginBrowser):
    def __init__(self, socai_executable=None, edge_executable=None):
        super().__init__(None)
        self.socai_executable = socai_executable
        self.edge_executable = edge_executable
        self.temporary = None
        self.edge_group = None
        self.process = self.root = self.reader = self.endpoint = None

    def _spawn_edge(self, command, **kwargs):
        if sys.platform == 'win32':
            if self.edge_group is None:
                from windows_processes import WindowsProcessGroup
                self.edge_group = WindowsProcessGroup()
            return self.edge_group.spawn(command, **kwargs)
        return subprocess.Popen(command, **kwargs)

    def open_browser(self):
        from playwright.sync_api import sync_playwright
        if self.root is not None:
            raise SamplingError('socai_config_conflict', '该独立浏览器会话已经启动。')
        self.socai_executable = str(executable_path(self.socai_executable) if self.socai_executable else discover('socai'))
        self.edge_executable = str(executable_path(self.edge_executable) if self.edge_executable else discover('edge'))
        self.opened_at = time.monotonic()
        self.temporary = owned_temporary_directory()
        self.root = Path(self.temporary.name).resolve()
        profile = self.root / 'edge'
        profile.mkdir(mode=0o700)
        try:
            self.process = self._spawn_edge([self.edge_executable, '--user-data-dir=' + str(profile),
                                             '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
                                             '--no-first-run', '--no-default-browser-check', 'about:blank'],
                                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, close_fds=True)
            end = time.monotonic() + 15
            while time.monotonic() < end and self.process.poll() is None:
                try:
                    port = int((profile / 'DevToolsActivePort').read_text().splitlines()[0])
                    if not 1 <= port <= 65535:
                        raise ValueError('invalid port')
                    self.endpoint = f'http://127.0.0.1:{port}'
                    break
                except (OSError, ValueError, IndexError):
                    time.sleep(.1)
            if not self.endpoint:
                raise SamplingError('socai_unavailable', '独立 Edge 未能启动，请管理员检查浏览器安装。')
            self.reader = SocaiReader(self.socai_executable, self.endpoint, self.root / 'socai')
            self.pw = sync_playwright().start()
            self.browser = self.pw.chromium.connect_over_cdp(self.endpoint, timeout=10000)
            if len(self.browser.contexts) != 1:
                raise SamplingError('socai_config_conflict', '独立浏览器上下文无法唯一识别，已停止。')
            self.context = self.browser.contexts[0]
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.page.set_default_timeout(10000)
        except SamplingError:
            self.close()
            raise
        except Exception:
            self.close()
            raise SamplingError('socai_unavailable', '无法连接独立 Edge，未读取个人浏览器资料。') from None

    def open(self):
        self.open_browser()
        self.page.goto('https://www.xiaohongshu.com/explore', wait_until='commit', timeout=30000)
        wait_login_ui(self.page)

    def collect(self, value, progress, cancelled):
        # Separate, same-owner page so metadata navigation cannot replace the
        # verified search list or interfere with Socai's own detail tab.
        date_page = self.context.new_page()
        try:
            def verify(row, window, check, deadline):
                return verify_publication(date_page, row, window, check, deadline)
            return collect_socai(value, self.page, self.reader, progress, cancelled, date_verifier=verify)
        finally:
            date_page.close()

    def close(self):
        cleanup_error = None
        try:
            if self.reader:
                self.reader.close()
        except Exception as error:
            cleanup_error = error
        finally:
            # CDP disconnect only; the separately owned OS process controls exit.
            if self.pw:
                try:
                    self.pw.stop()
                except Exception as error:
                    cleanup_error = cleanup_error or error
                finally:
                    self.pw = None
            if self.process and self.process.poll() is None:
                try:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=3)
                except Exception as error:
                    cleanup_error = cleanup_error or error
            if self.edge_group is not None:
                try:
                    self.edge_group.close()
                    self.edge_group = None
                except Exception as error:
                    cleanup_error = cleanup_error or error
            # Only this instance's newly created temporary directory, never HOME.
            if self.temporary is not None and cleanup_error is None:
                self.temporary.cleanup()
                self.temporary = None
                self.root = None
            elif self.temporary is not None:
                # Preserve an uncleared live session for explicit retry; an
                # object finalizer must not remove its IPC/profile underneath it.
                self.temporary._finalizer.detach()
        if cleanup_error:
            raise SamplingError('socai_cleanup_failed', '独立会话清理未全部完成，已停止；请管理员检查。') from None
