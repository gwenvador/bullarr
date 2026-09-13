"""
Scraper pour Bedetheque.com - Recherche de séries BD et récupération de leurs
métadonnées (auteurs, éditeur, statut, liste des albums)

Note anti-bot: la recherche (/search/tout) exige un cookie de session ET un paramètre
csrf_token_bel dont la valeur doit correspondre au cookie csrf_cookie_bel (double-submit),
plus un header Referer pointant vers le site. On visite donc une première fois la page
d'accueil pour obtenir ce jeton avant toute recherche. Les pages fiche série/album (GET
simple) n'ont pas cette contrainte.
"""
import requests
from bs4 import BeautifulSoup
import re
import copy
import sqlite3
from urllib.parse import urljoin
import time
import random
import json
import logging
import os
import unicodedata
from pathlib import Path
import hashlib

logger = logging.getLogger(__name__)


def _reformat_bedetheque_author_name(name):
    """"aussi pour les auteurs bedetheque ajoute des , supprime ca" - Bédéthèque affiche
    un auteur au format "Nom, Prénom" (ex: "Marini, Enrico"), parfois suivi d'un suffixe
    de désambiguïsation entre parenthèses pour un pseudonyme partagé par plusieurs
    personnes (ex: "Greg (1)" - deux "Greg" différents sur le site). Reformaté en
    "Prénom Nom (N)" (ex: "Enrico Marini") - un pseudonyme sans virgule (ex: "Lax",
    "Bdman") n'a rien à inverser et reste inchangé. Sans ça, la virgule interne à CHAQUE
    nom devenait indissociable de la virgule utilisée plus loin pour séparer plusieurs
    AUTEURS différents (', '.join(scenaristes)/dessinateurs) - un scénariste "Gaudin,
    Christian" au milieu d'une liste jointe se lisait comme deux personnes distinctes
    ("Gaudin" et "Christian"), et le filtre multi-valeur de la colonne Auteur (voir
    library.js, _tableFilterColumns) le découpait bel et bien en deux fausses entrées."""
    if not name:
        return name
    name = name.strip()
    suffix_match = re.search(r'\s*(\(\d+\))\s*$', name)
    suffix = ''
    core = name
    if suffix_match:
        suffix = ' ' + suffix_match.group(1)
        core = name[:suffix_match.start()].strip()
    if ', ' in core:
        lastname, firstname = core.split(', ', 1)
        core = f'{firstname.strip()} {lastname.strip()}'
    return core + suffix


def _author_link_from_span(span):
    """URL de la fiche auteur Bédéthèque (ex: https://www.bedetheque.com/auteur-123-BD-
    Pratt-Hugo.html) associée à un span[itemprop="author"/"illustrator"] - le nom de
    l'auteur est présent sous forme de lien <a href="...auteur-..."> AUTOUR de ce span sur
    la fiche série, jusqu'ici jamais lu (seul le texte du nom était gardé, voir
    _reformat_bedetheque_author_name) - item #25 improvement.txt ("click on author will
    open a modal for other album from the same author"). Un span sans <a> parent (rare,
    auteur sans fiche dédiée sur le site) retourne None plutôt que d'échouer."""
    if not span:
        return None
    link = span.find_parent('a')
    if not link or not link.get('href'):
        return None
    return urljoin('https://www.bedetheque.com', link['href'])


def _parse_int_hs_prefix(title):
    """Détecte un préfixe INT/HS en tête d'un titre d'album Bédéthèque ("INT1 . ...",
    "HS1 . ...") et en extrait le numéro éventuel - partagé entre _index_bedetheque_volumes
    ci-dessous (matching d'un fichier réellement possédé) et _sync_bedetheque_placeholder_
    volumes (blueprints/bedetheque/routes.py, création des tomes placeholder manquants),
    qui dupliquaient chacun leur propre copie de cette même regex ("un seul parseur" -
    même philosophie que LibraryScanner.parse_filename, voir CLAUDE.md).

    "serie 846 il y a INTFL1 c'est une intégrale france loisirs... ce qui n'est pas avec
    un T ne doit pas etre un volume" - Bédéthèque encode ses variantes d'intégrale/hors-
    série avec toutes sortes de qualificatifs entre le code et le chiffre (FL = France
    Loisirs, TL = Tirage de Luxe, TT = Tirage de Tête, N&B, Codex, un simple espace, un
    tiret, un "/"...) : "INTFL1", "INT FL", "INT-TS2025", "INT en Cof", "IntTL01",
    "HS2019/10" sont tous des formats réels rencontrés sur des fiches de cette
    bibliothèque. L'ancienne regex ("INT" suivi DIRECTEMENT de chiffres, ou d'une limite
    de mot juste après) ne couvrait qu'une poignée de ces formats et ratait tout le
    reste, laissant l'album hors classification (ni is_integral, ni volume_number), avec
    son code brut visible tel quel dans le titre affiché - "INTFL1" en étant l'exemple
    signalé, mais 38 autres séries de cette bibliothèque avaient le même problème sous
    une variante ou une autre (vérifié empiriquement en comparant chaque album Bédéthèque
    connu contre l'ancienne ET la nouvelle regex).

    Capture maintenant TOUT ce qui suit "INT"/"HS" jusqu'au séparateur " . " (convention
    Bédéthèque constante, "CODE . Titre" - jamais de point ailleurs dans un code réel),
    quels que soient les caractères entre les deux, et en extrait le premier nombre qu'il
    contient, s'il y en a un. Borné à 20 caractères pour éviter qu'un vrai titre sans
    aucun code (qui contiendrait par coïncidence un point plus loin dans une phrase) ne
    soit pris à tort pour un code interminable.

    Retourne (is_integral, integral_number, is_hs, hs_number)."""
    m = re.match(r'^INT([^.]{0,20}?)\.\s', title, re.IGNORECASE)
    if m:
        num_m = re.search(r'(\d+)', m.group(1))
        return True, (int(num_m.group(1)) if num_m else None), False, None
    m = re.match(r'^HS([^.]{0,20}?)\.\s', title, re.IGNORECASE)
    if m:
        num_m = re.search(r'(\d+)', m.group(1))
        return False, None, True, (int(num_m.group(1)) if num_m else None)
    return False, None, False, None


def _parse_special_prefix(title):
    """Détecte un préfixe de code d'édition spéciale en tête d'un titre d'album
    Bédéthèque - tout ce qui n'est ni un tome numéroté classique (champ 'number'), ni une
    intégrale/hors-série (voir _parse_int_hs_prefix ci-dessus, à essayer EN PREMIER par
    l'appelant), ni un épisode. "il y a un volume COF. ce n'est pas un volume, c'est un
    spécial (Coffret)... same for everything that is not a volume, no all in volumes as
    there are not" - contrairement à INT/HS, Bédéthèque n'a AUCUN vocabulaire fermé pour
    ces codes: coffrets ("Cof"), tirages ("TT", "TL", "HC"), rééditions promotionnelles
    liées à un partenaire ("Pub", "MBD05", "Quick1", "Lidl"...), compilations ("Compil1",
    "BestOf2"), recueils ("R1"-"R5")... 135 codes DISTINCTS recensés sur cette seule
    bibliothèque en pratique, la plupart un code ponctuel propre à un seul éditeur/une
    seule série - tenter de les nommer un par un serait sans fin. Le code brut détecté
    (avant le premier " . ") est renvoyé tel quel comme label plutôt qu'un nom "traduit",
    pas de dictionnaire figé à maintenir.

    Retourne (is_special, special_label) - special_label est le code brut (ex: "COF",
    "MBD05"), None si aucun code détecté."""
    m = re.match(r'^([A-Za-zÀ-ÿ0-9&/+\-\s]{1,20}?)\s*\.\s', title)
    if m:
        return True, m.group(1).strip()
    return False, None


def _index_bedetheque_volumes(bd_volumes):
    """Indexe les albums d'une fiche série Bedetheque (get_series_info()['volumes']) sur
    4 clés distinctes selon leur type: numéro de tome classique (champ 'number'), numéro
    d'intégrale, numéro de hors-série, ou numéro d'épisode. Bédéthèque laisse 'number' à
    None pour les intégrales/hors-séries et met leur numéro seulement en tête de titre
    ("INT1 . ...", "HS1 . ...") - sans cette indexation dédiée, ces albums étaient
    purement et simplement absents du matching par numéro (filtrés par
    `number is not None`) et retombaient sur l'URL générique de la fiche série au lieu de
    leur propre page d'album (constaté: intégrales de "Wayne Shelton" avec le <Web> de la
    série plutôt que le leur).

    Une série qui n'a qu'UNE seule intégrale/hors-série ne porte souvent aucun numéro du
    tout ("INT . L'intégrale", pas "INT1 . ..."): indexée à la clé None, comme un tome
    local sans integral_number/hs_number (voir parse_filename) - sans ce cas, un tel
    album Bédéthèque tombait dans aucune des 3 clés et un tome local is_integral=1/
    integral_number=None ne pouvait jamais le retrouver (constaté: "Universal War One",
    l'intégrale locale restait sans match malgré "INT . L'intégrale" bien présent sur la
    fiche série).

    "Épisode" traité AVANT le test sur 'number': contrairement à INT/HS, Bédéthèque
    numérote ses épisodes dans le même champ 'number' que ses tomes, à la queue leu leu -
    https://www.bedetheque.com/serie-70835-BD-Bete-Frank-Pe-Zidrou.html a "Tome 1" ET
    "Épisode 1" avec number=1 chacun ("il a episode et tome. this is different"). Router
    Épisode vers by_number comme les autres aurait fait gagner l'un des deux au hasard
    (setdefault, premier arrivé) et perdre l'autre en silence - le numéro d'épisode est
    donc extrait du TITRE plutôt que du champ 'number'."""
    by_number, by_integral, by_hs, by_episode = {}, {}, {}, {}
    for v in bd_volumes or []:
        title = (v.get('title') or '').strip()
        m_ep = re.search(r'\b[EÉ]p(?:isode)?\.?\s*(\d+)\b', title, re.IGNORECASE)
        if m_ep:
            by_episode.setdefault(int(m_ep.group(1)), []).append(v)
            continue
        if v.get('number') is not None:
            by_number.setdefault(v['number'], []).append(v)
            continue
        is_integral, integral_number, is_hs, hs_number = _parse_int_hs_prefix(title)
        if is_integral:
            by_integral.setdefault(integral_number, []).append(v)
            continue
        if is_hs:
            by_hs.setdefault(hs_number, []).append(v)
    return by_number, by_integral, by_hs, by_episode


