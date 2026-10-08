import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from blueprints.bedetheque import catalog_index


class FakeScraper:
    """Page de listing identique pour chaque lettre: les doublons doivent être dédupliqués."""

    def __init__(self, html=None, error=None):
        self.html = html or ''
        self.error = error
        self.session = self
        self.requested = []

    def _ensure_session(self):
        pass

    def get(self, url, timeout=None):
        self.requested.append(url)
        if self.error:
            raise self.error
        response = mock.Mock()
        response.content = self.html.encode('utf-8')
        return response


LISTING = '''<ul>
  <li><a href="https://www.bedetheque.com/serie-24038-BD-NOOB.html">NOOB</a></li>
  <li><a href="https://www.bedetheque.com/serie-1-BD-Pieds-Nickeles.html">Les Pieds Nickelés</a></li>
  <li><a href="https://www.bedetheque.com/serie-2-BD-Thorgal.html">Thorgal</a></li>
  <li><a href="https://www.bedetheque.com/serie-3-BD-Thorgal-Saga.html">Thorgal Saga</a></li>
  <li><a href="https://www.bedetheque.com/serie-4-BD-Vide.html"> </a></li>
  <li><a href="https://www.bedetheque.com/auteur-1-BD-Pas-Une-Serie.html">Auteur</a></li>
</ul>'''


class CatalogIndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, 'catalog.db')
        patches = [mock.patch.object(catalog_index, 'CATALOG_DB', self.db),
                   mock.patch('blueprints.bedetheque.scraper._anti_bot_delay')]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def build(self, scraper=None):
        scraper = scraper or FakeScraper(LISTING)
        catalog_index.build_bedetheque_catalog_index_sync(scraper)
        return scraper

    # --- generic scrape cache
    def test_cache_round_trip_and_overwrite(self):
        self.assertIsNone(catalog_index.get_cached_scrape('indispensables:all'))
        catalog_index.save_scrape_cache('indispensables:all', [{'rank': 1}])
        self.assertEqual(catalog_index.get_cached_scrape('indispensables:all'), [{'rank': 1}])
        catalog_index.save_scrape_cache('indispensables:all', {'items': []})
        self.assertEqual(catalog_index.get_cached_scrape('indispensables:all'), {'items': []})

    def test_cache_has_no_expiry_unless_asked_and_ignores_corrupt_values(self):
        catalog_index.save_scrape_cache('k', {'a': 1})
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE bedetheque_scrape_cache SET built_at = '2020-01-01 00:00:00'")
        conn.commit()
        conn.close()
        self.assertEqual(catalog_index.get_cached_scrape('k'), {'a': 1})
        self.assertIsNone(catalog_index.get_cached_scrape('k', max_age_seconds=60))
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE bedetheque_scrape_cache SET value_json = '{broken'")
        conn.commit()
        conn.close()
        self.assertIsNone(catalog_index.get_cached_scrape('k'))

    # --- series index
    def test_search_before_the_index_is_built_is_distinguishable_from_no_result(self):
        self.assertIsNone(catalog_index.search_catalog_index('noob'))
        self.assertEqual(catalog_index.get_catalog_status()['built'], False)

    def test_build_indexes_only_series_links_once_each(self):
        scraper = self.build()
        status = catalog_index.get_catalog_status()
        self.assertEqual((status['built'], status['count']), (True, 4))        # NOOB, Pieds Nickelés, Thorgal, Thorgal Saga
        self.assertIsNotNone(status['built_at'])
        self.assertEqual(len(scraper.requested), len(catalog_index._LETTERS))
        self.assertFalse(catalog_index.get_catalog_status()['running'])

    def test_search_is_accent_and_case_insensitive_and_requires_every_word(self):
        self.build()
        titles = lambda q: [r['title'] for r in catalog_index.search_catalog_index(q)]
        self.assertEqual(titles('pieds nickeles'), ['Les Pieds Nickelés'])
        self.assertEqual(titles('NOOB'), ['NOOB'])
        self.assertEqual(titles('thorgal saga'), ['Thorgal Saga'])
        self.assertEqual(sorted(titles('thorgal')), ['Thorgal', 'Thorgal Saga'])
        self.assertEqual(titles('thorgal inexistant'), [])

    def test_best_match_comes_first_and_results_are_limited(self):
        self.build()
        results = catalog_index.search_catalog_index('thorgal', limit=1)
        self.assertEqual([r['title'] for r in results], ['Thorgal'])
        self.assertEqual(results[0]['url'], 'https://www.bedetheque.com/serie-2-BD-Thorgal.html')
        self.assertIsNone(results[0]['genre'])

    def test_empty_and_too_short_queries_give_no_result(self):
        self.build()
        for query in ('', '   ', None, 'ab', 'a b'):
            self.assertEqual(catalog_index.search_catalog_index(query), [], repr(query))

    def test_a_failed_rebuild_keeps_the_previous_index_and_reports_the_error(self):
        self.build()
        catalog_index.build_bedetheque_catalog_index_sync(FakeScraper(error=RuntimeError('403 Cloudflare')))
        status = catalog_index.get_catalog_status()
        self.assertEqual(status['count'], 4)
        self.assertEqual(status['progress']['error'], 'Erreur interne')
        self.assertFalse(status['running'])


if __name__ == '__main__':
    unittest.main()
