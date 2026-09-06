"""
Routes pour l'intégration Deluge

Pilotée via l'API JSON-RPC de la Web UI de Deluge (deluge-web, port par défaut 8112,
endpoint /json) - authentification par mot de passe seul (pas de nom d'utilisateur,
contrairement aux autres clients: c'est le modèle d'auth propre à deluge-web) via
auth.login, puis un cookie de session pour les appels suivants (core.add_torrent_url...).
"""
from flask import request, jsonify, current_app
from . import deluge_bp
import os
import requests
from encryption import decrypt, load_encrypted_json_config, save_encrypted_json_config


def load_deluge_config():
    """Charge la configuration Deluge"""
    return load_encrypted_json_config(
        current_app.config['DELUGE_CONFIG_FILE'], current_app.config['DELUGE_CONFIG']
    )


def save_deluge_config(config):
    """Sauvegarde la configuration Deluge"""
    return save_encrypted_json_config(
        current_app.config['DELUGE_CONFIG_FILE'], config, client_label='Deluge'
    )


class DelugeError(Exception):
    pass


def _deluge_base_url(config):
    url = (config.get('url') or '').strip()
    if not url:
        raise DelugeError('URL manquante')
    if not url.startswith('http://') and not url.startswith('https://'):
        url = 'http://' + url
    # "or 8112" (pas juste .get('port', 8112)): le port peut désormais être stocké en
    # chaîne vide volontairement (voir /config GET/POST) plutôt qu'absent de la config,
    # auquel cas .get(clé, défaut) ne retomberait jamais sur le défaut
    port = config.get('port') or 8112
    if ':' in url.split('://')[-1]:
        return url
    return f"{url}:{port}"


def _deluge_rpc(session, base_url, method, params=None):
    """Appel JSON-RPC générique à deluge-web - lève DelugeError sur toute erreur JSON-RPC
    (pas seulement HTTP), le protocole encode ses propres erreurs dans le corps 200."""
    try:
        response = session.post(
            f"{base_url}/json",
            json={'method': method, 'params': params or [], 'id': 1},
            timeout=10, verify=False
        )
    except requests.exceptions.Timeout:
        raise DelugeError('⏱️ Timeout - impossible de se connecter à Deluge')
    except requests.exceptions.ConnectionError as e:
        raise DelugeError(f"🔌 Impossible de se connecter à Deluge: {str(e)[:120]}")

    if response.status_code != 200:
        raise DelugeError(f"Erreur HTTP {response.status_code}")

    data = response.json()
    if data.get('error'):
        raise DelugeError(data['error'].get('message', str(data['error'])))
    return data.get('result')


def _deluge_login(config):
    """Session authentifiée + URL de base - lève DelugeError si le mot de passe est
    manquant/incorrect ou si la connexion échoue."""
    base_url = _deluge_base_url(config)

    password = config.get('password_decrypted') or ''
    if not password and config.get('password'):
        try:
            password = decrypt(config.get('password')) or ''
        except Exception:
            password = ''
    if not password:
        raise DelugeError('Mot de passe manquant')

    session = requests.Session()
    ok = _deluge_rpc(session, base_url, 'auth.login', [password])
    if not ok:
        raise DelugeError('❌ Mot de passe incorrect')
    return session, base_url


@deluge_bp.route('/config', methods=['GET', 'POST'])
def deluge_config():
    """Configuration Deluge"""

    if request.method == 'GET':
        config = load_deluge_config()
        # Voir le commentaire équivalent dans qbittorrent/routes.py: tant qu'aucune config
        # n'a jamais été enregistrée, ne pas renvoyer les valeurs de repli de
        # DELUGE_CONFIG (config.py) comme si l'utilisateur les avait saisies.
        has_saved_config = os.path.exists(current_app.config['DELUGE_CONFIG_FILE'])
        return jsonify({
            'enabled': config.get('enabled', False),
            'url': config.get('url', '') if has_saved_config else '',
            'port': config.get('port') if has_saved_config else None,
            'password': '****' if config.get('password_decrypted') else ''
        })

    else:  # POST
        try:
            new_config = request.get_json()
            config = load_deluge_config()

            config['enabled'] = new_config.get('enabled', False)
            config['url'] = new_config.get('url', '').strip()
            config['port'] = new_config.get('port') or ''

            new_password = new_config.get('password', '')
            if new_password and new_password != '****':
                config['password_decrypted'] = new_password

            if save_deluge_config(config):
                return jsonify({'success': True})
            else:
                return jsonify({'success': False, 'error': 'Erreur de sauvegarde'}), 500

        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500


