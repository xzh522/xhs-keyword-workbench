"""Lifecycle contracts; fixtures never log in or access a social platform."""
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from socai_login_browser import SocaiLoginBrowser
from socai_runtime import SocaiReader
from web_sampling import SamplingError


class LifecycleTests(unittest.TestCase):
    def test_windows_edge_uses_own_kill_on_close_group(self):
        browser = SocaiLoginBrowser()
        group = Mock()
        self.assertTrue(hasattr(browser, '_spawn_edge'), 'Edge ownership helper is missing')
        with patch('sys.platform', 'win32'), patch(
                'windows_processes.WindowsProcessGroup', return_value=group):
            result = browser._spawn_edge(['edge.exe', 'about:blank'])
        group.spawn.assert_called_once_with(['edge.exe', 'about:blank'])
        self.assertIs(result, group.spawn.return_value)

    def test_close_releases_owned_edge_group_before_removing_profile(self):
        browser = SocaiLoginBrowser()
        group, temporary = Mock(), Mock()
        order = []
        group.close.side_effect = lambda: order.append('group')
        temporary.cleanup.side_effect = lambda: order.append('profile')
        browser.edge_group = group
        browser.temporary = temporary
        browser.close()
        self.assertEqual(order, ['group', 'profile'])

    def test_windows_stopped_owned_daemon_removes_only_its_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            reader = SocaiReader(sys.executable, 'http://127.0.0.1:9222', Path(directory).resolve() / 'socai')
            endpoint = reader.home / 'rust-daemon-endpoint.json'
            endpoint.write_text(json.dumps({'host': '127.0.0.1', 'port': 43210, 'token': 'fixture-only'}))
            reader.daemon = Mock()
            reader.daemon.poll.return_value = 0
            reader.process_group = Mock()
            group = reader.process_group
            with patch('socai_runtime.sys.platform', 'win32'):
                reader._stop_daemon()
            self.assertFalse(endpoint.exists(), 'stale endpoint would prevent the next start')
            group.close.assert_called_once()

    def test_unknown_endpoint_is_not_removed_without_owned_daemon(self):
        with tempfile.TemporaryDirectory() as directory:
            reader = SocaiReader(sys.executable, 'http://127.0.0.1:9222', Path(directory).resolve() / 'socai')
            endpoint = reader.home / 'rust-daemon-endpoint.json'
            endpoint.write_text(json.dumps({'host': '127.0.0.1', 'port': 43210, 'token': 'fixture-only'}))
            with patch('socai_runtime.sys.platform', 'win32'):
                with self.assertRaises(SamplingError):
                    reader._stop_daemon()
            self.assertTrue(endpoint.exists())

    @unittest.skipUnless(sys.platform == 'win32', 'native Windows Job Object acceptance only')
    def test_native_windows_job_stops_its_owned_child(self):
        from windows_processes import WindowsProcessGroup
        group = WindowsProcessGroup()
        process = None
        try:
            process = group.spawn([sys.executable, '-c', 'import time; time.sleep(30)'],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
            self.assertIsNone(process.poll())
            group.close()
            process.wait(timeout=5)
            self.assertIsNotNone(process.poll())
        finally:
            group.close()
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
