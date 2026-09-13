import unittest

from blueprints.library.scanner import LibraryScanner


class ScannerQualityYearTests(unittest.TestCase):
    def test_boule_et_bill_t37_metadata(self):
        parsed = LibraryScanner.parse_filename("Boule et Bill T37 (2016) (Dargaud) [1200px] (PRESSECiTRON).cbz")
        self.assertEqual(parsed["volume"], 37)
        self.assertEqual(parsed["year"], 2016)
        self.assertEqual(parsed["resolution"], "1200px")

    def test_boule_et_bill_t38_metadata(self):
        parsed = LibraryScanner.parse_filename("Boule et Bill T38 (2017) (Dargaud) [1920px] (PRiNTER).cbz")
        self.assertEqual(parsed["volume"], 38)
        self.assertEqual(parsed["year"], 2017)
        self.assertEqual(parsed["resolution"], "1920px")


if __name__ == "__main__":
    unittest.main()
