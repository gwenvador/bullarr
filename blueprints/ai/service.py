"""IA (API Claude d'Anthropic) : configuration, appel du modèle et logique des tags.

L'utilisateur saisit sa clé API dans Configuration → IA. Deux traitements utilisent l'IA :
- création des tags : propose des tags pour des séries (sans tags, ou toutes) à partir du
  titre, des auteurs, du genre et du résumé, en réutilisant le vocabulaire existant ;
  les tags proposés s'ajoutent aux tags existants, rien n'est retiré ;
- normalisation des tags : repère les tags qui veulent dire la même chose
  (« de-gaulle », « charles-de-gaulle », « general de gaulle ») et propose un tag commun.
  Rien n'est modifié tant que l'utilisateur n'a pas validé les regroupements.
"""
import json
import os
import re
import sqlite3

from flask import current_app

from encryption import decrypt, encrypt

MODELS = {
    'claude-opus-5-5': 'Claude Opus 5.5',
    'claude-sonnet-5-5': 'Claude Sonnet 5.5',
    'claude-haiku-5-5': 'Claude Haiku 5.5',
}
DEFAULT_MODEL = 'claude-opus-5-5'
# Modèles qui acceptent le repli côté serveur (fallbacks: "default") quand une requête
# est déclinée par les filtres de sécurité.
_FALLBACK_MODELS = {'claude-opus-5-5', 'claude-sonnet-5-5'}
MASK = '****'

GENERATE_BATCH_SIZE = 20
SUMMARY_MAX_CHARS = 1200
VOCABULARY_MIN_COUNT = 2


class AiError(Exception):
    """Erreur à afficher telle quelle à l'utilisateur."""


# ===== Configuration =====

def default_ai_config():
    return dict(current_app.config['AI_CONFIG'])


def load_ai_config():
    """Configuration IA, clé API déchiffrée dans api_key_decrypted."""
    path = current_app.config['AI_CONFIG_FILE']
    cfg = default_ai_config()
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                stored = json.load(handle)
            if isinstance(stored, dict):
                cfg.update(stored)
        except (OSError, ValueError):
            pass
    if cfg.get('api_key'):
        decrypted = decrypt(cfg['api_key'])
        if decrypted:
            cfg['api_key_decrypted'] = decrypted
    if cfg.get('model') not in MODELS:
        cfg['model'] = DEFAULT_MODEL
    return cfg


def save_ai_config(cfg):
    path = current_app.config['AI_CONFIG_FILE']
    to_save = {key: value for key, value in cfg.items() if not key.endswith('_decrypted')}
    plain = cfg.get('api_key_decrypted')
    to_save['api_key'] = encrypt(plain) if plain else ''
    try:
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(to_save, handle, indent=4)
        os.chmod(path, 0o600)
        return True
    except OSError as exc:
        print(f"Erreur sauvegarde config IA : {exc}")
        return False


def public_config(cfg):
    key = cfg.get('api_key_decrypted') or ''
    return {
        'enabled': bool(cfg.get('enabled')),
        'model': cfg.get('model') or DEFAULT_MODEL,
        'models': [{'id': model_id, 'name': name} for model_id, name in MODELS.items()],
        'has_api_key': bool(key),
        'api_key': MASK if key else '',
        'api_key_hint': f'…{key[-4:]}' if len(key) > 8 else '',
    }


def merge_form(cfg, data):
    """Reporte le formulaire dans cfg. Renvoie un message d'erreur ou None."""
    if 'enabled' in data:
        cfg['enabled'] = bool(data.get('enabled'))
    if 'model' in data:
        model = str(data.get('model') or '')
        if model not in MODELS:
            return 'Modèle inconnu'
        cfg['model'] = model
    if 'api_key' in data:
        key = str(data.get('api_key') or '').strip()
        if key != MASK:
            cfg['api_key_decrypted'] = key
            if not key:
                cfg['api_key'] = ''
    if cfg.get('enabled') and not cfg.get('api_key_decrypted'):
        return 'Saisissez une clé API pour activer l\'IA'
    return None


