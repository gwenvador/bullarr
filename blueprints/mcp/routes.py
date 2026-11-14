"""Serveur MCP de Bullarr (transport HTTP, POST /mcp) pour Claude et ChatGPT.

Outils : recherche de séries (mots, tags, année), fiche, liste des tags, tags d'une série,
listes « à lire plus tard » et favoris. Aucune suppression, aucun changement de fichier.
Authentification : jeton dédié BULLARR_MCP_TOKEN, distinct de la clé API, soit dans l'en-tête
Authorization: Bearer ... (Claude Code), soit dans l'adresse /mcp/<jeton> pour les connecteurs
qui ne prennent qu'une URL (claude.ai et l'appli Claude, ChatGPT). Sans ce jeton configuré,
l'endpoint répond 503.
"""
from error_utils import error_message
from encryption import decrypt, encrypt
import hmac
import secrets
import json
import os
import re
import sqlite3
import unicodedata

from flask import Blueprint, current_app, jsonify, request

mcp_bp = Blueprint('mcp', __name__)

PROTOCOL_VERSION = '2024-11-05'
# Versions acceptées : on répond dans celle du client si on la connaît (réponses JSON seules).
SUPPORTED_PROTOCOL_VERSIONS = ('2024-11-05', '2025-03-26', '2025-06-18')
MAX_BODY = 1_000_000
# Champs trop volumineux ou sans intérêt pour un assistant (fiche complète sans les listes externes)
_FICHE_EXCLUDED = {'bedetheque_albums', 'bedetheque_author_links', 'bedetheque_read_also'}


class McpError(Exception):
    pass


