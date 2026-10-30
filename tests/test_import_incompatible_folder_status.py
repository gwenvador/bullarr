import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class IncompatibleFolderStatusTest(unittest.TestCase):
    """Un téléchargement terminé qui est un dossier d'images en vrac ne doit pas annoncer
    « import automatique sous peu »."""

    def setUp(self):
        self.script = (ROOT / 'static/js/import.js').read_text()

    def test_row_shows_error_before_the_success_message(self):
        start = self.script.index('function _pendingDownloadRowHtml(')
        body = self.script[start:self.script.index('return `\n        <tr', start)]
        self.assertIn('_pendingIncompatibleFolder(pending)', body)
        self.assertLess(body.index('_pendingIncompatibleFolder(pending)'),
                        body.index('import automatique sous peu'))

    def test_helper_links_folder_to_download_and_ignores_importable_files(self):
        start = self.script.index('function _pendingIncompatibleFolder(')
        helper = self.script[start:self.script.index('function _pendingDownloadRowHtml(', start)]
        self.assertIn('f.pack_download_id === pending.id', helper)
        self.assertIn("pending.status !== 'completed'", helper)
        self.assertIn('hasImportable', helper)
        self.assertIn('import-download-error', helper)


if __name__ == '__main__':
    unittest.main()