def _local_title_from_filename(filename):
    """Dérive un titre comparable depuis un nom de fichier local, pour le repli par titre
    de match_bedetheque_volume ci-dessous - retire l'extension et le suffixe "- (AAAA)"
    (année de publication) qu'ajoute le renommage standard en fin de nom, laissant le
    reste ("Série - Titre de l'album") comparable au titre d'un album Bedetheque."""
    if not filename:
        return None
    name = re.sub(r'\.\w+$', '', filename)
    name = re.sub(r'\s*-\s*\(\d{4}\)\s*$', '', name)
    return name.strip() or None


def _album_author_score(local_author, candidate):
    """Compare l'auteur extrait du fichier local aux auteurs de l'album Bédéthèque.

    Les rôles sont séparés sur Bédéthèque (scénario/dessin/couleurs) et les noms peuvent
    être inversés ("Hugo Pratt" / "Pratt, Hugo"). Un score de Jaccard sur les mots
    rend ces variantes comparables sans accepter un simple prénom ou nom isolé comme
    preuve suffisante.
    """
    if not local_author:
        return 0.0
    local_tokens = set(BedethequeScraper._normalize_for_match(str(local_author)).split())
    if len(local_tokens) < 2:
        return 0.0
    candidate_tokens = set()
    for field in ('scenario', 'dessin', 'couleurs'):
        candidate_tokens.update(BedethequeScraper._normalize_for_match(candidate.get(field) or '').split())
    if not candidate_tokens:
        return 0.0
    return len(local_tokens & candidate_tokens) / len(local_tokens | candidate_tokens)


def _score_album_candidate(local_volume, candidate):
    """Score titre + auteur, utilisé pour départager des albums partageant un numéro.

    Le titre reste majoritaire; l'auteur est un bonus et ne peut donc pas faire gagner un
    candidat dont le titre est nettement moins ressemblant. Cela exploite les releases
    du type ``Série - T04 - Titre (Auteur)`` sans rendre le nom d'auteur obligatoire.
    """
    local_title = _local_title_from_filename(local_volume.get('filename'))
    title_score = BedethequeScraper._match_score(local_title, candidate.get('title') or '') if local_title else 0.0
    author_score = _album_author_score(local_volume.get('author'), candidate)
    return title_score + (0.25 * author_score), title_score, author_score


def _choose_ambiguous_album_match(matches, local_volume):
    """Départage des albums INT/HS partageant un même numéro technique.

    Bédéthèque peut avoir INT01 et INT1TT, tous deux réduits à ``1`` par le
    parseur. Le premier élément de la liste n'est donc pas une identité fiable.
    L'URL déjà stockée dans ComicInfo prime ; sinon le titre du fichier local est
    comparé aux titres candidats."""
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    try:
        ci = json.loads(local_volume.get('comicinfo') or '{}')
    except (TypeError, ValueError):
        ci = {}
    existing_web = ci.get('web')
    if existing_web:
        for candidate in matches:
            if candidate.get('url') == existing_web:
                return candidate
    local_title = _local_title_from_filename(local_volume.get('filename'))
    if local_title or local_volume.get('author'):
        scored = [(_score_album_candidate(local_volume, c), c) for c in matches]
        scored.sort(key=lambda item: item[0], reverse=True)
        if scored and (scored[0][0][0] >= 0.35 or scored[0][0][2] >= 0.75):
            return scored[0][1]
    # Un placeholder sans fichier/titre ne permet pas de choisir : conserver le
    # candidat stable déjà présent, sans écraser arbitrairement les autres URLs.
    return matches[0]


def match_bedetheque_volume(bd_volumes, local_volume):
    """Retrouve l'album Bedetheque correspondant à un tome local (dict-like avec les
    colonnes volume_number/is_integral/integral_number/is_hs/hs_number/filename de
    `volumes`), ou None si non trouvé. Voir _index_bedetheque_volumes pour le détail du
    matching par numéro.

    Cas particulier du one-shot: un local sans numéro de tome (ni intégrale ni
    hors-série) ne peut pas être retrouvé par numéro puisqu'il n'en a pas - mais
    Bédéthèque ne met pas non plus de 'number' sur l'unique album d'une série one-shot
    (constaté: "À l'intérieur" avait tous ses champs ComicInfo écrits sauf Title, faute
    de bd_volume trouvé). S'il n'y a qu'un seul album listé au total, il n'y a pas
    d'ambiguïté possible: c'est forcément celui-là.

    Cas particulier d'une série ENTIÈREMENT composée d'intégrales (ex: "Ranger Solitaire
    (Intégrale)"): Bédéthèque n'a alors aucune raison de préfixer "INT" (pas de tomes
    normaux à côté desquels les distinguer) - ses albums sont numérotés normalement
    (champ 'number', donc dans by_number) même s'ils s'appellent "L'Intégrale 1",
    "L'Intégrale 2"... Un tome local is_integral=True dont le numéro n'est pas trouvé
    dans by_integral retente donc par_number avant d'abandonner.

    Cas particulier "Épisode" (is_episode): voir _index_bedetheque_volumes - numéro
    d'épisode jamais cherché dans by_number (Tome et Épisode peuvent partager le même
    'number' Bédéthèque sans être le même album, ex. série #70835 "La Bête")."""
    by_number, by_integral, by_hs, by_episode = _index_bedetheque_volumes(bd_volumes)
    if local_volume.get('is_episode'):
        matches = by_episode.get(local_volume.get('episode_number'))
        return matches[0] if matches else None
    if local_volume.get('is_integral'):
        matches = by_integral.get(local_volume.get('integral_number')) or []
        match = _choose_ambiguous_album_match(matches, local_volume)
        if match is None and local_volume.get('integral_number') is not None:
            matches = by_number.get(local_volume.get('integral_number')) or []
            match = _choose_ambiguous_album_match(matches, local_volume)
        return match
    if local_volume.get('is_hs'):
        matches = by_hs.get(local_volume.get('hs_number')) or []
        match = _choose_ambiguous_album_match(matches, local_volume)
        if match is None and local_volume.get('hs_number') is not None:
            matches = by_number.get(local_volume.get('hs_number')) or []
            match = _choose_ambiguous_album_match(matches, local_volume)
        return match
    if local_volume.get('volume_number') is not None:
        matches = by_number.get(local_volume.get('volume_number')) or []
        return _choose_ambiguous_album_match(matches, local_volume)
    if bd_volumes and len(bd_volumes) == 1:
        return bd_volumes[0]

    local_title = _local_title_from_filename(local_volume.get('filename'))
    if local_title and bd_volumes:
        best_match, best_score = None, 0.0
        for bd_vol in bd_volumes:
            if not bd_vol.get('title'):
                continue
            score = _score_album_candidate(local_volume, bd_vol)[0]
            if score > best_score:
                best_match, best_score = bd_vol, score
        if best_match is not None and best_score >= 0.35:
            return best_match

    return None


def _anti_bot_delay():
    """Pause après chaque requête vers Bedetheque pour éviter un bannissement IP - durée
    aléatoire (pas une constante fixe) pour ne pas produire un intervalle parfaitement
    régulier entre requêtes, plus facilement repérable comme trafic automatisé qu'un
    délai qui varie. Cette valeur était montée à 2.5-4.5s (depuis un fixe de 2s) après
    des bannissements constatés en enrichissement de masse (des centaines de tomes
    d'affilée) - redescendue à ~1s sur demande explicite ("tu peux diminuer l'anti-bot
    delay a 1s") pour accélérer le matching (chaque candidat homonyme à départager coûte
    une requête pleine, voir search_and_get_best_match) : risque de bannissement plus
    élevé qu'avant assumé consciemment, à remonter si des blocages réapparaissent."""
    time.sleep(random.uniform(0.8, 1.2))


