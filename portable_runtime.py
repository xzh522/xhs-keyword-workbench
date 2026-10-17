"""Portable dependency discovery and bounded, private subprocess IO."""
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from web_sampling import SamplingError

EDGE_MAC = '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge'
DOWNLOADS = ('Python 3.10 或更新版本：https://www.python.org/downloads/\n'
             'Microsoft Edge：https://www.microsoft.com/edge/download\n'
             'Socai CLI（桌面安装包不能代替 CLI）：https://socai.io/docs/installation/')


def executable_path(value):
    path = Path(value)
    if (not path.is_absolute() or not path.is_file()
            or path.suffix.lower() in ('.msi', '.dmg', '.pkg', '.zip', '.bat', '.cmd')
            or any(word in path.name.lower() for word in ('setup', 'installer', 'updater'))
            or (os.name == 'nt' and path.suffix.lower() != '.exe')
            or (os.name != 'nt' and not os.access(path, os.X_OK))):
        raise SamplingError('socai_unavailable', '依赖路径必须是已安装程序的绝对路径，不能使用安装包。')
    return path


def read_settings(root):
    path = Path(root) / 'settings.local.json'
    if not path.exists():
        return {}
    try:
        blob = path.read_bytes()
        if len(blob) > 65536:
            raise ValueError()
        value = json.loads(blob)
        if not isinstance(value, dict) or any(not isinstance(value.get(key, ''), str)
                for key in ('edge_executable', 'socai_executable')):
            raise ValueError()
        return {key: value[key] for key in ('edge_executable', 'socai_executable') if value.get(key)}
    except (OSError, UnicodeError, ValueError):
        raise SamplingError('socai_config_conflict', 'settings.local.json 格式错误，请检查程序路径设置。') from None


def discover(kind, settings=None, environment=None, platform=None):
    environment = os.environ if environment is None else environment
    platform = sys.platform if platform is None else platform
    settings = settings or {}
    override = environment.get('CATHAY_' + kind.upper() + '_PATH') or settings.get(kind + '_executable')
    if override:
        return executable_path(override)
    candidates = []
    if kind == 'edge':
        if platform == 'win32':
            candidates = [Path(environment[key]) / 'Microsoft/Edge/Application/msedge.exe'
                          for key in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA')
                          if environment.get(key)]
        else:
            candidates = [Path(EDGE_MAC), Path.home() / EDGE_MAC.lstrip('/')]
    elif kind == 'socai':
        name = 'socai.exe' if platform == 'win32' else 'socai'
        candidates = [Path.home() / '.socai/bin' / name]
        on_path = shutil.which(name, path=environment.get('PATH', os.defpath))
        if on_path:
            candidates.append(Path(on_path))
    else:
        raise ValueError('unknown dependency')
    for candidate in candidates:
        try:
            return executable_path(candidate)
        except SamplingError:
            pass
    raise SamplingError('socai_unavailable', '未找到 ' + ('Microsoft Edge' if kind == 'edge' else 'Socai CLI') + '，请安装后重试。')


def stop_process(process, timeout=1):
    """Use only a Popen handle created by this caller, never a file's PID."""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def bounded_output(command, environment, cancelled, deadline, spawn=subprocess.Popen):
    end = min(deadline, time.monotonic() + 60)
    if cancelled():
        raise SamplingError('cancelled', '已取消本轮采样。')
    if end <= time.monotonic():
        raise SamplingError('page_timeout', '读取时间预算已用完，已停止采样。')
    process = None
    finished = threading.Event()
    chunks = queue.Queue(maxsize=8)
    worker = None
    try:
        process = spawn(command, env=environment, stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True)
        def pump():
            try:
                while not finished.is_set():
                    chunk = process.stdout.read1(65536)
                    while not finished.is_set():
                        try:
                            chunks.put(chunk, timeout=.05)
                            break
                        except queue.Full:
                            pass
                    if not chunk:
                        return
            except (OSError, ValueError):
                while not finished.is_set():
                    try:
                        chunks.put(None, timeout=.05)
                        return
                    except queue.Full:
                        pass
        worker = threading.Thread(target=pump, name='socai-output', daemon=True)
        worker.start()
        output = bytearray()
        eof = False
        while not eof or process.poll() is None:
            if cancelled():
                raise SamplingError('cancelled', '已取消本轮采样。')
            if time.monotonic() >= end:
                raise SamplingError('page_timeout', 'Socai 读取超时，已停止本次读取。')
            try:
                chunk = chunks.get(timeout=min(.05, max(.001, end - time.monotonic())))
            except queue.Empty:
                continue
            if chunk is None:
                raise OSError('pipe read failed')
            if not chunk:
                eof = True
            else:
                output.extend(chunk)
                if len(output) > 2 * 1024 * 1024:
                    raise SamplingError('socai_invalid_result', 'Socai 返回内容超过安全上限，已停止。')
        if process.returncode != 0:
            raise SamplingError('socai_command_failed', 'Socai 读取失败，未生成新的热度榜。')
        return bytes(output).decode('utf-8')
    except (OSError, UnicodeError):
        raise SamplingError('socai_command_failed', 'Socai 无法运行或返回不可读取内容，已停止。') from None
    finally:
        finished.set()
        if process is not None:
            stop_process(process)
            if worker:
                worker.join(timeout=2)
            process.stdout.close()


def owned_temporary_directory():
    # A short Unix socket path on macOS; Windows keeps its configured temp path.
    base = '/tmp' if sys.platform == 'darwin' else None
    return tempfile.TemporaryDirectory(prefix='cxhs-', dir=base)