def _db():
    conn = sqlite3.connect(current_app.config['DATABASE'], timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def _normalize(text):
    text = unicodedata.normalize('NFKD', str(text or ''))
    text = ''.join(ch for ch in text if not unicodedata.combining(ch))
    return text.lower().replace('œ', 'oe').replace('æ', 'ae')


def _normalize_tag(tag):
    return re.sub(r'\s+', '-', _normalize(tag).strip())


def _tags_of(row):
    try:
        value = json.loads(row['tags']) if row['tags'] else []
    except (TypeError, ValueError):
        return []
    return [str(t) for t in value if t] if isinstance(value, list) else []


def _summary(row):
    return row['local_summary'] or row['manual_summary'] or row['bedetheque_description'] or ''


def _year(row):
    match = re.search(r'(\d{4})', str(row['year'] or ''))
    return int(match.group(1)) if match else None


def _brief(row, summary_chars=300):
    summary = _summary(row)
    return {
        'id': row['id'],
        'title': row['title'],
        'year': _year(row),
        'author': row['author'] or '',
        'genre': row['genre'] or '',
        'tags': _tags_of(row),
        'oneshot': bool(row['is_oneshot']),
        'to_read_later': bool(row['to_read_later']),
        'favorite': bool(row['is_favorite']),
        'status': row['status'],
        'library_id': row['library_id'],
        'owned_volumes': row['owned_count'],
        'missing_volumes': row['missing_count'],
        'summary': summary[:summary_chars] + ('…' if len(summary) > summary_chars else ''),
    }


_SERIES_COLUMNS = """
    s.id, s.title, s.local_author, s.local_genre, s.tags, s.local_summary, s.manual_summary,
    s.bedetheque_description, s.is_oneshot, s.to_read_later, s.is_favorite, s.library_id,
    TRIM(COALESCE(NULLIF(s.manual_author, ''), NULLIF(s.local_author, ''),
         TRIM(COALESCE(s.bedetheque_scenaristes, '') || ', ' || COALESCE(s.bedetheque_dessinateurs, ''), ', '))) AS author,
    COALESCE(NULLIF(s.manual_genre, ''), NULLIF(s.local_genre, ''), s.bedetheque_genre, '') AS genre,
    COALESCE(s.manual_year_start, s.bedetheque_year_start, s.local_year) AS year,
    COALESCE(NULLIF(s.manual_status, ''), s.bedetheque_status, '') AS status,
    (SELECT COUNT(*) FROM volumes v WHERE v.series_id = s.id AND v.filepath IS NOT NULL AND v.filepath != '') AS owned_count,
    (SELECT COUNT(*) FROM volumes v WHERE v.series_id = s.id AND (v.filepath IS NULL OR v.filepath = '')) AS missing_count
"""


def _all_series():
    conn = _db()
    try:
        return conn.execute(f'SELECT {_SERIES_COLUMNS} FROM series s ORDER BY s.title').fetchall()
    finally:
        conn.close()


def _series_exists(series_id):
    conn = _db()
    try:
        return conn.execute('SELECT 1 FROM series WHERE id = ?', (series_id,)).fetchone() is not None
    finally:
        conn.close()


def tool_search_series(args):
    words = [_normalize(w) for w in re.split(r'\s+', str(args.get('query') or '').strip()) if w]
    tags = [_normalize_tag(t) for t in (args.get('tags') or [])]
    year_from = args.get('year_from')
    year_to = args.get('year_to')
    limit = max(1, min(int(args.get('limit', 25)), 100))
    author = _normalize(args.get('author') or '').strip()
    genre = _normalize(args.get('genre') or '').strip()
    library_id = args.get('library_id')
    incomplete = args.get('incomplete')
    results = []
    for row in _all_series():
        if author and author not in _normalize(row['author']):
            continue
        if genre and genre not in _normalize(row['genre']):
            continue
        if library_id is not None and row['library_id'] != int(library_id):
            continue
        if incomplete is not None and bool(row['missing_count']) != bool(incomplete):
            continue
        year = _year(row)
        if year_from is not None or year_to is not None:
            if year is None:
                continue
            if year_from is not None and year < int(year_from):
                continue
            if year_to is not None and year > int(year_to):
                continue
        row_tags = [_normalize_tag(t) for t in _tags_of(row)]
        if any(tag not in row_tags for tag in tags):
            continue
        haystack = _normalize(' '.join([
            row['title'] or '', row['author'] or '', row['genre'] or '',
            ' '.join(_tags_of(row)), _summary(row),
        ]))
        if any(word not in haystack for word in words):
            continue
        results.append(row)
    return {
        'total': len(results),
        'returned': min(len(results), limit),
        'results': [_brief(r) for r in results[:limit]],
    }


def tool_get_series(args):
    series_id = int(args['series_id'])
    conn = _db()
    try:
        row = conn.execute('SELECT * FROM series WHERE id = ?', (series_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise McpError(f'Série {series_id} introuvable')
    detail = {}
    for key in row.keys():
        if key in _FICHE_EXCLUDED:
            continue
        value = row[key]
        if isinstance(value, str) and len(value) > 4000:
            continue
        detail[key] = value
    detail['tags'] = _tags_of(row)
    return detail


def tool_list_tags(args):
    counts = {}
    for row in _all_series():
        for tag in _tags_of(row):
            counts[tag] = counts.get(tag, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], _normalize(item[0])))
    return {'tags': [{'tag': tag, 'series': count} for tag, count in ordered]}


def tool_set_series_tags(args):
    series_id = int(args['series_id'])
    if not _series_exists(series_id):
        raise McpError(f'Série {series_id} introuvable')
    tags, seen = [], set()
    for raw in args.get('tags') or []:
        tag = re.sub(r'\s+', '-', str(raw).strip().lower())
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    conn = _db()
    try:
        conn.execute('UPDATE series SET tags = ? WHERE id = ?', (json.dumps(tags, ensure_ascii=False), series_id))
        conn.commit()
    finally:
        conn.close()
    return {'series_id': series_id, 'tags': tags}


def tool_set_reading_lists(args):
    series_id = int(args['series_id'])
    if not _series_exists(series_id):
        raise McpError(f'Série {series_id} introuvable')
    updates = {column: 1 if args[key] else 0
               for key, column in (('to_read_later', 'to_read_later'), ('is_favorite', 'is_favorite'))
               if key in args}
    if not updates:
        raise McpError('Indiquez to_read_later et/ou is_favorite')
    conn = _db()
    try:
        assignments = ', '.join(f'{column} = ?' for column in updates)
        conn.execute(f'UPDATE series SET {assignments} WHERE id = ?', (*updates.values(), series_id))
        conn.commit()
        row = conn.execute('SELECT to_read_later, is_favorite FROM series WHERE id = ?', (series_id,)).fetchone()
    finally:
        conn.close()
    return {'series_id': series_id, 'to_read_later': bool(row['to_read_later']), 'is_favorite': bool(row['is_favorite'])}


# ---------------------------------------------------------------------------------------------
# Outils ajoutés : lecture (tomes, ajouts récents, lectures, bibliothèques, univers, demandes,
# téléchargements) et écritures légères et réversibles (lu / non lu, surveillance, recherche des
# tomes manquants, métadonnées de série). Rien qui supprime, déplace ou renomme sur le disque.
# Les écritures passent par les routes de l'interface (_call), pour garder exactement la même
# logique (Komga, notifications, rescan) ; l'appel MCP agit comme l'administrateur.

def _call(method, path, body=None, query=None):
    """Appelle une route de Bullarr en interne et renvoie son JSON ; erreur HTTP -> McpError."""
    adapter = current_app.url_map.bind('localhost')
    endpoint, view_args = adapter.match(path, method=method)
    with current_app.test_request_context(path, method=method, json=body, query_string=query or {}):
        response = current_app.make_response(current_app.view_functions[endpoint](**view_args))
        data = response.get_json(silent=True)
    if response.status_code >= 400 or (isinstance(data, dict) and data.get('success') is False):
        message = (data or {}).get('error') if isinstance(data, dict) else None
        raise McpError(message or f'Erreur {response.status_code}')
    return data


def _vol_label(row):
    from blueprints.library.book_requests import _volume_label
    return _volume_label(row)


def _owned(row):
    return bool(row['filepath']) and (row['filesystem_present'] if 'filesystem_present' in row.keys() else 1) != 0


def _limit(args, default, maximum):
    return max(1, min(int(args.get('limit', default)), maximum))


def tool_get_series_volumes(args):
    series_id = int(args['series_id'])
    if not _series_exists(series_id):
        raise McpError(f'Série {series_id} introuvable')
    rows = _call('GET', f'/api/series/{series_id}/volumes')
    rows = rows if isinstance(rows, list) else (rows or {}).get('volumes', [])
    volumes = []
    for v in rows:
        if v.get('id') is None:
            continue  # album Bédéthèque sans fiche locale : pas encore suivi
        ci = v.get('comicinfo') or {}
        if isinstance(ci, str):
            try:
                ci = json.loads(ci)
            except ValueError:
                ci = {}
        side = max(v.get('page_width') or 0, v.get('page_height') or 0)
        owned = bool(v.get('filepath')) and v.get('filesystem_present', 1) != 0
        volumes.append({
            'volume_id': v['id'],
            'label': _vol_label(v),
            'title': (ci or {}).get('title') or '',
            'owned': owned,
            'format': v.get('format') if owned else None,
            'pages': v.get('page_count') if owned else None,
            'page_size_px': side or None,
            'read': bool(v.get('komga_read_completed')) if owned else None,
            'reading_page': v.get('komga_read_page') if owned and not v.get('komga_read_completed') else None,
        })
    return {'series_id': series_id, 'owned': sum(1 for v in volumes if v['owned']),
            'missing': sum(1 for v in volumes if not v['owned']), 'volumes': volumes}


def tool_list_recent_additions(args):
    days = max(1, min(int(args.get('days', 7)), 365))
    limit = _limit(args, 50, 200)
    conn = _db()
    try:
        rows = conn.execute("""
            SELECT f.series_id, f.series_title, f.filename, f.action, f.created_at
            FROM import_history_files f
            WHERE f.status = 'success' AND f.action IN ('imported', 'replaced')
              AND f.created_at >= datetime('now', ?)
            ORDER BY f.created_at DESC, f.id DESC LIMIT ?
        """, (f'-{days} days', limit)).fetchall()
    finally:
        conn.close()
    return {'days': days, 'count': len(rows), 'additions': [
        {'series_id': r['series_id'], 'series_title': r['series_title'], 'file': r['filename'],
         'replaced': r['action'] == 'replaced', 'at': r['created_at']} for r in rows]}


def tool_get_reading_history(args):
    limit = _limit(args, 50, 300)
    data = _call('GET', '/api/reading-history')
    items = data.get('items') or []
    return {'source': data.get('source_label'), 'total': len(items), 'items': [
        {key: item.get(key) for key in ('series_id', 'series_title', 'volume_id', 'label', 'completed',
                                         'page', 'pages_count', 'read_at', 'library_name')}
        for item in items[:limit]]}


def tool_list_libraries(args):
    conn = _db()
    try:
        rows = conn.execute("""
            SELECT l.id, l.name, l.is_default,
                   (SELECT COUNT(*) FROM series s WHERE s.library_id = l.id) AS series,
                   (SELECT COUNT(*) FROM volumes v JOIN series s ON s.id = v.series_id
                     WHERE s.library_id = l.id AND v.filepath IS NOT NULL AND v.filepath != '') AS owned,
                   (SELECT COUNT(*) FROM volumes v JOIN series s ON s.id = v.series_id
                     WHERE s.library_id = l.id AND (v.filepath IS NULL OR v.filepath = '')) AS missing
            FROM libraries l ORDER BY l.name
        """).fetchall()
    finally:
        conn.close()
    return {'libraries': [{'library_id': r['id'], 'name': r['name'], 'default': bool(r['is_default']),
                           'series': r['series'], 'owned_volumes': r['owned'], 'missing_volumes': r['missing']}
                          for r in rows]}


def tool_list_universes(args):
    conn = _db()
    try:
        universes = conn.execute('SELECT id, name FROM universes ORDER BY name').fetchall()
        links = conn.execute("""
            SELECT us.universe_id, s.id, s.title FROM universe_series us
            JOIN series s ON s.id = us.series_id ORDER BY s.title
        """).fetchall()
    finally:
        conn.close()
    members = {}
    for row in links:
        members.setdefault(row['universe_id'], []).append({'series_id': row['id'], 'title': row['title']})
    return {'universes': [{'universe_id': u['id'], 'name': u['name'], 'series': members.get(u['id'], [])}
                          for u in universes]}


def tool_list_requests(args):
    query = {'status': args['status']} if args.get('status') else None
    data = _call('GET', '/api/requests', query=query)
    keys = ('id', 'kind', 'status', 'title', 'volume_label', 'series_id', 'user_name', 'library_name',
            'owned', 'total', 'error', 'created_at')
    return {'requests': [{k: r.get(k) for k in keys if k in r} for r in (data.get('requests') or [])[:_limit(args, 50, 300)]]}


def tool_list_downloads(args):
    include_finished = bool(args.get('include_finished'))
    conn = _db()
    try:
        where = '' if include_finished else "WHERE status NOT IN ('imported', 'failed', 'cancelled', 'skipped')"
        rows = conn.execute(f"""
            SELECT id, title, client, status, series_id, volume_number, bytes_downloaded, bytes_total,
                   created_at, completed_at, paused
            FROM active_downloads {where} ORDER BY id DESC LIMIT ?
        """, (_limit(args, 30, 200),)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        total = r['bytes_total'] or 0
        out.append({'id': r['id'], 'title': r['title'], 'client': r['client'], 'status': r['status'],
                    'paused': bool(r['paused']), 'series_id': r['series_id'], 'volume_number': r['volume_number'],
                    'progress_pct': round(100 * (r['bytes_downloaded'] or 0) / total) if total else None,
                    'created_at': r['created_at'], 'completed_at': r['completed_at']})
    return {'downloads': out}


def tool_set_read_status(args):
    read = bool(args.get('read', True))
    if args.get('volume_id') is not None:
        _call('POST', f"/api/volumes/{int(args['volume_id'])}/read-status", {'read': read})
        return {'volume_id': int(args['volume_id']), 'read': read}
    if args.get('series_id') is not None:
        data = _call('POST', f"/api/series/{int(args['series_id'])}/read-status", {'read': read})
        return {'series_id': int(args['series_id']), 'read': read,
                **{k: data[k] for k in ('done', 'failed') if k in data}}
    raise McpError('Indiquez volume_id ou series_id')


def tool_set_series_monitor(args):
    series_id = int(args['series_id'])
    enabled = bool(args.get('enabled', True))
    _call('POST', f'/api/missing-monitor/series/{series_id}/monitor', {'enabled': enabled})
    return {'series_id': series_id, 'monitored': enabled}


def tool_search_missing_volumes(args):
    series_id = int(args['series_id'])
    body = {'series_id': series_id}
    if args.get('volume_id') is not None:
        conn = _db()
        try:
            vol = conn.execute('SELECT * FROM volumes WHERE id = ? AND series_id = ?',
                               (int(args['volume_id']), series_id)).fetchone()
        finally:
            conn.close()
        if vol is None:
            raise McpError('Tome introuvable dans cette série')
        if _owned(vol):
            raise McpError(f'{_vol_label(vol)} est déjà en bibliothèque')
        if vol['is_integral']:
            body.update(is_integral=True, volume_number=vol['integral_number'])
        elif vol['is_hs']:
            body.update(is_hs=True, volume_number=vol['hs_number'])
        elif vol['is_episode']:
            body.update(is_episode=True, volume_number=vol['episode_number'])
        elif vol['volume_number'] is not None:
            body['volume_number'] = vol['volume_number']
        else:
            body['oneshot'] = True
    data = _call('POST', '/api/bedetheque/auto-acquire/run', body)
    return {'series_id': series_id, 'started': True,
            'message': (data or {}).get('message') or 'Recherche automatique lancée ; les tomes trouvés '
                       'avec confiance seront téléchargés puis importés.'}


_METADATA_FIELDS = ('summary', 'genre', 'status', 'author', 'year_start', 'year_end', 'universe_id')


def tool_update_series_metadata(args):
    series_id = int(args['series_id'])
    body = {key: args[key] for key in _METADATA_FIELDS if key in args}
    if not body:
        raise McpError('Aucun champ à modifier')
    _call('PUT', f'/api/series/{series_id}/manual-metadata', body)
    return {'series_id': series_id, 'updated': sorted(body)}


_SERIES_ID = {'type': 'integer', 'description': 'Identifiant de la série (voir search_series)'}

TOOLS = {
    'search_series': {
        'handler': tool_search_series,
        'description': (
            'Cherche des séries dans la bibliothèque Bullarr. La recherche texte porte sur le titre, '
            "l'auteur, le genre, les tags et le résumé ; plusieurs mots doivent tous être présents. "
            'Filtres : tags (tous requis), année de parution (year_from / year_to), auteur, genre, '
            'bibliothèque, incomplete (true = il manque des tomes). Exemple : des BD des années 1970 qui '
            'parlent de chats : query "chat", year_from 1970, year_to 1979.'
        ),
        'schema': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'description': 'Mots à chercher (tous requis), vide pour tout lister'},
                'tags': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Tags requis'},
                'year_from': {'type': 'integer'},
                'year_to': {'type': 'integer'},
                'author': {'type': 'string', 'description': "Partie du nom de l'auteur"},
                'genre': {'type': 'string', 'description': 'Partie du genre'},
                'library_id': {'type': 'integer', 'description': 'Voir list_libraries'},
                'incomplete': {'type': 'boolean', 'description': 'true : séries où il manque des tomes ; false : complètes'},
                'limit': {'type': 'integer', 'description': '1 à 100, 25 par défaut'},
            },
        },
    },
    'get_series': {
        'handler': tool_get_series,
        'description': "Fiche complète d'une série : résumé, auteur, genre, tags, statut, listes.",
        'schema': {'type': 'object', 'properties': {'series_id': _SERIES_ID}, 'required': ['series_id']},
    },
    'list_tags': {
        'handler': tool_list_tags,
        'description': 'Tous les tags de la bibliothèque, avec le nombre de séries pour chacun.',
        'schema': {'type': 'object', 'properties': {}},
    },
    'set_series_tags': {
        'handler': tool_set_series_tags,
        'description': "Remplace la liste de tags d'une série (minuscules, tirets). À utiliser après avoir lu le résumé.",
        'schema': {
            'type': 'object',
            'properties': {'series_id': _SERIES_ID, 'tags': {'type': 'array', 'items': {'type': 'string'}}},
            'required': ['series_id', 'tags'],
        },
    },
    'set_reading_lists': {
        'handler': tool_set_reading_lists,
        'description': 'Ajoute ou retire une série de la liste « à lire plus tard » et/ou des favoris.',
        'schema': {
            'type': 'object',
            'properties': {
                'series_id': _SERIES_ID,
                'to_read_later': {'type': 'boolean'},
                'is_favorite': {'type': 'boolean'},
            },
            'required': ['series_id'],
        },
    },
    'get_series_volumes': {
        'handler': tool_get_series_volumes,
        'description': ("Tomes d'une série, possédés et manquants : libellé (Tome N, HS, Intégrale), titre, "
                        'format, nombre de pages, taille des pages en pixels, lu ou non, page en cours.'),
        'schema': {'type': 'object', 'properties': {'series_id': _SERIES_ID}, 'required': ['series_id']},
    },
    'list_recent_additions': {
        'handler': tool_list_recent_additions,
        'description': 'Fichiers ajoutés ou remplacés dans la bibliothèque ces derniers jours, du plus récent au plus ancien.',
        'schema': {'type': 'object', 'properties': {
            'days': {'type': 'integer', 'description': '1 à 365, 7 par défaut'},
            'limit': {'type': 'integer', 'description': '1 à 200, 50 par défaut'}}},
    },
    'get_reading_history': {
        'handler': tool_get_reading_history,
        'description': 'Tomes lus ou commencés, du plus récent au plus ancien (compte administrateur).',
        'schema': {'type': 'object', 'properties': {'limit': {'type': 'integer', 'description': '1 à 300, 50 par défaut'}}},
    },
    'list_libraries': {
        'handler': tool_list_libraries,
        'description': 'Bibliothèques avec leur nombre de séries, de tomes possédés et de tomes manquants.',
        'schema': {'type': 'object', 'properties': {}},
    },
    'list_universes': {
        'handler': tool_list_universes,
        'description': 'Univers (groupes de séries liées, ex. spin-offs) et les séries de chacun.',
        'schema': {'type': 'object', 'properties': {}},
    },
    'list_requests': {
        'handler': tool_list_requests,
        'description': 'Demandes de séries ou de tomes faites par les lecteurs.',
        'schema': {'type': 'object', 'properties': {
            'status': {'type': 'string', 'description': 'Filtre facultatif : pending (en attente), searching, added, rejected…'},
            'limit': {'type': 'integer'}}},
    },
    'list_downloads': {
        'handler': tool_list_downloads,
        'description': 'Téléchargements en cours (progression, client) ; include_finished ajoute les terminés et échoués.',
        'schema': {'type': 'object', 'properties': {
            'include_finished': {'type': 'boolean'}, 'limit': {'type': 'integer', 'description': '1 à 200, 30 par défaut'}}},
    },
    'set_read_status': {
        'handler': tool_set_read_status,
        'description': ('Marque un tome (volume_id) ou tous les tomes possédés d\'une série (series_id) comme lus '
                        'ou non lus, pour le compte administrateur (synchronisé avec Komga si relié).'),
        'schema': {'type': 'object', 'properties': {
            'volume_id': {'type': 'integer', 'description': 'Voir get_series_volumes'},
            'series_id': _SERIES_ID, 'read': {'type': 'boolean', 'description': 'true par défaut'}}},
    },
    'set_series_monitor': {
        'handler': tool_set_series_monitor,
        'description': 'Active ou coupe la surveillance d\'une série : recherche et téléchargement automatiques des tomes manquants.',
        'schema': {'type': 'object', 'properties': {'series_id': _SERIES_ID, 'enabled': {'type': 'boolean'}},
                   'required': ['series_id', 'enabled']},
    },
    'search_missing_volumes': {
        'handler': tool_search_missing_volumes,
        'description': ('Lance tout de suite la recherche automatique des tomes manquants d\'une série, ou d\'un seul '
                        'tome manquant (volume_id). Seuls les résultats trouvés avec confiance sont téléchargés.'),
        'schema': {'type': 'object', 'properties': {
            'series_id': _SERIES_ID, 'volume_id': {'type': 'integer', 'description': 'Tome manquant précis (facultatif)'}},
            'required': ['series_id']},
    },
    'update_series_metadata': {
        'handler': tool_update_series_metadata,
        'description': ('Corrige les métadonnées d\'une série (résumé, genre, statut, auteur, années, univers). '
                        'Seuls les champs fournis changent ; chaîne vide = effacer. Ne touche à aucun fichier.'),
        'schema': {'type': 'object', 'properties': {
            'series_id': _SERIES_ID, 'summary': {'type': 'string'}, 'genre': {'type': 'string'},
            'status': {'type': 'string'}, 'author': {'type': 'string'},
            'year_start': {'type': 'string'}, 'year_end': {'type': 'string'},
            'universe_id': {'type': ['integer', 'null'], 'description': 'Voir list_universes ; null retire l\'univers'}},
            'required': ['series_id']},
    },
}


