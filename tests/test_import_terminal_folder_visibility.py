import unittest
from pathlib import Path


class ImportTerminalFolderVisibilityTest(unittest.TestCase):
    def test_import_scan_keeps_unfinalized_files_from_terminal_downloads(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / 'blueprints/library/routes.py').read_text()
        self.assertIn('trackable_downloads = get_trackable_active_downloads(include_terminal=True)', source)
        self.assertIn('_terminal_download_has_finalized_sibling', source)
        self.assertIn("download.get('download_status') in ('imported', 'skipped')", source)
        self.assertNotIn('terminal_download = next(', source)


if __name__ == '__main__':
    unittest.main()
