"""
Comptes locaux (identifiant + mot de passe) gérés dans Paramètres > Login >
« Utilisateurs et rôles », et révocation des sessions d'un compte supprimé ou dont le
mot de passe a changé.

Un compte local est une ligne de la table users avec auth_method = 'password' et un
password_hash ; sa clé est « password:<identifiant> » (voir roles.user_key). Son rôle est
role_override (admin / reader), administrateur par défaut. Les comptes OIDC (Pocket ID)
apparaissent dans la même table à leur première connexion.
"""
import re
import time

from flask import current_app
from werkzeug.security import check_password_hash, generate_password_hash

from .roles import ROLE_ADMIN, ROLES, _connect

USERNAME_RE = re.compile(r'^[A-Za-z0-9._@-]{2,64}$')
MIN_PASSWORD_LENGTH = 8
STATUS_PENDING = 'pending'
MAX_PENDING_REQUESTS = 50


class AccountError(ValueError):
    """Erreur affichable telle quelle (400)."""


def local_key(username):
    return f'password:{username}'


def _has_database():
    return bool(current_app.config.get('DATABASE'))


def migrate_legacy_account(config):
    """L'ancien compte unique (identifiant/mot de passe dans oidc_config.json) devient un
    compte administrateur de la table users. Renvoie True si la config a changé."""
    username = (config.get('username') or '').strip()
    password_hash = config.get('password_hash') or ''
    if not username or not password_hash or not _has_database():
        return False
    conn = _connect()
    try:
        conn.execute('''
            INSERT INTO users (user_key, name, auth_method, password_hash, role_override)
            VALUES (?, ?, 'password', ?, 'admin')
            ON CONFLICT(user_key) DO UPDATE SET
                auth_method = 'password',
                password_hash = COALESCE(users.password_hash, excluded.password_hash)''',
            (local_key(username), username, password_hash))
        conn.commit()
    finally:
        conn.close()
    config.pop('username', None)
    config.pop('password_hash', None)
    return True


def authenticate(username, password):
    """Ligne du compte local si l'identifiant et le mot de passe sont bons, sinon None."""
    if not username or not password or not _has_database():
        return None
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE user_key = ? AND auth_method = 'password'",
                           (local_key(username),)).fetchone()
    finally:
        conn.close()
    if not row or not row['password_hash'] or not check_password_hash(row['password_hash'], password):
        return None
    return row


def _check_password(password):
    if len(password or '') < MIN_PASSWORD_LENGTH:
        raise AccountError(f'Le mot de passe doit faire au moins {MIN_PASSWORD_LENGTH} caractères')


def local_admin_count(exclude_key=None):
    """Comptes locaux administrateurs capables de se connecter."""
    conn = _connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM users WHERE auth_method = 'password' AND password_hash IS NOT NULL "
            "AND COALESCE(role_override, 'admin') = 'admin' AND status IS NULL AND user_key != ?",
            (exclude_key or '',)).fetchone()[0]
    finally:
        conn.close()


def create_local_account(username, password, name=None, email=None, role=ROLE_ADMIN, status=None):
    username = (username or '').strip()
    if not USERNAME_RE.match(username):
        raise AccountError('Identifiant invalide : 2 à 64 caractères, lettres, chiffres, . _ @ -')
    _check_password(password)
    if role not in ROLES:
        raise AccountError('Rôle invalide')
    conn = _connect()
    try:
        if conn.execute('SELECT 1 FROM users WHERE user_key = ?', (local_key(username),)).fetchone():
            raise AccountError('Ce compte existe déjà')
        conn.execute('''
            INSERT INTO users (user_key, name, email, auth_method, password_hash, role_override, status)
            VALUES (?, ?, ?, 'password', ?, ?, ?)''',
            (local_key(username), (name or '').strip() or username, (email or '').strip() or None,
             generate_password_hash(password), role, status))
        conn.commit()
    finally:
        conn.close()
    return local_key(username)


def pending_count():
    conn = _connect()
    try:
        return conn.execute('SELECT COUNT(*) FROM users WHERE status = ?', (STATUS_PENDING,)).fetchone()[0]
    finally:
        conn.close()


def request_account(username, password, name=None, email=None):
    """Demande d'inscription depuis la page de connexion : compte lecteur en attente,
    qui ne peut pas se connecter tant qu'un administrateur ne l'a pas validé."""
    if pending_count() >= MAX_PENDING_REQUESTS:
        raise AccountError('Trop de demandes en attente : réessayez plus tard')
    return create_local_account(username, password, name=name, email=email, role='reader', status=STATUS_PENDING)


def approve_account(key, role='reader'):
    if role not in ROLES:
        raise AccountError('Rôle invalide')
    conn = _connect()
    try:
        changed = conn.execute('UPDATE users SET status = NULL, role_override = ? WHERE user_key = ? AND status = ?',
                               (role, key, STATUS_PENDING)).rowcount
        conn.commit()
    finally:
        conn.close()
    return bool(changed)


def get_user(key):
    conn = _connect()
    try:
        return conn.execute('SELECT * FROM users WHERE user_key = ?', (key,)).fetchone()
    finally:
        conn.close()


def update_account(key, name=None, email=None, password=None):
    """Nom, e-mail et mot de passe d'un compte local (ceux d'un compte OIDC viennent du
    fournisseur à chaque connexion)."""
    row = get_user(key)
    if not row:
        raise LookupError(key)
    if row['auth_method'] != 'password':
        raise AccountError('Le nom et l’e-mail d’un compte OIDC viennent du fournisseur (Pocket ID)')
    fields = {}
    if name is not None:
        fields['name'] = name.strip() or key.split(':', 1)[1]
    if email is not None:
        fields['email'] = email.strip() or None
    if password:
        _check_password(password)
        fields['password_hash'] = generate_password_hash(password)
    if not fields:
        return False
    conn = _connect()
    try:
        assignments = ', '.join(f'{column} = ?' for column in fields)
        conn.execute(f'UPDATE users SET {assignments} WHERE user_key = ?', (*fields.values(), key))
        conn.commit()
    finally:
        conn.close()
    if password:
        revoke_sessions(key)
    return True


def delete_account(key):
    """Supprime le compte, ses listes perso, et coupe ses sessions ouvertes. Un compte OIDC
    réapparaîtra à sa prochaine connexion (pour le bloquer, le retirer dans Pocket ID)."""
    conn = _connect()
    try:
        deleted = conn.execute('DELETE FROM users WHERE user_key = ?', (key,)).rowcount
        conn.execute('DELETE FROM user_reading_lists WHERE user_key = ?', (key,))
        conn.commit()
    finally:
        conn.close()
    if deleted:
        revoke_sessions(key)
    return bool(deleted)


def revoke_sessions(key):
    """Les sessions ouvertes avant maintenant ne sont plus acceptées (voir session_revoked)."""
    conn = _connect()
    try:
        conn.execute('INSERT OR REPLACE INTO revoked_users (user_key, revoked_at) VALUES (?, ?)', (key, time.time()))
        conn.commit()
    finally:
        conn.close()


def session_revoked(key, login_at):
    if not key or not _has_database():
        return False
    conn = _connect()
    try:
        row = conn.execute('SELECT revoked_at FROM revoked_users WHERE user_key = ?', (key,)).fetchone()
    except Exception:
        return False
    finally:
        conn.close()
    return bool(row) and float(login_at or 0) < row['revoked_at']
