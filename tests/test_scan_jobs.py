"""Manual scans run in the background and keep queryable outcomes."""
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from flask import Flask

from blueprints.library import library_bp
from blueprints.library.scan_jobs import get_scan, init_scan_jobs, start_scan


class ScanJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = os.path.join(self.tmp.name, 'bullarr.db')
        with sqlite3.connect(self.database) as conn:
            conn.execute('CREATE TABLE libraries (id INTEGER PRIMARY KEY, name TEXT, path TEXT)')
            conn.execute('INSERT INTO libraries VALUES (1, ?, ?)', ('Test', self.tmp.name))
            conn.execute('CREATE TABLE series (id INTEGER PRIMARY KEY)')
            conn.execute('INSERT INTO series VALUES (2)')
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, DATABASE=self.database)
        self.app.register_blueprint(library_bp)
        init_scan_jobs(self.database)

    def test_job_is_queryable_and_duplicate_is_rejected(self):
        started = threading.Event()
        release = threading.Event()
        def scan(*_args):
            started.set()
            self.assertTrue(release.wait(5))
            return 3
        with patch('blueprints.library.scan_service.scan_library_and_sync', side_effect=scan):
            job_id = start_scan(self.app, 'library', 1)
            self.assertTrue(started.wait(5))
            self.assertEqual(get_scan(self.database, job_id)['status'], 'running')
            self.assertIsNone(start_scan(self.app, 'series', 2))
            release.set()
            for _ in range(100):
                job = get_scan(self.database, job_id)
                if job['status'] != 'running':
                    break
                threading.Event().wait(0.01)
        self.assertEqual(job['status'], 'succeeded')
        self.assertEqual(job['result'], {'series_count': 3})

    def test_start_and_status_routes(self):
        with patch('blueprints.library.scan_jobs.start_scan', return_value='job-1'):
            with self.app.test_client() as client:
                response = client.post('/api/scan/1')
                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.json['job_id'], 'job-1')
                self.assertEqual(client.get('/api/scan/1').status_code, 405)
                self.assertEqual(client.get('/api/scan/jobs/missing').status_code, 404)

    def test_restart_marks_inflight_job_interrupted(self):
        with sqlite3.connect(self.database) as conn:
            conn.execute("INSERT INTO scan_jobs (id, kind, target_id, status) VALUES ('old', 'library', 1, 'running')")
        init_scan_jobs(self.database)
        self.assertEqual(get_scan(self.database, 'old')['status'], 'interrupted')


if __name__ == '__main__':
    unittest.main()
