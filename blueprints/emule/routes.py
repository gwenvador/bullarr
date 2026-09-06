"""
Routes pour l'intégration eMule/aMule
"""
from flask import request, jsonify, current_app
from . import emule_bp
import re
import subprocess
from encryption import load_encrypted_json_config, save_encrypted_json_config


def load_emule_config():
    """Charge la configuration eMule"""
    return load_encrypted_json_config(
        current_app.config['CONFIG_FILE'], current_app.config['EMULE_CONFIG']
    )


def save_emule_config(config):
    """Sauvegarde la configuration eMule"""
    return save_encrypted_json_config(current_app.config['CONFIG_FILE'], config)


@emule_bp.route('/config', methods=['GET', 'POST'])
def emule_config():
    """Configuration eMule"""
    
    if request.method == 'GET':
        config = load_emule_config()
        
        # Masquer le mot de passe
        return jsonify({
            'enabled': config['enabled'],
            'type': config['type'],
            'host': config['host'],
            'ec_port': config['ec_port'],
            'password': '****' if config.get('password') else ''
        })
    
    else:  # POST
        try:
            new_config = request.get_json()
            config = load_emule_config()

            config['enabled'] = new_config.get('enabled', False)
            config['type'] = new_config.get('type', 'amule')
            config['host'] = new_config.get('host', '127.0.0.1')
            config['ec_port'] = new_config.get('ec_port', 4712)

            # Ne change le mot de passe que s'il n'est pas masqué
            new_password = new_config.get('password', '')
            if new_password and new_password != '****':
                config['password_decrypted'] = new_password

            if save_emule_config(config):
                return jsonify({'success': True})
            else:
                return jsonify({'success': False, 'error': 'Erreur de sauvegarde'}), 500

        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500


def _title_from_ed2k_link(link, fallback):
    """Extrait le nom de fichier d'un lien ed2k://|file|<nom>|<taille>|<hash>|/ - c'est
    EXACTEMENT le nom qu'aMule rapportera ensuite via `amulecmd show dl`, donc à préférer
    systématiquement à un titre fourni par le frontend (voir add_to_emule) pour que le
    suivi de progression (/api/activity/status) compare deux chaînes identiques plutôt
    qu'un titre reconstruit et un nom de fichier réel."""
    try:
        parts = link.split('|')
        if len(parts) > 2 and parts[2]:
            from urllib.parse import unquote
            return unquote(parts[2])
    except Exception:
        pass
    return fallback


def _size_from_ed2k_link(link):
    """Extrait la taille (en octets) d'un lien ed2k://|file|<nom>|<taille>|<hash>|/ -
    "dans le téléchargement amule je n'ai pas l'info de la taille du fichier": amulecmd
    ("show dl") ne rapporte que des comptes de parts eD2K, jamais cette taille réelle
    (voir _amule_status, activity/routes.py) - seul le lien lui-même la porte, connue dès
    l'ajout. None si le lien n'a pas ce format ou si le champ n'est pas un entier."""
    try:
        parts = link.split('|')
        if len(parts) > 3 and parts[3]:
            return int(parts[3])
    except Exception:
        pass
    return None


