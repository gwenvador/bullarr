"""
Disponibilité (nombre de sources) d'un lien ed2k via ed2k.shortypower.org ("utilise amule
cli or https://ed2k.shortypower.org/ pour checker la disponibilité des liens emule").

Pourquoi ce site plutôt qu'amulecmd: amulecmd (voir routes.py, déjà utilisé pour
add/cancel/status) n'a aucune commande pour interroger le nombre de sources d'un hash
donné sans l'ajouter réellement au coeur aMule de l'utilisateur (SHOW DL n'affiche que les
téléchargements déjà en cours) - la seule façon de l'obtenir via amulecmd serait
add + attendre que les sources arrivent (kad/serveurs, plusieurs secondes minimum) +
cancel, ce qui pollue la file de téléchargement réelle de l'utilisateur pour chaque
résultat de recherche évalué. ed2k.shortypower.org expose au contraire un vrai endpoint
HTTP en lecture seule (`?hash=<hash>`), sans authentification, avec un cache serveur de
son côté (~5h, voir "Result is valid until" dans la réponse) - aucun effet de bord sur
l'installation aMule de l'utilisateur, et rapide (page généra en <1s côté serveur d'après
son propre pied de page).

Résultat au mieux ("best-effort"): une erreur réseau/timeout ou une réponse impossible
à interpréter retourne une disponibilité inconnue (None), jamais un faux 0. Seule une
réponse explicite « Nothing found » vaut 0 source.
"""
import re
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

ED2K_STATS_URL = 'https://ed2k.shortypower.org/'
# Générique HTTP, pas de user-agent "navigateur" nécessaire - testé manuellement sans, le
# site répond correctement à un client requests par défaut.
_REQUEST_TIMEOUT = 8
# 8 requêtes en vol à la fois: assez pour qu'une page de résultats (typiquement <20 liens
# EBDZ) se résolve en un aller-retour réseau plutôt qu'en 20 séquentiels, sans pour autant
# bombarder ed2k.shortypower.org de dizaines de requêtes simultanées pour une seule recherche.
_MAX_WORKERS = 8

# Hash MD4 (32 hex) toujours le 4e segment d'un lien ed2k://|file|<nom>|<taille>|<hash>|/
# (voir _title_from_ed2k_link ci-dessus dans routes.py pour le même découpage par '|').
_ED2K_HASH_RE = re.compile(r'ed2k://\|file\|[^|]*\|\d+\|([0-9A-Fa-f]{32})\|')


def extract_ed2k_hash(link):
    """Extrait le hash hexadécimal d'un lien ed2k://|file|...|, ou None si le lien ne
    correspond pas au format attendu (lien serveur/serverlist, chaîne malformée...)."""
    if not link:
        return None
    match = _ED2K_HASH_RE.search(link)
    return match.group(1).upper() if match else None


def _fetch_ed2k_availability(ed2k_hash):
    """Interroge ed2k.shortypower.org pour un hash unique, retourne le nombre total de
    sources agrégé tous serveurs confondus (dernière ligne du tableau de résultat, colonne
    "Availability" - voir la structure de page ci-dessous), 0 si explicitement introuvable,
    ou None si la vérification a échoué.

    Structure de la page (vérifiée manuellement, pas de documentation officielle d'API):
    - trouvé: <table class="result">...</table> suivi d'un <table cellspacing="1"
      cellpadding="1">...</table> dont la dernière <tr style="text-align:right;"> est la
      ligne "N Server" agrégée - son 4e <th> est le total "Availability" recherché (le 3e
      est le nom de fichier, invariant qu'on ne réutilise pas ici).
    - introuvable: <table class="errorm"> ("Nothing found for ed2k::<hash>") - 0 source.
    """
    try:
        response = requests.get(ED2K_STATS_URL, params={'hash': ed2k_hash}, timeout=_REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException:
        return None

    try:
        soup = BeautifulSoup(response.text, 'html.parser')
        error_table = soup.find('table', class_='errorm')
        if error_table:
            # Seule la réponse EXPLICITE "Nothing found" documentée par le site vaut un
            # vrai 0 confirmé. Une autre page d'erreur (maintenance, changement de mise en
            # page, message inattendu) partageant la même classe CSS ne doit jamais être
            # interprétée comme une confirmation de disponibilité nulle.
            if 'nothing found' not in error_table.get_text(' ', strip=True).lower():
                return None
            return 0

        result_tables = soup.find_all('table', attrs={'cellspacing': '1', 'cellpadding': '1'})
        if not result_tables:
            return None

        rows = result_tables[0].find_all('tr')
        if not rows:
            return None
        # Dernière ligne = total agrégé ("N Server" / nom / taille / Availability /
        # Complete / drapeau) - les lignes du milieu sont le détail par serveur individuel,
        # pas utile pour un simple tri par disponibilité globale.
        totals = rows[-1].find_all('th')
        if len(totals) < 4:
            return None
        return int(totals[3].get_text(strip=True))
    except (ValueError, AttributeError, IndexError):
        return None


def get_ed2k_availability_bulk(links):
    """Version "asynchrone" (parallèle via threads, pas d'event loop nécessaire ici -
    requests est bloquant mais chaque requête est indépendante) de _fetch_ed2k_availability
    pour une liste de liens ed2k - un seul appel HTTP par HASH UNIQUE (des doublons de
    lien, ex. même fichier vu sur 2 threads EBDZ différents, ne déclenchent qu'une seule
    requête réseau) plutôt qu'un aller-retour par ligne du tableau de résultats affiché.

    Retourne {lien_original: nb_sources} - ne couvre que les liens dont le hash a pu être
    extrait (voir extract_ed2k_hash), les autres sont simplement absents du dict retourné
    plutôt que d'y figurer à 0 (0 sources et "pas un lien ed2k" doivent rester
    distinguables côté appelant)."""
    hash_by_link = {link: extract_ed2k_hash(link) for link in links}
    unique_hashes = {h for h in hash_by_link.values() if h}
    if not unique_hashes:
        return {}

    availability_by_hash = {}
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
        futures = {executor.submit(_fetch_ed2k_availability, h): h for h in unique_hashes}
        for future in as_completed(futures):
            h = futures[future]
            try:
                availability_by_hash[h] = future.result()
            except Exception:
                availability_by_hash[h] = None

    return {
        link: availability_by_hash[h]
        for link, h in hash_by_link.items()
        if h is not None and availability_by_hash.get(h) is not None
    }
