import unittest

from blueprints.library.scanner import LibraryScanner
from blueprints.missing_monitor.searcher import MissingVolumeSearcher


class PackRangeWithHsTest(unittest.TestCase):
    def test_range_with_trailing_hs_is_a_pack(self):
        for title in ('Les Pompiers - T01 Ã  15 et 2 HS - pdf', 'Les Pompiers - T01 à 15 + HS - pdf'):
            parsed = LibraryScanner.parse_filename(title)
            self.assertFalse(parsed['is_hs'], title)
            self.assertTrue(parsed['is_integral'], title)
            self.assertEqual((parsed['integral_tome_start'], parsed['integral_tome_end']), (1, 15))
            self.assertEqual(parsed['title'], 'Les Pompiers')

    def test_single_hs_is_still_hs(self):
        parsed = LibraryScanner.parse_filename('Les Pompiers - HS 2 - pdf')
        self.assertTrue(parsed['is_hs'])
        self.assertEqual(parsed['hs_number'], 2)

    def test_search_label_shows_range(self):
        self.assertEqual(
            MissingVolumeSearcher._parsed_volume_label('Les Pompiers - T01 Ã  15 et 2 HS - pdf'),
            'T01 à T15',
        )


if __name__ == '__main__':
    unittest.main()
