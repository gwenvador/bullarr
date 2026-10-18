import unittest
from unittest.mock import patch

from blueprints.ebdz import routes


class BedethequeCatalogMatchTest(unittest.TestCase):
    def setUp(self):
        routes._CATALOG_MATCH_CACHE.clear()

    def test_release_title_finds_catalog_series_and_caches_it(self):
        candidates = [{'title': 'Chagrin', 'url': 'https://www.bedetheque.com/serie-1-BD-Chagrin.html'}]
        with patch('blueprints.bedetheque.catalog_index.search_catalog_index', return_value=candidates) as search:
            first = routes.bedetheque_catalog_match('Chagrin.Rodolphe.Griffo.2026.FR.[PDF]-NOTAG.pdf')
            second = routes.bedetheque_catalog_match('Chagrin.Rodolphe.Griffo.2026.FR.[PDF]-NOTAG.pdf')
        self.assertEqual(first['url'], candidates[0]['url'])
        self.assertEqual(second, first)
        calls = search.call_count
        self.assertGreaterEqual(calls, 1)
        with patch('blueprints.bedetheque.catalog_index.search_catalog_index') as search_again:
            routes.bedetheque_catalog_match('Chagrin.Rodolphe.Griffo.2026.FR.[PDF]-NOTAG.pdf')
        search_again.assert_not_called()

    def test_unrelated_candidate_is_not_used(self):
        with patch('blueprints.bedetheque.catalog_index.search_catalog_index',
                   return_value=[{'title': 'Blacksad', 'url': 'https://www.bedetheque.com/serie-2.html'}]):
            self.assertIsNone(routes.bedetheque_catalog_match('Chagrin.2026.FR.[PDF].pdf'))


if __name__ == '__main__':
    unittest.main()
