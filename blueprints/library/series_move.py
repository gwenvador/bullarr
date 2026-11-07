"""Déplacement de séries d'une bibliothèque à une autre (barre de sélection de la page
bibliothèque, « Déplacer vers… »).

Pour chaque série : son dossier est déplacé sous la racine de la bibliothèque cible en
gardant son chemin relatif (un sous-dossier d'univers reste un sous-dossier d'univers),
puis series.library_id/path, volumes.filepath et l'historique d'import
(import_history_files.destination_path) sont réécrits dans la même transaction. Les
bibliothèques sont souvent des montages distincts (/BD et /Comics sur le NAS) : os.rename
échoue alors (EXDEV) et le dossier est copié puis supprimé, ce qui peut prendre du temps,
d'où un travail en arrière-plan suivi par GET /api/series/move/<job_id>.

Les liens Komga des séries déplacées sont retirés (Komga leur donne de nouveaux
identifiants en les retrouvant à leur nouvel emplacement) puis retentés par titre une
fois le scan Komga terminé."""
import errno
import logging
import os
import shutil
import sqlite3
import threading
import uuid

from flask import current_app, jsonify, request

from path_safety import UnsafePathError, resolve_within, series_folder_conflict
from . import library_bp
from .scanner import scan_import_lock

logger = logging.getLogger(__name__)
_jobs = {}
_jobs_lock = threading.Lock()


