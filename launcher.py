#!/usr/bin/env python3
"""Local Mac/Windows launcher. Setup is explicit; offline needs only Python."""
import argparse
import importlib.util
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from portable_runtime import DOWNLOADS, discover, owned_temporary_directory, read_settings
from web_sampling import SamplingError

ROOT = Path(__file__).resolve().parent


def venv_python(root):
    return Path(root) / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def setup(root):
    try:
        interpreter = venv_python(root)
        if interpreter.is_file():
            # Windows cannot overwrite a python.exe currently running this
            # launcher. Validate and reuse an existing environment on reruns.
            subprocess.run([str(interpreter), '-c', 'import sys; assert sys.version_info >= (3, 10)'],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.run([sys.executable, '-m', 'venv', str(Path(root) / '.venv')], check=True)
        subprocess.run([str(venv_python(root)), '-m', 'pip', 'install', '-r',
                        str(Path(root) / 'requirements-web.txt')], check=True)
        print('准备完成。重新运行启动器即可使用。')
        return 0
    except (OSError, subprocess.CalledProcessError):
        print('准备失败。请检查 Python、网络与写入权限；若 .venv 已损坏，请关闭工作台后移除该目录并重新运行 --setup。')
        return 1


def check_dependencies(root):
    from socai_runtime import SocaiReader
    settings = read_settings(root)
    failures = False
    if importlib.util.find_spec('playwright') is None:
        print('Playwright：未安装；请运行 launcher.py --setup。')
        failures = True
    else:
        print('Playwright：可用。')
    for kind in ('edge', 'socai'):
        try:
            settings[kind + '_executable'] = str(discover(kind, settings))
            print(('Edge' if kind == 'edge' else 'Socai CLI') + '：程序可用。')
        except SamplingError as error:
            print(str(error))
            failures = True
    if settings.get('socai_executable'):
        with owned_temporary_directory() as directory:
            reader = SocaiReader(settings['socai_executable'], 'http://127.0.0.1:1',
                                 Path(directory).resolve() / 'socai')
            try:
                reader._preflight(lambda: False, time.monotonic() + 10)
                print('Socai CLI：版本 ' + reader.version + '，只读命令格式通过。')
            except SamplingError as error:
                print(str(error))
                failures = True
            finally:
                reader.close()
    if failures:
        print(DOWNLOADS)
        raise SamplingError('socai_unavailable', '在线依赖检查未通过。可使用 --offline，或安装后重试。')
    return settings


def serve(root, settings, port=8769):
    from server import create_server
    try:
        server = create_server('127.0.0.1', port, Path(root) / 'data', settings)
    except (OSError, ValueError):
        print('启动失败：端口已占用或本地数据目录不可写。请检查已有服务；启动器不会关闭它。')
        return 1
    actual_port = server.server_address[1]
    url = f'http://127.0.0.1:{actual_port}'
    finished = threading.Event()
    def open_when_ready():
        # Binding already succeeded: this check cannot attach to another service.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        end = time.monotonic() + 10
        while not finished.is_set() and time.monotonic() < end:
            try:
                with opener.open(url + '/healthz', timeout=.5) as response:
                    ready = response.status == 200
                if ready and not finished.is_set():
                    webbrowser.open(url)
                    return
            except (OSError, urllib.error.URLError):
                pass
            finished.wait(.1)
    browser_thread = threading.Thread(target=open_when_ready, name='workbench-browser', daemon=True)
    browser_thread.start()
    print('本机地址：' + url, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n服务已停止，历史已保存在 data 目录。')
    finally:
        finished.set()
        server.jobs.cancelled.set()
        if server.jobs.thread and server.jobs.thread.is_alive():
            server.jobs.thread.join(timeout=35)
        server.server_close()
        browser_thread.join(timeout=1)
    return 0


def main(argv=None, reexec=True):
    parser = argparse.ArgumentParser(description='国泰航空小红书本地工作台（Windows / Mac）')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--setup', action='store_true', help='创建 .venv 并安装固定版本网页依赖')
    mode.add_argument('--check', action='store_true', help='检查依赖和 CLI 格式，不连接社交平台')
    mode.add_argument('--offline', action='store_true', help='词包、导入和历史；不需要在线依赖')
    args = parser.parse_args(argv)
    if sys.version_info < (3, 10):
        print('需要 Python 3.10 或更新版本。\n' + DOWNLOADS)
        return 1
    if args.setup:
        return setup(ROOT)
    interpreter = venv_python(ROOT)
    if reexec and interpreter.is_file() and Path(sys.prefix).resolve() != (ROOT / '.venv').resolve():
        try:
            return subprocess.call([str(interpreter), str(ROOT / 'launcher.py'), *(argv if argv is not None else sys.argv[1:])])
        except OSError:
            print('项目 .venv 无法运行。请运行 --setup 重新准备。')
            return 1
    try:
        if args.offline:
            settings = {'web_enabled': False, 'sampling_provider': 'socai', 'allowed_hosts': []}
        else:
            dependencies = check_dependencies(ROOT)
            if args.check:
                return 0
            settings = {**dependencies, 'web_enabled': True, 'sampling_provider': 'socai', 'allowed_hosts': []}
        return serve(ROOT, settings)
    except SamplingError as error:
        print(str(error))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
