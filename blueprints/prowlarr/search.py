"""
Coeur de recherche Prowlarr, partagé par les 3 points d'entrée qui en avaient chacun leur
propre copie quasi-identique avant ce module : la page /search (prowlarr/routes.py), la
page /discover (search/routes.py), et la recherche par tome précis du monitoring de
volumes manquants (missing_monitor/searcher.py). Les 3 copies avaient déjà divergé
silencieusement - `parsed_volume` manquant sur celle de /discover (colonne Volume vide
sur cette seule page), et le warning "tome non confirmé" (`unconfirmed_volume`) invisible
partout sauf dans le monitoring, alors que rien ne justifiait ces différences. Chaque
appelant garde seulement ce qui lui est propre (jsonify, gestion d'erreur HTTP dédiée) -
voir search_prowlarr_raw ci-dessous pour ce qui est commun.

URL Prowlarr: construite à partir du seul champ `url` de la config, un schéma prepended
si absent - PAS reconstruite depuis `port` séparément (les 2 anciennes copies de
search/routes.py et missing_monitor/searcher.py le faisaient, une divergence avec
prowlarr/routes.py qui n'utilisait jamais ce champ `port`). Le champ `url` est censé déjà
contenir le port si besoin (voir le placeholder du formulaire dans /settings:
"http://192.168.1.100:9696") - c'est aussi la seule variante que /api/prowlarr/test valide
réellement quand l'utilisateur clique "Tester la connexion", donc la seule dont on est
sûr qu'elle fonctionne en pratique.
"""
import re
import requests
from urllib.parse import urlparse
from encryption import decrypt
from .config_store import load_prowlarr_config


def clean_prowlarr_query(name):
    """Nettoie un titre de série pour la recherche Prowlarr - ponctuation superflue
    retirée, espaces normalisés. Même algorithme que search/routes.py::clean_series_name
    et l'ex-MissingVolumeSearcher._clean_series_name, désormais unifiés ici."""
    if not name:
        return ""
    cleaned = name.lower().strip()
    for char in ',;:\'"`':
        cleaned = cleaned.replace(char, '')
    cleaned = cleaned.replace('.', '')
    return re.sub(r'\s+', ' ', cleaned).strip()


