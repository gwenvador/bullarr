import importlib
import sys
import unittest
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKTREE))


class VerificationKomgaDuplicatesTest(unittest.TestCase):
    def test_reports_two_komga_series_with_same_normalized_title(self):
        routes = importlib.import_module("blueprints.settings.routes")
        local_series = [{"id": 405, "title": "Blueberry (La Jeunesse de)", "path": "/BD/Blueberry (La Jeunesse de)"}]
        komga_series = [
            {"komga_series_id": "komga-a", "title": "Blueberry (La Jeunesse de)", "url": "https://komga/series/komga-a"},
            {"komga_series_id": "komga-b", "title": "Blueberry (La Jeunesse de)", "url": "https://komga/series/komga-b"},
        ]
        result = routes._verify_duplicate_komga_series(local_series, komga_series)
        self.assertEqual(1, len(result))
        self.assertTrue(result[0]["cleanup_eligible"])
        self.assertEqual("Blueberry (La Jeunesse de)", result[0]["duplicate_series_title"])
        self.assertEqual(2, len(result[0]["komga_series"]))


if __name__ == "__main__":
    unittest.main()
