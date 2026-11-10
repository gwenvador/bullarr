"""Avancement de lecture lu dans Komga, affiché dans la bibliothèque (barre « X/Y lus » par
série, état de chaque tome dans la fiche série).

Bullarr envoyait déjà à Komga la page lue dans son lecteur (update_read_progress) ; ici on
lit l'avancement du compte de la clé API, quel que soit le lecteur utilisé (Komga, Mihon,
Tachiyomi, le lecteur de Bullarr...). Synchronisé toutes les 15 minutes, sur demande
(POST /api/komga/read-progress/sync) et aussitôt après une page lue dans le lecteur de
Bullarr. Stocké dans komga_read_progress (voir LibraryScanner.init_database).

Chaque synchronisation rattache aussi à leur livre Komga les tomes qui ne l'étaient pas (ou
plus : fichier renommé, série matchée avant l'arrivée du fichier), par nom de fichier exact
dans la série Komga de la série - sans ce lien, l'avancement d'un tome ne peut pas s'afficher."""
import os
import sqlite3
import time
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from flask import current_app

SYNC_INTERVAL_MINUTES = 15


def _connect():
    return sqlite3.connect(current_app.config['DATABASE'], timeout=30.0)


# Dernière synchronisation (affichée par le bouton « Synchroniser avec Komga »).
last_sync = {'at': None, 'error': None, 'stats': None}


def _relink_volumes(conn, progress):
    """Rattache à leur livre Komga les tomes (avec fichier) d'une série matchée sur Komga dont
    le lien manque ou ne correspond plus : même nom de fichier (sans extension) dans la même
    série Komga. Retourne le nombre de tomes rattachés."""
    by_name = {}
    for book_id, book in progress.items():
        if book_id and book.get('series_id') and book.get('name'):
            by_name.setdefault((book['series_id'], book['name'].strip()), (book_id, book.get('url')))
    rows = conn.execute('''
        SELECT v.id, v.filename, v.komga_book_id, s.komga_series_id
        FROM volumes v JOIN series s ON s.id = v.series_id
        WHERE s.komga_series_id IS NOT NULL AND v.filepath IS NOT NULL AND v.filename IS NOT NULL
    ''').fetchall()
    known_ids = set(progress)
    updates = []
    for volume_id, filename, current, komga_series_id in rows:
        match = by_name.get((komga_series_id, os.path.splitext(filename)[0].strip()))
        if match and match[0] != current and (not current or current not in known_ids):
            updates.append((match[0], match[1], volume_id))
    conn.executemany('UPDATE volumes SET komga_book_id = ?, komga_book_url = ? WHERE id = ?', updates)
    return len(updates)


def sync_read_progress():
    """Relit l'avancement de tous les livres Komga et rattache les tomes qui ne l'étaient pas.
    Retourne {'books', 'read', 'reading', 'relinked', 'linked_volumes', 'volumes_with_progress'},
    ou None si Komga n'est pas activé. Lève KomgaError si Komga ne répond pas."""
    from .client import KomgaClient, KomgaError
    from .config_store import load_komga_config
    if not load_komga_config().get('enabled'):
        return None
    try:
        progress = KomgaClient().list_books_read_progress()
    except KomgaError as exc:
        last_sync.update(at=time.time(), error=str(exc))
        raise
    rows = [(book_id, p['page'], p['pages_count'], int(p['completed']), p['read_at'])
            for book_id, p in progress.items() if book_id and (p['completed'] or p['page'])]
    conn = _connect()
    try:
        relinked = _relink_volumes(conn, progress)
        conn.execute('DELETE FROM komga_read_progress')
        conn.executemany(
            'INSERT INTO komga_read_progress (komga_book_id, page, pages_count, completed, read_at, synced_at) '
            'VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)', rows)
        conn.commit()
        linked, with_progress = conn.execute('''
            SELECT COUNT(*), COUNT(p.komga_book_id) FROM volumes v
            LEFT JOIN komga_read_progress p ON p.komga_book_id = v.komga_book_id
            WHERE v.komga_book_id IS NOT NULL AND v.filepath IS NOT NULL
        ''').fetchone()
    finally:
        conn.close()
    stats = {
        'books': len(progress),
        'read': sum(1 for row in rows if row[3]),
        'reading': sum(1 for row in rows if not row[3]),
        'relinked': relinked,
        'linked_volumes': linked,
        'volumes_with_progress': with_progress,
    }
    last_sync.update(at=time.time(), error=None, stats=stats)
    return stats


