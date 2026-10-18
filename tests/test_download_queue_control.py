import threading
import time
import unittest
from unittest import mock

from flask import Flask

from blueprints.activity import routes
from blueprints.telegram_channels import scraper


class TelegramQueueOrderTest(unittest.TestCase):
    """File Telegram: scrape/recherche d'abord, puis priorité décroissante, puis ordre
    d'arrivée; un téléchargement en pause n'empêche pas les suivants de passer."""

    def _run_waiters(self, queue, waiters):
        order = []
        queue.acquire(None)  # occupe la file le temps que tout le monde arrive
        threads = []
        for download_id in waiters:
            thread = threading.Thread(target=self._take, args=(queue, download_id, order))
            thread.start()
            threads.append(thread)
            time.sleep(0.02)
        queue.release()
        for thread in threads:
            thread.join(timeout=2)
        return order, threads

    @staticmethod
    def _take(queue, download_id, order):
        queue.acquire(download_id)
        order.append(download_id)
        queue.release()

    def test_priority_then_arrival_and_scrapes_first(self):
        queue = scraper._TelegramQueue()
        for download_id in (1, 2, 3):
            queue.update(download_id, priority=0, paused=False)
        queue.update(3, priority=5)
        order, _ = self._run_waiters(queue, [1, 2, 3, None])
        self.assertEqual(order, [None, 3, 1, 2])

    def test_paused_download_waits_without_blocking_others(self):
        queue = scraper._TelegramQueue()
        queue.update(1, priority=9, paused=True)
        queue.update(2, priority=0, paused=False)
        order, threads = self._run_waiters(queue, [1, 2])
        self.assertEqual(order, [2])
        self.assertTrue(threads[0].is_alive())
        queue.update(1, paused=False)
        threads[0].join(timeout=2)
        self.assertEqual(order, [2, 1])


class DownloadControlRouteTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)

    def _post(self, payload):
        with self.app.test_request_context('/api/activity/download-control', method='POST', json=payload):
            response = routes.download_control()
        if isinstance(response, tuple):
            return response[1], response[0].get_json()
        return 200, response.get_json()

    def test_unknown_action_is_rejected(self):
        status, payload = self._post({'client': 'amule', 'id': 'a' * 32, 'action': 'delete'})
        self.assertEqual(status, 400)
        self.assertFalse(payload['success'])

    def test_amule_commands(self):
        calls = []

        def fake_run(cmd, input, **kwargs):
            calls.append(input)
            return mock.Mock(returncode=0, stderr='')

        config = {'enabled': True, 'host': 'h', 'ec_port': 4712, 'password_decrypted': 'p'}
        with mock.patch('blueprints.emule.routes.load_emule_config', return_value=config), \
                mock.patch.object(routes.subprocess, 'run', side_effect=fake_run):
            for action in ('top', 'pause', 'resume'):
                status, payload = self._post({'client': 'amule', 'id': 'A' * 32, 'action': action})
                self.assertEqual((status, payload['success']), (200, True))
        self.assertEqual(calls, [f'priority high {"A" * 32}\n', f'pause {"A" * 32}\n', f'resume {"A" * 32}\n'])

    def test_amule_rejects_invalid_hash(self):
        status, payload = self._post({'client': 'amule', 'id': 'abc; shutdown', 'action': 'pause'})
        self.assertEqual(status, 400)
        self.assertFalse(payload['success'])

    def test_qbittorrent_uses_v5_endpoints_with_v4_fallback(self):
        session = mock.Mock()
        posted = []

        def fake_post(url, data, **kwargs):
            endpoint = url.rsplit('/', 1)[1]
            posted.append(endpoint)
            return mock.Mock(status_code=404 if endpoint == 'stop' else 200)

        session.post.side_effect = fake_post
        with mock.patch('blueprints.qbittorrent.routes.load_qbittorrent_config', return_value={'enabled': True}), \
                mock.patch('blueprints.qbittorrent.routes.create_qbittorrent_session', return_value=(session, 'http://qb', None)):
            for action in ('top', 'pause', 'resume'):
                status, payload = self._post({'client': 'qbittorrent', 'id': 'b' * 40, 'action': action})
                self.assertEqual((status, payload['success']), (200, True))
        self.assertEqual(posted, ['topPrio', 'stop', 'pause', 'start'])

    def test_rtorrent_is_not_supported(self):
        status, payload = self._post({'client': 'rtorrent', 'id': 'c' * 40, 'action': 'pause'})
        self.assertEqual(status, 400)
        self.assertFalse(payload['success'])


if __name__ == '__main__':
    unittest.main()