def _error(request_id, code, message):
    return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': code, 'message': message}}


def process(message):
    """Traite un message JSON-RPC et renvoie la réponse (None pour une notification)."""
    if not isinstance(message, dict):
        return _error(None, -32600, 'Requête JSON-RPC invalide')
    method = message.get('method')
    request_id = message.get('id')
    if request_id is None:
        return None  # notification, ex. notifications/initialized
    if method == 'initialize':
        requested = (message.get('params') or {}).get('protocolVersion')
        result = {
            'protocolVersion': requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION,
            'capabilities': {'tools': {}},
            'serverInfo': {'name': 'bullarr', 'version': current_app.config.get('APP_VERSION', '')},
        }
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        result = {'tools': [
            {'name': name, 'description': spec['description'], 'inputSchema': spec['schema']}
            for name, spec in TOOLS.items()
        ]}
    elif method == 'tools/call':
        params = message.get('params') or {}
        spec = TOOLS.get(params.get('name'))
        if spec is None:
            return _error(request_id, -32602, f"Outil inconnu : {params.get('name')}")
        try:
            output = spec['handler'](params.get('arguments') or {})
            result = {'content': [{'type': 'text', 'text': json.dumps(output, ensure_ascii=False, indent=2)}],
                      'isError': False}
        except (McpError, KeyError, ValueError, TypeError, sqlite3.Error) as exc:
            result = {'content': [{'type': 'text', 'text': f'Erreur : {error_message(exc)}'}], 'isError': True}
    else:
        return _error(request_id, -32601, f'Méthode non gérée : {method}')
    return {'jsonrpc': '2.0', 'id': request_id, 'result': result}


