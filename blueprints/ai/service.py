"""IA : configuration, appel du modèle et logique des tags.

Sept fournisseurs au choix (Configuration → IA), chacun avec sa clé API et son modèle :
- aiautotagging.com (par défaut) : service gratuit, sans clé ni compte, qui renvoie des tags
  pour un texte (une requête par série, 10 par minute, 500 par jour). Il ne sait faire que
  ça : la normalisation des tags demande un des modèles de langage ci-dessous ;
- Claude (Anthropic), via le SDK officiel « anthropic » ;
- Gemini (Google), via l'API REST generativelanguage.googleapis.com ;
- ChatGPT (OpenAI), via l'API REST api.openai.com (Chat Completions, sortie JSON stricte) ;
- OpenRouter, une seule clé pour des centaines de modèles (Claude, GPT, Gemini, DeepSeek,
  Mistral…), via son API compatible OpenAI. Modèles à sortie JSON structurée, plus les
  modèles gratuits (JSON demandé dans la consigne quand le modèle ne sait pas faire mieux) ;
- Groq : modèles ouverts (GPT-OSS, Llama, Qwen…) très rapides, offre gratuite avec une clé,
  via son API compatible OpenAI ;
- LLM local : un serveur compatible OpenAI sur votre réseau (Ollama, LM Studio, llama.cpp,
  vLLM…), à son adresse (…/v1), clé facultative.

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
import threading
import time
import unicodedata

import requests
from flask import current_app

from encryption import decrypt, encrypt

# key : 'required' (clé API obligatoire), 'optional' (LLM local) ou 'none' (aiautotagging.com).
PROVIDERS = {
    'aiautotagging': {
        'name': 'aiautotagging.com (gratuit, sans clé)',
        'models': {'auto': 'Modèle du service (Llama 3 8B)'},
        'default_model': 'auto',
        'key_placeholder': '',
        'key_url': 'https://aiautotagging.com',
        'dynamic_models': False,
        'key': 'none',
        'llm': False,
    },
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
    'openrouter': {
        'name': 'OpenRouter',
        # Liste de départ : la liste réelle est lue chez OpenRouter (list_models).
        'models': {
            'google/gemini-3.5-flash-lite': 'Google: Gemini 3.5 Flash Lite',
            'openai/gpt-5-mini': 'OpenAI: GPT-5 Mini',
            'anthropic/claude-haiku-5.5': 'Anthropic: Claude Haiku 5.5',
            'deepseek/deepseek-v4.1-flash': 'DeepSeek: DeepSeek V4.1 Flash',
            'openrouter/free': 'OpenRouter: Free (gratuit, modèle choisi par OpenRouter)',
        },
        'default_model': 'google/gemini-3.5-flash-lite',
        'key_placeholder': 'sk-or-v1-...',
        'key_url': 'https://openrouter.ai/settings/keys',
        'dynamic_models': True,
    },
    'groq': {
        'name': 'Groq',
        # Liste de départ : la liste réelle est lue chez Groq avec la clé (list_models).
        'models': {
            'openai/gpt-oss-20b': 'GPT-OSS 20B',
            'openai/gpt-oss-120b': 'GPT-OSS 120B',
            'llama-3.3-70b-versatile': 'Llama 3.3 70B',
            'meta-llama/llama-4-scout-17b-16e-instruct': 'Llama 4 Scout',
        },
        'default_model': 'openai/gpt-oss-20b',
        'key_placeholder': 'gsk_...',
        'key_url': 'https://console.groq.com/keys',
        'dynamic_models': True,
    },
    'local': {
        'name': 'LLM local (Ollama, LM Studio…)',
        # Liste de départ : la liste réelle est lue sur le serveur (list_models).
        'models': {
            'qwen3:8b': 'qwen3:8b',
            'llama3.1:8b': 'llama3.1:8b',
            'mistral-nemo': 'mistral-nemo',
        },
        'default_model': 'qwen3:8b',
        'key_placeholder': 'facultative',
        'key_url': 'https://ollama.com',
        'dynamic_models': True,
        'key': 'optional',
    },
}
DEFAULT_PROVIDER = 'aiautotagging'
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
OPENROUTER_API = 'https://openrouter.ai/api/v1'
# Identifiants OpenRouter : « fournisseur/modèle », avec un suffixe éventuel (« :free »).
_OPENROUTER_MODEL_RE = re.compile(r'^[a-z0-9][a-z0-9._\-]{0,40}/[a-z0-9][a-z0-9._\-]{0,100}(:[a-z0-9\-]{1,20})?$')
# Modèles gratuits sans intérêt pour des tags (modération, code).
_OPENROUTER_SKIP = ('content-safety', 'guard', '-code')
OPENROUTER_CATALOG_TTL = 3600
AIAUTOTAGGING_API = 'https://aiautotagging.com/api/tag/text'
# 10 requêtes par minute et par adresse IP : une toutes les 6,5 s.
AIAUTOTAGGING_INTERVAL = 6.5
AIAUTOTAGGING_RETRY_DELAYS = (30, 65)
AIAUTOTAGGING_MAX_TAGS = 8
AIAUTOTAGGING_MIN_CONFIDENCE = 0.3
AIAUTOTAGGING_CONTEXT = (
    "Bibliothèque de bandes dessinées francophone. Tags courts en français, en minuscules : "
    "genre, thèmes, époque, lieux, personnages historiques ou événements réels, public, ton. "
    "Pas de tag pour le titre, les auteurs ou l'éditeur.")
_aiautotagging_lock = threading.Lock()
_aiautotagging_last = [0.0]
# Ollama ; « host.docker.internal » est relié à l'hôte dans docker-compose.yml.
LOCAL_DEFAULT_URL = 'http://host.docker.internal:11434/v1'
_LOCAL_URL_RE = re.compile(r'^https?://[^\s/?#]+(/[^\s?#]*)?$')
# Noms Ollama (« qwen3:8b »), LM Studio (« qwen/qwen3-8b »), fichiers GGUF…
_LOCAL_MODEL_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,150}$')
LOCAL_BATCH_SIZE = 5
GROQ_API = 'https://api.groq.com/openai/v1'
# Modèles Groq sans intérêt ici (audio, modération, agents).
_GROQ_SKIP = ('whisper', 'tts', 'guard', 'playai', 'orpheus', 'compound', 'safeguard')
# Sortie JSON stricte (schéma garanti) ; les autres modèles suivent le schéma au mieux.
_GROQ_STRICT = ('openai/gpt-oss',)
GROQ_MAX_WAIT = 60
# Plafond de départ des tokens de réponse, réduit ensuite selon le modèle et la limite par
# minute de l'offre (les tokens de réponse demandés comptent dans cette limite).
GROQ_MAX_COMPLETION = 16384
GROQ_MIN_COMPLETION = 1500
# Normalisation avec Groq : liste des tags envoyée par morceaux (offre gratuite : 8 000 tokens
# par minute, réponse comprise).
GROQ_NORMALIZE_CHUNK_CHARS = 7000
_GROQ_MAX_RE = re.compile(r'max_completion_tokens`? must be less than or equal to `?(\d+)')
_GROQ_TPM_RE = re.compile(r'\(I?TPM\): Limit (\d+), Requested (\d+)')
_openrouter_catalog_cache = {'at': 0.0, 'models': None}
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
    cfg['base_urls'] = dict(cfg.get('base_urls') or {})
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
    if provider == 'openrouter':
        return bool(_OPENROUTER_MODEL_RE.match(str(model)))
    if provider in ('local', 'groq'):
        return bool(_LOCAL_MODEL_RE.match(str(model)))
    if info['dynamic_models']:
        return bool(_MODEL_ID_RE.match(str(model)))
    return model in info['models']


def active_key(cfg):
    return (cfg.get('api_keys_decrypted') or {}).get(cfg.get('provider'), '')


def key_mode(provider):
    return (PROVIDERS.get(provider) or {}).get('key', 'required')


def provider_ready(cfg):
    """Le fournisseur choisi peut être appelé (clé renseignée quand il en faut une)."""
    return key_mode(cfg.get('provider') or DEFAULT_PROVIDER) != 'required' or bool(active_key(cfg))


def supports_llm(cfg):
    """Modèle de langage capable de suivre une consigne (la normalisation en a besoin)."""
    return (PROVIDERS.get(cfg.get('provider') or DEFAULT_PROVIDER) or {}).get('llm', True)


def generate_batch_size(cfg):
    # Groq gratuit : quelques milliers de tokens par minute, d'où des lots courts.
    if cfg.get('provider') in ('aiautotagging', 'local', 'groq'):
        return LOCAL_BATCH_SIZE
    return GENERATE_BATCH_SIZE


def local_base_url(cfg):
    return (cfg.get('base_urls') or {}).get('local') or LOCAL_DEFAULT_URL


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
            'key': key_mode(provider),
            'llm': info.get('llm', True),
        })
        if provider == 'local':
            providers[-1]['base_url'] = local_base_url(cfg)
            providers[-1]['default_base_url'] = LOCAL_DEFAULT_URL
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
    if 'api_key' in data and key_mode(provider) != 'none':
        key = str(data.get('api_key') or '').strip()
        if key != MASK:
            keys = cfg.setdefault('api_keys_decrypted', {})
            if key:
                keys[provider] = key
            else:
                keys.pop(provider, None)
    if 'base_url' in data and provider == 'local':
        url = str(data.get('base_url') or '').strip().rstrip('/')
        urls = cfg.setdefault('base_urls', {})
        if not url:
            urls.pop('local', None)
        elif not _LOCAL_URL_RE.match(url):
            return 'Adresse du LLM local invalide (exemple : http://192.168.1.10:11434/v1)'
        else:
            urls['local'] = url
    if cfg.get('enabled') and not provider_ready(cfg):
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
    if provider == 'local':
        return _local_models(cfg)
    if not active_key(cfg):
        raise AiError('Saisissez une clé API pour lire la liste des modèles')
    if provider == 'openai':
        return _openai_models(cfg)
    if provider == 'openrouter':
        return _openrouter_models(cfg)
    if provider == 'groq':
        return _groq_models(cfg)
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


# ===== OpenRouter =====

def _openrouter_request(cfg, method, path, **kwargs):
    headers = {'Authorization': f'Bearer {active_key(cfg)}', 'Content-Type': 'application/json',
               'X-Title': 'Bullarr'}
    for attempt in range(len(GEMINI_RETRY_DELAYS) + 1):
        try:
            response = requests.request(method, f'{OPENROUTER_API}/{path}', headers=headers, **kwargs)
        except requests.RequestException as exc:
            raise AiError('Impossible de joindre OpenRouter') from exc
        # Limite par minute ou modèle surchargé : on réessaie (pas quand le crédit manque).
        if response.status_code in (429, 500, 502, 503) and attempt < len(GEMINI_RETRY_DELAYS):
            time.sleep(GEMINI_RETRY_DELAYS[attempt])
            continue
        break
    if response.ok:
        return response.json()
    try:
        error = (response.json() or {}).get('error') or {}
    except ValueError:
        error = {}
    message = error.get('message') or response.text[:200]
    if response.status_code == 401:
        raise AiError('Clé API refusée par OpenRouter')
    if response.status_code == 402:
        raise AiError('Crédit OpenRouter insuffisant : ajoutez du crédit sur openrouter.ai')
    if response.status_code == 403:
        raise AiError(f'OpenRouter a refusé cette demande (modération) : {message}')
    if response.status_code == 404:
        raise AiError('Modèle OpenRouter introuvable ou sans sortie JSON structurée')
    if response.status_code == 429:
        raise AiError('Limite de requêtes OpenRouter atteinte, réessayez plus tard')
    raise AiError(f'Erreur de l\'API OpenRouter ({response.status_code}) : {message}')


def _openrouter_is_free(item):
    pricing = item.get('pricing') or {}
    return str(item.get('id') or '').endswith(':free') or (
        str(pricing.get('prompt')) in ('0', '0.0') and str(pricing.get('completion')) in ('0', '0.0'))


def _openrouter_catalog(cfg, refresh=False):
    """Modèles texte d'OpenRouter utilisables ici (hors « :batch ») : ceux à sortie JSON
    structurée, plus les gratuits. Chaque entrée dit comment obtenir du JSON :
    json_mode 'schema' (schéma strict), 'object' (JSON libre) ou 'prompt' (consigne seule)."""
    cache = _openrouter_catalog_cache
    if not refresh and cache['models'] is not None and time.time() - cache['at'] < OPENROUTER_CATALOG_TTL:
        return cache['models']
    data = _openrouter_request(cfg, 'GET', 'models', timeout=30)
    models = []
    for item in data.get('data') or []:
        model_id = str(item.get('id') or '')
        if model_id.endswith(':batch') or not _OPENROUTER_MODEL_RE.match(model_id):
            continue
        outputs = ((item.get('architecture') or {}).get('output_modalities')) or ['text']
        if outputs != ['text']:
            continue
        params = item.get('supported_parameters') or []
        free = _openrouter_is_free(item)
        if 'structured_outputs' in params:
            mode = 'schema'
        elif free and not any(word in model_id for word in _OPENROUTER_SKIP):
            mode = 'object' if 'response_format' in params else 'prompt'
        else:
            continue
        name = item.get('name') or model_id
        if free:
            name = re.sub(r'\s*\(free\)\s*', ' ', name).strip() + ' (gratuit)'
        models.append({'id': model_id, 'name': name, 'free': free, 'json_mode': mode})
    cache.update(at=time.time(), models=models)
    return models


def _openrouter_models(cfg):
    # Gratuits en tête, puis par nom.
    models = sorted(_openrouter_catalog(cfg, refresh=True),
                    key=lambda item: (not item['free'], item['name'].lower()))
    return [{'id': item['id'], 'name': item['name']} for item in models]


def _openrouter_test(cfg):
    _openrouter_request(cfg, 'GET', 'key', timeout=30)  # vérifie la clé
    item = _openrouter_model_info(cfg, refresh=True)
    if not item:
        raise AiError('Modèle OpenRouter introuvable ou sans sortie JSON structurée')
    return item['name']


def _openrouter_model_info(cfg, refresh=False):
    model = active_model(cfg)
    for item in _openrouter_catalog(cfg, refresh=refresh):
        if item['id'] == model:
            return item
    return None


def _openrouter_json(cfg, system, user, schema, max_tokens):
    # Mode JSON du modèle : liste en cache ; si OpenRouter n'est pas joignable, schéma strict.
    try:
        info = _openrouter_model_info(cfg) or {}
    except AiError:
        info = {}
    mode = info.get('json_mode', 'schema')
    if mode != 'schema':
        # Modèle sans sortie structurée (souvent gratuit) : le schéma va dans la consigne.
        system = (f'{system}\n\nRéponds uniquement par un objet JSON valide, sans texte autour ni '
                  f'bloc de code, conforme à ce schéma JSON :\n{json.dumps(schema, ensure_ascii=False)}')
    body = {
        'model': active_model(cfg),
        'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
        'max_tokens': min(max_tokens, 64000),
    }
    if mode == 'schema':
        body['response_format'] = {
            'type': 'json_schema',
            'json_schema': {'name': 'reponse', 'strict': True, 'schema': schema},
        }
    elif mode == 'object':
        body['response_format'] = {'type': 'json_object'}
    if mode != 'prompt':
        # Seulement les hébergeurs qui respectent response_format.
        body['provider'] = {'require_parameters': True}
    data = _openrouter_request(cfg, 'POST', 'chat/completions', json=body, timeout=900)
    if data.get('error'):
        raise AiError(f'Erreur OpenRouter : {(data["error"] or {}).get("message") or data["error"]}')
    choices = data.get('choices') or []
    if not choices:
        raise AiError('OpenRouter n\'a renvoyé aucune réponse')
    choice = choices[0]
    if choice.get('error'):
        raise AiError(f'Erreur du modèle via OpenRouter : {(choice["error"] or {}).get("message") or choice["error"]}')
    message = choice.get('message') or {}
    if message.get('refusal'):
        raise AiError(f'Le modèle a refusé de traiter cette demande : {message["refusal"]}')
    if choice.get('finish_reason') == 'length':
        raise AiError('Réponse du modèle tronquée (trop longue)')
    if choice.get('finish_reason') == 'content_filter':
        raise AiError('Le modèle a refusé de traiter cette demande (filtre de contenu)')
    return message.get('content') or ''


# ===== aiautotagging.com =====

def _plain(value):
    """Forme de comparaison des tags : sans accents ni tirets."""
    text = unicodedata.normalize('NFKD', clean_tag(value))
    return re.sub(r'[^a-z0-9]', '', ''.join(c for c in text if not unicodedata.combining(c)))


def _aiautotagging_text(brief):
    parts = [f"Bande dessinée : {brief.get('titre') or ''}"]
    for label, key in (('Auteurs', 'auteurs'), ('Genre', 'genre'), ('Année', 'annee'),
                       ('Origine', 'origine'), ('Résumé', 'resume')):
        if brief.get(key):
            parts.append(f'{label} : {brief[key]}')
    return '\n'.join(parts)[:10000]


def _aiautotagging_error(response):
    try:
        data = response.json() or {}
    except ValueError:
        data = {}
    error = data.get('error') if isinstance(data, dict) else None
    if isinstance(error, dict):
        return str(error.get('message') or error.get('code') or '')
    return str(error or data.get('message') or response.text[:200] if isinstance(data, dict) else '')


def _aiautotagging_call(text, max_tags=AIAUTOTAGGING_MAX_TAGS):
    """Tags proposés par aiautotagging.com pour un texte : [(libellé, confiance ou None)]."""
    body = {'text': text, 'options': {
        'maxTags': max_tags, 'language': 'fr', 'includeConfidence': True,
        'additionalContext': AIAUTOTAGGING_CONTEXT[:500]}}
    # Une requête à la fois, espacées, pour rester sous la limite par minute.
    with _aiautotagging_lock:
        for attempt in range(len(AIAUTOTAGGING_RETRY_DELAYS) + 1):
            wait = _aiautotagging_last[0] + AIAUTOTAGGING_INTERVAL - time.time()
            if wait > 0:
                time.sleep(wait)
            try:
                response = requests.request('POST', AIAUTOTAGGING_API, json=body, timeout=60,
                                            headers={'X-Client-Id': 'bullarr'})
            except requests.RequestException as exc:
                raise AiError('Impossible de joindre aiautotagging.com') from exc
            finally:
                _aiautotagging_last[0] = time.time()
            if response.status_code in (429, 500, 502, 503) and attempt < len(AIAUTOTAGGING_RETRY_DELAYS):
                time.sleep(AIAUTOTAGGING_RETRY_DELAYS[attempt])
                continue
            break
    if response.status_code == 429:
        raise AiError('Limite de aiautotagging.com atteinte (10 requêtes par minute, 100 par heure, '
                      '500 par jour) : réessayez plus tard ou choisissez un autre fournisseur')
    if response.status_code == 413:
        raise AiError('Texte trop long pour aiautotagging.com')
    if not response.ok:
        raise AiError(f'Erreur de aiautotagging.com ({response.status_code}) : {_aiautotagging_error(response)}')
    try:
        data = response.json() or {}
    except ValueError as exc:
        raise AiError('Réponse illisible de aiautotagging.com') from exc
    tags = []
    for item in data.get('tags') or []:
        if isinstance(item, dict):
            label, confidence = item.get('label') or item.get('tag') or item.get('name'), item.get('confidence')
        else:
            label, confidence = item, None
        if label:
            tags.append((str(label), confidence if isinstance(confidence, (int, float)) else None))
    return tags


def aiautotag_series(briefs, known_tags):
    """Tags des séries via aiautotagging.com, au format de GENERATE_SCHEMA ({'series': […]}).
    Un tag qui ne diffère d'un tag existant que par les accents ou les tirets prend sa forme."""
    known = {}
    for tag in known_tags:
        known.setdefault(_plain(tag), tag)
    results = []
    for brief in briefs:
        names = [brief.get('titre') or ''] + re.split(r'[,;&/]', brief.get('auteurs') or '')
        excluded = {_plain(name) for name in names if name.strip()}
        tags = []
        for label, confidence in _aiautotagging_call(_aiautotagging_text(brief)):
            if confidence is not None and confidence < AIAUTOTAGGING_MIN_CONFIDENCE:
                continue
            plain = _plain(label)
            if not plain or plain in excluded:
                continue
            tag = known.get(plain) or clean_tag(label)
            if tag not in tags:
                tags.append(tag)
        results.append({'id': brief.get('id'), 'tags': tags})
    return {'series': results}


