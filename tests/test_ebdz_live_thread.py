import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from flask import Flask

from blueprints.ebdz import live_thread
from blueprints.ebdz.live_thread import EbdzThreadError, parse_thread_page, scrape_thread_now, thread_id_from_text
from blueprints.ebdz.scraper import MyBBScraper
from blueprints.library import routes as library_routes
from blueprints.library.routes import library_bp

THREAD_HTML = """<html><head><title>Les chroniques de Saint-Roustan - EmuleBDZ</title></head><body>
<div class="navigation"><a href="index.php">EmuleBDZ</a> &raquo; <a href="forumdisplay.php?fid=7">Bande Dessinées</a>
&raquo; <span class="active">Les chroniques de Saint-Roustan</span></div><div class="post_body">...</div></body></html>"""
LOGIN_HTML = "<html><head><title>EmuleBDZ</title></head><body><form>Connexion</form></body></html>"


class ParseThreadPageTest(unittest.TestCase):
    def test_title_and_forum_are_read_from_the_breadcrumb(self):
        self.assertEqual(parse_thread_page(THREAD_HTML), ('Les chroniques de Saint-Roustan', '7'))

    def test_title_falls_back_to_the_page_title_and_the_login_page_is_not_a_thread(self):
        html = "<html><head><title>Mon sujet - EmuleBDZ</title></head><body></body></html>"
        self.assertEqual(parse_thread_page(html), ('Mon sujet', None))
        self.assertEqual(parse_thread_page(LOGIN_HTML), (None, None))
        self.assertEqual(parse_thread_page(''), (None, None))

    def test_thread_id_is_read_from_a_pasted_address(self):
        self.assertEqual(thread_id_from_text('https://ebdz.net/forum/showthread.php?tid=35656&highlight=roustan'), '35656')
        self.assertIsNone(thread_id_from_text('https://ebdz.net/forum/'))


class ScrapeThreadNowTest(unittest.TestCase):
    CONFIG = {'username': 'u', 'password_decrypted': 'p', 'forums': [{'fid': 3, 'category': 'Romans'}, {'fid': 7, 'category': 'Bande Dessinées'}]}

    def _scraper(self, html=THREAD_HTML, logged_in=True, links=None):
        scraper = MagicMock()
        scraper.login.return_value = logged_in
        scraper.session.get.return_value = MagicMock(text=html)
        scraper.scrape_thread.return_value = links if links is not None else []
        return scraper

    def test_links_are_saved_with_the_category_of_the_forum_and_the_title_is_remembered(self):
        scraper = self._scraper(links=[{'link': 'ed2k://a'}, {'link': 'ed2k://b'}])
        with patch.object(live_thread, 'MyBBScraper', return_value=scraper):
            result = scrape_thread_now('35656', self.CONFIG, 'x.db')
        self.assertEqual(result, {'thread_id': '35656', 'title': 'Les chroniques de Saint-Roustan',
                                  'url': 'https://ebdz.net/forum/showthread.php?tid=35656',
                                  'forum_category': 'Bande Dessinées', 'links': 2})
        scraper.save_to_db.assert_called_once()
        scraper._save_thread_state.assert_called_once_with(
            '35656', None, title='Les chroniques de Saint-Roustan', url='https://ebdz.net/forum/showthread.php?tid=35656')

    def test_a_thread_without_links_is_still_remembered(self):
        scraper = self._scraper(links=[])
        with patch.object(live_thread, 'MyBBScraper', return_value=scraper):
            result = scrape_thread_now('35656', self.CONFIG, 'x.db')
        self.assertEqual(result['links'], 0)
        scraper.save_to_db.assert_not_called()
        scraper._save_thread_state.assert_called_once()

    def test_clear_errors(self):
        with self.assertRaisesRegex(EbdzThreadError, 'Identifiants EBDZ non configurés'):
            scrape_thread_now('1', {}, 'x.db')
        with patch.object(live_thread, 'MyBBScraper', return_value=self._scraper(logged_in=False)):
            with self.assertRaisesRegex(EbdzThreadError, 'Connexion à EBDZ impossible'):
                scrape_thread_now('1', self.CONFIG, 'x.db')
        with patch.object(live_thread, 'MyBBScraper', return_value=self._scraper(html=LOGIN_HTML)):
            with self.assertRaisesRegex(EbdzThreadError, 'introuvable ou illisible'):
                scrape_thread_now('1', self.CONFIG, 'x.db')


class ThreadStateTitleTest(unittest.TestCase):
    def test_old_state_table_is_migrated_and_a_later_save_keeps_the_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, 'ebdz.db')
            conn = sqlite3.connect(db)
            conn.execute("""CREATE TABLE ebdz_thread_state (thread_id TEXT NOT NULL, forum_category TEXT NOT NULL,
                reply_count INTEGER, last_scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (thread_id, forum_category))""")
            conn.execute("INSERT INTO ebdz_thread_state (thread_id, forum_category, reply_count) VALUES ('35656', 'BD', 0)")
            conn.commit()
            conn.close()
            scraper = MyBBScraper('https://ebdz.net/forum/forumdisplay.php?fid=7', db, 'u', 'p', forum_category='BD')
            scraper._save_thread_state('35656', None, title='Saint-Roustan', url='https://ebdz.net/x?tid=35656')
            scraper._save_thread_state('35656', 4)  # scrape suivant sans titre : le titre reste
            row = sqlite3.connect(db).execute(
                'SELECT reply_count, thread_title, thread_url FROM ebdz_thread_state WHERE thread_id = ?', ('35656',)).fetchone()
        self.assertEqual(row, (4, 'Saint-Roustan', 'https://ebdz.net/x?tid=35656'))