# Interrupteur et jeton réglables depuis Configuration → IA, dans mcp_config.json (à côté de
# ai_config.json). Le jeton BULLARR_MCP_TOKEN du .env, s'il existe, reste prioritaire.
def _config_path():
    return os.path.join(os.path.dirname(current_app.config['AI_CONFIG_FILE']), 'mcp_config.json')


def load_mcp_config():
    cfg = {'enabled': True, 'token': ''}
    try:
        with open(_config_path(), 'r', encoding='utf-8') as handle:
            stored = json.load(handle)
        if isinstance(stored, dict):
            cfg['enabled'] = bool(stored.get('enabled', True))
            cfg['token'] = decrypt(stored['token']) if stored.get('token') else ''
    except (OSError, ValueError):
        pass
    return cfg


def save_mcp_config(cfg):
    with open(_config_path(), 'w', encoding='utf-8') as handle:
        json.dump({'enabled': bool(cfg.get('enabled')),
                   'token': encrypt(cfg['token']) if cfg.get('token') else ''}, handle, indent=4)


def _effective_token(cfg=None):
    """(jeton, source) : celui du .env d'abord, sinon celui créé depuis la Configuration."""
    env = os.environ.get('BULLARR_MCP_TOKEN', '')
    if env:
        return env, 'env'
    cfg = cfg or load_mcp_config()
    return (cfg['token'], 'settings') if cfg['token'] else ('', None)