# ===== Appel du modèle =====

def _client(cfg):
    key = cfg.get('api_key_decrypted')
    if not key:
        raise AiError('Aucune clé API : renseignez-la dans Configuration → IA')
    import anthropic
    return anthropic.Anthropic(api_key=key, max_retries=3, timeout=900.0)


def _friendly_error(exc):
    import anthropic
    if isinstance(exc, anthropic.AuthenticationError):
        return 'Clé API refusée par Anthropic'
    if isinstance(exc, anthropic.PermissionDeniedError):
        return 'Cette clé API n\'a pas accès à ce modèle'
    if isinstance(exc, anthropic.NotFoundError):
        return 'Modèle introuvable pour cette clé API'
    if isinstance(exc, anthropic.RateLimitError):
        return 'Limite de requêtes Anthropic atteinte, réessayez dans un moment'
    if isinstance(exc, anthropic.APIStatusError):
        message = getattr(exc, 'message', '') or str(exc)
        return f'Erreur de l\'API Anthropic ({exc.status_code}) : {message}'
    if isinstance(exc, anthropic.APIConnectionError):
        return 'Impossible de joindre l\'API Anthropic'
    return str(exc)


def test_connection(cfg):
    """Vérifie la clé et l'accès au modèle choisi, sans consommer de tokens."""
    client = _client(cfg)
    try:
        model = client.models.retrieve(cfg.get('model') or DEFAULT_MODEL)
    except Exception as exc:  # noqa: BLE001 - message lisible pour l'interface
        raise AiError(_friendly_error(exc)) from exc
    return getattr(model, 'display_name', None) or MODELS.get(cfg.get('model'), cfg.get('model'))


def call_json(cfg, system, user, schema, cache_system=False, max_tokens=64000):
    """Envoie une requête au modèle et renvoie la réponse JSON (validée par le schéma)."""
    client = _client(cfg)
    model = cfg.get('model') or DEFAULT_MODEL
    system_block = {'type': 'text', 'text': system}
    if cache_system:
        system_block['cache_control'] = {'type': 'ephemeral'}
    params = {
        'model': model,
        'max_tokens': max_tokens,
        'system': [system_block],
        'messages': [{'role': 'user', 'content': user}],
        'thinking': {'type': 'adaptive'},
        'output_config': {
            'effort': 'medium',
            'format': {'type': 'json_schema', 'schema': schema},
        },
    }
    if model in _FALLBACK_MODELS:
        params['betas'] = ['server-side-fallback-2026-07-01']
        params['fallbacks'] = 'default'
    try:
        with client.beta.messages.stream(**params) as stream:
            message = stream.get_final_message()
    except Exception as exc:  # noqa: BLE001 - message lisible pour l'interface
        raise AiError(_friendly_error(exc)) from exc
    if message.stop_reason == 'refusal':
        raise AiError('Le modèle a refusé de traiter cette demande')
    if message.stop_reason == 'max_tokens':
        raise AiError('Réponse du modèle tronquée (trop longue)')
    text = next((block.text for block in message.content if block.type == 'text'), '')
    try:
        return json.loads(text)
    except ValueError as exc:
        raise AiError('Réponse du modèle illisible') from exc


# ===== Tags =====

def clean_tag(value):
    """Forme des tags de Bullarr : minuscules, mots reliés par des tirets."""
    tag = str(value or '').strip().lstrip('#').strip().lower()
    return re.sub(r'\s+', '-', tag)


def parse_tags(raw):
    try:
        value = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return [str(tag) for tag in value if tag] if isinstance(value, list) else []


def dedupe(tags):
    result, seen = [], set()
    for tag in tags:
        if tag and tag not in seen:
            seen.add(tag)
            result.append(tag)
    return result


