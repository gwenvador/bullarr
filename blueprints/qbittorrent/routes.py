"""
Routes pour l'intégration qBittorrent
"""
from flask import request, jsonify, current_app
from . import qbittorrent_bp
import os
import sys
import time
import requests
from encryption import decrypt, load_encrypted_json_config, save_encrypted_json_config
from network_safety import safe_external_get


def load_qbittorrent_config():
    """Charge la configuration qBittorrent"""
    return load_encrypted_json_config(
        current_app.config['QBITTORRENT_CONFIG_FILE'], current_app.config['QBITTORRENT_CONFIG']
    )


def save_qbittorrent_config(config):
    """Sauvegarde la configuration qBittorrent"""
    return save_encrypted_json_config(
        current_app.config['QBITTORRENT_CONFIG_FILE'], config, client_label='qBittorrent'
    )


@qbittorrent_bp.route('/config', methods=['GET', 'POST'])
def qbittorrent_config():
    """Configuration qBittorrent"""

    if request.method == 'GET':
        config = load_qbittorrent_config()
        # Tant qu'aucune config n'a jamais été enregistrée, ne pas renvoyer les valeurs
        # de repli de QBITTORRENT_CONFIG (config.py) comme si l'utilisateur les avait
        # saisies - "par défaut ne met le texte en gris [placeholder] mais pas de valeur,
        # seulement celles qui sont entrées par l'utilisateur". Une fois un premier
        # enregistrement fait (même avec des champs vides), le fichier existe et ce qu'il
        # contient - y compris une valeur vide volontaire - fait foi.
        has_saved_config = os.path.exists(current_app.config['QBITTORRENT_CONFIG_FILE'])

        # Mot de passe masqué, comme les autres intégrations (EBDZ/Komga/Prowlarr) -
        # jamais renvoyé en clair sur un simple GET (voir docs/security audit 2026-07-10)
        return jsonify({
            'enabled': config.get('enabled', False),
            'url': config.get('url', '') if has_saved_config else '',
            'port': config.get('port') if has_saved_config else None,
            'username': config.get('username', '') if has_saved_config else '',
            'password': '****' if config.get('password_decrypted') else '',
            'default_category': config.get('default_category', '') if has_saved_config else ''
        })

    else:  # POST
        try:
            new_config = request.get_json()
            config = load_qbittorrent_config()

            config['enabled'] = new_config.get('enabled', False)
            config['url'] = new_config.get('url', '').strip()
            config['port'] = new_config.get('port') or ''
            config['username'] = new_config.get('username', '').strip()
            config['default_category'] = new_config.get('default_category', '').strip()

            # Mettre à jour le mot de passe seulement s'il a changé - '****' signifie
            # "inchangé" (valeur masquée renvoyée par le GET ci-dessus, voir EBDZ/Komga/
            # Prowlarr pour le même pattern)
            new_password = new_config.get('password', '')
            if new_password and new_password != '****':
                config['password_decrypted'] = new_password

            if save_qbittorrent_config(config):
                return jsonify({'success': True})
            else:
                return jsonify({'success': False, 'error': 'Erreur de sauvegarde'}), 500

        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500


