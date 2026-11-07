import errno
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from config import Config


class SeriesMoveTest(unittest.TestCase):
    def setUp(self):
        os.makedirs('data', exist_ok=True)
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        Config._init_database(self.db_path)
        from blueprints.library.scanner import LibraryScanner
        LibraryScanner(self.db_path)  # colonnes Komga/EBDZ ajoutées au démarrage
        self.root = tempfile.mkdtemp()
        self.bd = os.path.join(self.root, 'BD')
        self.comics = os.path.join(self.root, 'Comics')
        self.series_path = os.path.join(self.bd, 'Marvel', 'Spider-Man')
        os.makedirs(self.series_path)
        os.makedirs(self.comics)
        with open(os.path.join(self.series_path, 'T01.cbz'), 'wb') as handle:
            handle.write(b'cbz')
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO libraries (id, name, path) VALUES (1, 'BD', ?)", (self.bd,))
        conn.execute("INSERT INTO libraries (id, name, path) VALUES (2, 'Comics', ?)", (self.comics,))
        conn.execute("""INSERT INTO series (id, library_id, title, path, total_volumes, missing_volumes, has_parts, komga_series_id)
                        VALUES (1, 1, 'Spider-Man', ?, 1, '[]', 0, 'K1')""", (self.series_path,))
        conn.execute("""INSERT INTO series (id, library_id, title, path, total_volumes, missing_volumes, has_parts)
                        VALUES (2, 1, 'Sans fichiers', ?, 0, '[]', 0)""", (os.path.join(self.bd, 'Sans fichiers'),))
        conn.execute("""INSERT INTO series (id, library_id, title, path, total_volumes, missing_volumes, has_parts)
                        VALUES (3, 1, 'Racine', ?, 0, '[]', 0)""", (self.bd,))
        conn.execute("INSERT INTO volumes (series_id, volume_number, filename, filepath) VALUES (1, 1, 'T01.cbz', ?)",
                     (os.path.join(self.series_path, 'T01.cbz'),))
        conn.commit()
        conn.close()
        from app import create_app
        self.app = create_app()
        self.app.config.update(DATABASE=self.db_path, TESTING=True)
        self.client = self.app.test_client()

    def tearDown(self):
        os.remove(self.db_path)
        shutil.rmtree(self.root, ignore_errors=True)

    def move(self, ids, library_id=2):
        with mock.patch('blueprints.library.series_move._relink_komga'):
            response = self.client.post('/api/series/move', json={'series_ids': ids, 'library_id': library_id})
            self.assertEqual(response.status_code, 202, response.get_json())
            job_id = response.get_json()['job_id']
            for _ in range(100):
                job = self.client.get(f'/api/series/move/{job_id}').get_json()['job']
                if job['status'] != 'running':
                    return job
                time.sleep(0.05)
        self.fail('déplacement trop long')

    def db(self, sql, *args):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def check_moved(self):
        new_path = os.path.join(self.comics, 'Marvel', 'Spider-Man')
        self.assertTrue(os.path.isfile(os.path.join(new_path, 'T01.cbz')))
        self.assertFalse(os.path.exists(os.path.join(self.bd, 'Marvel')))  # dossier d'univers vidé retiré
        self.assertEqual(self.db('SELECT library_id, path, komga_series_id FROM series WHERE id = 1'), [(2, new_path, None)])
        self.assertEqual(self.db('SELECT filepath FROM volumes WHERE series_id = 1'), [(os.path.join(new_path, 'T01.cbz'),)])

    def test_moves_folder_and_rewrites_paths(self):
        job = self.move([1, 2])
        self.assertEqual(job['status'], 'succeeded', job)
        self.assertTrue(all(r['success'] for r in job['results']), job)
        self.check_moved()
        # Série sans dossier sur le disque : seule la base change.
        self.assertEqual(self.db('SELECT library_id FROM series WHERE id = 2'), [(2,)])

    def test_copies_across_mounts(self):
        real_rename = os.rename
        calls = []

        def rename(src, dst):
            calls.append(dst)
            if dst.endswith('Spider-Man') and not src.endswith('.bullarr-move'):
                raise OSError(errno.EXDEV, 'Invalid cross-device link')
            return real_rename(src, dst)

        with mock.patch('blueprints.library.series_move.os.rename', side_effect=rename):
            job = self.move([1])
        self.assertTrue(job['results'][0]['success'], job)
        self.check_moved()
        self.assertFalse(any(os.path.exists(path + '.bullarr-move') for path in calls))

    def test_refuses_root_folder_and_existing_destination(self):
        os.makedirs(os.path.join(self.comics, 'Marvel', 'Spider-Man'))
        job = self.move([1, 3])
        errors = {r['title']: r.get('error') for r in job['results']}
        self.assertIn('existe déjà', errors['Spider-Man'])
        self.assertIn('racine', errors['Racine'])
        self.assertTrue(os.path.isfile(os.path.join(self.series_path, 'T01.cbz')))
        self.assertEqual(self.db('SELECT library_id FROM series WHERE id IN (1, 3) ORDER BY id'), [(1,), (1,)])

    def test_database_failure_puts_folder_back(self):
        with mock.patch('blueprints.library.series_move._rewrite_series_paths', side_effect=sqlite3.OperationalError('verrou')):
            job = self.move([1])
        self.assertFalse(job['results'][0]['success'])
        self.assertIn('Base non mise à jour', job['results'][0]['error'])
        self.assertTrue(os.path.isfile(os.path.join(self.series_path, 'T01.cbz')))
        self.assertFalse(os.path.exists(os.path.join(self.comics, 'Marvel', 'Spider-Man')))
        self.assertEqual(self.db('SELECT library_id, path FROM series WHERE id = 1'), [(1, self.series_path)])

    def test_same_library_is_skipped_and_bad_target_rejected(self):
        self.assertTrue(self.move([1], library_id=1)['results'][0]['skipped'])
        self.assertEqual(self.client.post('/api/series/move', json={'series_ids': [1], 'library_id': 99}).status_code, 404)
        self.assertEqual(self.client.post('/api/series/move', json={'series_ids': [], 'library_id': 2}).status_code, 400)


if __name__ == '__main__':
    unittest.main()
