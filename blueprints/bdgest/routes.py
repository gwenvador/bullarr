"""
Routes pour le Top 100 annuel BDGest (https://www.bdgest.com/top/annuel)

"de meme pour les top 100 par annee: https://www.bdgest.com/top/annuel?annee=2026&Origine=1"
- même esprit que Panthéon/Thèmes (blueprints/bedetheque/routes.py): scrape + cache 6h +
  détection "déjà possédé", mais un site différent (bdgest.com, pas bedetheque.com) donc
  son propre blueprint plutôt que d'agrandir celui de Bédéthèque. Chaque entrée du
  classement renvoie néanmoins vers sa fiche Bédéthèque (même les BD BDGest ont une
  fiche là-bas) - le matching "déjà possédé" réutilise donc le même identifiant fort
  (series.bedetheque_url) que le reste de l'app, pas un nouveau mécanisme.
"""
from flask import request, jsonify, current_app
from . import bdgest_bp
from network_safety import safe_external_get
from error_utils import error_message
import requests
import sqlite3
import time
import unicodedata
import re
import logging

logger = logging.getLogger(__name__)

_ORIGINE_PARAM = {'general': None, 'franco-belge': 1, 'manga': 2, 'comics': 3}


def _normalize_title(value):
    text = unicodedata.normalize('NFKD', value or '')
    text = ''.join(ch for ch in text if not unicodedata.combining(ch)).lower()
    return re.sub(r'[^a-z0-9]+', ' ', text).strip()


def _warmed_bdgest_session():
    """Même raisonnement que BedethequeScraper._ensure_session (bedetheque/scraper.py)
    mais pour bdgest.com - domaine différent, cookie Cloudflare distinct de
    bedetheque.com malgré le site jumeau (confirmé en pratique: 403 sur un fetch nu ici
    aussi, 200 une fois la page d'accueil visitée d'abord)."""
    from blueprints.bedetheque.guard import GuardedSession
    session = GuardedSession()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'fr-FR,fr;q=0.9',
        'Referer': 'https://www.bdgest.com/'
    })
    try:
        session.get('https://www.bdgest.com/', timeout=10)
    except Exception:
        pass
    return session


def _fetch_bdgest_page(url):
    """Page BDGest (adresse fixée par le code, pas par l'utilisateur) lue avec la session
    réchauffée elle-même: sa connexion et ses cookies Cloudflare servent à la requête, et la
    garde espace l'accueil et la page (1,5 s). Via safe_external_get, la page partait sur une
    nouvelle connexion 0,16 s après l'accueil et Cloudflare la refusait (403)."""
    response = _warmed_bdgest_session().get(url, timeout=20)
    response.raise_for_status()
    if len(response.content) > 12 * 1024 * 1024:
        raise ValueError('Page BDGest trop volumineuse')
    return response


def _upstream_error_response(exc):
    """Message lisible quand BDGest refuse ou que la garde anti-blocage est en pause."""
    from blueprints.bedetheque.guard import BedethequeGuardError
    if isinstance(exc, BedethequeGuardError):
        return jsonify({'success': False, 'error': error_message(exc)}), 503
    status = getattr(getattr(exc, 'response', None), 'status_code', None)
    if status in (403, 429):
        return jsonify({'success': False, 'error': f"BDGest a refusé la requête (HTTP {status}, protection anti-robot) - réessayez plus tard"}), 502
    return None


@bdgest_bp.route('/top-annuel', methods=['GET'])
def top_annuel():
    origine = request.args.get('origine', 'general').strip().lower()
    if origine not in _ORIGINE_PARAM:
        return jsonify({'success': False, 'error': 'Origine inconnue'}), 400
    try:
        annee = int(request.args.get('annee', time.gmtime().tm_year))
    except ValueError:
        return jsonify({'success': False, 'error': 'Année invalide'}), 400
    # Pas de borne connue côté BDGest - le classement le plus ancien vu manuellement
    # remonte aux années 2000, une borne large évite juste de scraper une année absurde
    # (ex: script/bot mal utilisé) sans deviner la vraie limite du site.
    if annee < 1990 or annee > 2100:
        return jsonify({'success': False, 'error': 'Année hors limites'}), 400

    from bs4 import BeautifulSoup
    # "do all the entry in enrichir get cache?" / "the data does not really change much
    # so no need to update automatic. put only update manual" - même cache générique
    # persistant, sans expiration, que Indispensables/Panthéon/Thèmes
    # (blueprints/bedetheque/routes.py), data/bedetheque_catalog.db - bdgest.com n'est
    # pas bedetheque.com mais le mécanisme de cache lui-même n'a rien de spécifique au
    # site. Rafraîchi seulement sur ?refresh=1 explicite (bouton "Actualiser").
    from blueprints.bedetheque.catalog_index import get_cached_scrape, save_scrape_cache
    from blueprints.bedetheque.parsers import parse_bdgest_top
    cache_key = f'bdgest:top-annuel:{annee}:{origine}'
    force_refresh = request.args.get('refresh') in ('1', 'true')
    try:
        cached_items = None if force_refresh else get_cached_scrape(cache_key)
        if cached_items is not None:
            items = list(cached_items)
            was_cached = True
        else:
            url = f'https://www.bdgest.com/top/annuel?annee={annee}'
            origine_param = _ORIGINE_PARAM[origine]
            if origine_param is not None:
                url += f'&Origine={origine_param}'
            # Voir _warmed_bdgest_session plus haut - même 403 Cloudflare qu'un fetch nu
            # vers bedetheque.com sans session réchauffée.
            response = _fetch_bdgest_page(url)
            soup = BeautifulSoup(response.content, 'html.parser')
            items = parse_bdgest_top(soup)
            if not items:
                # Rien n'est mis en cache: un résultat vide y resterait (pas d'expiration).
                return jsonify({'success': False, 'error': "Aucun résultat - la mise en page de BDGest a peut-être changé"}), 502
            save_scrape_cache(cache_key, items)
            was_cached = False

        conn = sqlite3.connect(current_app.config['DATABASE'])
        rows = conn.execute("SELECT id, bedetheque_url, title FROM series WHERE bedetheque_url IS NOT NULL OR title IS NOT NULL").fetchall()
        conn.close()
        series_by_url = {str(r[1]).rstrip('/'): r[0] for r in rows if r[1]}
        series_by_title = {_normalize_title(r[2]): r[0] for r in rows if r[2]}

        def _match_series_id(item):
            return series_by_url.get(item['url'].rstrip('/')) or series_by_title.get(_normalize_title(item['title']))

        items = [dict(item, already_owned=_match_series_id(item) is not None, series_id=_match_series_id(item)) for item in items]
        return jsonify({'success': True, 'annee': annee, 'origine': origine, 'items': items, 'cached': was_cached})
    except Exception as exc:
        known = _upstream_error_response(exc)
        if known:
            return known
        return jsonify({'success': False, 'error': 'Erreur interne'}), 502