def search_prowlarr_raw(title, volume_num=None, label=None, confirm=False, limit=None):
    """Interroge Prowlarr et retourne une liste de résultats déjà scorés/triés, ou None
    si Prowlarr n'est pas configuré/activé (distinct de [] = configuré mais 0 résultat) -
    chaque appelant décide comment signaler ce cas (page dédiée: message d'erreur
    explicite : recherche multi-sources: simplement aucun résultat Prowlarr).

    label (optionnel, ex. "Intégrale 6"/"HS 2"): remplace le simple numéro de tome dans
    le texte de la requête - permet de chercher une intégrale/hors-série correctement
    plutôt que le tome numéroté du même numéro.

    confirm=True calcule en plus unconfirmed_volume/unconfirmed_reason (le titre du
    résultat contient-il vraiment le tome/l'intégrale demandé, ou juste le nom de la
    série ?) - seule la recherche par tome précis (missing_monitor) en a besoin, pas une
    recherche libre par titre de série sans tome demandé en particulier.

    limit (optionnel): ne considérer que les `limit` premiers résultats bruts renvoyés
    par Prowlarr avant scoring - repli historique du monitoring (voir l'ancien
    _search_prowlarr), pas appliqué par défaut."""
    config = load_prowlarr_config()
    if not config.get('enabled'):
        return None

    url = config.get('url', '').strip()
    api_key = config.get('api_key_decrypted') or decrypt(config.get('api_key', ''))
    if not url or not api_key:
        return None

    if not url.startswith('http://') and not url.startswith('https://'):
        url = 'http://' + url

    clean_title = clean_prowlarr_query(title)
    search_title = clean_title
    if label:
        search_title += f' {label}'
    elif volume_num is not None:
        search_title += f' {volume_num}'

    params = {'query': search_title, 'type': 'search'}

    selected_indexers = config.get('selected_indexers', [])
    if selected_indexers:
        params['indexerIds'] = selected_indexers

    selected_categories_config = config.get('selected_categories', {})
    all_categories = set()
    for indexer_id in selected_indexers:
        indexer_id_str = str(indexer_id)
        if indexer_id_str in selected_categories_config:
            all_categories.update(selected_categories_config[indexer_id_str])
    if all_categories:
        params['categories'] = list(all_categories)

    try:
        response = requests.get(
            f"{url}/api/v1/search", headers={'X-Api-Key': api_key}, params=params, timeout=15
        )
    except requests.exceptions.RequestException as e:
        print(f"Erreur recherche Prowlarr: {e}")
        return None

    if response.status_code != 200:
        print(f"Erreur recherche Prowlarr: HTTP {response.status_code}")
        return None

    raw_data = response.json()
    data = raw_data if isinstance(raw_data, list) else raw_data.get('results', [])
    if limit:
        data = data[:limit]

    # Import tardif (évite un import circulaire au chargement du module: MissingVolumeSearcher
    # importe elle-même ce module pour son ancien _search_prowlarr, désormais un simple
    # relais vers search_prowlarr_raw - voir missing_monitor/searcher.py).
    from blueprints.missing_monitor.searcher import MissingVolumeSearcher
    from blueprints.library.scanner import LibraryScanner

    query_lower = title.lower()
    query_words = query_lower.split()

    results = []
    for item in data:
        item_title = item.get('title', '')
        item_title_lower = item_title.lower()

        score = 0
        if query_lower in item_title_lower:
            score += 100
        for word in query_words:
            if len(word) > 2 and re.search(r'\b' + re.escape(word) + r'\b', item_title_lower):
                score += 50

        if score == 0:
            continue

        info_url = item.get('infoUrl', '')
        tracker_name = ''
        if info_url:
            try:
                parsed_url = urlparse(info_url)
                tracker_name = (parsed_url.netloc or parsed_url.path).split('?')[0].split('#')[0]
            except Exception:
                tracker_name = info_url[:50]

        # Numéro de tome brut ("pourquoi nordheim ca na pas bien matcher les volumes") -
        # même parsing que EBDZ (voir 'volume'/'is_integral'/etc. dans /api/search côté
        # search/routes.py), jusqu'ici absent ici: seul le libellé d'affichage
        # (parsed_volume) existait, jamais un numéro exploitable par le frontend pour
        # taguer automatiquement CE résultat précis (voir search-results-table.js,
        # trackingVolumeNumber).
        parsed = LibraryScanner.parse_filename(item_title)
        result = {
            'source': 'prowlarr',
            'title': item.get('title', 'Sans titre'),
            'link': item.get('link', ''),
            'guid': item.get('guid', ''),
            'download_url': item.get('downloadUrl', ''),
            'size': item.get('size', 0),
            'seeders': item.get('seeders', 0),
            'peers': item.get('leechers', 0),
            'publish_date': item.get('publishDate', ''),
            'description': item.get('description', ''),
            'indexer': item.get('indexer', 'Prowlarr'),
            'tracker': tracker_name,
            'info_url': info_url,
            'parsed_volume': MissingVolumeSearcher._parsed_volume_label(item_title),
            'volume': parsed['volume'],
            'is_integral': parsed['is_integral'],
            'integral_number': parsed['integral_number'],
            'is_hs': parsed['is_hs'],
            'hs_number': parsed['hs_number'],
            'resolution': parsed['resolution'],
            '_score': score,
        }
        if confirm:
            confirmed, reason = MissingVolumeSearcher()._confirms_requested_volume(item_title, volume_num, label)
            result['unconfirmed_volume'] = not confirmed
            result['unconfirmed_reason'] = reason
        results.append(result)

    if confirm:
        results.sort(key=lambda r: (r['unconfirmed_volume'], -r['_score'], -(r.get('seeders', 0) or 0)))
    else:
        results.sort(key=lambda r: (-(r.get('seeders', 0) or 0), -r['_score']))
    for r in results:
        del r['_score']

    return results
