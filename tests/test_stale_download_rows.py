import sqlite3
import tempfile
import unittest

from flask import Flask

from blueprints.library import import_history
from blueprints.missing_monitor import downloader

SEARCH_TITLE = 'Le.Meilleur.Des.Pieds.Nickeles.Int.Integrale.05.Pellos.2006.FR.[CBZ]-TONER'
TORRENT_NAME = 'Le.Meilleur.des.Pieds.Nickeles.T05.2006.FRENCH.HYBRiD.COMiC.CBZ.eBook-TONER.cbz'
IMPORTED_FILE = 'Le.Meilleur.des.Pieds.Nickeles.T05.2006.FRENCH.HYBRiD.COMiC.CBZ.eBook-TONER.cbr'


class StaleDownloadRowsTest(unittest.TestCase):
    """Cas réel: la ligne « Int.Integrale.05 » restait « En attente… » après l'import du tome 5,
    parce que l'import n'avait refermé que la ligne jumelle dont le titre ressemblait au fichier."""

    def setUp(self):
        self.db = tempfile.NamedTemporaryFile(suffix='.db')
        self.app = Flask(__name__)
        self.app.config['DATABASE'] = self.db.name
        self.app.config['IMPORT_DIRECTORIES'] = []
        self.ctx = self.app.app_context()
        self.ctx.push()
        import_history.init_import_history_table()
        conn = sqlite3.connect(self.db.name)
        conn.execute('CREATE TABLE series (id INTEGER PRIMARY KEY, title TEXT, path TEXT)')
        conn.execute("INSERT INTO series VALUES (1331, 'Pieds Nickelés (Le meilleur des)', '/BD/x'), (2, 'Autre', '/BD/y')")
        conn.execute('''CREATE TABLE active_downloads (
            id INTEGER PRIMARY KEY, title TEXT, client TEXT, status TEXT, series_id INTEGER, volume_id INTEGER,
            volume_number INTEGER, is_pack INTEGER DEFAULT 0, expected_volume_count INTEGER, client_item_id TEXT,
            client_item_name TEXT, force_replace INTEGER DEFAULT 0, is_integral INTEGER DEFAULT 0, integral_number INTEGER,
            is_hs INTEGER DEFAULT 0, hs_number INTEGER, is_episode INTEGER DEFAULT 0, episode_number INTEGER,
            created_at TEXT, completed_at TEXT)''')
        conn.commit()
        conn.close()

    def tearDown(self):
        self.ctx.pop()
        self.db.close()

    def add_row(self, row_id, title=SEARCH_TITLE, client_name=TORRENT_NAME, status='pending', series_id=1331,
                is_pack=0, created_at='2026-10-06 21:04:34'):
        conn = sqlite3.connect(self.db.name)
        conn.execute('INSERT INTO active_downloads (id, title, client, status, series_id, is_pack, client_item_name, created_at) '
                     "VALUES (?, ?, 'qbittorrent', ?, ?, ?, ?, ?)",
                     (row_id, title, status, series_id, is_pack, client_name, created_at))
        conn.commit()
        conn.close()

    def status(self, row_id):
        conn = sqlite3.connect(self.db.name)
        row = conn.execute('SELECT status FROM active_downloads WHERE id = ?', (row_id,)).fetchone()
        conn.close()
        return row[0]

    def log_import(self, filename=IMPORTED_FILE, action='imported'):
        import_history.log_import_file('op', filename, '/downloads/torrents/' + filename, '', 'Pieds Nickelés (Le meilleur des)',
                                       action, 'success', '', None)

    # --- matching
    def test_import_matches_a_tracking_row_through_the_real_torrent_name(self):
        rows = downloader.prepare_trackable_downloads_for_matching([
            {'id': 4675, 'title': SEARCH_TITLE, 'client_item_name': TORRENT_NAME, 'series_id': 1331,
             'volume_id': None, 'volume_number': None},
        ])
        self.assertEqual(downloader.match_filename_against_trackable_downloads(IMPORTED_FILE, rows)['tracking_id'], 4675)
        rows[0]['client_item_name'] = None
        rows[0]['_normalized_client_item_name'] = ''
        self.assertIsNone(downloader.match_filename_against_trackable_downloads(IMPORTED_FILE, rows))

    # --- sibling rows closed at import time
    def test_import_closes_the_twin_rows_of_the_same_release_only(self):
        self.add_row(4682, title=TORRENT_NAME[:-4], client_name=TORRENT_NAME[:-4], status='imported')
        self.add_row(4675)                                                    # jumelle périmée
        self.add_row(4690, title='Autre.Release.T09', client_name='Le.Meilleur.des.Pieds.Nickeles.T09.2011.FRENCH.TONER.cbz')
        self.add_row(4691, series_id=2)                                       # autre série
        self.add_row(4692, is_pack=1)                                         # pack: jamais fermé ici
        closed = downloader.finalize_sibling_downloads(IMPORTED_FILE, 1331, 4682, 'imported')
        self.assertEqual(closed, 1)
        self.assertEqual(self.status(4675), 'imported')
        for untouched in (4690, 4691, 4692):
            self.assertEqual(self.status(untouched), 'pending', untouched)

    def test_skipped_duplicates_close_twins_as_skipped(self):
        self.add_row(4675)
        downloader.finalize_sibling_downloads(IMPORTED_FILE, 1331, None, 'skipped')
        self.assertEqual(self.status(4675), 'skipped')

    # --- periodic safety net
    def test_reconcile_closes_a_pending_row_whose_file_was_already_imported(self):
        self.add_row(4675)
        self.log_import()
        self.assertEqual(downloader.reconcile_completed_download_files(), 1)
        self.assertEqual(self.status(4675), 'imported')

    def test_reconcile_only_closes_as_skipped_when_every_match_was_skipped(self):
        self.add_row(4675)
        self.log_import(action='skipped')
        downloader.reconcile_pending_downloads_with_imports()
        self.assertEqual(self.status(4675), 'skipped')

    def test_reconcile_ignores_imports_older_than_the_row(self):
        self.add_row(4675, created_at='2999-01-01 00:00:00')   # créée APRÈS l'import
        self.log_import()
        self.assertEqual(downloader.reconcile_pending_downloads_with_imports(), 0)
        self.assertEqual(self.status(4675), 'pending')

    def test_reconcile_ignores_other_series_short_names_and_packs(self):
        self.add_row(4691, series_id=2)
        self.add_row(4692, is_pack=1)
        self.add_row(4693, client_name='Tome 5')
        self.add_row(4694, client_name=None, title=SEARCH_TITLE)
        self.log_import()
        self.log_import(filename='Serie Tome 5 xyz.cbz')
        self.assertEqual(downloader.reconcile_pending_downloads_with_imports(), 0)
        for row_id in (4691, 4692, 4693, 4694):
            self.assertEqual(self.status(row_id), 'pending', row_id)

    def test_import_flow_is_wired_to_close_twins(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / 'blueprints/library/routes.py').read_text(encoding='utf-8')
        self.assertIn('finalize_sibling_downloads(', source)
        self.assertLess(source.index('_maybe_complete_tracking_after_move(\n                source_path, destination'),
                        source.index('finalize_sibling_downloads('))


if __name__ == '__main__':
    unittest.main()