def _connect(database):
    conn = sqlite3.connect(database, timeout=120.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _replace_prefix(value, old, new):
    if value is None:
        return None
    if value == old:
        return new
    if value.startswith(old.rstrip('/') + '/'):
        return new.rstrip('/') + value[len(old.rstrip('/')):]
    return value


def _move_tree(source, destination):
    """Déplace un dossier, même entre deux montages : copie dans un nom temporaire, puis
    renommage (la destination n'apparaît que complète), puis suppression de la source."""
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    try:
        os.rename(source, destination)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    partial = destination + '.bullarr-move'
    if os.path.exists(partial):
        shutil.rmtree(partial)
    try:
        shutil.copytree(source, partial, copy_function=shutil.copy2)
        os.rename(partial, destination)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    shutil.rmtree(source)


def _remove_empty_parents(path, stop_at):
    """Retire les dossiers parents devenus vides (dossier d'univers), sans jamais
    toucher à la racine de la bibliothèque."""
    stop_real = os.path.realpath(stop_at)
    current = os.path.dirname(os.path.realpath(path))
    while current.startswith(stop_real + os.sep) and current != stop_real:
        try:
            os.rmdir(current)
        except OSError:
            return
        current = os.path.dirname(current)


def _rewrite_series_paths(conn, series_id, library_id, old_path, new_path):
    conn.execute('UPDATE series SET library_id = ?, path = ?, komga_series_id = NULL, komga_url = NULL '
                 'WHERE id = ?', (library_id, new_path, series_id))
    for volume in conn.execute('SELECT id, filepath FROM volumes WHERE series_id = ?', (series_id,)).fetchall():
        conn.execute('UPDATE volumes SET filepath = ?, komga_book_id = NULL WHERE id = ?',
                     (_replace_prefix(volume['filepath'], old_path, new_path), volume['id']))
    has_history = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'import_history_files'").fetchone()
    if has_history:
        for entry in conn.execute('SELECT rowid, destination_path FROM import_history_files '
                                  'WHERE destination_path = ? OR destination_path LIKE ?',
                                  (old_path, old_path.rstrip('/') + '/%')).fetchall():
            conn.execute('UPDATE import_history_files SET destination_path = ? WHERE rowid = ?',
                         (_replace_prefix(entry['destination_path'], old_path, new_path), entry['rowid']))
    conn.commit()


def move_one_series(conn, series_id, target):
    """Déplace une série vers la bibliothèque `target` (Row id/path). Renvoie un dict
    {success, title, error?, new_path?}."""
    row = conn.execute(
        'SELECT s.id, s.title, s.path, s.library_id, l.path AS library_path '
        'FROM series s JOIN libraries l ON l.id = s.library_id WHERE s.id = ?', (series_id,)).fetchone()
    if row is None:
        return {'success': False, 'title': f'#{series_id}', 'error': 'Série introuvable'}
    title = row['title']
    if row['library_id'] == target['id']:
        return {'success': True, 'title': title, 'skipped': True}
    if not row['path']:
        return {'success': False, 'title': title, 'error': 'Série sans dossier'}

    source_root = row['library_path']
    try:
        source_path = resolve_within(row['path'], source_root)
    except UnsafePathError:
        return {'success': False, 'title': title, 'error': f"Dossier hors de sa bibliothèque ({row['path']})"}
    relative = os.path.relpath(source_path, os.path.realpath(source_root))
    if relative in ('.', '') or relative.startswith('..'):
        return {'success': False, 'title': title, 'error': 'La série est à la racine de la bibliothèque'}
    others = [r[0] for r in conn.execute('SELECT path FROM series WHERE id != ? AND path IS NOT NULL', (series_id,))]
    conflict = series_folder_conflict(row['path'], source_root, others)
    if conflict:
        return {'success': False, 'title': title, 'error': f'Dossier non déplacé : {conflict}'}

    if not os.path.isdir(target['path']):
        return {'success': False, 'title': title, 'error': f"Dossier de destination inaccessible ({target['path']})"}
    try:
        destination = resolve_within(os.path.join(target['path'], relative), target['path'])
    except UnsafePathError as exc:
        return {'success': False, 'title': title, 'error': str(exc)}
    if os.path.exists(destination):
        return {'success': False, 'title': title, 'error': f'Un dossier existe déjà : {destination}'}
    # Chemin enregistré : celui de la bibliothèque cible + chemin relatif, sans résoudre
    # les liens (comme le scan l'écrit).
    new_path = os.path.join(target['path'], relative)

    moved_on_disk = os.path.isdir(source_path)
    if moved_on_disk:
        try:
            _move_tree(source_path, destination)
        except OSError as exc:
            return {'success': False, 'title': title, 'error': f'Déplacement impossible : {exc}'}
    # Sans dossier sur le disque (série suivie sans aucun fichier) : seule la base change.

    try:
        _rewrite_series_paths(conn, series_id, target['id'], row['path'], new_path)
    except Exception as exc:
        conn.rollback()
        if moved_on_disk:
            # La base n'a pas suivi : le dossier revient à sa place plutôt que de laisser
            # la série pointer vers un chemin qui n'existe plus.
            try:
                _move_tree(destination, source_path)
            except OSError:
                logger.exception('Série %s : dossier resté dans %s', series_id, destination)
                return {'success': False, 'title': title,
                        'error': f'Base non mise à jour ({exc}) et dossier resté dans {destination}'}
        return {'success': False, 'title': title, 'error': f'Base non mise à jour : {exc}'}
    if moved_on_disk:
        _remove_empty_parents(source_path, source_root)
    return {'success': True, 'title': title, 'new_path': new_path}


def _relink_komga(app, series_ids):
    from blueprints.komga.client import KomgaClient, KomgaError, trigger_scan_async

    def after_scan():
        from .routes import _try_komga_title_match
        try:
            client = KomgaClient()
        except KomgaError:
            return
        conn = _connect(app.config['DATABASE'])
        try:
            rows = conn.execute(f"SELECT id, title FROM series WHERE id IN ({','.join('?' * len(series_ids))})",
                                tuple(series_ids)).fetchall()
        finally:
            conn.close()
        for row in rows:
            try:
                _try_komga_title_match(row['id'], row['title'], client)
            except Exception:
                continue

    with app.app_context():
        trigger_scan_async(after_scan=after_scan)


def _run(app, job_id, series_ids, target_id):
    job = _jobs[job_id]
    moved = []
    try:
        if not scan_import_lock.acquire(timeout=120):
            job.update(status='failed', error='Un import ou un scan est en cours, réessayez plus tard')
            return
        try:
            conn = _connect(app.config['DATABASE'])
            try:
                target = conn.execute('SELECT id, name, path FROM libraries WHERE id = ?', (target_id,)).fetchone()
                for index, series_id in enumerate(series_ids, 1):
                    job['current'] = index
                    result = move_one_series(conn, series_id, target)
                    job['results'].append(result)
                    if result['success'] and not result.get('skipped'):
                        moved.append(series_id)
            finally:
                conn.close()
        finally:
            scan_import_lock.release()
        job['status'] = 'succeeded'
    except Exception as exc:
        logger.exception('Déplacement de séries %s', job_id)
        job.update(status='failed', error=str(exc))
    finally:
        if moved:
            try:
                _relink_komga(app, moved)
            except Exception:
                pass


@library_bp.route('/api/series/move', methods=['POST'])
def start_series_move():
    data = request.get_json(silent=True) or {}
    try:
        target_id = int(data.get('library_id'))
        series_ids = [int(value) for value in data.get('series_ids') or []]
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Paramètres invalides'}), 400
    if not series_ids:
        return jsonify({'success': False, 'error': 'Aucune série sélectionnée'}), 400
    conn = _connect(current_app.config['DATABASE'])
    try:
        target = conn.execute('SELECT id FROM libraries WHERE id = ?', (target_id,)).fetchone()
    finally:
        conn.close()
    if target is None:
        return jsonify({'success': False, 'error': 'Bibliothèque introuvable'}), 404
    with _jobs_lock:
        if any(job['status'] == 'running' for job in _jobs.values()):
            return jsonify({'success': False, 'error': 'Un déplacement est déjà en cours'}), 409
        job_id = uuid.uuid4().hex
        _jobs[job_id] = {'status': 'running', 'total': len(series_ids), 'current': 0, 'results': [], 'error': None}
    app = current_app._get_current_object()
    threading.Thread(target=_run, args=(app, job_id, series_ids, target_id), daemon=True).start()
    return jsonify({'success': True, 'job_id': job_id}), 202


@library_bp.route('/api/series/move/<job_id>', methods=['GET'])
def series_move_status(job_id):
    job = _jobs.get(job_id)
    if job is None:
        return jsonify({'success': False, 'error': 'Déplacement introuvable'}), 404
    return jsonify({'success': True, 'job': job})