def _db():
    conn = sqlite3.connect(current_app.config['DATABASE'], timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def tag_counts(conn):
    counts = {}
    for row in conn.execute('SELECT tags FROM series'):
        for tag in parse_tags(row['tags']):
            counts[tag] = counts.get(tag, 0) + 1
    return counts


NORMALIZE_SCHEMA = {
    'type': 'object',
    'properties': {
        'groups': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'canonical': {'type': 'string'},
                    'variants': {'type': 'array', 'items': {'type': 'string'}},
                    'reason': {'type': 'string'},
                },
                'required': ['canonical', 'variants', 'reason'],
                'additionalProperties': False,
            },
        },
    },
    'required': ['groups'],
    'additionalProperties': False,
}

NORMALIZE_SYSTEM = (
    "Tu aides à ranger les tags d'une bibliothèque personnelle de bandes dessinées "
    "(francophone). On te donne la liste complète des tags avec, entre parenthèses, le "
    "nombre de séries qui portent chaque tag.\n\n"
    "Repère les tags qui désignent exactement la même chose et doivent devenir un seul tag : "
    "variantes d'écriture, pluriel ou singulier, accents, fautes de frappe, traductions, "
    "abréviations, et formes différentes du nom d'une même personne, d'un même lieu ou "
    "d'un même événement (par exemple « de-gaulle », « charles-de-gaulle » et "
    "« general-de-gaulle »).\n\n"
    "Ne regroupe pas des notions seulement proches ou liées : « guerre » et "
    "« seconde-guerre-mondiale » restent distincts, comme « humour » et « satire ». "
    "En cas de doute, ne regroupe pas.\n\n"
    "Pour chaque groupe :\n"
    "- variants : tous les tags du groupe, recopiés exactement comme dans la liste "
    "(au moins deux) ;\n"
    "- canonical : le tag à garder, en français, en minuscules, mots reliés par des "
    "tirets ; de préférence la forme la plus claire ou la plus utilisée ;\n"
    "- reason : une phrase courte qui explique le regroupement.\n"
    "Ne renvoie que les groupes qui fusionnent au moins deux tags existants."
)


def normalize_proposals(cfg, conn):
    """Regroupements proposés par le modèle, filtrés sur les tags qui existent vraiment."""
    counts = tag_counts(conn)
    if len(counts) < 2:
        return []
    listing = '\n'.join(f'{tag} ({count})' for tag, count in sorted(counts.items()))
    data = call_json(cfg, NORMALIZE_SYSTEM, f'Tags de la bibliothèque :\n{listing}', NORMALIZE_SCHEMA)
    groups, used = [], set()
    for group in data.get('groups') or []:
        canonical = clean_tag(group.get('canonical'))
        variants = [tag for tag in dedupe(group.get('variants') or [])
                    if tag in counts and tag not in used]
        # Garde aussi un tag seul renommé (« general de gaulle » → « general-de-gaulle »).
        if not canonical or not [tag for tag in variants if tag != canonical]:
            continue
        used.update(variants)
        groups.append({
            'canonical': canonical,
            'variants': [{'tag': tag, 'series': counts[tag]} for tag in variants],
            'reason': str(group.get('reason') or ''),
            'series': sum(counts[tag] for tag in variants),
        })
    groups.sort(key=lambda item: -item['series'])
    return groups


def apply_tag_mapping(conn, mapping):
    """Remplace chaque tag de mapping par sa cible dans toutes les séries.
    Renvoie le nombre de séries modifiées."""
    changed = 0
    rows = conn.execute('SELECT id, tags FROM series').fetchall()
    for row in rows:
        tags = parse_tags(row['tags'])
        if not tags:
            continue
        updated = dedupe([mapping.get(tag, tag) for tag in tags])
        if updated != tags:
            conn.execute('UPDATE series SET tags = ? WHERE id = ?',
                         (json.dumps(updated, ensure_ascii=False), row['id']))
            changed += 1
    conn.commit()
    return changed


