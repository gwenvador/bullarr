import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from blueprints.bedetheque import origins

PAGE = ('<ul class="bdt-ah-pastilles"><li class="bdt-ah-origine" title="Origine de la série">USA</li>'
        '<li class="bdt-ah-langue" title="Langue">Français</li></ul>')


class OriginParsingTest(unittest.TestCase):
    def test_parse_origin_and_kind(self):
        self.assertEqual(origins.parse_origin(PAGE), 'USA')
        self.assertEqual(origins.parse_origin('<li><label>Origine : </label>Asie</li>'), 'Asie')
        self.assertIsNone(origins.parse_origin('<html>rien</html>'))
        self.assertEqual([origins.origin_kind(v) for v in ('Europe', 'USA', 'asie', 'Autre', None)],
                         ['bd', 'comics', 'manga', None, None])

    def test_only_bedetheque_series_urls(self):
        self.assertEqual(origins.normalize_series_url('http://bedetheque.com/serie-500-BD-Blacksad.html?x=1#y'),
                         'https://www.bedetheque.com/serie-500-BD-Blacksad.html')
        for bad in ('https://evil.example/serie-1-BD-X.html', 'https://www.bedetheque.com/BD-Album-1.html', '', None):
            self.assertIsNone(origins.normalize_series_url(bad))


class ResolveOriginsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'bullarr.db')
        conn = sqlite3.connect(self.db)
        conn.execute('CREATE TABLE series (id INTEGER, bedetheque_url TEXT, bedetheque_origin TEXT)')
        conn.execute("INSERT INTO series VALUES (1, 'https://www.bedetheque.com/serie-500-BD-Blacksad.html', 'Europe')")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_library_then_cache_then_page(self):
        fetched = []

        def fetch(url):
            fetched.append(url)
            return PAGE

        urls = ['https://www.bedetheque.com/serie-500-BD-Blacksad.html',
                'https://www.bedetheque.com/serie-11312-BD-Walking-Dead.html']
        with patch.object(origins, 'ORIGIN_FETCH_INTERVAL_SECONDS', 0):
            result, pending = origins.resolve_origins(self.db, urls, fetch)
            self.assertEqual(result, {urls[0]: 'Europe', urls[1]: 'USA'})
            self.assertEqual((pending, fetched), (0, [urls[1]]))
            # Deuxième appel : tout vient de la bibliothèque et du cache
            result, pending = origins.resolve_origins(self.db, urls, fetch)
        self.assertEqual(result[urls[1]], 'USA')
        self.assertEqual(len(fetched), 1)

    def test_fetch_limit_leaves_pending(self):
        urls = [f'https://www.bedetheque.com/serie-{n}-BD-X{n}.html' for n in range(1, 6)]
        with patch.object(origins, 'ORIGIN_FETCH_INTERVAL_SECONDS', 0):
            result, pending = origins.resolve_origins(self.db, urls, lambda url: PAGE, fetch_limit=2)
        self.assertEqual((len(result), pending), (2, 3))
        result, pending = origins.resolve_origins(self.db, urls, None)
        self.assertEqual((len(result), pending), (2, 3))

    def test_page_without_origin_is_retried_after_a_week(self):
        url = 'https://www.bedetheque.com/serie-9-BD-Sans.html'
        calls = []
        with patch.object(origins, 'ORIGIN_FETCH_INTERVAL_SECONDS', 0):
            origins.resolve_origins(self.db, [url], lambda u: calls.append(u) or '<html></html>')
            origins.resolve_origins(self.db, [url], lambda u: calls.append(u) or '<html></html>')
            self.assertEqual(len(calls), 1)
            conn = sqlite3.connect(self.db)
            conn.execute('UPDATE bedetheque_origin_cache SET fetched_at = ?', (time.time() - 8 * 86400,))
            conn.commit()
            conn.close()
            origins.resolve_origins(self.db, [url], lambda u: calls.append(u) or PAGE)
        self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()