class BedethequeScraper:
    """Scraper pour rechercher des séries et récupérer leurs infos sur Bedetheque"""

    def __init__(self):
        self.base_url = "https://www.bedetheque.com"
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'fr-FR,fr;q=0.9',
            'Referer': f'{self.base_url}/'
        })
        self.csrf_token = None
        self.cache = {}  # Cache pour éviter les rechutes sur une même série

    def _ensure_session(self):
        """Visite la page d'accueil pour obtenir le cookie de session et le jeton CSRF
        nécessaires à la recherche (voir note anti-bot en tête de fichier)"""
        if self.csrf_token:
            return
        try:
            self.session.get(f'{self.base_url}/', timeout=10)
            self.csrf_token = self.session.cookies.get('csrf_cookie_bel')
            time.sleep(1)
        except Exception as e:
            logger.error(f"Erreur lors de l'initialisation de la session Bedetheque: {e}")

    @staticmethod
    def _reorder_trailing_article(query):
        """
        Les titres locaux stockent parfois l'article en fin de titre pour le tri, sous
        plusieurs conventions: "Chats du Louvre (Les)", "Serpent et la lance, Le" ou
        "Serpent et la lance Le" (la virgule disparaît des noms de dossiers). Bedetheque
        référence la série sous sa forme naturelle ("Le Serpent et la lance") et sa
        recherche ne trouve RIEN avec l'article laissé en fin - on réordonne donc la
        requête dans ces trois cas, sinon on la renvoie telle quelle. En cas de faux
        positif (titre se terminant réellement par un de ces mots), search_series
        retente avec la requête d'origine.
        """
        query = query.strip()
        match = re.match(r"^(.+?)\s*\((Le|La|L'|Les|Un|Une|Du|Des)\)$", query, re.IGNORECASE)
        if not match:
            match = re.match(r"^(.+?),?\s+(Le|La|L'|Les|Un|Une|Du|Des)$", query, re.IGNORECASE)
        if not match:
            return query

        rest = match.group(1).strip().rstrip(',')
        article = match.group(2)
        if not rest:
            return query

        return f"{article}{rest}" if article.lower() == "l'" else f"{article} {rest}"

    def search_series(self, query, limit=20):
        """
        Recherche une série sur Bedetheque (recherche globale, qui remonte les fiches
        séries contrairement à la recherche d'albums)
        Retourne une liste de résultats avec titre, URL et genre

        Essaie d'abord la requête avec l'article final réordonné (voir
        _reorder_trailing_article), puis la requête d'origine, puis - si le titre local
        porte un sous-titre ("Titre - Sous-titre", séparateur le plus courant en pratique,
        voir aussi " / " que Bedetheque utilise lui-même dans ses propres résultats) -
        juste la partie avant le séparateur. La recherche Bedetheque ne fait pas de
        matching flou: une requête complète avec sous-titre ("L'adoption - Une histoire
        d'adoption") peut ne renvoyer STRICTEMENT AUCUN résultat alors que le titre
        principal seul ("L'Adoption") trouve la fiche du premier coup - constaté en
        pratique sur une série restée non matchée après import malgré un titre local
        pourtant correct.
        """
        query = unicodedata.normalize('NFC', query)

        candidates = [self._reorder_trailing_article(query), query.strip()]
        for sep in (' - ', ' / '):
            if sep in query:
                candidates.append(query.split(sep, 1)[0].strip())

        from blueprints.search.routes import ebdz_core_title
        no_suffix = ebdz_core_title(query)
        if no_suffix and no_suffix.lower() != query.strip().lower():
            candidates.append(no_suffix)

        no_punct = re.sub(r'[!?…]+', '', query)
        no_punct = re.sub(r'\s+', ' ', no_punct).strip()
        if no_punct and no_punct.lower() != query.strip().lower():
            candidates.append(no_punct)

        words = query.split()
        truncated = [' '.join(words[:n]) for n in range(len(words) - 1, 1, -1)]

        def _try_all(search_fn):
            seen = set()
            for candidate in candidates + truncated:
                if not candidate or candidate.lower() in seen:
                    continue
                seen.add(candidate.lower())
                results = search_fn(candidate, limit)
                if results:
                    return results
            return []

        # "apres ce qu'on pourrait faire c'est telecharger deja en db toutes l'index des
        # series et chercher prendrait tres peu de temsp" - l'index local (voir
        # catalog_index.py, construit manuellement depuis /settings) est essayé EN
        # PREMIER pour la totalité des variantes ci-dessus, sans le moindre coût réseau/
        # anti-bot. search_catalog_index renvoie None (pas []) tant que l'index n'a
        # jamais été construit - _try_all le traite alors comme "rien trouvé" pour
        # chaque variante et retombe naturellement sur la recherche live juste après,
        # exactement comme si cet essai local n'avait pas eu lieu.
        from blueprints.bedetheque.catalog_index import search_catalog_index
        local_results = _try_all(search_catalog_index)
        if local_results:
            return local_results

        return _try_all(self._search_series_raw)

    def _search_series_raw(self, query, limit=20):
        """Une requête de recherche Bedetheque, telle quelle (sans réécriture du titre)"""
        try:
            self._ensure_session()
            if not self.csrf_token:
                logger.warning("Jeton CSRF Bedetheque indisponible, recherche annulée")
                return []

            logger.info(f"Recherche Bedetheque: {query}")

            params = {'RechTexte': query, 'RechWhere': '1', 'csrf_token_bel': self.csrf_token}
            response = self.session.get(f'{self.base_url}/search/tout', params=params, timeout=10)
            response.encoding = 'utf-8'

            # Pause pour éviter un bannissement IP
            _anti_bot_delay()

            if response.status_code != 200:
                logger.warning(f"Erreur HTTP {response.status_code} pour la recherche")
                return []

            soup = BeautifulSoup(response.content, 'html.parser')

            results = []
            for li in soup.select('ul.nav-liste li'):
                link = li.select_one('a[href*="/serie-"]')
                if not link:
                    continue
                title = link.get_text(strip=True)
                if not title:
                    continue
                genre_el = li.select_one('span.count')
                results.append({
                    'title': title,
                    'url': urljoin(self.base_url, link['href']),
                    'genre': genre_el.get_text(strip=True) if genre_el else None
                })
                if len(results) >= limit:
                    break

            return results

        except Exception as e:
            logger.error(f"Erreur lors de la recherche Bedetheque: {e}")
            return []

    def search_authors(self, query, limit=20):
        """Recherche un auteur par nom sur Bedetheque (item #25 improvement.txt: "add
        album from auteur... search album per authors"). Réutilise EXACTEMENT la même
        requête que _search_series_raw (/search/tout, RechWhere=1) plutôt qu'un second
        endpoint dédié: la page de résultats est déjà multi-sections ("X séries trouvées",
        "X auteurs trouvés"...) en une seule requête - _search_series_raw ne lisait
        jusqu'ici que la section séries (a[href*="/serie-"]), celle-ci lit la section
        auteurs (a[href*="/auteur-"]), présente dans le même ul.nav-liste.

        Retourne [{'name', 'url'}] - le nom déjà au format Bédéthèque "Nom, Prénom" (pas
        reformaté via _reformat_bedetheque_author_name: c'est une liste de choix pour
        l'utilisateur, pas une valeur stockée en base à dédupliquer)."""
        try:
            self._ensure_session()
            if not self.csrf_token:
                logger.warning("Jeton CSRF Bedetheque indisponible, recherche auteur annulée")
                return []

            params = {'RechTexte': query, 'RechWhere': '1', 'csrf_token_bel': self.csrf_token}
            response = self.session.get(f'{self.base_url}/search/tout', params=params, timeout=10)
            response.encoding = 'utf-8'
            _anti_bot_delay()

            if response.status_code != 200:
                logger.warning(f"Erreur HTTP {response.status_code} pour la recherche auteur")
                return []

            soup = BeautifulSoup(response.content, 'html.parser')
            results = []
            seen_urls = set()
            for link in soup.select('a[href*="/auteur-"]'):
                url = urljoin(self.base_url, link['href'])
                if url in seen_urls:
                    continue
                name = link.get_text(strip=True)
                if not name:
                    continue
                seen_urls.add(url)
                results.append({'name': name, 'url': url})
                if len(results) >= limit:
                    break
            return results
        except Exception as e:
            logger.error(f"Erreur lors de la recherche d'auteur Bedetheque: {e}")
            return []

    def get_author_bibliography(self, author_url):
        """Bibliographie d'un auteur (item #25 improvement.txt) - fiche auteur Bédéthèque
        (ex: https://www.bedetheque.com/auteur-123-BD-Pratt-Hugo.html), table "Séries
        principales" uniquement (table.biblio-auteur, la première du document - les
        suivantes, "Autres collaborations"/"Documents, Monographies, Biographies", ne sont
        pas des séries à ajouter à la bibliothèque: la première regroupe des piges dans
        les séries D'AUTRES auteurs, la seconde des livres À PROPOS de l'auteur, pas des
        BD de lui).

        "in bedetheque one author can have album in different language. keep the one in
        french. there is a french flag": chaque ligne porte un drapeau (span.ico img,
        nom de fichier "France.png"/"Italy.png"/...) - is_french posé ici pour que
        l'appelant (route API) puisse filtrer sans reparser le HTML, mais renvoyé pour
        CHAQUE ligne (pas déjà filtré) au cas où l'appelant voudrait un jour voir le reste.

        Retourne [{'title', 'bedetheque_url', 'year_start', 'year_end', 'is_french',
        'flag_country'}].

        "auteur tu peux scraper la biographie pour affiche dans la fenêtre modelale" -
        délibérément PAS implémenté: le texte de biographie Bédéthèque (.bio sur cette
        même page) est un texte éditorial de Bédéthèque, pas une donnée factuelle comme
        un titre/genre - le reproduire (même partiellement) dans cette appli reviendrait
        à copier leur contenu protégé. Le lien vers la fiche auteur (déjà affiché dans la
        modale "Albums de l'auteur", voir author-albums.js) reste le moyen de la lire,
        directement sur Bédéthèque."""
        try:
            self._ensure_session()
            response = self.session.get(author_url, timeout=15)
            response.encoding = 'utf-8'
            _anti_bot_delay()

            if response.status_code != 200:
                logger.warning(f"Erreur HTTP {response.status_code} pour la fiche auteur {author_url}")
                return []

            soup = BeautifulSoup(response.content, 'html.parser')
            table = soup.select_one('table.biblio-auteur')
            if not table:
                return []

            results = []
            seen_urls = set()
            for row in table.select('tbody tr'):
                serie_link = row.select_one('span.serie a[href*="/serie-"]')
                if not serie_link:
                    continue
                serie_url = urljoin(self.base_url, serie_link['href'])
                if serie_url in seen_urls:
                    continue
                seen_urls.add(serie_url)

                flag_img = row.select_one('span.ico img')
                flag_filename = flag_img['src'].split('/')[-1] if flag_img and flag_img.get('src') else ''
                flag_country = os.path.splitext(flag_filename)[0]

                year_cells = row.select('td')
                year_start = year_cells[1].get_text(strip=True) if len(year_cells) > 1 else None
                year_end = year_cells[2].get_text(strip=True) if len(year_cells) > 2 else None

                results.append({
                    'title': serie_link.get_text(strip=True),
                    'bedetheque_url': serie_url,
                    'year_start': int(year_start) if year_start and year_start.isdigit() else None,
                    'year_end': int(year_end) if year_end and year_end.isdigit() else None,
                    'is_french': flag_country == 'France',
                    'flag_country': flag_country or None,
                })
            return results
        except Exception as e:
            logger.error(f"Erreur lors de la récupération de la bibliographie auteur {author_url}: {e}")
            return []

    # Bédéthèque renvoie ce logo générique comme <link rel="image_src"> quand un
    # auteur n'a pas de vraie photo (confirmé sur plusieurs pseudos/collectifs de
    # coloristes, ex: auteur-7691-BD-Quadrichromie.html) - jamais une image cassée/404,
    # donc la seule façon de distinguer "pas de photo" de "a une photo" est de
    # comparer l'URL à ce fichier précis plutôt que de tester le succès du téléchargement.
    _NO_PHOTO_MARKER = 'Logo_BDGest_rel.png'

    def get_author_photo_path(self, author_url, covers_dir=None):
        """Récupère (et télécharge/cache localement) la photo d'un auteur depuis sa
        fiche Bédéthèque - "dans panthéon tu as les icones des auteurs. c'est possible
        de les récupérer en static pour les afficher dans les pages de serie".

        Retourne un chemin relatif servi via /covers/... (voir _download_cover), ou
        '' si Bédéthèque n'a pas de vraie photo pour cet auteur, ou None en cas
        d'erreur réseau (à distinguer de '' par l'appelant: None ne doit pas être mis
        en cache comme un résultat définitif, voir author_photos.photo_path)."""
        if covers_dir is None:
            # _download_cover renvoie toujours un chemin "covers/<filename>" SANS le
            # sous-dossier passé en paramètre (aucun appelant existant n'utilisait de
            # sous-dossier avant celui-ci) - un dossier séparé "covers/authors/" ferait
            # donc pointer le chemin renvoyé vers le mauvais fichier une fois servi via
            # /covers/<path:filename>. Même dossier plat que les couvertures d'albums;
            # les noms de fichiers sont déjà des hash MD5 de l'URL source, la collision
            # entre une couverture d'album et une photo d'auteur est virtuellement nulle.
            covers_dir = "./data/covers"
        try:
            self._ensure_session()
            response = self.session.get(author_url, timeout=15)
            response.encoding = 'utf-8'
            _anti_bot_delay()
            if response.status_code != 200:
                logger.warning(f"Erreur HTTP {response.status_code} pour la fiche auteur {author_url}")
                return None

            soup = BeautifulSoup(response.content, 'html.parser')
            link = soup.select_one('link[rel="image_src"]')
            photo_url = link.get('href', '').strip() if link else ''
            if not photo_url or self._NO_PHOTO_MARKER in photo_url:
                return ''
            return self._download_cover(photo_url, covers_dir) or None
        except Exception as e:
            logger.error(f"Erreur lors de la récupération de la photo auteur {author_url}: {e}")
            return None

    def get_series_info(self, url_or_title, covers_dir=None):
        """
        Récupère les infos détaillées d'une série (et la liste de ses albums)
        Accepte soit une URL complète de fiche série, soit un titre (cherche d'abord)

        Retourne:
        {
            'title': str, 'url': str, 'cover_url': str, 'cover_path': str,
            'genre': str, 'status': str, 'total_volumes': int or None,
            'origin': str, 'language': str, 'year_start': int, 'year_end': int or None,
            'description': str,
            'scenaristes': [str], 'dessinateurs': [str], 'editeurs': [str],
            'volumes': [{
                'id', 'url', 'number', 'title', 'cover_url',
                'scenario', 'dessin', 'couleurs', 'editeur', 'isbn',
                'date_publication', 'pages'
            }]
        }
        """
        if covers_dir is None:
            covers_dir = "./data/covers"
        try:
            if not url_or_title.startswith('http'):
                results = self.search_series(url_or_title, limit=1)
                if not results:
                    logger.warning(f"Aucune série trouvée pour: {url_or_title}")
                    return None
                series_url = results[0]['url']
            else:
                series_url = url_or_title

            if series_url in self.cache:
                return self.cache[series_url]

            # La page de fiche série ne montre par défaut qu'une quinzaine d'albums
            # (pagination). Le suffixe "__10000" correspond au lien "Tout" du site et
            # renvoie l'intégralité des albums de la série sur une seule page, avec le
            # même en-tête (titre, genre, statut...) que la page de base - une seule
            # requête suffit donc pour tout récupérer
            all_url = re.sub(r'\.html$', '__10000.html', series_url)

            logger.info(f"Récupération des infos: {all_url}")

            response = self.session.get(all_url, timeout=15)
            response.encoding = 'utf-8'

            # Pause pour éviter un bannissement IP
            _anti_bot_delay()

            if response.status_code != 200:
                logger.warning(f"Erreur HTTP {response.status_code} pour {all_url}")
                return None

            soup = BeautifulSoup(response.content, 'html.parser')

            info = {
                'title': None,
                'url': series_url,
                'cover_url': None,
                'cover_path': None,
                'genre': None,
                'status': None,
                'total_volumes': None,
                'origin': None,
                'language': None,
                'year_start': None,
                'year_end': None,
                'description': None,
                'scenaristes': [],
                'dessinateurs': [],
                'editeurs': [],
                'author_links': {},
                'volumes': [],
                'related_series': [],
                # Recommandations éditoriales « A lire aussi » de la fiche, distinctes
                # des « Séries liées » de l'univers.
                'read_also': []
            }

            h1 = soup.select_one('.bandeau-info.serie h1 a')
            if h1:
                info['title'] = h1.get_text(strip=True)

            for li in soup.select('ul.serie-info li'):
                label = li.find('label')
                if not label:
                    continue
                label_text = label.get_text(strip=True)
                key = label_text.rstrip(':').strip()
                value = li.get_text(strip=True)[len(label_text):].strip()
                if key == 'Genre':
                    info['genre'] = value or None
                elif key == 'Parution':
                    info['status'] = value or None
                elif key == 'Tomes':
                    info['total_volumes'] = int(value) if value.isdigit() else None
                elif key == 'Origine':
                    info['origin'] = value or None
                elif key == 'Langue':
                    info['language'] = value or None

            h3 = soup.select_one('.bandeau-info.serie h3')
            if h3:
                h3_text = h3.get_text(' ', strip=True)
                year_match = re.search(r'\b(19\d{2}|20\d{2})\s*-\s*(19\d{2}|20\d{2})\b', h3_text)
                if year_match:
                    info['year_start'] = int(year_match.group(1))
                    info['year_end'] = int(year_match.group(2))
                else:
                    single_year = re.search(r'\b(19\d{2}|20\d{2})\b', h3_text)
                    if single_year:
                        info['year_start'] = int(single_year.group(1))

            desc_p = soup.select_one('div.single-content.serie > p')
            if desc_p:
                info['description'] = desc_p.get_text(separator=' ', strip=True)

            cover_img = soup.select_one('div.serie-image img')
            if cover_img and cover_img.get('src'):
                cover_url = urljoin(self.base_url, cover_img['src'])
                info['cover_url'] = cover_url
                try:
                    cover_path = self._download_cover(cover_url, covers_dir)
                    if cover_path:
                        info['cover_path'] = cover_path
                except Exception as e:
                    logger.warning(f"Impossible de télécharger la couverture: {e}")

            # "Séries liées" (widget sidebar, absent sur la plupart des fiches série) -
            # identifié par sa classe `serie-liee` plutôt que le texte du h3 (compteur
            # variable, "5 Séries liées"). Chaque lien répète le href de la miniature -
            # dédupliqué par URL, dans l'ordre d'apparition.
            seen_related_urls = set()
            for a in soup.select('div.tab_content.serie-liee div.description ul li a'):
                related_url = a.get('href')
                if not related_url or related_url in seen_related_urls:
                    continue
                related_title = (a.get('title') or a.get_text(strip=True).lstrip('••').strip())
                if not related_title:
                    continue
                seen_related_urls.add(related_url)
                info['related_series'].append({'title': related_title, 'url': related_url})

            # « A lire aussi » apparaît dans le bloc `.alire` sous l'ancre `aussi`.
            # Les recommandations sont des liens de fiches série avec le titre dans
            # l'attribut title et la couverture dans l'image; on garde aussi l'image
            # pour que la modale reste utile sans nouvelle requête par recommandation.
            seen_read_also_urls = set()
            for a in soup.select('div.alire ul#wrapper-ul li a[href*="/serie-"]'):
                read_also_url = a.get('href')
                if not read_also_url:
                    continue
                read_also_url = urljoin(self.base_url, read_also_url.strip())
                if read_also_url in seen_read_also_urls:
                    continue
                image = a.find('img')
                read_also_title = (a.get('title') or (image.get('alt') if image else '') or a.get_text(strip=True)).strip()
                if not read_also_title:
                    continue
                seen_read_also_urls.add(read_also_url)
                read_also_cover = image.get('src') if image and image.get('src') else None
                info['read_also'].append({
                    'title': read_also_title,
                    'url': read_also_url,
                    'cover_url': urljoin(self.base_url, read_also_cover) if read_also_cover else None
                })

            scenaristes, dessinateurs, editeurs = [], [], []
            # {nom: url fiche auteur} - item #25 improvement.txt, voir _author_link_from_span.
            # Un même nom peut apparaître sur plusieurs tomes (voir _reformat_bedetheque_
            # author_name pour le format "Prénom Nom"), on garde la première URL trouvée.
            author_links = {}
            for album_li in soup.select('ul.liste-albums li[itemtype="https://schema.org/Book"]'):
                volume = self._parse_volume(album_li)
                if not volume:
                    continue
                info['volumes'].append(volume)
                if volume['scenario'] and volume['scenario'] not in scenaristes:
                    scenaristes.append(volume['scenario'])
                if volume['dessin'] and volume['dessin'] not in dessinateurs:
                    dessinateurs.append(volume['dessin'])
                if volume['editeur'] and volume['editeur'] not in editeurs:
                    editeurs.append(volume['editeur'])
                for name, url in (
                    (volume['scenario'], volume['scenario_url']),
                    (volume['dessin'], volume['dessin_url']),
                    (volume['couleurs'], volume['couleurs_url']),
                ):
                    if name and url and name not in author_links:
                        author_links[name] = url

            info['scenaristes'] = scenaristes
            info['dessinateurs'] = dessinateurs
            info['editeurs'] = editeurs
            info['author_links'] = author_links

            if info['total_volumes'] is None:
                count_icon = soup.select_one('i.icon-book')
                if count_icon and count_icon.parent:
                    count_match = re.search(r'(\d{1,4})', count_icon.parent.get_text(strip=True))
                    if count_match:
                        info['total_volumes'] = int(count_match.group(1))

            # Un one-shot n'a souvent pas de résumé sur sa page "série" (le <p> existe
            # mais reste vide) - le vrai résumé est sur la page de l'unique album, dans
            # <span itemprop="description">. On ne va le chercher que dans ce cas précis
            # (un seul album) pour ne pas multiplier les requêtes sur les séries à tomes
            if not info['description'] and len(info['volumes']) == 1:
                info['description'] = self.fetch_album_description(info['volumes'][0].get('url'))

            self.cache[series_url] = info

            logger.debug(f"Infos extraites: {info['title']} - {len(info['volumes'])} albums")

            return info

        except Exception as e:
            logger.error(f"Erreur lors de la récupération des infos Bedetheque: {e}")
            return None

    def get_series_url_from_album_url(self, album_url):
        """Retrouve l'URL de la fiche série à partir de l'URL d'un album précis (ex:
        ComicInfo <Web> déjà écrit dans un tome). Utilisé quand une série n'a pas encore
        de bedetheque_url mais qu'un de ses tomes a déjà un lien Bédéthèque associé -
        pour matcher via ce lien exact plutôt que de risquer une recherche automatique
        par titre qui peut se tromper (titre ambigu, homonymes...) ou ne rien trouver."""
        if not album_url:
            return None

        try:
            response = self.session.get(album_url, timeout=15)
            response.encoding = 'utf-8'
            _anti_bot_delay()

            if response.status_code != 200:
                return None

            soup = BeautifulSoup(response.content, 'html.parser')
            link = soup.select_one('a[href*="/serie-"]')
            if link and link.get('href'):
                return urljoin(self.base_url, link['href'])
            return None

        except Exception as e:
            logger.warning(f"Impossible de retrouver la série depuis l'album {album_url}: {e}")
            return None

    def fetch_album_description(self, album_url):
        """Récupère le résumé d'un album depuis sa propre page (<span itemprop="description">).
        Utilisé en repli quand la page série n'en a pas (cas fréquent des one-shots), et par
        les routes update-metadata pour écrire un Summary propre à chaque tome (la page
        série n'a souvent aucun résumé - ou un seul, identique pour tous les tomes)"""
        if not album_url:
            return ''

        try:
            response = self.session.get(album_url, timeout=15)
            response.encoding = 'utf-8'
            _anti_bot_delay()

            if response.status_code != 200:
                return ''

            soup = BeautifulSoup(response.content, 'html.parser')
            desc_span = soup.select_one('span[itemprop="description"]')
            if not desc_span:
                return ''

            # Le résumé est tronqué par un lien "Lire la suite" ajouté par le site en JS
            # (masqué par défaut) : on le retire pour ne garder que le texte réel
            more_link = desc_span.select_one('a.more-resume')
            if more_link:
                more_link.decompose()

            return desc_span.get_text(separator=' ', strip=True)

        except Exception as e:
            logger.warning(f"Impossible de récupérer le résumé de l'album {album_url}: {e}")
            return ''

    def get_album_reviews(self, album_url):
        """Récupère les avis de lecteurs publiés sur la page d'un album ("L'avis des
        visiteurs", <ol class="commentlist"><div class="the-comment" itemprop="reviews"
        itemtype="https://schema.org/Review">...) - texte intégral, pas juste la note
        agrégée déjà récupérée par ailleurs (voir _index_bedetheque_volumes/'rating').
        Une page d'album sans aucun avis n'a pas ce bloc du tout (pas de <ol> vide) -
        retourne simplement []."""
        if not album_url:
            return []

        try:
            response = self.session.get(album_url, timeout=15)
            response.encoding = 'utf-8'
            _anti_bot_delay()

            if response.status_code != 200:
                return []

            soup = BeautifulSoup(response.content, 'html.parser')
            reviews = []
            for comment in soup.select('.commentlist .the-comment'):
                author_el = comment.select_one('.comment-author .name')
                date_meta = comment.select_one('.comment-author meta[itemprop="datePublished"]')
                rating_meta = comment.select_one('meta[itemprop="reviewRating"]')
                body_el = comment.select_one('.comment-text [itemprop="reviewBody"]')
                if not body_el:
                    continue

                # <br> dans le texte de l'avis -> retour à la ligne réel plutôt qu'espace
                # (get_text seul les avalerait silencieusement)
                for br in body_el.find_all('br'):
                    br.replace_with('\n')

                rating = None
                if rating_meta and rating_meta.get('content'):
                    try:
                        rating = int(rating_meta['content'])
                    except ValueError:
                        rating = None

                reviews.append({
                    'author': author_el.get_text(strip=True) if author_el else None,
                    'date': date_meta['content'] if date_meta and date_meta.get('content') else None,
                    'rating': rating,
                    'text': body_el.get_text(strip=True),
                })

            return reviews

        except Exception as e:
            logger.warning(f"Impossible de récupérer les avis de l'album {album_url}: {e}")
            return []

    def _parse_volume(self, li):
        """Parse un <li> d'album de la liste 'liste-albums' d'une fiche série"""
        try:
            volume = {
                'id': None,
                'url': None,
                'number': None,
                'bis_suffix': None,
                'title': None,
                'cover_url': None,
                'scenario': None,
                'scenario_url': None,
                'dessin': None,
                'dessin_url': None,
                'couleurs': None,
                'couleurs_url': None,
                'editeur': None,
                'isbn': None,
                'date_publication': None,
                'pages': None,
                'rating': None,
                'rating_count': None
            }

            anchor = li.find('a', attrs={'name': True})
            if anchor:
                volume['id'] = anchor.get('name')

            title_link = li.select_one('h3 a.titre')
            if title_link and title_link.get('href'):
                volume['url'] = urljoin(self.base_url, title_link['href'])

            name_span = li.select_one('span[itemprop="name"]')
            if name_span:
                full_text = re.sub(r'\s+', ' ', name_span.get_text(' ', strip=True)).strip()
                num_match = re.match(r'^(\d+)\s*\.\s+(.*)$', full_text)
                if num_match:
                    volume['number'] = int(num_match.group(1))
                    volume['title'] = num_match.group(2).strip() or None
                else:
                    # "13 bis is not special it is part of the album" / "special have
                    # usually a different colour in bedetheque liste" - Bédéthèque
                    # distingue structurellement une variante "N Bis" (garde un numéro
                    # de tome normal N, SEUL le suffixe "Bis" est dans <span
                    # class="numa">) d'un VRAI spécial (BOBD, HS04TL1... - le libellé
                    # ENTIER est dans .numa, aucun numéro devant) - voir CLAUDE.md. "N
                    # Bis" échoue le regex ci-dessus car son texte aplati est "N Bis .
                    # Titre" (le "." n'est plus immédiatement après le chiffre, "Bis"
                    # s'intercale) - une fois .numa retiré d'une COPIE du nœud (jamais
                    # l'original: un vrai spécial doit garder son code complet dans le
                    # titre retourné, d'autres parseurs comme _parse_special_prefix/
                    # _parse_int_hs_prefix le lisent depuis là), le texte redevient "N .
                    # Titre", identique à un tome classique.
                    numa_span = name_span.select_one('span.numa')
                    numa_text = re.sub(r'\s+', ' ', numa_span.get_text(strip=True)).strip() if numa_span else ''
                    bis_number = None
                    if numa_span and numa_text:
                        clone = copy.copy(name_span)
                        clone_numa = clone.select_one('span.numa')
                        if clone_numa:
                            clone_numa.extract()
                        text_without_numa = re.sub(r'\s+', ' ', clone.get_text(' ', strip=True)).strip()
                        bis_match = re.match(r'^(\d+)\s*\.\s+(.*)$', text_without_numa)
                        if bis_match:
                            bis_number = int(bis_match.group(1))
                            volume['title'] = bis_match.group(2).strip() or None
                    if bis_number is not None:
                        volume['number'] = bis_number
                        volume['bis_suffix'] = numa_text
                    else:
                        volume['title'] = full_text or None

            cover_img = li.select_one('.couv img')
            if cover_img and cover_img.get('src'):
                volume['cover_url'] = urljoin(self.base_url, cover_img['src'])

            for info_li in li.select('ul.infos li'):
                label = info_li.find('label')
                if not label:
                    continue
                key = label.get_text(strip=True).rstrip(':').strip().lower()
                if key == 'scénario':
                    span = info_li.select_one('span[itemprop="author"]')
                    volume['scenario'] = _reformat_bedetheque_author_name(span.get_text(strip=True)) if span else None
                    volume['scenario_url'] = _author_link_from_span(span)
                elif key == 'dessin':
                    span = info_li.select_one('span[itemprop="illustrator"]')
                    volume['dessin'] = _reformat_bedetheque_author_name(span.get_text(strip=True)) if span else None
                    volume['dessin_url'] = _author_link_from_span(span)
                elif key == 'couleurs':
                    span = info_li.select_one('span[itemprop="illustrator"]')
                    volume['couleurs'] = _reformat_bedetheque_author_name(span.get_text(strip=True)) if span else None
                    volume['couleurs_url'] = _author_link_from_span(span)
                elif key == 'editeur':
                    span = info_li.select_one('span[itemprop="publisher"]')
                    volume['editeur'] = span.get_text(strip=True) if span else None
                elif key == 'isbn':
                    span = info_li.select_one('span[itemprop="isbn"]')
                    volume['isbn'] = span.get_text(strip=True) if span else None
                elif key == 'planches':
                    span = info_li.select_one('span[itemprop="numberOfPages"]')
                    volume['pages'] = span.get_text(strip=True) if span else None

            meta_date = li.select_one('meta[itemprop="datePublished"]')
            if meta_date and meta_date.get('content'):
                volume['date_publication'] = meta_date['content']

            # Note des lecteurs ("parse les notes des volumes bedetheque pour recuperer
            # la note / nb review") - déjà présente sur CETTE MÊME page (fiche série,
            # un <p class="message"> par album dans son .ratingblock, ex. "Note: <strong>
            # 3.9</strong>/5 (114 votes)") : aucune requête supplémentaire nécessaire,
            # contrairement au résumé (fetch_album_description) qui lui exige la page de
            # l'album. Absent (album jamais noté) plutôt qu'une erreur si la structure
            # attendue ne s'y trouve pas.
            rating_msg = li.select_one('.ratingblock p.message')
            if rating_msg:
                strong = rating_msg.find('strong')
                if strong and strong.get_text(strip=True):
                    try:
                        volume['rating'] = float(strong.get_text(strip=True).replace(',', '.'))
                    except ValueError:
                        pass
                count_match = re.search(r'\(([\d\s \xa0]+)\s*votes?\)', rating_msg.get_text())
                if count_match:
                    digits = re.sub(r'[^\d]', '', count_match.group(1))
                    if digits:
                        volume['rating_count'] = int(digits)

            return volume

        except Exception as e:
            logger.warning(f"Erreur lors du parsing d'un album: {e}")
            return None

    def _download_cover(self, cover_url, covers_dir):
        """
        Télécharge la couverture et la sauvegarde localement

        Returns:
            str: Chemin relatif du fichier téléchargé, ou None en cas d'erreur
        """
        try:
            Path(covers_dir).mkdir(parents=True, exist_ok=True)

            # Nom de fichier unique basé sur le hash de l'URL, en gardant l'extension d'origine
            url_hash = hashlib.md5(cover_url.encode()).hexdigest()
            file_ext = os.path.splitext(cover_url.split('?')[0])[1] or '.jpg'
            filename = f"{url_hash}{file_ext}"
            filepath = os.path.join(covers_dir, filename)

            if os.path.exists(filepath):
                return f"covers/{filename}"

            response = self.session.get(cover_url, timeout=10)
            if response.status_code == 200:
                with open(filepath, 'wb') as f:
                    f.write(response.content)
                logger.info(f"Couverture téléchargée: {filename}")
                return f"covers/{filename}"
            else:
                logger.warning(f"Erreur téléchargement couverture: HTTP {response.status_code}")
                return None

        except Exception as e:
            logger.error(f"Erreur lors du téléchargement de la couverture: {e}")
            return None

    @staticmethod
    def _normalize_for_match(text):
        """Réduit un titre à ses mots significatifs pour comparaison: accents, casse et
        ponctuation ignorés, "/" et "-" traités comme des séparateurs de mots équivalents
        (voir search_series - le tiret local et la barre oblique Bedetheque désignent la
        même chose dans "Titre (Auteur1-Auteur2)" vs "Titre (Auteur1/Auteur2)").

        "[]" au même titre que "()" ("usually taking from ebdz it add [author]") - sans
        ça, "Titre [Murawiec]" (crochets, convention EBDZ) normalisait en "titre
        [murawiec]" (crochets gardés tels quels) alors que le candidat Bedetheque "Titre
        (Murawiec)" normalisait en "titre murawiec" (parenthèses retirées) - le jeu de
        mots ne partageait donc jamais "murawiec"/"[murawiec]" comme un mot commun, et
        deux candidats homonymes ne différant que par l'auteur (ex: "Grand vide (Munoz)"
        vs "Grand vide (Murawiec)") finissaient à score EXACTEMENT égal, le nom d'auteur
        ne comptant plus du tout dans la comparaison - le tri stable retombait alors sur
        le premier de la liste Bedetheque, pas nécessairement le bon."""
        import unicodedata
        text = unicodedata.normalize('NFKD', text or '')
        text = ''.join(c for c in text if not unicodedata.combining(c))
        text = text.lower()
        text = re.sub(r"[/\-,.:!?…'’‘\"()\[\]]", ' ', text)
        return re.sub(r'\s+', ' ', text).strip()

    @staticmethod
    def _strip_trailing_annotation(title):
        ""
        return re.sub(r'\s*[(\[][^)\]]*[)\]]\s*$', '', title or '').strip()

    @classmethod
    def _bedetheque_evidence_tokens(cls, info):
        """Mots normalisés identifiant CETTE fiche précise (auteurs, éditeur, année de
        début, plus titre/année de chaque album) - departage des séries homonymes que le
        titre seul ne peut pas distinguer (voir search_and_get_best_match). Réutilise
        entièrement des champs déjà renvoyés par get_series_info, aucune requête HTTP
        supplémentaire par candidat au-delà de la fiche elle-même."""
        parts = list(info.get('scenaristes') or []) + list(info.get('dessinateurs') or []) + list(info.get('editeurs') or [])
        if info.get('year_start'):
            parts.append(str(info['year_start']))
        for volume in info.get('volumes') or []:
            if volume.get('title'):
                parts.append(volume['title'])
            if volume.get('date_publication'):
                parts.append(str(volume['date_publication'])[:4])
        tokens = set()
        for part in parts:
            tokens |= set(cls._normalize_for_match(part).split())
        return tokens

    @classmethod
    def _match_score(cls, query, candidate_title):
        """Similarité entre la requête et le titre d'un candidat - 1.0 pour un titre
        identique (à l'accentuation/ponctuation/ordre des mots près), 0 si aucun mot en
        commun.

        Deux passes:
        1. Egalité stricte une fois TOUS les espaces retirés (pas seulement la
           ponctuation) - rattrape un titre local qui colle plusieurs mots sans
           séparateur (constaté: "Virus (RicardRica)" en local pour "Virus
           (Ricard/Rica)" sur Bedetheque). Un score par ensemble de mots serait trompé
           ici: le token "ricardrica" ne matche ni "ricard" ni "rica" pris séparément,
           et un candidat plus court sans rapport ("Virus (Cornelis)") gagnerait
           artificiellement par une plus petite union - alors que la comparaison sans
           aucun espace les retrouve identiques.
        2. Sinon, indice de Jaccard par ensemble de mots (ordre/nombre d'occurrences
           ignorés)."""
        q_norm = cls._normalize_for_match(query)
        c_norm = cls._normalize_for_match(candidate_title)
        if not q_norm or not c_norm:
            return 0.0

        if q_norm.replace(' ', '') == c_norm.replace(' ', ''):
            return 1.0

        q_base_norm = cls._normalize_for_match(cls._strip_trailing_annotation(query))
        c_base_norm = cls._normalize_for_match(cls._strip_trailing_annotation(candidate_title))
        if q_base_norm and c_base_norm and q_base_norm.replace(' ', '') == c_base_norm.replace(' ', ''):
            return 1.0

        q_tokens = set(q_norm.split())
        c_tokens = set(c_norm.split())
        return len(q_tokens & c_tokens) / len(q_tokens | c_tokens)

    @classmethod
    def _is_confident_series_match(cls, query, candidate_title, score):
        """Indique si un résultat Bédéthèque peut être choisi sans validation humaine.

        Un seul mot partagé n'est pas une identité de série : « Mes P'tits Docs Paris
        Stéphanie Ledu… » avait ainsi été associé à « Mes petits vieux » parce que le
        précédent appelant acceptait tout score strictement positif (0,077 dans ce cas).
        Le score Jaccard reste utile pour classer les résultats, mais il est complété par
        la couverture des mots distinctifs du candidat. Les mots-outils français ne sont
        jamais une preuve et les métadonnées ajoutées à un nom de fichier peuvent rester
        dans la requête : « Mes P'tits Docs Paris Stéphanie Ledu… » couvre bien les mots
        de « Mes P'tits Docs Paris », malgré son bruit de release.
        """
        if score < 0.30:
            return False

        ignored_tokens = {
            'a', 'ai', 'au', 'aux', 'ce', 'ces', 'cet', 'cette', 'd', 'de', 'des',
            'du', 'elle', 'en', 'et', 'il', 'ils', 'j', 'je', 'l', 'la', 'le', 'les',
            'ma', 'mes', 'mon', 'n', 'ne', 'nos', 'notre', 'on', 'ou', 'par', 'pour',
            'sa', 'se', 'ses', 'son', 'sur', 't', 'ta', 'te', 'tes', 'toi', 'ton',
            'tu', 'un', 'une', 'vos', 'votre', 'y',
        }
        query_tokens = {
            token for token in cls._normalize_for_match(query).split()
            if len(token) > 1 and token not in ignored_tokens
        }
        candidate_tokens = {
            token for token in cls._normalize_for_match(
                cls._strip_trailing_annotation(candidate_title)
            ).split()
            if len(token) > 1 and token not in ignored_tokens
        }
        if not candidate_tokens:
            return False
        return len(query_tokens & candidate_tokens) / len(candidate_tokens) >= 0.75

    def search_and_get_best_match(self, title, raw_hint=None):
        """Cherche une série et retourne les infos du meilleur résultat.

        Les candidats sont classés par similarité de titre normalisée avec la requête
        (voir _match_score) plutôt que de prendre le premier de la liste dont la fiche
        charge: Bedetheque classe parfois un spin-off/dérivé avant la série elle-même
        (ex: chercher "Kenya" renvoyait en premier "Amazonie (Kenya - Saison 3)" alors que
        la fiche "Kenya" exacte était aussi dans les résultats, plus bas).

        Un résultat est renvoyé uniquement s'il franchit le seuil de confiance et couvre
        au moins 75 % de ses mots distinctifs dans la requête. Une requête courte ou
        bruitée qui ne partage qu'un mot générique avec une fiche ne doit jamais créer
        une mauvaise série automatiquement : elle est envoyée vers la validation manuelle
        par l'appelant.

        limit=30 (pas 5): Bedetheque classe ses résultats par pertinence à lui - un
        titre exact peut se retrouver loin dans la liste derrière des dizaines de
        dérivés/collections quand le mot recherché est courant ("Portugal" trouvé en
        position 12, "Incroyable !" en position 20+ sur des requêtes qui en renvoient
        20-40) - _search_series_raw parse déjà toute la page de résultats en une seule
        requête HTTP, augmenter la limite ne coûte donc rien en requêtes/rate-limit,
        seulement plus de candidats à comparer localement par _match_score.

        raw_hint: texte source plus riche que `title` (ex: le nom de fichier ORIGINAL,
        avant que parse_filename n'ait jeté auteur/éditeur/année dans des champs séparés
        et ne garde que le titre) - "L'autre_T03_Jaalab_Lilian_Martin_Glénat_2024@..."
        matchait "L'autre (Manù)" au lieu de "L'autre (Lylian/Martín)" (la vraie série,
        Glénat/2024, "Lilian_Martin" ~ scénariste Lylian + dessinatrice Montse Martín) :
        deux séries homonymes ne différant QUE par leur groupe d'auteur final pénalisent
        structurellement par _match_score le candidat qui a le PLUS de mots d'auteur
        (Jaccard), quel que soit lequel est réellement le bon - "the match is evident with
        the author and name... does not make sense" à raison. Détecté ici en regroupant
        les candidats par titre de base (voir _strip_trailing_annotation) : seulement
        pour CE cas précis (homonymes réels, pas juste un score serré), départagé par la
        vraie fiche de chacun (auteurs/éditeur/année/titres d'albums - voir
        _bedetheque_evidence_tokens) comparée à raw_hint plutôt qu'au titre déjà nettoyé.
        Sans preuve distinctive (aucun homonyme n'a de mot en commun avec raw_hint, ou
        plusieurs sont à égalité), ne devine toujours pas - même philosophie que le score
        nul ci-dessus. raw_hint absent (défaut None, replié sur `title`): comportement
        inchangé pour les appelants qui n'ont rien de plus riche à fournir."""
        results = self.search_series(title, limit=30)

        if not results:
            return None

        scored = sorted(results, key=lambda r: self._match_score(title, r['title']), reverse=True)
        # Ne pas rejeter toute la recherche sur le premier résultat seulement : un titre
        # court peut obtenir un Jaccard un peu plus haut par hasard, alors qu'un candidat
        # légèrement plus bas couvre réellement tous ses mots distinctifs. Seuls les
        # candidats validés restent donc en lice pour le départage homonyme ci-dessous.
        scored = [
            result for result in scored
            if self._is_confident_series_match(
                title, result['title'], self._match_score(title, result['title'])
            )
        ]
        if not scored:
            return None

        base_key = self._normalize_for_match(self._strip_trailing_annotation(scored[0]['title']))
        homonyms = [
            c for c in scored
            if self._normalize_for_match(self._strip_trailing_annotation(c['title'])) == base_key
        ]
        MAX_HOMONYM_FETCHES = 3
        if len(homonyms) > MAX_HOMONYM_FETCHES:
            homonyms = homonyms[:MAX_HOMONYM_FETCHES]
        if len(homonyms) > 1:
            # "_" n'est jamais un séparateur de mots pour un titre Bédéthèque (jamais
            # nettoyé par _normalize_for_match, à raison - inutile là-bas) mais L'EST pour
            # un nom de fichier Telegram source de raw_hint ("...T03_Jaalab_Lilian_Martin_
            # Glénat_2024@...") : sans ce remplacement, tout le nom de fichier reste collé
            # en UN SEUL token qui ne peut matcher aucun mot d'auteur/éditeur - le premier
            # essai réel de ce correctif a échoué exactement pour cette raison. "@canal" et
            # l'extension retirés pour la même raison (bruit qui ne peut jamais correspondre
            # à rien côté Bédéthèque).
            hint_text = re.sub(r'\.[a-zA-Z0-9]{2,4}$', '', re.sub(r'@.*$', '', raw_hint or title)).replace('_', ' ')
            hint_tokens = set(self._normalize_for_match(hint_text).split())
            best_info, best_overlap, tie = None, 0, False
            for candidate in homonyms:
                info = self.get_series_info(candidate['url'])
                if not info:
                    continue
                overlap = len(hint_tokens & self._bedetheque_evidence_tokens(info))
                if overlap > best_overlap:
                    best_info, best_overlap, tie = info, overlap, False
                elif overlap == best_overlap and overlap > 0:
                    tie = True
            return best_info if (best_info and not tie) else None

        for result in scored:
            info = self.get_series_info(result['url'])
            if info:
                return info

        return None


