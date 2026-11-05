"""IA : configuration, appel du modèle et logique des tags.

Trois fournisseurs au choix (Configuration → IA), chacun avec sa clé API et son modèle :
- Claude (Anthropic), via le SDK officiel « anthropic » ;
- Gemini (Google), via l'API REST generativelanguage.googleapis.com ;
- ChatGPT (OpenAI), via l'API REST api.openai.com (Chat Completions, sortie JSON stricte).

Deux traitements utilisent l'IA :
- création des tags : propose des tags pour des séries (sans tags, ou toutes) à partir du
  titre, des auteurs, du genre et du résumé, en réutilisant le vocabulaire existant ;
  les tags proposés s'ajoutent aux tags existants, rien n'est retiré ;
- normalisation des tags : repère les tags qui veulent dire la même chose
  (« de-gaulle », « charles-de-gaulle », « general de gaulle ») et propose un tag commun.
  Rien n'est modifié tant que l'utilisateur n'a pas validé les regroupements.
"""
import copy
import json
import os
import re
import sqlite3
import time

import requests
from flask import current_app

from encryption import decrypt, encrypt

PROVIDERS = {
    'anthropic': {
        'name': 'Claude (Anthropic)',
        'models': {
            'claude-opus-5-5': 'Claude Opus 5.5',
            'claude-sonnet-5-5': 'Claude Sonnet 5.5',
            'claude-haiku-5-5': 'Claude Haiku 5.5',
        },
        'default_model': 'claude-opus-5-5',
        'key_placeholder': 'sk-ant-...',
        'key_url': 'https://platform.claude.com/settings/keys',
        'dynamic_models': False,
    },
    'gemini': {
        'name': 'Gemini (Google)',
        # Liste de départ : la liste réelle est lue chez Google avec la clé (list_models).
        'models': {
            'gemini-3.5-flash-lite': 'Gemini 3.5 Flash-Lite',
            'gemini-3.5-flash': 'Gemini 3.5 Flash',
            'gemini-3.1-flash-lite': 'Gemini 3.1 Flash-Lite',
        },
        'default_model': 'gemini-3.5-flash-lite',
        'key_placeholder': 'AIza...',
        'key_url': 'https://aistudio.google.com/apikey',
        'dynamic_models': True,
    },
    'openai': {
        'name': 'ChatGPT (OpenAI)',
        # Liste de départ : la liste réelle est lue chez OpenAI avec la clé (list_models).
        'models': {
            'gpt-5-mini': 'GPT-5 mini',
            'gpt-5': 'GPT-5',
            'gpt-5-nano': 'GPT-5 nano',
        },
        'default_model': 'gpt-5-mini',
        'key_placeholder': 'sk-proj-...',
        'key_url': 'https://platform.openai.com/api-keys',
        'dynamic_models': True,
    },
}
DEFAULT_PROVIDER = 'anthropic'
# Modèles Claude qui acceptent le repli côté serveur (fallbacks: "default") quand une
# requête est déclinée par les filtres de sécurité.
_FALLBACK_MODELS = {'claude-opus-5-5', 'claude-sonnet-5-5'}
_MODEL_ID_RE = re.compile(r'^[a-z0-9][a-z0-9.\-]{1,80}$')
GEMINI_API = 'https://generativelanguage.googleapis.com/v1beta'
GEMINI_RETRY_DELAYS = (5, 20)
OPENAI_API = 'https://api.openai.com/v1'
# Modèles OpenAI sans intérêt ici (audio, image, embeddings…), retirés de la liste.
_OPENAI_SKIP = ('audio', 'realtime', 'tts', 'transcribe', 'image', 'embedding', 'search',
                'moderation', 'instruct', 'whisper', 'dall-e', 'codex', 'computer-use')
MASK = '****'

GENERATE_BATCH_SIZE = 20
SUMMARY_MAX_CHARS = 1200
VOCABULARY_MIN_COUNT = 2


class AiError(Exception):
    """Erreur à afficher telle quelle à l'utilisateur."""


# ===== Configuration =====

def default_ai_config():
    return copy.deepcopy(current_app.config['AI_CONFIG'])


