"""
Notifications ntfy (https://ntfy.sh ou serveur auto-hébergé) : configuration, envoi et test.

Même rôle que blueprints/telegram : ce module ne décide pas QUAND notifier. Les points
d'envoi de l'app passent par notifications.send_notification, qui appelle
send_ntfy_notification si ntfy est activé et que ce type de notification est coché.
"""
import json
import os
import re
from urllib.parse import urlparse

import requests
from flask import current_app, jsonify, request

from encryption import decrypt, encrypt
from . import ntfy_bp

# Règle de nommage des topics ntfy (voir la doc ntfy, « Publishing »).
_TOPIC_RE = re.compile(r'^[-_A-Za-z0-9]{1,64}$')
_SECRET_FIELDS = ('token', 'password')
MASK = '****'


def default_ntfy_config():
    return dict(current_app.config['NTFY_CONFIG'])


def load_ntfy_config():
    """Configuration ntfy, secrets déchiffrés dans <champ>_decrypted."""
    path = current_app.config['NTFY_CONFIG_FILE']
    cfg = default_ntfy_config()
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                stored = json.load(handle)
            if isinstance(stored, dict):
                cfg.update(stored)
        except (OSError, ValueError):
            pass
    for field in _SECRET_FIELDS:
        if cfg.get(field):
            decrypted = decrypt(cfg[field])
            if decrypted:
                cfg[f'{field}_decrypted'] = decrypted
    return cfg


def save_ntfy_config(config):
    path = current_app.config['NTFY_CONFIG_FILE']
    to_save = {key: value for key, value in config.items() if not key.endswith('_decrypted')}
    for field in _SECRET_FIELDS:
        plain = config.get(f'{field}_decrypted')
        if plain:
            to_save[field] = encrypt(plain)
        elif field in config and not config.get(field):
            to_save[field] = ''
    try:
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(to_save, handle, indent=4)
        os.chmod(path, 0o600)
        return True
    except OSError as exc:
        print(f"Erreur sauvegarde config ntfy : {exc}")
        return False


def normalize_server_url(value):
    """URL du serveur ntfy sans « / » final, None si ce n'est pas une URL http(s)."""
    value = (value or '').strip().rstrip('/')
    parsed = urlparse(value)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return None
    return value


def valid_topic(value):
    return bool(_TOPIC_RE.match((value or '').strip()))


def _auth_headers(cfg):
    mode = cfg.get('auth', 'none')
    if mode == 'token':
        token = cfg.get('token_decrypted') or ''
        return {'Authorization': f'Bearer {token}'} if token else {}
    if mode == 'basic':
        username = cfg.get('username') or ''
        password = cfg.get('password_decrypted') or ''
        if username:
            import base64
            raw = f'{username}:{password}'.encode('utf-8')
            return {'Authorization': 'Basic ' + base64.b64encode(raw).decode('ascii')}
    return {}


def _priority(cfg):
    try:
        value = int(cfg.get('priority', 3))
    except (TypeError, ValueError):
        return 3
    return min(max(value, 1), 5)


def send_ntfy_notification(message, title=None, tags=None, click=None, photo_path=None, config=None):
    """Publie `message` sur le topic configuré. Retourne (succès, erreur|None), sans jamais
    lever d'exception : une notification ratée ne doit pas faire échouer l'opération qui
    l'a déclenchée.

    config : configuration à utiliser telle quelle (test de valeurs pas encore
    enregistrées) ; par défaut la configuration sauvegardée, qui doit être activée.
    photo_path : image jointe (couverture). Si le serveur refuse les pièces jointes, la
    notification repart sans elle plutôt que d'être perdue."""
    cfg = config if config is not None else load_ntfy_config()
    if config is None and not cfg.get('enabled'):
        return False, 'Notifications ntfy désactivées'
    server = normalize_server_url(cfg.get('server_url'))
    topic = (cfg.get('topic') or '').strip()
    if not server:
        return False, 'Adresse du serveur ntfy invalide'
    if not valid_topic(topic):
        return False, 'Topic ntfy invalide (lettres, chiffres, - et _, 64 caractères maximum)'

    headers = _auth_headers(cfg)
    params = {'message': (message or '').strip() or (title or 'Bullarr'), 'priority': _priority(cfg)}
    if title:
        # « BD Importé : » sert de première ligne sur Telegram ; ici c'est un vrai titre.
        params['title'] = title.rstrip(' :')
    if tags:
        params['tags'] = ','.join(tags)
    if click:
        params['click'] = click

    try:
        if photo_path and os.path.isfile(photo_path):
            # Pièce jointe : le corps de la requête est le fichier, le reste passe en
            # paramètres d'URL (les en-têtes HTTP n'acceptent pas l'UTF-8 des titres).
            with open(photo_path, 'rb') as photo:
                response = requests.put(
                    f'{server}/{topic}', data=photo, timeout=20,
                    params={**params, 'filename': os.path.basename(photo_path)}, headers=headers,
                )
            if response.ok:
                return True, None
            if response.status_code in (401, 403):
                return False, _error_text(response)
        payload = {'topic': topic, **params}
        if tags:
            payload['tags'] = list(tags)
        response = requests.post(server, json=payload, headers=headers, timeout=10)
        if response.ok:
            return True, None
        return False, _error_text(response)
    except requests.exceptions.Timeout:
        return False, 'Timeout : le serveur ntfy ne répond pas'
    except requests.exceptions.ConnectionError:
        return False, f'Impossible de contacter {server}'
    except Exception as exc:  # pragma: no cover - filet de sécurité
        print(f"Erreur envoi ntfy : {exc}")
        return False, 'Erreur interne'


