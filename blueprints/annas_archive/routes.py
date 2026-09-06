import json
import os
from flask import request, jsonify, current_app
from . import annas_archive_bp

def load_annas_archive_config():
    path = current_app.config['ANNAS_ARCHIVE_CONFIG_FILE']
    if os.path.exists(path):
        with open(path, 'r') as f: return json.load(f)
    return current_app.config['ANNAS_ARCHIVE_CONFIG'].copy()

def save_annas_archive_config(config):
    path = current_app.config['ANNAS_ARCHIVE_CONFIG_FILE']
    with open(path, 'w') as f: json.dump(config, f, indent=2)

@annas_archive_bp.route('/config', methods=['GET', 'POST'])
def annas_archive_config():
    if request.method == 'GET': return jsonify(load_annas_archive_config())
    data = request.get_json() or {}
    config = load_annas_archive_config()
    from .scraper import ANNA_ARCHIVE_BASE_URL
    config['enabled'] = bool(data.get('enabled', True))
    config['base_url'] = (data.get('base_url') or '').strip().rstrip('/') or ANNA_ARCHIVE_BASE_URL
    save_annas_archive_config(config)
    return jsonify({'success': True})

@annas_archive_bp.route('/test', methods=['GET'])
def annas_archive_test():
    from .scraper import get_annas_archive_base_url, _HEADERS
    from network_safety import safe_external_get
    try:
        response = safe_external_get(get_annas_archive_base_url(), headers=_HEADERS, timeout=15, max_bytes=1024 * 1024)
        if response.status_code == 200: return jsonify({'success': True})
        return jsonify({'success': False, 'error': f'HTTP {response.status_code}'}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@annas_archive_bp.route('/shelfmark-download', methods=['POST'])
def shelfmark_download():
    """Queue an Anna's Archive release in the configured Shelfmark instance."""
    import re
    import requests
    from flask import current_app

    data = request.get_json() or {}
    md5 = str(data.get('md5') or '').strip().lower()
    if not re.fullmatch(r'[a-f0-9]{32}', md5):
        return jsonify({'success': False, 'error': 'Identifiant Anna invalide'}), 400
    title = str(data.get('title') or 'Anna\'s Archive release').strip()[:500]
    from blueprints.shelfmark.routes import load_shelfmark_config
    shelfmark = load_shelfmark_config()
    if not shelfmark.get('enabled', False):
        return jsonify({'success': False, 'error': 'Shelfmark est désactivé dans Configuration → Clients'}), 409
    base_url = shelfmark.get('base_url', '').rstrip('/')
    username = shelfmark.get('username', '')
    password = shelfmark.get('password', '')
    if not base_url or not username or not password:
        return jsonify({'success': False, 'error': 'Shelfmark n’est pas configuré'}), 503

    def _history(success, message):
        try:
            from blueprints.missing_monitor.downloader import log_manual_download
            log_manual_download(title, 'shelfmark', success, message, source='annas_archive', source_link=data.get('info_url'))
        except Exception as exc:
            print(f'Erreur historique Shelfmark: {exc}')

    try:
        with requests.Session() as session:
            login = session.post(
                f'{base_url}/api/auth/login',
                json={'username': username, 'password': password, 'remember_me': True},
                timeout=15,
            )
            if login.status_code >= 400:
                _history(False, f'Connexion Shelfmark refusée (HTTP {login.status_code})')
                return jsonify({'success': False, 'error': f'Connexion Shelfmark refusée (HTTP {login.status_code})'}), 502
            queued = session.post(
                f'{base_url}/api/releases/download',
                json={
                    'source': 'direct_download',
                    'source_id': md5,
                    'title': title,
                    'format': data.get('format') or 'cbz',
                    'size': data.get('size'),
                    'source_url': data.get('info_url'),
                    'content_type': 'ebook',
                },
                timeout=15,
            )
            if queued.status_code >= 400:
                try:
                    detail = queued.json().get('error')
                except ValueError:
                    detail = None
                _history(False, detail or f'Shelfmark a refusé la release (HTTP {queued.status_code})')
                return jsonify({'success': False, 'error': detail or f'Shelfmark a refusé la release (HTTP {queued.status_code})'}), 502
            from blueprints.missing_monitor.downloader import mark_download_pending
            tracking_id = mark_download_pending(
                title, 'shelfmark',
                series_id=data.get('series_id'), volume_id=data.get('volume_id'),
                volume_number=data.get('volume_number'),
                # "Remplacer quand même" - voir mark_download_pending (downloader.py).
                force_replace=bool(data.get('force_replace'))
            )
            _history(True, 'Release envoyée à Shelfmark')
            return jsonify({'success': True, 'status': 'queued', 'tracking_id': tracking_id})
    except requests.RequestException as exc:
        _history(False, f'Shelfmark inaccessible: {exc}')
        return jsonify({'success': False, 'error': f'Shelfmark inaccessible: {exc}'}), 502
