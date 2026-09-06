"""
Routes pour l'intégration Prowlarr
"""
from flask import request, jsonify, current_app
from . import prowlarr_bp
import requests
from encryption import decrypt
from .config_store import load_prowlarr_config, save_prowlarr_config


@prowlarr_bp.route('/config', methods=['GET', 'POST'])
def prowlarr_config():
    """Configuration Prowlarr"""
    
    if request.method == 'GET':
        config = load_prowlarr_config()
        
        # Masquer la clé API
        return jsonify({
            'enabled': config['enabled'],
            'url': config.get('url', ''),
            'port': config.get('port', 9696),
            'api_key': '****' if config.get('api_key') else '',
            # Nombre d'indexeurs activés affiché sur la carte Indexeurs (voir
            # loadIndexeurCardStatuses, settings.js) - manquait ici, la carte affichait
            # toujours "0 indexeur activé" faute de ce champ dans la réponse
            'selected_indexers': config.get('selected_indexers', [])
        })
    
    else:  # POST
        try:
            new_config = request.get_json()
            config = load_prowlarr_config()

            config['enabled'] = new_config.get('enabled', False)
            config['url'] = new_config.get('url', '').strip()
            config['port'] = new_config.get('port', 9696)

            # Ne change la clé API que si elle n'est pas masquée
            new_api_key = new_config.get('api_key', '')
            if new_api_key and new_api_key != '****':
                config['api_key_decrypted'] = new_api_key

            if save_prowlarr_config(config):
                return jsonify({'success': True})
            else:
                return jsonify({'success': False, 'error': 'Erreur de sauvegarde'}), 500

        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500


@prowlarr_bp.route('/test', methods=['POST', 'GET'])
def test_prowlarr_connection():
    """Teste la connexion à Prowlarr"""
    try:
        config = load_prowlarr_config()
        
        if not config['enabled']:
            return jsonify({'success': False, 'error': 'Integration Prowlarr désactivée'}), 400
        
        url = config.get('url', '').strip()
        api_key = config.get('api_key_decrypted') or decrypt(config.get('api_key', ''))
        
        if not url or not api_key:
            return jsonify({'success': False, 'error': 'URL ou clé API manquante'}), 400
        
        # Normaliser l'URL
        if not url.startswith('http://') and not url.startswith('https://'):
            url = 'http://' + url
        
        # URL de l'API Prowlarr
        test_url = f"{url}/api/v1/system/status"
        headers = {'X-Api-Key': api_key}
        
        response = requests.get(test_url, headers=headers, timeout=5)
        
        if response.status_code == 200:
            data = response.json()
            return jsonify({
                'success': True,
                'message': f"Connexion réussie à Prowlarr v{data.get('version', 'N/A')}"
            })
        else:
            return jsonify({
                'success': False,
                'error': f"Erreur HTTP {response.status_code}: {response.text[:200]}"
            }), response.status_code
            
    except requests.exceptions.Timeout:
        return jsonify({
            'success': False,
            'error': 'Timeout: impossible de se connecter à Prowlarr'
        }), 500
    except requests.exceptions.ConnectionError:
        return jsonify({
            'success': False,
            'error': 'Impossible de se connecter à Prowlarr. Vérifiez l\'URL.'
        }), 500
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f"Erreur: {str(e)}"
        }), 500


@prowlarr_bp.route('/search', methods=['GET'])
def search_prowlarr():
    """Recherche sur les indexeurs Prowlarr - endpoint dédié (utilisé par la page /search),
    contrairement à /api/search/prowlarr (une source parmi plusieurs pour /discover) ou au
    monitoring de volumes manquants: seul celui-ci a besoin d'un message d'erreur explicite
    quand Prowlarr n'est pas configuré, l'utilisateur ayant délibérément choisi cette
    source. Le coeur de la recherche (construire la requête, scorer, mettre en forme les
    résultats) est partagé - voir search_prowlarr_raw, search.py de ce même dossier."""
    from .search import search_prowlarr_raw

    query = request.args.get('query', '').strip()
    volume = request.args.get('volume', '').strip()

    if not query:
        return jsonify({'error': 'Veuillez entrer au moins un titre'}), 400

    config = load_prowlarr_config()
    if not config.get('enabled'):
        return jsonify({
            'error': 'Prowlarr n\'est pas activé. Allez dans la configuration pour l\'activer.'
        }), 400
    if not config.get('url', '').strip() or not (config.get('api_key_decrypted') or config.get('api_key')):
        return jsonify({
            'error': 'Configuration Prowlarr incomplète. Veuillez la compléter.'
        }), 400

    try:
        results = search_prowlarr_raw(query, volume_num=volume or None)
    except Exception as e:
        return jsonify({'error': f'Erreur de recherche: {str(e)}'}), 500

    # search_prowlarr_raw avale ses propres erreurs réseau/HTTP (voir search.py, pensé
    # pour des appelants qui veulent juste "aucun résultat" en silence) et renvoie None -
    # config déjà validée juste au-dessus, donc None ici ne peut venir que d'un problème
    # de connexion/réponse Prowlarr, pas d'une config manquante: seul CET appelant a
    # besoin de le distinguer d'une recherche simplement sans résultat ([]).
    if results is None:
        return jsonify({'error': 'Impossible de se connecter à Prowlarr. Vérifiez l\'URL et la clé API.'}), 500

    return jsonify({'results': results})


