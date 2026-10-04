import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


class OlderNouveautesRangeTests(unittest.TestCase):
    def test_telegram_query_returns_only_newly_requested_older_period(self):
        from blueprints.telegram_channels.scraper import get_latest_files

        conn = sqlite3.connect(':memory:')
        conn.execute('''CREATE TABLE telegram_files (
            id INTEGER PRIMARY KEY, channel TEXT, channel_title TEXT,
            message_id INTEGER, filename TEXT, file_size INTEGER,
            message_date TEXT, filename_normalized TEXT
        )''')
        now = datetime.now(timezone.utc)
        for days_ago, name in ((30, 'recent.cbz'), (90, 'older.cbz'), (130, 'too-old.cbz')):
            stamp = (now - timedelta(days=days_ago)).strftime('%Y-%m-%d %H:%M:%S')
            conn.execute('INSERT INTO telegram_files VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                         (days_ago, 'chan', 'Channel', days_ago, name, 1, stamp, name))
        conn.commit()

        with patch('blueprints.telegram_channels.scraper._connect_db', return_value=conn):
            rows = get_latest_files(days=120, older_than_days=60)

        self.assertEqual([row['filename'] for row in rows], ['older.cbz'])

    def test_precomputed_series_index_preserves_exact_prefix_and_ambiguity_rules(self):
        from blueprints.library.routes import (
            _build_auto_import_series_match_index,
            _match_series_for_auto_import,
        )

        rows = [
            (1, 1, '/bd', 'Alpha', 0, None),
            (2, 1, '/bd', 'Alpha Beta', 0, None),
            (5, 1, '/bd', 'Gamma', 0, None),
            (3, 1, '/bd', 'Doublon', 0, None),
            (4, 1, '/bd', 'Doublon', 0, None),
        ]
        index = _build_auto_import_series_match_index(rows)
        self.assertEqual(_match_series_for_auto_import('alpha', rows, index)[0], 1)
        self.assertEqual(_match_series_for_auto_import('gamma tome', rows, index)[0], 5)
        self.assertIsNone(_match_series_for_auto_import('doublon', rows, index))

    def test_frontend_loads_only_delta_and_shows_spinner_without_empty_state(self):
        source = (Path(__file__).resolve().parents[1] / 'static/js/ebdz-latest.js').read_text()
        self.assertIn('olderThanDays', source)
        self.assertIn('nouveautes-load-older-spinner', source)
        self.assertIn("svgIcon('loader-circle', 'icon-spin')", source)
        self.assertIn("if (empty) empty.style.display = 'none'", source)
        self.assertIn("nouveautesHasMoreEbdz && currentNouveautesFilter !== 'rss'", source)
        self.assertIn('appendEvents', source)
        self.assertIn('older_than_days=', source)


if __name__ == '__main__':
    unittest.main()
