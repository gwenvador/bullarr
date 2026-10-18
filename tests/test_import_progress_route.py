import unittest
from pathlib import Path
from unittest import mock

from flask import Flask

from blueprints.library import import_history, routes


class ImportProgressRouteTest(unittest.TestCase):
    """GET /api/import/progress: état d'import léger sondé par /import toutes les 5 s."""

    def setUp(self):
        self.app = Flask(__name__)

    def test_returns_claimed_files_processing_file_and_pause(self):
        processing = {'file_id': 3, 'filename': 'Alix - T12.cbz', 'elapsed_seconds': 4}
        with mock.patch.object(import_history, 'get_claimed_import_filepaths', return_value=['/imports/Alix - T12.cbz']), \
                mock.patch.object(import_history, 'get_currently_processing_file', return_value=processing), \
                mock.patch.object(routes.import_control, 'is_paused', return_value=False):
            with self.app.test_request_context('/api/import/progress'):
                payload = routes.import_progress().get_json()
        self.assertEqual(payload, {
            'success': True,
            'importing_filepaths': ['/imports/Alix - T12.cbz'],
            'currently_processing': processing,
            'import_paused': False,
        })

    def test_route_never_scans_disk_or_probes_clients(self):
        source = Path('blueprints/library/routes.py').read_text()
        route = source[source.index('def import_progress():'):source.index("@library_bp.route('/api/import/state'")]
        self.assertNotIn('_scan_tracked_import_files', route)
        self.assertNotIn('activity_status', route)

    def test_import_page_polls_progress_and_reloads_on_change(self):
        source = Path('static/js/import.js').read_text()
        start = source.index('async function pollImportProgress')
        poll = source[start:source.index('setInterval(pollImportProgress', start)]
        self.assertIn("fetch('/api/import/progress')", poll)
        self.assertIn('await loadActiveDownloads()', poll)
        self.assertIn('setInterval(pollImportProgress, IMPORT_PROGRESS_POLL_MS)', source)


if __name__ == '__main__':
    unittest.main()
