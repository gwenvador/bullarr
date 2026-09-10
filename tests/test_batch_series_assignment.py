import unittest
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


@unittest.skipUnless(sync_playwright, 'Playwright is required for this regression')
class BatchSeriesAssignmentTests(unittest.TestCase):
    def test_bulk_series_assign_includes_selected_files_with_or_without_destination(self):
        source = Path('static/js/import.js').read_text()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path='/usr/bin/google-chrome')
            page = browser.new_page()
            page.set_content('<div id="import-bulk-assign-bar"></div>')
            page.add_script_tag(content=source)
            page.evaluate("window.pluralize = (count, word) => count === 1 ? word : `${word}s`; window.svgIcon = () => ''; ")
            result = page.evaluate('''() => {
                importFiles = [
                    { filename: 'already-linked.cbz', destination: { series_id: 810 }, selected: true, validation_error: 'Archive CBZ corrompue' },
                    { filename: 'needs-review.cbz', destination: null, _bulkSelected: true },
                    { filename: 'not-selected.cbz', destination: { series_id: 811 }, selected: false },
                ];
                _updateImportBulkAssignBar();
                return {
                    indices: _selectedFileIndicesForBulkAssignment(),
                    text: document.getElementById('import-bulk-assign-bar').textContent,
                };
            }''')
            browser.close()

        self.assertEqual([0, 1], result['indices'])
        self.assertIn('2 fichiers sélectionnés', result['text'])

    def test_clearing_bulk_assignment_selection_clears_already_assigned_files_too(self):
        source = Path('static/js/import.js').read_text()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path='/usr/bin/google-chrome')
            page = browser.new_page()
            page.set_content('<div id="import-bulk-assign-bar"></div>')
            page.add_script_tag(content=source)
            result = page.evaluate('''() => {
                window.displayImportFiles = () => {};
                importFiles = [
                    { filename: 'already-linked.cbz', destination: { series_id: 810 }, selected: true },
                    { filename: 'needs-review.cbz', destination: null, _bulkSelected: true },
                ];
                clearUnassignedSelection();
                return _selectedFileIndicesForBulkAssignment();
            }''')
            browser.close()

        self.assertEqual([], result)


if __name__ == '__main__':
    unittest.main()