class _Shared:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return self._conn.cursor()

    def execute(self, *args):
        return self._conn.execute(*args)

    def commit(self):
        self._conn.commit()

    def close(self):
        pass


class EbdzMatchLiveScrapeTest(unittest.TestCase):
    """Coller l'adresse d'un sujet jamais scrapé le relit en direct avant de rapprocher la série."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ebdz_db = os.path.join(self.tmp.name, 'ebdz.db')
        ebdz = sqlite3.connect(self.ebdz_db)
        ebdz.execute("""CREATE TABLE ed2k_links (id INTEGER PRIMARY KEY AUTOINCREMENT, link TEXT UNIQUE, volume INTEGER,
            thread_title TEXT, thread_url TEXT, thread_id TEXT)""")
        ebdz.execute("INSERT INTO ed2k_links (link, volume, thread_title, thread_url, thread_id) VALUES ('ed2k://old', 1, 'Autre', 'https://ebdz.net/u?tid=11', '11')")
        ebdz.commit()
        ebdz.close()
        conn = sqlite3.connect(os.path.join(self.tmp.name, 'lib.db'))
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE series (id INTEGER PRIMARY KEY, title TEXT, ebdz_volumes_count INTEGER, ebdz_missing_volumes TEXT,
                ebdz_checked_at TEXT, ebdz_thread_url TEXT, ebdz_thread_id TEXT, ebdz_matched_title TEXT, ebdz_match_status TEXT);
            CREATE TABLE volumes (id INTEGER PRIMARY KEY, series_id INTEGER, volume_number INTEGER);
            INSERT INTO series (id, title) VALUES (1, 'Les chroniques de Saint-Roustan');
            INSERT INTO volumes (series_id, volume_number) VALUES (1, 1);
        """)
        conn.commit()
        self.conn = conn
        app = Flask(__name__)
        app.config['DB_FILE'] = self.ebdz_db
        app.register_blueprint(library_bp)
        self.client = app.test_client()
        patcher = patch.object(library_routes, 'get_db_connection', side_effect=lambda: _Shared(conn))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _series(self):
        return dict(self.conn.execute('SELECT * FROM series WHERE id = 1').fetchone())

    def _post(self, url, live):
        with patch('blueprints.ebdz.live_thread.scrape_thread_now', side_effect=live) as scrape, \
             patch('blueprints.ebdz.routes.load_ebdz_config', return_value={}):
            response = self.client.post('/api/series/1/ebdz-match', json={'thread_url': url})
        return response, scrape

    def test_unknown_thread_without_links_is_read_live_and_the_series_is_matched_on_its_title(self):
        title = 'Les chroniques de Saint-Roustan [Auteur]'
        live = lambda tid, cfg, db: {'thread_id': tid, 'title': title, 'url': f'https://ebdz.net/f?tid={tid}',
                                     'forum_category': 'BD', 'links': 0}
        response, scrape = self._post('https://ebdz.net/forum/showthread.php?tid=35656&highlight=roustan', live)
        body = response.get_json()
        self.assertEqual(response.status_code, 200, body)
        scrape.assert_called_once()
        self.assertEqual((body['live_scraped'], body['links_found'], body['matched_title']), (True, 0, title))
        series = self._series()
        self.assertEqual((series['ebdz_match_status'], series['ebdz_thread_id'], series['ebdz_matched_title']), ('matched', '35656', title))
        self.assertEqual(series['ebdz_thread_url'], 'https://ebdz.net/f?tid=35656')

    def test_links_found_live_give_the_missing_volumes(self):
        def live(tid, cfg, db):
            conn = sqlite3.connect(db)
            for number in (1, 2, 3):
                conn.execute("INSERT INTO ed2k_links (link, volume, thread_title, thread_url, thread_id) VALUES (?, ?, 'Titre', 'https://ebdz.net/f?tid=9', ?)",
                             (f'ed2k://n{number}', number, tid))
            conn.commit()
            conn.close()
            return {'thread_id': tid, 'title': 'Titre', 'url': 'https://ebdz.net/f?tid=9', 'forum_category': 'BD', 'links': 3}
        response, _ = self._post('https://ebdz.net/forum/showthread.php?tid=9', live)
        body = response.get_json()
        self.assertEqual((body['ebdz_count'], body['missing_volumes'], body['links_found']), (3, [2, 3], 3))

    def test_known_thread_is_not_read_again(self):
        response, scrape = self._post('https://ebdz.net/forum/showthread.php?tid=11', lambda *a: None)
        self.assertEqual(response.status_code, 200)
        scrape.assert_not_called()
        self.assertFalse(response.get_json()['live_scraped'])

    def test_failure_is_reported_and_the_series_is_left_untouched(self):
        def live(tid, cfg, db):
            raise EbdzThreadError('Connexion à EBDZ impossible : vérifiez les identifiants')
        response, _ = self._post('https://ebdz.net/forum/showthread.php?tid=35656', live)
        self.assertEqual(response.status_code, 502)
        self.assertIn('Connexion à EBDZ impossible', response.get_json()['error'])
        self.assertIsNone(self._series()['ebdz_match_status'])


if __name__ == '__main__':
    unittest.main()
