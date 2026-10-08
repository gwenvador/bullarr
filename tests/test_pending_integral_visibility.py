import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from blueprints.missing_monitor import downloader


def _make_database(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE series (
            id INTEGER PRIMARY KEY, title TEXT, path TEXT, is_oneshot INTEGER DEFAULT 0
        );
        CREATE TABLE volumes (
            id INTEGER PRIMARY KEY, series_id INTEGER, volume_number INTEGER,
            is_integral INTEGER DEFAULT 0, integral_number INTEGER,
            is_hs INTEGER DEFAULT 0, hs_number INTEGER,
            is_episode INTEGER DEFAULT 0, episode_number INTEGER,
            filepath TEXT
        );
        CREATE TABLE active_downloads (
            id INTEGER PRIMARY KEY, title TEXT, client TEXT, status TEXT,
            created_at TEXT, completed_at TEXT, series_id INTEGER, volume_id INTEGER,
            volume_number INTEGER, bytes_downloaded INTEGER, bytes_total INTEGER,
            last_progress_at TEXT, retry_count INTEGER DEFAULT 0, is_pack INTEGER DEFAULT 0,
            expected_volume_count INTEGER, client_item_id TEXT, is_integral INTEGER DEFAULT 0,
            integral_number INTEGER, is_hs INTEGER DEFAULT 0, hs_number INTEGER,
            is_episode INTEGER DEFAULT 0, episode_number INTEGER
        );
        INSERT INTO series(id, title, path, is_oneshot) VALUES (1, 'Imago Mundi', 'Imago Mundi', 0);
        INSERT INTO volumes(series_id, is_integral, integral_number, filepath)
            VALUES (1, 1, 1, '/BD/Imago Mundi/INT01.cbz');
        """
    )
    for download_id, title in (
        (10, 'Imago Mundi INT02 (2011)@BD_fr.cbz'),
        (11, 'Imago Mundi INT01 (2011)@BD_fr.cbz'),
    ):
        conn.execute(
            """
            INSERT INTO active_downloads(
                id, title, client, status, created_at, series_id,
                bytes_downloaded, bytes_total
            ) VALUES (?, ?, 'telegram', 'pending', '2026-01-01 00:00:00', 1, 1, 2)
            """,
            (download_id, title),
        )
    conn.commit()
    conn.close()


class PendingIntegralVisibilityTest(unittest.TestCase):
    """La base fait référence: tout téléchargement en attente est listé, y compris quand le
    tome est déjà possédé (alors signalé par already_owned, jamais masqué)."""

    def test_pending_integrals_are_all_listed_and_owned_ones_are_flagged_by_integral_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / 'bullarr.db'
            _make_database(db_path)
            app = Flask(__name__)
            app.config['DATABASE'] = str(db_path)

            fake_scanner = types.ModuleType('blueprints.library.scanner')

            class FakeLibraryScanner:
                @staticmethod
                def parse_filename(title):
                    number = int(title.split('INT', 1)[1].split()[0])
                    return {
                        'is_integral': True, 'integral_number': number,
                        'is_hs': False, 'hs_number': None,
                        'is_episode': False, 'episode_number': None,
                        'volume': None,
                    }

            fake_scanner.LibraryScanner = FakeLibraryScanner
            fake_library = types.ModuleType('blueprints.library')
            fake_library.__path__ = []

            with patch.dict(sys.modules, {
                'blueprints.library': fake_library,
                'blueprints.library.scanner': fake_scanner,
            }), app.app_context(), patch.object(downloader, '_revert_stale_importing_downloads'), \
                    patch.object(downloader, 'reconcile_stale_active_downloads'), \
                    patch.object(downloader, '_reconcile_stuck_completed_download', return_value=False), \
                    patch.object(downloader, 'remove_completed_pack_if_owned', return_value=False):
                pending = downloader.get_pending_downloads()

        by_id = {item['id']: item for item in pending}
        self.assertEqual(set(by_id), {10, 11})
        self.assertEqual((by_id[10]['integral_number'], by_id[10]['already_owned']), (2, False))
        self.assertEqual((by_id[11]['integral_number'], by_id[11]['already_owned']), (1, True))
        self.assertTrue(by_id[10]['is_integral'])


if __name__ == '__main__':
    unittest.main()
