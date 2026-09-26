from pathlib import Path
import unittest


class ImportDeleteTrackingIdTest(unittest.TestCase):
    def test_delete_payload_uses_pack_tracking_id_when_no_destination(self):
        source = (Path(__file__).resolve().parents[1] / 'static/js/import.js').read_text()
        expected = 'tracking_id: file.destination?.tracking_id ?? file.pack_download_id'
        self.assertEqual(source.count(expected), 2)

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


class ImportManualReviewPresentationTest(unittest.TestCase):
    def test_initial_import_load_populates_known_volumes_before_rendering(self):
        source = (Path(__file__).resolve().parents[1] / 'static/js/import.js').read_text()
        start = source.index('async function loadActiveDownloads()')
        end = source.index('function renderCurrentlyProcessingBanner()', start)
        body = source[start:end]
        self.assertIn('await _ensureVolumesLoadedForFiles(importFiles);', body)
        self.assertLess(body.index('await _ensureVolumesLoadedForFiles(importFiles);'), body.index('displayImportFiles();'))


class ImportVolumeOwnershipPresentationTest(unittest.TestCase):
    def test_import_volume_select_loads_all_catalogue_entries(self):
        source = (Path(__file__).resolve().parents[1] / 'static/js/import.js').read_text()
        start = source.index('async function _ensureSeriesVolumesLoaded(seriesId)')
        end = source.index('// Précharge en une fois', start)
        cache_loader = source[start:end]
        self.assertIn('fetch(`/api/series/${seriesId}/volumes`)', cache_loader)
        self.assertNotIn('existing_only', cache_loader)

class SearchResultsBatchDownloadTest(unittest.TestCase):
    def test_telegram_download_rows_are_eligible_for_batch_download(self):
        source = (Path(__file__).resolve().parents[1] / 'static/js/search-results-table.js').read_text()
        start = source.index('} else if (isTelegram)')
        end = source.index('} else if (isFourtoutici)', start)
        telegram = source[start:end]
        self.assertIn('downloadTelegramFile', telegram)
        self.assertIn('search-result-download-action', telegram)
    def test_search_rows_have_selectors_and_a_batch_download_action(self):
        source = (Path(__file__).resolve().parents[1] / 'static/js/search-results-table.js').read_text()
        self.assertIn('class=\"search-result-select\"', source)
        self.assertIn('function downloadSelectedSearchResults()', source)
        self.assertIn('search-result-download-action', source)

    def test_search_rerender_restores_checked_rows_from_stable_selection_state(self):
        source = (Path(__file__).resolve().parents[1] / 'static/js/search-results-table.js').read_text()
        self.assertIn("_searchTableSelectedKeys.has(_searchResultSelectionKey(result))", source)
        self.assertIn('checked=""', source)
        self.assertIn("_updateSearchBatchDownloadToolbar();", source)

    def test_batch_download_uses_only_checked_visible_rows_and_can_reset(self):
        source = (Path(__file__).resolve().parents[1] / 'static/js/search-results-table.js').read_text()
        start = source.index('function downloadSelectedSearchResults()')
        end = source.index('\n}', start) + 2
        body = source[start:end]
        self.assertIn("querySelectorAll('#search-results-tbody .search-result-select:checked')", body)
        self.assertIn("closest('tr')", body)
        self.assertIn(".search-result-download-action", body)


if __name__ == '__main__':
    unittest.main()
