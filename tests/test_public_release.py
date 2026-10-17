import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from engine import CATALOG, date_window, research

ROOT = Path(__file__).resolve().parents[1]


class PublicReleaseTests(unittest.TestCase):
    def test_all_example_categories_generate_five_or_more_words(self):
        for project in CATALOG['projects']:
            for category in project['categories']:
                with self.subTest(category=category['name']):
                    result = research({'project': project['id'], 'category': category['id'], 'mode': 'library'})
                    self.assertTrue(result['rows'])
                    for row in result['rows']:
                        self.assertGreaterEqual(len(row['keywords']), 5)
                        self.assertIsNone(row['count'])

    def test_readme_states_local_installation_and_unverified_windows_sampling(self):
        self.assertTrue((ROOT / 'README.md').is_file(), 'public README is missing')
        text = (ROOT / 'README.md').read_text(encoding='utf-8')
        for term in ('Python', 'Windows', 'Mac', 'Socai', '未完成', '不是搜索量', '本地'):
            self.assertIn(term, text)
        self.assertIn('以下开发命令仅适用于完整源码 ZIP', text)

    def test_zip_allowlist_excludes_planted_private_files(self):
        path = ROOT / 'scripts/package_public.py'
        self.assertTrue(path.is_file(), 'public safe packager is missing')
        spec = importlib.util.spec_from_file_location('public_packager', path)
        package = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(package)
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / 'source'
            fixture.mkdir()
            for name in package.COMMON:
                target = fixture / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('public fixture', encoding='utf-8')
            for name in package.MAC + package.WINDOWS:
                target = fixture / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('public fixture', encoding='utf-8')
            for name in ('data/session.key', 'settings.local.json', '.env', '.venv/private', '__pycache__/private.pyc'):
                target = fixture / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('DO NOT PUBLISH', encoding='utf-8')
            paths = package.build(fixture, Path(directory) / 'out')
            self.assertEqual(len(paths), 2)
            for archive in paths:
                with ZipFile(archive) as zip_file:
                    self.assertIsNone(zip_file.testzip())
                    for name in zip_file.namelist():
                        self.assertNotIn('DO NOT PUBLISH', zip_file.read(name).decode())
                        self.assertNotIn('/data/', name)
                    if archive.name.endswith('-mac.zip'):
                        entry = zip_file.getinfo('xhs-keyword-workbench/启动工作台.command')
                        self.assertTrue((entry.external_attr >> 16) & 0o111)
            self.assertTrue((Path(directory) / 'out' / 'SHA256SUMS.txt').is_file())

    def test_all_packaged_files_exist(self):
        path = ROOT / 'scripts/package_public.py'
        self.assertTrue(path.is_file(), 'public safe packager is missing')
        spec = importlib.util.spec_from_file_location('public_packager', path)
        package = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(package)
        for name in package.COMMON + package.MAC + package.WINDOWS:
            self.assertTrue((ROOT / name).is_file(), name)
            self.assertFalse((ROOT / name).is_symlink(), name)


if __name__ == '__main__':
    unittest.main()