def _aiautotagging_test():
    tags = _aiautotagging_call('Bande dessinée : une aventure de pirates dans les Caraïbes au XVIIe siècle, '
                               'avec abordages, trésors et humour.', max_tags=3)
    return f'aiautotagging.com ({", ".join(label for label, _ in tags) or "aucun tag"})'


# ===== LLM local (API compatible OpenAI) =====

class _LocalBadRequest(AiError):
    """Requête refusée (400) : souvent response_format non pris en charge."""


def _local_request(cfg, method, path, **kwargs):
    base = local_base_url(cfg)
    headers = {'Content-Type': 'application/json'}
    if active_key(cfg):
        headers['Authorization'] = f'Bearer {active_key(cfg)}'
    try:
        response = requests.request(method, f'{base}/{path}', headers=headers, **kwargs)
    except requests.Timeout as exc:
        raise AiError(f'Le LLM local ({base}) n\'a pas répondu à temps') from exc
    except requests.RequestException as exc:
        raise AiError(f'Impossible de joindre le LLM local à {base} : vérifiez qu\'il tourne et qu\'il '
                      'écoute sur une adresse joignable depuis Bullarr (pas seulement 127.0.0.1 '
                      'quand Bullarr tourne dans Docker)') from exc
    try:
        data = response.json()
    except ValueError:
        data = None
    if response.ok:
        if not isinstance(data, dict):
            raise AiError('Réponse illisible du LLM local : l\'adresse pointe-t-elle vers une API '
                          'compatible OpenAI (elle se termine en général par /v1) ?')
        return data
    error = (data or {}).get('error') if isinstance(data, dict) else None
    message = (error.get('message') if isinstance(error, dict) else error) or response.text[:200]
    if response.status_code in (401, 403):
        raise AiError('Clé API refusée par le LLM local')
    if response.status_code == 404:
        raise AiError('Adresse ou modèle introuvable sur le LLM local : vérifiez l\'adresse (elle se '
                      f'termine en général par /v1) et le nom du modèle ({message})')
    if response.status_code == 400:
        raise _LocalBadRequest(f'Requête refusée par le LLM local : {message}')
    raise AiError(f'Erreur du LLM local ({response.status_code}) : {message}')


