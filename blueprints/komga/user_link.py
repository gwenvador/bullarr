"""Compte Komga personnel d'un utilisateur de Bullarr (Réglages → Intégrations de l'app).

La connexion Komga du serveur (config_store) est celle de l'administrateur : son avancement
de lecture est celui de son compte Komga. Un lecteur peut relier ici SON compte Komga, sur le
même serveur Komga : Bullarr se connecte une fois avec son identifiant et son mot de passe,
crée une clé API à son nom dans Komga (« Bullarr ») et ne garde que cette clé, chiffrée.
Le mot de passe n'est jamais enregistré. Ensuite :
- son avancement de lecture est lu dans Komga avec sa clé (user_komga_read_progress) ;
- les pages lues dans le lecteur de Bullarr sont envoyées à son compte Komga.

Sans compte Komga relié, un lecteur (ou tout compte quand Komga n'est pas configuré) a un
avancement LOCAL, propre à lui, enregistré par Bullarr : pages lues dans le lecteur de Bullarr,
« Continuer la lecture », barres de progression (user_local_read_progress). La clé est
(user_key, series_id, filename) : les lignes `volumes` sont recréées à chaque rescan, leur id
n'est pas stable.

progress_source() rend la table d'avancement à joindre, toujours avec la même forme : une
colonne vol_id (= volumes.id) puis page, pages_count, completed, read_at, synced_at."""
import sqlite3
import time
from collections import namedtuple

import requests
from flask import current_app

from encryption import decrypt, encrypt
from .client import KomgaClient, KomgaError
from .config_store import load_komga_config, normalize_komga_url

API_KEY_COMMENT = 'Bullarr'


