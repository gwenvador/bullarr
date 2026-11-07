"""Demandes de BD (page /demandes des lecteurs, section « Demandes » de /validation).

Un lecteur cherche une série sur Bédéthèque et la demande. Par défaut la demande attend
la décision d'un administrateur dans la page Validation (notification « Demande de BD ») ;
avec « Valider automatiquement les demandes » (Configuration → Login → Utilisateurs et
rôles), elle est ajoutée tout de suite. Une série validée entre dans la bibliothèque
choisie au moment de la demande (la bibliothèque par défaut si le lecteur y a accès,
sinon la première qui lui est permise) et la recherche automatique des tomes est lancée,
comme un ajout fait par un administrateur.

Statuts : pending (à valider), added, already (déjà dans la bibliothèque), error,
rejected (refusée, motif dans error), cancelled (annulée par le lecteur)."""
import json
import sqlite3

from flask import current_app, jsonify, redirect, render_template, request, session

from blueprints.auth.activity import log_activity
from blueprints.auth.roles import current_user_key, is_reader
from . import library_bp

STATUS_PENDING = 'pending'
MAX_PENDING_PER_USER = 30
BEDETHEQUE_PREFIX = 'https://www.bedetheque.com/'


def _connect():
    conn = sqlite3.connect(current_app.config['DATABASE'], timeout=60.0)
    conn.row_factory = sqlite3.Row
    return conn


def _owned(total, missing_json):
    if not total:
        return 0
    try:
        missing = json.loads(missing_json or '[]')
    except (TypeError, ValueError):
        missing = []
    return max(total - len(missing), 0)


def _target_library(conn):
    """Bibliothèque par défaut si le compte y a accès, sinon la première qui lui est permise."""
    from blueprints.auth.roles import library_visible
    for row in conn.execute('SELECT id, name FROM libraries ORDER BY COALESCE(is_default, 0) DESC, name'):
        if library_visible(row['id']):
            return row
    return None


def auto_approve_enabled():
    from blueprints.auth.config_store import load_oidc_config
    return bool(load_oidc_config().get('auto_approve_requests'))


def pending_requests_count():
    conn = _connect()
    try:
        return conn.execute('SELECT COUNT(*) FROM book_requests WHERE status = ?', (STATUS_PENDING,)).fetchone()[0]
    finally:
        conn.close()


def _add_series(url, library_id):
    """Ajoute la série comme le bouton « Ajouter » de Bédéthèque. -> (status, series_id, title, error)"""
    from blueprints.bedetheque.routes import add_series_from_bedetheque
    payload = {'url': url, 'library_id': library_id, 'force_auto_acquire': True}
    with current_app.test_request_context(json=payload):
        response = add_series_from_bedetheque()
    if isinstance(response, tuple):
        response = response[0]
    result = response.get_json() or {}
    if result.get('success'):
        return ('already' if result.get('already_exists') else 'added'), result.get('series_id'), result.get('title'), None
    return 'error', None, None, result.get('error') or 'Erreur inconnue'


def _cover_url(row):
    path = row['local_cover_path'] or row['bedetheque_cover_path'] or row['komga_cover_path']
    return f'/{path}' if path else None


_COVER_COLUMNS = 's.local_cover_path, s.bedetheque_cover_path, s.komga_cover_path'


def _session_user():
    user = session.get('user') or {}
    return current_user_key(), user.get('name') or user.get('email')


def _notify_pending(user_name, title, url):
    try:
        from notifications import send_notification
        send_notification(f"{user_name or 'Un lecteur'} demande « {title or url} ».\n"
                          "À valider dans la page Validation.",
                          kind='book_request', title='Demande de BD', icon='📮', tags=['books'])
    except Exception as exc:  # pragma: no cover - notification non bloquante
        print(f"Notification demande de BD : {exc}")


@library_bp.route('/demandes')
def requests_page():
    # Les administrateurs traitent les demandes dans Validation (pas de page à part).
    if not is_reader():
        return redirect('/validation#demandes')
    return render_template('requests.html')


