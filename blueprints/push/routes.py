"""
Notifications instantanées de l'application Android (UnifiedPush).

Le distributeur UnifiedPush du téléphone (l'app ntfy, en général) donne à l'application une
adresse personnelle ; l'application l'enregistre ici (POST /api/push/register, au démarrage et
après chaque changement). Bullarr envoie ensuite chaque évènement à cette adresse, sous la forme
d'un JSON {"kind", "title", "body"} que l'application affiche selon les alertes cochées sur le
téléphone. Types : import_completed, import_available, monitor_found, review, test.

Une adresse que le distributeur déclare supprimée (404 ou 410, voir la spécification
UnifiedPush) est retirée de la liste.
"""
import json
import os
import threading
import time
from urllib.parse import urlparse

import requests
from flask import current_app, jsonify, request

from . import push_bp

MAX_ENDPOINTS = 20
MAX_BODY_CHARS = 3000  # UnifiedPush garantit 4 Ko de message
_lock = threading.Lock()


def _path():
    return current_app.config['PUSH_ENDPOINTS_FILE']


def load_endpoints():
    try:
        with open(_path(), 'r', encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    entries = data.get('endpoints') if isinstance(data, dict) else None
    return [entry for entry in entries or [] if isinstance(entry, dict) and entry.get('endpoint')]


def _save_endpoints(entries):
    path = _path()
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump({'endpoints': entries}, handle, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _valid_endpoint(value):
    if not isinstance(value, str) or len(value) > 2048:
        return False
    parsed = urlparse(value)
    return parsed.scheme in ('http', 'https') and bool(parsed.netloc)


def register_endpoint(endpoint, device='', previous=None):
    with _lock:
        entries = [entry for entry in load_endpoints() if entry['endpoint'] not in (endpoint, previous)]
        entries.append({'endpoint': endpoint, 'device': (device or '')[:120], 'registered_at': int(time.time())})
        _save_endpoints(entries[-MAX_ENDPOINTS:])


def unregister_endpoint(endpoint):
    with _lock:
        entries = load_endpoints()
        kept = [entry for entry in entries if entry['endpoint'] != endpoint]
        if len(kept) != len(entries):
            _save_endpoints(kept)


def has_endpoints():
    return bool(load_endpoints())


def send_push(kind, title, body):
    """Envoie l'évènement à chaque téléphone abonné. Retourne (envois réussis, [erreurs]),
    sans jamais lever d'exception."""
    entries = load_endpoints()
    if not entries:
        return 0, []
    body = (body or '').strip()
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS - 1] + '…'
    payload = json.dumps({'kind': kind, 'title': title or 'Bullarr', 'body': body}, ensure_ascii=False).encode('utf-8')
    sent, errors, gone = 0, [], []
    for entry in entries:
        device = entry.get('device') or 'téléphone'
        host = urlparse(entry['endpoint']).netloc
        try:
            response = requests.post(entry['endpoint'], data=payload, timeout=10, allow_redirects=False,
                                     headers={'Content-Type': 'application/json', 'TTL': '86400'})
        except requests.exceptions.SSLError:
            errors.append(f"{device} : certificat HTTPS de {host} refusé par le serveur Bullarr")
            continue
        except requests.exceptions.Timeout:
            errors.append(f"{device} : {host} ne répond pas (délai dépassé)")
            continue
        except requests.RequestException:
            errors.append(f"{device} : impossible de joindre {host} depuis le serveur Bullarr "
                          f"(nom introuvable ou réseau Docker)")
            continue
        if response.status_code in (404, 410):
            gone.append(entry['endpoint'])
            errors.append(f"{device} : abonnement supprimé par {host}, réactivez les notifications dans l'application")
        elif 200 <= response.status_code < 300:
            sent += 1
        elif response.status_code in (401, 403):
            errors.append(f"{device} : {host} refuse l'envoi (HTTP {response.status_code}). Sur un ntfy protégé, "
                          f"autorisez l'écriture anonyme des topics UnifiedPush : "
                          f"ntfy access everyone 'up*' write-only")
        else:
            detail = ''
            try:
                detail = (response.json() or {}).get('error') or ''
            except ValueError:
                pass
            errors.append(f"{device} : {host} a répondu HTTP {response.status_code}"
                          + (f" ({detail})" if detail else ''))
    for endpoint in gone:
        unregister_endpoint(endpoint)
    return sent, errors


@push_bp.route('/register', methods=['POST'])
def push_register():
    data = request.get_json(silent=True) or {}
    endpoint = data.get('endpoint')
    if not _valid_endpoint(endpoint):
        return jsonify({'success': False, 'error': 'Adresse de notification invalide'}), 400
    previous = data.get('previous_endpoint') if _valid_endpoint(data.get('previous_endpoint')) else None
    register_endpoint(endpoint, data.get('device') or '', previous)
    return jsonify({'success': True, 'devices': len(load_endpoints())})


@push_bp.route('/unregister', methods=['POST'])
def push_unregister():
    data = request.get_json(silent=True) or {}
    if isinstance(data.get('endpoint'), str):
        unregister_endpoint(data['endpoint'])
    return jsonify({'success': True})


@push_bp.route('/test', methods=['POST'])
def push_test():
    if not has_endpoints():
        return jsonify({'success': False, 'error': 'Aucun téléphone abonné aux notifications instantanées'}), 400
    sent, errors = send_push('test', 'Bullarr : notification de test',
                             'Les notifications instantanées fonctionnent.')
    if sent:
        return jsonify({'success': True, 'sent': sent})
    # 400 et non 5xx : Bullarr fonctionne, c'est le distributeur (ntfy) qui refuse ou est
    # injoignable - l'application afficherait sinon « Erreur interne de Bullarr ».
    return jsonify({'success': False, 'error': '; '.join(errors) or 'Envoi impossible'}), 400


@push_bp.route('/devices', methods=['GET'])
def push_devices():
    """Téléphones abonnés (sans leurs adresses, qui valent un droit d'envoi)."""
    return jsonify({'success': True, 'devices': [
        {'device': entry.get('device') or 'Téléphone', 'registered_at': entry.get('registered_at')}
        for entry in load_endpoints()
    ]})
