"""
Routes pour l'intégration rTorrent

rTorrent n'a pas de Web UI/API REST native (contrairement à qBittorrent/Deluge) - il expose
une interface XML-RPC, généralement servie en HTTP via une passerelle devant son socket SCGI
(rutorrent, xmlrpc-c scgi_to_http, un reverse-proxy nginx...). rpc_path (config, défaut
"/RPC2") est l'endpoint HTTP de cette passerelle, pas un port d'API dédié comme les autres
clients - varie selon comment l'utilisateur a exposé rTorrent (rutorrent utilise souvent
"/RPC2" ou "/plugins/httprpc/action.php").
"""
from flask import request, jsonify, current_app
from . import rtorrent_bp
import os
import xmlrpc.client
from urllib.parse import urlsplit, urlunsplit, quote
from encryption import decrypt, load_encrypted_json_config, save_encrypted_json_config


def load_rtorrent_config():
    """Charge la configuration rTorrent"""
    return load_encrypted_json_config(
        current_app.config['RTORRENT_CONFIG_FILE'], current_app.config['RTORRENT_CONFIG']
    )


def save_rtorrent_config(config):
    """Sauvegarde la configuration rTorrent"""
    return save_encrypted_json_config(
        current_app.config['RTORRENT_CONFIG_FILE'], config, client_label='rTorrent'
    )


class RtorrentError(Exception):
    pass


def _rtorrent_proxy(config):
    """Construit un ServerProxy XML-RPC pointant vers la passerelle HTTP de rTorrent,
    avec identifiants Basic Auth embarqués dans l'URL si fournis (pris en charge
    nativement par xmlrpc.client, pas besoin d'un Transport custom)."""
    url = (config.get('url') or '').strip()
    if not url:
        raise RtorrentError('URL manquante')

    if not url.startswith('http://') and not url.startswith('https://'):
        url = 'http://' + url

    parts = urlsplit(url)
    port = config.get('port')
    netloc = parts.netloc
    if port and ':' not in netloc:
        netloc = f"{netloc}:{port}"

    username = (config.get('username') or '').strip()
    password = config.get('password_decrypted') or ''
    if not password and config.get('password'):
        try:
            password = decrypt(config.get('password')) or ''
        except Exception:
            password = ''

    if username and password:
        netloc = f"{quote(username)}:{quote(password)}@{netloc}"

    rpc_path = config.get('rpc_path') or '/RPC2'
    if not rpc_path.startswith('/'):
        rpc_path = '/' + rpc_path

    full_url = urlunsplit((parts.scheme, netloc, rpc_path, '', ''))
    return xmlrpc.client.ServerProxy(full_url, allow_none=True)


@rtorrent_bp.route('/config', methods=['GET', 'POST'])
def rtorrent_config():
    """Configuration rTorrent"""

    if request.method == 'GET':
        config = load_rtorrent_config()
        # Voir le commentaire équivalent dans qbittorrent/routes.py: tant qu'aucune config
        # n'a jamais été enregistrée, ne pas renvoyer les valeurs de repli de
        # RTORRENT_CONFIG (config.py) comme si l'utilisateur les avait saisies.
        has_saved_config = os.path.exists(current_app.config['RTORRENT_CONFIG_FILE'])
        return jsonify({
            'enabled': config.get('enabled', False),
            'url': config.get('url', '') if has_saved_config else '',
            'port': config.get('port') if has_saved_config else None,
            'rpc_path': config.get('rpc_path', '/RPC2') if has_saved_config else '',
            'username': config.get('username', '') if has_saved_config else '',
            'password': '****' if config.get('password_decrypted') else ''
        })

    else:  # POST
        try:
            new_config = request.get_json()
            config = load_rtorrent_config()

            config['enabled'] = new_config.get('enabled', False)
            config['url'] = new_config.get('url', '').strip()
            config['port'] = new_config.get('port') or ''
            config['rpc_path'] = new_config.get('rpc_path', '/RPC2').strip() or '/RPC2'
            config['username'] = new_config.get('username', '').strip()

            new_password = new_config.get('password', '')
            if new_password and new_password != '****':
                config['password_decrypted'] = new_password

            if save_rtorrent_config(config):
                return jsonify({'success': True})
            else:
                return jsonify({'success': False, 'error': 'Erreur de sauvegarde'}), 500

        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500