GENERATE_SCHEMA = {
    'type': 'object',
    'properties': {
        'series': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'integer'},
                    'tags': {'type': 'array', 'items': {'type': 'string'}},
                },
                'required': ['id', 'tags'],
                'additionalProperties': False,
            },
        },
    },
    'required': ['series'],
    'additionalProperties': False,
}


def generate_system(vocabulary):
    listing = ', '.join(vocabulary) if vocabulary else '(aucun tag pour l\'instant)'
    return (
        "Tu ajoutes des tags aux séries d'une bibliothèque personnelle de bandes dessinées "
        "(francophone). Les tags servent à retrouver des séries : genre, thèmes, époque, "
        "lieux, personnages historiques ou événements réels, public, ton.\n\n"
        "Règles :\n"
        "- 3 à 8 tags par série, seulement ceux que les informations fournies justifient ;\n"
        "- en français, en minuscules, mots reliés par des tirets (ex. « seconde-guerre-mondiale ») ;\n"
        "- réutilise en priorité les tags déjà employés dans la bibliothèque (liste ci-dessous), "
        "tels quels, plutôt que d'inventer une variante ; crée un nouveau tag seulement si aucun "
        "ne convient ;\n"
        "- pas de tag pour le titre de la série, ses auteurs ou son éditeur ;\n"
        "- renvoie chaque série reçue avec son id, même avec une liste vide si tu ne sais rien en "
        "dire.\n\n"
        f"Tags déjà employés dans la bibliothèque :\n{listing}"
    )


def _first(*values):
    for value in values:
        if value and str(value).strip():
            return str(value).strip()
    return ''


def series_brief(row):
    authors = _first(row['manual_author'], ', '.join(filter(None, [
        row['bedetheque_scenaristes'], row['bedetheque_dessinateurs']])), row['local_author'],
        row['komga_authors'])
    summary = _first(row['manual_summary'], row['local_summary'], row['bedetheque_description'],
                     row['komga_summary'])
    if len(summary) > SUMMARY_MAX_CHARS:
        summary = summary[:SUMMARY_MAX_CHARS].rsplit(' ', 1)[0] + '…'
    years = _first(row['manual_year_start'], row['bedetheque_year_start'], row['local_year'])
    return {
        'id': row['id'],
        'titre': row['title'],
        'auteurs': authors,
        'genre': _first(row['manual_genre'], row['bedetheque_genre'], row['local_genre']),
        'annee': years,
        'origine': row['bedetheque_origin'] or '',
        'resume': summary,
        'tags_actuels': parse_tags(row['tags']),
    }


def series_to_tag(conn, scope, library_id=None):
    query = 'SELECT * FROM series'
    params = []
    if library_id:
        query += ' WHERE library_id = ?'
        params.append(int(library_id))
    query += ' ORDER BY title COLLATE NOCASE'
    rows = conn.execute(query, params).fetchall()
    if scope == 'untagged':
        rows = [row for row in rows if not parse_tags(row['tags'])]
    return rows


def vocabulary(conn):
    counts = tag_counts(conn)
    return [tag for tag, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            if count >= VOCABULARY_MIN_COUNT]


def merge_generated_tags(conn, results, allowed_ids):
    """Ajoute les tags proposés aux tags existants. Renvoie (séries modifiées, tags ajoutés)."""
    changed, added = 0, 0
    for item in results:
        try:
            series_id = int(item.get('id'))
        except (TypeError, ValueError):
            continue
        if series_id not in allowed_ids:
            continue
        row = conn.execute('SELECT tags FROM series WHERE id = ?', (series_id,)).fetchone()
        if not row:
            continue
        current = parse_tags(row['tags'])
        updated = dedupe(current + [clean_tag(tag) for tag in item.get('tags') or []])
        if updated != current:
            conn.execute('UPDATE series SET tags = ? WHERE id = ?',
                         (json.dumps(updated, ensure_ascii=False), series_id))
            changed += 1
            added += len(updated) - len(current)
    conn.commit()
    return changed, added
