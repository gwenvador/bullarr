import unittest

from blueprints.library.scanner import LibraryScanner


class OneShotAlbumNumberParsingTests(unittest.TestCase):
    def test_os_suffix_disables_explicit_volume_parsing(self):
        parsed = LibraryScanner.parse_filename("Example_T02_OS_Artist_2020.cbr")

        self.assertIsNone(parsed["volume"])
        self.assertIn("T02", parsed["title"])

    def test_os_album_title_number_is_not_a_volume(self):
        parsed = LibraryScanner.parse_filename(
            "Île_des_Justes_L'_Corse,_Été_42_OS_Piatzszek_Espé_2015_.cbr"
        )

        self.assertIsNone(parsed["volume"])
        self.assertIn("Été 42", parsed["title"])
        self.assertNotIn(" OS", parsed["title"])
        self.assertNotIn("2015", parsed["title"])


if __name__ == "__main__":
    unittest.main()
