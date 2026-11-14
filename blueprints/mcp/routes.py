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
        'author': row['local_author'] or '',
        'genre': row['local_genre'] or '',
        'tags': _tags_of(row),
        'oneshot': bool(row['is_oneshot']),
        'to_read_later': bool(row['to_read_later']),
        'favorite': bool(row['is_favorite']),
        'summary': summary[:summary_chars] + ('…' if len(summary) > summary_chars else ''),
    }


_SERIES_COLUMNS = """
    s.id, s.title, s.local_author, s.local_genre, s.tags, s.local_summary, s.manual_summary,
    s.bedetheque_description, s.is_oneshot, s.to_read_later, s.is_favorite,
    COALESCE(s.manual_year_start, s.bedetheque_year_start, s.local_year) AS year
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
    results = []
    for row in _all_series():
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
            row['title'] or '', row['local_author'] or '', row['local_genre'] or '',
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


_SERIES_ID = {'type': 'integer', 'description': 'Identifiant de la série (voir search_series)'}

TOOLS = {
    'search_series': {
        'handler': tool_search_series,
        'description': (
            'Cherche des séries dans la bibliothèque Bullarr. La recherche texte porte sur le titre, '
            "l'auteur, le genre, les tags et le résumé ; plusieurs mots doivent tous être présents. "
            'Filtres : tags (tous requis), année de parution (year_from / year_to). Exemple : des BD '
            'des années 1970 qui parlent de chats : query "chat", year_from 1970, year_to 1979.'
        ),
        'schema': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'description': 'Mots à chercher (tous requis), vide pour tout lister'},
                'tags': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Tags requis'},
                'year_from': {'type': 'integer'},
                'year_to': {'type': 'integer'},
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