def load_ai_config():
    """Configuration IA. Clés déchiffrées dans api_keys_decrypted[fournisseur]."""
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
    cfg['api_keys'] = dict(cfg.get('api_keys') or {})
    cfg['models'] = dict(cfg.get('models') or {})
    # Ancien format (Claude seul) : api_key / model au premier niveau.
    legacy_key = cfg.pop('api_key', '')
    legacy_model = cfg.pop('model', '')
    if legacy_key and not cfg['api_keys'].get('anthropic'):
        cfg['api_keys']['anthropic'] = legacy_key
    if legacy_model and not cfg['models'].get('anthropic'):
        cfg['models']['anthropic'] = legacy_model
    if cfg.get('provider') not in PROVIDERS:
        cfg['provider'] = DEFAULT_PROVIDER
    cfg['api_keys_decrypted'] = {}
    for provider, encrypted in cfg['api_keys'].items():
        if provider in PROVIDERS and encrypted:
            decrypted = decrypt(encrypted)
            if decrypted:
                cfg['api_keys_decrypted'][provider] = decrypted
    for provider, info in PROVIDERS.items():
        if not valid_model(provider, cfg['models'].get(provider)):
            cfg['models'][provider] = info['default_model']
    return cfg


def save_ai_config(cfg):
    path = current_app.config['AI_CONFIG_FILE']
    to_save = {key: value for key, value in cfg.items() if not key.endswith('_decrypted')}
    to_save['api_keys'] = {provider: encrypt(key)
                           for provider, key in (cfg.get('api_keys_decrypted') or {}).items() if key}
    try:
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(to_save, handle, indent=4)
        os.chmod(path, 0o600)
        return True
    except OSError as exc:
        print(f"Erreur sauvegarde config IA : {exc}")
        return False


def valid_model(provider, model):
    info = PROVIDERS.get(provider)
    if not info or not model:
        return False
    if info['dynamic_models']:
        return bool(_MODEL_ID_RE.match(str(model)))
    return model in info['models']


def active_key(cfg):
    return (cfg.get('api_keys_decrypted') or {}).get(cfg.get('provider'), '')


def active_model(cfg):
    provider = cfg.get('provider') or DEFAULT_PROVIDER
    return cfg['models'].get(provider) or PROVIDERS[provider]['default_model']


def public_config(cfg):
    keys = cfg.get('api_keys_decrypted') or {}
    providers = []
    for provider, info in PROVIDERS.items():
        key = keys.get(provider) or ''
        model = cfg['models'].get(provider) or info['default_model']
        models = dict(info['models'])
        models.setdefault(model, model)
        providers.append({
            'id': provider,
            'name': info['name'],
            'model': model,
            'models': [{'id': model_id, 'name': name} for model_id, name in models.items()],
            'dynamic_models': info['dynamic_models'],
            'has_api_key': bool(key),
            'api_key': MASK if key else '',
            'api_key_hint': f'…{key[-4:]}' if len(key) > 8 else '',
            'key_placeholder': info['key_placeholder'],
            'key_url': info['key_url'],
        })
    return {
        'enabled': bool(cfg.get('enabled')),
        'auto_tag_new': bool(cfg.get('auto_tag_new')),
        'provider': cfg.get('provider') or DEFAULT_PROVIDER,
        'providers': providers,
    }


def merge_form(cfg, data):
    """Reporte le formulaire dans cfg. Renvoie un message d'erreur ou None.

    data : enabled, provider, et la clé / le modèle de ce fournisseur (api_key, model)."""
    if 'provider' in data:
        provider = str(data.get('provider') or '')
        if provider not in PROVIDERS:
            return 'Fournisseur d\'IA inconnu'
        cfg['provider'] = provider
    provider = cfg.get('provider') or DEFAULT_PROVIDER
    if 'enabled' in data:
        cfg['enabled'] = bool(data.get('enabled'))
    if 'auto_tag_new' in data:
        cfg['auto_tag_new'] = bool(data.get('auto_tag_new'))
    if 'model' in data:
        model = str(data.get('model') or '').strip()
        if model.startswith('models/'):
            model = model[len('models/'):]
        if not valid_model(provider, model):
            return 'Modèle inconnu'
        cfg['models'][provider] = model
    if 'api_key' in data:
        key = str(data.get('api_key') or '').strip()
        if key != MASK:
            keys = cfg.setdefault('api_keys_decrypted', {})
            if key:
                keys[provider] = key
            else:
                keys.pop(provider, None)
    if cfg.get('enabled') and not active_key(cfg):
        return 'Saisissez une clé API pour activer l\'IA'
    return None