@qbittorrent_bp.route('/test', methods=['POST', 'GET'])
def test_qbittorrent_connection():
    """Teste la connexion à qBittorrent"""
    try:
        # Utiliser la config du POST si fournie, sinon charger la config sauvegardée
        post_config = request.get_json() if request.method == 'POST' and request.get_json() else None

        if post_config:
            # Utiliser la configuration temporaire du formulaire pour le test
            config = post_config
            if not config.get('password_decrypted') or config.get('password_decrypted') == '****':
                config['password_decrypted'] = load_qbittorrent_config().get('password_decrypted')
        else:
            # Charger la configuration sauvegardée
            config = load_qbittorrent_config()
            if not config.get('enabled', False):
                return jsonify({'success': False, 'error': 'Intégration qBittorrent désactivée'}), 400

        if not config.get('url', '').strip():
            return jsonify({'success': False, 'error': 'URL manquante'}), 400

        # Créer une session authentifiée
        session, base_url, error = create_qbittorrent_session(config)
        if error:
            return jsonify({'success': False, 'error': f"Erreur config: {error}"}), 500

        api_url = f"{base_url}/api/v2/app/webuiVersion"

        response = session.get(api_url, timeout=5, verify=False)

        # Si 404 sur webuiVersion, essayer d'autres endpoints
        if response.status_code == 404:
            # Essayer /api/v2/app/preferences (indique si auth marche)
            api_url_alt = f"{base_url}/api/v2/app/preferences"
            response = session.get(api_url_alt, timeout=5, verify=False)

            if response.status_code == 200:
                return jsonify({
                    'success': True,
                    'message': f"✅ Connexion réussie à qBittorrent (authentification OK)"
                })

        if response.status_code == 200:
            version = response.text.strip()
            return jsonify({
                'success': True,
                'message': f"✅ Connexion réussie à qBittorrent (Web UI Version: {version})"
            })
        elif response.status_code == 403:
            no_auth_session = requests.Session()
            no_auth_response = no_auth_session.get(api_url, timeout=5, verify=False)

            if no_auth_response.status_code == 200:
                # C'était un problème d'identifiant
                return jsonify({
                    'success': False,
                    'error': "❌ Identifiants incorrects - qBittorrent est accessible mais le login a échoué.\n\nVérifiez:\n- Nom d'utilisateur correct\n- Mot de passe correct\n\nOu, si vous n'avez pas d'authentification:\n- Laissez les champs vides"
                }), 403
            else:
                # qBittorrent demande l'auth ET elle échoue
                return jsonify({
                    'success': False,
                    'error': "❌ Accès refusé (403) - Les identifiants semblent incorrects ou qBittorrent rejette votre authentification"
                }), 403
        elif response.status_code == 401:
            return jsonify({
                'success': False,
                'error': "Non authentifié (401) - Vérifiez vos identifiants"
            }), 401
        else:
            error_detail = response.text[:300] if response.text else 'Pas de détail'
            return jsonify({
                'success': False,
                'error': f"Erreur HTTP {response.status_code}: {error_detail}"
            }), response.status_code

    except requests.exceptions.Timeout:
        return jsonify({
            'success': False,
            'error': '⏱️ Timeout - Impossible de se connecter à qBittorrent.\n\nVérifiez:\n- L\'URL est correcte\n- Le port est correct (défaut: 8080)\n- qBittorrent est démarré\n- Le Web UI est activé\n- qBittorrent est accessible sur le réseau'
        }), 500
    except requests.exceptions.ConnectionError as ce:
        return jsonify({
            'success': False,
            'error': f"🔌 Impossible de se connecter à qBittorrent.\n\nVérifiez:\n- L'URL: {config.get('url')}\n- Le port: {config.get('port')}\n- qBittorrent est démarré\n- Le Web UI est activé\n- Pas de pare-feu bloquant\n\nErreur: {str(ce)[:80]}"
        }), 500
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f"Erreur: {str(e)}"
        }), 500


@qbittorrent_bp.route('/categories_and_tags', methods=['GET'])
def get_categories_and_tags():
    """Récupère les catégories et tags disponibles dans qBittorrent"""
    try:
        config = load_qbittorrent_config()

        if not config.get('enabled', False):
            return jsonify({
                'success': False,
                'error': 'qBittorrent n\'est pas activé',
                'categories': [],
                'tags': []
            }), 400

        # Créer une session authentifiée
        session, base_url, error = create_qbittorrent_session(config)
        if error:
            return jsonify({
                'success': False,
                'error': f"Erreur config: {error}",
                'categories': [],
                'tags': []
            }), 500

        categories = []
        tags = []

        # Récupérer les catégories
        try:
            cat_url = f"{base_url}/api/v2/torrents/categories"
            cat_response = session.get(cat_url, timeout=5, verify=False)

            if cat_response.status_code == 200:
                cat_data = cat_response.json()
                if isinstance(cat_data, dict):
                    categories = list(cat_data.keys())
        except Exception as e:
            # Best-effort: catégories laissées vides plutôt que de faire échouer toute
            # la route (les tags peuvent quand même être récupérés ci-dessous), aucune
            # autre trace de cet échec sinon.
            print(f"[qBittorrent] Erreur récupération catégories: {str(e)}", file=sys.stderr)

        # Récupérer les tags
        try:
            tag_url = f"{base_url}/api/v2/tags"
            tag_response = session.get(tag_url, timeout=5, verify=False)

            if tag_response.status_code == 200:
                tags = tag_response.json()
                if not isinstance(tags, list):
                    tags = []
        except Exception as e:
            # Best-effort, voir commentaire sur les catégories ci-dessus.
            print(f"[qBittorrent] Erreur récupération tags: {str(e)}", file=sys.stderr)

        return jsonify({
            'success': True,
            'categories': categories,
            'tags': tags
        })

    except Exception as e:
        return jsonify({
            'success': False,
            'error': f"Erreur: {str(e)}",
            'categories': [],
            'tags': []
        }), 500


