"""Forum VK « Au Phil des Bulles » : configuration de la recherche, test et téléchargement."""
import json
import os
import time

from flask import current_app, jsonify, request

from . import vk_bp
from .scraper import download_vk_file_background, is_vk_doc_url, safe_filename


@vk_bp.route('/download', methods=['POST'])
def download_vk_file():
    """Télécharge un document VK dans le répertoire d'import VK, puis l'importe comme un
    fichier Telegram ou fourtoutici. Retourne immédiatement (thread dédié)."""
    data = request.get_json(silent=True) or {}
    url = str(data.get('url') or '')
    filename = safe_filename(data.get('filename') or '')
    if not is_vk_doc_url(url):
        return jsonify({'success': False, 'error': 'Lien VK invalide (vk.com/doc… attendu)'}), 400
    if not filename:
        return jsonify({'success': False, 'error': 'Nom de fichier invalide'}), 400
    target_dir = current_app.config.get('VK_IMPORT_DIRECTORY')
    if not target_dir:
        return jsonify({'success': False, 'error': "Répertoire d'import VK non configuré"}), 500
    download_vk_file_background(
        url, filename, target_dir, app=current_app._get_current_object(), pending_title=filename,
        series_id=data.get('series_id'), volume_id=data.get('volume_id'),
        volume_number=data.get('volume_number'), force_replace=bool(data.get('force_replace')),
        source_link=str(data.get('source_link') or '') or None)
    return jsonify({'success': True, 'started': True})


def load_vk_config():
    """Recherche VK activée ou non (activée par défaut, comme les autres sources web)."""
    path = current_app.config['VK_CONFIG_FILE']
    try:
        with open(path, encoding='utf-8') as handle:
            config = json.load(handle)
    except (OSError, ValueError):
        config = {}
    return {**current_app.config['VK_CONFIG'], **(config if isinstance(config, dict) else {})}


def vk_search_enabled():
    return load_vk_config().get('enabled', True) is not False


@vk_bp.route('/config', methods=['GET', 'POST'])
def vk_config():
    """GET : réglage de la recherche VK et état de l'index des séries. POST {enabled}."""
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        config = load_vk_config()
        config['enabled'] = bool(data.get('enabled', True))
        from .scraper import normalize_base_url, configure
        try:
            config['base_url'] = normalize_base_url(data.get('base_url'))
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        configure(config['base_url'])
        path = current_app.config['VK_CONFIG_FILE']
        temp = path + '.tmp'
        with open(temp, 'w', encoding='utf-8') as handle:
            json.dump(config, handle, indent=4)
        os.replace(temp, path)
        return jsonify({'success': True, **config})
    config = load_vk_config()
    index = {}
    try:
        with open(os.path.join(current_app.config['DATA_DIR'], 'vk_index.json'), encoding='utf-8') as handle:
            stored = json.load(handle)
        index = {'series': len(stored.get('series') or []), 'built_at': stored.get('built_at')}
    except (OSError, ValueError, AttributeError):
        pass
    return jsonify({**config, 'index': index})


@vk_bp.route('/test', methods=['GET'])
def test_vk():
    """Vérifie que le forum public est lisible (sujets embarqués dans la page)."""
    from . import scraper
    try:
        scraper.configure_from_app()
        started = time.monotonic()
        page = scraper._fetch_page(scraper.VK_BOARD_URL)
        blocks = scraper.embedded_api_responses(page, 'board.getTopics')
        total = blocks[0]['response'].get('count') if blocks else 0
        if not total:
            return jsonify({'success': False, 'error': 'Forum VK joignable, mais aucun sujet lisible sur la page publique'}), 502
        return jsonify({'success': True, 'topics': total, 'seconds': round(time.monotonic() - started, 1)})
    except Exception as exc:
        return jsonify({'success': False, 'error': f'Forum VK injoignable : {exc}'}), 502
