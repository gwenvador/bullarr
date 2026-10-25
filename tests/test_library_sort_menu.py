import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LibrarySortMenuTest(unittest.TestCase):
    """Menu « Trier » des vues affiches et aperçu de la bibliothèque."""

    def setUp(self):
        self.template = (ROOT / 'templates/library.html').read_text()
        self.script = (ROOT / 'static/js/library.js').read_text()

    def test_menu_offers_every_sort_mode_defined_in_the_script(self):
        modes = re.search(r"const SERIES_SORT_MODES = \{(.*?)\n\};", self.script, re.S).group(1)
        defined = set(re.findall(r"^\s{4}(\w+): \{ label", modes, re.M))
        self.assertEqual(defined, {'alpha', 'votes', 'added', 'year', 'size', 'volumes'})
        offered = set(re.findall(r'data-sort="(\w+)" onclick="setSeriesSortMode\(', self.template))
        self.assertEqual(offered, defined)

    def test_menu_is_hidden_in_table_view_which_sorts_by_column_headers(self):
        start = self.script.index('function updateViewSwitcherButtons(')
        body = self.script[start:self.script.index('function displaySeries(', start)]
        self.assertIn("series-sort-section", body)
        self.assertIn("seriesViewMode === 'table' ? 'none' : ''", body)

    def test_missing_values_stay_last_and_same_mode_reverses(self):
        start = self.script.index('function sortSeriesList(')
        body = self.script[start:self.script.index('// Retire les menus', start)]
        self.assertIn('toujours à la fin', body)
        setter = self.script[self.script.index('function setSeriesSortMode('):]
        self.assertIn('mode === seriesSortMode ? !seriesSortReversed : false', setter)

    def test_sort_direction_is_remembered_per_library(self):
        self.assertIn("getFilterStorageKey('sortReversed')", self.script)


if __name__ == '__main__':
    unittest.main()