def create_qbittorrent_session(config, for_test=False):
    """Crée une session requests authentifiée pour qBittorrent

    Args:
        config: Configuration dict avec url, port, username, password
        for_test: Si True, continue même si auth échoue (pour le diagnostic)

    Returns:
        Tuple (session, base_url, error_message)
    """
    try:
        url = config.get('url', '').strip()
        # "or 8080" (pas juste .get('port', 8080)): le port peut désormais être stocké en
        # chaîne vide volontairement (voir /config GET/POST) plutôt qu'absent de la config,
        # auquel cas .get(clé, défaut) ne retomberait jamais sur le défaut
        port = config.get('port') or 8080

        # Normaliser l'URL
        if not url.startswith('http://') and not url.startswith('https://'):
            url = 'http://' + url

        # Construire l'URL de base
        if ':' in url.split('://')[-1]:  # Port déjà présent
            base_url = url
        else:  # Port absent
            base_url = f"{url}:{port}"

        session = requests.Session()

        username = config.get('username', '').strip()

        # Gérer plusieurs cas de mot de passe:
        # 1. password_decrypted: mot de passe en texte clair (du formulaire)
        # 2. password: mot de passe chiffré (de la config sauvegardée)
        password = ''
        if config.get('password_decrypted'):
            # Du formulaire (texte clair)
            password = config.get('password_decrypted')
        elif config.get('password'):
            # De la config sauvegardée (chiffré)
            try:
                password = decrypt(config.get('password'))
            except Exception as decrypt_error:
                # Best-effort: retombe sur un mot de passe vide plutôt que de faire
                # échouer toute la session, aucune autre trace de cet échec sinon.
                print(f"[qBittorrent] Erreur déchiffrement: {str(decrypt_error)}", file=sys.stderr)
                password = ''

        if username and password:
            # Essayer de se logger d'abord
            login_url = f"{base_url}/api/v2/auth/login"
            try:
                login_response = session.post(login_url,
                    data={'username': username, 'password': password},
                    timeout=5, verify=False)

                # "== 200" à lui seul rejetait à tort un login pourtant réussi: certaines
                # installations qBittorrent (selon version/proxy inverse devant) renvoient
                # 204 No Content sur un login OK plutôt que 200 - le cookie de session est
                # posé sur `session` (par requests) dans les deux cas, le corps de
                # réponse n'a pas d'importance ici. Accepter toute la plage 2xx évite un
                # repli Basic Auth (et donc une authentification en double à chaque appel)
                # pour un login qui avait en réalité déjà fonctionné.
                if 200 <= login_response.status_code < 300:
                    return session, base_url, None
                else:
                    # Essayer avec Basic Auth
                    session.auth = (username, password)
                    return session, base_url, None
            except Exception as login_error:
                # Best-effort: retombe sur Basic Auth plutôt que de faire échouer toute
                # la session, aucune autre trace de cet échec sinon.
                print(f"[qBittorrent] Erreur login: {str(login_error)}", file=sys.stderr)
                session.auth = (username, password)
                return session, base_url, None

        return session, base_url, None

    except Exception as e:
        return None, None, str(e)


