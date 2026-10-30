import os
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from bs4 import BeautifulSoup
from flask import Flask

from blueprints.ebdz.removed import active_and, active_where, has_removed_column
from blueprints.ebdz.scraper import MyBBScraper
from blueprints.library import routes as library_routes
from blueprints.library.routes import library_bp

H_A = '0123456789ABCDEF0123456789ABCDEF'
H_B = 'FEDCBA9876543210FEDCBA9876543210'
LINK_A = f'ed2k://|file|A.cbz|100|{H_A}|/'
LINK_A_AICH = f'ed2k://|file|A.cbz|100|{H_A}|h=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA|/'
LINK_B = f'ed2k://|file|B.cbz|200|{H_B}|/'


def _page(*links, paginated=False, thread=True):
    body = ''.join(f'<a href="{link}">{link}</a>' for link in links)
    post = f'<div class="post_body">{body}</div>' if thread else '<form>Connexion</form>'
    pager = '<div class="pagination"><a class="pagination_page" href="showthread.php?tid=1&page=2">2</a></div>' if paginated else ''
    return f'<html><body>{pager}{post}</body></html>'


class RemovedLinksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, 'ebdz.db')
        conn = sqlite3.connect(self.db)
        conn.execute("""CREATE TABLE ed2k_links (id INTEGER PRIMARY KEY AUTOINCREMENT, link TEXT UNIQUE, thread_id TEXT,
            filename TEXT, volume INTEGER, thread_title TEXT)""")
        conn.executemany('INSERT INTO ed2k_links (link, thread_id, filename) VALUES (?, ?, ?)',
                         [(LINK_A, '7', 'A'), (LINK_B, '7', 'B'), ('ed2k://|file|Z.cbz|1|' + 'AB' * 16 + '|/', '99', 'Z')])
        conn.commit()
        conn.close()
        self.scraper = MyBBScraper('https://ebdz.net/forum/forumdisplay.php?fid=7', self.db, 'u', 'p', forum_category='BD')

    def _removed(self):
        conn = sqlite3.connect(self.db)
        if not has_removed_column(conn):  # rien n'a été marqué : la colonne n'a pas été ajoutée
            names = [row[0] for row in conn.execute('SELECT filename FROM ed2k_links')]
            conn.close()
            return {name: False for name in names}
        rows = dict(conn.execute('SELECT filename, removed_at IS NOT NULL FROM ed2k_links').fetchall())
        conn.close()
        return {name: bool(flag) for name, flag in rows.items()}

    def test_column_is_added_to_an_old_table_and_missing_links_are_marked_not_deleted(self):
        self.scraper._sync_removed_links('7', [LINK_A])
        self.assertEqual(self._removed(), {'A': False, 'B': True, 'Z': False})  # B gardé, marqué ; autre sujet intact

    def test_same_file_with_another_aich_suffix_counts_as_still_there(self):
        self.scraper._sync_removed_links('7', [LINK_A_AICH, LINK_B])
        self.assertEqual(self._removed(), {'A': False, 'B': False, 'Z': False})

    def test_a_link_that_comes_back_is_active_again(self):
        self.scraper._sync_removed_links('7', [LINK_A])
        self.assertTrue(self._removed()['B'])
        self.scraper._sync_removed_links('7', [LINK_A, LINK_B])
        self.assertFalse(self._removed()['B'])

    def test_thread_left_with_no_link_marks_everything_removed(self):
        self.scraper._sync_removed_links('7', [])
        self.assertEqual(self._removed(), {'A': True, 'B': True, 'Z': False})

    def test_page_detection_needs_a_real_thread_on_a_single_page(self):
        detect = MyBBScraper._page_lists_all_links
        self.assertTrue(detect(BeautifulSoup(_page(LINK_A), 'html.parser')))
        self.assertFalse(detect(BeautifulSoup(_page(LINK_A, paginated=True), 'html.parser')))
        self.assertFalse(detect(BeautifulSoup(_page(thread=False), 'html.parser')))

    def _scrape(self, html):
        self.scraper.session = MagicMock()
        self.scraper.session.get.return_value = MagicMock(text=html, content=html.encode())
        return self.scraper.scrape_thread('https://ebdz.net/forum/showthread.php?tid=7', 'Sujet')

    def test_reading_a_thread_marks_what_disappeared(self):
        data = self._scrape(_page(LINK_A))
        self.assertEqual({d['link'] for d in data}, {LINK_A})  # la page répète le lien (adresse et texte)
        self.assertEqual(self._removed(), {'A': False, 'B': True, 'Z': False})

    def test_login_page_or_multi_page_thread_never_marks_anything(self):
        self._scrape(_page(thread=False))
        self._scrape(_page(LINK_A, paginated=True))
        self.assertEqual(self._removed(), {'A': False, 'B': False, 'Z': False})


