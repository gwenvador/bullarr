"""Compte Komga relié par utilisateur et avancement local sans Komga (blueprints/komga/user_link.py)."""
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from flask import Flask

from blueprints.library.scanner import LibraryScanner

KOMGA_ON = {'enabled': True, 'url': 'http://komga', 'api_key': 'k'}


def _resp(status, payload=None):
    r = mock.Mock(status_code=status)
    r.json.return_value = payload or {}
    return r


class UserLinkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, 'bullarr.db')
        from config import Config
        Config._init_database(self.db)
        LibraryScanner(self.db)
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO libraries (id, name, path) VALUES (1, 'BD', '/BD')")
        conn.execute("INSERT INTO series (id, library_id, title, path) VALUES (1, 1, 'Astérix', '/BD/Astérix')")
        for n, book in ((1, 'b1'), (2, None)):
            conn.execute('INSERT INTO volumes (series_id, volume_number, filename, filepath, komga_book_id, page_count) VALUES (1, ?, ?, ?, ?, 48)',
                         (n, f'T{n}.cbz', f'/BD/Astérix/T{n}.cbz', book))
        conn.commit()
        conn.close()
        self.app = Flask(__name__)
        self.app.config['DATABASE'] = self.db
        self.cfg = dict(KOMGA_ON)
        p = mock.patch('blueprints.komga.user_link.load_komga_config', side_effect=lambda: self.cfg)
        p.start()
        self.addCleanup(p.stop)
        e = mock.patch('blueprints.komga.user_link.encrypt', side_effect=lambda s: 'enc:' + s)
        d = mock.patch('blueprints.komga.user_link.decrypt', side_effect=lambda s: s[4:])
        e.start(); d.start()
        self.addCleanup(e.stop); self.addCleanup(d.stop)

    def test_source_kinds(self):
        from blueprints.komga import user_link
        with self.app.app_context():
            self.assertEqual(user_link.progress_source('password:jules', True).kind, 'local')
            self.assertEqual(user_link.progress_source('password:admin', False).kind, 'komga')
            self.assertEqual(user_link.progress_source(None, False).kind, 'komga')
            self.cfg['enabled'] = False
            self.assertEqual(user_link.progress_source('password:admin', False).kind, 'local')

    def test_local_progress_survives_rescan_and_feeds_keep_reading(self):
        from blueprints.komga import user_link
        from blueprints.komga.read_progress import keep_reading
        with self.app.app_context():
            user_link.record_local_progress('password:jules', 1, 'T2.cbz', 7, False)
            # Rescan : la ligne du tome est recréée avec un autre id.
            conn = sqlite3.connect(self.db)
            conn.execute("DELETE FROM volumes WHERE filename = 'T2.cbz'")
            conn.execute("INSERT INTO volumes (id, series_id, volume_number, filename, filepath, page_count) VALUES (99, 1, 2, 'T2.cbz', '/BD/Astérix/T2.cbz', 48)")
            conn.commit(); conn.close()
            mine = keep_reading(1, source=user_link.progress_source('password:jules', True))
            other = keep_reading(1, source=user_link.progress_source('password:lea', True))
        self.assertEqual([(i['volume_id'], i['page'], i['pages_count']) for i in mine], [(99, 7, 48)])
        self.assertEqual(other, [])

    def test_link_with_password_keeps_only_the_api_key(self):
        from blueprints.komga import user_link
        session = mock.Mock()
        session.get.return_value = _resp(200, {'email': 'jules@x'})
        session.post.return_value = _resp(201, {'key': 'abc123'})
        books = _resp(200, {'content': [{'id': 'b1', 'seriesId': 's', 'name': 'T1', 'media': {'pagesCount': 48},
                                         'readProgress': {'page': 10, 'completed': False}}], 'last': True})
        with self.app.app_context(), \
                mock.patch('blueprints.komga.user_link.requests.Session', return_value=session), \
                mock.patch('blueprints.komga.client.requests.get', return_value=books) as get:
            status = user_link.link('password:jules', username='jules@x', password='secret')
            source = user_link.progress_source('password:jules', True)
            conn = sqlite3.connect(self.db)
            stored = conn.execute('SELECT api_key, komga_user FROM user_komga_links').fetchall()
            rows = conn.execute(f'SELECT vol_id, page FROM {source.sql} p', source.params).fetchall()
            conn.close()
        self.assertTrue(status['linked'])
        self.assertEqual(status['mode'], 'komga_user')
        self.assertEqual(stored, [('enc:abc123', 'jules@x')])
        self.assertEqual(get.call_args.kwargs['headers'], {'X-API-Key': 'abc123'})
        self.assertEqual(rows, [(1, 10)])
        self.assertNotIn('secret', repr(stored))

    def test_wrong_password_is_reported(self):
        from blueprints.komga import user_link
        from blueprints.komga.client import KomgaError
        session = mock.Mock()
        session.get.return_value = _resp(401)
        with self.app.app_context(), mock.patch('blueprints.komga.user_link.requests.Session', return_value=session):
            with self.assertRaises(KomgaError):
                user_link.link('password:jules', username='jules@x', password='nope')
            self.assertFalse(user_link.has_link('password:jules'))

    def test_unlink_falls_back_to_local(self):
        from blueprints.komga import user_link
        with self.app.app_context(), mock.patch('blueprints.komga.user_link._whoami', return_value='jules@x'), \
                mock.patch('blueprints.komga.user_link.sync_user'):
            user_link.link('password:jules', api_key='abc')
            self.assertEqual(user_link.progress_source('password:jules', True).kind, 'komga_user')
            user_link.unlink('password:jules')
            self.assertEqual(user_link.progress_source('password:jules', True).kind, 'local')


if __name__ == '__main__':
    unittest.main()
