"""
Routes pour l'intégration Komga (serveur de bibliothèque BD)
Documentation API: https://komga.org/docs/openapi/komga-api
"""
from flask import request, jsonify
from . import komga_bp
import requests
from encryption import decrypt
from .client import KomgaClient, KomgaError
from .config_store import load_komga_config, save_komga_config, normalize_komga_url


@komga_bp.route('/config', methods=['GET', 'POST'])
def komga_config():
    """Configuration Komga"""

    if request.method == 'GET':
        config = load_komga_config()

        # Masquer la clé API
        return jsonify({
            'enabled': config.get('enabled', False),
            'url': config.get('url', ''),
            'api_key': '****' if config.get('api_key') else ''
        })

    else:  # POST
        try:
            new_config = request.get_json()
            config = load_komga_config()

            config['enabled'] = new_config.get('enabled', False)
            config['url'] = new_config.get('url', '').strip()

            # Ne change la clé API que si elle n'est pas masquée
            new_api_key = new_config.get('api_key', '')
            if new_api_key and new_api_key != '****':
                config['api_key_decrypted'] = new_api_key

            if save_komga_config(config):
                return jsonify({'success': True})
            else:
                return jsonify({'success': False, 'error': 'Erreur de sauvegarde'}), 500

        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500


@komga_bp.route('/test', methods=['GET', 'POST'])
def test_komga_connection():
    """Teste la connexion à Komga en récupérant la liste des bibliothèques.

    Accepte optionnellement un body JSON {url, api_key} pour tester les valeurs
    actuellement saisies dans le formulaire, sans exiger qu'elles aient été
    enregistrées ni que l'intégration soit déjà activée. Sans body (ou champs
    vides/masqués), retombe sur la configuration sauvegardée.
    """
    try:
        config = load_komga_config()
        payload = request.get_json(silent=True) or {}

        url = (payload.get('url') or '').strip() or config.get('url', '').strip()

        payload_api_key = payload.get('api_key', '')
        if payload_api_key and payload_api_key != '****':
            api_key = payload_api_key
        else:
            api_key = config.get('api_key_decrypted') or decrypt(config.get('api_key', ''))

        if not url or not api_key:
            return jsonify({'success': False, 'error': 'URL ou clé API manquante'}), 400

        url = normalize_komga_url(url)
        headers = {'X-API-Key': api_key}

        response = requests.get(f"{url}/api/v1/libraries", headers=headers, timeout=10)

        if response.status_code == 200:
            libraries = response.json()
            names = ', '.join(lib.get('name', '?') for lib in libraries) or 'aucune'
            return jsonify({
                'success': True,
                'message': f"Connexion réussie à Komga ({len(libraries)} bibliothèque(s): {names})"
            })
        elif response.status_code in (401, 403):
            return jsonify({'success': False, 'error': 'Clé API invalide ou refusée par Komga'}), 401
        else:
            return jsonify({
                'success': False,
                'error': f"Erreur HTTP {response.status_code}: {response.text[:200]}"
            }), response.status_code

    except requests.exceptions.Timeout:
        return jsonify({'success': False, 'error': 'Timeout: impossible de se connecter à Komga'}), 500
    except requests.exceptions.ConnectionError:
        return jsonify({'success': False, 'error': "Impossible de se connecter à Komga. Vérifiez l'URL."}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': f"Erreur: {str(e)}"}), 500


@komga_bp.route('/scan', methods=['POST'])
def scan_libraries():
    """Déclenche un scan du système de fichiers de toutes les bibliothèques Komga"""
    try:
        client = KomgaClient()
        libraries = client.list_libraries()

        if not libraries:
            return jsonify({'success': False, 'error': 'Aucune bibliothèque trouvée sur Komga'}), 404

        for library in libraries:
            client.scan_library(library['id'])

        names = ', '.join(lib.get('name', '?') for lib in libraries)
        return jsonify({
            'success': True,
            'message': f"Scan lancé pour {len(libraries)} bibliothèque(s): {names}"
        })

    except KomgaError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': f"Erreur: {str(e)}"}), 500