def keep_reading(library_id=None, limit=20, source=None):
    """« Continuer la lecture » (comme le « Keep reading » de Komga) : tomes commencés et pas
    finis, du plus récemment lu au plus ancien."""
    if source is None:  # voir user_link.progress_source
        from .user_link import SERVER_SOURCE
        source = SERVER_SOURCE
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute('''
            SELECT v.id, v.series_id, v.volume_number, v.is_integral, v.integral_number, v.is_hs,
                   v.hs_number, v.filename, v.cover_path, v.page_count, s.title AS series_title,
                   s.local_cover_path, s.bedetheque_cover_path, s.komga_cover_path,
                   p.page, p.pages_count, p.read_at
            FROM {source} p
            JOIN volumes v ON v.id = p.vol_id AND v.filepath IS NOT NULL
            JOIN series s ON s.id = v.series_id
            WHERE COALESCE(p.completed, 0) = 0 AND COALESCE(p.page, 0) > 0
              AND (? IS NULL OR s.library_id = ?)
            ORDER BY p.read_at DESC, p.synced_at DESC
            LIMIT ?
        '''.format(source=source.sql), (*source.params, library_id, library_id, limit)).fetchall()
    finally:
        conn.close()
    items = []
    for row in rows:
        if row['is_integral']:
            label = f"Intégrale {row['integral_number'] or ''}".strip()
        elif row['is_hs']:
            label = f"HS {row['hs_number'] or ''}".strip()
        elif row['volume_number'] is not None:
            label = f"Tome {row['volume_number']}"
        else:
            label = os.path.splitext(row['filename'] or '')[0]
        items.append({
            'volume_id': row['id'], 'series_id': row['series_id'], 'series_title': row['series_title'],
            'label': label, 'page': row['page'], 'pages_count': row['pages_count'] or row['page_count'],
            'read_at': row['read_at'],
            'cover_path': row['cover_path'] or row['local_cover_path'] or row['bedetheque_cover_path']
                          or row['komga_cover_path'],
        })
    return items


def record_local_progress(komga_book_id, page, completed, pages_count=None):
    """Page lue dans le lecteur de Bullarr, envoyée à Komga avec succès : reflétée tout de
    suite dans la bibliothèque, sans attendre la prochaine synchronisation."""
    if not komga_book_id:
        return
    conn = _connect()
    try:
        if pages_count is None:
            # Livre jamais ouvert dans Komga : nombre de pages du tome tel que scanné.
            row = conn.execute('SELECT page_count FROM volumes WHERE komga_book_id = ? AND page_count IS NOT NULL LIMIT 1',
                               (komga_book_id,)).fetchone()
            pages_count = row[0] if row else None
        conn.execute(
            'INSERT INTO komga_read_progress (komga_book_id, page, pages_count, completed, read_at, synced_at) '
            "VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), CURRENT_TIMESTAMP) "
            'ON CONFLICT(komga_book_id) DO UPDATE SET page = excluded.page, completed = excluded.completed, '
            'pages_count = COALESCE(komga_read_progress.pages_count, excluded.pages_count), '
            'read_at = excluded.read_at, synced_at = excluded.synced_at',
            (komga_book_id, page, pages_count, int(bool(completed))))
        conn.commit()
    finally:
        conn.close()


def clear_local_progress(komga_book_id):
    """« Marquer comme non lu » envoyé à Komga avec succès : reflété tout de suite."""
    if not komga_book_id:
        return
    conn = _connect()
    try:
        conn.execute('DELETE FROM komga_read_progress WHERE komga_book_id = ?', (komga_book_id,))
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
            try:
                from .user_link import sync_all_users
                sync_all_users()
            except Exception as exc:
                print(f"⚠️ Synchronisation des comptes Komga reliés en échec: {exc}")


read_progress_scheduler = ReadProgressScheduler()