def _request_dict(row):
    return {
        'id': row['id'], 'user_name': row['user_name'], 'url': row['bedetheque_url'],
        'title': row['series_title'] or row['title'],
        'series_id': row['series_id'] if row['series_title'] else None,
        'status': row['status'], 'error': row['error'], 'created_at': row['created_at'],
        'decided_at': row['decided_at'], 'decided_by': row['decided_by'],
        'library_id': row['library_id'], 'library_name': row['library_name'],
        # Même compte que la fiche série : tomes attendus moins tomes manquants.
        'owned': _owned(row['total'], row['missing_json']), 'total': row['total'],
        'cover_url': _cover_url(row) if row['series_title'] else None,
    }


_LIST_SQL = ('SELECT r.*, s.title AS series_title, s.total_volumes AS total, s.missing_volumes AS missing_json, '
             f'{_COVER_COLUMNS}, '
             'l.name AS library_name '
             'FROM book_requests r LEFT JOIN series s ON s.id = r.series_id '
             'LEFT JOIN libraries l ON l.id = r.library_id ')


@library_bp.route('/api/requests', methods=['GET'])
def list_requests():
    status = (request.args.get('status') or '').strip()
    clauses, params = [], []
    if is_reader():
        clauses.append('r.user_key = ?')
        params.append(current_user_key())
    if status:
        clauses.append('r.status = ?')
        params.append(status)
    where = ('WHERE ' + ' AND '.join(clauses) + ' ') if clauses else ''
    conn = _connect()
    try:
        rows = conn.execute(_LIST_SQL + where + 'ORDER BY r.created_at DESC, r.id DESC LIMIT 300', params).fetchall()
    finally:
        conn.close()
    return jsonify({'success': True, 'mine_only': is_reader(), 'auto_approve': auto_approve_enabled(),
                    'requests': [_request_dict(row) for row in rows]})


@library_bp.route('/api/requests/lookup', methods=['POST'])
def lookup_requests():
    """État de résultats Bédéthèque pour ce compte : déjà dans la bibliothèque, déjà demandé."""
    data = request.get_json(silent=True) or {}
    urls = [u for u in (data.get('urls') or []) if isinstance(u, str) and u.startswith(BEDETHEQUE_PREFIX)][:60]
    if not urls:
        return jsonify({'success': True, 'items': {}})
    marks = ','.join('?' * len(urls))
    items = {}
    conn = _connect()
    try:
        for row in conn.execute(f'SELECT s.id, s.bedetheque_url, s.total_volumes, s.missing_volumes, {_COVER_COLUMNS} '
                                f'FROM series s WHERE s.bedetheque_url IN ({marks})', urls):
            items[row['bedetheque_url']] = {'state': 'library', 'series_id': row['id'], 'cover_url': _cover_url(row),
                                            'owned': _owned(row['total_volumes'], row['missing_volumes']),
                                            'total': row['total_volumes']}
        for row in conn.execute(f'SELECT bedetheque_url, status FROM book_requests WHERE user_key = ? '
                                f"AND status IN ('pending', 'rejected') AND bedetheque_url IN ({marks}) "
                                f'ORDER BY id', (current_user_key(), *urls)):
            if items.get(row['bedetheque_url'], {}).get('state') != 'library':
                items[row['bedetheque_url']] = {'state': row['status']}
    finally:
        conn.close()
    return jsonify({'success': True, 'items': items})


