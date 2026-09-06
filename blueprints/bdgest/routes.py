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
from flask import Blueprint, request, jsonify, current_app
from . import bdgest_bp
from network_safety import safe_external_get
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
    session = requests.Session()
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
            response = safe_external_get(url, session=_warmed_bdgest_session(), timeout=20, max_bytes=12 * 1024 * 1024)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            items = []
            ol = soup.select_one('ol.top-ventes')
            for li in (ol.select(':scope > li') if ol else []):
                link = li.select_one('.main h3 a') or li.select_one('.couv')
                if not link:
                    continue
                url_ = link.get('href', '').strip()
                series_title = link.get_text(strip=True)
                if not url_ or not series_title:
                    continue
                place_node = li.select_one('.place')
                rank_match = re.search(r'(\d+)', place_node.get_text() if place_node else '')
                rank = int(rank_match.group(1)) if rank_match else len(items) + 1
                # Le libellé de tome ("8. La Longue Marche de Lucky Luke") vit dans le
                # texte qui suit le <br/> À L'INTÉRIEUR du même <h3> - absent pour un
                # one-shot (rien après le <br/>, voir "Cauchon..." dans le HTML observé).
                h3 = li.select_one('.main h3')
                volume_label = ''
                if h3:
                    br = h3.find('br')
                    if br and br.next_sibling:
                        volume_label = ' '.join(str(br.next_sibling).split())
                cover = li.select_one('.couv img')
                publisher_node = li.select_one('.infos .icon-building')
                publisher = publisher_node.find_next('span').get_text(strip=True) if publisher_node else ''
                date_node = li.select_one('.infos .icon-calendar')
                release_date = date_node.find_next('span').get_text(strip=True) if date_node else ''
                votes_node = li.select_one('.infos .icon-trophy')
                votes = votes_node.find_next('span').get_text(strip=True) if votes_node else ''
                summary_node = li.select_one('.main > p')
                items.append({
                    'rank': rank,
                    'title': series_title,
                    'volume_label': volume_label,
                    'url': url_,
                    'cover': cover.get('src', '').strip() if cover else None,
                    'publisher': publisher,
                    'release_date': release_date,
                    'votes': votes,
                    'summary': summary_node.get_text(' ', strip=True) if summary_node else '',
                })
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
        return jsonify({'success': False, 'error': str(exc)}), 502
