"""Real loopback HTTP with temporary history; no platform or personal login."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from engine import date_window
from server import create_server


class LocalHTTPTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.start_server()

    def start_server(self):
        self.app = create_server('127.0.0.1', 0, Path(self.folder.name), {'sampling_provider': 'socai'})
        self.worker = threading.Thread(target=self.app.serve_forever, daemon=True)
        self.worker.start()
        self.base = 'http://127.0.0.1:' + str(self.app.server_port)

    def stop_server(self):
        self.app.shutdown()
        self.app.server_close()
        self.worker.join(5)

    def tearDown(self):
        self.stop_server()
        self.folder.cleanup()

    def request(self, path, body=None, cookie='', origin=None):
        headers = {'Cookie': cookie, 'Content-Type': 'application/json', 'X-Requested-With': 'CathayWorkbench'}
        if origin:
            headers['Origin'] = origin
        request = urllib.request.Request(self.base + path, headers=headers,
            data=None if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8'))
        return self.opener.open(request, timeout=5)

    def bootstrap(self):
        with self.request('/api/bootstrap') as response:
            return response.headers['Set-Cookie'].split(';')[0], json.load(response)

    def test_library_history_isolation_backup_and_restart(self):
        cookie, bootstrap = self.bootstrap()
        self.assertLess(bootstrap['uniqueWords'], 300)
        with self.request('/api/research', {'input': {'mode': 'library'}}, cookie) as response:
            result = json.load(response)
        self.assertTrue(result['rows'])
        self.assertGreaterEqual(len(result['rows'][0]['keywords']), 5)
        with self.request('/api/history', cookie=cookie) as response:
            self.assertEqual(len(json.load(response)['items']), 1)
        with self.request('/api/history') as response:
            self.assertEqual(json.load(response)['items'], [])
        with self.request('/api/history-export', cookie=cookie) as response:
            self.assertEqual(len(json.load(response)['runs']), 1)
        self.stop_server()
        self.start_server()
        with self.request('/api/history/' + result['id'], cookie=cookie) as response:
            self.assertEqual(json.load(response)['id'], result['id'])

    def test_import_excludes_duplicate_and_unknown_date_and_keeps_source(self):
        cookie, _ = self.bootstrap()
        note = {'url': 'https://www.xiaohongshu.com/explore/fixture201', 'title': '国泰行李额软件测试',
                'publishedAt': date_window()['end'], 'text': '仅供本地软件测试的固定内容'}
        unknown = {**note, 'url': 'https://www.xiaohongshu.com/explore/fixture202', 'publishedAt': ''}
        with self.request('/api/research', {'input': {'mode': 'import', 'topic': '行李额'},
                                          'notes': [note, note, unknown]}, cookie) as response:
            result = json.load(response)
        self.assertEqual(result['acceptedNotes'], 1)
        self.assertEqual(result['excluded']['duplicate'], 1)
        self.assertEqual(result['excluded']['date'], 1)
        self.assertEqual(result['rows'][0]['sources'][0]['url'], note['url'])

    def test_cross_origin_writes_and_private_files_are_rejected(self):
        cookie, _ = self.bootstrap()
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/research', {'input': {'mode': 'library'}}, cookie, 'https://untrusted.example')
        self.assertEqual(error.exception.code, 403)
        for path in ('/data/history.sqlite3', '/settings.local.json', '/catalog.json'):
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as error:
                self.request(path)
            self.assertEqual(error.exception.code, 404)

    def test_online_mode_without_login_does_not_create_fake_result(self):
        cookie, _ = self.bootstrap()
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/jobs', {'input': {'mode': 'online'}}, cookie)
        self.assertIn(error.exception.code, (403, 409, 503))
        with self.request('/api/history', cookie=cookie) as response:
            self.assertEqual(json.load(response)['items'], [])


if __name__ == '__main__':
    unittest.main()
