import os
import sqlite3
import tempfile
import unittest

from config import Config


class LibraryEditTest(unittest.TestCase):
    def setUp(self):
        os.makedirs('data', exist_ok=True)
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        Config._init_database(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO libraries (id, name, path, description) VALUES (1, 'BD', '/BD', 'ancienne')")
        conn.execute("INSERT INTO libraries (id, name, path) VALUES (2, 'Comics', '/Comics')")
        conn.commit()
        conn.close()
        from app import create_app
        self.app = create_app()
        self.app.config.update(DATABASE=self.db_path, TESTING=True)
        self.client = self.app.test_client()

    def tearDown(self):
        os.remove(self.db_path)

    def row(self, library_id):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute('SELECT name, path, description FROM libraries WHERE id = ?', (library_id,)).fetchone()
        finally:
            conn.close()

    def test_updates_name_and_description_but_never_path(self):
        response = self.client.put('/api/libraries/1', json={'name': ' Bandes dessinées ', 'description': 'Collection', 'path': '/ailleurs'})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.row(1), ('Bandes dessinées', '/BD', 'Collection'))

    def test_description_alone_keeps_name(self):
        self.assertEqual(self.client.put('/api/libraries/1', json={'description': ''}).status_code, 200)
        self.assertEqual(self.row(1), ('BD', '/BD', ''))

    def test_rejects_empty_or_duplicate_name(self):
        self.assertEqual(self.client.put('/api/libraries/1', json={'name': '  '}).status_code, 400)
        response = self.client.put('/api/libraries/1', json={'name': 'Comics'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('existe déjà', response.get_json()['error'])
        self.assertEqual(self.client.put('/api/libraries/1', json={}).status_code, 400)
        self.assertEqual(self.client.put('/api/libraries/99', json={'name': 'X'}).status_code, 404)


if __name__ == '__main__':
    unittest.main()