@deluge_bp.route('/test', methods=['POST', 'GET'])
def test_deluge_connection():
    """Teste la connexion à Deluge (auth.login + web.connected)"""
    try:
        post_config = request.get_json() if request.method == 'POST' and request.get_json() else None
        config = post_config if post_config else load_deluge_config()

        if not post_config and not config.get('enabled', False):
            return jsonify({'success': False, 'error': 'Intégration Deluge désactivée'}), 400

        # Voir le commentaire équivalent dans qbittorrent/routes.py: le mot de passe du
        # formulaire peut être le sentinel masqué '****' si l'utilisateur teste sans le
        # retaper - retomber sur le mot de passe réellement enregistré dans ce cas. C'était
        # le vrai bug derrière "intégration de deluge ne marche pas": Deluge s'authentifie
        # par mot de passe SEUL (pas de nom d'utilisateur comme les autres clients), donc
        # ce mot de passe masqué envoyé tel quel faisait systématiquement échouer
        # auth.login, sans équivalent "juste un des champs" pour masquer le problème.
        if post_config and (not config.get('password_decrypted') or config.get('password_decrypted') == '****'):
            config['password_decrypted'] = load_deluge_config().get('password_decrypted')

        session, base_url = _deluge_login(config)
        connected = _deluge_rpc(session, base_url, 'web.connected')

        if connected:
            return jsonify({'success': True, 'message': "✅ Connexion réussie à Deluge (démon connecté)"})
        return jsonify({
            'success': True,
            'message': "⚠️ Authentifié, mais deluge-web n'est connecté à aucun démon Deluge"
        })

    except DelugeError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': f"Erreur: {str(e)}"}), 500


@deluge_bp.route('/add', methods=['POST'])
def add_torrent():
    """Ajoute un torrent à Deluge via core.add_torrent_url - accepte un lien magnet ou
    l'URL d'un fichier .torrent (Deluge télécharge lui-même le fichier le cas échéant)"""
    try:
        config = load_deluge_config()

        if not config.get('enabled', False):
            return jsonify({'success': False, 'error': 'Deluge n\'est pas activé'}), 400

        data = request.get_json()
        torrent_url = data.get('torrent_url') or data.get('url')
        if not torrent_url:
            return jsonify({'success': False, 'error': 'URL du torrent manquante'}), 400

        title = data.get('title') or torrent_url[:80]
        # Connus seulement si l'ajout part d'une fiche série - voir même commentaire côté
        # qbittorrent/routes.py.
        series_id = data.get('series_id')
        volume_id = data.get('volume_id')
        volume_number = data.get('volume_number')
        source = data.get('source')
        source_link = data.get('source_link')
        force_replace = bool(data.get('force_replace'))

        session, base_url = _deluge_login(config)
        result = _deluge_rpc(session, base_url, 'core.add_torrent_url', [torrent_url, {}])

        if not result:
            return jsonify({'success': False, 'error': 'Deluge a refusé le torrent (déjà présent ?)'}), 400

        from blueprints.missing_monitor.downloader import log_manual_download, mark_download_pending
        tracking_id = mark_download_pending(title, 'deluge', series_id=series_id, volume_id=volume_id,
                                             volume_number=volume_number, client_item_id=result,
                                             force_replace=force_replace)
        log_manual_download(title, 'deluge', True, source=source, source_link=source_link, tracking_id=tracking_id)
        return jsonify({'success': True, 'message': 'Torrent ajouté à Deluge'})

    except DelugeError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': f"Erreur: {str(e)}"}), 500


@deluge_bp.route('/remove', methods=['POST'])
def remove_torrent():
    """Supprime un torrent encore en téléchargement ("dans import ajoute l'option
    supprimer pour les fichiers en téléchargement") - avec ses données partielles
    (remove_data=True, deuxième argument de core.remove_torrent)."""
    try:
        config = load_deluge_config()
        if not config.get('enabled', False):
            return jsonify({'success': False, 'error': "Deluge n'est pas activé"}), 400

        data = request.get_json() or {}
        torrent_id = data.get('id')
        if not torrent_id:
            return jsonify({'success': False, 'error': 'id (hash) manquant'}), 400

        session, base_url = _deluge_login(config)
        _deluge_rpc(session, base_url, 'core.remove_torrent', [torrent_id, True])
        return jsonify({'success': True})

    except DelugeError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': f"Erreur: {str(e)}"}), 500
