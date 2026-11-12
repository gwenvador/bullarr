"""Comptes administrateur et lecteur.

Un administrateur a accès à tout. Un lecteur parcourt les bibliothèques, lit en ligne,
télécharge les fichiers, tient ses propres listes (« À lire », « Favoris ») et demande
des BD (page /demandes) ; tout le reste lui est refusé côté serveur (READER_ENDPOINTS est
une liste blanche : une route absente de la liste est interdite).

Rôle d'un compte, par ordre de priorité :
1. connexion par mot de passe local, clé API ou authentification désactivée : admin ;
2. rôle forcé dans Configuration → Login → Utilisateurs (users.role_override) ;
3. groupe d'administrateurs OIDC (admin_group de la configuration d'authentification) :
   membre du groupe = admin, sinon lecteur. Sans groupe configuré, tout le monde est admin
   (comportement d'avant l'arrivée des rôles : personne n'est verrouillé dehors)."""
import json
import sqlite3

from flask import current_app, g, session

ROLE_ADMIN = 'admin'
ROLE_READER = 'reader'
ROLES = (ROLE_ADMIN, ROLE_READER)

_GET = frozenset({'GET'})
READER_ENDPOINTS = {
    # Pages
    'static': _GET, 'serve_cover': _GET,
    'library.index': _GET, 'library.library_detail': _GET, 'library.series_detail_page': _GET,
    'library.requests_page': _GET, 'auth.profile_page': _GET, 'auth.serve_avatar': _GET,
    'auth.logout': _GET, 'auth.logout_provider': _GET,
    # Compte
    'auth.current_user': _GET, 'auth.csrf_token': _GET,
    'auth.get_profile': _GET, 'auth.update_profile': frozenset({'PUT'}),
    'auth.upload_avatar': frozenset({'POST'}), 'auth.delete_avatar': frozenset({'DELETE'}),
    'auth.choose_avatar': frozenset({'PUT'}), 'auth.avatar_presets': _GET,
    'auth.get_my_api_key': _GET, 'auth.create_my_api_key': frozenset({'POST'}),
    'auth.delete_my_api_key': frozenset({'DELETE'}),
    # Bibliothèques et séries (lecture)
    'library.libraries': _GET, 'library.library_operations': _GET, 'library.get_library_info': _GET,
    'library.get_library_last_updated': _GET, 'library.get_library_series': _GET,
    'library.get_library_stats_route': _GET, 'library.get_series_details': _GET,
    'library.get_series_volumes': _GET, 'library.get_series_preview_pages': _GET,
    'library.get_catalog_series': _GET,
    'library.manage_series_tags': _GET,
    'bedetheque.get_series_bedetheque_info': _GET, 'bedetheque.get_series_universe': _GET,
    'bedetheque.get_series_read_also': _GET, 'bedetheque.get_volume_bedetheque_reviews': _GET,
    'bedetheque.get_authors_photos': _GET,
    # Lecture en ligne et téléchargement
    'library.get_volume_reader_info': _GET, 'library.get_volume_reader_page': _GET,
    'library.get_volume_reader_thumbnail': _GET, 'library.post_volume_reader_progress': frozenset({'POST'}),
    # « Marquer comme lu / non lu » (tome, série) : état de lecture propre au compte.
    'library.post_volume_read_status': frozenset({'POST'}), 'library.post_series_read_status': frozenset({'POST'}),
    'library.download_series': _GET, 'library.download_volume': _GET,
    'library.download_volumes_zip': frozenset({'POST'}),
    # Listes personnelles
    'library.set_series_reading_lists': frozenset({'PUT'}),
    # Demandes
    'bedetheque.search_bedetheque': _GET,
    'library.list_requests': _GET, 'library.create_request': frozenset({'POST'}),
    'library.lookup_requests': frozenset({'POST'}), 'library.cancel_request': frozenset({'DELETE'}),
    # Compte Komga personnel et son avancement de lecture
    'komga.my_komga_account': frozenset({'GET', 'PUT', 'DELETE'}),
    'komga.sync_komga_read_progress': frozenset({'POST'}), 'komga.get_keep_reading': _GET,
}


