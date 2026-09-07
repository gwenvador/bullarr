"""
Routes pour l'intégration Bedetheque
"""
from flask import request, jsonify, current_app
from . import bedetheque_bp
from .scraper import BedethequeScraper, BedethequeDatabase, match_bedetheque_volume, _parse_int_hs_prefix, _parse_special_prefix
from .comicinfo_writer import build_comicinfo_fields, apply_volume_comicinfo, UnsupportedFormatError
from .cbr_converter import convert_cbr_to_cbz, CbrConversionError
from network_safety import safe_external_get
import sqlite3
import json
import logging
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import unicodedata
import re

logger = logging.getLogger(__name__)


def get_db_connection():
    """Retourne une connexion à la base de données"""
    conn = sqlite3.connect(current_app.config['DATABASE'], timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn

def _find_volume_bedetheque_web_hint(cursor, series_id):
    """Cherche, parmi les tomes d'une série, un lien Bédéthèque déjà présent dans un
    ComicInfo <Web> (voir _ensure_bedetheque_match) - utilisé quand la série elle-même
    n'a pas encore de bedetheque_url mais qu'un de ses tomes a déjà été matché
    individuellement (import Komga, édition manuelle du ComicInfo...)."""
    cursor.execute('SELECT comicinfo FROM volumes WHERE series_id = ? AND comicinfo IS NOT NULL', (series_id,))
    for row in cursor.fetchall():
        try:
            ci = json.loads(row['comicinfo'])
        except (TypeError, ValueError):
            continue
        web = ci.get('web')
        if web and 'bedetheque.com' in web:
            return web
    return None

def _ensure_bedetheque_match(series_id, series_title, existing_url, db_manager, scraper, volume_web_hint=None):
    """
    S'assure qu'une série a un match Bedetheque et retourne ses infos complètes (albums
    inclus), dans cet ordre de priorité:
      1. existing_url (series.bedetheque_url déjà enregistré): récupère simplement les
         infos à jour depuis cette URL.
      2. volume_web_hint (lien Bédéthèque déjà présent dans le ComicInfo <Web> d'un tome
         de cette série, ex: écrit manuellement ou par Komga): retrouve la fiche série
         correspondante depuis cet album précis - fiable car ça pointe un album précis,
         pas une recherche par titre.

    Retourne le dict info (BedethequeScraper.get_series_info) ou None si introuvable -
    dans ce cas l'appelant doit renvoyer une erreur invitant à matcher manuellement
    (POST /enrich/<series_id>, avec confirmation utilisateur du résultat), PAS retomber
    sur une recherche automatique par titre ici: cette fonction est appelée par les
    routes de "MAJ métadonnées" (update_metadata_series/volume), qui écrivent aussitôt le
    titre + le ComicInfo de tous les tomes concernés - une recherche floue sans
    confirmation risquait de matcher silencieusement la mauvaise série (titre ambigu/
    partagé) et d'écraser les métadonnées avec celles d'une autre BD. Voir
    POST /enrich/<series_id> et /enrich-batch pour le matching par titre volontaire,
    déclenché explicitement par l'utilisateur.
    """
    if existing_url:
        return scraper.get_series_info(existing_url)

    if volume_web_hint:
        series_url = scraper.get_series_url_from_album_url(volume_web_hint)
        if series_url:
            info = scraper.get_series_info(series_url)
            if info:
                db_manager.update_series_bedetheque_info(series_id, info)
                logger.info(f"✓ Bedetheque matché pour la série #{series_id} via un lien déjà associé à un tome: {series_url}")
                return info

    return None

def _get_cached_series_bedetheque_info(cursor, series_id):
    """Reconstruit un dict 'info' (même forme que BedethequeScraper.get_series_info,
    limité aux champs que build_comicinfo_fields/match_bedetheque_volume utilisent
    réellement) à partir du seul cache local series.bedetheque_* - aucun accès réseau.
    Retourne None si la série n'a jamais été entièrement scrapée (bedetheque_albums
    vide), auquel cas l'appelant doit retomber sur un vrai fetch réseau.

    Une consultation en lecture (preview, avis lecteurs, rattachement d'un lien) n'a pas
    besoin d'aller-retour réseau juste pour retrouver une donnée déjà récupérée par une
    précédente "MAJ métadonnées" - contrairement à update-metadata/series|volume, dont le
    but même est de rafraîchir depuis la source (voir _ensure_bedetheque_match), ces
    consultations peuvent se contenter de la dernière copie connue."""
    cursor.execute('''
        SELECT bedetheque_url, bedetheque_description, bedetheque_genre,
               bedetheque_scenaristes, bedetheque_dessinateurs, bedetheque_editeurs,
               bedetheque_albums
        FROM series WHERE id = ?
    ''', (series_id,))
    row = cursor.fetchone()
    if not row or not row['bedetheque_albums']:
        return None
    try:
        volumes = json.loads(row['bedetheque_albums'])
    except (TypeError, ValueError):
        return None
    return {
        'url': row['bedetheque_url'],
        'description': row['bedetheque_description'],
        'genre': row['bedetheque_genre'],
        # bedetheque_scenaristes/dessinateurs/editeurs sont stockés déjà joints par
        # ', '.join(...) (voir update_series_bedetheque_info) - build_comicinfo_fields
        # refait le même ', '.join sur une liste, donc les remettre dans une liste à un
        # seul élément restitue exactement la même chaîne.
        'scenaristes': [row['bedetheque_scenaristes']] if row['bedetheque_scenaristes'] else [],
        'dessinateurs': [row['bedetheque_dessinateurs']] if row['bedetheque_dessinateurs'] else [],
        'editeurs': [row['bedetheque_editeurs']] if row['bedetheque_editeurs'] else [],
        'volumes': volumes,
    }

def _persist_cached_bd_volume_description(series_id, volumes):
    """Persiste UNIQUEMENT series.bedetheque_albums (pas le reste de la ligne) - utilisé
    quand l'info série vient de _get_cached_series_bedetheque_info (dict reconstruit
    partiel), contrairement à BedethequeDatabase.update_series_bedetheque_info qui réécrit
    toute la ligne à partir d'un dict complet de get_series_info() et écraserait donc
    status/total_volumes/cover_path/dates avec des None si on lui passait ce dict partiel."""
    conn = sqlite3.connect(current_app.config['DATABASE'], timeout=30.0)
    conn.execute('UPDATE series SET bedetheque_albums = ? WHERE id = ?', (json.dumps(volumes), series_id))
    conn.commit()
    conn.close()

def _update_volume_filepath_format(volume_id, filepath, format_type):
    """Persiste le nouveau filepath/format d'un volume en base après conversion cbr->cbz
    (le fichier sur disque a changé de nom/extension, voir cbr_converter.convert_cbr_to_cbz)"""
    conn = sqlite3.connect(current_app.config['DATABASE'], timeout=30.0)
    cursor = conn.cursor()
    cursor.execute('UPDATE volumes SET filepath = ?, format = ? WHERE id = ?',
                   (filepath, format_type, volume_id))
    conn.commit()
    conn.close()

_INDISPENSABLE_CATEGORIES = {
    'all': 'https://www.bedetheque.com/indispensables.html',
    'franco-belge': 'https://www.bedetheque.com/indispensables-origine-1.html',
    'comics': 'https://www.bedetheque.com/indispensables-origine-3.html',
    'manga': 'https://www.bedetheque.com/indispensables-origine-2.html',
}

def _parse_indispensable_genre(url):
    try:
        from bs4 import BeautifulSoup
        scraper = BedethequeScraper()
        scraper._ensure_session()
        response = safe_external_get(url, session=scraper.session, timeout=15, max_bytes=4 * 1024 * 1024)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        label = soup.find('label', string=lambda value: value and 'Genre' in value)
        value = label.find_next('span', class_='style-serie') if label else None
        return value.get_text(' ', strip=True) if value else '-'
    except Exception:
        return '-'



def _normalize_indispensable_title(value):
    text = unicodedata.normalize('NFKD', value or '')
    text = ''.join(ch for ch in text if not unicodedata.combining(ch)).lower()
    return re.sub(r'[^a-z0-9]+', ' ', text).strip()


_PANTHEON_CATEGORIES = {
    'all': 'https://www.bedetheque.com/pantheon',
    'franco-belge': 'https://www.bedetheque.com/pantheon/index/cat/1',
    'comics': 'https://www.bedetheque.com/pantheon/index/cat/2',
    'manga': 'https://www.bedetheque.com/pantheon/index/cat/3',
    'fondateurs': 'https://www.bedetheque.com/pantheon/index/cat/4',
}


@bedetheque_bp.route('/pantheon', methods=['GET'])
def pantheon_authors():
    from bs4 import BeautifulSoup
    category = request.args.get('category', 'all').strip().lower()
    if category not in _PANTHEON_CATEGORIES:
        return jsonify({'success': False, 'error': 'Catégorie inconnue'}), 400
    try:
        # "do all the entry in enrichir get cache?" / "the data does not really change
        # much so no need to update automatic. put only update manual" - voir le
        # commentaire de indispensable_series plus bas, même cache générique persistant
        # sans expiration, rafraîchi uniquement sur ?refresh=1 explicite (bouton
        # "Actualiser" de /bedetheque-enrich).
        from .catalog_index import get_cached_scrape, save_scrape_cache
        cache_key = f'pantheon:{category}'
        force_refresh = request.args.get('refresh') in ('1', 'true')
        cached_items = None if force_refresh else get_cached_scrape(cache_key)
        if cached_items is not None:
            items = list(cached_items)
            was_cached = True
        else:
            # Voir le commentaire de _parse_indispensable_genre plus haut - même 403
            # Cloudflare sans la session réchauffée du scraper principal.
            scraper = BedethequeScraper()
            scraper._ensure_session()
            response = safe_external_get(_PANTHEON_CATEGORIES[category], session=scraper.session, timeout=20, max_bytes=12 * 1024 * 1024)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            items = []
            listing = soup.select_one('ul.indispensables-list')
            rows = listing.select(':scope > li') if listing else []
            for row in rows:
                link = row.select_one('.hall-auteur .auteur a[href*="/auteur-"]')
                if not link:
                    continue
                url = link.get('href', '').strip()
                name = link.get_text(' ', strip=True) or link.get('title') or ''
                if not name or not url:
                    continue
                photo = row.select_one('.hall-photo img')
                dates_node = row.select_one('.date-auteur')
                professions_node = row.select_one('.metiers-auteur')
                country_node = row.select_one('.pays-auteur')
                works_node = row.select_one('.texte-auteur')
                cat_node = row.select_one('.cat-hall a')
                year_node = row.select_one('.annee-hall')
                # get_text(' ', strip=True) ne collapse pas les runs d'espaces/retours à
                # la ligne À L'INTÉRIEUR d'un même nœud texte (seulement en début/fin) -
                # "Scénariste,\n                    Dessinateur" (mise en forme HTML
                # source) restait donc avec tous ses espaces internes intacts.
                def _clean(node):
                    return ' '.join(node.get_text(' ', strip=True).split()) if node else ''
                items.append({
                    'rank': len(items) + 1,
                    'name': name,
                    'url': url,
                    'photo': photo.get('src', '').strip() if photo else None,
                    'dates': _clean(dates_node),
                    'professions': _clean(professions_node),
                    'country': _clean(country_node),
                    'notable_works': _clean(works_node),
                    'category': _clean(cat_node),
                    'year': _clean(year_node),
                })
            save_scrape_cache(cache_key, items)
            was_cached = False

        conn = sqlite3.connect(current_app.config['DATABASE'])
        rows = conn.execute("SELECT bedetheque_author_links FROM series WHERE bedetheque_author_links IS NOT NULL").fetchall()
        conn.close()
        # Même normalisation que _normalize_indispensable_title, mais sur une URL
        # d'auteur (pas de titre à désarticler ici) - juste au cas où bedetheque_author_links
        # contiendrait une URL avec/sans slash final selon la série.
        owned_author_urls = set()
        for (raw,) in rows:
            try:
                links = json.loads(raw or '{}')
            except (TypeError, ValueError):
                continue
            if isinstance(links, dict):
                owned_author_urls.update(str(u).rstrip('/') for u in links.values() if u)
        items = [dict(item, already_owned=item['url'].rstrip('/') in owned_author_urls) for item in items]
        return jsonify({'success': True, 'category': category, 'items': items, 'cached': was_cached})
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 502


_THEME_SLUG_RE = re.compile(r'^[A-Za-z0-9_-]{1,80}$')


@bedetheque_bp.route('/themes', methods=['GET'])
def list_themes():
    from bs4 import BeautifulSoup
    try:
        # "do all the entry in enrichir get cache?" / "the data does not really change
        # much so no need to update automatic. put only update manual" - voir le
        # commentaire de indispensable_series plus bas.
        from .catalog_index import get_cached_scrape, save_scrape_cache
        force_refresh = request.args.get('refresh') in ('1', 'true')
        cached_groups = None if force_refresh else get_cached_scrape('themes:list')
        if cached_groups is not None:
            return jsonify({'success': True, 'groups': cached_groups, 'cached': True})

        # Voir le commentaire de _parse_indispensable_genre plus haut - même 403
        # Cloudflare sans la session réchauffée du scraper principal.
        scraper = BedethequeScraper()
        scraper._ensure_session()
        response = safe_external_get('https://www.bedetheque.com/theme', session=scraper.session, timeout=20, max_bytes=12 * 1024 * 1024)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        ul = soup.select_one('div.select-theme > ul')
        groups = []
        current = None
        for li in (ul.select(':scope > li') if ul else []):
            a = li.select_one('a')
            if not a:
                continue
            href = a.get('href', '').strip()
            name = a.get_text(strip=True)
            if 'super' in (li.get('class') or []):
                current = {'name': name, 'themes': []}
                groups.append(current)
                continue
            m = re.search(r'theme-BD-([A-Za-z0-9_-]+)\.html', href)
            if not m or current is None:
                continue
            current['themes'].append({'name': name, 'slug': m.group(1), 'url': href})
        save_scrape_cache('themes:list', groups)
        return jsonify({'success': True, 'groups': groups, 'cached': False})
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 502


@bedetheque_bp.route('/theme', methods=['GET'])
def theme_series():
    from bs4 import BeautifulSoup
    slug = request.args.get('slug', '').strip()
    if not _THEME_SLUG_RE.match(slug):
        return jsonify({'success': False, 'error': 'Thème inconnu'}), 400
    try:
        # "do all the entry in enrichir get cache?" / "the data does not really change
        # much so no need to update automatic. put only update manual" - voir le
        # commentaire de indispensable_series plus bas. items ET title dans la même
        # valeur mise en cache (theme_title n'est connu qu'au fetch, pas déductible du
        # slug seul).
        from .catalog_index import get_cached_scrape, save_scrape_cache
        cache_key = f'themes:detail:{slug}'
        force_refresh = request.args.get('refresh') in ('1', 'true')
        cached_value = None if force_refresh else get_cached_scrape(cache_key)
        if cached_value is not None:
            items = list(cached_value['items'])
            theme_title = cached_value['title']
            was_cached = True
        else:
            theme_url = f'https://www.bedetheque.com/theme-BD-{slug}.html'
            # Voir le commentaire de _parse_indispensable_genre plus haut - même 403
            # Cloudflare sans la session réchauffée du scraper principal.
            scraper = BedethequeScraper()
            scraper._ensure_session()
            response = safe_external_get(theme_url, session=scraper.session, timeout=20, max_bytes=12 * 1024 * 1024)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            # Pas de <h1>/<h2> dédié sur cette page - le nom du thème n'apparaît que
            # comme dernier maillon (li.active) de son propre fil d'Ariane.
            title_node = soup.select_one('.single-title-serie .breadcrumb li.active')
            theme_title = title_node.get_text(strip=True) if title_node else slug
            items = []
            ul = soup.select_one('ul.theme')
            for li in (ul.select(':scope > li') if ul else []):
                link = li.select_one('.info h3 a') or li.select_one('.couv a')
                if not link:
                    continue
                url = link.get('href', '').strip()
                title = link.get_text(strip=True)
                if not url or not title:
                    continue
                cover = li.select_one('.couv img')
                origin_node = li.select_one('.info .origine')
                authors_node = li.select_one('.info .auteurs')
                note_node = li.select_one('.info .note img')
                summary_node = li.select_one('.info > p')
                items.append({
                    'title': title,
                    'url': url,
                    'cover': cover.get('src', '').strip() if cover else None,
                    'origin': origin_node.get_text(strip=True) if origin_node else '',
                    'authors': authors_node.get_text(strip=True) if authors_node else '',
                    'note': note_node.get('title', '').strip() if note_node else '',
                    'summary': summary_node.get_text(' ', strip=True) if summary_node else '',
                })
            save_scrape_cache(cache_key, {'items': items, 'title': theme_title})
            was_cached = False

        conn = get_db_connection()
        cursor = conn.cursor()
        rows = cursor.execute("SELECT id, bedetheque_url, title FROM series WHERE bedetheque_url IS NOT NULL OR title IS NOT NULL").fetchall()
        conn.close()
        # Même stratégie de matching que /indispensables ci-dessus: URL Bédéthèque
        # exacte en priorité (identifiant fort, chaque item de thème pointe déjà vers
        # une fiche serie-XXX), titre normalisé en repli.
        series_by_url = {str(r[1]).rstrip('/'): r[0] for r in rows if r[1]}
        series_by_title = {_normalize_indispensable_title(r[2]): r[0] for r in rows if r[2]}

        def _match_series_id(item):
            return series_by_url.get(item['url'].rstrip('/')) or series_by_title.get(_normalize_indispensable_title(item['title']))

        items = [dict(item, already_owned=_match_series_id(item) is not None, series_id=_match_series_id(item)) for item in items]
        return jsonify({'success': True, 'slug': slug, 'title': theme_title, 'items': items, 'cached': was_cached})
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 502


@bedetheque_bp.route('/indispensables', methods=['GET'])
def indispensable_series():
    from bs4 import BeautifulSoup
    category = request.args.get('category', 'all').strip().lower()
    if category not in _INDISPENSABLE_CATEGORIES:
        return jsonify({'success': False, 'error': 'Catégorie inconnue'}), 400
    try:
        from .catalog_index import get_cached_scrape, save_scrape_cache
        cache_key = f'indispensables:{category}'
        force_refresh = request.args.get('refresh') in ('1', 'true')
        cached_items = None if force_refresh else get_cached_scrape(cache_key)
        if cached_items is not None:
            items = list(cached_items)
            was_cached = True
        else:
            # "Erreur : 403 Client Error: Forbidden for url:
            # https://www.bedetheque.com/indispensables-origine-1.html" - voir le
            # commentaire de _parse_indispensable_genre plus haut, même 403 Cloudflare
            # sans la session réchauffée du scraper principal.
            scraper = BedethequeScraper()
            scraper._ensure_session()
            response = safe_external_get(_INDISPENSABLE_CATEGORIES[category], session=scraper.session, timeout=20, max_bytes=12 * 1024 * 1024)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            items, seen = [], set()
            listing = soup.select_one('ul.indispensables-list')
            links = []
            if listing:
                for row in listing.select(':scope > li'):
                    link = row.select_one('a[href*="/serie-"]')
                    if not link:
                        continue
                    links.append((link, row.select_one('.style')))
            else:
                links = [(link, None) for link in soup.select('a[href*="/serie-"]')]
            for link, genre_node in links:
                url = link.get('href', '')
                if not url.startswith('https://www.bedetheque.com/serie-'):
                    continue
                title = (link.get_text(' ', strip=True) or link.get('title') or '').strip()
                article_match = re.match(r'^(.*?)\s*\((Le|La|Les|L\'|Un|Une)\)$', title, re.IGNORECASE)
                if article_match:
                    base, article = article_match.groups()
                    title = f"{article} {base[:1].lower() + base[1:] if base else base}"
                if not title or url in seen:
                    continue
                seen.add(url)
                genre = genre_node.get_text(' ', strip=True) if genre_node else '-'
                items.append({'rank': len(items) + 1, 'title': title, 'url': url, 'genre': genre, 'category': {'all': 'Tous', 'franco-belge': 'Franco-belge', 'comics': 'Comics', 'manga': 'Manga'}[category]})
                if len(items) >= 100:
                    break
            save_scrape_cache(cache_key, items)
            was_cached = False
        conn = sqlite3.connect(current_app.config['DATABASE'])
        rows = conn.execute('SELECT id, title, bedetheque_url FROM series').fetchall()
        conn.close()
        # id nécessaire pour le bouton "Supprimer" côté frontend (une série déjà
        # possédée doit pouvoir être retirée directement depuis ce tableau) - avant, seule
        # une correspondance booléenne (already_in_library) était renvoyée.
        series_by_url = {str(row[2]).rstrip('/'): row[0] for row in rows if row[2]}
        series_by_title = {_normalize_indispensable_title(row[1]): row[0] for row in rows if row[1]}
        def _match_series_id(item):
            return series_by_url.get(item['url'].rstrip('/')) or series_by_title.get(_normalize_indispensable_title(item['title']))
        items = [dict(item, already_in_library=_match_series_id(item) is not None, series_id=_match_series_id(item)) for item in items]
        return jsonify({'success': True, 'category': category, 'items': items, 'cached': was_cached})
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 502

@bedetheque_bp.route('/search', methods=['GET'])
def search_bedetheque():
    """
    Recherche une série sur Bedetheque
    Query params: ?q=titre_de_la_serie, ou directement ?q=https://www.bedetheque.com/serie-...
    """
    query = request.args.get('q', '').strip()

    if not query:
        return jsonify({'error': 'Paramètre q requis'}), 400

    try:
        scraper = BedethequeScraper()

        # Coller directement une URL de fiche série (plutôt que de chercher par titre)
        # évite l'échec de la recherche texte pour un titre trop générique ou mal
        # normalisé - on résout la série depuis l'URL et on la renvoie dans le même
        # format qu'un résultat de recherche pour ne rien changer côté frontend
        if re.match(r'^https?://(www\.)?bedetheque\.com/', query, re.IGNORECASE):
            info = scraper.get_series_info(query)
            if not info:
                return jsonify({
                    'success': False,
                    'error': 'Série introuvable à cette adresse'
                }), 404
            results = [{
                'title': info['title'],
                'url': info['url'],
                'genre': info.get('genre')
            }]
        else:
            results = scraper.search_series(query)

        return jsonify({
            'success': True,
            'query': query,
            'results': results
        })

    except Exception as e:
        logger.error(f"Erreur lors de la recherche Bedetheque: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bedetheque_bp.route('/catalog-index/status', methods=['GET'])
def get_catalog_index_status():
    """État de l'index local du catalogue Bédéthèque (voir catalog_index.py) - alimente
    la carte dédiée de /settings (nombre de séries indexées, date de dernière
    construction, progression si une construction est en cours)."""
    from blueprints.bedetheque.catalog_index import get_catalog_status
    return jsonify({'success': True, **get_catalog_status()})


@bedetheque_bp.route('/catalog-index/build', methods=['POST'])
def build_catalog_index():
    """Lance (en arrière-plan) la (re)construction complète de l'index local du
    catalogue Bédéthèque - "apres ce qu'on pourrait faire c'est telecharger deja en db
    toutes l'index des series et chercher prendrait tres peu de temsp". Manuelle
    uniquement (sur demande explicite), pas de planification automatique - voir
    catalog_index.py. ~27 requêtes (une par lettre + chiffres), thread dédié pour ne
    pas bloquer la requête HTTP le temps que ça tourne (de l'ordre de la minute)."""
    from blueprints.bedetheque.catalog_index import build_bedetheque_catalog_index_sync, get_catalog_status
    if get_catalog_status()['running']:
        return jsonify({'success': False, 'error': 'Construction déjà en cours'}), 409

    scraper = BedethequeScraper()
    thread = threading.Thread(target=build_bedetheque_catalog_index_sync, args=(scraper,), daemon=True)
    thread.start()
    return jsonify({'success': True, 'started': True})


@bedetheque_bp.route('/authors/list', methods=['GET'])
def list_database_authors():
    """Retourne les auteurs connus localement pour le combobox Par auteur.

    "dans auteur il y a des entrées bizarres. c'est vraiment les auteurs? ... simplifié
    juste auteur" - ne retient QUE series.bedetheque_author_links (nom + URL confirmés par
    Bédéthèque lui-même), plus jamais local_author/manual_author/volumes.author: ces
    derniers viennent du ComicInfo.xml de chaque fichier (Writer/Penciller...), pas
    toujours fiable - constaté en pratique des plages d'années ("1978-1984"), des tags de
    scan/édition ("2015 - Couleur format normal", "Dargaud 10.2008"), et même des
    fragments de titre de série mal étiquetés "auteur" par une release ("Kenya - Saison
    2", "Livre 1"). Cette page sert justement à parcourir la bibliographie BÉDÉTHÈQUE d'un
    auteur (voir GET /authors/albums) - un nom sans URL Bédéthèque confirmée n'a de toute
    façon rien de fiable à proposer ici."""
    try:
        conn = get_db_connection()
        rows = conn.execute('SELECT bedetheque_author_links FROM series').fetchall()
        conn.close()
        authors = {}
        for row in rows:
            try:
                links = json.loads(row['bedetheque_author_links'] or '{}')
            except (TypeError, ValueError):
                continue
            if not isinstance(links, dict):
                continue
            for name, url in links.items():
                name = str(name or '').strip()
                if name and url:
                    authors.setdefault(name.casefold(), {'name': name, 'url': url})
        return jsonify({'success': True, 'authors': sorted(authors.values(), key=lambda a: a['name'].casefold())})
    except Exception as exc:
        logger.exception('Erreur lors du chargement des auteurs locaux')
        return jsonify({'success': False, 'error': str(exc)}), 500


@bedetheque_bp.route('/authors/search', methods=['GET'])
def search_bedetheque_authors():
    """Recherche un auteur par nom (item #25 improvement.txt, onglet "Par auteur" de
    /bedetheque-enrich + modale "albums de l'auteur" sur la fiche série) - query param
    ?q=nom_auteur."""
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'success': False, 'error': 'Paramètre q requis'}), 400

    try:
        scraper = BedethequeScraper()
        results = scraper.search_authors(query)
        return jsonify({'success': True, 'query': query, 'results': results})
    except Exception as e:
        logger.error(f"Erreur lors de la recherche d'auteur Bedetheque: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@bedetheque_bp.route('/authors/albums', methods=['GET'])
def get_bedetheque_author_albums():
    """Bibliographie d'un auteur, filtrée aux séries en français (item #25
    improvement.txt: "in bedetheque one author can have album in different language.
    keep the one in french") - query param ?url=fiche_auteur_bedetheque."""
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'success': False, 'error': 'Paramètre url requis'}), 400
    if not url.startswith('https://www.bedetheque.com/'):
        return jsonify({'success': False, 'error': 'URL bedetheque.com requise'}), 400

    try:
        scraper = BedethequeScraper()
        bibliography = scraper.get_author_bibliography(url)
        albums = [a for a in bibliography if a['is_french']]

        if albums:
            conn = get_db_connection()
            cursor = conn.cursor()
            placeholders = ','.join('?' * len(albums))
            cursor.execute(
                f'SELECT bedetheque_url FROM series WHERE bedetheque_url IN ({placeholders})',
                [a['bedetheque_url'] for a in albums]
            )
            owned_urls = {row[0] for row in cursor.fetchall()}
            conn.close()
            for album in albums:
                album['already_in_library'] = album['bedetheque_url'] in owned_urls

        return jsonify({'success': True, 'url': url, 'albums': albums})
    except Exception as e:
        logger.error(f"Erreur lors de la récupération de la bibliographie auteur: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


def _get_cached_author_photos(urls):
    """Lecture SEULE de author_photos (jamais de scraping ici) - utilisée par GET
    /authors/photos quand une fiche série s'affiche. "will you scrape pictures when
    adding a new serie?" / "add it" / "but only when adding a new serie": le scraping
    (requêtes réseau vers bedetheque.com, anti-bot) ne doit se déclencher qu'une fois,
    à l'ajout de la série (voir _fetch_and_cache_author_photos/
    _start_author_photos_fetch_thread ci-dessous) - PAS chaque fois qu'un utilisateur
    ouvre une fiche série. Une série ajoutée avant l'existence de cette fonctionnalité
    n'aura donc jamais de photo tant qu'elle n'est pas explicitement ré-ajoutée -
    c'est le compromis demandé, pas un oubli."""
    conn = get_db_connection()
    cursor = conn.cursor()
    placeholders = ','.join('?' * len(urls))
    cursor.execute(f"SELECT author_url, photo_path FROM author_photos WHERE author_url IN ({placeholders})", urls)
    photos = {row[0]: row[1] for row in cursor.fetchall()}
    conn.close()
    return photos


def _fetch_and_cache_author_photos(urls):
    """Seul point d'entrée qui scrape réellement bedetheque.com pour une photo
    d'auteur - appelé uniquement par _start_author_photos_fetch_thread (ajout d'une
    série), jamais depuis une requête interactive de la fiche série (voir
    _get_cached_author_photos ci-dessus). Retourne {url: chemin_relatif_ou_vide} (une
    URL absente du résultat = erreur réseau ponctuelle, pas mise en cache)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    placeholders = ','.join('?' * len(urls))
    cursor.execute(f"SELECT author_url, photo_path FROM author_photos WHERE author_url IN ({placeholders})", urls)
    photos = {row[0]: row[1] for row in cursor.fetchall()}
    missing = [u for u in urls if u not in photos]
    if missing:
        scraper = BedethequeScraper()
        # Séquentiel, pas de ThreadPoolExecutor (contrairement à emule/ed2k_stats.py) -
        # bedetheque.com est déjà protégé par _anti_bot_delay() entre chaque requête,
        # comme tout le reste du scraping Bédéthèque (enrich_series_batch...);
        # paralléliser ici contournerait ce throttling volontaire.
        #
        # commit() APRÈS CHAQUE auteur, pas une seule fois à la fin de la boucle: le
        # rattrapage plein-bibliothèque (backfill_author_photos) peut porter sur plus
        # d'un millier d'auteurs uniques, largement plus d'une heure avec l'anti-bot -
        # un seul commit final ne persistait RIEN tant que la boucle entière n'avait pas
        # fini, donc perdait tout le travail déjà fait si le thread était interrompu
        # (redémarrage du conteneur, exception non prévue) et rendait le progrès
        # impossible à observer depuis l'extérieur (SELECT COUNT(*) FROM author_photos
        # restait à 0 jusqu'à la fin complète).
        for u in missing:
            path = scraper.get_author_photo_path(u)
            if path is not None:
                photos[u] = path
                cursor.execute("INSERT OR REPLACE INTO author_photos (author_url, photo_path) VALUES (?, ?)", (u, path))
                conn.commit()
    conn.close()
    return photos


def _start_author_photos_fetch_thread(app, author_links):
    """"will you scrape pictures when adding a new serie?" - "add it" - "but only when
    adding a new serie": récupère en arrière-plan les photos des auteurs d'une série
    tout juste ajoutée (add_series_from_bedetheque) - le SEUL déclencheur de scraping
    de photos dans toute l'app, voir _get_cached_author_photos pour le côté lecture.
    Thread dédié avec app.app_context() poussé explicitement - même précaution que
    _start_metadata_write_thread/Komga (voir CLAUDE.md, current_app indisponible par
    défaut dans un thread d'arrière-plan), et surtout _fetch_and_cache_author_photos
    peut prendre plusieurs secondes par auteur inconnu (anti-bot Bédéthèque) pour 2-4
    auteurs typiquement - largement trop long pour bloquer la réponse HTTP de l'ajout
    de série."""
    urls = list(dict.fromkeys((author_links or {}).values()))
    if not urls:
        return

    def _run():
        with app.app_context():
            try:
                _fetch_and_cache_author_photos(urls)
            except Exception as e:
                logger.warning(f"Échec récupération photos auteurs en arrière-plan: {e}")

    threading.Thread(target=_run, daemon=True).start()


@bedetheque_bp.route('/authors/photos', methods=['GET'])
def get_authors_photos():
    """Lecture seule des photos d'auteurs déjà en cache (voir _get_cached_author_photos
    - jamais de scraping ici, uniquement à l'ajout de série). Query param répété
    ?url=... (URLs de fiche auteur Bédéthèque, déjà connues via
    series.bedetheque_author_links) - GET plutôt que POST maintenant que cette route
    ne fait plus qu'une lecture DB.

    Retourne {"photos": {url: chemin_relatif_ou_vide}} - une URL absente du résultat
    veut simplement dire "pas encore en cache" (jamais ajoutée depuis ce changement,
    ou ajoutée mais le thread d'arrière-plan n'a rien trouvé)."""
    urls = request.args.getlist('url')
    urls = list(dict.fromkeys(u.strip() for u in urls if isinstance(u, str) and u.strip().startswith('https://www.bedetheque.com/')))
    if not urls:
        return jsonify({'success': True, 'photos': {}})
    try:
        photos = _get_cached_author_photos(urls)
        return jsonify({'success': True, 'photos': photos})
    except Exception as e:
        logger.error(f"Erreur lors de la récupération des photos auteurs: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@bedetheque_bp.route('/authors/photos/backfill', methods=['POST'])
def backfill_author_photos():
    """"can you scrape the author pictures. they don't show now" - rattrapage pour
    toutes les séries déjà en bibliothèque AVANT l'existence du scraping à l'ajout
    (_start_author_photos_fetch_thread, add_series_from_bedetheque): celles-ci n'ont
    jamais eu leurs auteurs scrapés puisque, "but only when adding a new serie", la
    fiche série elle-même ne scrape jamais (voir _get_cached_author_photos).

    Parcourt series.bedetheque_author_links de TOUTE la bibliothèque plutôt qu'une
    série à la fois - peut prendre plus d'une heure sur une grosse bibliothèque
    (anti-bot Bédéthèque ~3s/auteur, des centaines d'auteurs uniques typiquement),
    tourne donc dans un thread d'arrière-plan; cette requête répond immédiatement
    avec le nombre d'auteurs à traiter plutôt que d'attendre le résultat."""
    conn = get_db_connection()
    rows = conn.execute("SELECT bedetheque_author_links FROM series WHERE bedetheque_author_links IS NOT NULL").fetchall()
    conn.close()
    urls = set()
    for (raw,) in rows:
        try:
            links = json.loads(raw or '{}')
        except (TypeError, ValueError):
            continue
        if isinstance(links, dict):
            urls.update(u for u in links.values() if u)
    urls = list(urls)
    if not urls:
        return jsonify({'success': True, 'count': 0})

    app = current_app._get_current_object()

    def _run():
        with app.app_context():
            try:
                _fetch_and_cache_author_photos(urls)
                logger.info(f"Rattrapage photos auteurs terminé: {len(urls)} auteur(s)")
            except Exception as e:
                logger.warning(f"Échec rattrapage photos auteurs: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'success': True, 'count': len(urls)})


@bedetheque_bp.route('/info', methods=['GET'])
def get_bedetheque_info():
    """
    Récupère les infos détaillées d'une série (avec ses albums)
    Query params: ?url=bedetheque_url ou ?title=titre_serie[&raw=texte_source_brut]

    raw: optionnel, voir search_and_get_best_match - un appelant qui dispose du nom de
    fichier ORIGINAL (avant que parse_filename ne jette auteur/éditeur/année) le passe
    ici pour départager deux séries homonymes que le titre seul ne peut pas distinguer.
    """
    url = request.args.get('url', '').strip()
    title = request.args.get('title', '').strip()
    raw_hint = request.args.get('raw', '').strip() or None

    if not url and not title:
        return jsonify({'error': 'Paramètre url ou title requis'}), 400

    try:
        scraper = BedethequeScraper()

        if url:
            info = scraper.get_series_info(url)
        else:
            info = scraper.search_and_get_best_match(title, raw_hint=raw_hint)

        if not info:
            return jsonify({
                'success': False,
                'error': 'Série non trouvée sur Bedetheque'
            }), 404

        return jsonify({
            'success': True,
            'info': info
        })

    except Exception as e:
        logger.error(f"Erreur lors de la récupération des infos: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


def _perform_series_match(series_id, series_title, search_by, value, write_volumes=True):
    """Cœur de enrich_series (recherche Bédéthèque + sauvegarde + alignement titre/dossier)
    - factorisé pour être appelé directement par un appelant non-HTTP (voir
    run_library_onboarding, blueprints/library/onboarding.py) sans dupliquer cette
    logique. Retourne (success, info_or_None, error_message_or_None) plutôt que de
    construire une réponse Flask, pour rester utilisable hors requête."""
    scraper = BedethequeScraper()

    if search_by == 'url':
        info = scraper.get_series_info(value)
    else:
        info = scraper.search_and_get_best_match(value)

    if not info:
        return False, None, 'Impossible de trouver la série sur Bedetheque'

    db_manager = BedethequeDatabase(current_app.config['DATABASE'])
    save_result = db_manager.update_series_bedetheque_info(series_id, info)
    if not save_result:
        return False, None, f'Impossible de sauvegarder les infos pour la série #{series_id}'

    # Le matching seul ne met à jour que les champs de référence (résumé, genre,
    # couverture...) sur `series`, jamais series.title ni le <Title> des tomes (voir
    # _align_title_and_start_metadata_write): sans ça, matcher une série sur une
    # nouvelle fiche laissait le titre local et les albums figés sur l'ancien match
    # jusqu'à ce qu'on pense à cliquer séparément sur "Mettre à jour les métadonnées"
    _align_title_and_start_metadata_write(series_id, series_title, info, write_volumes=write_volumes)
    return True, info, None


@bedetheque_bp.route('/enrich/<int:series_id>', methods=['POST'])
def enrich_series(series_id):
    """
    Enrichit une série avec les infos Bedetheque
    Body: {
        "search_by": "title" ou "url",
        "value": "titre_serie" ou "bedetheque_url",
        "write_volumes": true (défaut) ou false
    }
    write_volumes=false: n'aligne que series.title + les champs de référence, sans lancer
    l'écriture ComicInfo de tous les tomes - utilisé par la page de vérification pour un
    matching non confirmé (recherche floue par titre, en masse ou pas): il ne faut pas
    réécrire des tomes qui ont potentiellement déjà les bonnes métadonnées tant que
    l'utilisateur n'a pas vérifié que le match trouvé est le bon (voir
    _align_title_and_start_metadata_write, déjà utilisé en scope "série uniquement" par
    update_metadata_series pour la même raison).
    """
    data = request.get_json()

    if not data:
        return jsonify({'error': 'Corps de requête JSON requis'}), 400

    search_by = data.get('search_by', 'title').lower()
    value = data.get('value', '').strip()
    write_volumes = data.get('write_volumes', True)

    if not value:
        return jsonify({'error': 'Paramètre value requis'}), 400

    if search_by not in ['title', 'url']:
        return jsonify({'error': 'search_by doit être "title" ou "url"'}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT title FROM series WHERE id = ?', (series_id,))
        series = cursor.fetchone()
        conn.close()

        if not series:
            return jsonify({'error': 'Série non trouvée'}), 404

        logger.info(f"Enrichissement de la série #{series_id} ({series['title']}) avec {search_by}={value}")

        success, info, error = _perform_series_match(series_id, series['title'], search_by, value, write_volumes=write_volumes)
        if not success:
            logger.warning(f"Aucune info trouvée pour la série #{series_id}: {error}")
            return jsonify({'success': False, 'error': error}), 404

        logger.info(f"Infos trouvées: {info.get('title')} - {len(info.get('volumes') or [])} albums")
        logger.info(f"✓ Série #{series_id} enrichie avec succès")

        return jsonify({
            'success': True,
            'info': info,
            'message': f"Infos récupérées pour: {info.get('title', series['title'])}"
        })

    except Exception as e:
        logger.error(f"Erreur lors de l'enrichissement: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


def _get_series_missing_match(library_id=None, ebdz_configured=True, komga_configured=False):
    """Séries avec au moins un fichier réellement possédé ("match existing folders on
    disk" - pas les tomes placeholder d'une série ajoutée depuis Bédéthèque sans aucun
    fichier, voir add_series_from_bedetheque) qui n'ont pas de match Bédéthèque et/ou
    EBDZ - alimente GET /missing-matches (tableau de diagnostic, onglet "Séries avec
    informations manquantes").

    ebdz_configured=False ("make sure that if komga or ebdz is not configured they
    dont show up in the table"): une série n'est jamais listée pour la seule raison
    qu'elle n'a pas de match EBDZ si EBDZ n'a pas d'identifiants configurés - ce serait
    signaler comme "manquant" quelque chose qui n'a de toute façon aucune chance
    d'aboutir tant que rien n'est configuré."""
    conn = get_db_connection()
    cursor = conn.cursor()
    sql = '''
        SELECT series.id, series.title, series.library_id, series.bedetheque_url,
               series.ebdz_match_status, series.ebdz_thread_url,
               series.komga_series_id, series.komga_url
        FROM series
        JOIN libraries ON libraries.id = series.library_id
        WHERE series.total_volumes > 0
    '''
    missing_conditions = ['bedetheque_url IS NULL']
    if ebdz_configured:
        missing_conditions.append("ebdz_match_status IS NULL OR ebdz_match_status != 'matched'")
    if komga_configured:
        missing_conditions.append("komga_series_id IS NULL OR komga_series_id = ''")
    sql += ' AND (' + ' OR '.join(missing_conditions) + ')'
    params = []
    if library_id:
        sql += ' AND library_id = ?'
        params.append(int(library_id))
    sql += ' ORDER BY title COLLATE NOCASE'
    cursor.execute(sql, params)
    rows = cursor.fetchall()
    conn.close()
    return rows


@bedetheque_bp.route('/missing-matches', methods=['GET'])
def get_series_missing_matches():
    """Tableau de diagnostic (onglet "Matching incorrect" de /bedetheque-enrich, renommé
    depuis "Séries avec informations manquantes"): liste les séries qui n'ont pas de match Bédéthèque et/ou EBDZ,
    pour matcher directement depuis cette page plutôt que d'ouvrir chaque fiche série une
    par une. ?library_id=N (optionnel) restreint à une bibliothèque, sinon toutes.
    ebdz_configured dans la réponse: le frontend s'en sert pour masquer entièrement la
    colonne EBDZ plutôt que de l'afficher toujours à "✗" (jamais actionnable tant que
    rien n'est configuré)."""
    from blueprints.ebdz.routes import is_ebdz_configured
    from blueprints.komga.config_store import is_komga_configured
    library_id = request.args.get('library_id', type=int)
    ebdz_configured = is_ebdz_configured()
    komga_configured = is_komga_configured()
    rows = _get_series_missing_match(library_id, ebdz_configured=ebdz_configured, komga_configured=komga_configured)
    return jsonify({
        'success': True,
        'ebdz_configured': ebdz_configured,
        'komga_configured': komga_configured,
        'series': [
            {
                'id': row['id'],
                'title': row['title'],
                'library_id': row['library_id'],
                'bedetheque_matched': bool(row['bedetheque_url']),
                'bedetheque_url': row['bedetheque_url'],
                'ebdz_matched': row['ebdz_match_status'] == 'matched',
                'ebdz_thread_url': row['ebdz_thread_url'],
                'komga_matched': bool(row['komga_series_id']),
                'komga_url': row['komga_url'],
            }
            for row in rows
        ]
    })


@bedetheque_bp.route('/series/<int:series_id>', methods=['GET'])
def get_series_bedetheque_info(series_id):
    """Récupère les infos Bedetheque sauvegardées d'une série"""
    try:
        db_manager = BedethequeDatabase(current_app.config['DATABASE'])
        info = db_manager.get_series_bedetheque_info(series_id)

        if info is None:
            return jsonify({'error': 'Série non trouvée'}), 404

        return jsonify(info)

    except Exception as e:
        logger.error(f"Erreur: {e}")
        return jsonify({'error': str(e)}), 500


@bedetheque_bp.route('/read-also/<int:series_id>', methods=['GET'])
def get_series_read_also(series_id):
    """Retourne les recommandations « À lire aussi » d'une série.

    Les anciennes fiches n'ont pas encore la nouvelle colonne cache remplie : dans ce
    cas, le premier clic déclenche un seul scraping de leur fiche Bédéthèque, puis la
    liste JSON est conservée pour les ouvertures suivantes. Le statut possédé est
    toujours recalculé depuis la table series.
    """
    try:
        conn = get_db_connection()
        row = conn.execute(
            'SELECT bedetheque_url, bedetheque_read_also FROM series WHERE id = ?',
            (series_id,)
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({'success': False, 'error': 'Série introuvable'}), 404

        cached = row['bedetheque_read_also']
        if cached is None and row['bedetheque_url']:
            info = BedethequeScraper().get_series_info(row['bedetheque_url'])
            read_also = info.get('read_also', []) if info else []
            conn.execute(
                'UPDATE series SET bedetheque_read_also = ? WHERE id = ?',
                (json.dumps(read_also), series_id)
            )
            conn.commit()
        else:
            try:
                read_also = json.loads(cached or '[]')
            except (TypeError, ValueError):
                read_also = []

        read_also = [item for item in (read_also if isinstance(read_also, list) else [])
                     if isinstance(item, dict) and item.get('url')]
        urls = [item['url'].rstrip('/') for item in read_also]
        owned_by_url = {}
        if urls:
            placeholders = ','.join('?' * len(urls))
            owned_rows = conn.execute(
                f"SELECT id, bedetheque_url FROM series WHERE rtrim(bedetheque_url, '/') IN ({placeholders})",
                urls
            ).fetchall()
            owned_by_url = {((r['bedetheque_url'] or '').rstrip('/')): r['id'] for r in owned_rows}
        for item in read_also:
            item['series_id'] = owned_by_url.get(item['url'].rstrip('/'))
        conn.close()
        return jsonify({'success': True, 'items': read_also})
    except Exception as e:
        logger.error(f"Erreur récupération À lire aussi série #{series_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@bedetheque_bp.route('/universe/<int:series_id>', methods=['GET'])
def get_series_universe(series_id):
    """"toutes ces series font parties du meme univers [...] Il faut aussi ajouter
    univers dans le renommage" - univers Bédéthèque de cette série (voir
    BedethequeDatabase.sync_series_universe) et ses membres, possédés ou non. `success:
    true, universe: null` (pas une erreur 404) si cette série n'appartient à aucun
    univers connu - la grande majorité des séries, pas de "Séries liées" sur Bédéthèque."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT universe_id, library_id FROM series WHERE id = ?', (series_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return jsonify({'error': 'Série non trouvée'}), 404

        universe_id, library_id = row[0], row[1]
        if not universe_id:
            conn.close()
            return jsonify({'success': True, 'universe': None})

        cursor.execute('SELECT name FROM universes WHERE id = ?', (universe_id,))
        universe_row = cursor.fetchone()
        cursor.execute(
            'SELECT bedetheque_url, title, series_id FROM universe_series WHERE universe_id = ? ORDER BY title COLLATE NOCASE',
            (universe_id,)
        )
        members = [
            {'title': m[1], 'bedetheque_url': m[0], 'series_id': m[2]}
            for m in cursor.fetchall()
        ]
        conn.close()

        return jsonify({
            'success': True,
            'universe': {
                'id': universe_id,
                'name': universe_row[0] if universe_row else None,
                'members': members,
                'library_id': library_id
            }
        })
    except Exception as e:
        logger.error(f"Erreur récupération univers série #{series_id}: {e}")
        return jsonify({'error': str(e)}), 500


@bedetheque_bp.route('/universe/<int:universe_id>/name', methods=['PUT'])
def rename_universe(universe_id):
    """"the universe is Aldebaran instead of Antares" - un univers est nommé d'après la
    PREMIÈRE série qui l'a détecté (voir sync_series_universe), pas forcément la série
    "de référence" du cycle du point de vue de l'utilisateur - renommable à la main
    depuis la fiche série (bouton crayon à côté du nom de l'univers).

    "renaming the universe will have to rename the files" - toute série DÉJÀ nichée sous
    l'ancien dossier d'univers (<ancien_nom>/<série>, voir _rename_series_folder) est
    déplacée vers le nouveau (une seule opération os.rename sur le dossier d'univers
    PARTAGÉ par bibliothèque, pas un renommage série par série) - sans ça le nom réel du
    dossier divergerait silencieusement du nom d'univers en base dès qu'une série du
    groupe qui n'a pas elle-même besoin d'être renommée. Une série pas encore nichée
    (jamais renommée depuis l'ajout de l'univers) n'est pas touchée : son prochain
    "Renommer" utilisera de toute façon le nouveau nom."""
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'success': False, 'error': 'Nom requis'}), 400

    from blueprints.library.routes import resolve_within, UnsafePathError, sanitize_path_component

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT name FROM universes WHERE id = ?', (universe_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return jsonify({'success': False, 'error': 'Univers introuvable'}), 404
        old_name = row['name']

        moved_folders = []
        move_errors = []
        if old_name and old_name != name:
            cursor.execute('''
                SELECT s.id, s.path, l.path AS library_path
                FROM series s JOIN libraries l ON s.library_id = l.id
                WHERE s.universe_id = ? AND s.path IS NOT NULL
            ''', (universe_id,))
            member_series = cursor.fetchall()

            # Regroupe par bibliothèque: le dossier d'univers est PARTAGÉ par toutes les
            # séries qui y sont nichées dans une même bibliothèque, un seul os.rename par
            # bibliothèque déplace donc tout leur contenu d'un coup.
            series_by_library = {}
            for s in member_series:
                expected_old_dir = os.path.join(s['library_path'], old_name)
                if os.path.dirname(s['path'].rstrip('/')) == expected_old_dir.rstrip('/'):
                    series_by_library.setdefault(s['library_path'], []).append(s)

            for library_path, series_list in series_by_library.items():
                try:
                    new_folder_name = sanitize_path_component(name, "Nom d'univers")
                    old_universe_dir = resolve_within(os.path.join(library_path, old_name), library_path)
                    new_universe_dir = resolve_within(os.path.join(library_path, new_folder_name), library_path)
                except UnsafePathError as e:
                    move_errors.append(str(e))
                    continue

                if not os.path.isdir(old_universe_dir) or os.path.exists(new_universe_dir):
                    move_errors.append(f"Dossier introuvable ou déjà existant : {new_universe_dir}")
                    continue

                try:
                    os.rename(old_universe_dir, new_universe_dir)
                except OSError as e:
                    move_errors.append(str(e))
                    continue

                for s in series_list:
                    series_basename = os.path.basename(s['path'].rstrip('/'))
                    new_series_path = os.path.join(new_universe_dir, series_basename)
                    if not os.path.isdir(new_series_path):
                        # Le dossier série n'est pas là où sa colonne `path` (déjà
                        # incohérente AVANT ce renommage) le laissait supposer - le
                        # contenu a quand même suivi le déplacement du dossier d'univers
                        # (os.rename déplace tout ce qu'il contient), il est juste nesté
                        # différemment que prévu (vu en réel: une série retrouvée nichée
                        # dans le dossier d'une AUTRE série du même univers). On le
                        # recherche plutôt que d'écrire un chemin en base qui n'existe
                        # pas sur le disque.
                        candidates = [os.path.join(dirpath, series_basename)
                                      for dirpath, dirnames, _ in os.walk(new_universe_dir)
                                      if series_basename in dirnames]
                        if len(candidates) == 1:
                            new_series_path = candidates[0]
                        else:
                            move_errors.append(
                                f"Série #{s['id']} : dossier « {series_basename} » introuvable "
                                f"sans ambiguïté après déplacement, chemin en base non mis à jour")
                            continue
                    cursor.execute('UPDATE series SET path = ? WHERE id = ?', (new_series_path, s['id']))
                    cursor.execute('SELECT id, filepath FROM volumes WHERE series_id = ? AND filepath IS NOT NULL', (s['id'],))
                    for v in cursor.fetchall():
                        new_filepath = v['filepath'].replace(s['path'], new_series_path, 1)
                        cursor.execute('UPDATE volumes SET filepath = ? WHERE id = ?', (new_filepath, v['id']))
                    moved_folders.append(new_series_path)

        cursor.execute('UPDATE universes SET name = ? WHERE id = ?', (name, universe_id))
        conn.commit()
        conn.close()

        if moved_folders:
            try:
                from blueprints.komga.client import trigger_scan_async
                trigger_scan_async()
            except Exception:
                pass
            try:
                from blueprints.library.action_history import log_action
                log_action('rename', None, name,
                           f"Univers renommé « {old_name} » → « {name} » ({len(moved_folders)} série(s) déplacée(s))")
            except Exception as e:
                logger.warning(f"Échec journalisation renommage univers #{universe_id}: {e}")

        return jsonify({'success': True, 'name': name, 'moved_folders': moved_folders, 'errors': move_errors})
    except Exception as e:
        logger.error(f"Erreur renommage univers #{universe_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# État de progression de _write_series_volumes_metadata_async, en mémoire (process
# unique, pas besoin de DB) et par série - consulté par GET
# /update-metadata/series/<id>/progress pour que l'UI affiche le tome en cours de
# traitement (le thread lui-même n'a aucun moyen de "pousser" cette info au client, qui
# doit donc la sonder pendant que le thread tourne). Purgé (entrée retirée) à la fin plutôt
# que marqué "done": une absence d'entrée veut dire "rien en cours pour cette série", plus
# simple pour le client que de devoir distinguer "terminé" de "jamais lancé".
# Sert aussi de garde anti-double-lancement (voir _align_title_and_start_metadata_write):
# la présence d'une entrée pour une série empêche d'en démarrer un deuxième thread
# d'écriture par-dessus (double-clic, ou enrich + MAJ métadonnées coup sur coup) qui
# réécrirait les mêmes cbz en parallèle et ferait disparaître l'entrée dès que le premier
# des deux threads termine, alors que le second tourne encore.
_metadata_write_progress = {}
_metadata_write_lock = threading.Lock()


def _write_series_volumes_metadata_async(app, db_path, series_id, series_title, info):
    """Écrit, en arrière-plan, les métadonnées Bedetheque dans le ComicInfo.xml de tous
    les volumes cbz d'une série déjà matchée. En thread séparé car le scraper espace
    volontairement ses requêtes de résumé par album (~2s chacune, voir
    fetch_album_description): pour une série de plusieurs dizaines de tomes, faire cette
    boucle dans la requête HTTP dépasserait le timeout d'un reverse proxy typique (30-60s)
    bien avant la fin, renvoyant une page d'erreur HTML au client au lieu du JSON attendu -
    alors que le traitement, lui, continuerait et finirait par réussir côté serveur.

    `app` (capturé dans le thread appelant, voir _align_title_and_start_metadata_write) est
    poussé en contexte ici: sans ça, trigger_scan_async() plus bas échouait silencieusement
    (RuntimeError "Working outside of application context", avalée par le except général)
    puisque ce thread n'a par défaut aucun contexte Flask actif."""
    from blueprints.library.scanner import LibraryScanner

    try:
        with app.app_context():
            conn = sqlite3.connect(db_path, timeout=120.0)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT id, volume_number, filename, filepath, format, comicinfo,
                       is_integral, integral_number, is_hs, hs_number, is_episode, episode_number
                FROM volumes WHERE series_id = ?
            ''', (series_id,))
            local_volumes = cursor.fetchall()
            is_oneshot_row = cursor.execute('SELECT is_oneshot FROM series WHERE id = ?', (series_id,)).fetchone()
            is_oneshot_series = bool(is_oneshot_row['is_oneshot']) if is_oneshot_row else False
            conn.close()

            scraper = BedethequeScraper()
            bd_volumes = info.get('volumes') or []

            scanner = LibraryScanner(db_path)
            updated = 0
            skipped = []
            errors = []
            total = len(local_volumes)

            for idx, lv in enumerate(local_volumes, start=1):
                bd_volume = match_bedetheque_volume(bd_volumes, dict(lv))
                # Même convention que l'affichage des tomes côté fiche série
                # (buildVolumeItemHtml, ci.title || filename): montrer le titre de
                # l'album quand on l'a déjà (celui de la fiche Bedetheque, pas encore
                # celui qu'on s'apprête à écrire dans son ComicInfo) plutôt que juste le
                # numéro de tome, pour rester cohérent avec ce que l'utilisateur voit
                # déjà ailleurs dans l'UI
                if lv['volume_number'] is not None:
                    label = f"Tome {lv['volume_number']}"
                    if bd_volume and bd_volume.get('title'):
                        label += f" - {bd_volume['title']}"
                else:
                    label = lv['filename']
                _metadata_write_progress[series_id] = {
                    'index': idx,
                    'total': total,
                    'label': label,
                }
                # Résumé propre à ce tome depuis la page de son album (la page série n'a
                # souvent pas de résumé, ou un seul identique pour tous les tomes). Mémoïsé
                # dans le dict ('' compris) pour ne pas multiplier les requêtes vers
                # Bedetheque en pure perte. Récupéré même pour un tome au format non
                # réinscriptible (cbr/pdf) ou un placeholder sans fichier: la DB reste
                # la référence (apply_volume_comicinfo écrit volumes.comicinfo dans tous les
                # cas, voir sa docstring) même si la propagation dans le fichier, elle,
                # reste soumise au format - restreindre la RÉCUPÉRATION du résumé au format
                # du fichier n'avait pas de sens, la fiche Bédéthèque existe indépendamment
                # de ce qu'on possède déjà.
                if bd_volume is not None and bd_volume.get('description') is None:
                    bd_volume['description'] = scraper.fetch_album_description(bd_volume.get('url'))
                fields = build_comicinfo_fields(lv['volume_number'], series_title, info, bd_volume)

                if not fields:
                    skipped.append({'filename': lv['filename'], 'reason': 'Aucune métadonnée Bedetheque à écrire pour ce tome'})
                    continue

                try:
                    # DB-first: volumes.comicinfo est mis à jour avant le fichier, qui
                    # n'en est qu'une projection (voir apply_volume_comicinfo) - plus de
                    # relecture du fichier après écriture, la DB fusionnée fait foi
                    apply_volume_comicinfo(db_path, lv['id'], lv['filepath'], lv['format'], fields)
                    updated += 1
                except UnsupportedFormatError as e:
                    skipped.append({'filename': lv['filename'], 'reason': str(e)})
                except Exception as e:
                    logger.error(f"Erreur écriture ComicInfo.xml pour {lv['filepath']}: {e}", exc_info=True)
                    errors.append({'filename': lv['filename'], 'error': str(e)})
                    continue

                _refresh_volume_cover(lv['id'], bd_volume)

                # "maj métadonnées should copy all the data from Bédéthèque. so tome 3
                # should have appeared" - même correctif que update_metadata_volume (voir
                # son commentaire): resynchronise volume_number/is_integral/is_hs/
                # is_episode/is_special depuis bd_volume (l'album réellement matché),
                # pas seulement le ComicInfo.xml - couvre aussi la MAJ "série entière"
                # (ce thread), pas seulement le bouton par tome.
                if bd_volume is not None:
                    classified = _classify_bedetheque_number(bd_volume.get('number'), bd_volume.get('title'), is_oneshot_series)
                    (new_volume_number, new_is_integral, new_integral_number, new_is_hs, new_hs_number,
                     new_is_episode, new_episode_number, new_is_special, new_special_label) = classified
                    conn2 = sqlite3.connect(db_path, timeout=120.0)
                    conn2.execute('''
                        UPDATE volumes SET volume_number = ?, is_integral = ?, integral_number = ?,
                                            is_hs = ?, hs_number = ?, is_episode = ?, episode_number = ?,
                                            is_special = ?, special_label = ?
                        WHERE id = ?
                    ''', (new_volume_number, int(new_is_integral), new_integral_number, int(new_is_hs), new_hs_number,
                          int(new_is_episode), new_episode_number, int(new_is_special), new_special_label, lv['id']))
                    conn2.commit()
                    conn2.close()

            # Les résumés récupérés ci-dessus (ligne "bd_volume['description'] = ...")
            # mutent directement les dicts de bd_volumes, qui SONT les éléments de
            # info['volumes'] (même référence, pas une copie) - les repersister dans
            # series.bedetheque_albums ici évite de les refetcher à chaque future MAJ
            # métadonnées, et surtout les rend disponibles à l'import (execute_import),
            # qui reconstruit le ComicInfo depuis ce blob sans jamais faire de requête
            # réseau - sans ce commit, un import ultérieur dans cette série retombait
            # systématiquement sur "titre mais pas de résumé".
            BedethequeDatabase(db_path).update_series_bedetheque_info(series_id, info)

            # Les résumé/genre/auteur locaux de la série sont dérivés des ComicInfo.xml des
            # volumes (voir update_series_stats): à recalculer maintenant qu'ils ont changé
            scanner.update_series_stats(series_id)

            logger.info(f"✓ Métadonnées Bedetheque écrites pour la série #{series_id}: {updated} mis à jour, "
                        f"{len(skipped)} ignorés, {len(errors)} erreurs")

            if updated:
                from blueprints.komga.client import trigger_scan_async
                trigger_scan_async()
    except Exception as e:
        logger.error(f"Erreur MAJ métadonnées en arrière-plan pour la série #{series_id}: {e}", exc_info=True)
    finally:
        _metadata_write_progress.pop(series_id, None)


def _start_metadata_write_thread(series_id, series_title, info, total_volumes):
    """Démarre (si aucune n'est déjà en cours pour cette série) l'écriture en tâche de
    fond des ComicInfo/résumés de tous les tomes - voir _write_series_volumes_metadata_async.
    Factorisé hors de _align_title_and_start_metadata_write pour être réutilisé par
    add_series_from_bedetheque: une série tout juste ajoutée n'a que des tomes
    placeholder (aucun fichier), mais ce même thread les couvre déjà (la requête SQL de
    _write_series_volumes_metadata_async ne filtre pas sur filepath) - le résumé de
    chaque album est ainsi déjà en base au moment où le premier import atterrit dans
    cette série, plutôt que de ne jamais l'être (voir commentaire "Aucun appel réseau"
    dans execute_import).

    Retourne True si un thread a effectivement démarré, False si une écriture était déjà
    en cours (pas une erreur, juste rien à faire de plus)."""
    with _metadata_write_lock:
        if series_id in _metadata_write_progress:
            return False
        _metadata_write_progress[series_id] = {'index': 0, 'total': total_volumes, 'label': 'Préparation...'}

    db_path = current_app.config['DATABASE']
    app = current_app._get_current_object()
    threading.Thread(
        target=_write_series_volumes_metadata_async,
        args=(app, db_path, series_id, series_title, info),
        daemon=True
    ).start()
    return True


def _align_title_and_start_metadata_write(series_id, series_title, info, write_volumes=True):
    """Aligne series.title sur le titre Bedetheque (voir commentaire ci-dessous) et, si
    write_volumes (par défaut), lance en arrière-plan l'écriture du ComicInfo.xml de tous
    les tomes cbz de la série. Partagé entre update_metadata_series (bouton dédié, les
    deux options "série + tomes" et "série seule") et enrich_series (matching manuel/auto):
    sans ce partage, matcher une série sur une nouvelle fiche
    Bedetheque ne mettait à jour que les champs de référence (résumé, genre,
    couverture...) sur `series`, jamais le titre affiché ni le <Title> des tomes - il
    fallait ensuite penser à cliquer sur l'action "Mettre à jour les métadonnées"
    séparément pour que ça se propage.
    write_volumes=False (option "MAJ série uniquement"): aligne juste series.title, sans
    toucher aux fichiers - utile quand on veut rafraîchir résumé/couverture/statut/titre
    sans relancer une écriture ComicInfo coûteuse (~2s/tome) sur toute la série."""
    conn = get_db_connection()
    cursor = conn.cursor()

    # Aligne series.title sur le titre Bedetheque à chaque match/MAJ métadonnées: sans
    # ça, le titre local restait figé sur celui déduit du nom de fichier au premier scan,
    # et le renommage de dossier au format standard n'avait donc jamais rien à changer,
    # même après un match Bedetheque correct
    bd_title = (info.get('title') or '').strip()
    if bd_title and bd_title != series_title:
        cursor.execute('UPDATE series SET title = ? WHERE id = ?', (bd_title, series_id))
        series_title = bd_title

    conn.commit()

    if bd_title:
        try:
            from blueprints.library.routes import _fetch_series_for_rename, _rename_series_folder, _log_rename_action
            from blueprints.settings.rename_config_store import load_rename_config
            series_for_rename = _fetch_series_for_rename(cursor, series_id)
            if series_for_rename and series_for_rename['path']:
                rename_cfg = load_rename_config()
                folder_result = _rename_series_folder(
                    conn, series_id, series_for_rename['path'], bd_title,
                    series_for_rename['library_path'], {}, rename_cfg['series_template'],
                    universe_name=series_for_rename['universe_name']
                )
                if folder_result and folder_result.get('success') and folder_result.get('changed'):
                    _log_rename_action(series_id, bd_title, [], folder_result)
                    # Mutation disque hors des chemins qui déclenchent déjà leur propre
                    # rescan Komga plus loin (voir CLAUDE.md, tout chemin qui touche au
                    # système de fichiers doit le faire) - pas conditionné à write_volumes,
                    # ce déplacement a lieu même en scope "série uniquement".
                    from blueprints.komga.client import trigger_scan_async
                    trigger_scan_async()
                elif not (folder_result and folder_result.get('success')):
                    logger.warning(f"Renommage automatique du dossier échoué pour la série #{series_id}: {folder_result}")
        except Exception as e:
            logger.warning(f"Renommage automatique du dossier échoué pour la série #{series_id}: {e}")

    conn.close()

    # Crée une ligne placeholder pour tout album de la fiche pas encore représenté en
    # base (numéroté, intégrale ou hors-série) - idempotent, voir
    # _sync_bedetheque_placeholder_volumes. Fait à chaque match/MAJ métadonnées, pas
    # seulement à l'ajout initial d'une série: une série déjà en bibliothèque dont
    # Bédéthèque publie un nouveau tome (ou qui a été matchée avant l'existence des
    # tomes placeholder) récupère ainsi les tomes manquants sans devoir être re-ajoutée.
    try:
        _sync_bedetheque_placeholder_volumes(series_id, info, BedethequeScraper())
    except Exception as e:
        logger.warning(f"Échec synchronisation des tomes placeholder pour la série #{series_id}: {e}")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) AS n FROM volumes WHERE series_id = ?', (series_id,))
    total_volumes = cursor.fetchone()['n']
    conn.commit()
    conn.close()

    already_running = False
    if write_volumes:
        started = _start_metadata_write_thread(series_id, series_title, info, total_volumes)
        already_running = not started

    return series_title, total_volumes, already_running


@bedetheque_bp.route('/update-metadata/series/<int:series_id>', methods=['POST'])
def update_metadata_series(series_id):
    """
    Met à jour les métadonnées Bedetheque d'une série (s'assure d'abord qu'un match
    Bedetheque existe). Body JSON optionnel {"scope": "series"} pour se limiter à
    series.title + aux champs de référence (résumé, couverture, statut...), sans toucher
    aux fichiers - par défaut ("all", ou body absent) écrit aussi le ComicInfo.xml de
    tous les volumes cbz. L'écriture des volumes (une requête réseau par tome pour son
    résumé, volontairement espacées par le scraper) se fait en arrière-plan (voir
    _write_series_volumes_metadata_async): cette route ne fait que le matching, rapide,
    et répond aussitôt un thread lancé - sans quoi une série de plusieurs dizaines de
    tomes ferait dépasser le timeout d'un reverse proxy avant la fin de la requête. Les
    cbr/pdf sont ignorés (pas de réécriture possible pour ces formats), un échec sur
    un volume ne bloque pas les suivants.

    La conversion cbr->cbz est une action séparée (voir POST /convert-cbr/<volume_id>),
    déclenchée depuis une icône dédiée sur les tomes cbr - pas mélangée à cette mise à jour.
    """
    try:
        data = request.get_json(silent=True) or {}
        write_volumes = data.get('scope', 'all') != 'series'

        # "add MAJ metadonnées as a way to reload the bedetheque entries... i did not
        # delete the volumes. refresh metadata should recover the missing volumes" - un
        # tome réellement présent sur disque mais mal numéroté en base (ex: bug de
        # parsing déjà corrigé depuis, voir LibraryScanner.parse_filename) restait
        # invisible pour le matching Bédéthèque tant qu'un rescan MANUEL séparé n'avait
        # pas eu lieu. Rescan rapide de la série AVANT le matching (réutilise page_count/
        # ComicInfo déjà en cache pour tout fichier inchangé, ne retraite vraiment que ce
        # qui a changé) plutôt que de faire confiance à volumes.volume_number tel que
        # figé en base au dernier scan, potentiellement périmé.
        from blueprints.library.scanner import LibraryScanner
        try:
            LibraryScanner(current_app.config['DATABASE']).scan_single_series(series_id)
        except Exception as e:
            logger.warning(f"Rescan préalable à la MAJ métadonnées ignoré pour la série #{series_id}: {e}")

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT id, title, bedetheque_url FROM series WHERE id = ?', (series_id,))
        series = cursor.fetchone()

        if not series:
            conn.close()
            return jsonify({'error': 'Série non trouvée'}), 404

        db_manager = BedethequeDatabase(current_app.config['DATABASE'])
        scraper = BedethequeScraper()
        volume_web_hint = _find_volume_bedetheque_web_hint(cursor, series_id) if not series['bedetheque_url'] else None

        conn.close()

        info = _ensure_bedetheque_match(series_id, series['title'], series['bedetheque_url'], db_manager, scraper, volume_web_hint)
        if not info:
            return jsonify({'success': False, 'error': 'Impossible de trouver la série sur Bedetheque'}), 404

        db_manager.update_series_bedetheque_info(series_id, info)

        _, total_volumes, already_running = _align_title_and_start_metadata_write(series_id, series['title'], info, write_volumes=write_volumes)

        return jsonify({
            'success': True,
            'started': write_volumes and not already_running,
            'already_running': already_running,
            'total_volumes': total_volumes
        })

    except Exception as e:
        logger.error(f"Erreur lors de la mise à jour des métadonnées de la série #{series_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@bedetheque_bp.route('/update-metadata/series/<int:series_id>/progress', methods=['GET'])
def update_metadata_series_progress(series_id):
    """Renvoie la progression de l'écriture ComicInfo en arrière-plan pour cette série
    (voir _metadata_write_progress), sondée côté client pendant qu'un thread lancé par
    update_metadata_series tourne pour afficher le tome en cours de traitement. Pas
    d'entrée (progress: null) = rien en cours pour cette série (jamais lancé, ou déjà
    terminé - _write_series_volumes_metadata_async retire son entrée en quittant)."""
    return jsonify({'success': True, 'progress': _metadata_write_progress.get(series_id)})


def _resolve_volume_bedetheque_fields(cursor, vol, prefer_cache=False):
    """Retrouve le match Bedetheque de la série de ce volume puis calcule les champs
    ComicInfo (Title/Summary/Writer/...) à lui écrire, sans rien écrire nulle part -
    factorisé hors de update_metadata_volume pour être réutilisé tel quel par le preview
    en lecture seule (GET /volume-preview/<id>, voir la modale "Éditer manuellement").

    prefer_cache=True: tente d'abord _get_cached_series_bedetheque_info (aucun réseau) et
    ne retombe sur le fetch réseau complet de _ensure_bedetheque_match que si le cache est
    vide OU n'a pas ce tome précis (série mise à jour côté Bédéthèque depuis le dernier
    scrape complet) - pour les appelants qui n'ont pas besoin de données garanties
    fraîches (preview, avis lecteurs, rattachement d'un lien). update_metadata_volume,
    dont le but même est de rafraîchir depuis la source, garde prefer_cache=False.

    Retourne (fields, error, bd_volume): error est un message si aucun match/aucune
    métadonnée disponible (fields est {} dans ce cas, bd_volume peut quand même être
    non-None si un album a été trouvé mais que build_comicinfo_fields n'en a rien tiré).
    bd_volume (dict brut de get_series_info, avec 'cover_url') est renvoyé pour que
    l'appelant puisse aussi rafraîchir cover_path - "le cover tome 16 n'est pas bon et MAJ
    metadatas ... ne change rien": apply_volume_comicinfo n'a jamais touché cover_path,
    figé depuis la création du placeholder/l'import, jamais revu par une MAJ métadonnées
    ultérieure."""
    series_id = vol['series_id']

    db_manager = BedethequeDatabase(current_app.config['DATABASE'])
    scraper = BedethequeScraper()

    info = None
    bd_volume = None
    info_is_partial_cache = False
    if prefer_cache:
        info = _get_cached_series_bedetheque_info(cursor, series_id)
        if info is not None:
            bd_volume = match_bedetheque_volume(info.get('volumes') or [], dict(vol))
            if bd_volume is None:
                info = None
            else:
                info_is_partial_cache = True

    if info is None:
        volume_web_hint = _find_volume_bedetheque_web_hint(cursor, series_id) if not vol['bedetheque_url'] else None
        info = _ensure_bedetheque_match(series_id, vol['series_title'], vol['bedetheque_url'], db_manager, scraper, volume_web_hint)
        if not info:
            return {}, 'Impossible de trouver la série sur Bedetheque', None
        bd_volume = match_bedetheque_volume(info.get('volumes') or [], dict(vol))
    # Résumé propre à ce tome depuis la page de son album (voir update_metadata_series -
    # même choix de ne plus restreindre la récupération au format réinscriptible)
    if bd_volume is not None and bd_volume.get('description') is None:
        bd_volume['description'] = scraper.fetch_album_description(bd_volume.get('url'))
        if info_is_partial_cache:
            # info vient de _get_cached_series_bedetheque_info: un dict PARTIEL (seuls les
            # champs utilisés par build_comicinfo_fields). Passer ça à
            # update_series_bedetheque_info écraserait toutes les autres colonnes série
            # (status, total_volumes, cover_path, dates...) avec des None - on ne persiste
            # donc que la colonne bedetheque_albums elle-même.
            _persist_cached_bd_volume_description(series_id, info['volumes'])
        else:
            # Repersisté dans series.bedetheque_albums (même raison que dans
            # _write_series_volumes_metadata_async): sinon ce résumé, fraîchement récupéré
            # au prix d'une requête réseau, resterait local à cet appel et serait refetché à
            # la prochaine consultation de ce tome ou de la série.
            db_manager.update_series_bedetheque_info(series_id, info)
    fields = build_comicinfo_fields(vol['volume_number'], vol['series_title'], info, bd_volume)

    if not fields:
        return {}, 'Aucune métadonnée Bedetheque disponible pour ce tome', bd_volume
    return fields, None, bd_volume


def _refresh_volume_cover(volume_id, bd_volume):
    """Retélécharge et met à jour cover_path depuis bd_volume['cover_url'] - séparé
    d'apply_volume_comicinfo, qui ne touche JAMAIS cover_path (voir son docstring/
    _resolve_volume_bedetheque_fields). Sans cet appel explicite après une MAJ
    métadonnées, la couverture restait celle figée au moment de la création du
    placeholder ou de l'import initial, jamais revue même quand le bon album Bédéthèque
    change ensuite - "le cover tome 16 n'est pas bon et MAJ metadatas ... ne change
    rien". Best-effort (ne lève jamais): une couverture ratée ne doit pas faire échouer
    la MAJ métadonnées elle-même, qui a déjà réussi côté ComicInfo à ce stade."""
    if not bd_volume or not bd_volume.get('cover_url'):
        return
    try:
        cover_path = BedethequeScraper()._download_cover(bd_volume['cover_url'], './data/covers')
        if cover_path:
            conn = get_db_connection()
            conn.execute('UPDATE volumes SET cover_path = ? WHERE id = ?', (cover_path, volume_id))
            conn.commit()
            conn.close()
    except Exception as e:
        logger.warning(f"Rafraîchissement de couverture échoué pour le volume #{volume_id}: {e}")


@bedetheque_bp.route('/volume-preview/<int:volume_id>', methods=['GET'])
def preview_volume_bedetheque_metadata(volume_id):
    """Calcule ce que POST /update-metadata/volume/<id> écrirait pour ce tome, SANS rien
    écrire (ni fichier ni DB) - utilisé par le bouton "Charger depuis Bédéthèque" de la
    modale d'édition manuelle: l'utilisateur veut voir/ajuster les valeurs dans le
    formulaire avant de les enregistrer lui-même via "Enregistrer ce tome", pas les
    appliquer directement.

    prefer_cache=True: une prévisualisation n'a pas besoin de données garanties fraîches
    (l'utilisateur peut toujours relire/corriger dans le formulaire avant d'enregistrer) -
    évite un aller-retour réseau complet vers la fiche série quand series.bedetheque_albums
    l'a déjà en cache (voir _get_cached_series_bedetheque_info)."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT v.id, v.series_id, v.volume_number, v.filename, v.filepath, v.format,
                   v.is_integral, v.integral_number, v.is_hs, v.hs_number, v.is_episode, v.episode_number,
                   s.title AS series_title, s.bedetheque_url
            FROM volumes v
            JOIN series s ON v.series_id = s.id
            WHERE v.id = ?
        ''', (volume_id,))
        vol = cursor.fetchone()

        if not vol:
            conn.close()
            return jsonify({'error': 'Volume non trouvé'}), 404

        fields, error, _bd_volume = _resolve_volume_bedetheque_fields(cursor, vol, prefer_cache=True)
        conn.close()

        if error:
            return jsonify({'success': False, 'error': error}), 404

        return jsonify({'success': True, 'fields': fields})

    except Exception as e:
        logger.error(f"Erreur lors du preview Bedetheque du volume #{volume_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@bedetheque_bp.route('/reviews/volume/<int:volume_id>', methods=['GET'])
def get_volume_bedetheque_reviews(volume_id):
    """Avis de lecteurs Bédéthèque pour la page d'album de ce tome ("créer une modale
    quand je clique sur review"). L'URL d'album utilisée est celle déjà figée dans le
    ComicInfo <Web> du tome (écrite par une précédente MAJ métadonnées) - pas de
    re-matching ici, ce serait un aller-retour réseau (recherche + fiche série) juste
    pour rouvrir une modale de lecture. Sans <Web> d'album exploitable (tome jamais mis à
    jour, ou pointant seulement vers la fiche série faute de match précis), on retombe
    sur _resolve_volume_bedetheque_fields comme /volume-preview pour retrouver l'album."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT v.id, v.series_id, v.volume_number, v.filename, v.filepath, v.format,
                   v.is_integral, v.integral_number, v.is_hs, v.hs_number, v.is_episode, v.episode_number,
                   v.comicinfo, s.title AS series_title, s.bedetheque_url
            FROM volumes v
            JOIN series s ON v.series_id = s.id
            WHERE v.id = ?
        ''', (volume_id,))
        vol = cursor.fetchone()

        if not vol:
            conn.close()
            return jsonify({'success': False, 'error': 'Volume non trouvé'}), 404

        album_url = None
        try:
            ci = json.loads(vol['comicinfo']) if vol['comicinfo'] else {}
        except (TypeError, ValueError):
            ci = {}
        web = ci.get('web')
        if web and 'bedetheque.com' in web and '/serie-' not in web:
            album_url = web

        if not album_url:
            _fields, _error, bd_volume = _resolve_volume_bedetheque_fields(cursor, vol, prefer_cache=True)
            if bd_volume and bd_volume.get('url'):
                album_url = bd_volume['url']

        conn.close()

        if not album_url:
            return jsonify({'success': False, 'error': "Aucune page d'album Bédéthèque trouvée pour ce tome"}), 404

        reviews = BedethequeScraper().get_album_reviews(album_url)
        return jsonify({'success': True, 'album_url': album_url, 'reviews': reviews})

    except Exception as e:
        logger.error(f"Erreur lors de la récupération des avis Bedetheque du volume #{volume_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


def _link_single_volume_to_bedetheque(cursor, db_path, vol):
    """Cœur de link_volume_to_bedetheque, factorisé pour être réutilisé tel quel par le
    job en lot (_link_volumes_batch_async) - "Tomes possédés sans lien Bédéthèque ->
    solution is to MAJ metadonnées. mais il faut juste ajouter le lien bedetheque. pas
    besoin de tout mettre à jour": contrairement à update-metadata/volume (qui réécrit
    Title/Summary/Writer/... au complet), n'écrit QUE le champ Web - apply_volume_comicinfo
    fusionne dans le ComicInfo existant (voir son docstring), les autres champs déjà
    présents (souvent déjà corrects sur un tome possédé, juste jamais rattaché à un album
    précis) restent intacts plutôt que d'être recalculés/écrasés pour rien.

    "Rattachement Bédéthèque is very slow" - _resolve_volume_bedetheque_fields (via
    _ensure_bedetheque_match) refait TOUJOURS un fetch réseau complet de la page série
    (+ anti-bot delay ~2.5-4.5s), même quand la série est déjà matchée ET que sa liste
    d'albums est déjà en cache (series.bedetheque_albums, alimenté par le dernier "MAJ
    métadonnées" - voir update_series_bedetheque_info). Comme cette action n'a besoin que
    de retrouver l'URL d'un album déjà listé (pas de données plus fraîches), prefer_cache=True
    tente d'abord ce cache local (aucun réseau, voir _get_cached_series_bedetheque_info) et
    ne retombe sur un fetch complet que s'il est vide (série jamais entièrement scrapée).

    Retourne (success, error, new_comicinfo)."""
    fields, error, _bd_volume = _resolve_volume_bedetheque_fields(cursor, vol, prefer_cache=True)
    web_url = fields.get('Web') if not error else None

    if not web_url:
        return False, "Aucun lien d'album Bédéthèque trouvé pour ce tome", None

    try:
        new_comicinfo = apply_volume_comicinfo(db_path, vol['id'], vol['filepath'], vol['format'], {'Web': web_url})
    except UnsupportedFormatError:
        if vol['filepath']:
            raise
        conn2 = sqlite3.connect(db_path, timeout=30.0)
        conn2.row_factory = sqlite3.Row
        row2 = conn2.execute('SELECT comicinfo FROM volumes WHERE id = ?', (vol['id'],)).fetchone()
        conn2.close()
        new_comicinfo = json.loads(row2['comicinfo']) if row2 and row2['comicinfo'] else {}

    return True, None, new_comicinfo


@bedetheque_bp.route('/link-volume/<int:volume_id>', methods=['POST'])
def link_volume_to_bedetheque(volume_id):
    """Rattache un seul tome POSSÉDÉ à son album Bédéthèque - voir
    _link_single_volume_to_bedetheque pour le détail (cache-first, écrit seulement Web).
    Pour rattacher plusieurs tomes d'un coup depuis /verification, voir POST
    /link-volumes-batch ci-dessous (tâche de fond, contrairement à cette route qui répond
    en synchrone - un seul tome ne justifie pas l'infrastructure d'un thread)."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT v.id, v.series_id, v.volume_number, v.filename, v.filepath, v.format,
                   v.is_integral, v.integral_number, v.is_hs, v.hs_number, v.is_episode, v.episode_number,
                   s.title AS series_title, s.bedetheque_url
            FROM volumes v
            JOIN series s ON v.series_id = s.id
            WHERE v.id = ?
        ''', (volume_id,))
        vol = cursor.fetchone()

        if not vol:
            conn.close()
            return jsonify({'error': 'Volume non trouvé'}), 404

        success, error, new_comicinfo = _link_single_volume_to_bedetheque(cursor, current_app.config['DATABASE'], vol)
        conn.close()

        if not success:
            return jsonify({'success': False, 'error': error}), 404

        logger.info(f"✓ Lien Bédéthèque ajouté pour le volume #{volume_id} ({vol['filename']})")

        from blueprints.komga.client import trigger_scan_async
        trigger_scan_async()

        return jsonify({'success': True, 'comicinfo': new_comicinfo})

    except UnsupportedFormatError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logger.error(f"Erreur lors du rattachement Bédéthèque du volume #{volume_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


_link_volumes_job = {}
_link_volumes_job_lock = threading.Lock()


def _link_volumes_batch_async(app, db_path, volume_ids):
    """Rattache en arrière-plan une liste de tomes à leur album Bédéthèque (un thread par
    job, voir _link_volumes_job ci-dessus) - chaque cache-hit (cas courant, voir
    _link_single_volume_to_bedetheque) est quasi instantané, seul un tome jamais scrapé
    retombe sur un fetch réseau complet, donc pas besoin de l'espacement anti-bot d'une
    vraie boucle de scraping ici."""
    try:
        with app.app_context():
            conn = sqlite3.connect(db_path, timeout=120.0)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            total = len(volume_ids)
            linked = 0
            failed = []

            for idx, volume_id in enumerate(volume_ids, start=1):
                cursor.execute('''
                    SELECT v.id, v.series_id, v.volume_number, v.filename, v.filepath, v.format,
                           v.is_integral, v.integral_number, v.is_hs, v.hs_number, v.is_episode, v.episode_number,
                           s.title AS series_title, s.bedetheque_url
                    FROM volumes v
                    JOIN series s ON v.series_id = s.id
                    WHERE v.id = ?
                ''', (volume_id,))
                vol = cursor.fetchone()
                _link_volumes_job['progress'] = {
                    'index': idx,
                    'total': total,
                    'label': vol['series_title'] if vol else f"#{volume_id}",
                }

                if not vol:
                    failed.append({'volume_id': volume_id, 'title': f"#{volume_id}", 'error': 'Volume non trouvé'})
                    continue

                try:
                    success, error, _new_comicinfo = _link_single_volume_to_bedetheque(cursor, db_path, vol)
                except Exception as e:
                    logger.error(f"Erreur rattachement Bédéthèque du volume #{volume_id}: {e}", exc_info=True)
                    success, error = False, str(e)

                if success:
                    linked += 1
                else:
                    failed.append({'volume_id': volume_id, 'title': vol['series_title'], 'error': error})

            conn.close()

            logger.info(f"✓ Rattachement Bédéthèque en lot terminé: {linked}/{total} rattachés")

            if linked:
                from blueprints.komga.client import trigger_scan_async
                trigger_scan_async()

            _link_volumes_job['result'] = {'linked': linked, 'total': total, 'failed': failed}
    except Exception as e:
        logger.error(f"Erreur rattachement Bédéthèque en lot: {e}", exc_info=True)
        _link_volumes_job['result'] = {'linked': 0, 'total': len(volume_ids), 'failed': [], 'error': str(e)}
    finally:
        _link_volumes_job['running'] = False


@bedetheque_bp.route('/link-volumes-batch', methods=['POST'])
def link_volumes_batch():
    """Lance en tâche de fond le rattachement Bédéthèque d'une liste de tomes (bouton
    "Rattacher à Bédéthèque (N)" de /verification, sélection multiple) - voir
    _link_volumes_batch_async. Un seul job à la fois (409 sinon), même garde que
    _start_metadata_write_thread."""
    data = request.get_json(silent=True) or {}
    volume_ids = data.get('volume_ids') or []
    if not volume_ids:
        return jsonify({'success': False, 'error': 'Aucun volume sélectionné'}), 400

    with _link_volumes_job_lock:
        if _link_volumes_job.get('running'):
            return jsonify({'success': False, 'error': 'Un rattachement en lot est déjà en cours'}), 409
        _link_volumes_job.clear()
        _link_volumes_job['running'] = True
        _link_volumes_job['progress'] = {'index': 0, 'total': len(volume_ids), 'label': 'Préparation...'}

    db_path = current_app.config['DATABASE']
    app = current_app._get_current_object()
    threading.Thread(
        target=_link_volumes_batch_async,
        args=(app, db_path, volume_ids),
        daemon=True
    ).start()

    return jsonify({'success': True, 'started': True, 'total': len(volume_ids)})


@bedetheque_bp.route('/link-volumes-batch/progress', methods=['GET'])
def link_volumes_batch_progress():
    """Progression du job lancé par POST /link-volumes-batch, sondée côté client - même
    convention que /update-metadata/series/<id>/progress (index/total/label), plus
    'running'/'result' puisque ce job n'est pas rattaché à une série précise dont
    l'absence d'entrée suffirait à signaler la fin."""
    return jsonify({
        'success': True,
        'running': _link_volumes_job.get('running', False),
        'progress': _link_volumes_job.get('progress'),
        'result': _link_volumes_job.get('result'),
    })


@bedetheque_bp.route('/update-metadata/volume/<int:volume_id>', methods=['POST'])
def update_metadata_volume(volume_id):
    """
    Écrit les métadonnées Bedetheque dans le ComicInfo.xml d'un seul volume (s'assure
    d'abord qu'un match Bedetheque existe pour sa série)

    Un tome cbr lève UnsupportedFormatError (HTTP 400) - la conversion cbr->cbz est une
    action séparée (voir POST /convert-cbr/<volume_id>), pas mélangée à cette mise à jour.
    """
    from blueprints.library.scanner import LibraryScanner

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT v.id, v.series_id, v.volume_number, v.filename, v.filepath, v.format,
                   v.is_integral, v.integral_number, v.is_hs, v.hs_number, v.is_episode, v.episode_number,
                   s.title AS series_title, s.bedetheque_url, s.is_oneshot
            FROM volumes v
            JOIN series s ON v.series_id = s.id
            WHERE v.id = ?
        ''', (volume_id,))
        vol = cursor.fetchone()

        if not vol:
            conn.close()
            return jsonify({'error': 'Volume non trouvé'}), 404

        fields, error, bd_volume = _resolve_volume_bedetheque_fields(cursor, vol)
        conn.close()

        if error:
            return jsonify({'success': False, 'error': error}), 404

        # DB-first (voir apply_volume_comicinfo): volumes.comicinfo est mis à jour avant
        # le fichier, qui n'en est qu'une projection - plus de relecture du fichier après
        # écriture, la DB fusionnée fait foi
        try:
            new_comicinfo = apply_volume_comicinfo(current_app.config['DATABASE'], volume_id, vol['filepath'], vol['format'], fields)
        except UnsupportedFormatError:
            # "Le Beurre numero 16 je ne peux pas mettre à jour... Format '' non
            # supporté" - un tome PLACEHOLDER (pas encore de fichier, voir
            # add_series_from_bedetheque/_sync_bedetheque_placeholder_volumes) a
            # filepath/format NULL: apply_volume_comicinfo a déjà écrit volumes.comicinfo
            # (elle le fait TOUJOURS en premier, voir son docstring) avant de lever cette
            # exception en tentant de propager dans un fichier qui n'existe pas encore -
            # ce n'est pas une vraie erreur dans ce cas précis (rien à convertir en cbz,
            # contrairement à un cbr/pdf réel déjà sur disque), juste rien de plus à
            # faire tant que le fichier n'est pas importé.
            if vol['filepath']:
                raise
            conn2 = get_db_connection()
            row2 = conn2.execute('SELECT comicinfo FROM volumes WHERE id = ?', (volume_id,)).fetchone()
            conn2.close()
            new_comicinfo = json.loads(row2['comicinfo']) if row2 and row2['comicinfo'] else {}

        _refresh_volume_cover(volume_id, bd_volume)

        # "maj métadonnées should copy all the data from Bédéthèque. so tome 3 should have
        # appeared" - à raison: jusqu'ici cette route ne réécrivait que le ComicInfo.xml
        # (apply_volume_comicinfo ci-dessus), jamais volumes.volume_number/is_integral/
        # is_hs/is_episode/is_special - les colonnes structurelles que le frontend utilise
        # réellement pour le regroupement Tomes/Spéciaux. Un tome mal classifié localement
        # (ex: matché avant correction contre la mauvaise série Bédéthèque) restait donc
        # affiché "Spécial" indéfiniment même après une MAJ métadonnées réussie. bd_volume
        # est l'album RÉELLEMENT matché pour ce tome (voir _resolve_volume_bedetheque_
        # fields/match_bedetheque_volume) - sa classification prime, c'est tout le sens de
        # cette action ("copier les données depuis Bédéthèque").
        if bd_volume is not None:
            classified = _classify_bedetheque_number(bd_volume.get('number'), bd_volume.get('title'), bool(vol['is_oneshot']))
            (new_volume_number, new_is_integral, new_integral_number, new_is_hs, new_hs_number,
             new_is_episode, new_episode_number, new_is_special, new_special_label) = classified
            conn3 = get_db_connection()
            conn3.execute('''
                UPDATE volumes SET volume_number = ?, is_integral = ?, integral_number = ?,
                                    is_hs = ?, hs_number = ?, is_episode = ?, episode_number = ?,
                                    is_special = ?, special_label = ?
                WHERE id = ?
            ''', (new_volume_number, int(new_is_integral), new_integral_number, int(new_is_hs), new_hs_number,
                  int(new_is_episode), new_episode_number, int(new_is_special), new_special_label, volume_id))
            conn3.commit()
            conn3.close()

        scanner = LibraryScanner()
        scanner.update_series_stats(vol['series_id'])

        logger.info(f"✓ Métadonnées Bedetheque écrites pour le volume #{volume_id} ({vol['filename']})")

        from blueprints.komga.client import trigger_scan_async
        trigger_scan_async()

        return jsonify({'success': True, 'comicinfo': new_comicinfo})

    except UnsupportedFormatError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logger.error(f"Erreur lors de la mise à jour des métadonnées du volume #{volume_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


def _classify_bedetheque_number(number, title, is_oneshot_series):
    """Classification structurelle (volume_number/is_integral/is_hs/is_episode/is_special
    + numéros associés) d'un album Bédéthèque, à partir de son `number` (int ou None) et
    son titre - factorisée hors de _sync_bedetheque_placeholder_volumes (qui a le même
    besoin pour créer un placeholder) pour être RÉUTILISÉE par update_metadata_volume/
    _write_series_volumes_metadata_async ("maj métadonnées should copy all the data from
    Bédéthèque. so tome 3 should have appeared" - à raison: ces deux actions ne réécrivent
    jusqu'ici que le ComicInfo.xml (voir apply_volume_comicinfo), jamais les colonnes
    volumes.volume_number/is_special/etc. elles-mêmes utilisées par le frontend pour le
    regroupement Tomes/Spéciaux - un tome déjà mal classifié localement (ex: matché avant
    correction contre la mauvaise série, voir CLAUDE.md "L'autre") restait donc affiché
    "Spécial" indéfiniment même après une MAJ métadonnées réussie, sans que rien d'autre
    qu'une correction manuelle (renumber_volume) ne puisse le réparer.

    Retourne (volume_number, is_integral, integral_number, is_hs, hs_number, is_episode,
    episode_number, is_special, special_label)."""
    if is_oneshot_series:
        number = None
    is_integral = is_hs = is_episode = is_special = False
    integral_number = hs_number = episode_number = special_label = None
    title = (title or '').strip()

    if number is None:
        is_integral, integral_number, is_hs, hs_number = _parse_int_hs_prefix(title)

    if not is_integral and not is_hs:
        m_ep = re.search(r'\b[EÉ]p(?:isode)?\.?\s*(\d+)\b', title, re.IGNORECASE)
        if m_ep:
            is_episode = True
            episode_number = int(m_ep.group(1))

    if number is None and not is_integral and not is_hs and not is_episode and not is_oneshot_series:
        is_special = True
        _, special_label = _parse_special_prefix(title)

    volume_number = None if (is_integral or is_hs or is_episode or is_special) else number
    return volume_number, is_integral, integral_number, is_hs, hs_number, is_episode, episode_number, is_special, special_label


def _bedetheque_title_to_folder_name(title):
    """Titre Bédéthèque -> nom de dossier de série valide: uniquement les caractères
    interdits dans un nom de fichier sont remplacés, le reste du titre (y compris
    l'ordre des mots/articles) est conservé tel quel - c'est la valeur exacte de
    Bédéthèque qui fait référence pour le titre/dossier/ComicInfo, sans reformatage.
    Même logique que côté UI d'import (bedethequeTitleToSeriesName dans import.js)."""
    import re as _re
    name = _re.sub(r'[\\/:*?"<>|]', ' ', title)
    name = _re.sub(r'\s+', ' ', name).strip()
    return name


def _sync_bedetheque_placeholder_volumes(series_id, info, scraper):
    """Crée une ligne `volumes` "placeholder" (sans fichier - filepath/filename/format
    NULL, "comme s'il existait") pour chaque album de la fiche Bédéthèque qui n'est pas
    déjà représenté en base pour cette série (ni possédé, ni déjà un placeholder d'un
    appel précédent) - idempotent, appelable à répétition sans jamais créer de doublon.

    Utilisée à l'ajout d'une série vide (add_series_from_bedetheque) ET à chaque MAJ
    métadonnées/matching (_align_title_and_start_metadata_write, partagée par le bouton
    "MAJ métadonnées", le matching manuel et les enrichissements batch/bibliothèque):
    une série déjà en bibliothèque dont Bédéthèque publie un nouveau tome, ou qui a été
    ajoutée avant l'existence des tomes placeholder, récupère ainsi les tomes manquants
    (numérotés, intégrales, hors-séries) sans repasser par un ajout complet.

    Retourne le nombre de lignes créées."""
    conn = get_db_connection()
    cursor = conn.cursor()
    # BEGIN IMMEDIATE prend le verrou d'écriture dès l'ouverture de la transaction,
    # avant même le SELECT ci-dessous: sans ça, deux appels concurrents (ex: l'utilisateur
    # clique "MAJ métadonnées" pendant qu'un enrichissement par lot tourne) peuvent chacun
    # lire "rien en base pour ce numéro" avant que l'autre n'ait committé son propre
    # INSERT, et créer un doublon du même tome placeholder - constaté sur plusieurs
    # dizaines de séries après un enrichissement de bibliothèque en tâche de fond pendant
    # que d'autres MAJ métadonnées individuelles tournaient en parallèle depuis l'UI.
    cursor.execute('BEGIN IMMEDIATE')

    # "but you should look at bedetheque and see that it is a one-shot so no need to
    # match a volume number" - un one-shot n'a par définition aucune notion de "tome N",
    # quoi que la fiche Bédéthèque elle-même puisse renseigner dans son champ `number`
    # (fiable ou non, voir aussi le bug de parsing corrigé dans _parse_volume,
    # scraper.py). Lu une seule fois ici plutôt que de faire dépendre la signature de
    # cette fonction d'un paramètre supplémentaire chez tous ses appelants.
    is_oneshot_series = bool(cursor.execute('SELECT is_oneshot FROM series WHERE id = ?', (series_id,)).fetchone()[0])

    # "Après l'orage (Cremers)" (voir CLAUDE.md): un one-shot n'a par définition qu'UNE
    # seule oeuvre - tout autre album non numéroté que Bédéthèque liste sur la même page
    # (tirage/édition alternative) n'est jamais un second contenu à acquérir dès qu'on
    # possède déjà le fichier réel. Le dédoublonnage par titre (existing_all_titles plus
    # bas) suffisait pour Le Gaulois/Lucky Luke/Nordheim (albums numérotés, titre Bédéthèque
    # identique au titre local) mais pas ici: le titre du fichier réel d'un one-shot est
    # souvent celui de la SÉRIE (comicinfo['series'], pas 'title', voir _row_title), qui
    # porte parfois un suffixe de désambiguïsation purement local ("(Cremers)", ajouté
    # pour distinguer deux séries locales de même titre) absent du titre brut Bédéthèque
    # de l'édition alternative ("Après l'orage" sans suffixe) - la comparaison de titre
    # échoue alors silencieusement. Pour un one-shot déjà possédé, plus besoin de
    # comparer les titres du tout: AUCUN spécial supplémentaire n'a de raison d'exister.
    has_owned_real_volume = bool(cursor.execute(
        'SELECT 1 FROM volumes WHERE series_id = ? AND filepath IS NOT NULL LIMIT 1', (series_id,)
    ).fetchone())

    # Identité de chaque tome déjà en base (possédé ou placeholder), pour ne pas
    # dupliquer un album déjà représenté. Les albums non numérotés et non identifiables
    # comme intégrale/hors-série (voir plus bas) sont dédupliqués par leur URL Bédéthèque
    # (comicinfo.web) faute de tout autre identifiant stable pour eux.
    cursor.execute('''
        SELECT volume_number, is_integral, integral_number, is_hs, hs_number, is_episode, episode_number,
               is_special, special_label, comicinfo, filepath, is_bis, bis_suffix
        FROM volumes WHERE series_id = ?
    ''', (series_id,))
    existing_numbers = set()
    existing_bis_identities = set()
    existing_integral_numbers = set()
    existing_hs_numbers = set()
    # Dédupliqué indépendamment de existing_numbers: Bédéthèque numérote ses épisodes
    # dans le même champ `number` que ses tomes (voir plus bas), un tome et un épisode
    # peuvent donc légitimement partager le même numéro sans être le même album.
    existing_episode_numbers = set()
    # "check nordheim... some are duplicates" (suite): le matching d'un fichier RÉELLEMENT
    # possédé (match_bedetheque_volume, "MAJ métadonnées") peut lui aussi écrire l'URL
    # générique de la série en <Web> quand il n'est pas sûr à 100% de l'album précis
    # (constaté sur L'Épervier: un fichier matché "INT01TL . 1+2" mais avec web = URL de
    # la série, pas celle de l'album) - même repli par titre qu'en dessous pour
    # existing_unclassified_titles, appliqué ici aux intégrales/hors-séries.
    existing_integral_titles = set()
    existing_hs_titles = set()
    existing_unclassified_titles = set()
    # Tome réel (filepath non NULL) SANS URL Bédéthèque encore enregistrée (ComicInfo pas
    # encore écrit) - la seule situation où l'URL ne suffit pas à reconnaître "cet album
    # est déjà représenté", faute d'avoir quoi que ce soit à comparer. Repli sur le numéro
    # local (parsé depuis le nom de fichier, voir LibraryScanner.parse_filename) pour
    # cette unique situation - jamais pour comparer deux albums Bédéthèque entre eux.
    # unclaimed_unclassified_real_volumes (compteur, pas un numéro): "un seul album sur
    # la page" pour un one-shot sans numéro du tout ("pourquoi les metadata... c'est pas
    # chargé" - un tome placeholder EN DOUBLE de ce fichier bien réel se créait sinon à la
    # toute première MAJ métadonnées d'une série neuve).
    def _row_web_url(row):
        try:
            ci = json.loads(row['comicinfo']) if row['comicinfo'] else {}
        except (TypeError, ValueError):
            ci = {}
        return ci.get('web')

    def _row_title(row):
        try:
            ci = json.loads(row['comicinfo']) if row['comicinfo'] else {}
        except (TypeError, ValueError):
            ci = {}
        return ci.get('title')

    # "check nordheim... some are duplicates": une ligne réelle importée AVANT l'existence
    # d'une classification (ex: is_episode, ajoutée à ce parser le 19/07 - un fichier
    # "Épisode N" scanné avant cette date est resté classé volume_number=N/is_episode=0)
    # ne matche alors plus jamais la MÊME entrée Bédéthèque, reclassée différemment par un
    # scrape plus récent - un nouveau placeholder se recréait à côté à chaque
    # resynchronisation, quelle que soit sa classification à elle. L'URL Bédéthèque d'une
    # ligne, elle, ne dépend d'aucun schéma de classification et reste le même identifiant
    # quoi qu'il arrive - vérifiée en premier, tous types confondus, avant tout repli
    # spécifique à un type.
    existing_all_urls = set()
    unclaimed_unclassified_real_volumes = 0
    unclaimed_integral_real_numbers = set()
    unclaimed_hs_real_numbers = set()
    # Spéciaux (voir _parse_special_prefix) - pas de numéro structuré comme INT/HS, donc
    # dédupliqués par TITRE uniquement, même mécanique que le repli "unclassified" déjà
    # existant (existing_unclassified_titles) mais tenue à part: un spécial déjà classifié
    # ne doit jamais se faire absorber par le compteur générique unclaimed_unclassified_
    # real_volumes (qui suppose un tome sans AUCUNE classification, un cas différent).
    existing_special_titles = set()
    unclaimed_special_titles = set()
    # "Aldobrando"/"Le Gaulois" (voir CLAUDE.md): Bédéthèque catalogue parfois un TIRAGE ou
    # une ÉDITION alternative d'un album déjà possédé (luxe "TL", réédition, coffret...)
    # comme sa PROPRE page d'album, sans aucun 'number' - exactement la même mécanique
    # qu'un vrai spécial (COF/Pub/...) aux yeux de ce parseur, mais ce n'est pas un
    # contenu à acquérir en plus, juste un autre tirage de ce qu'on a déjà. Titre exact
    # (ou titre après un éventuel préfixe "CODE . ", voir plus bas) comparé contre TOUS
    # les titres déjà connus dans la série, tomes numérotés compris - pas seulement
    # existing_special_titles/existing_unclassified_titles comme avant, qui ne
    # couvraient pas le cas d'une réédition d'un TOME NUMÉROTÉ (Le Gaulois Tome 4: la
    # version normale est en base sous volume_number=4, jamais comparée jusqu'ici contre
    # le titre d'un spécial candidat).
    existing_all_titles = set()
    for row in cursor.fetchall():
        _url = _row_web_url(row)
        if _url:
            existing_all_urls.add(_url)
        _t_any = _row_title(row)
        if _t_any:
            existing_all_titles.add(_t_any)
        if row['is_bis']:
            existing_bis_identities.add((row['volume_number'], row['bis_suffix']))
        elif row['volume_number'] is not None:
            existing_numbers.add(row['volume_number'])
        elif row['is_integral']:
            existing_integral_numbers.add(row['integral_number'])
            if not _url and row['filepath']:
                unclaimed_integral_real_numbers.add(row['integral_number'])
            _t = _row_title(row)
            if _t:
                existing_integral_titles.add(_t)
        elif row['is_hs']:
            existing_hs_numbers.add(row['hs_number'])
            if not _url and row['filepath']:
                unclaimed_hs_real_numbers.add(row['hs_number'])
            _t = _row_title(row)
            if _t:
                existing_hs_titles.add(_t)
        elif row['is_episode']:
            existing_episode_numbers.add(row['episode_number'])
        elif row['is_special']:
            _t = _row_title(row)
            if _t:
                existing_special_titles.add(_t)
                if not _url and row['filepath']:
                    unclaimed_special_titles.add(_t)
        else:
            if not _url and row['filepath']:
                unclaimed_unclassified_real_volumes += 1
            _t = _row_title(row)
            if _t:
                existing_unclassified_titles.add(_t)

    covers_dir = './data/covers'
    created = 0

    bd_volumes_for_order = info.get('volumes') or []
    # Quand Bédéthèque ne fournit aucun numéro structuré mais liste plusieurs albums,
    # l'ordre de la fiche devient leur numérotation stable (1..N). On ne l'applique pas
    # aux listes composées d'intégrales/HS identifiables par leur préfixe.
    all_unstructured = len(bd_volumes_for_order) > 1 and all(
        v.get('number') is None and not _parse_int_hs_prefix((v.get('title') or '').strip())[0]
        and not _parse_int_hs_prefix((v.get('title') or '').strip())[2]
        for v in bd_volumes_for_order
    )
    for ordinal, bd_vol in enumerate(bd_volumes_for_order, 1):
        number = ordinal if all_unstructured else bd_vol.get('number')
        title = (bd_vol.get('title') or '').strip()
        # Classification (is_integral/is_hs/is_episode/is_special) factorisée dans
        # _classify_bedetheque_number - réutilisée par update_metadata_volume/
        # _write_series_volumes_metadata_async, voir son docstring.
        (volume_number, is_integral, integral_number, is_hs, hs_number,
         is_episode, episode_number, is_special, special_label) = _classify_bedetheque_number(number, title, is_oneshot_series)
        if is_oneshot_series:
            # Jamais un numéro de tome pour un one-shot - la dédup plus bas doit voir
            # `number` lui aussi à None (pas seulement volume_number), voir
            # _classify_bedetheque_number qui applique la même règle en interne.
            number = None

        bis_suffix = (bd_vol.get('bis_suffix') or '').strip()
        is_bis = bool(bis_suffix) and number is not None \
            and not is_integral and not is_hs and not is_episode and not is_special

        if bd_vol.get('url') and bd_vol['url'] in existing_all_urls:
            continue
        if is_episode:
            if episode_number in existing_episode_numbers:
                continue
        elif is_integral:
            if integral_number is not None and integral_number in unclaimed_integral_real_numbers:
                unclaimed_integral_real_numbers.discard(integral_number)
                continue
            if title and title in existing_integral_titles:
                continue
        elif is_hs:
            if hs_number is not None and hs_number in unclaimed_hs_real_numbers:
                unclaimed_hs_real_numbers.discard(hs_number)
                continue
            if title and title in existing_hs_titles:
                continue
        elif is_bis:
            # Identité propre (voir existing_bis_identities plus haut) - jamais croisée
            # contre existing_numbers: le vrai tome N déjà possédé/placeholder ne doit
            # jamais empêcher la création de "N Bis", ni l'inverse.
            if (number, bis_suffix) in existing_bis_identities:
                continue
        elif number is not None:
            # "check serie 464... why there are not all the albums compared to bedetheque"
            # (I.R.$.) - ce croisement doit rester dans l'UN SEUL sens documenté plus haut
            # (un album Bédéthèque numéroté contre un tome déjà possédé LOCALEMENT comme
            # intégrale/HS, voir unclaimed_integral_real_numbers/unclaimed_hs_real_numbers)
            # mais comparait à tort contre existing_integral_numbers/existing_hs_numbers en
            # ENTIER - qui contiennent aussi les placeholders DÉJÀ CRÉÉS depuis Bédéthèque
            # lui-même (ex: "INT1 . Les nazis et l'or juif", classifié via le préfixe INT du
            # titre, voir plus haut) plutôt que d'un fichier réellement possédé. Pour I.R.$.,
            # les Tomes 1 à 7 partageaient un numéro avec INT1 à INT7 (des albums Bédéthèque
            # totalement différents, même pas le même titre) et se voyaient donc jamais créés
            # comme placeholder. Restreint aux seuls numéros "réclamables" par un fichier
            # local pas encore matché - les placeholders déjà issus de Bédéthèque, eux, ne
            # doivent jamais bloquer la création d'un album numéroté par pure coïncidence de
            # numéro.
            if number in existing_numbers or number in unclaimed_integral_real_numbers or number in unclaimed_hs_real_numbers:
                continue
        elif is_special:
            # Voir has_owned_real_volume plus haut: un one-shot déjà possédé n'a jamais
            # besoin d'un spécial de plus, quel que soit son titre - vérifié en premier,
            # avant toute comparaison de titre (qui peut échouer pour la raison
            # expliquée là-bas).
            if is_oneshot_series and has_owned_real_volume:
                continue
            # Pas de numéro structuré pour un spécial (voir _parse_special_prefix) -
            # dédupliqué par titre uniquement, même logique que existing_integral_titles/
            # existing_hs_titles ci-dessus mais tenue dans son propre set (existing_special_
            # titles) pour ne jamais se confondre avec le repli générique "unclassified"
            # plus bas (un tome sans AUCUNE classification, cas différent).
            if title and title in unclaimed_special_titles:
                unclaimed_special_titles.discard(title)
                continue
            if title and title in existing_special_titles:
                continue
            # Voir existing_all_titles plus haut: un "spécial" qui n'est en réalité qu'un
            # tirage/une édition alternative d'un album déjà représenté (numéroté,
            # intégrale, HS, one-shot non classifié...) sous le MÊME titre - comparé
            # titre exact d'abord, puis titre débarrassé de son préfixe "CODE . " (format
            # documenté dans _parse_special_prefix) au cas où seule l'édition alternative
            # porte ce préfixe alors que l'album déjà possédé, lui, ne le porte pas
            # (ex: tome normal "Le Gaulois le Gaulois" déjà possédé, édition de luxe listée
            # par Bédéthèque sous "TL . Le Gaulois le Gaulois").
            if title and title in existing_all_titles:
                continue
            if special_label and ' . ' in title:
                remainder = title.split(' . ', 1)[1].strip()
                if remainder and remainder in existing_all_titles:
                    continue
        elif title and title in existing_unclassified_titles:
            continue
        elif unclaimed_unclassified_real_volumes > 0:
            # Un fichier réel non classifié attend déjà d'être rattaché à un album - cet
            # album Bédéthèque correspond à CE fichier (voir le commentaire sur
            # unclaimed_unclassified_real_volumes plus haut), pas la peine d'en créer un
            # placeholder en double.
            unclaimed_unclassified_real_volumes -= 1
            continue

        comicinfo = {}
        if bd_vol.get('title'):
            comicinfo['title'] = bd_vol['title']
        # Résumé propre à l'album si déjà en cache (voir _start_metadata_write_thread,
        # qui repasse derrière ce placeholder dès que le fetch par album termine), sinon
        # repli sur le résumé de la série - même logique que build_comicinfo_fields,
        # jamais réutilisée ici jusqu'à présent (ce dict est construit à la main, pas via
        # build_comicinfo_fields, faute de tome local à passer en `local_volume_number`)
        summary = bd_vol.get('description') or info.get('description')
        if summary:
            comicinfo['summary'] = summary
        writer = bd_vol.get('scenario') or ', '.join(info.get('scenaristes') or []) or None
        if writer:
            comicinfo['writer'] = writer
        penciller = bd_vol.get('dessin') or ', '.join(info.get('dessinateurs') or []) or None
        if penciller:
            comicinfo['penciller'] = penciller
        publisher = bd_vol.get('editeur') or ', '.join(info.get('editeurs') or []) or None
        if publisher:
            comicinfo['publisher'] = publisher
        if info.get('genre'):
            comicinfo['genre'] = info['genre']
        web = bd_vol.get('url') or info.get('url')
        if web:
            comicinfo['web'] = web
        date_publication = bd_vol.get('date_publication')
        if date_publication:
            year_part = date_publication.split('-')[0]
            if year_part.isdigit():
                comicinfo['year'] = year_part

        cover_path = None
        if bd_vol.get('cover_url'):
            try:
                cover_path = scraper._download_cover(bd_vol['cover_url'], covers_dir)
            except Exception as e:
                logger.warning(f"Échec téléchargement couverture de l'album '{bd_vol.get('title')}' de la série #{series_id}: {e}")

        # volume_number NULL pour une intégrale/hors-série/épisode (comme pour un tome réel
        # dont le numéro n'est pas encore connu): get_series_details/buildVolumeItemHtml les
        # trient déjà après les tomes numérotés (ORDER BY volume_number IS NULL), elles
        # apparaissent donc à la fin de la liste sans changement côté affichage. Un épisode
        # ne garde jamais le `number` brut de Bédéthèque en volume_number: il coïnciderait
        # avec le numéro d'un vrai tome (Tome 1/Épisode 1 partagent number=1, voir plus haut).
        # volume_number (calculé par _classify_bedetheque_number ci-dessus) applique déjà
        # exactement cette règle.
        cursor.execute('''
            INSERT INTO volumes (series_id, volume_number, filepath, filename, format, comicinfo, cover_path,
                                  is_integral, integral_number, is_hs, hs_number, is_episode, episode_number,
                                  is_special, special_label, is_bis, bis_suffix)
            VALUES (?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (series_id, volume_number, json.dumps(comicinfo) if comicinfo else None, cover_path,
              int(is_integral), integral_number, int(is_hs), hs_number, int(is_episode), episode_number,
              int(is_special), special_label, int(is_bis), bis_suffix if is_bis else None))
        created += 1

        # "serie 775 ca a creer d'autres tomes duplique" / "i did not use fusionner pour
        # cette serie" - existing_numbers/existing_integral_numbers/existing_hs_numbers ne
        # sont construits qu'UNE FOIS avant cette boucle, jamais mis à jour au fil des
        # insertions qu'elle fait elle-même: si la fiche Bédéthèque liste deux albums avec
        # le même numéro (ou plusieurs hors-séries/intégrales sans le moindre numéro,
        # is_integral/is_hs=True avec number=None, cas constaté sur plusieurs séries de
        # cette bibliothèque), le second n'était jamais reconnu comme "déjà créé à
        # l'instant par cette même boucle" et repartait sur un nouveau placeholder en
        # double - sans le moindre rapport avec un import ou une fusion de séries.
        # is_episode vérifié avant `number is not None` pour la même raison que dans le
        # bloc de dédup ci-dessus (number est non-None aussi pour un épisode).
        if bd_vol.get('url'):
            existing_all_urls.add(bd_vol['url'])
        if is_episode:
            existing_episode_numbers.add(episode_number)
        elif is_integral:
            existing_integral_numbers.add(integral_number)
            if title:
                existing_integral_titles.add(title)
        elif is_hs:
            existing_hs_numbers.add(hs_number)
            if title:
                existing_hs_titles.add(title)
        elif is_bis:
            existing_bis_identities.add((number, bis_suffix))
        elif number is not None:
            existing_numbers.add(number)
        elif title:
            existing_unclassified_titles.add(title)

    conn.commit()
    conn.close()

    if created:
        logger.info(f"✓ {created} nouveau(x) tome(s) placeholder créé(s) pour la série #{series_id}")

    return created


@bedetheque_bp.route('/add-series', methods=['POST'])
def add_series_from_bedetheque():
    """Ajoute une série à une bibliothèque directement depuis sa fiche Bedetheque, SANS
    créer son dossier physique (créé plus tard, au premier téléchargement/import réel -
    scan_library ne supprime plus les séries sans dossier, voir son commentaire) -
    seulement sa ligne en base avec toutes les métadonnées du site
    (couverture, résumé, auteurs, statut, nombre de tomes...), et renseigne
    missing_volumes avec tous les numéros d'albums de la fiche - la page de la série
    liste ainsi immédiatement les tomes à récupérer (recherche EBDZ/Prowlarr).
    Corps JSON: {url: fiche série bedetheque.com, library_id, from_discover, skip_auto_acquire}.
    Si une série du même nom existe déjà dans la bibliothèque, renvoie son id
    (already_exists: true) sans rien créer.

    from_discover (optionnel, bool): "auto search and download is not active only in
    the découvrir as there is a recherche et recherche auto option. auto download button
    should be there and if active auto search when adding from everything except
    découvrir" - Découvrir a déjà sa propre étape dédiée "Chercher les sources"
    (discover.js -> POST /auto-acquire/run), qui ne doit jamais se déclencher deux fois
    (une fois ici, une fois via ce bouton dédié) - seul discover.js envoie ce flag.
    Tout autre appelant (fiche série, Nouveautés EBDZ/Telegram, thèmes/indispensables/
    top auteurs...) respecte au contraire le réglage global "Téléchargement automatique
    à l'ajout" (auto_acquire_on_add_enabled, Configuration > Recherche) - voir plus bas.

    skip_auto_acquire (optionnel, bool): "tu telecharge le fichier, tu ajoutes la série,
    tu ne fait pas de recherche auto puisque le fichier a deja ete telecharge" -
    ebdz-latest.js télécharge déjà le fichier concerné AVANT d'appeler cette route,
    passe ce flag pour ne pas relancer une recherche+téléchargement redondante pour
    toute la série (constaté: "Le Marche-Lune" téléchargé deux fois, chaque tentative
    épuisant un peu plus le flood-wait Telegram de ce fichier)."""
    from blueprints.library.routes import resolve_within, UnsafePathError, _import_execution_lock, sanitize_path_component

    data = request.get_json() or {}
    url = (data.get('url') or '').strip()
    library_id = data.get('library_id')
    from_discover = bool(data.get('from_discover'))
    skip_auto_acquire = bool(data.get('skip_auto_acquire'))

    if not url or not library_id:
        return jsonify({'success': False, 'error': 'url et library_id requis'}), 400
    if not url.startswith('https://www.bedetheque.com/'):
        return jsonify({'success': False, 'error': 'URL bedetheque.com requise'}), 400

    lock_acquired = False
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT id, path FROM libraries WHERE id = ?', (library_id,))
        library = cursor.fetchone()
        if not library:
            conn.close()
            return jsonify({'success': False, 'error': 'Bibliothèque introuvable'}), 404

        scraper = BedethequeScraper()
        info = scraper.get_series_info(url)
        if not info or not info.get('title'):
            conn.close()
            return jsonify({'success': False, 'error': 'Fiche Bedetheque introuvable ou illisible'}), 404

        series_title = _bedetheque_title_to_folder_name(info['title'])

        # "pourquoi il a créé 2 blanc autour": la vérification "existe déjà" ci-dessous
        # ET l'INSERT juste en bas doivent former une section critique unique avec
        # execute_import/execute_auto_import (mêmes deux opérations pour une série
        # auto-créée pendant l'import - voir _import_execution_lock côté library/routes.py)
        # - sinon un import automatique en cours et cet ajout manuel depuis Bédéthèque
        # peuvent chacun trouver "pas encore de série avec ce titre" au même instant et
        # créer chacun la leur (constaté: deux séries "Blanc autour" identiques,
        # bedetheque_url identique, l'une avec un vrai fichier, l'autre un placeholder
        # vide). Acquis seulement à partir d'ici (pas pendant le scraping Bédéthèque,
        # potentiellement long avec son délai anti-bot) pour ne pas bloquer l'import
        # automatique plus que nécessaire.
        if not _import_execution_lock.acquire(timeout=30):
            conn.close()
            return jsonify({'success': False, 'error': 'Import en cours, réessayez dans un instant'}), 409
        lock_acquired = True

        # Série déjà présente sous ce nom: on renvoie simplement sa fiche
        cursor.execute('SELECT id FROM series WHERE library_id = ? AND title = ?', (library_id, series_title))
        existing = cursor.fetchone()
        if existing:
            conn.close()
            return jsonify({'success': True, 'series_id': existing['id'], 'already_exists': True})

        related_urls = [url] + [r['url'] for r in (info.get('related_series') or []) if r.get('url')]
        universe_name = None
        if related_urls:
            placeholders = ','.join('?' * len(related_urls))
            cursor.execute(
                f'SELECT u.name FROM universe_series us JOIN universes u ON u.id = us.universe_id '
                f'WHERE us.bedetheque_url IN ({placeholders}) LIMIT 1',
                related_urls
            )
            existing_universe = cursor.fetchone()
            if existing_universe:
                universe_name = existing_universe[0]

        from blueprints.settings.rename_config_store import load_rename_config
        from rename_handler import render_series_folder_name
        rename_cfg = load_rename_config()
        folder_name_rendered = render_series_folder_name(series_title, rename_cfg['series_template'], universe_name)

        try:
            folder_segments = [sanitize_path_component(part, 'Titre de série') for part in folder_name_rendered.split('/') if part]
            if not folder_segments:
                raise UnsafePathError(f"Titre de série invalide: {folder_name_rendered!r}")
            series_path = resolve_within(os.path.join(library['path'], *folder_segments), library['path'])
        except UnsafePathError as e:
            conn.close()
            return jsonify({'success': False, 'error': f'Nom de série invalide: {e}'}), 400

        # Tous les albums numérotés de la fiche = tomes manquants (aucun fichier encore):
        # la fiche série affiche ainsi directement la liste des tomes à récupérer.
        bd_volumes = info.get('volumes') or []
        missing = sorted({v['number'] for v in bd_volumes if v.get('number') is not None})
        if not missing and bd_volumes:
            # Certaines séries d'albums (ex. « Les grands Peintres ») affichent une
            # date/collection dans le titre (« 2015/02 . Goya ») au lieu d'un numéro
            # Bédéthèque exploitable. Sans repli, missing restait vide et le réglage
            # « téléchargement automatique à l'ajout » ne lançait jamais la recherche.
            # Pour une liste multi-albums entièrement non numérotée, l'ordre de la fiche
            # est la seule séquence fiable disponible : on l'utilise pour rechercher 1..N.
            is_one_shot = (info.get('status') or '').strip().lower() == 'one shot'
            missing = [None] if is_one_shot or len(bd_volumes) == 1 else list(range(1, len(bd_volumes) + 1))

        cursor.execute('''
            INSERT INTO series (library_id, title, path, total_volumes, missing_volumes, has_parts)
            VALUES (?, ?, ?, 0, ?, 0)
        ''', (library_id, series_title, series_path, json.dumps(missing)))
        series_id = cursor.lastrowid

        # "2020 • pages null • N/A" (série #889 "La fuite du cerveau" et 4 autres one-shots
        # ajoutés depuis Bédéthèque) - series.is_oneshot n'était sinon posé que bien plus
        # tard, par un scan/update_series_stats (scanner.py, à partir de
        # bedetheque_status=='One shot'). Entre-temps, _sync_bedetheque_placeholder_volumes
        # ci-dessous tournait avec is_oneshot_series=False (valeur par défaut de la colonne
        # à l'INSERT) et classifiait donc à tort l'unique album du one-shot is_special=1 au
        # lieu du placeholder "plain one-shot" attendu (voir son commentaire "not
        # is_oneshot_series"). Le vrai fichier, importé plus tard, ne retrouvait alors
        # jamais ce placeholder (is_special=0 exigé par _find_existing_volume_for_import)
        # et créait sa PROPRE ligne à côté - un one-shot possédé se retrouvait avec 2
        # lignes volumes, et la fiche série pouvait afficher les stats (pages/taille) du
        # placeholder vide (data.volumes[0]) au lieu du fichier réel. Posé ici, AVANT
        # _sync_bedetheque_placeholder_volumes, à partir du même signal Bédéthèque
        # ('Parution' == 'One shot') que update_series_stats utilise.
        # Insensible à la casse (voir la même précaution côté scanner.py,
        # update_series_stats - "one-shot is in bedetheque written as One Shot").
        if (info.get('status') or '').strip().lower() == 'one shot':
            cursor.execute('UPDATE series SET is_oneshot = 1 WHERE id = ?', (series_id,))

        conn.commit()
        conn.close()

        # Section critique terminée (la ligne série est commitée, un check concurrent la
        # trouvera désormais) - relâcher tout de suite plutôt que de garder le verrou
        # pendant le reste de la fonction (écriture Bédéthèque, sync EBDZ...), qui peut
        # faire des appels réseau et bloquerait inutilement l'import automatique.
        _import_execution_lock.release()
        lock_acquired = False

        try:
            from blueprints.library.action_history import log_action
            log_action('add_series', series_id, series_title,
                       f"Série ajoutée depuis Bédéthèque ({len(missing)} tome(s) à récupérer)")
        except Exception as e:
            logger.warning(f"Échec journalisation ajout série #{series_id}: {e}")

        db_manager = BedethequeDatabase(current_app.config['DATABASE'])
        db_manager.update_series_bedetheque_info(series_id, info)

        # "will you scrape pictures when adding a new serie?" / "add it" / "but only
        # when adding a new serie" - déclenche le seul scraping de photos d'auteurs de
        # toute l'app, ici et nulle part ailleurs (voir _start_author_photos_fetch_thread).
        _start_author_photos_fetch_thread(current_app._get_current_object(), info.get('author_links'))

        _sync_bedetheque_placeholder_volumes(series_id, info, scraper)

        # Récupère en tâche de fond le résumé de CHAQUE album (une requête par album,
        # trop lent pour cette requête HTTP - voir _write_series_volumes_metadata_async)
        # et le met en cache dans series.bedetheque_albums + le ComicInfo des tomes
        # placeholder créés ci-dessus: toutes les métadonnées de la fiche sont ainsi déjà
        # en base avant même le premier import réel dans cette série, au lieu de ne
        # jamais l'être (voir "Aucun appel réseau" dans execute_import).
        prefetch_conn = get_db_connection()
        total_volumes = prefetch_conn.execute(
            'SELECT COUNT(*) AS n FROM volumes WHERE series_id = ?', (series_id,)
        ).fetchone()['n']
        prefetch_conn.close()
        _start_metadata_write_thread(series_id, series_title, info, total_volumes)

        # Flux "façon Sonarr": une série ajoutée vide est surveillée d'office (voir
        # plan-sonarr-monitoring.md) - auto_download_enabled reste à False par défaut,
        # le téléchargement est toujours une action manuelle depuis la fiche série
        try:
            from blueprints.missing_monitor.detector import MissingVolumeDetector
            MissingVolumeDetector(current_app.config['DATABASE']).create_monitor_entry(series_id)
        except Exception as e:
            logger.warning(f"Échec activation surveillance pour la série #{series_id}: {e}")

        # Tentative de matching EBDZ automatique, best-effort: ne doit jamais faire
        # échouer l'ajout. Réutilise le même helper que le matching auto post-scan
        # (import local pour éviter un cycle bedetheque routes -> library routes)
        try:
            from blueprints.library.routes import _ebdz_enrich_series
            _ebdz_enrich_series(series_id)
        except Exception as e:
            logger.info(f"Matching EBDZ auto non concluant pour la série #{series_id}: {e}")

        # "auto search and download is not active only in the découvrir as there is a
        # recherche et recherche auto option. auto download button should be there and
        # if active auto search when adding from everything except découvrir" -
        # Découvrir (from_discover) garde sa propre étape dédiée "Chercher les sources"
        # (discover.js -> POST /auto-acquire/run juste après l'ajout), qui ne doit
        # jamais se déclencher EN PLUS d'ici - deux recherches simultanées pour la même
        # série tout juste créée. ebdz-latest.js (skip_auto_acquire) a déjà téléchargé
        # LE fichier concerné avant d'appeler cette route, une recherche ici retélécharge-
        # rait le même à l'identique (voir sa docstring plus haut, incident réel "Le
        # Marche-Lune" téléchargé deux fois). Tout autre appelant (fiche série,
        # thèmes/indispensables/top auteurs, Nouveautés sans passer par le flux
        # ebdz-latest.js dédié...) respecte le réglage global "Téléchargement
        # automatique à l'ajout" (gate_on_global_setting=True côté
        # run_auto_acquire_for_series, relu à chaque tome - pas de recherche lancée du
        # tout si le réglage est déjà désactivé à cet instant précis).
        auto_acquire_started = False
        if not from_discover and not skip_auto_acquire:
            from blueprints.bedetheque.auto_acquire import run_auto_acquire_for_series
            threading.Thread(
                target=run_auto_acquire_for_series,
                args=(current_app._get_current_object(), series_id, series_title, missing),
                kwargs={'gate_on_global_setting': True},
                daemon=True
            ).start()
            auto_acquire_started = True

        logger.info(f"✓ Série ajoutée depuis Bedetheque: {series_title} (#{series_id}, {len(missing)} tomes à récupérer)")
        return jsonify({'success': True, 'series_id': series_id, 'title': series_title,
                        'missing_volumes': missing, 'auto_acquire_started': auto_acquire_started})

    except Exception as e:
        logger.error(f"Erreur lors de l'ajout de série depuis Bedetheque: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if lock_acquired:
            _import_execution_lock.release()


@bedetheque_bp.route('/auto-acquire/status/<int:series_id>', methods=['GET'])
def auto_acquire_status(series_id):
    from blueprints.bedetheque.auto_acquire import get_auto_acquire_status
    return jsonify(get_auto_acquire_status(series_id))


@bedetheque_bp.route('/auto-acquire/run', methods=['POST'])
def run_auto_acquire_now():
    """Lance l'acquisition automatique (voir auto_acquire.py) pour une série DÉJÀ créée,
    à la demande explicite de l'utilisateur - "dans découvrir met une option pour
    recherche automatique dans étape 2 et donc ne pas faire de recherche manuelle" (étape
    "Chercher les sources" de /discover, voir static/js/discover.js). Indépendant du
    réglage global "Téléchargement automatique à l'ajout" (Configuration > Recherche) :
    un choix ponctuel pour CETTE série, que le réglage global soit activé ou non - si le
    réglage global l'avait déjà déclenché à la création de la série, ce nouvel appel ne
    fait qu'une recherche redondante (jamais un doublon réel: _find_existing_volume_for_import
    empêche déjà tout tome en double, voir library/routes.py).

    volume_number (optionnel): cible UN seul tome (molette d'un volume précis, manquant
    ou pour un remplacement - voir runAutoAcquireNowForVolume, static/js/library.js) au
    lieu de tous les tomes manquants de la série (bouton de la fiche série / étape
    Découvrir, sans ce paramètre).

    is_integral/is_hs/is_episode (optionnels, avec volume_number): "Recherche
    automatique" est explicitement désactivé pour ces types dans l'UI - le bouton n'était
    affiché QUE pour un tome numéroté classique (voir buildVolumeMissingActionsGearHtml/
    buildVolumeActionsGearHtml, library.js), car sans ces flags run_auto_acquire_for_series
    n'avait aucun moyen de dire à _confirms_requested_volume (searcher.py) qu'un numéro
    donné désigne une intégrale/HS/épisode plutôt qu'un tome plain - un résultat pourtant
    correct ("INT2 - ...") se faisait rejeter en amont ("Tome 2 demandé, mais ce titre est
    une intégrale") avant même la comparaison de titre. Construit ici le label
    ("Intégrale N"/"HS N"/"Épisode N") que _confirms_requested_volume sait déjà
    interpréter (voir son paramètre `label`), pour que ce chemin fonctionne enfin.

    oneshot (optionnel, bool): recherche SANS numéro pour un one-shot qui n'en a aucun
    (pas d'intégrale/HS/tome identifiable) - voir runAutoAcquireNowForOneshot,
    static/js/library.js. Distinct de "pas de volume_number fourni" (qui, lui, veut dire
    "tous les tomes manquants de la série"): un one-shot n'a pas de missing_volumes à
    proprement parler, cette recherche vise l'unique édition de la série elle-même.
    _best_confident_result (auto_acquire.py) applique alors une vérification par
    similarité de titre plutôt que par numéro, "tu vérifies si le nom correspond. si tu
    n'es pas sûr tu demandes la validation". Ce flag est un raccourci explicite, pas la
    seule façon d'y arriver: si l'appelant ne le passe pas et que missing_volumes est
    vide, on retombe automatiquement sur ce même mode dès que series.is_oneshot est vrai
    (voir plus bas) - un one-shot fraîchement ajouté a par construction missing_volumes=[]
    (add_series_from_bedetheque n'y met que les albums numérotés), un appelant qui l'ignore
    (ex: runAutoAcquireNow, static/js/discover.js, juste après l'ajout) ne doit pas se
    retrouver à tort sur "Aucun tome manquant"."""
    data = request.get_json() or {}
    series_id = data.get('series_id')
    if not series_id:
        return jsonify({'success': False, 'error': 'series_id requis'}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT title, missing_volumes, is_oneshot FROM series WHERE id = ?', (series_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return jsonify({'success': False, 'error': 'Série introuvable'}), 404

    volume_number = data.get('volume_number')
    type_label = None
    if volume_number is not None:
        if data.get('is_integral'):
            type_label = f"Intégrale {volume_number}"
        elif data.get('is_hs'):
            type_label = f"HS {volume_number}"
        elif data.get('is_episode'):
            type_label = f"Épisode {volume_number}"

    if data.get('oneshot'):
        missing = [None]
    elif volume_number is not None:
        missing = [volume_number]
    else:
        try:
            missing = json.loads(row['missing_volumes']) if row['missing_volumes'] else []
        except (TypeError, ValueError):
            missing = []
        if not missing and row['is_oneshot']:
            missing = [None]

    if not missing:
        return jsonify({'success': True, 'started': False, 'count': 0})

    from blueprints.library.routes import load_library_import_config
    sources = load_library_import_config().get('auto_acquire_sources') or []

    from blueprints.library.action_history import log_action
    vol_label = (
        'One-shot' if (data.get('oneshot') or (volume_number is None and missing == [None]))
        else type_label if type_label
        else f"Tome {volume_number}" if volume_number is not None
        else f"Recherche automatique ({len(missing)} tome(s), {len(sources)} source(s))"
    )
    try:
        log_action('search', series_id, row['title'], vol_label, success=True)
    except Exception:
        pass

    from blueprints.bedetheque.auto_acquire import run_auto_acquire_for_series
    app = current_app._get_current_object()
    threading.Thread(
        target=run_auto_acquire_for_series,
        args=(app, series_id, row['title'], missing),
        kwargs={
            'label': type_label,
            'search_mode': 'volume' if volume_number is not None or data.get('oneshot') else 'series',
        },
        daemon=True
    ).start()

    return jsonify({'success': True, 'started': True, 'count': len(missing), 'sources_count': len(sources)})


def _convert_volume_to_cbz(volume_id, source_format, convert_fn, error_cls):
    """Convertit un tome déjà possédé (`source_format`: cbr/pdf/zip) en cbz - factorisé
    entre convert_cbr/convert_pdf/convert_zip, mêmes routes déclenchées depuis l'icône
    "Convertir en cbz" de la molette d'un tome ("regarde s'il y a une option dans la
    molette des volumes pour convertir vers cbz" - jusqu'ici seul le cbr l'avait, pas le
    pdf/zip nu). Met à jour le filepath/format du volume en base une fois la conversion
    validée (voir cbr_converter/pdf_converter/zip_converter pour la sécurité de
    l'écriture)."""
    from blueprints.library.scanner import LibraryScanner

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT id, series_id, filename, filepath, format FROM volumes WHERE id = ?', (volume_id,))
        vol = cursor.fetchone()
        conn.close()

        if not vol:
            return jsonify({'success': False, 'error': 'Volume non trouvé'}), 404

        if (vol['format'] or '').lower() != source_format:
            return jsonify({'success': False, 'error': f"Ce tome n'est pas au format {source_format}"}), 400

        from blueprints.library.routes import _conversion_lock
        with _conversion_lock:
            new_filepath = convert_fn(vol['filepath'])
        _update_volume_filepath_format(volume_id, new_filepath, 'cbz')

        # DB-first (voir apply_volume_comicinfo/CLAUDE.md): projette ce qui est DÉJÀ en
        # base dans le fichier fraîchement converti, jamais l'inverse. Le fichier
        # d'origine n'était pas réinscriptible (cbr/pdf/zip nu): si une "MAJ métadonnées"
        # avait mis à jour la DB pendant qu'il était encore dans ce format, son
        # ComicInfo.xml embarqué (recopié tel quel par convert_fn quand il en a un) peut
        # être périmé ou vide - le relire écraserait la donnée Bédéthèque déjà en base
        # avec du contenu obsolète.
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT comicinfo FROM volumes WHERE id = ?', (volume_id,))
        existing_row = cursor.fetchone()
        conn.close()

        if existing_row and existing_row['comicinfo']:
            from blueprints.library.scanner import COMICINFO_FIELDS
            merged = json.loads(existing_row['comicinfo'])
            title_case_fields = {
                field: merged[field.lower()]
                for field in COMICINFO_FIELDS if field.lower() in merged
            }
            if title_case_fields:
                apply_volume_comicinfo(
                    current_app.config['DATABASE'], volume_id, new_filepath, 'cbz', title_case_fields
                )

        scanner = LibraryScanner()
        scanner.update_series_stats(vol['series_id'])

        from blueprints.komga.client import trigger_scan_async
        trigger_scan_async()

        from blueprints.library.action_history import log_action
        conn2 = get_db_connection()
        series_row = conn2.execute('SELECT title FROM series WHERE id = ?', (vol['series_id'],)).fetchone()
        conn2.close()
        series_title = series_row['title'] if series_row else f"Série #{vol['series_id']}"
        log_action('convert', vol['series_id'], series_title,
                    f"{vol['filename']} → {os.path.basename(new_filepath)} ({source_format} → cbz)")

        logger.info(f"✓ Volume #{volume_id} converti en cbz: {os.path.basename(new_filepath)}")

        return jsonify({
            'success': True,
            'filepath': new_filepath,
            'filename': os.path.basename(new_filepath),
            'format': 'cbz'
        })

    except error_cls as e:
        logger.error(f"Erreur conversion {source_format}->cbz pour le volume #{volume_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    except Exception as e:
        logger.error(f"Erreur lors de la conversion du volume #{volume_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@bedetheque_bp.route('/convert-cbr/<int:volume_id>', methods=['POST'])
def convert_cbr(volume_id):
    """Convertit un tome cbr en cbz (voir _convert_volume_to_cbz)."""
    return _convert_volume_to_cbz(volume_id, 'cbr', convert_cbr_to_cbz, CbrConversionError)


@bedetheque_bp.route('/convert-pdf/<int:volume_id>', methods=['POST'])
def convert_pdf(volume_id):
    """Convertit un tome pdf en cbz (voir _convert_volume_to_cbz)."""
    from .pdf_converter import convert_pdf_to_cbz, PdfConversionError
    return _convert_volume_to_cbz(volume_id, 'pdf', convert_pdf_to_cbz, PdfConversionError)


@bedetheque_bp.route('/convert-zip/<int:volume_id>', methods=['POST'])
def convert_zip(volume_id):
    """Convertit un tome zip nu en cbz (voir _convert_volume_to_cbz)."""
    from blueprints.library.zip_converter import convert_zip_to_cbz, ZipConversionError
    return _convert_volume_to_cbz(volume_id, 'zip', convert_zip_to_cbz, ZipConversionError)


@bedetheque_bp.route('/convert-volumes-batch', methods=['POST'])
def convert_volumes_batch():
    """Version "en arrière-plan" de convert-cbr/convert-pdf/convert-zip pour plusieurs
    tomes d'un coup (bouton groupé "Convertir en cbz" de la page série,
    bulkConvertSelectedVolumes côté library.js) - "toujours erreur pour conversion [...]
    quand je quitte la page": l'ancienne UI pilotait la séquence de conversions DEPUIS LE
    NAVIGATEUR (_runBulkVolumeAction, library.js - un fetch par tome, le suivant attend
    la réponse du précédent) - fermer l'onglet ou naviguer ailleurs interrompait cette
    boucle JS elle-même, laissant tout tome pas encore lancé jamais converti (le tome EN
    COURS de conversion au moment de la fermeture, lui, continue bien côté serveur - Flask
    threaded=True, voir app.py, ne tue pas le thread de la requête juste parce que le
    client s'est déconnecté; seul l'envoi de la réponse finale échoue alors
    silencieusement). Cette route lance UN SEUL thread serveur qui traite toute la liste
    dans l'ordre (format de chaque tome relu depuis la base, comme le faisait déjà le
    dispatch cbr/pdf/zip côté JS) sans plus jamais dépendre du navigateur - répond
    immédiatement (fire-and-forget), le résultat de chaque conversion n'est visible qu'au
    prochain rechargement de la fiche série."""
    data = request.get_json() or {}
    volume_ids = data.get('volume_ids') or []
    if not volume_ids:
        return jsonify({'error': 'volume_ids requis'}), 400

    app = current_app._get_current_object()

    def _run_batch():
        with app.app_context():
            for vid in volume_ids:
                try:
                    conn = get_db_connection()
                    row = conn.execute('SELECT format FROM volumes WHERE id = ?', (vid,)).fetchone()
                    conn.close()
                    fmt = (row['format'] or '').lower() if row else ''
                    if fmt == 'cbr':
                        resp = convert_cbr(vid)
                    elif fmt == 'pdf':
                        resp = convert_pdf(vid)
                    elif fmt == 'zip':
                        resp = convert_zip(vid)
                    else:
                        print(f"✗ Conversion en arrière-plan ignorée pour le tome #{vid}: format '{fmt}' non convertible")
                        continue
                    payload = resp.get_json(silent=True) or {}
                    if not payload.get('success'):
                        print(f"✗ Erreur conversion en arrière-plan (tome #{vid}): {payload.get('error')}")
                except Exception as e:
                    print(f"✗ Erreur conversion en arrière-plan (tome #{vid}): {e}")

    threading.Thread(target=_run_batch, daemon=True).start()
    return jsonify({'success': True, 'count': len(volume_ids)})