@library_bp.route('/api/requests', methods=['POST'])
def create_request():
    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    title = (data.get('title') or '').strip()[:300] or None
    if not url.startswith(BEDETHEQUE_PREFIX):
        return jsonify({'success': False, 'error': 'Choisissez une série Bédéthèque'}), 400
    key, name = _session_user()

    conn = _connect()
    try:
        existing = conn.execute('SELECT id, title FROM series WHERE bedetheque_url = ? LIMIT 1', (url,)).fetchone()
        library = _target_library(conn)
        duplicate = conn.execute('SELECT id FROM book_requests WHERE bedetheque_url = ? AND user_key IS ? AND status = ?',
                                 (url, key, STATUS_PENDING)).fetchone()
        pending_mine = conn.execute('SELECT COUNT(*) FROM book_requests WHERE user_key IS ? AND status = ?',
                                    (key, STATUS_PENDING)).fetchone()[0]
    finally:
        conn.close()
    if library is None:
        return jsonify({'success': False, 'error': 'Aucune bibliothèque'}), 400
    if duplicate and not existing:
        return jsonify({'success': True, 'status': STATUS_PENDING, 'id': duplicate['id'], 'title': title,
                        'duplicate': True})

    immediate = (not is_reader()) or auto_approve_enabled()
    if existing:
        status, series_id, error, title = 'already', existing['id'], None, existing['title']
    elif immediate:
        status, series_id, added_title, error = _add_series(url, library['id'])
        title = added_title or title
    else:
        if pending_mine >= MAX_PENDING_PER_USER:
            return jsonify({'success': False, 'error': 'Trop de demandes en attente : patientez qu’elles soient traitées'}), 429
        status, series_id, error = STATUS_PENDING, None, None

    conn = _connect()
    try:
        cursor = conn.execute(
            'INSERT INTO book_requests (user_key, user_name, bedetheque_url, title, series_id, status, error, library_id, '
            'decided_at, decided_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '
            "CASE WHEN ? = 'pending' THEN NULL ELSE CURRENT_TIMESTAMP END, ?)",
            (key, name, url, title, series_id, status, error, library['id'], status,
             None if status == STATUS_PENDING else ('auto' if is_reader() else name)))
        request_id = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()

    detail = {'pending': 'En attente de validation', 'added': f"Ajoutée dans {library['name']}",
              'already': 'Déjà dans la bibliothèque', 'error': error}.get(status)
    if status == 'added' and is_reader():
        detail += ' (validation automatique)'
    log_activity('request_created', target_name=title or url, series_id=series_id, detail=detail,
                 success=status != 'error')
    if status == STATUS_PENDING:
        _notify_pending(name, title, url)
    if status == 'error':
        return jsonify({'success': False, 'error': error}), 502
    return jsonify({'success': True, 'status': status, 'id': request_id, 'series_id': series_id, 'title': title,
                    'library': library['name']})


def _pending_row(conn, request_id):
    return conn.execute('SELECT * FROM book_requests WHERE id = ?', (request_id,)).fetchone()


@library_bp.route('/api/requests/<int:request_id>/approve', methods=['POST'])
def approve_request(request_id):
    """Valide une demande : ajoute la série (bibliothèque choisie, sinon celle de la demande)."""
    data = request.get_json(silent=True) or {}
    conn = _connect()
    try:
        row = _pending_row(conn, request_id)
        library_id = data.get('library_id') or (row['library_id'] if row else None)
        library = conn.execute('SELECT id, name FROM libraries WHERE id = ?', (library_id,)).fetchone() if library_id else None
        if library is None:
            library = conn.execute('SELECT id, name FROM libraries ORDER BY COALESCE(is_default, 0) DESC, name LIMIT 1').fetchone()
    finally:
        conn.close()
    if row is None:
        return jsonify({'success': False, 'error': 'Demande introuvable'}), 404
    if row['status'] != STATUS_PENDING:
        return jsonify({'success': False, 'error': 'Cette demande a déjà été traitée'}), 409
    if library is None:
        return jsonify({'success': False, 'error': 'Aucune bibliothèque'}), 400

    status, series_id, title, error = _add_series(row['bedetheque_url'], library['id'])
    _key, name = _session_user()
    label = title or row['title'] or row['bedetheque_url']
    if status == 'error':
        # La demande reste en attente : l'administrateur peut réessayer ou la refuser.
        log_activity('request_approved', target_key=row['user_key'], target_name=label, success=False, detail=error)
        return jsonify({'success': False, 'error': error}), 502
    conn = _connect()
    try:
        # Toutes les demandes en attente de la même série sont satisfaites ensemble.
        conn.execute('UPDATE book_requests SET status = ?, series_id = ?, error = NULL, title = COALESCE(?, title), '
                     'library_id = ?, decided_at = CURRENT_TIMESTAMP, decided_by = ? '
                     'WHERE bedetheque_url = ? AND status = ?',
                     (status, series_id, title, library['id'], name, row['bedetheque_url'], STATUS_PENDING))
        conn.commit()
    finally:
        conn.close()
    log_activity('request_approved', target_key=row['user_key'], target_name=label, series_id=series_id,
                 detail=f"Demandée par {row['user_name'] or '?'} · {library['name']}")
    return jsonify({'success': True, 'status': status, 'series_id': series_id, 'title': title, 'library': library['name']})