@rtorrent_bp.route('/test', methods=['POST', 'GET'])
def test_rtorrent_connection():
    """Teste la connexion à rTorrent (appel XML-RPC system.client_version)"""
    try:
        post_config = request.get_json() if request.method == 'POST' and request.get_json() else None
        config = post_config if post_config else load_rtorrent_config()

        if not post_config and not config.get('enabled', False):
            return jsonify({'success': False, 'error': 'Intégration rTorrent désactivée'}), 400

        # Voir le commentaire équivalent dans qbittorrent/routes.py: le mot de passe du
        # formulaire peut être le sentinel masqué '****' si l'utilisateur teste sans le
        # retaper - retomber sur le mot de passe réellement enregistré dans ce cas.
        if post_config and (not config.get('password_decrypted') or config.get('password_decrypted') == '****'):
            config['password_decrypted'] = load_rtorrent_config().get('password_decrypted')

        proxy = _rtorrent_proxy(config)
        version = proxy.system.client_version()
        return jsonify({'success': True, 'message': f"✅ Connexion réussie à rTorrent (version {version})"})

    except RtorrentError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except xmlrpc.client.ProtocolError as e:
        if e.errcode in (401, 403):
            return jsonify({'success': False, 'error': "❌ Accès refusé - vérifiez les identifiants"}), e.errcode
        return jsonify({'success': False, 'error': f"Erreur HTTP {e.errcode}: {e.errmsg}"}), 500
    except (ConnectionRefusedError, OSError) as e:
        return jsonify({'success': False, 'error': f"🔌 Impossible de se connecter à rTorrent: {str(e)}"}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': f"Erreur: {str(e)}"}), 500


@rtorrent_bp.route('/add', methods=['POST'])
def add_torrent():
    """Ajoute un torrent à rTorrent via load.start (accepte un lien magnet ou l'URL d'un
    fichier .torrent - rTorrent télécharge lui-même le fichier le cas échéant, pas besoin
    de le télécharger côté serveur comme pour qBittorrent)"""
    try:
        config = load_rtorrent_config()

        if not config.get('enabled', False):
            return jsonify({'success': False, 'error': 'rTorrent n\'est pas activé'}), 400

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

        proxy = _rtorrent_proxy(config)
        proxy.load.start('', torrent_url)

        from torrent_hash import extract_magnet_btih
        computed_client_item_id = extract_magnet_btih(torrent_url) if torrent_url.startswith('magnet:') else None

        from blueprints.missing_monitor.downloader import log_manual_download, mark_download_pending
        tracking_id = mark_download_pending(title, 'rtorrent', series_id=series_id, volume_id=volume_id,
                                             volume_number=volume_number, client_item_id=computed_client_item_id,
                                             force_replace=force_replace)
        log_manual_download(title, 'rtorrent', True, source=source, source_link=source_link, tracking_id=tracking_id)
        return jsonify({'success': True, 'message': 'Torrent ajouté à rTorrent'})

    except RtorrentError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except xmlrpc.client.ProtocolError as e:
        if e.errcode in (401, 403):
            return jsonify({'success': False, 'error': "Accès refusé - vérifiez les identifiants"}), e.errcode
        return jsonify({'success': False, 'error': f"Erreur HTTP {e.errcode}: {e.errmsg}"}), 500
    except (ConnectionRefusedError, OSError) as e:
        return jsonify({'success': False, 'error': f"Impossible de se connecter à rTorrent: {str(e)}"}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': f"Erreur: {str(e)}"}), 500


@rtorrent_bp.route('/remove', methods=['POST'])
def remove_torrent():
    """Supprime un torrent encore en téléchargement ("dans import ajoute l'option
    supprimer pour les fichiers en téléchargement") - d.erase retire le torrent du client
    mais ne touche pas aux données déjà écrites sur disque (rTorrent n'expose pas de
    suppression de fichiers via cette méthode XML-RPC, contrairement à qBittorrent) : un
    fichier partiel abandonné n'est de toute façon jamais repris par le scan d'import
    (extension non reconnue tant qu'il n'est pas complet)."""
    try:
        config = load_rtorrent_config()
        if not config.get('enabled', False):
            return jsonify({'success': False, 'error': "rTorrent n'est pas activé"}), 400

        data = request.get_json() or {}
        torrent_hash = data.get('id')
        if not torrent_hash:
            return jsonify({'success': False, 'error': 'id (hash) manquant'}), 400

        proxy = _rtorrent_proxy(config)
        proxy.d.erase(torrent_hash)
        return jsonify({'success': True})

    except RtorrentError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except (xmlrpc.client.Fault, xmlrpc.client.ProtocolError, OSError) as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': f"Erreur: {str(e)}"}), 500
