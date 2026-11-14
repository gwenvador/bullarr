import os
import sqlite3
import tempfile
import unittest

from config import Config


class HistoryFallbackTest(unittest.TestCase):
    def setUp(self):
        os.makedirs('data', exist_ok=True)
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        Config._init_database(self.db_path)
        from blueprints.library.scanner import LibraryScanner
        LibraryScanner(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO libraries (id, name, path) VALUES (1, 'BD', '/tmp/bd')")
        conn.execute("""INSERT INTO series (id, library_id, title, path, total_volumes, missing_volumes, has_parts)
                        VALUES (1, 1, 'Blacksad', '/tmp/bd/Blacksad', 0, '[]', 0)""")
        conn.commit()
        conn.close()
        from app import create_app
        self.app = create_app()
        self.app.config.update(DATABASE=self.db_path, TESTING=True)
        from blueprints.library.action_history import init_action_history_table
        with self.app.app_context():
            init_action_history_table()
        self.client = self.app.test_client()

    def tearDown(self):
        os.remove(self.db_path)

    def rows(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute('SELECT action_type, series_id, series_title, detail FROM action_history').fetchall()
        finally:
            conn.close()

    def test_every_listed_endpoint_exists(self):
        from blueprints.library.history_fallback import ACTIONS, SETTINGS
        endpoints = {rule.endpoint for rule in self.app.url_map.iter_rules()}
        self.assertEqual(sorted((set(ACTIONS) | set(SETTINGS)) - endpoints), [])

    def test_unlogged_action_gets_a_history_line(self):
        response = self.client.post('/api/series/1/toggle-oneshot')
        self.assertLess(response.status_code, 400, response.get_data(as_text=True))
        rows = self.rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0][:3], ('series_edit', 1, 'Blacksad'))

    def test_failed_or_read_only_requests_are_not_logged(self):
        self.client.post('/api/series/999/toggle-oneshot')
        self.client.get('/api/series/1/volumes')
        self.assertEqual(self.rows(), [])

    def test_no_duplicate_when_action_logs_itself(self):
        from blueprints.library.action_history import log_action
        with self.app.test_request_context('/api/series/1/toggle-oneshot', method='POST'):
            log_action('rename', 1, 'Blacksad', 'déjà journalisé')
            from flask import g
            self.assertTrue(getattr(g, '_history_logged', False))


if __name__ == '__main__':
    unittest.main()
