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


class PendingOneShotDisplayTest(unittest.TestCase):
    def test_pending_oneshot_renders_one_shot_in_volume_column(self):
        source = (Path(__file__).resolve().parents[1] / 'static/js/import.js').read_text()
        start = source.index('function _pendingVolumeLabel(pending)')
        end = source.index('\n}', start) + 2
        self.assertIn("if (pending.is_oneshot) return 'One Shot';", source[start:end])


class ActiveOneShotDisplayPayloadTest(unittest.TestCase):
    def test_linked_client_download_carries_oneshot_to_the_shared_volume_renderer(self):
        downloader = (Path(__file__).resolve().parents[1] / 'blueprints/missing_monitor/downloader.py').read_text()
        activity = (Path(__file__).resolve().parents[1] / 'blueprints/activity/routes.py').read_text()
        self.assertIn('s.is_oneshot', downloader[downloader.index('def find_active_downloads_by_client_item_ids'):])
        self.assertIn("'is_oneshot': bool(is_oneshot)", downloader[downloader.index('def find_active_downloads_by_client_item_ids'):])
        self.assertIn("item['is_oneshot'] = linked.get('is_oneshot', False)", activity)


class PendingOneShotPayloadTest(unittest.TestCase):
    def test_pending_download_payload_includes_oneshot_state(self):
        source = (Path(__file__).resolve().parents[1] / 'blueprints/missing_monitor/downloader.py').read_text()
        start = source.index("pending.append({")
        end = source.index("        return pending", start)
        self.assertIn("'is_oneshot': bool(is_oneshot)", source[start:end])


class ValidationBedethequeMatchTest(unittest.TestCase):
    def test_matching_only_verifies_and_resolves_the_review(self):
        source = (Path(__file__).resolve().parents[1] / 'blueprints/bedetheque/auto_acquire.py').read_text()
        start = source.index('def match_manual_review_series(')
        end = source.index('\ndef get_auto_acquire_status', start)
        body = source[start:end]
        self.assertIn("SET status = 'resolved', resolved_at = CURRENT_TIMESTAMP", body)
        self.assertNotIn('add_series_from_bedetheque', body)
        self.assertNotIn('UPDATE active_downloads', body)


class ImportManualReviewPresentationTest(unittest.TestCase):
    def test_wrong_series_message_requires_verification_then_manual_import(self):
        source = (Path(__file__).resolve().parents[1] / 'blueprints/library/routes.py').read_text()
        self.assertIn('vérifiez que c’est la bonne série et importez manuellement', source)

    def test_initial_import_load_populates_known_volumes_before_rendering(self):
        source = (Path(__file__).resolve().parents[1] / 'static/js/import.js').read_text()
        start = source.index('async function loadActiveDownloads()')
        end = source.index('function renderCurrentlyProcessingBanner()', start)
        body = source[start:end]
        self.assertIn('await _ensureVolumesLoadedForFiles(importFiles);', body)
        self.assertLess(body.index('await _ensureVolumesLoadedForFiles(importFiles);'), body.index('displayImportFiles();'))


class ImportVolumeOwnershipPresentationTest(unittest.TestCase):
    def test_dropdown_checkmark_uses_live_file_existence_not_a_stale_filepath(self):
        routes = (Path(__file__).resolve().parents[1] / 'blueprints/library/routes.py').read_text()
        nav = (Path(__file__).resolve().parents[1] / 'static/js/nav.js').read_text()
        start = routes.index('def get_series_volumes(series_id):')
        end = len(routes)
        self.assertIn("entry['is_owned'] = bool(entry.get('filepath') and os.path.isfile(entry['filepath']))", routes[start:end])
        self.assertIn('if (showOwned && v.is_owned)', nav)

    def test_stale_local_volume_without_a_catalogue_album_is_not_returned(self):
        routes = (Path(__file__).resolve().parents[1] / 'blueprints/library/routes.py').read_text()
        fallback_start = routes.index('for v in owned_volumes:', routes.index('# Tomes possédés mais absents'))
        fallback_end = routes.index('    return jsonify(merged)', fallback_start)
        fallback = routes[fallback_start:fallback_end]
        self.assertIn("if not v.get('is_owned'):", fallback)
        self.assertIn('continue', fallback)


if __name__ == '__main__':
    unittest.main()
