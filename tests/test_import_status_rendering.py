import unittest
from pathlib import Path


class ImportStatusRenderingTest(unittest.TestCase):
    def setUp(self):
        self.source = (Path(__file__).resolve().parents[1] / 'static/js/import.js').read_text()

    def test_only_claimed_files_are_rendered_as_importing_rows_first(self):
        helper = self.source[self.source.index('function _isFileImportingNow'):self.source.index('function _importFileRowHtml')]
        self.assertIn('importingFilepaths.has(file.filepath)', helper)
        self.assertNotIn('willAutoImportItself', helper)
        self.assertNotIn('anyImportInProgress', helper)
        display = self.source[self.source.index('function displayImportFiles'): ]
        self.assertIn('const importingRowsHtml = importingEntries.map', display)
        self.assertIn('const mainRowsHtml = importingRowsHtml +', display)

    def test_importing_badge_precedes_completed_ready_badge(self):
        row = self.source[self.source.index('function _importFileRowHtml'):self.source.index('function toggleUnassignedSelection')]
        self.assertLess(row.index(': isImportingNow'), row.index(": file.destination?.download_status === 'completed'"))
        self.assertIn('Import en cours', row)
        self.assertIn('attente validation manuelle de la série', self.source)


if __name__ == '__main__':
    unittest.main()
