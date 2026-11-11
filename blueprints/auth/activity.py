"""Journal des actions des comptes (page Historique, onglets Utilisateurs et Demandes).

Connexions (réussies ou non), déconnexions, demandes et créations de compte, modifications,
suppressions, changements de rôle ou d'accès, demandes de BD et leur traitement. Seuls les
administrateurs le lisent (GET /api/auth/activity). Jamais de mot de passe dans le détail.

log_activity ne lève jamais d'exception : un journal raté ne fait pas échouer l'action."""
import sqlite3

from flask import current_app, has_request_context, session

MAX_ROWS = 5000


def init_table(cursor):
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            actor_key TEXT,
            actor_name TEXT,
            action TEXT NOT NULL,
            target_key TEXT,
            target_name TEXT,
            series_id INTEGER,
            detail TEXT,
            success INTEGER DEFAULT 1
        )''')


def _connect():
    conn = sqlite3.connect(current_app.config['DATABASE'], timeout=60.0)
    conn.row_factory = sqlite3.Row
    return conn


def _session_actor():
    if not has_request_context():
        return None, None
    from .roles import request_user, user_key
    user = request_user() or None
    if not user:
        return None, None
    return user_key(user), user.get('name') or user.get('email')


def log_activity(action, *, target_key=None, target_name=None, series_id=None, detail=None,
                 success=True, actor_key=None, actor_name=None):
    try:
        if actor_key is None and actor_name is None:
            actor_key, actor_name = _session_actor()
        conn = _connect()
        try:
            conn.execute(
                'INSERT INTO user_activity (actor_key, actor_name, action, target_key, target_name, '
                'series_id, detail, success) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (actor_key, (actor_name or '')[:200] or None, action, target_key,
                 (target_name or '')[:300] or None, series_id, (detail or '')[:500] or None,
                 1 if success else 0))
            conn.execute('DELETE FROM user_activity WHERE id <= (SELECT MAX(id) FROM user_activity) - ?',
                         (MAX_ROWS,))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - journal non bloquant
        print(f"Journal des comptes : {exc}")


def list_activity(limit=500):
    conn = _connect()
    try:
        try:
            rows = conn.execute('SELECT a.*, s.title AS series_title FROM user_activity a '
                                'LEFT JOIN series s ON s.id = a.series_id '
                                'ORDER BY a.id DESC LIMIT ?', (limit,)).fetchall()
        except sqlite3.OperationalError:  # base sans bibliothèque (tests des comptes)
            rows = conn.execute('SELECT a.*, NULL AS series_title FROM user_activity a '
                                'ORDER BY a.id DESC LIMIT ?', (limit,)).fetchall()
    finally:
        conn.close()
    return [{
        'id': row['id'], 'created_at': row['created_at'], 'action': row['action'],
        'actor_key': row['actor_key'], 'actor_name': row['actor_name'],
        'target_key': row['target_key'], 'target_name': row['target_name'],
        'series_id': row['series_id'] if row['series_title'] else None,
        'series_title': row['series_title'], 'detail': row['detail'], 'success': bool(row['success']),
    } for row in rows]
