"""Avancement de lecture lu dans Komga, affiché dans la bibliothèque (barre « X/Y lus » par
série, état de chaque tome dans la fiche série).

Bullarr envoyait déjà à Komga la page lue dans son lecteur (update_read_progress) ; ici on
lit l'avancement du compte de la clé API, quel que soit le lecteur utilisé (Komga, Mihon,
Tachiyomi, le lecteur de Bullarr...). Synchronisé toutes les 15 minutes, sur demande
(POST /api/komga/read-progress/sync) et aussitôt après une page lue dans le lecteur de
Bullarr. Stocké dans komga_read_progress (voir LibraryScanner.init_database)."""
import sqlite3
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from flask import current_app

SYNC_INTERVAL_MINUTES = 15


def _connect():
    return sqlite3.connect(current_app.config['DATABASE'], timeout=30.0)


def sync_read_progress():
    """Relit l'avancement de tous les livres Komga. Retourne {'books', 'read', 'reading'},
    ou None si Komga n'est pas activé. Lève KomgaError si Komga ne répond pas."""
    from .client import KomgaClient
    from .config_store import load_komga_config
    if not load_komga_config().get('enabled'):
        return None
    progress = KomgaClient().list_books_read_progress()
    rows = [(book_id, p['page'], p['pages_count'], int(p['completed']), p['read_at'])
            for book_id, p in progress.items() if book_id]
    conn = _connect()
    try:
        conn.execute('DELETE FROM komga_read_progress')
        conn.executemany(
            'INSERT INTO komga_read_progress (komga_book_id, page, pages_count, completed, read_at, synced_at) '
            'VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)', rows)
        conn.commit()
    finally:
        conn.close()
    return {
        'books': len(rows),
        'read': sum(1 for row in rows if row[3]),
        'reading': sum(1 for row in rows if not row[3] and row[1]),
    }


def record_local_progress(komga_book_id, page, completed, pages_count=None):
    """Page lue dans le lecteur de Bullarr, envoyée à Komga avec succès : reflétée tout de
    suite dans la bibliothèque, sans attendre la prochaine synchronisation."""
    if not komga_book_id:
        return
    conn = _connect()
    try:
        conn.execute(
            'INSERT INTO komga_read_progress (komga_book_id, page, pages_count, completed, read_at, synced_at) '
            "VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), CURRENT_TIMESTAMP) "
            'ON CONFLICT(komga_book_id) DO UPDATE SET page = excluded.page, completed = excluded.completed, '
            'pages_count = COALESCE(excluded.pages_count, komga_read_progress.pages_count), '
            'read_at = excluded.read_at, synced_at = excluded.synced_at',
            (komga_book_id, page, pages_count, int(bool(completed))))
        conn.commit()
    finally:
        conn.close()


class ReadProgressScheduler:
    def __init__(self):
        self.scheduler = None
        self.app = None

    def start(self, app):
        if self.scheduler is not None:
            return
        self.app = app
        self.scheduler = BackgroundScheduler(daemon=True)
        self.scheduler.add_job(
            func=self._run, trigger=IntervalTrigger(minutes=SYNC_INTERVAL_MINUTES),
            next_run_time=datetime.now() + timedelta(minutes=1),
            id='komga_read_progress', name='Komga read progress', replace_existing=True,
            coalesce=True, max_instances=1,
        )
        self.scheduler.start()
        print(f"✓ Synchronisation de l'avancement de lecture Komga programmée: toutes les {SYNC_INTERVAL_MINUTES} minutes")

    def _run(self):
        with self.app.app_context():
            try:
                sync_read_progress()
            except Exception as exc:
                print(f"⚠️ Synchronisation de la lecture Komga en échec: {exc}")


read_progress_scheduler = ReadProgressScheduler()