class BedethequeDatabase:
    """Gère la sauvegarde des infos Bedetheque dans la BDD"""

    BEDETHEQUE_COLUMNS = [
        ('bedetheque_url', 'TEXT'),
        ('bedetheque_cover_path', 'TEXT'),
        ('bedetheque_description', 'TEXT'),
        ('bedetheque_genre', 'TEXT'),
        ('bedetheque_status', 'TEXT'),
        ('bedetheque_total_volumes', 'INTEGER'),
        ('bedetheque_origin', 'TEXT'),
        ('bedetheque_language', 'TEXT'),
        ('bedetheque_scenaristes', 'TEXT'),
        ('bedetheque_dessinateurs', 'TEXT'),
        ('bedetheque_editeurs', 'TEXT'),
        ('bedetheque_year_start', 'INTEGER'),
        ('bedetheque_year_end', 'INTEGER'),
        ('bedetheque_updated_at', 'TIMESTAMP'),
        # Copie complète (JSON) de la liste des albums telle que scrapée
        # (get_series_info()['volumes']: number/title/description/date_publication/
        # scenario/dessin/couleurs/editeur/isbn/pages/url par album) - la DB doit être
        # la référence absolue, y compris pour un tome qui n'a jamais eu de ligne
        # `volumes` propre (jamais possédé) ou dont la ligne a été supprimée/recréée
        # (remplacement à l'import) : sans cette copie au niveau série, ces données
        # par album n'existaient QUE dans volumes.comicinfo d'un tome donné, perdues
        # dès que sa ligne disparaissait sans précaution (voir docs/database.md).
        ('bedetheque_albums', 'TEXT'),
        # Sélection Bédéthèque « A lire aussi » (JSON {title,url,cover_url}) -
        # conservée avec la fiche pour afficher la recommandation sans refaire une
        # requête réseau à chaque ouverture de la série.
        ('bedetheque_read_also', 'TEXT'),
        # {nom: url fiche auteur Bédéthèque} en JSON (voir get_series_info()['author_links']
        # / _author_link_from_span) - item #25 improvement.txt, "click on author will open
        # a modal for other album from the same author": bedetheque_scenaristes/
        # dessinateurs ci-dessus ne gardent que le TEXTE du nom, jamais assez pour retrouver
        # la fiche auteur au clic.
        ('bedetheque_author_links', 'TEXT'),
        ('bedetheque_complete', 'INTEGER'),
        ('bedetheque_complete_reason', 'TEXT'),
        ('universe_id', 'INTEGER'),
    ]

    def __init__(self, db_path):
        self.db_path = db_path
        self.init_database()

    def init_database(self):
        """Initialise le schéma Bedetheque dans la BDD"""
        try:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            cursor = conn.cursor()

            cursor.execute("PRAGMA table_info(series)")
            existing_columns = {row[1] for row in cursor.fetchall()}

            for col_name, col_type in self.BEDETHEQUE_COLUMNS:
                if col_name not in existing_columns:
                    try:
                        cursor.execute(f"ALTER TABLE series ADD COLUMN {col_name} {col_type}")
                        logger.info(f"Colonne {col_name} créée")
                    except sqlite3.OperationalError as e:
                        logger.warning(f"Erreur création colonne {col_name}: {e}")

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS universes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS universe_series (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    universe_id INTEGER NOT NULL REFERENCES universes(id) ON DELETE CASCADE,
                    bedetheque_url TEXT NOT NULL UNIQUE,
                    title TEXT,
                    series_id INTEGER REFERENCES series(id) ON DELETE SET NULL
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS author_photos (
                    author_url TEXT PRIMARY KEY,
                    photo_path TEXT,
                    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            conn.commit()
            conn.close()
            logger.info("Base de données Bedetheque initialisée")

        except Exception as e:
            logger.error(f"Erreur lors de l'initialisation de la BDD: {e}")

    def update_series_bedetheque_info(self, series_id, bedetheque_info):
        """Met à jour les infos Bedetheque d'une série"""
        try:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            cursor = conn.cursor()

            cursor.execute('''
                UPDATE series SET
                    bedetheque_url = ?,
                    bedetheque_cover_path = ?,
                    bedetheque_description = ?,
                    bedetheque_genre = ?,
                    bedetheque_status = ?,
                    bedetheque_total_volumes = ?,
                    bedetheque_origin = ?,
                    bedetheque_language = ?,
                    bedetheque_scenaristes = ?,
                    bedetheque_dessinateurs = ?,
                    bedetheque_editeurs = ?,
                    bedetheque_year_start = ?,
                    bedetheque_year_end = ?,
                    bedetheque_albums = ?,
                    bedetheque_read_also = ?,
                    bedetheque_author_links = ?,
                    bedetheque_updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (
                bedetheque_info.get('url'),
                bedetheque_info.get('cover_path'),
                bedetheque_info.get('description') or None,
                bedetheque_info.get('genre'),
                bedetheque_info.get('status'),
                bedetheque_info.get('total_volumes'),
                bedetheque_info.get('origin'),
                bedetheque_info.get('language'),
                ', '.join(bedetheque_info.get('scenaristes') or []) or None,
                ', '.join(bedetheque_info.get('dessinateurs') or []) or None,
                ', '.join(bedetheque_info.get('editeurs') or []) or None,
                bedetheque_info.get('year_start'),
                bedetheque_info.get('year_end'),
                # Copie brute de la liste des albums scrapés (voir BEDETHEQUE_COLUMNS
                # ci-dessus) - la DB doit rester la référence même pour un tome sans
                # ligne `volumes` propre
                json.dumps(bedetheque_info.get('volumes')) if bedetheque_info.get('volumes') else None,
                json.dumps(bedetheque_info.get('read_also')) if bedetheque_info.get('read_also') is not None else None,
                json.dumps(bedetheque_info.get('author_links')) if bedetheque_info.get('author_links') else None,
                series_id
            ))

            rows_affected = cursor.rowcount
            if rows_affected == 0:
                logger.error(f"Aucune ligne affectée - série #{series_id} non trouvée ?")
                conn.close()
                return False

            conn.commit()
            conn.close()

            logger.info(f"✓ Infos Bedetheque mises à jour pour série #{series_id}")

            self.sync_series_universe(series_id, bedetheque_info)

            return True

        except Exception as e:
            logger.error(f"Erreur lors de la mise à jour Bedetheque: {e}", exc_info=True)
            return False

    def sync_series_universe(self, series_id, bedetheque_info):
        ""
        related = bedetheque_info.get('related_series') or []
        series_url = bedetheque_info.get('url')

        try:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            # The shared folder-reconciliation helper reads named columns; keep
            # positional access valid too (sqlite3.Row supports both styles).
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            if series_url:
                cursor.execute(
                    'UPDATE universe_series SET series_id = NULL WHERE series_id = ? AND bedetheque_url != ?',
                    (series_id, series_url)
                )
            else:
                cursor.execute('UPDATE universe_series SET series_id = NULL WHERE series_id = ?', (series_id,))

            if not related or not series_url:
                # Plus aucune "série liée" pour la fiche actuellement matchée - cette
                # série ne fait plus partie d'aucun univers connu, quel que soit son
                # ancien universe_id (jamais remis à NULL avant ce correctif).
                cursor.execute('UPDATE series SET universe_id = NULL WHERE id = ?', (series_id,))
                conn.commit()
                try:
                    from blueprints.library.routes import _move_series_folder_for_universe
                    result = _move_series_folder_for_universe(conn, series_id)
                    if not result.get('success'):
                        logger.warning(f"Déplacement après retrait d'univers échoué pour série #{series_id}: {result}")
                except Exception as exc:
                    logger.warning(f"Déplacement après retrait d'univers échoué pour série #{series_id}: {exc}")
                conn.close()
                return

            members = {series_url: bedetheque_info.get('title')}
            for r in related:
                if r.get('url'):
                    members[r['url']] = r.get('title')
            all_urls = list(members.keys())

            placeholders = ','.join('?' * len(all_urls))
            cursor.execute(
                f'SELECT universe_id FROM universe_series WHERE bedetheque_url IN ({placeholders}) LIMIT 1',
                all_urls
            )
            existing = cursor.fetchone()

            if existing:
                universe_id = existing[0]
            else:
                cursor.execute(
                    'INSERT INTO universes (name) VALUES (?)',
                    (bedetheque_info.get('title') or 'Univers',)
                )
                universe_id = cursor.lastrowid

            for url, title in members.items():
                cursor.execute('SELECT id FROM series WHERE bedetheque_url = ?', (url,))
                matched = cursor.fetchone()
                matched_series_id = matched[0] if matched else None

                cursor.execute('''
                    INSERT INTO universe_series (universe_id, bedetheque_url, title, series_id)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(bedetheque_url) DO UPDATE SET
                        title = excluded.title,
                        series_id = excluded.series_id
                ''', (universe_id, url, title, matched_series_id))

                if matched_series_id:
                    cursor.execute('UPDATE series SET universe_id = ? WHERE id = ?', (universe_id, matched_series_id))

            conn.commit()
            # Universe detection can occur after a series has already been created at
            # the library root. Reconcile its persisted folder immediately so creation,
            # manual assignment and later Bédéthèque enrichment share one layout rule.
            try:
                from blueprints.library.routes import _move_series_folder_for_universe
                for matched_series_id in {row[2] for row in cursor.execute(
                    'SELECT universe_id, bedetheque_url, series_id FROM universe_series WHERE universe_id = ?',
                    (universe_id,)
                ) if row[2]}:
                    result = _move_series_folder_for_universe(conn, matched_series_id)
                    if not result.get('success'):
                        logger.warning(f"Déplacement après synchronisation univers échoué pour série #{matched_series_id}: {result}")
            except Exception as exc:
                logger.warning(f"Déplacement après synchronisation univers échoué: {exc}")
            conn.close()
        except Exception as e:
            logger.warning(f"Erreur synchronisation univers pour série #{series_id}: {e}")

    def get_series_bedetheque_info(self, series_id):
        """Récupère les infos Bedetheque d'une série"""
        try:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute('''
                SELECT
                    bedetheque_url,
                    bedetheque_cover_path,
                    bedetheque_genre,
                    bedetheque_status,
                    bedetheque_total_volumes,
                    bedetheque_origin,
                    bedetheque_language,
                    bedetheque_scenaristes,
                    bedetheque_dessinateurs,
                    bedetheque_editeurs,
                    bedetheque_year_start,
                    bedetheque_year_end,
                    bedetheque_read_also,
                    bedetheque_author_links,
                    bedetheque_updated_at
                FROM series
                WHERE id = ?
            ''', (series_id,))

            row = cursor.fetchone()
            conn.close()

            if row:
                return dict(row)
            return None

        except Exception as e:
            logger.error(f"Erreur lors de la récupération: {e}")
            return None


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

    if len(sys.argv) < 2:
        print("Usage: python -m blueprints.bedetheque.scraper <nom de série ou URL bedetheque>")
        sys.exit(1)

    query = ' '.join(sys.argv[1:])
    scraper = BedethequeScraper()

    info = scraper.get_series_info(query) if query.startswith('http') else scraper.search_and_get_best_match(query)

    if not info:
        print(json.dumps({'error': 'Série non trouvée sur Bedetheque'}, ensure_ascii=False))
        sys.exit(1)

    print(json.dumps(info, ensure_ascii=False, indent=2))