def get_qbittorrent_torrent_names(hashes):
    """"can the clients api tell you the location of the files so it will be easier to
    match" / "we might need to setup a remote path mapping. check this if it is needed" -
    qBittorrent peut tourner sur un tout autre hôte que cette app (voir docker-compose.yml,
    cette app monte son propre /downloads, potentiellement synchronisé depuis ailleurs via
    Syncthing) : comparer des CHEMINS ABSOLUS renvoyés par son API (save_path) contre les
    chemins vus localement demanderait un mapping chemin distant <-> chemin local (comme
    Sonarr/Radarr) - PAS implémenté ici, volontairement. On ne demande à l'API que le NOM
    du torrent (une simple chaîne, pas un chemin) - qBittorrent l'utilise par défaut comme
    nom du dossier de sauvegarde, un signal fiable pour relier un dossier trouvé sur disque
    à SON téléchargement suivi sans dépendre du nom des fichiers extraits à l'intérieur
    (souvent sans aucun rapport avec le titre suivi/le nom de la série, voir
    find_active_download_destination_by_torrent_name, library/routes.py) - et qui ne
    nécessite donc AUCUN mapping de chemin, seulement une comparaison de nom de dossier.

    Un seul appel API pour tous les hashes demandés (voir son usage dans
    scan_import_directory: un scan porte sur des dizaines de fichiers, potentiellement
    plusieurs téléchargements suivis distincts - même raisonnement que
    find_active_downloads_by_client_item_ids, downloader.py, pour éviter un aller-retour
    par hash). Retourne un dict hash (minuscule) -> nom, vide si qBittorrent n'est pas
    configuré/joignable ou si aucun hash ne correspond (best-effort, ne doit jamais faire
    échouer le scan qui l'appelle)."""
    if not hashes:
        return {}
    try:
        config = load_qbittorrent_config()
        if not config.get('enabled'):
            return {}
        session, base_url, error = create_qbittorrent_session(config)
        if error or not session:
            return {}
        response = session.get(
            f"{base_url}/api/v2/torrents/info",
            params={'hashes': '|'.join(hashes)}, timeout=8, verify=False
        )
        response.raise_for_status()
        return {t['hash'].lower(): t['name'] for t in response.json() if t.get('hash') and t.get('name')}
    except Exception as e:
        print(f"Erreur récupération noms de torrents qBittorrent: {e}")
        return {}


