"""Persistent status for manual scans executed outside request threads."""
import json
import logging
import sqlite3
import threading
import uuid
from contextlib import contextmanager

logger = logging.getLogger(__name__)


@contextmanager
def _connect(database):
    conn = sqlite3.connect(database, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_scan_jobs(database):
    with _connect(database) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS scan_jobs (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            target_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            force INTEGER NOT NULL DEFAULT 0,
            result_json TEXT,
            error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at TEXT
        )''')
        # A crashed/restarted worker cannot resume an in-memory scan thread.
        conn.execute("UPDATE scan_jobs SET status='interrupted', finished_at=CURRENT_TIMESTAMP "
                     "WHERE status='running'")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_active_scan "
                     "ON scan_jobs((1)) WHERE status='running'")


def start_scan(app, kind, target_id, force=False):
    if kind not in ('library', 'series'):
        raise ValueError('Invalid scan kind')
    job_id = str(uuid.uuid4())
    database = app.config['DATABASE']
    with _connect(database) as conn:
        try:
            conn.execute('INSERT INTO scan_jobs (id, kind, target_id, status, force) '
                         'VALUES (?, ?, ?, ?, ?)', (job_id, kind, target_id, 'running', int(force)))
        except sqlite3.IntegrityError:
            return None
    thread = threading.Thread(target=_run_scan, args=(app, job_id, kind, target_id, force), daemon=True)
    try:
        thread.start()
    except Exception:
        with _connect(database) as conn:
            conn.execute("UPDATE scan_jobs SET status='failed', error='Impossible de démarrer le scan', "
                         'finished_at=CURRENT_TIMESTAMP WHERE id=?', (job_id,))
        raise
    return job_id


def _run_scan(app, job_id, kind, target_id, force):
    database = app.config['DATABASE']
    try:
        with app.app_context():
            from .scan_service import scan_library_and_sync, scan_series_and_sync
            if kind == 'library':
                with _connect(database) as conn:
                    row = conn.execute('SELECT path FROM libraries WHERE id=?', (target_id,)).fetchone()
                if row is None:
                    raise ValueError('Bibliothèque supprimée avant le scan')
                count = scan_library_and_sync(target_id, row['path'], force)
                result = {'series_count': count}
            else:
                try:
                    count = scan_series_and_sync(target_id, force)
                    result = {'volumes_count': count}
                except Exception as exc:
                    from .scanner import SeriesDirectoryMissingError
                    if not isinstance(exc, SeriesDirectoryMissingError):
                        raise
                    result = {'deleted': True, 'message': f'Répertoire introuvable, série "{exc}" supprimée de la bibliothèque'}
        with _connect(database) as conn:
            conn.execute("UPDATE scan_jobs SET status='succeeded', result_json=?, "
                         'finished_at=CURRENT_TIMESTAMP WHERE id=?', (json.dumps(result), job_id))
    except Exception:
        logger.exception('Scan job %s failed', job_id)
        with _connect(database) as conn:
            conn.execute("UPDATE scan_jobs SET status='failed', error='Erreur lors du scan', "
                         'finished_at=CURRENT_TIMESTAMP WHERE id=?', (job_id,))


def get_scan(database, job_id):
    with _connect(database) as conn:
        row = conn.execute('SELECT * FROM scan_jobs WHERE id=?', (job_id,)).fetchone()
    if row is None:
        return None
    return {
        'id': row['id'], 'kind': row['kind'], 'target_id': row['target_id'],
        'status': row['status'], 'force': bool(row['force']),
        'result': json.loads(row['result_json']) if row['result_json'] else None,
        'error': row['error'], 'created_at': row['created_at'],
        'finished_at': row['finished_at'],
    }
