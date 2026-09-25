import unittest
from pathlib import Path


class MobileImportScrollTest(unittest.TestCase):
    def test_import_file_container_has_only_inner_horizontal_scroll(self):
        root = Path(__file__).resolve().parents[1]
        css = (root / 'static/css/style.css').read_text()
        start = css.index('#import-files-container {')
        end = css.index('}', start)
        rule = css[start:end]
        self.assertIn('overflow-x: hidden', rule)
        self.assertNotIn('overflow-x: auto', rule)
        nav = (root / 'static/js/nav.js').read_text()
        selectors_start = nav.index('const selectors = [')
        selectors_end = nav.index('];', selectors_start)
        selectors = nav[selectors_start:selectors_end]
        self.assertNotIn("'.import-files-scroll',", selectors)
        self.assertIn('div[style*="overflow-x:auto"]:not(.import-files-scroll)', selectors)
        self.assertNotIn('.import-files-scroll::-webkit-scrollbar', css)


if __name__ == '__main__':
    unittest.main()
