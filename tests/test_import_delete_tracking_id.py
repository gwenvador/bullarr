from pathlib import Path
import unittest


class ImportDeleteTrackingIdTest(unittest.TestCase):
    def test_delete_payload_uses_pack_tracking_id_when_no_destination(self):
        source = (Path(__file__).resolve().parents[1] / 'static/js/import.js').read_text()
        expected = 'tracking_id: file.destination?.tracking_id ?? file.pack_download_id'
        self.assertEqual(source.count(expected), 2)

    def test_read_only_delete_marks_the_tracked_download_cancelled(self):
        source = (Path(__file__).resolve().parents[1] / 'blueprints/library/routes.py').read_text()
        start = source.index('def delete_import_file():')
        end = source.index('\n\n@library_bp.route', start)
        self.assertIn('mark_download_cancelled(tracking_id)', source[start:end])


class PendingOneShotPayloadTest(unittest.TestCase):
    def test_pending_download_payload_includes_oneshot_state(self):
        source = (Path(__file__).resolve().parents[1] / 'blueprints/missing_monitor/downloader.py').read_text()
        start = source.index("pending.append({")
        end = source.index("        return pending", start)
        self.assertIn("'is_oneshot': bool(is_oneshot)", source[start:end])


class ValidationBedethequeMatchTest(unittest.TestCase):
    def test_manual_review_match_uses_internal_request_context_not_unauthenticated_client(self):
        source = (Path(__file__).resolve().parents[1] / 'blueprints/bedetheque/auto_acquire.py').read_text()
        start = source.index('def match_manual_review_series(')
        end = source.index('\ndef get_auto_acquire_status', start)
        body = source[start:end]
        self.assertIn('test_request_context', body)
        self.assertIn('add_series_from_bedetheque', body)
        self.assertNotIn('test_client().post', body)

    def test_manual_review_match_also_links_existing_download(self):
        source = (Path(__file__).resolve().parents[1] / 'blueprints/bedetheque/auto_acquire.py').read_text()
        start = source.index('def match_manual_review_series(')
        end = source.index('\ndef get_auto_acquire_status', start)
        self.assertIn('UPDATE active_downloads', source[start:end])


if __name__ == '__main__':
    unittest.main()