# ===== Claude (Anthropic) =====

def _anthropic_client(cfg):
    import anthropic
    return anthropic.Anthropic(api_key=active_key(cfg), max_retries=3, timeout=900.0)


def _anthropic_error(exc):
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


def _anthropic_test(cfg):
    try:
        model = _anthropic_client(cfg).models.retrieve(active_model(cfg))
    except Exception as exc:  # noqa: BLE001 - message lisible pour l'interface
        raise AiError(_anthropic_error(exc)) from exc
    return getattr(model, 'display_name', None) or active_model(cfg)


def _anthropic_json(cfg, system, user, schema, cache_system, max_tokens):
    model = active_model(cfg)
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
        with _anthropic_client(cfg).beta.messages.stream(**params) as stream:
            message = stream.get_final_message()
    except Exception as exc:  # noqa: BLE001 - message lisible pour l'interface
        raise AiError(_anthropic_error(exc)) from exc
    if message.stop_reason == 'refusal':
        raise AiError('Le modèle a refusé de traiter cette demande')
    if message.stop_reason == 'max_tokens':
        raise AiError('Réponse du modèle tronquée (trop longue)')
    return next((block.text for block in message.content if block.type == 'text'), '')


# ===== Gemini (Google) =====

def gemini_schema(schema):
    """Schéma JSON → responseSchema de Gemini (types en majuscules, sans additionalProperties)."""
    if isinstance(schema, dict):
        converted = {}
        for key, value in schema.items():
            if key == 'additionalProperties':
                continue
            if key == 'type' and isinstance(value, str):
                converted[key] = value.upper()
            else:
                converted[key] = gemini_schema(value)
        return converted
    if isinstance(schema, list):
        return [gemini_schema(item) for item in schema]
    return schema


def _gemini_request(cfg, method, path, **kwargs):
    headers = {'x-goog-api-key': active_key(cfg), 'Content-Type': 'application/json'}
    for attempt in range(len(GEMINI_RETRY_DELAYS) + 1):
        try:
            response = requests.request(method, f'{GEMINI_API}/{path}', headers=headers, **kwargs)
        except requests.RequestException as exc:
            raise AiError('Impossible de joindre l\'API Gemini') from exc
        # Surcharge ou quota par minute : on réessaie un peu plus tard.
        if response.status_code in (429, 500, 503) and attempt < len(GEMINI_RETRY_DELAYS):
            time.sleep(GEMINI_RETRY_DELAYS[attempt])
            continue
        break
    if response.ok:
        return response.json()
    try:
        error = response.json().get('error') or {}
    except ValueError:
        error = {}
    message = error.get('message') or response.text[:200]
    reasons = json.dumps(error.get('details') or [])
    if response.status_code == 400 and 'API_KEY_INVALID' in reasons:
        raise AiError('Clé API refusée par Google')
    if response.status_code in (401, 403):
        raise AiError('Clé API refusée par Google, ou sans accès à ce modèle')
    if response.status_code == 404:
        raise AiError('Modèle Gemini introuvable pour cette clé API')
    if response.status_code == 429:
        raise AiError('Quota Gemini atteint, réessayez plus tard')
    raise AiError(f'Erreur de l\'API Gemini ({response.status_code}) : {message}')


def _gemini_test(cfg):
    data = _gemini_request(cfg, 'GET', f'models/{active_model(cfg)}', timeout=30)
    return data.get('displayName') or active_model(cfg)


