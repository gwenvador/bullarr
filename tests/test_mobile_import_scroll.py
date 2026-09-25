import unittest
from pathlib import Path


class MobileImportScrollTest(unittest.TestCase):
    def test_import_table_has_only_one_horizontal_scroll_owner(self):
        root = Path(__file__).resolve().parents[1]
        css = (root / 'static/css/style.css').read_text()
        container_start = css.index('#import-files-container {')
        container_rule = css[container_start:css.index('}', container_start)]
        self.assertIn('overflow-x: hidden', container_rule)
        self.assertNotIn('overflow-x: auto', container_rule)
        table_start = css.index('.import-files-table {')
        table_rule = css[table_start:css.index('}', table_start)]
        self.assertIn('display: table', table_rule)
        self.assertIn('overflow: visible', table_rule)
        nav = (root / 'static/js/nav.js').read_text()
        selectors_start = nav.index('const selectors = [')
        selectors_end = nav.index('];', selectors_start)
        selectors = nav[selectors_start:selectors_end]
        self.assertNotIn("'.import-files-scroll',", selectors)
        self.assertIn('div[style*="overflow-x:auto"]:not(.import-files-scroll)', selectors)
        self.assertNotIn('.import-files-scroll::-webkit-scrollbar', css)


if __name__ == '__main__':
    unittest.main()
