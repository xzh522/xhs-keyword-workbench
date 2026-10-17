"""Package an explicit source allowlist; never traverse user histories/configs."""
import argparse
import hashlib
import stat
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

COMMON = (
    'README.md', 'THIRD_PARTY_NOTICES.md', '.gitignore', 'index.html',
    'catalog.json', 'destinations.json', 'requirements-web.txt', 'settings.example.json',
    'analysis_core.py', 'engine.py', 'launcher.py', 'server.py',
    'login_browser.py', 'login_sessions.py', 'ordinary_search.py',
    'portable_runtime.py', 'publication_verifier.py', 'sampling_jobs.py',
    'socai_login_browser.py', 'socai_notes.py', 'socai_runtime.py', 'socai_sampling.py',
    'tls_support.py', 'web_sampling.py', 'windows_processes.py', 'web-adapter.json',
    'docs/Mac.md', 'docs/Windows.md', 'docs/FAQ.md', 'docs/Validation.md',
    'scripts/package_public.py',
)
MAC = ('启动工作台.command', '启动本机扫码版.command', '安装网页组件.command',
       '检查运行环境.command', 'scripts/macos_bootstrap.sh')
WINDOWS = ('Windows_启动词包模式.cmd', 'Windows_启动拓词工具.cmd',
           'Windows_安装网页组件.cmd', 'Windows_检查运行环境.cmd', 'scripts/windows_bootstrap.cmd')
TESTS = ('tests/test_lifecycle.py', 'tests/test_public_release.py', 'tests/test_http.py')
WORKFLOWS = ('.github/workflows/verify.yml',)
PUBLIC_FILES = COMMON + MAC + WINDOWS + TESTS + WORKFLOWS


def build(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    archives = []
    for label, extra in (('mac', MAC), ('windows', WINDOWS)):
        target = output / ('xhs-keyword-workbench-' + label + '.zip')
        with ZipFile(target, 'w', ZIP_DEFLATED) as archive:
            for name in COMMON + extra:
                path = root / name
                if not path.is_file() or path.is_symlink() or path.resolve().parent != path.parent.resolve():
                    raise ValueError('Missing or linked release input: ' + name)
                if not path.resolve().is_relative_to(root):
                    raise ValueError('Release input leaves source root: ' + name)
                info = ZipInfo('xhs-keyword-workbench/' + name, (2026, 10, 17, 0, 0, 0))
                info.create_system = 3
                mode = 0o755 if name.endswith(('.command', '.sh')) else 0o644
                info.external_attr = (stat.S_IFREG | mode) << 16
                info.compress_type = ZIP_DEFLATED
                archive.writestr(info, path.read_bytes())
        with ZipFile(target) as archive:
            if archive.testzip() is not None:
                raise ValueError('Archive integrity check failed')
        archives.append(target)
    checksums = ''.join(hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.name + '\n' for path in archives)
    (output / 'SHA256SUMS.txt').write_text(checksums, encoding='ascii')
    return archives


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Create privacy-clean local download archives')
    parser.add_argument('--output', type=Path, default=Path('dist'))
    args = parser.parse_args()
    for path in build(Path(__file__).resolve().parents[1], args.output):
        print(path.name + ': ' + str(path.stat().st_size) + ' bytes')
