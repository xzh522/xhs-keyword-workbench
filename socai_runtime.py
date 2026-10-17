"""Read-only Socai subprocesses bound to an explicitly owned local browser.

Never use global `socai stop`: it can terminate other sessions. Cleanup uses
the kernel-reported PID of this reader's private Unix socket, not a PID file.
"""
import errno
import json
import os
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from portable_runtime import bounded_output, executable_path, stop_process
from socai_notes import normalize_note
from web_sampling import SamplingError, note_url


class SocaiReader:
    def __init__(self, executable, endpoint, home):
        try:
            parsed = urlsplit(endpoint)
            valid = (parsed.scheme == 'http' and parsed.hostname == '127.0.0.1'
                     and not parsed.username and not parsed.password and parsed.port
                     and parsed.path == '' and not parsed.query and not parsed.fragment)
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise SamplingError('socai_config_conflict', 'Socai 必须连接本工具的独立本机会话。')
        self.executable = executable_path(executable)
        self.home = Path(home)
        if (not self.home.is_absolute() or self.home.resolve() != self.home
                or (sys.platform != 'win32' and len(os.fsencode(self.home / 'rust-daemon.sock')) >= 104)):
            raise SamplingError('socai_config_conflict', 'Socai 工作目录必须是独立的私有目录。')
        try:
            self.home.mkdir(mode=0o700)
        except OSError:
            raise SamplingError('socai_config_conflict', 'Socai 工作目录不可创建或已被其他会话占用。') from None
        self.endpoint, self.closed, self.checked = endpoint, False, False
        self.version = None
        self.daemon = None
        self.process_group = None
        self.home_identity = self.home.stat()
        self.environment = {k: v for k, v in os.environ.items() if not k.startswith('SOCAI_')}
        self.environment.update(SOCAI_HOME=str(self.home), SOCAI_RUNS_DIR=str(self.home / 'runs'),
                                SOCAI_CDP_URL=endpoint, SOCAI_TELEMETRY='0',
                                SOCAI_TELEMETRY_QUERY_TEXT='0', SOCAI_TELEMETRY_CHAT_TEXT='0',
                                SOCAI_TELEMETRY_TASK_TEXT='0')

    def _command(self, args):
        return [str(self.executable), *args]

    def _run(self, args, cancelled, deadline):
        return bounded_output(self._command(args), self.environment, cancelled, deadline, self._spawn)

    def _spawn(self, command, **kwargs):
        if os.name == 'nt':
            if self.process_group is None:
                from windows_processes import WindowsProcessGroup
                self.process_group = WindowsProcessGroup()
            return self.process_group.spawn(command, **kwargs)
        return subprocess.Popen(command, **kwargs)

    def _daemon_endpoint(self):
        try:
            metadata = self.home.lstat()
            if (self.home.is_symlink() or metadata.st_ino != self.home_identity.st_ino
                    or metadata.st_dev != self.home_identity.st_dev):
                raise ValueError()
            path = self.home / 'rust-daemon-endpoint.json'
            if path.is_symlink() or path.stat().st_size > 8192:
                raise ValueError()
            value = json.loads(path.read_bytes())
            if (not isinstance(value, dict) or value.get('host') != '127.0.0.1'
                    or type(value.get('port')) is not int or not 1 <= value['port'] <= 65535
                    or not isinstance(value.get('token'), str) or not 1 <= len(value['token']) <= 4096):
                raise ValueError()
            return value
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError):
            raise SamplingError('socai_config_conflict', '本会话后台端点无法核验，已停止。') from None

    def _start_daemon(self, cancelled, deadline):
        if self.daemon is not None:
            if self.daemon.poll() is not None:
                raise SamplingError('socai_command_failed', '本会话后台进程已退出，请重新连接。')
            return
        target = self.home / 'rust-daemon.sock'
        if ((sys.platform == 'win32' and self._daemon_endpoint() is not None)
                or (sys.platform != 'win32' and target.exists())):
            raise SamplingError('socai_config_conflict', '本会话后台端点已被占用，已停止。')
        try:
            self.daemon = self._spawn(self._command(['__daemon']), env=self.environment,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, close_fds=True)
            end = min(deadline, time.monotonic() + 15)
            while time.monotonic() < end:
                if cancelled():
                    raise SamplingError('cancelled', '已取消本轮采样。')
                if self.daemon.poll() is not None:
                    raise SamplingError('socai_command_failed', 'Socai 后台进程未能启动。')
                if sys.platform == 'win32':
                    if self._daemon_endpoint() is not None:
                        return
                elif target.exists():
                    metadata = target.lstat()
                    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid():
                        raise SamplingError('socai_config_conflict', '本会话后台端点无法核验，已停止。')
                    with socket.socket(socket.AF_UNIX) as client:
                        client.settimeout(1)
                        client.connect(str(target))
                        if self._peer_pid(client) != self.daemon.pid:
                            raise SamplingError('socai_config_conflict', '本会话后台进程身份不符，已停止。')
                    return
                time.sleep(.05)
            raise SamplingError('page_timeout', 'Socai 后台进程启动超时，已停止。')
        except (OSError, ValueError):
            raise SamplingError('socai_command_failed', 'Socai 后台进程未能启动。') from None

    def _json(self, text):
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            raise SamplingError('socai_invalid_result', 'Socai 返回格式无法解析，已停止采样。') from None

    def _preflight(self, cancelled, deadline):
        if not self.checked:
            version = self._json(self._run(['version', '--no-check', '--json'], cancelled, deadline))
            if not isinstance(version, dict) or version.get('current_version') not in ('0.6.0', '0.6.6'):
                raise SamplingError('socai_incompatible', '当前 Socai 版本尚未验证，请管理员检查兼容性。')
            help_text = self._run(['xhs', 'get-notes', '--help'], cancelled, deadline)
            if not all(flag in help_text for flag in ('--note', '--num-comments')):
                raise SamplingError('socai_incompatible', 'Socai 缺少已验证的只读笔记命令。')
            self.version = version['current_version']
            if sys.platform == 'win32' and self.version != '0.6.6':
                raise SamplingError('socai_incompatible', 'Windows 需要已检查命令格式的 Socai CLI 0.6.6。')
            self.checked = True
        # This release reads global config even with SOCAI_HOME. Never mutate it.
        config = self._json(self._run(['config', 'list'], cancelled, deadline))
        conflict = not isinstance(config, dict)
        if not conflict:
            chrome, runs, cloud = (config.get(k, {}) for k in ('chrome', 'runs', 'cloud'))
            conflict = (not all(isinstance(v, dict) for v in (chrome, runs, cloud))
                        or chrome.get('profile') not in (None, 'existing', 'remote')
                        or bool(chrome.get('profile_dir')) or bool(runs.get('dir'))
                        or bool(cloud.get('base_url')))
        if conflict:
            raise SamplingError('socai_config_conflict', 'Socai 全局配置与独立会话冲突，已停止；未修改你的配置。')

    def read_note(self, url, query, window, cancelled, deadline):
        if self.closed:
            raise SamplingError('socai_unavailable', '该 Socai 会话已关闭，请重新连接。')
        if not isinstance(url, str) or not note_url(url):
            raise SamplingError('socai_invalid_result', '笔记来源不是已验证的小红书官方链接。')
        parsed = urlsplit(url)
        tokens = parse_qs(parsed.query).get('xsec_token', [])
        if len(tokens) != 1 or not re.fullmatch(r'[a-zA-Z0-9_+=/\-]{1,2048}', tokens[0]):
            raise SamplingError('note_unavailable', '笔记缺少可用的公开访问链接，已排除本篇。')
        try:
            self._preflight(cancelled, deadline)
            if self.version == '0.6.6':
                self._start_daemon(cancelled, deadline)
            identifier = parsed.path.rsplit('/', 1)[-1]
            payload = self._json(self._run(['xhs', 'get-notes', '--note', identifier + '=' + tokens[0],
                                          '--num-comments', '0'], cancelled, deadline))
            return normalize_note(payload, url, query, window)
        except SamplingError:
            self._stop_daemon()
            raise

    def _stop_daemon(self):
        if sys.platform == 'win32':
            owned_daemon = self.daemon is not None
            try:
                if owned_daemon:
                    stop_process(self.daemon)
            finally:
                if self.process_group is not None:
                    self.process_group.close()
                    self.process_group = None
            # The endpoint is not proof of ownership. Only remove a file in
            # our unchanged private home after our held daemon and job stopped.
            endpoint = self._daemon_endpoint()
            if endpoint is not None:
                if not owned_daemon:
                    raise SamplingError('socai_cleanup_failed', '发现无法归属的会话端点，未删除；请重新连接。')
                try:
                    (self.home / 'rust-daemon-endpoint.json').unlink()
                except OSError:
                    raise SamplingError('socai_cleanup_failed', '自有会话连接文件清理失败，请重新连接。') from None
            self.daemon = None
            return
        try:
            self._stop_unix_daemon()
        finally:
            # Startup can be cancelled before a socket exists. The directly
            # spawned 0.6.6 daemon is still known through its owned Popen handle.
            if self.daemon is not None:
                stop_process(self.daemon)
                self.daemon = None

    def _peer_pid(self, client):
        if sys.platform == 'darwin':
            return struct.unpack('i', client.getsockopt(0, 2, 4))[0]
        if hasattr(socket, 'SO_PEERCRED'):
            pid, uid, _ = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid == os.getuid():
                return pid
        raise ValueError('peer identity unsupported or mismatched')

    def _stop_unix_daemon(self):
        target = self.home / 'rust-daemon.sock'
        try:
            metadata = target.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise SamplingError('socai_cleanup_failed', '无法核验本会话后台进程，已停止；请管理员检查。')
        try:
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(1)
                client.connect(str(target))
                pid = self._peer_pid(client)
                if pid <= 1 or pid == os.getpid():
                    raise ValueError('invalid peer')
                if self.daemon is not None and pid != self.daemon.pid:
                    raise ValueError('owned child mismatch')
                os.kill(pid, signal.SIGTERM)
        except OSError as error:
            if error.errno in (errno.ENOENT, errno.ECONNREFUSED, errno.ESRCH):
                return
            raise SamplingError('socai_cleanup_failed', '无法关闭本会话后台进程，已停止；请管理员检查。') from None
        except ValueError:
            raise SamplingError('socai_cleanup_failed', '无法核验本会话后台进程，已停止；请管理员检查。') from None
        # Treat a reaped or zombie process as stopped; don't signal a reused PID.
        end = time.monotonic() + 20
        while time.monotonic() < end:
            state = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'stat='],
                                   capture_output=True, text=True, timeout=1).stdout.strip()
            if not state or state.startswith('Z'):
                return
            time.sleep(.1)
        raise SamplingError('socai_cleanup_failed', '本会话后台进程未完成退出，已停止；不会继续采样。')

    def close(self):
        self._stop_daemon()
        self.closed = True