@qbittorrent_bp.route('/remove', methods=['POST'])
def remove_torrent():
    """Supprime un torrent encore en téléchargement ("dans import ajoute l'option
    supprimer pour les fichiers en téléchargement") - avec ses données partielles
    (deleteFiles=true), un téléchargement abandonné ne laisse pas de fichier incomplet
    traîner sur disque."""
    data = request.get_json() or {}
    torrent_hash = data.get('id')
    if not torrent_hash:
        return jsonify({'success': False, 'error': 'id (hash) manquant'}), 400

    config = load_qbittorrent_config()
    if not config.get('enabled'):
        return jsonify({'success': False, 'error': "qBittorrent n'est pas activé"}), 400

    session, base_url, error = create_qbittorrent_session(config)
    if error or not session:
        return jsonify({'success': False, 'error': error or 'Connexion impossible'}), 500

    try:
        response = session.post(
            f"{base_url}/api/v2/torrents/delete",
            data={'hashes': torrent_hash, 'deleteFiles': 'true'},
            timeout=10, verify=False
        )
        if response.status_code != 200:
            return jsonify({'success': False, 'error': f'HTTP {response.status_code}'}), 500
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@qbittorrent_bp.route('/add', methods=['POST'])
def add_torrent():
    """Ajoute un torrent à qBittorrent

    Procédure:
    - Les magnet links sont envoyés directement
    - Les URLs de fichiers torrent sont téléchargées puis envoyées comme fichier
    """
    try:
        import tempfile

        config = load_qbittorrent_config()

        if not config.get('enabled', False):
            return jsonify({'success': False, 'error': 'qBittorrent n\'est pas activé'}), 400

        data = request.get_json()
        torrent_url = data.get('torrent_url') or data.get('url')

        if not torrent_url:
            return jsonify({'success': False, 'error': 'URL du torrent manquante'}), 400

        title = data.get('title') or torrent_url[:80]
        # Connus seulement si l'ajout part d'une fiche série (voir searchMissingVolume/
        # displaySearchResults côté library.js) - None en recherche libre (/search), voir
        # mark_download_pending.
        series_id = data.get('series_id')
        volume_id = data.get('volume_id')
        volume_number = data.get('volume_number')
        source = data.get('source')
        source_link = data.get('source_link')
        force_replace = bool(data.get('force_replace'))

        # Créer une session authentifiée
        session, base_url, error = create_qbittorrent_session(config)
        if error:
            return jsonify({'success': False, 'error': f"Erreur config: {error}"}), 500

        # Ajouter le torrent
        api_url = f"{base_url}/api/v2/torrents/add"

        # Préparer le payload
        payload = {'paused': 'false'}  # String 'false' pour qBittorrent API
        files_to_send = None
        torrent_file_path = None

        # Ajouter la catégorie si fournie
        category = data.get('category', '').strip()
        if category:
            payload['category'] = category

        # Ajouter les tags si fournis
        tags = data.get('tags', [])
        if tags:
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(',') if t.strip()]
            if tags:
                payload['tags'] = ','.join(tags)

        # "once you add the file to the client ask for its id so you can put in the db
        # and use it later" - calculé ICI plutôt que redemandé à qBittorrent après coup
        # (son API "add" ne renvoie jamais l'id de ce qu'elle vient d'ajouter): un magnet
        # contient déjà son hash, un .torrent permet de le calculer nous-mêmes une fois
        # téléchargé plus bas (voir torrent_hash.py) - stocké sur la ligne
        # active_downloads dès l'insertion (mark_download_pending), plus jamais retrouvé
        # par correspondance de nom pour ce téléchargement (voir
        # find_active_downloads_by_client_item_ids, downloader.py). None si non
        # déterminable (échec du téléchargement/parsing du .torrent) - le lien se fait
        # alors comme avant, par nom, au premier sondage.
        computed_client_item_id = None

        # Vérifier si c'est un magnet link
        if torrent_url.startswith('magnet:'):
            # Magnet link - envoyer directement comme URL
            payload['urls'] = torrent_url
            from torrent_hash import extract_magnet_btih
            computed_client_item_id = extract_magnet_btih(torrent_url)
        else:
            # C'est une URL de fichier torrent - télécharger le fichier
            try:
                # Télécharger le fichier torrent
                torrent_response = safe_external_get(torrent_url, timeout=30, max_bytes=10 * 1024 * 1024)
                torrent_response.raise_for_status()

                # Créer un fichier temporaire
                temp_dir = tempfile.gettempdir()
                torrent_file_path = os.path.join(temp_dir, 'bullarr_torrent.torrent')

                # Écrire le fichier
                with open(torrent_file_path, 'wb') as f:
                    f.write(torrent_response.content)

                from torrent_hash import compute_torrent_info_hash
                computed_client_item_id = compute_torrent_info_hash(torrent_response.content)

                # Préparer le fichier à envoyer en multipart
                files_to_send = {
                    'torrents': ('torrent.torrent', open(torrent_file_path, 'rb'), 'application/x-bittorrent')
                }

            except requests.exceptions.Timeout:
                return jsonify({
                    'success': False,
                    'error': 'Timeout lors du téléchargement du torrent'
                }), 500
            except requests.exceptions.ConnectionError:
                return jsonify({
                    'success': False,
                    'error': 'Impossible de télécharger le torrent depuis Prowlarr/ED2K'
                }), 500
            except Exception as e:
                return jsonify({
                    'success': False,
                    'error': f"Erreur lors du téléchargement du torrent: {str(e)}"
                }), 500

        # Convertir tous les paramètres en strings pour le form-data
        data_payload = {k: str(v) if not isinstance(v, str) else v for k, v in payload.items()}

        # Envoyer à qBittorrent
        try:
            if files_to_send:
                # Envoyer le fichier binaire avec les paramètres en data
                response = session.post(api_url, data=data_payload, files=files_to_send, timeout=10, verify=False)
            else:
                # Envoyer comme URL (magnet)
                response = session.post(api_url, data=data_payload, timeout=10, verify=False)
        finally:
            # Fermer et supprimer le fichier temporaire
            if files_to_send and torrent_file_path:
                try:
                    files_to_send['torrents'][1].close()
                    if os.path.exists(torrent_file_path):
                        os.remove(torrent_file_path)
                except Exception as e:
                    # Best-effort: le fichier temporaire resterait juste orphelin sinon,
                    # aucune autre trace de cet échec.
                    print(f"[qBittorrent Add] Erreur suppression fichier temp: {str(e)}", file=sys.stderr)

        if response.status_code == 200:
            # qBittorrent's /add répond "Ok." (HTTP 200) même quand rien n'a été ajouté -
            # un .torrent invalide/inexploitable (ex: la réponse d'un tracker qui n'était
            # pas vraiment un fichier torrent) échoue silencieusement côté qBittorrent, sans
            # jamais remonter d'erreur HTTP. Confirmé sur deux téléchargements Prowlarr
            # restés bloqués "en cours" indéfiniment, absents du client - "this should have
            # warned of an error". On vérifie donc que le hash calculé apparaît bien dans
            # /torrents/info avant de déclarer un succès; qBittorrent traite l'ajout de
            # façon asynchrone, d'où quelques tentatives espacées plutôt qu'un seul essai
            # immédiat.
            if computed_client_item_id:
                added = False
                for attempt in range(4):
                    time.sleep(0.5 if attempt == 0 else 1)
                    try:
                        check = session.get(
                            f"{base_url}/api/v2/torrents/info",
                            params={'hashes': computed_client_item_id}, timeout=8, verify=False
                        )
                        check.raise_for_status()
                        if any(t.get('hash', '').lower() == computed_client_item_id for t in check.json()):
                            added = True
                            break
                    except Exception:
                        pass
                if not added:
                    # 200 (pas un code d'erreur HTTP) volontairement: c'est un échec détecté
                    # côté app, pas une réponse d'erreur de qBittorrent - _post_to_client
                    # (missing_monitor/downloader.py) ne lit le message détaillé de `error`
                    # que sur un 200, un autre code perdrait ce détail au profit d'un
                    # générique "HTTP xxx".
                    return jsonify({
                        'success': False,
                        'error': "qBittorrent a répondu Ok. mais le torrent n'apparaît pas dans sa liste "
                                 "(fichier torrent invalide ou rejeté silencieusement)"
                    })

            from blueprints.missing_monitor.downloader import log_manual_download, mark_download_pending
            tracking_id = mark_download_pending(title, 'qbittorrent', series_id=series_id, volume_id=volume_id,
                                                 volume_number=volume_number, client_item_id=computed_client_item_id,
                                                 force_replace=force_replace)
            log_manual_download(title, 'qbittorrent', True, source=source, source_link=source_link, tracking_id=tracking_id)
            return jsonify({
                'success': True,
                'message': 'Torrent ajouté à qBittorrent'
            })
        elif response.status_code == 403:
            return jsonify({
                'success': False,
                'error': 'Accès refusé - Vérifiez les identifiants'
            }), 403
        elif response.status_code == 401:
            return jsonify({
                'success': False,
                'error': 'Non authentifié - Vérifiez vos identifiants'
            }), 401
        else:
            error_detail = response.text[:200] if response.text else 'Pas de détail'
            return jsonify({
                'success': False,
                'error': f"Erreur qBittorrent ({response.status_code}): {error_detail}"
            }), response.status_code

    except requests.exceptions.Timeout:
        return jsonify({
            'success': False,
            'error': 'Timeout: impossible de se connecter à qBittorrent'
        }), 500
    except requests.exceptions.ConnectionError:
        return jsonify({
            'success': False,
            'error': 'Impossible de se connecter à qBittorrent'
        }), 500
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f"Erreur: {str(e)}"
        }), 500
