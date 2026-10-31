import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LibraryTableMobileScrollTest(unittest.TestCase):
    def test_wrapper_is_the_only_horizontal_scroller_on_small_screens(self):
        css = (ROOT / 'static/css/style-library-search.css').read_text()
        block = css[css.rindex('@media (max-width: 768px)'):]
        self.assertIn('.series-list.series-table-wrapper .series-table:not(.series-table-compact)', block)
        self.assertIn('display: table;', block)
        self.assertIn('overflow: visible;', block)


    def test_mirror_scrollbar_follows_the_table_currently_in_the_wrapper(self):
        script = (ROOT / 'static/js/nav.js').read_text()
        start = script.index('function attach(wrapper)')
        body = script[start:script.index('function scan(', start)]
        self.assertIn("wrapper.querySelector('table')", body)
        self.assertIn('new MutationObserver(observeCurrentTable).observe(wrapper', body)
        self.assertNotIn('observe(table)', body)


if __name__ == '__main__':
    unittest.main()