@emule_bp.route('/add', methods=['POST'])
def add_to_emule():
    """Ajoute un lien ED2K à eMule"""

    config = load_emule_config()

    if not config['enabled']:
        return jsonify({'success': False, 'error': 'aMule non activé'}), 400

    data = request.get_json()
    link = data.get('link')

    if not link:
        return jsonify({'success': False, 'error': 'Lien manquant'}), 400

    # Nom RÉEL embarqué dans le lien ed2k en priorité (voir _title_from_ed2k_link) -
    # repli sur le titre fourni par le frontend seulement si l'extraction échoue.
    title = _title_from_ed2k_link(link, data.get('title') or link[:80])
    # Connus seulement si l'ajout part d'une fiche série - voir même commentaire côté
    # qbittorrent/routes.py.
    series_id = data.get('series_id')
    volume_id = data.get('volume_id')
    volume_number = data.get('volume_number')
    source = data.get('source')
    source_link = data.get('source_link')
    force_replace = bool(data.get('force_replace'))

    try:
        cmd = [
            'amulecmd',
            '-h', config['host'],
            '-P', config.get('password_decrypted', ''),
            '-p', str(config['ec_port']),
            '-c', f'add {link}'
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

        from blueprints.missing_monitor.downloader import log_manual_download, mark_download_pending
        if result.returncode == 0:
            # "when adding a new file whatever source. it should be automatically added
            # to import" - visible sur /import dès maintenant (voir get_pending_downloads),
            # pas seulement une fois découvert par le sondage périodique d'aMule.
            # bytes_total: "dans le téléchargement amule je n'ai pas l'info de la taille
            # du fichier" - voir _size_from_ed2k_link ci-dessus/_amule_status,
            # activity/routes.py.
            from .ed2k_stats import extract_ed2k_hash
            # "once you add the file to the client ask for its id": c'est EXACTEMENT
            # l'id qu'`amulecmd show dl` rapporte ensuite (voir _AMULE_NAME_RE,
            # activity/routes.py, casse haute) - connu directement depuis le lien,
            # jamais besoin de le redemander à aMule (voir client_item_id ci-dessus).
            tracking_id = mark_download_pending(title, 'amule', series_id=series_id, volume_id=volume_id,
                                                 volume_number=volume_number, bytes_total=_size_from_ed2k_link(link),
                                                 client_item_id=extract_ed2k_hash(link),
                                                 force_replace=force_replace)
            log_manual_download(title, 'amule', True, source=source, source_link=source_link, tracking_id=tracking_id)
            return jsonify({'success': True})
        else:
            log_manual_download(title, 'amule', False, result.stderr, source=source, source_link=source_link)
            return jsonify({'success': False, 'error': result.stderr}), 500

    except Exception as e:
        from blueprints.missing_monitor.downloader import log_manual_download
        log_manual_download(title, 'amule', False, str(e), source=source, source_link=source_link)
        return jsonify({'success': False, 'error': str(e)}), 500


@emule_bp.route('/remove', methods=['POST'])
def remove_download():
    """Annule un téléchargement encore en cours ("dans import ajoute l'option supprimer
    pour les fichiers en téléchargement") - amulecmd "cancel <hash>", même hash ED2K que
    celui affiché par "show dl" (voir _amule_status, blueprints/activity/routes.py)."""
    config = load_emule_config()

    if not config['enabled']:
        return jsonify({'success': False, 'error': 'aMule non activé'}), 400

    data = request.get_json() or {}
    file_hash = data.get('id', '')
    # Uniquement hexadécimal (même format que celui capturé depuis "show dl") - construit
    # ensuite une commande amulecmd via f-string, pas de risque d'injection shell
    # (subprocess.run reçoit une liste d'arguments, pas une chaîne interprétée par un
    # shell) mais amulecmd interprète lui-même sa propre syntaxe de commande, autant
    # rejeter d'emblée une valeur qui ne peut de toute façon pas être un hash valide.
    if not re.fullmatch(r'[0-9A-Fa-f]{32}', file_hash or ''):
        return jsonify({'success': False, 'error': 'id (hash) invalide'}), 400

    try:
        cmd = [
            'amulecmd',
            '-h', config['host'],
            '-P', config.get('password_decrypted', ''),
            '-p', str(config['ec_port']),
            '-c', f'cancel {file_hash}'
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            return jsonify({'success': False, 'error': (result.stderr or 'Erreur amulecmd')[:200]}), 500
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@emule_bp.route('/ed2k-availability', methods=['POST'])
def ed2k_availability():
    """Disponibilité (nombre de sources ed2k) d'une liste de liens - enrichissement du
    tableau de résultats de recherche ("ensuite on pourra ordonner par nombre de seed
    comme pour les torrents"), voir ed2k_stats.py pour le détail de comment/pourquoi.
    Ne dépend PAS de la config aMule (`enabled`/host/ec_port) contrairement aux autres
    routes de ce blueprint: ed2k.shortypower.org est un service public indépendant de
    l'installation aMule de l'utilisateur, interrogeable même si aMule n'est pas configuré
    ici (seul l'AJOUT d'un lien à télécharger a besoin d'un aMule joignable)."""
    data = request.get_json(silent=True) or {}
    links = [l for l in (data.get('links') or []) if isinstance(l, str)]
    if not links:
        return jsonify({'success': True, 'availability': {}})

    from .ed2k_stats import get_ed2k_availability_bulk
    try:
        availability = get_ed2k_availability_bulk(links)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

    return jsonify({'success': True, 'availability': availability})


@emule_bp.route('/test', methods=['GET'])
def test_connection():
    """Test la connexion à eMule"""
    
    config = load_emule_config()
    
    if not config['enabled']:
        return jsonify({'success': False, 'error': 'aMule non activé'}), 400
    
    try:
        cmd = [
            'amulecmd',
            '-h', config['host'],
            '-P', config.get('password_decrypted', ''),
            '-p', str(config['ec_port']),
            '-c', 'status'
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        
        # amulecmd peut retourner 0 alors que la connexion EC a échoué : dans ce
        # cas, l'erreur est écrite dans stdout (par exemple « EC connection
        # failed » / « Connection Failed »), pas dans stderr. Ne pas laisser ce
        # faux positif apparaître comme une connexion réussie dans les paramètres.
        output = '\n'.join(part for part in (result.stdout, result.stderr) if part)
        connection_error = re.search(
            r'(?:ec\s+connection\s+failed|connection\s+failed|unable\s+to\s+connect|empty\s+reply)',
            output,
            re.IGNORECASE,
        )
        if result.returncode == 0 and not connection_error:
            return jsonify({'success': True, 'message': 'Connexion réussie'})

        if connection_error:
            error = (
                'Le serveur aMule ne répond pas ou refuse la connexion. '
                "Vérifiez l’hôte, le port et le mot de passe."
            )
        else:
            error = output.strip() or 'Connexion refusée'
        return jsonify({'success': False, 'error': error[:500]}), 500
    
    except FileNotFoundError:
        return jsonify({'success': False, 'error': 'amulecmd introuvable'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