@prowlarr_bp.route('/indexers', methods=['GET', 'POST'])
def prowlarr_indexers():
    """Gère la liste des indexeurs Prowlarr"""
    
    if request.method == 'GET':
        # Récupérer les indexeurs depuis Prowlarr
        try:
            config = load_prowlarr_config()
            
            if not config['enabled']:
                return jsonify({
                    'success': False,
                    'error': 'Prowlarr n\'est pas activé'
                }), 400
            
            url = config.get('url', '').strip()
            api_key = config.get('api_key_decrypted') or decrypt(config.get('api_key', ''))
            
            if not url or not api_key:
                return jsonify({
                    'success': False,
                    'error': 'Configuration Prowlarr incomplète'
                }), 400
            
            # Normaliser l'URL
            if not url.startswith('http://') and not url.startswith('https://'):
                url = 'http://' + url
            
            # Supprime le port s'il est déjà dans l'URL
            if ':' in url and '/api' not in url:
                # C'est une URL avec port
                pass
            else:
                # Ajouter le port si spécifié
                port = config.get('port', '')
                if port and ':' not in url.split('//')[-1]:
                    url = url.rstrip('/') + ':' + str(port)
            
            # Récupérer les indexeurs - essayer plusieurs endpoints possibles
            indexers_url = f"{url}/api/v1/indexer"
            headers = {'X-Api-Key': api_key}
            
            response = requests.get(indexers_url, headers=headers, timeout=10)

            if response.status_code != 200:
                # Essayer un autre endpoint
                indexers_url = f"{url}/api/v1/indexers"
                response = requests.get(indexers_url, headers=headers, timeout=10)
            
            if response.status_code != 200:
                return jsonify({
                    'success': False,
                    'error': f'Erreur Prowlarr ({response.status_code}) - Vérifie l\'URL et la clé API de Prowlarr. URLs essayées: {url}/api/v1/indexer et {url}/api/v1/indexers'
                }), response.status_code
            
            indexers_data = response.json()
            
            # Charger la sélection sauvegardée
            saved_config = load_prowlarr_config()
            selected_ids = saved_config.get('selected_indexers', [])
            selected_categories = saved_config.get('selected_categories', {})  # Format: {indexer_id: [cat_ids]}
            
            # Formater les indexeurs
            indexers = []
            if isinstance(indexers_data, list):
                indexers_list = indexers_data
            elif isinstance(indexers_data, dict) and 'indexers' in indexers_data:
                indexers_list = indexers_data['indexers']
            else:
                indexers_list = []
            
            for indexer in indexers_list:
                indexer_id = indexer.get('id')
                selected_cats_for_indexer = selected_categories.get(str(indexer_id), [])
                
                # Extraire les catégories depuis capabilities
                categories = []
                capabilities = indexer.get('capabilities', {})
                if isinstance(capabilities, dict):
                    caps_categories = capabilities.get('categories', [])
                    
                    # Aplatir les catégories et sous-catégories
                    def flatten_categories(cats):
                        result = []
                        for cat in cats:
                            result.append({
                                'id': cat.get('id'),
                                'name': cat.get('name', 'Catégorie')
                            })
                            # Ajouter les sous-catégories
                            for subcat in cat.get('subCategories', []):
                                result.append({
                                    'id': subcat.get('id'),
                                    'name': f"  ↳ {subcat.get('name', 'Sous-catégorie')}"
                                })
                        return result
                    
                    categories = flatten_categories(caps_categories)
                
                indexers.append({
                    'id': indexer_id,
                    'name': indexer.get('name', 'Indexeur'),
                    'language': indexer.get('language'),
                    'selected': indexer_id in selected_ids,
                    'categories': [
                        {
                            'id': cat['id'],
                            'name': cat['name'],
                            'selected': cat['id'] in selected_cats_for_indexer
                        }
                        for cat in categories
                    ]
                })
            
            return jsonify({
                'success': True,
                'indexers': indexers
            })
            
        except requests.exceptions.Timeout:
            return jsonify({
                'success': False,
                'error': 'Timeout: impossible de se connecter à Prowlarr'
            }), 500
        except requests.exceptions.ConnectionError:
            return jsonify({
                'success': False,
                'error': 'Impossible de se connecter à Prowlarr'
            }), 500
        except Exception as e:
            return jsonify({
                'success': False,
                'error': f'Erreur: {str(e)}'
            }), 500
    
    else:  # POST - Sauvegarder la sélection
        try:
            data = request.get_json()
            selected_indexers = data.get('selected_indexers', [])
            selected_categories = data.get('selected_categories', {})  # Format: {indexer_id: [cat_ids]}
            
            config = load_prowlarr_config()
            config['selected_indexers'] = selected_indexers
            config['selected_categories'] = selected_categories
            
            if save_prowlarr_config(config):
                return jsonify({'success': True})
            else:
                return jsonify({'success': False, 'error': 'Erreur de sauvegarde'}), 500
                
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