def _local_models(cfg):
    data = _local_request(cfg, 'GET', 'models', timeout=30)
    models = []
    for item in data.get('data') or []:
        model_id = str((item or {}).get('id') or '')
        if model_id and 'embed' not in model_id.lower() and _LOCAL_MODEL_RE.match(model_id):
            models.append({'id': model_id, 'name': model_id})
    models.sort(key=lambda item: item['id'].lower())
    return models


def _local_test(cfg):
    models = [item['id'] for item in _local_models(cfg)]
    model = active_model(cfg)
    if models and model not in models:
        raise AiError(f'Modèle « {model} » absent du LLM local. Modèles disponibles : {", ".join(models[:15])}')
    return f'{model} sur {local_base_url(cfg)}'


def _local_json(cfg, system, user, schema, max_tokens):
    body = {
        'model': active_model(cfg),
        'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
        'max_tokens': min(max_tokens, 16000),
        'temperature': 0.2,
        'stream': False,
        'response_format': {
            'type': 'json_schema',
            'json_schema': {'name': 'reponse', 'strict': True, 'schema': schema},
        },
    }
    try:
        data = _local_request(cfg, 'POST', 'chat/completions', json=body, timeout=1800)
    except _LocalBadRequest:
        # Serveur sans sortie structurée : le schéma va dans la consigne.
        prompt = (f'{system}\n\nRéponds uniquement par un objet JSON valide, sans texte autour ni bloc '
                  f'de code, conforme à ce schéma JSON :\n{json.dumps(schema, ensure_ascii=False)}')
        body = {key: value for key, value in body.items() if key != 'response_format'}
        body['messages'] = [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': user}]
        data = _local_request(cfg, 'POST', 'chat/completions', json=body, timeout=1800)
    choices = data.get('choices') or []
    if not choices:
        raise AiError('Le LLM local n\'a renvoyé aucune réponse')
    choice = choices[0]
    if choice.get('finish_reason') == 'length':
        raise AiError('Réponse du LLM local tronquée : augmentez la taille du contexte du modèle '
                      '(num_ctx dans Ollama, « Context Length » dans LM Studio)')
    return (choice.get('message') or {}).get('content') or ''


# ===== Groq (API compatible OpenAI) =====

class _GroqBadRequest(AiError):
    """Requête refusée (400) : souvent un JSON non conforme au schéma demandé."""

    def __init__(self, message, detail=''):
        super().__init__(message)
        self.detail = detail


class _GroqTooLarge(AiError):
    """Requête plus grosse que la limite de tokens par minute de l'offre (413)."""

    def __init__(self, message, limit=0, requested=0):
        super().__init__(message)
        self.limit, self.requested = limit, requested


def _groq_request(cfg, method, path, **kwargs):
    headers = {'Authorization': f'Bearer {active_key(cfg)}', 'Content-Type': 'application/json'}
    for attempt in range(len(GEMINI_RETRY_DELAYS) + 1):
        try:
            response = requests.request(method, f'{GROQ_API}/{path}', headers=headers, **kwargs)
        except requests.RequestException as exc:
            raise AiError('Impossible de joindre l\'API Groq') from exc
        if response.status_code in (429, 500, 502, 503) and attempt < len(GEMINI_RETRY_DELAYS):
            # Limite par minute de l'offre gratuite : Groq dit combien attendre.
            try:
                wait = float(response.headers.get('retry-after') or 0)
            except (TypeError, ValueError, AttributeError):
                wait = 0
            time.sleep(min(max(wait, GEMINI_RETRY_DELAYS[attempt]), GROQ_MAX_WAIT))
            continue
        break
    try:
        data = response.json()
    except ValueError:
        data = None
    if response.ok and isinstance(data, dict):
        return data
    error = (data or {}).get('error') if isinstance(data, dict) else None
    error = error if isinstance(error, dict) else {'message': error}
    message = error.get('message') or response.text[:200]
    if response.status_code == 401:
        raise AiError('Clé API refusée par Groq')
    if response.status_code == 403:
        raise AiError('Clé API Groq sans accès à ce modèle')
    if response.status_code == 404:
        raise AiError('Modèle Groq introuvable : ↻ relit la liste des modèles disponibles')
    if response.status_code == 413:
        match = _GROQ_TPM_RE.search(message)
        limit, requested = (int(match.group(1)), int(match.group(2))) if match else (0, 0)
        detail = f' ({requested} tokens demandés, limite {limit} par minute)' if match else ''
        raise _GroqTooLarge(f'Requête trop grosse pour la limite de votre offre Groq{detail}',
                            limit, requested)
    if response.status_code == 429:
        raise AiError('Limite de requêtes Groq atteinte (offre gratuite : par minute et par jour), '
                      'réessayez plus tard')
    if response.status_code == 400:
        raise _GroqBadRequest(f'Requête refusée par Groq : {message}', message)
    raise AiError(f'Erreur de l\'API Groq ({response.status_code}) : {message}')


def _groq_models(cfg):
    data = _groq_request(cfg, 'GET', 'models', timeout=30)
    models = []
    for item in data.get('data') or []:
        model_id = str((item or {}).get('id') or '')
        if not model_id or item.get('active') is False or not _LOCAL_MODEL_RE.match(model_id):
            continue
        if any(word in model_id.lower() for word in _GROQ_SKIP):
            continue
        models.append({'id': model_id, 'name': model_id})
    models.sort(key=lambda item: item['id'])
    return models


def _groq_test(cfg):
    data = _groq_request(cfg, 'GET', f'models/{active_model(cfg)}', timeout=30)
    return data.get('id') or active_model(cfg)


def _groq_json(cfg, system, user, schema, max_tokens):
    model = active_model(cfg)
    body = {
        'model': model,
        'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
        'max_completion_tokens': min(max_tokens, GROQ_MAX_COMPLETION),
        'temperature': 0.2,
        'response_format': {
            'type': 'json_schema',
            'json_schema': {'name': 'reponse', 'strict': model.startswith(_GROQ_STRICT), 'schema': schema},
        },
    }
    if model.startswith('openai/gpt-oss'):
        # Raisonnement court : il compte dans les tokens de réponse.
        body['reasoning_effort'] = 'low'
    fallback = False
    for _ in range(4):
        try:
            data = _groq_request(cfg, 'POST', 'chat/completions', json=body, timeout=900)
            break
        except _GroqTooLarge as exc:
            # Limite par minute : réponse réduite à ce qui reste après la question.
            room = exc.limit - (exc.requested - body['max_completion_tokens']) - 100
            if not exc.limit or room < GROQ_MIN_COMPLETION or room >= body['max_completion_tokens']:
                raise
            body = {**body, 'max_completion_tokens': room}
        except _GroqBadRequest as exc:
            match = _GROQ_MAX_RE.search(exc.detail)
            if match and int(match.group(1)) < body['max_completion_tokens']:
                # Plafond propre au modèle (16 384 pour certains).
                body = {**body, 'max_completion_tokens': int(match.group(1))}
                continue
            if fallback:
                raise
            # Modèle sans sortie structurée (ou JSON refusé) : mode JSON simple, schéma dans la consigne.
            fallback = True
            prompt = (f'{system}\n\nRéponds uniquement par un objet JSON valide, sans texte autour ni bloc '
                      f'de code, conforme à ce schéma JSON :\n{json.dumps(schema, ensure_ascii=False)}')
            body = {**body, 'response_format': {'type': 'json_object'},
                    'messages': [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': user}]}
    else:
        raise AiError('Groq refuse la requête : essayez un autre modèle')
    choices = data.get('choices') or []
    if not choices:
        raise AiError('Groq n\'a renvoyé aucune réponse')
    choice = choices[0]
    if choice.get('finish_reason') == 'length':
        raise AiError('Réponse du modèle tronquée (trop longue)')
    return (choice.get('message') or {}).get('content') or ''


# ===== Appel commun =====

def _require_key(cfg):
    if not provider_ready(cfg):
        raise AiError('Aucune clé API : renseignez-la dans Configuration → IA')


def test_connection(cfg):
    """Vérifie la clé et l'accès au modèle choisi, sans consommer de tokens
    (aiautotagging.com : une requête d'essai, comptée dans sa limite)."""
    _require_key(cfg)
    if cfg.get('provider') == 'aiautotagging':
        return _aiautotagging_test()
    if cfg.get('provider') == 'local':
        return _local_test(cfg)
    if cfg.get('provider') == 'groq':
        return _groq_test(cfg)
    if cfg.get('provider') == 'gemini':
        return _gemini_test(cfg)
    if cfg.get('provider') == 'openai':
        return _openai_test(cfg)
    if cfg.get('provider') == 'openrouter':
        return _openrouter_test(cfg)
    return _anthropic_test(cfg)


def call_json(cfg, system, user, schema, cache_system=False, max_tokens=64000):
    """Envoie une requête au modèle et renvoie la réponse JSON (conforme au schéma)."""
    _require_key(cfg)
    if not supports_llm(cfg):
        raise AiError('Ce traitement demande un modèle de langage : aiautotagging.com ne sait que '
                      'proposer des tags. Choisissez un autre fournisseur dans Configuration → IA')
    if cfg.get('provider') == 'local':
        text = _local_json(cfg, system, user, schema, max_tokens)
    elif cfg.get('provider') == 'groq':
        text = _groq_json(cfg, system, user, schema, max_tokens)
    elif cfg.get('provider') == 'gemini':
        text = _gemini_json(cfg, system, user, schema, max_tokens)
    elif cfg.get('provider') == 'openai':
        text = _openai_json(cfg, system, user, schema, max_tokens)
    elif cfg.get('provider') == 'openrouter':
        text = _openrouter_json(cfg, system, user, schema, max_tokens)
    else:
        text = _anthropic_json(cfg, system, user, schema, cache_system, max_tokens)
    # Modèles qui raisonnent à voix haute (qwen3, deepseek-r1…) : bloc <think> retiré.
    text = re.sub(r'^\s*<think>.*?</think>', '', text or '', flags=re.S).strip()
    if text.startswith('```'):
        text = re.sub(r'^```[a-zA-Z]*\s*|\s*```$', '', text)
    try:
        return json.loads(text)
    except ValueError:
        pass
    # Modèles sans sortie structurée : texte autour de l'objet JSON.
    start, end = text.find('{'), text.rfind('}')
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            pass
    raise AiError('Réponse du modèle illisible (JSON invalide) : essayez un autre modèle')


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


def _normalize_order(tag):
    """Rapproche les variantes d'un même tag (« de-gaulle », « charles-de-gaulle »,
    « general-de-gaulle ») : tri sur le dernier mot, sans accents."""
    words = [_plain(word) for word in re.split(r'[-\s_]+', tag) if _plain(word)]
    return (words[-1] if words else '', _plain(tag), tag)


def normalize_chunks(cfg, counts):
    """Liste des tags envoyée au modèle : en une fois, ou par morceaux (Groq gratuit)."""
    if cfg.get('provider') != 'groq':
        return ['\n'.join(f'{tag} ({count})' for tag, count in sorted(counts.items()))]
    chunks, lines, size = [], [], 0
    for tag in sorted(counts, key=_normalize_order):
        line = f'{tag} ({counts[tag]})'
        if lines and size + len(line) + 1 > GROQ_NORMALIZE_CHUNK_CHARS:
            chunks.append('\n'.join(lines))
            lines, size = [], 0
        lines.append(line)
        size += len(line) + 1
    if lines:
        chunks.append('\n'.join(lines))
    return chunks


def normalize_proposals(cfg, conn, progress=None):
    """Regroupements proposés par le modèle, filtrés sur les tags qui existent vraiment.
    progress(fait, total) est appelé après chaque morceau de la liste."""
    counts = tag_counts(conn)
    if len(counts) < 2:
        return []
    chunks = normalize_chunks(cfg, counts)
    answers = []
    for index, listing in enumerate(chunks):
        if len(chunks) == 1:
            user = f'Tags de la bibliothèque :\n{listing}'
        else:
            user = (f'Tags de la bibliothèque, partie {index + 1} sur {len(chunks)} (les tags voisins '
                    f'sont rapprochés ; regroupe seulement ceux de cette partie) :\n{listing}')
        answers.extend(call_json(cfg, NORMALIZE_SYSTEM, user, NORMALIZE_SCHEMA).get('groups') or [])
        if progress:
            progress(index + 1, len(chunks))
    groups, used = [], set()
    for group in answers:
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
