"""Demandes de BD (page /demandes).

Un lecteur cherche une série sur Bédéthèque et la demande : elle est ajoutée tout de
suite dans la bibliothèque par défaut (sinon la première), et la recherche automatique
des tomes est lancée, comme un ajout fait par un administrateur. Chaque demande est
gardée (qui, quand, quelle série) : un lecteur voit les siennes, un administrateur
toutes."""
import sqlite3

import json

from flask import current_app, jsonify, render_template, request, session

from blueprints.auth.roles import current_user_key, is_reader
from . import library_bp


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


@library_bp.route('/demandes')
def requests_page():
    return render_template('requests.html')


@library_bp.route('/api/requests', methods=['GET'])
def list_requests():
    conn = _connect()
    try:
        sql = ('SELECT r.id, r.user_name, r.bedetheque_url, r.title, r.series_id, r.status, r.error, r.created_at, '
               's.title AS series_title, '
               's.total_volumes AS total, s.missing_volumes AS missing_json '
               'FROM book_requests r LEFT JOIN series s ON s.id = r.series_id ')
        params = ()
        if is_reader():
            sql += 'WHERE r.user_key = ? '
            params = (current_user_key(),)
        rows = conn.execute(sql + 'ORDER BY r.created_at DESC, r.id DESC LIMIT 200', params).fetchall()
    finally:
        conn.close()
    return jsonify({'success': True, 'mine_only': is_reader(), 'requests': [{
        'id': row['id'], 'user_name': row['user_name'], 'url': row['bedetheque_url'],
        'title': row['series_title'] or row['title'], 'series_id': row['series_id'] if row['series_title'] else None,
        'status': row['status'], 'error': row['error'], 'created_at': row['created_at'],
        # Même compte que la fiche série : tomes attendus moins tomes manquants.
        'owned': _owned(row['total'], row['missing_json']), 'total': row['total'],
    } for row in rows]})


@library_bp.route('/api/requests', methods=['POST'])
def create_request():
    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    title = (data.get('title') or '').strip()[:300] or None
    if not url.startswith('https://www.bedetheque.com/'):
        return jsonify({'success': False, 'error': 'Choisissez une série Bédéthèque'}), 400

    conn = _connect()
    try:
        existing = conn.execute('SELECT id, title FROM series WHERE bedetheque_url = ? LIMIT 1', (url,)).fetchone()
        library = _target_library(conn)
    finally:
        conn.close()
    if library is None:
        return jsonify({'success': False, 'error': 'Aucune bibliothèque'}), 400

    if existing:
        status, series_id, error = 'already', existing['id'], None
        title = existing['title']
    else:
        from blueprints.bedetheque.routes import add_series_from_bedetheque
        payload = {'url': url, 'library_id': library['id'], 'force_auto_acquire': True}
        with current_app.test_request_context(json=payload):
            response = add_series_from_bedetheque()
        if isinstance(response, tuple):
            response = response[0]
        result = response.get_json() or {}
        if result.get('success'):
            status = 'already' if result.get('already_exists') else 'added'
            series_id, error = result.get('series_id'), None
            title = result.get('title') or title
        else:
            status, series_id, error = 'error', None, result.get('error') or 'Erreur inconnue'

    user = session.get('user') or {}
    conn = _connect()
    try:
        conn.execute('INSERT INTO book_requests (user_key, user_name, bedetheque_url, title, series_id, status, error) '
                     'VALUES (?, ?, ?, ?, ?, ?, ?)',
                     (current_user_key(), user.get('name') or user.get('email'), url, title, series_id, status, error))
        conn.commit()
    finally:
        conn.close()
    if status == 'error':
        return jsonify({'success': False, 'error': error}), 502
    return jsonify({'success': True, 'status': status, 'series_id': series_id, 'title': title,
                    'library': library['name']})
