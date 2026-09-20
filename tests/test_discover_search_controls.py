from pathlib import Path
import unittest


class DiscoverSearchResultsControlsTests(unittest.TestCase):
    def test_sanitized_source_results_rebind_sort_and_filter_controls(self):
        table = Path('static/js/search-results-table.js').read_text()
        discover = Path('static/js/discover.js').read_text()
        self.assertIn('function bindSearchResultsTableControls', table)
        self.assertIn('bindSearchResultsTableControls(sourcesResultsList)', discover)
        self.assertIn('data-sort-column="${column}"', table)
        self.assertIn('data-filter-field="', table)


if __name__ == '__main__':
    unittest.main()
