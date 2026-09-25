import unittest
from pathlib import Path


class MobileImportScrollTest(unittest.TestCase):
    def test_import_file_container_has_only_inner_horizontal_scroll(self):
        css = (Path(__file__).resolve().parents[1] / 'static/css/style.css').read_text()
        start = css.index('#import-files-container {')
        end = css.index('}', start)
        rule = css[start:end]
        self.assertIn('overflow-x: hidden', rule)
        self.assertNotIn('overflow-x: auto', rule)


if __name__ == '__main__':
    unittest.main()
