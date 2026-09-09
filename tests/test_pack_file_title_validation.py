import unittest

from blueprints.library.routes import _pack_file_matches_destination


class PackFileTitleValidationTests(unittest.TestCase):
    def test_core_series_file_matches_exact_destination_title(self):
        self.assertTrue(_pack_file_matches_destination(
            {'title': 'Thorgal', 'volume': 20},
            {'series_title': 'Thorgal'},
        ))

    def test_spinoff_file_does_not_match_shorter_parent_series_title(self):
        self.assertFalse(_pack_file_matches_destination(
            {'title': 'Thorgal Saga', 'volume': 2},
            {'series_title': 'Thorgal'},
        ))

    def test_unparsed_pack_file_is_not_auto_importable(self):
        self.assertFalse(_pack_file_matches_destination(
            {'title': None, 'volume': None},
            {'series_title': 'Thorgal'},
        ))


if __name__ == '__main__':
    unittest.main()