def _info(cfg):
    token, source = _effective_token(cfg)
    return {'success': True, 'enabled': bool(cfg['enabled'] and token), 'switch': cfg['enabled'],
            'token': token or None, 'token_source': source, 'tools': list(TOOLS)}


@mcp_bp.route('/api/mcp/config', methods=['POST'])
def mcp_config():
    """Active ou coupe le serveur MCP ; à la première activation sans jeton, en crée un.
    {"regenerate": true} remplace le jeton enregistré (pas celui du .env)."""
    body = request.get_json(silent=True) or {}
    cfg = load_mcp_config()
    if 'enabled' in body:
        cfg['enabled'] = bool(body['enabled'])
    if body.get('regenerate') or (cfg['enabled'] and not _effective_token(cfg)[0]):
        cfg['token'] = secrets.token_urlsafe(32)
    save_mcp_config(cfg)
    return jsonify(_info(cfg))


def _token_ok(supplied, expected):
    return bool(supplied) and hmac.compare_digest(supplied.encode('utf-8'), expected.encode('utf-8'))


@mcp_bp.route('/api/mcp/info', methods=['GET'])
def mcp_info():
    """Onglet IA de la Configuration (administrateurs) : état du serveur MCP et jeton à copier."""
    return jsonify(_info(load_mcp_config()))


