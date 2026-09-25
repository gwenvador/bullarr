import unittest
from pathlib import Path


class ImportTerminalFolderVisibilityTest(unittest.TestCase):
    def test_import_scan_keeps_terminal_download_for_unfinalized_siblings(self):
        source = (Path(__file__).resolve().parents[1] / 'blueprints/library/routes.py').read_text()
        marker = 'trackable_downloads = get_trackable_active_downloads(include_terminal=True)'
        self.assertIn(marker, source)
        self.assertIn('_terminal_download_has_finalized_sibling', source)
        self.assertIn("download.get('download_status') in ('imported', 'skipped')", source)


if __name__ == '__main__':
    unittest.main()