def list_models(cfg):
    """Modèles disponibles pour le fournisseur choisi (Gemini : lus chez Google)."""
    provider = cfg.get('provider') or DEFAULT_PROVIDER
    info = PROVIDERS[provider]
    if not info['dynamic_models']:
        return [{'id': model_id, 'name': name} for model_id, name in info['models'].items()]
    if not active_key(cfg):
        raise AiError('Saisissez une clé API pour lire la liste des modèles')
    if provider == 'openai':
        return _openai_models(cfg)
    models = []
    token = None
    for _ in range(10):
        params = {'pageSize': 1000}
        if token:
            params['pageToken'] = token
        data = _gemini_request(cfg, 'GET', 'models', params=params, timeout=30)
        for item in data.get('models') or []:
            model_id = str(item.get('name') or '').removeprefix('models/')
            if (model_id.startswith('gemini')
                    and 'generateContent' in (item.get('supportedGenerationMethods') or [])):
                models.append({'id': model_id, 'name': item.get('displayName') or model_id})
        token = data.get('nextPageToken')
        if not token:
            break
    models.sort(key=lambda item: item['id'], reverse=True)
    return models


def _gemini_json(cfg, system, user, schema, max_tokens):
    body = {
        'systemInstruction': {'parts': [{'text': system}]},
        'contents': [{'role': 'user', 'parts': [{'text': user}]}],
        'generationConfig': {
            'responseMimeType': 'application/json',
            'responseSchema': gemini_schema(schema),
            'maxOutputTokens': min(max_tokens, 65536),
        },
    }
    data = _gemini_request(cfg, 'POST', f'models/{active_model(cfg)}:generateContent',
                           json=body, timeout=900)
    block = (data.get('promptFeedback') or {}).get('blockReason')
    if block:
        raise AiError(f'Gemini a refusé de traiter cette demande ({block})')
    candidates = data.get('candidates') or []
    if not candidates:
        raise AiError('Gemini n\'a renvoyé aucune réponse')
    candidate = candidates[0]
    reason = candidate.get('finishReason')
    if reason == 'MAX_TOKENS':
        raise AiError('Réponse du modèle tronquée (trop longue)')
    if reason in ('SAFETY', 'RECITATION', 'PROHIBITED_CONTENT', 'BLOCKLIST', 'SPII'):
        raise AiError(f'Gemini a refusé de traiter cette demande ({reason})')
    parts = (candidate.get('content') or {}).get('parts') or []
    return ''.join(part.get('text', '') for part in parts if not part.get('thought'))


# ===== ChatGPT (OpenAI) =====

def _openai_request(cfg, method, path, **kwargs):
    headers = {'Authorization': f'Bearer {active_key(cfg)}', 'Content-Type': 'application/json'}
    for attempt in range(len(GEMINI_RETRY_DELAYS) + 1):
        try:
            response = requests.request(method, f'{OPENAI_API}/{path}', headers=headers, **kwargs)
        except requests.RequestException as exc:
            raise AiError('Impossible de joindre l\'API OpenAI') from exc
        try:
            error = (response.json() or {}).get('error') or {} if not response.ok else {}
        except ValueError:
            error = {}
        # Limite par minute ou surcharge : on réessaie (pas quand le crédit est épuisé).
        retry = response.status_code in (500, 502, 503) or (
            response.status_code == 429 and error.get('code') != 'insufficient_quota')
        if retry and attempt < len(GEMINI_RETRY_DELAYS):
            time.sleep(GEMINI_RETRY_DELAYS[attempt])
            continue
        break
    if response.ok:
        return response.json()
    message = error.get('message') or response.text[:200]
    if response.status_code == 401:
        raise AiError('Clé API refusée par OpenAI')
    if response.status_code == 403:
        raise AiError('Clé API OpenAI sans accès à ce modèle')
    if response.status_code == 404:
        raise AiError('Modèle OpenAI introuvable pour cette clé API')
    if response.status_code == 429:
        if error.get('code') == 'insufficient_quota':
            raise AiError('Crédit OpenAI épuisé : ajoutez du crédit sur platform.openai.com')
        raise AiError('Limite de requêtes OpenAI atteinte, réessayez plus tard')
    raise AiError(f'Erreur de l\'API OpenAI ({response.status_code}) : {message}')


def _openai_test(cfg):
    data = _openai_request(cfg, 'GET', f'models/{active_model(cfg)}', timeout=30)
    return data.get('id') or active_model(cfg)


