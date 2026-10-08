import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask

from blueprints.library import import_history, routes
from blueprints.missing_monitor import downloader

TWIN_NAME = 'Le.Meilleur.des.Pieds.Nickeles.T05.2006.FRENCH.HYBRiD.COMiC.CBZ.eBook-TONER'


class DownloadsFollowTheDatabaseTest(unittest.TestCase):
    """La base fait référence: ce qui est en téléchargement ou en attente d'import s'affiche sur
    la page Import (même si le tome est déjà possédé), et une ligne n'est refermée que lorsque
    SON propre téléchargement est terminé chez le client."""

    def setUp(self):
        self.db = tempfile.NamedTemporaryFile(suffix='.db')
        self.app = Flask(__name__)
        self.app.config['DATABASE'] = self.db.name
        self.app.config['IMPORT_DIRECTORIES'] = []
        self.ctx = self.app.app_context()
        self.ctx.push()
        import_history.init_import_history_table()
        conn = sqlite3.connect(self.db.name)
        conn.executescript('''
            CREATE TABLE series (id INTEGER PRIMARY KEY, title TEXT, path TEXT, is_oneshot INTEGER DEFAULT 0);
            CREATE TABLE volumes (id INTEGER PRIMARY KEY, series_id INTEGER, volume_number INTEGER,
                is_integral INTEGER DEFAULT 0, integral_number INTEGER, is_hs INTEGER DEFAULT 0, hs_number INTEGER,
                is_episode INTEGER DEFAULT 0, episode_number INTEGER, filepath TEXT);
            CREATE TABLE active_downloads (
                id INTEGER PRIMARY KEY, title TEXT, client TEXT, status TEXT, created_at TEXT, completed_at TEXT,
                series_id INTEGER, volume_id INTEGER, volume_number INTEGER, bytes_downloaded INTEGER,
                bytes_total INTEGER, last_progress_at TEXT, retry_count INTEGER DEFAULT 0, is_pack INTEGER DEFAULT 0,
                expected_volume_count INTEGER, client_item_id TEXT, client_item_name TEXT, force_replace INTEGER DEFAULT 0,
                is_integral INTEGER DEFAULT 0, integral_number INTEGER, is_hs INTEGER DEFAULT 0, hs_number INTEGER,
                is_episode INTEGER DEFAULT 0, episode_number INTEGER);
            INSERT INTO series VALUES (1331, 'Pieds Nickelés (Le meilleur des)', '/BD/PN', 0);
        ''')
        for number in (1, 2, 3, 5, 6, 8):
            conn.execute("INSERT INTO volumes (series_id, volume_number, filepath) VALUES (1331, ?, ?)",
                         (number, f'/BD/PN/PN - #{number:02d}.cbz'))
        conn.commit()
        conn.close()

    def tearDown(self):
        self.ctx.pop()
        self.db.close()

    def add_row(self, row_id, status='pending', volume_number=None, client='qbittorrent', client_item_id=None,
                client_name=None, title=None, series_id=1331, is_pack=0, created_at='2026-10-06 21:04:34'):
        conn = sqlite3.connect(self.db.name)
        conn.execute('INSERT INTO active_downloads (id, title, client, status, created_at, series_id, volume_number, '
                     'is_pack, client_item_id, client_item_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                     (row_id, title or client_name or f'row {row_id}', client, status, created_at, series_id,
                      volume_number, is_pack, client_item_id, client_name))
        conn.commit()
        conn.close()

    def row(self, row_id):
        conn = sqlite3.connect(self.db.name)
        result = conn.execute('SELECT status, completed_at FROM active_downloads WHERE id = ?', (row_id,)).fetchone()
        conn.close()
        return result

    def pending_page(self):
        with mock.patch.object(downloader, '_revert_stale_importing_downloads'), \
                mock.patch.object(downloader, 'reconcile_stale_active_downloads'), \
                mock.patch.object(downloader, 'remove_completed_pack_if_owned', return_value=False):
            return {item['id']: item for item in downloader.get_pending_downloads()}

    # --- the Import page follows the database
    def test_every_unfinished_row_is_listed_even_when_the_volume_is_already_owned(self):
        for row_id, volume in ((4685, 1), (4683, 2), (4684, 3), (4682, 5), (4679, 6), (4681, 8)):
            self.add_row(row_id, volume_number=volume,
                         title=f'Le.Meilleur.des.Pieds.Nickeles.T0{volume}.FRENCH.TONER')
        self.add_row(4690, volume_number=4, title='Le.Meilleur.des.Pieds.Nickeles.T04.FRENCH.TONER')   # non possédé
        self.add_row(4700, status='completed', volume_number=2, title='Pieds Nickeles T02 pret.cbz')
        self.add_row(4701, status='importing', volume_number=3, title='Pieds Nickeles T03 import.cbz')
        self.add_row(4702, status='imported', volume_number=1, title='deja termine.cbz')
        self.add_row(4703, status='skipped', volume_number=1, title='doublon ecarte.cbz')
        page = self.pending_page()
        self.assertEqual(set(page), {4685, 4683, 4684, 4682, 4679, 4681, 4690, 4700, 4701})
        self.assertTrue(all(page[i]['already_owned'] for i in (4685, 4683, 4684, 4682, 4679, 4681, 4700, 4701)))
        self.assertFalse(page[4690]['already_owned'])
        self.assertEqual(page[4700]['status'], 'completed')
        self.assertEqual(page[4701]['status'], 'importing')

    def test_the_page_script_tells_the_user_the_volume_is_already_owned(self):
        source = (Path(__file__).resolve().parents[1] / 'static/js/import.js').read_text(encoding='utf-8')
        self.assertIn('pending.already_owned', source)
        self.assertIn('déjà possédé', source)

    # --- reading the client
    def test_incomplete_client_items_are_read_from_each_client_and_failures_are_ignored(self):
        def qbittorrent():
            return {'items': [{'id': 'AAA', 'progress': 0}, {'id': 'bbb', 'progress': 100},
                              {'id': 'ccc', 'progress': 42.5}, {'id': 'ddd', 'progress': None}, {'id': None, 'progress': 0}]}

        def broken():
            raise RuntimeError('client down')

        with mock.patch.dict('blueprints.activity.routes._CLIENT_STATUS_FNS',
                             {'qbittorrent': qbittorrent, 'deluge': broken}, clear=True):
            result = downloader.incomplete_client_item_ids(['qbittorrent', 'deluge', 'rtorrent'])
        self.assertEqual(result, {'qbittorrent': {'aaa', 'ccc'}, 'deluge': set(), 'rtorrent': set()})

    def test_download_still_incomplete_compares_ids_case_insensitively_and_fails_open(self):
        self.add_row(10, client_item_id='ABC123')
        self.add_row(11, client_item_id=None)
        with mock.patch.object(downloader, 'incomplete_client_item_ids', return_value={'qbittorrent': {'abc123'}}):
            self.assertTrue(downloader.download_still_incomplete(10))
            self.assertFalse(downloader.download_still_incomplete(11))
            self.assertFalse(downloader.download_still_incomplete(999))
        with mock.patch.object(downloader, 'incomplete_client_item_ids', side_effect=RuntimeError('boom')):
            self.assertFalse(downloader.download_still_incomplete(10))

    # --- a row is closed only when its own download is complete
    def test_twin_whose_torrent_is_still_downloading_is_not_closed_by_the_other_twins_file(self):
        self.add_row(4682, client_item_id='stalled', client_name=TWIN_NAME, title=TWIN_NAME)
        self.add_row(4675, client_item_id='done', client_name=TWIN_NAME + '.cbz', title='Int.Integrale.05.Pellos')
        with mock.patch.object(downloader, 'incomplete_client_item_ids', return_value={'qbittorrent': {'stalled'}}):
            closed = downloader.finalize_sibling_downloads(TWIN_NAME + '.cbr', 1331, None, 'imported')
        self.assertEqual(closed, 1)
        self.assertEqual(self.row(4675)[0], 'imported')
        self.assertEqual(self.row(4682), ('pending', None))

    def test_periodic_reconcile_never_closes_a_row_still_downloading(self):
        self.add_row(4682, client_item_id='stalled', client_name=TWIN_NAME)
        self.add_row(4675, client_item_id='done', client_name=TWIN_NAME + '.cbz')
        import_history.log_import_file('op', TWIN_NAME + '.cbr', '/downloads/torrents/x.cbr', '',
                                       'Pieds Nickelés (Le meilleur des)', 'imported', 'success', '', None)
        with mock.patch.object(downloader, 'incomplete_client_item_ids', return_value={'qbittorrent': {'stalled'}}):
            self.assertEqual(downloader.reconcile_pending_downloads_with_imports(), 1)
        self.assertEqual(self.row(4675)[0], 'imported')
        self.assertEqual(self.row(4682)[0], 'pending')

    def test_import_keeps_the_primary_row_in_progress_when_its_own_torrent_is_incomplete(self):
        self.add_row(20, status='importing', client_item_id='stalled')
        with mock.patch.object(downloader, 'download_still_incomplete', return_value=True):
            routes._maybe_complete_tracking_after_move('/downloads/torrents/x.cbr', {'tracking_id': 20}, outcome='imported')
        self.assertEqual(self.row(20), ('pending', None))

    def test_import_closes_the_primary_row_when_its_torrent_is_complete(self):
        self.add_row(21, status='importing', client_item_id='done')
        with mock.patch.object(downloader, 'download_still_incomplete', return_value=False):
            routes._maybe_complete_tracking_after_move('/downloads/torrents/x.cbr', {'tracking_id': 21}, outcome='imported')
        self.assertEqual(self.row(21)[0], 'imported')

    def test_a_client_outage_does_not_block_closing_a_row(self):
        self.add_row(22, status='importing', client_item_id='x')
        with mock.patch.object(downloader, 'incomplete_client_item_ids', side_effect=RuntimeError('down')):
            routes._maybe_complete_tracking_after_move('/downloads/torrents/x.cbr', {'tracking_id': 22}, outcome='skipped')
        self.assertEqual(self.row(22)[0], 'skipped')


if __name__ == '__main__':
    unittest.main()
