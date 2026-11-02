import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SearchResultDateTest(unittest.TestCase):
    def test_every_source_carries_its_date_to_the_results(self):
        searcher = (ROOT / 'blueprints/missing_monitor/searcher.py').read_text()
        self.assertIn("'date': row[8],", searcher)
        self.assertIn("'date': f.get('message_date'),", searcher)
        self.assertIn('volume, date_scraped', searcher)

    def test_results_table_has_a_sortable_date_column(self):
        script = (ROOT / 'static/js/search-results-table.js').read_text()
        self.assertIn("_searchResultsSortableLabelHtml('date', 'Date')", script)
        self.assertIn("case 'date': return _searchResultDate(result) || 0;", script)
        self.assertIn("_searchResultDateHtml(result)", script)
        self.assertIn('colspan="12"', script)
        self.assertNotIn('colspan="11"', script)

    def test_date_is_the_first_column_and_has_no_first_seen_label(self):
        script = (ROOT / 'static/js/search-results-table.js').read_text()
        header = script.index('replace-results-select-header')
        self.assertLess(header, script.index("_searchResultsSortableLabelHtml('date', 'Date')"))
        self.assertLess(script.index("_searchResultsSortableLabelHtml('date', 'Date')"), script.index('>Actions</th>'))
        start = script.index('function _searchResultDateHtml(')
        body = script[start:script.index('\n}\n', start)]
        self.assertNotIn('vu le', body)


if __name__ == '__main__':
    unittest.main()
