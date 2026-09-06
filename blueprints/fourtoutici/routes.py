import json
import os
import re

from flask import request, jsonify, current_app
from . import fourtoutici_bp


def load_fourtoutici_config():
    """Charge la configuration fourtoutici - pas de secret à déchiffrer (source publique
    sans identifiants, contrairement à Prowlarr/Komga/Telegram), juste une bascule
    activé/désactivé comme les autres sources ("j'ai désactivé prowlarr mais il s'affiche
    toujours...")."""
    config_file = current_app.config['FOURTOUTICI_CONFIG_FILE']
    if os.path.exists(config_file):
        with open(config_file, 'r') as f:
            return json.load(f)
    return current_app.config['FOURTOUTICI_CONFIG'].copy()


def save_fourtoutici_config(config):
    config_file = current_app.config['FOURTOUTICI_CONFIG_FILE']
    with open(config_file, 'w') as f:
        json.dump(config, f, indent=4)


@fourtoutici_bp.route('/config', methods=['GET', 'POST'])
def fourtoutici_config():
    if request.method == 'GET':
        return jsonify(load_fourtoutici_config())

    new_config = request.get_json() or {}
    config = load_fourtoutici_config()
    config['enabled'] = bool(new_config.get('enabled', True))
    # "je voudrais modifier manuellement l'adresse de fourtoutici" - le site change
    # parfois de domaine/miroir. Chaîne vide/absente -> repli sur la valeur par défaut
    # (FOURTOUTICI_BASE_URL côté scraper.py) plutôt que de sauvegarder une base_url vide
    # qui casserait toutes les URLs construites dessus.
    from .scraper import FOURTOUTICI_BASE_URL
    base_url = (new_config.get('base_url') or '').strip().rstrip('/')
    config['base_url'] = base_url or FOURTOUTICI_BASE_URL
    save_fourtoutici_config(config)
    return jsonify({'success': True})


@fourtoutici_bp.route('/test', methods=['GET'])
def test_fourtoutici():
    """Vérifie que fourtoutici(.cc, ou l'adresse configurée) est joignable ("Tester la
    connexion", même convention que les autres cartes Indexeurs/Clients) - un simple GET
    sur la page d'accueil, pas besoin d'identifiants à valider puisqu'il n'y en a pas."""
    import requests
    from .scraper import get_fourtoutici_base_url, _HEADERS
    from network_safety import safe_external_get

    try:
        response = safe_external_get(get_fourtoutici_base_url(), headers=_HEADERS, timeout=10, max_bytes=1024 * 1024)
        if response.status_code == 200:
            return jsonify({'success': True})
        return jsonify({'success': False, 'error': f'HTTP {response.status_code}'}), 400
    except (requests.exceptions.RequestException, ValueError) as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@fourtoutici_bp.route('/download', methods=['POST'])
def download_fourtoutici_file():
    """Démarre le téléchargement d'un fichier fourtoutici dans son propre répertoire
    d'import dédié (FOURTOUTICI_IMPORT_DIRECTORY, voir config.py) - suit ensuite
    exactement le même chemin que n'importe quel fichier aMule/torrent/Telegram (scan,
    matching auto ou assignation manuelle sur /import). Retourne IMMÉDIATEMENT (le
    téléchargement tourne dans un thread dédié, voir scraper.download_fourtoutici_file_background)
    - même contrat que POST /api/telegram-channels/download."""
    from .scraper import download_fourtoutici_file_background, get_fourtoutici_base_url

    data = request.get_json() or {}
    file_id = data.get('file_id')
    filename = data.get('filename')
    if not file_id or not filename:
        return jsonify({'success': False, 'error': 'file_id et filename requis'}), 400

    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', str(file_id)):
        return jsonify({'success': False, 'error': 'file_id invalide'}), 400

    # filename is supplied by the caller, not by the remote catalogue. Never let it
    # select a path outside the dedicated import directory.
    from werkzeug.utils import secure_filename
    filename = secure_filename(os.path.basename(str(filename)))
    if not filename:
        return jsonify({'success': False, 'error': 'Nom de fichier invalide'}), 400

    series_id = data.get('series_id')
    volume_id = data.get('volume_id')
    volume_number = data.get('volume_number')
    # "Remplacer quand même" - voir mark_download_pending (downloader.py).
    force_replace = bool(data.get('force_replace'))

    target_dir = current_app.config.get('FOURTOUTICI_IMPORT_DIRECTORY')
    if not target_dir:
        return jsonify({'success': False, 'error': "Répertoire d'import fourtoutici non configuré"}), 500

    # Résolue ICI (contexte de requête) plutôt que dans le thread d'arrière-plan, qui n'a
    # pas de contexte Flask automatique pour relire la config (voir get_fourtoutici_base_url).
    base_url = get_fourtoutici_base_url()

    # _get_current_object(): le thread d'arrière-plan a besoin de l'objet Flask réel pour
    # pousser son propre app.app_context() (journalisation, active_downloads) - current_app
    # lui-même est un proxy lié à CETTE requête, invalide une fois sortie de son contexte.
    app = current_app._get_current_object()
    download_fourtoutici_file_background(
        file_id, filename, target_dir, base_url, app=app, pending_title=filename,
        series_id=series_id, volume_id=volume_id, volume_number=volume_number,
        force_replace=force_replace
    )

    return jsonify({'success': True, 'started': True})