def _error_text(response):
    try:
        detail = response.json().get('error') or ''
    except ValueError:
        detail = response.text[:200]
    if response.status_code in (401, 403):
        return f'Accès refusé par ntfy ({response.status_code}) : vérifiez le jeton ou les identifiants'
    return f'Erreur ntfy ({response.status_code}) : {detail}'.rstrip(' :')


def _public_config(cfg):
    return {
        'enabled': bool(cfg.get('enabled')),
        'server_url': cfg.get('server_url') or 'https://ntfy.sh',
        'topic': cfg.get('topic') or '',
        'auth': cfg.get('auth') or 'none',
        'username': cfg.get('username') or '',
        'token': MASK if cfg.get('token') else '',
        'password': MASK if cfg.get('password') else '',
        'priority': _priority(cfg),
    }


def _merge_form(cfg, data):
    """Applique les valeurs du formulaire à cfg. Retourne un message d'erreur ou None."""
    server = normalize_server_url(data.get('server_url') or 'https://ntfy.sh')
    if not server:
        return "Adresse du serveur invalide (http:// ou https://)"
    topic = (data.get('topic') or '').strip()
    if (data.get('enabled') or topic) and not valid_topic(topic):
        return 'Topic invalide : lettres, chiffres, - et _ uniquement (64 caractères maximum)'
    auth = data.get('auth') if data.get('auth') in ('none', 'token', 'basic') else 'none'
    cfg.update({
        'enabled': bool(data.get('enabled')),
        'server_url': server,
        'topic': topic,
        'auth': auth,
        'username': (data.get('username') or '').strip(),
        'priority': _priority({'priority': data.get('priority', 3)}),
    })
    # Cases par type : réglées dans le tableau « Événements », changées seulement si envoyées.
    for key, value in data.items():
        if key.startswith('notify_'):
            cfg[key] = bool(value)
    for field in _SECRET_FIELDS:
        value = data.get(field)
        if value is None or value == MASK:
            continue
        cfg[f'{field}_decrypted'] = value
        if not value:
            cfg[field] = ''
            cfg.pop(f'{field}_decrypted', None)
    return None


@ntfy_bp.route('/config', methods=['GET', 'POST'])
def ntfy_config():
    if request.method == 'GET':
        return jsonify(_public_config(load_ntfy_config()))
    data = request.get_json(silent=True) or {}
    cfg = load_ntfy_config()
    error = _merge_form(cfg, data)
    if error:
        return jsonify({'success': False, 'error': error}), 400
    if not save_ntfy_config(cfg):
        return jsonify({'success': False, 'error': 'Erreur de sauvegarde'}), 500
    # La notification « import disponible » fait tourner la tâche planifiée d'import
    # même quand l'import automatique est désactivé (voir sync_auto_import_schedule).
    from blueprints.library.scheduler import sync_auto_import_schedule
    sync_auto_import_schedule()
    return jsonify({'success': True})


@ntfy_bp.route('/test', methods=['POST'])
def test_ntfy_notification():
    """Envoie une notification de test avec les valeurs saisies dans le formulaire, sans
    devoir les enregistrer ni activer ntfy d'abord (secrets masqués : valeurs sauvegardées)."""
    data = request.get_json(silent=True) or {}
    cfg = load_ntfy_config()
    error = _merge_form(cfg, {**data, 'enabled': True})
    if error:
        return jsonify({'success': False, 'error': error}), 400
    success, error = send_ntfy_notification(
        'La configuration ntfy fonctionne.', title='Bullarr : notification de test',
        tags=['white_check_mark'], config=cfg,
    )
    if success:
        return jsonify({'success': True, 'message': 'Notification envoyée'})
    return jsonify({'success': False, 'error': error}), 400