class ActiveClauseTest(unittest.TestCase):
    def test_filter_only_applies_when_the_column_exists(self):
        old = sqlite3.connect(':memory:')
        old.execute('CREATE TABLE ed2k_links (id INTEGER, link TEXT)')
        self.assertFalse(has_removed_column(old))
        self.assertEqual((active_and(old), active_where(old)), ('', ''))
        new = sqlite3.connect(':memory:')
        new.execute('CREATE TABLE ed2k_links (id INTEGER, link TEXT, removed_at TIMESTAMP)')
        self.assertTrue(has_removed_column(new))
        self.assertEqual((active_and(new), active_where(new)), (' AND removed_at IS NULL', ' WHERE removed_at IS NULL'))


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


class MatchIgnoresRemovedLinksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ebdz_db = os.path.join(self.tmp.name, 'ebdz.db')
        ebdz = sqlite3.connect(self.ebdz_db)
        ebdz.execute("""CREATE TABLE ed2k_links (id INTEGER PRIMARY KEY AUTOINCREMENT, link TEXT UNIQUE, volume INTEGER,
            thread_title TEXT, thread_url TEXT, thread_id TEXT, removed_at TIMESTAMP)""")
        ebdz.executemany("INSERT INTO ed2k_links (link, volume, thread_title, thread_url, thread_id, removed_at) VALUES (?, ?, 'T', 'https://ebdz.net/f?tid=5', '5', ?)",
                         [('ed2k://v1', 1, None), ('ed2k://v2', 2, '2026-10-27 10:00:00'),
                          ('ed2k://w1', 1, '2026-10-27 10:00:00')])
        ebdz.execute("INSERT INTO ed2k_links (link, volume, thread_title, thread_url, thread_id, removed_at) VALUES ('ed2k://x1', 1, 'Seul', 'https://ebdz.net/f?tid=6', '6', '2026-10-27 10:00:00')")
        ebdz.commit()
        ebdz.close()
        conn = sqlite3.connect(os.path.join(self.tmp.name, 'lib.db'))
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE series (id INTEGER PRIMARY KEY, title TEXT, ebdz_volumes_count INTEGER, ebdz_missing_volumes TEXT,
                ebdz_checked_at TEXT, ebdz_thread_url TEXT, ebdz_thread_id TEXT, ebdz_matched_title TEXT, ebdz_match_status TEXT);
            CREATE TABLE volumes (id INTEGER PRIMARY KEY, series_id INTEGER, volume_number INTEGER);
            INSERT INTO series (id, title) VALUES (1, 'Serie');
        """)
        conn.commit()
        app = Flask(__name__)
        app.config['DB_FILE'] = self.ebdz_db
        app.register_blueprint(library_bp)
        self.client = app.test_client()
        patcher = patch.object(library_routes, 'get_db_connection', side_effect=lambda: _Shared(conn))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_removed_volumes_do_not_count_as_available_on_ebdz(self):
        with patch('blueprints.ebdz.live_thread.scrape_thread_now') as live:
            body = self.client.post('/api/series/1/ebdz-match', json={'thread_id': 5}).get_json()
        live.assert_not_called()
        self.assertEqual((body['ebdz_count'], body['missing_volumes']), (1, [1]))  # seul le tome 1 reste actif

    def test_thread_whose_links_are_all_removed_is_read_again(self):
        live_result = {'thread_id': '6', 'title': 'Seul', 'url': 'https://ebdz.net/f?tid=6', 'forum_category': 'BD', 'links': 0}
        with patch('blueprints.ebdz.live_thread.scrape_thread_now', return_value=live_result) as live, \
             patch('blueprints.ebdz.routes.load_ebdz_config', return_value={}):
            body = self.client.post('/api/series/1/ebdz-match', json={'thread_id': 6}).get_json()
        live.assert_called_once()
        self.assertEqual((body['live_scraped'], body['ebdz_count']), (True, 0))


if __name__ == '__main__':
    unittest.main()