def _connect():
    conn = sqlite3.connect(current_app.config['DATABASE'], timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute('''
        CREATE TABLE IF NOT EXISTS user_komga_links (
            user_key TEXT PRIMARY KEY,
            api_key TEXT NOT NULL,
            komga_user TEXT,
            linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_sync_at REAL,
            last_error TEXT,
            stats_json TEXT
        )''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS user_komga_read_progress (
            user_key TEXT NOT NULL,
            komga_book_id TEXT NOT NULL,
            page INTEGER,
            pages_count INTEGER,
            completed INTEGER DEFAULT 0,
            read_at TEXT,
            synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_key, komga_book_id)
        )''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS user_local_read_progress (
            user_key TEXT NOT NULL,
            series_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            page INTEGER,
            pages_count INTEGER,
            completed INTEGER DEFAULT 0,
            read_at TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_key, series_id, filename)
        )''')
    return conn


def _server_url():
    config = load_komga_config()
    if not config.get('enabled') or not (config.get('url') or '').strip():
        raise KomgaError("Komga n'est pas configuré sur ce serveur")
    return normalize_komga_url(config['url'])


class UserKomgaClient(KomgaClient):
    """KomgaClient avec la clé API d'un utilisateur, sur le serveur Komga configuré."""

    def __init__(self, api_key):  # pylint: disable=super-init-not-called
        self.base_url = _server_url()
        self.headers = {'X-API-Key': api_key}


def _link_row(user_key):
    if not user_key:
        return None
    conn = _connect()
    try:
        return conn.execute('SELECT * FROM user_komga_links WHERE user_key = ?', (user_key,)).fetchone()
    finally:
        conn.close()


def user_client(user_key):
    """Client Komga du compte relié, ou None si l'utilisateur n'a pas relié de compte."""
    row = _link_row(user_key)
    if not row:
        return None
    return UserKomgaClient(decrypt(row['api_key']))


def has_link(user_key):
    return _link_row(user_key) is not None


# kind : 'komga' (compte du serveur), 'komga_user' (compte relié) ou 'local'. komga_only : seuls
# les tomes rattachés à un livre Komga peuvent avoir un avancement (dénominateur des barres).
ProgressSource = namedtuple('ProgressSource', 'sql params kind komga_only')

_COLUMNS = 'v2.id AS vol_id, k.page, k.pages_count, k.completed, k.read_at, k.synced_at'
SERVER_SOURCE = ProgressSource(
    f'(SELECT {_COLUMNS} FROM volumes v2 JOIN komga_read_progress k ON k.komga_book_id = v2.komga_book_id)',
    (), 'komga', True)
_USER_SQL = (f'(SELECT {_COLUMNS} FROM volumes v2 JOIN user_komga_read_progress k '
             'ON k.komga_book_id = v2.komga_book_id AND k.user_key = ?)')
_LOCAL_SQL = ('(SELECT v2.id AS vol_id, k.page, k.pages_count, k.completed, k.read_at, k.updated_at AS synced_at '
              'FROM volumes v2 JOIN user_local_read_progress k '
              'ON k.series_id = v2.series_id AND k.filename = v2.filename AND k.user_key = ?)')


def _server_sync_on():
    config = load_komga_config()
    return bool(config.get('enabled')) and config.get('sync_read_progress', True) is not False


def progress_source(user_key, reader):
    """Avancement à afficher et à écrire pour ce compte : son compte Komga relié ; sinon, pour
    un lecteur (ou quand Komga n'est pas configuré), son avancement local ; sinon le compte
    Komga du serveur (administrateur)."""
    if user_key and has_link(user_key):
        return ProgressSource(_USER_SQL, (user_key,), 'komga_user', True)
    if user_key and (reader or not _server_sync_on()):
        return ProgressSource(_LOCAL_SQL, (user_key,), 'local', False)
    return SERVER_SOURCE


def current_progress_source():
    from blueprints.auth.roles import current_user_key, is_reader
    try:
        key = current_user_key()
    except RuntimeError:
        key = None
    return progress_source(key, is_reader())


def record_local_progress(user_key, series_id, filename, page, completed, pages_count=None):
    """Page lue dans le lecteur de Bullarr par un compte sans Komga relié."""
    conn = _connect()
    try:
        if pages_count is None:
            row = conn.execute('SELECT page_count FROM volumes WHERE series_id = ? AND filename = ? AND page_count IS NOT NULL LIMIT 1',
                               (series_id, filename)).fetchone()
            pages_count = row[0] if row else None
        conn.execute(
            'INSERT INTO user_local_read_progress (user_key, series_id, filename, page, pages_count, completed, read_at, updated_at) '
            "VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), CURRENT_TIMESTAMP) "
            'ON CONFLICT(user_key, series_id, filename) DO UPDATE SET page = excluded.page, completed = excluded.completed, '
            'pages_count = COALESCE(excluded.pages_count, user_local_read_progress.pages_count), '
            'read_at = excluded.read_at, updated_at = excluded.updated_at',
            (user_key, series_id, filename, page, pages_count, int(bool(completed))))
        conn.commit()
    finally:
        conn.close()


def status(user_key, reader=True):
    import json
    config = load_komga_config()
    available = bool(config.get('enabled') and (config.get('url') or '').strip())
    row = _link_row(user_key)
    source = progress_source(user_key, reader) if user_key else None
    out = {
        'available': available,
        'mode': source.kind if source else None,
        'komga_url': normalize_komga_url(config['url']) if available else None,
        'linked': row is not None,
    }
    if row:
        out.update(komga_user=row['komga_user'], linked_at=row['linked_at'], last_sync_at=row['last_sync_at'],
                   last_error=row['last_error'], stats=json.loads(row['stats_json']) if row['stats_json'] else None)
    return out


def _create_api_key(base_url, username, password):
    """Se connecte à Komga (authentification basique) et crée une clé API pour ce compte.
    Retourne (clé, nom affiché du compte Komga)."""
    session = requests.Session()
    session.auth = (username, password)
    try:
        me = session.get(f"{base_url}/api/v2/users/me", timeout=15)
        if me.status_code in (401, 403):
            raise KomgaError('Identifiant ou mot de passe Komga incorrect')
        if me.status_code != 200:
            raise KomgaError(f"Erreur HTTP Komga {me.status_code}")
        created = session.post(f"{base_url}/api/v2/users/me/api-keys",
                               json={'comment': f"{API_KEY_COMMENT} {time.strftime('%Y-%m-%d %H:%M')}"}, timeout=15)
    except requests.exceptions.RequestException as e:
        raise KomgaError(f"Impossible de contacter Komga: {e}") from e
    if created.status_code not in (200, 201):
        raise KomgaError(f"Komga a refusé la création d'une clé API (HTTP {created.status_code})")
    key = (created.json() or {}).get('key')
    if not key:
        raise KomgaError("Komga n'a pas renvoyé de clé API")
    return key, (me.json() or {}).get('email') or username


def _whoami(base_url, api_key):
    try:
        me = requests.get(f"{base_url}/api/v2/users/me", headers={'X-API-Key': api_key}, timeout=15)
    except requests.exceptions.RequestException as e:
        raise KomgaError(f"Impossible de contacter Komga: {e}") from e
    if me.status_code in (401, 403):
        raise KomgaError('Clé API Komga invalide ou refusée')
    if me.status_code != 200:
        raise KomgaError(f"Erreur HTTP Komga {me.status_code}")
    return (me.json() or {}).get('email') or 'compte Komga'


def link(user_key, username=None, password=None, api_key=None):
    """Relie le compte Komga de l'utilisateur (identifiant + mot de passe, ou clé API existante)
    puis lit tout de suite son avancement. Lève KomgaError (message lisible) en cas d'échec."""
    if not user_key:
        raise KomgaError('Connectez-vous avec un compte Bullarr pour relier Komga')
    base_url = _server_url()
    if api_key:
        komga_user = _whoami(base_url, api_key.strip())
        key = api_key.strip()
    else:
        if not username or not password:
            raise KomgaError('Identifiant et mot de passe Komga requis')
        key, komga_user = _create_api_key(base_url, username.strip(), password)
    conn = _connect()
    try:
        conn.execute('''
            INSERT INTO user_komga_links (user_key, api_key, komga_user, linked_at, last_sync_at, last_error, stats_json)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP, NULL, NULL, NULL)
            ON CONFLICT(user_key) DO UPDATE SET api_key = excluded.api_key, komga_user = excluded.komga_user,
                linked_at = excluded.linked_at, last_sync_at = NULL, last_error = NULL, stats_json = NULL''',
                     (user_key, encrypt(key), komga_user))
        conn.execute('DELETE FROM user_komga_read_progress WHERE user_key = ?', (user_key,))
        conn.commit()
    finally:
        conn.close()
    try:
        sync_user(user_key)
    except KomgaError:
        pass  # relié quand même : l'erreur est gardée dans last_error
    return status(user_key)


def unlink(user_key):
    conn = _connect()
    try:
        conn.execute('DELETE FROM user_komga_links WHERE user_key = ?', (user_key,))
        conn.execute('DELETE FROM user_komga_read_progress WHERE user_key = ?', (user_key,))
        conn.commit()
    finally:
        conn.close()
    return status(user_key)


def sync_user(user_key):
    """Relit l'avancement de lecture du compte Komga relié. Retourne les statistiques, ou None
    sans compte relié. Lève KomgaError si Komga ne répond pas."""
    import json
    client = user_client(user_key)
    if client is None:
        return None
    conn = _connect()
    try:
        try:
            progress = client.list_books_read_progress()
        except KomgaError as exc:
            conn.execute('UPDATE user_komga_links SET last_sync_at = ?, last_error = ? WHERE user_key = ?',
                         (time.time(), str(exc), user_key))
            conn.commit()
            raise
        rows = [(user_key, book_id, p['page'], p['pages_count'], int(p['completed']), p['read_at'])
                for book_id, p in progress.items() if book_id and (p['completed'] or p['page'])]
        conn.execute('DELETE FROM user_komga_read_progress WHERE user_key = ?', (user_key,))
        conn.executemany(
            'INSERT INTO user_komga_read_progress (user_key, komga_book_id, page, pages_count, completed, read_at, synced_at) '
            'VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)', rows)
        stats = {'books': len(progress), 'read': sum(1 for r in rows if r[4]),
                 'reading': sum(1 for r in rows if not r[4])}
        conn.execute('UPDATE user_komga_links SET last_sync_at = ?, last_error = NULL, stats_json = ? WHERE user_key = ?',
                     (time.time(), json.dumps(stats), user_key))
        conn.commit()
    finally:
        conn.close()
    return stats


def sync_all_users():
    conn = _connect()
    try:
        keys = [r[0] for r in conn.execute('SELECT user_key FROM user_komga_links').fetchall()]
    finally:
        conn.close()
    for key in keys:
        try:
            sync_user(key)
        except Exception as exc:  # un compte en échec n'empêche pas les autres
            print(f"⚠️ Synchronisation Komga du compte relié en échec: {exc}")


def record_user_progress(user_key, komga_book_id, page, completed, pages_count=None):
    """Page lue dans le lecteur de Bullarr et envoyée au compte Komga relié : reflétée tout de suite."""
    conn = _connect()
    try:
        if pages_count is None:
            row = conn.execute('SELECT page_count FROM volumes WHERE komga_book_id = ? AND page_count IS NOT NULL LIMIT 1',
                               (komga_book_id,)).fetchone()
            pages_count = row[0] if row else None
        conn.execute(
            'INSERT INTO user_komga_read_progress (user_key, komga_book_id, page, pages_count, completed, read_at, synced_at) '
            "VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), CURRENT_TIMESTAMP) "
            'ON CONFLICT(user_key, komga_book_id) DO UPDATE SET page = excluded.page, completed = excluded.completed, '
            'pages_count = COALESCE(user_komga_read_progress.pages_count, excluded.pages_count), '
            'read_at = excluded.read_at, synced_at = excluded.synced_at',
            (user_key, komga_book_id, page, pages_count, int(bool(completed))))
        conn.commit()
    finally:
        conn.close()
