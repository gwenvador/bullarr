import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from blueprints.bedetheque import origins

PAGE = ('<ul class="bdt-ah-pastilles"><li class="bdt-ah-origine" title="Origine de la série">USA</li>'
        '<li class="bdt-ah-langue" title="Langue">Français</li></ul>')


class OriginParsingTest(unittest.TestCase):
    def test_parse_origin_and_kind(self):
        self.assertEqual(origins.parse_origin(PAGE), 'USA')
        self.assertEqual(origins.parse_origin('<li><label>Origine : </label>Asie</li>'), 'Asie')
        self.assertIsNone(origins.parse_origin('<html>rien</html>'))
        # Mise en page actuelle : une icône précède le libellé.
        self.assertEqual(origins.parse_origin(
            '<li title="Origine de la série"><i class="fa-solid bdt-ori bdt-ori-2" aria-hidden="true"></i> Manga</li>'),
            'Asie')
        self.assertEqual(origins.parse_origin(
            '<li title="Origine de la série"><i class="fa-solid bdt-ori bdt-ori-1"></i> Franco-Belge</li>'),
            'Europe')
        self.assertEqual(origins.origin_kind(origins.parse_origin(
            '<li title="Origine de la série"><i class="bdt-ori"></i> Manga</li>')), 'manga')
        self.assertEqual([origins.origin_kind(v) for v in ('Europe', 'USA', 'asie', 'Autre', None)],
                         ['bd', 'comics', 'manga', None, None])

    def test_new_bedetheque_labels_are_understood_everywhere(self):
        # La fiche affiche « Franco-Belge » / « Comics » / « Manga » : rattachement d'une
        # série, Nouveautés et bibliothèque doivent donner le même drapeau qu'avec Europe/USA/Asie.
        from blueprints.bedetheque.scraper import _canonical_origin
        self.assertEqual(_canonical_origin(' Franco-Belge '), 'Europe')
        self.assertEqual(_canonical_origin('Manga'), 'Asie')
        self.assertEqual(_canonical_origin('Autre'), 'Autre')
        self.assertIsNone(_canonical_origin(''))
        for label, kind in (('Franco-Belge', 'bd'), ('Comics', 'comics'), ('Manga', 'manga'), ('Europe', 'bd')):
            self.assertEqual(origins.origin_kind(label), kind)
        script = (Path(__file__).resolve().parents[1] / 'static/js/nav.js').read_text()
        self.assertIn("BEDETHEQUE_ORIGIN_KINDS['franco-belge']", script)

    def test_only_bedetheque_series_urls(self):
        self.assertEqual(origins.normalize_series_url('http://bedetheque.com/serie-500-BD-Blacksad.html?x=1#y'),
                         'https://www.bedetheque.com/serie-500-BD-Blacksad.html')
        for bad in ('https://evil.example/serie-1-BD-X.html', 'https://www.bedetheque.com/BD-Album-1.html', '', None):
            self.assertIsNone(origins.normalize_series_url(bad))


class ResolveOriginsTest(unittest.TestCase):
    def setUp(self):
        # Catalogue d'origines isolé: un test ne doit jamais lire ce qu'un autre y a écrit.
        from blueprints.bedetheque import catalog_index
        self._catalog_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._catalog_tmp.cleanup)
        catalog_patch = patch.object(catalog_index, 'CATALOG_DB', os.path.join(self._catalog_tmp.name, 'catalog.db'))
        catalog_patch.start()
        self.addCleanup(catalog_patch.stop)
        origins._catalog_seeded_for.clear()
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

    def test_page_without_origin_is_retried_after_a_month(self):
        url = 'https://www.bedetheque.com/serie-9-BD-Sans.html'
        calls = []
        with patch.object(origins, 'ORIGIN_FETCH_INTERVAL_SECONDS', 0):
            origins.resolve_origins(self.db, [url], lambda u: calls.append(u) or '<html></html>')
            origins.resolve_origins(self.db, [url], lambda u: calls.append(u) or '<html></html>')
            self.assertEqual(len(calls), 1)
            conn = sqlite3.connect(self.db)
            conn.execute('UPDATE bedetheque_origin_cache SET fetched_at = ?', (time.time() - 31 * 86400,))
            conn.commit()
            conn.close()
            origins.resolve_origins(self.db, [url], lambda u: calls.append(u) or PAGE)
        self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()


class CatalogOriginsTest(unittest.TestCase):
    """Chaque origine lue est ajoutée au catalogue; celles déjà connues de la bibliothèque aussi."""

    def setUp(self):
        from blueprints.bedetheque import catalog_index
        self.catalog_index = catalog_index
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, 'bullarr.db')
        conn = sqlite3.connect(self.db)
        conn.execute('CREATE TABLE series (id INTEGER PRIMARY KEY, bedetheque_url TEXT, bedetheque_origin TEXT)')
        conn.execute("INSERT INTO series (bedetheque_url, bedetheque_origin) VALUES "
                     "('https://www.bedetheque.com/serie-1-BD-Alix.html', 'Franco-Belge')")
        conn.commit()
        conn.close()
        patcher = patch.object(catalog_index, 'CATALOG_DB', os.path.join(self.tmp.name, 'catalog.db'))
        patcher.start()
        self.addCleanup(patcher.stop)
        origins._catalog_seeded_for.clear()

    def test_library_origins_are_added_to_the_catalog(self):
        origins.resolve_origins(self.db, ['https://www.bedetheque.com/serie-1-BD-Alix.html'])
        self.assertEqual(self.catalog_index.get_catalog_origins(['https://www.bedetheque.com/serie-1-BD-Alix.html']),
                         {'https://www.bedetheque.com/serie-1-BD-Alix.html': 'Europe'})

    def test_page_read_is_added_to_the_catalog_and_not_read_twice(self):
        url = 'https://www.bedetheque.com/serie-2-BD-Akira.html'
        pages = []

        def fetch(u):
            pages.append(u)
            return '<li title="Origine de la série"><i class="x"></i> Manga</li>'

        origins.resolve_origins(self.db, [url], fetch)
        self.assertEqual(self.catalog_index.get_catalog_origins([url]), {url: 'Asie'})
        # Autre base (cache d'origines vide), même catalogue: plus aucune lecture réseau.
        other = os.path.join(self.tmp.name, 'other.db')
        conn = sqlite3.connect(other)
        conn.execute('CREATE TABLE series (id INTEGER PRIMARY KEY, bedetheque_url TEXT, bedetheque_origin TEXT)')
        conn.commit()
        conn.close()
        result, pending = origins.resolve_origins(other, [url], fetch)
        self.assertEqual((result, pending, len(pages)), ({url: 'Asie'}, 0, 1))

    def test_series_index_rebuild_keeps_the_origins(self):
        url = 'https://www.bedetheque.com/serie-3-BD-Blake.html'
        self.catalog_index.save_catalog_origins([(url, 'Europe')])
        conn = self.catalog_index._connect_db()
        conn.execute('DELETE FROM bedetheque_series')  # ce que fait la reconstruction de l\'index
        conn.commit()
        conn.close()
        self.assertEqual(self.catalog_index.get_catalog_origins([url]), {url: 'Europe'})
