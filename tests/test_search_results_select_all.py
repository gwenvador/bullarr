import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SearchResultsSelectAllTest(unittest.TestCase):
    """« Tout sélectionner » des résultats de recherche (fiche série et Découvrir)."""

    def setUp(self):
        self.table = (ROOT / 'static/js/search-results-table.js').read_text()
        self.library = (ROOT / 'static/js/library.js').read_text()

    def test_series_search_modal_binds_selection_controls(self):
        start = self.library.index('searchModalBody.innerHTML = html;')
        self.assertIn('bindSearchResultsTableControls(searchModalBody)', self.library[start:start + 300])

    def test_inline_handlers_are_not_bound_twice(self):
        start = self.table.index('function bindSearchResultsTableActions(')
        body = self.table[start:self.table.index('function bindSearchResultsTableControls(', start)]
        self.assertIn("if (control.hasAttribute('onclick')) return;", body)
        self.assertLess(body.index("hasAttribute('onclick')"), body.index("addEventListener('click'"))

    def test_select_all_takes_only_downloadable_rows(self):
        start = self.table.index('function _selectableSearchResultCheckboxes(')
        body = self.table[start:self.table.index('function toggleAllSearchResults(', start)]
        self.assertIn('.search-result-download-action:not([disabled])', body)
        self.assertNotIn('replace-results-row-owned', body)
        toggle = self.table[self.table.index('function toggleAllSearchResults('):]
        self.assertIn('_selectableSearchResultCheckboxes()', toggle[:400])

    def test_vk_download_action_is_dispatched(self):
        self.assertRegex(self.table, re.compile(r"case 'downloadVk': return downloadVkResult\("))


if __name__ == '__main__':
    unittest.main()