@library_bp.route('/api/requests/<int:request_id>/reject', methods=['POST'])
def reject_request(request_id):
    data = request.get_json(silent=True) or {}
    reason = (data.get('reason') or '').strip()[:300] or None
    _key, name = _session_user()
    conn = _connect()
    try:
        row = _pending_row(conn, request_id)
        if row is None:
            return jsonify({'success': False, 'error': 'Demande introuvable'}), 404
        if row['status'] != STATUS_PENDING:
            return jsonify({'success': False, 'error': 'Cette demande a déjà été traitée'}), 409
        conn.execute('UPDATE book_requests SET status = ?, error = ?, decided_at = CURRENT_TIMESTAMP, decided_by = ? '
                     'WHERE id = ?', ('rejected', reason, name, request_id))
        conn.commit()
    finally:
        conn.close()
    log_activity('request_rejected', target_key=row['user_key'], target_name=row['title'] or row['bedetheque_url'],
                 detail=f"Demandée par {row['user_name'] or '?'}" + (f' · {reason}' if reason else ''))
    return jsonify({'success': True})


@library_bp.route('/api/requests/<int:request_id>', methods=['DELETE'])
def cancel_request(request_id):
    """Un lecteur annule sa propre demande encore en attente (un admin : n'importe laquelle)."""
    conn = _connect()
    try:
        row = _pending_row(conn, request_id)
        if row is None or (is_reader() and row['user_key'] != current_user_key()):
            return jsonify({'success': False, 'error': 'Demande introuvable'}), 404
        if row['status'] != STATUS_PENDING:
            return jsonify({'success': False, 'error': 'Cette demande a déjà été traitée'}), 409
        conn.execute("UPDATE book_requests SET status = 'cancelled', decided_at = CURRENT_TIMESTAMP WHERE id = ?",
                     (request_id,))
        conn.commit()
    finally:
        conn.close()
    log_activity('request_cancelled', target_name=row['title'] or row['bedetheque_url'])
    return jsonify({'success': True})


@library_bp.route('/api/requests/pending-count', methods=['GET'])
def requests_pending_count():
    """Pour la cloche de l'en-tête : demandes de BD et demandes de compte à valider."""
    from blueprints.auth import accounts
    books = pending_requests_count()
    try:
        account_count = accounts.pending_count()
    except Exception:
        account_count = 0
    return jsonify({'success': True, 'books': books, 'accounts': account_count, 'total': books + account_count})


@library_bp.route('/api/requests/settings', methods=['PUT'])
def set_requests_settings():
    """Valider automatiquement les demandes des lecteurs (sinon : page Validation)."""
    from blueprints.auth.config_store import load_oidc_config, save_oidc_config
    data = request.get_json(silent=True) or {}
    config = load_oidc_config()
    config['auto_approve_requests'] = bool(data.get('auto_approve'))
    if not save_oidc_config(config):
        return jsonify({'success': False, 'error': 'Erreur de sauvegarde'}), 500
    log_activity('settings_changed', detail='Validation automatique des demandes : '
                 + ('activée' if config['auto_approve_requests'] else 'désactivée'))
    return jsonify({'success': True})
