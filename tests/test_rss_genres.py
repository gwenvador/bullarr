import json
import os
import tempfile
import unittest
from pathlib import Path

from flask import Flask

from blueprints.settings import rss
from blueprints.settings import settings_bp

ROOT = Path(__file__).resolve().parents[1]


class RssGenresStorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'rss_feeds.json')

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_genres_by_default_and_validation(self):
        self.assertEqual(rss.load_genres(self.path), ['bd', 'comics', 'manga'])
        self.assertEqual(rss.normalize_genres(['Manga', 'BD', 'inconnu']), ['bd', 'manga'])
        for bad in ([], ['x'], 'bd'):
            with self.assertRaises(ValueError):
                rss.normalize_genres(bad)

    def test_genres_and_feeds_are_saved_together(self):
        rss.save_genres(self.path, ['manga'])
        self.assertEqual(rss.load_genres(self.path), ['manga'])
        # Enregistrer les flux ne perd pas les genres
        rss.save_feeds(self.path, [{'provider': 'vk', 'name': 'VK'}])
        self.assertEqual(rss.load_genres(self.path), ['manga'])
        self.assertEqual([f['provider'] for f in rss.load_feeds(self.path)], ['vk'])
        # Enregistrer les genres ne perd pas les flux
        rss.save_genres(self.path, ['bd', 'comics'])
        self.assertEqual([f['provider'] for f in rss.load_feeds(self.path)], ['vk'])
        self.assertEqual(json.load(open(self.path))['genres'], ['bd', 'comics'])


class RssGenresRouteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Flask('t')
        self.app.config['RSS_CONFIG_FILE'] = os.path.join(self.tmp.name, 'rss_feeds.json')
        self.app.register_blueprint(settings_bp)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_get_and_post_genres(self):
        self.assertEqual(self.client.get('/api/settings/rss').get_json()['genres'], ['bd', 'comics', 'manga'])
        feeds = self.client.get('/api/settings/rss').get_json()['feeds']
        response = self.client.post('/api/settings/rss', json={'feeds': feeds, 'genres': ['manga', 'bd']})
        self.assertEqual(response.get_json()['genres'], ['bd', 'manga'])
        self.assertEqual(self.client.get('/api/settings/rss').get_json()['genres'], ['bd', 'manga'])
        # Sans clé « genres » : inchangés
        self.client.post('/api/settings/rss', json={'feeds': feeds})
        self.assertEqual(self.client.get('/api/settings/rss').get_json()['genres'], ['bd', 'manga'])

    def test_empty_selection_is_refused(self):
        response = self.client.post('/api/settings/rss', json={'feeds': [], 'genres': []})
        self.assertEqual(response.status_code, 400)
        self.assertIn('au moins un genre', response.get_json()['error'])


class RssGenresPageTest(unittest.TestCase):
    def test_settings_checkboxes_and_nouveautes_filter(self):
        html = (ROOT / 'templates/settings.html').read_text()
        for genre in ('bd', 'comics', 'manga'):
            self.assertIn(f'class="rss-genre-checkbox" value="{genre}"', html)
        js = (ROOT / 'static/js/ebdz-latest.js').read_text()
        self.assertIn('_nouveautesRssGenreHidden(e)', js)
        self.assertIn("event.type !== 'rss' || nouveautesRssGenres.size >= 3", js)


if __name__ == '__main__':
    unittest.main()