def _openai_models(cfg):
    data = _openai_request(cfg, 'GET', 'models', timeout=30)
    models = []
    for item in data.get('data') or []:
        model_id = str(item.get('id') or '')
        if not (model_id.startswith('gpt-') or model_id[:2] in ('o1', 'o3', 'o4')):
            continue
        if any(word in model_id for word in _OPENAI_SKIP) or not _MODEL_ID_RE.match(model_id):
            continue
        models.append({'id': model_id, 'name': model_id})
    models.sort(key=lambda item: item['id'], reverse=True)
    return models


def _openai_json(cfg, system, user, schema, max_tokens):
    body = {
        'model': active_model(cfg),
        'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
        'response_format': {
            'type': 'json_schema',
            'json_schema': {'name': 'reponse', 'strict': True, 'schema': schema},
        },
        'max_completion_tokens': min(max_tokens, 100000),
    }
    data = _openai_request(cfg, 'POST', 'chat/completions', json=body, timeout=900)
    choices = data.get('choices') or []
    if not choices:
        raise AiError('OpenAI n\'a renvoyé aucune réponse')
    choice = choices[0]
    message = choice.get('message') or {}
    if message.get('refusal'):
        raise AiError(f'ChatGPT a refusé de traiter cette demande : {message["refusal"]}')
    if choice.get('finish_reason') == 'length':
        raise AiError('Réponse du modèle tronquée (trop longue)')
    if choice.get('finish_reason') == 'content_filter':
        raise AiError('ChatGPT a refusé de traiter cette demande (filtre de contenu)')
    return message.get('content') or ''


# ===== Appel commun =====

def _require_key(cfg):
    if not active_key(cfg):
        raise AiError('Aucune clé API : renseignez-la dans Configuration → IA')


def test_connection(cfg):
    """Vérifie la clé et l'accès au modèle choisi, sans consommer de tokens."""
    _require_key(cfg)
    if cfg.get('provider') == 'gemini':
        return _gemini_test(cfg)
    if cfg.get('provider') == 'openai':
        return _openai_test(cfg)
    return _anthropic_test(cfg)


def call_json(cfg, system, user, schema, cache_system=False, max_tokens=64000):
    """Envoie une requête au modèle et renvoie la réponse JSON (conforme au schéma)."""
    _require_key(cfg)
    if cfg.get('provider') == 'gemini':
        text = _gemini_json(cfg, system, user, schema, max_tokens)
    elif cfg.get('provider') == 'openai':
        text = _openai_json(cfg, system, user, schema, max_tokens)
    else:
        text = _anthropic_json(cfg, system, user, schema, cache_system, max_tokens)
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


def count_series(conn, library_id=None):
    """Nombre de séries, et de séries sans tags (choix « Créer des tags »)."""
    query, params = 'SELECT tags FROM series', []
    if library_id:
        query += ' WHERE library_id = ?'
        params.append(int(library_id))
    rows = conn.execute(query, params).fetchall()
    return {'total': len(rows), 'untagged': sum(1 for row in rows if not parse_tags(row['tags']))}


def merge_generated_tags(conn, results, allowed_ids, changes=None):
    """Ajoute les tags proposés aux tags existants. Renvoie (séries modifiées, tags ajoutés).
    Si changes est une liste, y ajoute {series_id, title, added} pour l'historique."""
    changed, added = 0, 0
    for item in results:
        try:
            series_id = int(item.get('id'))
        except (TypeError, ValueError):
            continue
        if series_id not in allowed_ids:
            continue
        row = conn.execute('SELECT title, tags FROM series WHERE id = ?', (series_id,)).fetchone()
        if not row:
            continue
        current = parse_tags(row['tags'])
        updated = dedupe(current + [clean_tag(tag) for tag in item.get('tags') or []])
        if updated != current:
            conn.execute('UPDATE series SET tags = ? WHERE id = ?',
                         (json.dumps(updated, ensure_ascii=False), series_id))
            changed += 1
            added += len(updated) - len(current)
            if changes is not None:
                changes.append({'series_id': series_id, 'title': row['title'],
                                'added': [tag for tag in updated if tag not in current]})
    conn.commit()
    return changed, added