_SORTIES_PAGES = {'bd': 'sorties_bd', 'manga': 'sorties_manga', 'comics': 'sorties_comics'}


@bdgest_bp.route('/sorties', methods=['GET'])
def sorties():
    """Calendrier des sorties BDGest (https://www.bdgest.com/sorties_bd?vue=liste), par
    catégorie et par mois (« MM/AAAA »; vide = « ces jours-ci »), avec détection « série déjà
    en bibliothèque » par titre. Mis en cache comme les autres onglets d'Enrichir (rafraîchi
    à la demande: ?refresh=1)."""
    categorie = request.args.get('categorie', 'bd').strip().lower()
    if categorie not in _SORTIES_PAGES:
        return jsonify({'success': False, 'error': 'Catégorie inconnue'}), 400
    mois = request.args.get('mois', '').strip()
    year = time.gmtime().tm_year
    if mois:
        match = re.fullmatch(r'(0[1-9]|1[0-2])/(20\d{2})', mois)
        if not match:
            return jsonify({'success': False, 'error': 'Mois invalide'}), 400
        year = int(match.group(2))

    from bs4 import BeautifulSoup
    from blueprints.bedetheque.catalog_index import get_cached_scrape, save_scrape_cache
    from blueprints.bedetheque.parsers import parse_bdgest_sorties
    cache_key = f'bdgest:sorties:{categorie}:{mois or "courant"}'
    force_refresh = request.args.get('refresh') in ('1', 'true')
    try:
        cached_items = None if force_refresh else get_cached_scrape(cache_key)
        if cached_items is not None:
            items = list(cached_items)
            was_cached = True
        else:
            url = f'https://www.bdgest.com/{_SORTIES_PAGES[categorie]}?vue=liste'
            if mois:
                url += '&DL=' + mois.replace('/', '%2F')
            response = _fetch_bdgest_page(url)
            soup = BeautifulSoup(response.content, 'html.parser')
            if not mois:
                # « Ces jours-ci » enjambe parfois deux années (décembre -> janvier): on
                # déduit l'année de chaque jour par rapport à aujourd'hui.
                today = time.gmtime()
                items = []
                for item in parse_bdgest_sorties(soup, today.tm_year):
                    month = int(item['date'][5:7])
                    if month - today.tm_mon > 6:
                        item['date'] = f"{today.tm_year - 1:04d}{item['date'][4:]}"
                    elif today.tm_mon - month > 6:
                        item['date'] = f"{today.tm_year + 1:04d}{item['date'][4:]}"
                    items.append(item)
            else:
                items = parse_bdgest_sorties(soup, year)
            if not items:
                return jsonify({'success': False, 'error': "Aucune sortie trouvée - la mise en page de BDGest a peut-être changé"}), 502
            save_scrape_cache(cache_key, items)
            was_cached = False

        from series_title_match import index_series_by_title
        conn = sqlite3.connect(current_app.config['DATABASE'])
        rows = conn.execute("SELECT id, title FROM series WHERE title IS NOT NULL").fetchall()
        conn.close()
        index = index_series_by_title(rows, lambda r: r[1], _normalize_title)
        owned = []
        for item in items:
            candidates = index.get(_normalize_title(item['title'])) or []
            series_id = candidates[0][0] if candidates else None
            owned.append(dict(item, already_owned=series_id is not None, series_id=series_id))
        return jsonify({'success': True, 'categorie': categorie, 'mois': mois, 'items': owned, 'cached': was_cached})
    except Exception as exc:
        known = _upstream_error_response(exc)
        if known:
            return known
        logger.exception('Calendrier des sorties BDGest')
        return jsonify({'success': False, 'error': 'Erreur interne'}), 502