def init_tables(cursor):
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_key TEXT PRIMARY KEY,
            name TEXT,
            email TEXT,
            auth_method TEXT,
            groups_json TEXT DEFAULT '[]',
            role_override TEXT,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_reading_lists (
            user_key TEXT NOT NULL,
            series_id INTEGER NOT NULL,
            to_read_later INTEGER DEFAULT 0,
            is_favorite INTEGER DEFAULT 0,
            PRIMARY KEY (user_key, series_id)
        )''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS book_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_key TEXT,
            user_name TEXT,
            bedetheque_url TEXT NOT NULL,
            title TEXT,
            series_id INTEGER,
            status TEXT NOT NULL,
            error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
    # Restrictions d'un lecteur : bibliothèques permises (NULL = toutes) et genres
    # Bédéthèque masqués (contrôle parental).
    columns = {row[1] for row in cursor.execute('PRAGMA table_info(users)').fetchall()}
    # status : NULL = actif, 'pending' = demande d'inscription à valider par un admin.
    # avatar : nom du fichier dans data/avatars (photo de profil choisie par le compte).
    # api_key_hash / api_key_enc : clé API personnelle (empreinte pour la recherche,
    # valeur chiffrée pour l'afficher à son propriétaire dans « Mon profil »).
    for column in ('allowed_libraries_json', 'blocked_genres_json', 'password_hash', 'status', 'avatar',
                   'api_key_hash', 'api_key_enc'):
        if column not in columns:
            cursor.execute(f'ALTER TABLE users ADD COLUMN {column} TEXT')
    # Demande à valider : bibliothèque visée au moment de la demande, décision (qui, quand).
    request_columns = {row[1] for row in cursor.execute('PRAGMA table_info(book_requests)').fetchall()}
    for column, kind in (('library_id', 'INTEGER'), ('decided_at', 'TIMESTAMP'), ('decided_by', 'TEXT')):
        if column not in request_columns:
            cursor.execute(f'ALTER TABLE book_requests ADD COLUMN {column} {kind}')
    cursor.execute("UPDATE book_requests SET status = 'pending', decided_at = NULL, decided_by = NULL, "
                   "error = 'Ajout interrompu par un redémarrage : à valider de nouveau' WHERE status = 'adding'")
    from .activity import init_table as init_activity_table
    init_activity_table(cursor)
    # Sessions ouvertes avant revoked_at refusées (compte supprimé, mot de passe changé).
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS revoked_users (
            user_key TEXT PRIMARY KEY,
            revoked_at REAL NOT NULL
        )''')


def _connect():
    conn = sqlite3.connect(current_app.config['DATABASE'], timeout=60.0)
    conn.row_factory = sqlite3.Row
    return conn


def user_key(user):
    if not user:
        return None
    if user.get('sub'):
        return f"oidc:{user['sub']}"
    return f"password:{user.get('name') or ''}"


def normalize_groups(value):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return sorted({str(item).strip() for item in value if str(item).strip()})


def record_login(user):
    """Enregistre (ou met à jour) le compte à chaque connexion : la page Utilisateurs
    liste ainsi tous ceux qui se sont déjà connectés."""
    key = user_key(user)
    if not key:
        return
    conn = _connect()
    try:
        if user.get('auth_method') == 'password':
            # Compte local : nom, e-mail et rôle se règlent dans les paramètres.
            conn.execute('UPDATE users SET last_seen = CURRENT_TIMESTAMP WHERE user_key = ?', (key,))
            conn.commit()
            return
        conn.execute('''
            INSERT INTO users (user_key, name, email, auth_method, groups_json, last_seen)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_key) DO UPDATE SET name = excluded.name, email = excluded.email,
                auth_method = excluded.auth_method, groups_json = excluded.groups_json,
                last_seen = CURRENT_TIMESTAMP''',
            (key, user.get('name'), user.get('email'), user.get('auth_method') or 'oidc',
             json.dumps(normalize_groups(user.get('groups')))))
        conn.commit()
    finally:
        conn.close()


def computed_role(groups, admin_group):
    admin_group = (admin_group or '').strip()
    if not admin_group:
        return ROLE_ADMIN
    return ROLE_ADMIN if admin_group in normalize_groups(groups) else ROLE_READER


def role_for_user(user, config):
    # Appels internes du planificateur (session signée côté serveur, voir
    # missing_monitor/downloader.py) : toujours administrateur.
    if not user or user.get('auth_method') == 'internal':
        return ROLE_ADMIN
    row = None
    if current_app.config.get('DATABASE'):  # absente de certaines apps de test minimales
        conn = _connect()
        try:
            row = conn.execute('SELECT role_override, groups_json FROM users WHERE user_key = ?',
                               (user_key(user),)).fetchone()
        finally:
            conn.close()
    if row and row['role_override'] in ROLES:
        return row['role_override']
    if user.get('auth_method') == 'password':
        return ROLE_ADMIN  # compte local sans rôle explicite (ancien compte unique)
    groups = user.get('groups')
    if groups is None and row:
        groups = json.loads(row['groups_json'] or '[]')
    return computed_role(groups, config.get('admin_group'))


def current_role(config=None):
    """Rôle de la requête en cours (mis en cache pour la requête). enforce_login passe la
    configuration qu'il vient de lire."""
    if 'bullarr_role' in g:
        return g.bullarr_role
    role = ROLE_ADMIN
    user = request_user()
    if user and not g.get('api_key_auth') and not current_app.config.get('AUTH_BYPASS_LOGIN', False):
        if config is None:
            from .config_store import load_oidc_config
            config = load_oidc_config()
        if config.get('mode', 'none') != 'none':
            role = role_for_user(user, config)
    g.bullarr_role = role
    return role


def request_user():
    """Compte de la requête : celui de la clé API personnelle envoyée, sinon celui de la session."""
    return g.get('api_user') or session.get('user')


def is_reader():
    try:
        return current_role() == ROLE_READER
    except RuntimeError:
        return False


def current_user_key():
    return user_key(request_user())


def reader_allowed(endpoint, method):
    allowed = READER_ENDPOINTS.get(endpoint)
    if not allowed:
        return False
    return method in allowed or (method in ('HEAD', 'OPTIONS') and 'GET' in allowed)


def apply_personal_lists(items):
    """Pour un lecteur, remplace les drapeaux « À lire » / « Favori » (communs, ceux de
    l'administrateur) par les siens. Sans effet pour un administrateur."""
    if not items or not is_reader():
        return items
    key = current_user_key()
    ids = [item['id'] for item in items if item.get('id') is not None]
    flags = {}
    conn = _connect()
    try:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            for row in conn.execute(
                    f"SELECT series_id, to_read_later, is_favorite FROM user_reading_lists "
                    f"WHERE user_key = ? AND series_id IN ({','.join('?' * len(chunk))})", (key, *chunk)):
                flags[row['series_id']] = row
    finally:
        conn.close()
    for item in items:
        row = flags.get(item.get('id'))
        item['to_read_later'] = bool(row['to_read_later']) if row else False
        item['is_favorite'] = bool(row['is_favorite']) if row else False
    return items


def set_personal_lists(series_id, updates):
    key = current_user_key()
    conn = _connect()
    try:
        conn.execute('INSERT OR IGNORE INTO user_reading_lists (user_key, series_id) VALUES (?, ?)', (key, series_id))
        assignments = ', '.join(f'{column} = ?' for column in updates)
        conn.execute(f'UPDATE user_reading_lists SET {assignments} WHERE user_key = ? AND series_id = ?',
                     (*updates.values(), key, series_id))
        conn.commit()
        row = conn.execute('SELECT to_read_later, is_favorite FROM user_reading_lists WHERE user_key = ? AND series_id = ?',
                           (key, series_id)).fetchone()
    finally:
        conn.close()
    return {'to_read_later': bool(row['to_read_later']), 'is_favorite': bool(row['is_favorite'])}



# ===== Ce qu'un lecteur peut voir : bibliothèques et contrôle parental =====
# Genres masqués par le préréglage « Enfant » (genres tels qu'écrits par Bédéthèque).
PARENTAL_PRESET = ['Érotique', 'Érotisme', 'Hentai', 'Manga - Hentai', 'Manga - Seinen', 'Horreur', 'Gore',
                   'Guerre', 'Thriller', 'Polar', 'Policier', 'Humour noir', 'Ésotérisme']

# Genre retenu pour une série : saisi à la main, sinon Bédéthèque, sinon lu dans le fichier.
SERIES_GENRE_SQL = "COALESCE(NULLIF(manual_genre, ''), NULLIF(bedetheque_genre, ''), NULLIF(local_genre, ''), '')"


def _split_genres(value):
    return {part.strip().casefold() for part in (value or '').replace(';', ',').split(',') if part.strip()}


def restrictions_for(user_key_value):
    """(bibliothèques permises ou None pour toutes, genres masqués en minuscules)."""
    conn = _connect()
    try:
        row = conn.execute('SELECT allowed_libraries_json, blocked_genres_json FROM users WHERE user_key = ?',
                           (user_key_value,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None, set()
    allowed = json.loads(row['allowed_libraries_json']) if row['allowed_libraries_json'] else None
    blocked = json.loads(row['blocked_genres_json']) if row['blocked_genres_json'] else []
    return (set(int(i) for i in allowed) if allowed is not None else None), {str(gn).casefold() for gn in blocked}


def current_restrictions():
    """Restrictions du compte connecté ; aucune pour un administrateur."""
    if 'bullarr_restrictions' in g:
        return g.bullarr_restrictions
    result = (None, set())
    if is_reader():
        result = restrictions_for(current_user_key())
    g.bullarr_restrictions = result
    return result


def has_restrictions():
    allowed, blocked = current_restrictions()
    return allowed is not None or bool(blocked)


def library_visible(library_id):
    allowed, _ = current_restrictions()
    return allowed is None or int(library_id) in allowed


def genre_blocked(genre_value, blocked=None):
    if blocked is None:
        blocked = current_restrictions()[1]
    return bool(blocked) and bool(_split_genres(genre_value) & blocked)


def series_visible(series_id):
    if not has_restrictions():
        return True
    conn = _connect()
    try:
        row = conn.execute(f'SELECT library_id, {SERIES_GENRE_SQL} AS genre FROM series WHERE id = ?', (series_id,)).fetchone()
    finally:
        conn.close()
    return row is None or (library_visible(row['library_id']) and not genre_blocked(row['genre']))


def volume_visible(volume_id):
    if not has_restrictions():
        return True
    conn = _connect()
    try:
        row = conn.execute('SELECT series_id FROM volumes WHERE id = ?', (volume_id,)).fetchone()
    finally:
        conn.close()
    return row is None or series_visible(row['series_id'])


def reader_scope_allowed(view_args, json_body=None):
    """Contrôle commun à toutes les routes d'un lecteur : la bibliothèque, la série ou le
    tome visé doit lui être visible."""
    if not has_restrictions():
        return True
    view_args = view_args or {}
    if 'library_id' in view_args and not library_visible(view_args['library_id']):
        return False
    if 'series_id' in view_args and not series_visible(view_args['series_id']):
        return False
    if 'volume_id' in view_args and not volume_visible(view_args['volume_id']):
        return False
    if isinstance(json_body, dict) and isinstance(json_body.get('ids'), list):
        return all(volume_visible(int(i)) for i in json_body['ids'] if str(i).isdigit())
    return True


def filter_libraries(items):
    if not has_restrictions():
        return items
    return [item for item in items if library_visible(item['id'])]


def filter_visible_series(items):
    """Retire d'une liste de séries (dicts avec 'id') celles qu'un lecteur ne doit pas voir."""
    if not items or not has_restrictions():
        return items
    _, blocked = current_restrictions()
    hidden = set()
    conn = _connect()
    try:
        for row in conn.execute(f'SELECT id, library_id, {SERIES_GENRE_SQL} AS genre FROM series'):
            if not library_visible(row['library_id']) or genre_blocked(row['genre'], blocked):
                hidden.add(row['id'])
    finally:
        conn.close()
    return [item for item in items if item.get('id') not in hidden]


def all_genres():
    """Genres présents dans la bibliothèque, du plus fréquent au plus rare."""
    counts = {}
    conn = _connect()
    try:
        for (value,) in conn.execute(f'SELECT {SERIES_GENRE_SQL} FROM series'):
            for part in (value or '').replace(';', ',').split(','):
                part = part.strip()
                if part:
                    counts[part] = counts.get(part, 0) + 1
    finally:
        conn.close()
    return sorted(counts.items(), key=lambda item: (-item[1], item[0].casefold()))
