import json
import os
from urllib.parse import urlparse

import requests
from flask import current_app, jsonify, request

from . import shelfmark_bp


def load_shelfmark_config():
    path = current_app.config['SHELFMARK_CONFIG_FILE']
    if os.path.exists(path):
        with open(path, encoding='utf-8') as handle:
            saved = json.load(handle)
        config = current_app.config['SHELFMARK_CONFIG'].copy()
        config.update(saved)
        return config
    return current_app.config['SHELFMARK_CONFIG'].copy()


def save_shelfmark_config(config):
    path = current_app.config['SHELFMARK_CONFIG_FILE']
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(config, handle, indent=2)


@shelfmark_bp.route('/config', methods=['GET', 'POST'])
def shelfmark_config():
    if request.method == 'GET':
        config = load_shelfmark_config()
        return jsonify({**config, 'password': bool(config.get('password'))})
    data = request.get_json() or {}
    base_url = str(data.get('base_url') or '').strip().rstrip('/')
    parsed = urlparse(base_url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return jsonify({'success': False, 'error': 'URL Shelfmark invalide'}), 400
    config = load_shelfmark_config()
    config['enabled'] = bool(data.get('enabled', config.get('enabled', False)))
    config['base_url'] = base_url
    config['username'] = str(data.get('username') or '').strip()
    if data.get('password'):
        config['password'] = str(data['password'])
    save_shelfmark_config(config)
    return jsonify({'success': True})


@shelfmark_bp.route('/test', methods=['POST'])
def shelfmark_test():
    config = load_shelfmark_config()
    data = request.get_json() or {}
    base_url = str(data.get('base_url') or config.get('base_url') or '').strip().rstrip('/')
    username = str(data.get('username') or config.get('username') or '').strip()
    password = str(data.get('password') or config.get('password') or '')
    try:
        with requests.Session() as session:
            response = session.post(
                f'{base_url}/api/auth/login',
                json={'username': username, 'password': password, 'remember_me': False},
                timeout=15,
            )
            if response.status_code >= 400:
                return jsonify({'success': False, 'error': f'Connexion refusée (HTTP {response.status_code})'}), 400
        return jsonify({'success': True})
    except requests.RequestException as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
