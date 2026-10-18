import unittest
from unittest import mock

from flask import Flask

from blueprints.activity import routes
from blueprints.missing_monitor import downloader


PENDING_ROW = {
    'id': 7, 'status': 'pending', 'client': 'amule', 'client_item_id': 'abc', 'is_pack': 0,
}


class AmuleAbsenceSnapshotTest(unittest.TestCase):
    """Un téléchargement aMule ne passe en échec qu'après des absences constatées sur des
    relevés amulecmd DISTINCTS, et un relevé en cache n'est jamais modifié par la route."""

    def setUp(self):
        routes._amule_status_cache.update({'result': None, 'fetched_at': 0.0})
        routes._absence_counted_snapshots.clear()
        self.app = Flask(__name__)
        self.shared = set()
        self.uncached_calls = 0
        self.streak = 0
        self.increment = mock.Mock(side_effect=self._increment)
        self.mark_failed = mock.Mock()
        self.mark_completed = mock.Mock()
        patches = [
            mock.patch('blueprints.emule.routes.load_emule_config', return_value={'enabled': True}),
            mock.patch.object(routes, '_amule_status_uncached', side_effect=self._uncached),
            mock.patch.object(downloader, 'get_pending_downloads', return_value=[dict(PENDING_ROW)]),
            mock.patch.object(downloader, 'find_active_downloads_by_client_item_ids', return_value={}),
            mock.patch.object(downloader, 'increment_client_absence_streak', self.increment),
            mock.patch.object(downloader, 'reset_client_absence_streak', mock.Mock()),
            mock.patch.object(downloader, 'mark_download_failed', self.mark_failed),
            mock.patch.object(downloader, 'mark_download_completed', self.mark_completed),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _uncached(self, config):
        self.uncached_calls += 1
        return {'client': 'amule', 'label': 'aMule', 'error': None, 'items': [],
                'shared_hashes': set(self.shared)}

    def _increment(self, download_id):
        self.streak += 1
        return self.streak

    def _poll(self):
        with self.app.test_request_context('/api/activity/status'):
            return routes.activity_status().get_json()

    def _expire_cache(self):
        routes._amule_status_cache['fetched_at'] = 0.0

    def test_polls_on_the_same_cached_snapshot_count_one_absence(self):
        for _ in range(5):
            self._poll()
        self.assertEqual(self.uncached_calls, 1)
        self.assertEqual(self.increment.call_count, 1)
        self.mark_failed.assert_not_called()

    def test_failure_needs_threshold_distinct_snapshots(self):
        threshold = downloader.CLIENT_ABSENCE_FAILURE_THRESHOLD
        for _ in range(threshold - 1):
            self._poll()
            self._poll()
            self._expire_cache()
        self.mark_failed.assert_not_called()
        self._poll()
        self.assertEqual(self.increment.call_count, threshold)
        self.mark_failed.assert_called_once_with(7)

    def test_completed_download_stays_completed_on_cached_polls(self):
        self.shared = {'abc'}
        for _ in range(4):
            self._poll()
        self.mark_completed.assert_called_once_with(7)
        self.increment.assert_not_called()
        # Le relevé en cache garde ses fichiers partagés: la route travaille sur une copie.
        self.assertEqual(routes._amule_status_cache['result']['shared_hashes'], {'abc'})

    def test_response_does_not_expose_internal_fields(self):
        payload = self._poll()
        amule = next(c for c in payload['clients'] if c['client'] == 'amule')
        self.assertNotIn('snapshot_id', amule)
        self.assertNotIn('shared_hashes', amule)


if __name__ == '__main__':
    unittest.main()