@mcp_bp.route('/mcp', methods=['GET', 'POST'])
@mcp_bp.route('/mcp/<path:url_token>', methods=['GET', 'POST'])
def mcp_endpoint(url_token=None):
    """Endpoint MCP (Streamable HTTP, réponses JSON seulement). Accès par jeton dédié, dans
    l'en-tête Authorization ou dans l'adresse (/mcp/<jeton>)."""
    cfg = load_mcp_config()
    expected, _source = _effective_token(cfg)
    if not cfg['enabled']:
        return jsonify({'success': False, 'error': 'Serveur MCP désactivé dans la Configuration'}), 503
    if not expected:
        return jsonify({'success': False, 'error': 'Serveur MCP désactivé : aucun jeton configuré'}), 503
    if request.method == 'GET':
        # Pas de flux SSE côté serveur : les clients utilisent uniquement POST
        return jsonify({'success': False, 'error': 'Utilisez POST'}), 405
    header = request.headers.get('Authorization') or ''
    supplied = header[7:] if header.startswith('Bearer ') else ''
    if not (_token_ok(supplied, expected) or _token_ok(url_token or '', expected)):
        return (jsonify({'success': False, 'error': 'Jeton MCP absent ou invalide'}), 401,
                {'WWW-Authenticate': 'Bearer realm="bullarr-mcp"'})
    if (request.content_length or 0) > MAX_BODY:
        return jsonify({'success': False, 'error': 'Requête trop volumineuse'}), 413
    message = request.get_json(silent=True)
    if message is None:
        return jsonify(_error(None, -32700, 'JSON invalide')), 400
    if isinstance(message, list):
        responses = [r for r in (process(m) for m in message) if r is not None]
        return (jsonify(responses), 200) if responses else ('', 202)
    response = process(message)
    return ('', 202) if response is None else (jsonify(response), 200)
