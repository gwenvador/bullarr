"""
Index local du catalogue Bédéthèque - "apres ce qu'on pourrait faire c'est telecharger
deja en db toutes l'index des series et chercher prendrait tres peu de temsp" : les 27
pages de listing alphabétique (bandes_dessinees_0.html + _A.html à _Z.html, ~5000+ séries
CHACUNE rien que pour "A") donnent déjà titre+URL de CHAQUE série connue de Bédéthèque, en
un seul fetch par lettre - une recherche locale (FTS5, quasi instantanée) dans cette copie
remplace alors la requête live vers /search/tout (avec son délai anti-bot, voir
_anti_bot_delay côté scraper.py) qu'on paie aujourd'hui à CHAQUE appel de search_series.
La seule requête live qui reste est le fetch de la fiche complète du candidat FINALEMENT
choisi (déjà fait aujourd'hui par get_series_info, inchangé).

Construction MANUELLE uniquement (pas de rafraîchissement automatique planifié, sur
demande explicite - voir /settings) : ~27 requêtes à ~1s d'anti-bot delay chacune, de
l'ordre de la minute pour tout reconstruire. Base dédiée (data/bedetheque_catalog.db),
même principe qu'ebdz.db/telegram_messages.db - un cache scrapé séparé de bullarr.db,
entièrement reconstructible depuis Bédéthèque à tout moment.
"""
import json
import os
import sqlite3
import time

CATALOG_DB = './data/bedetheque_catalog.db'
_LETTERS = ['0'] + [chr(c) for c in range(ord('A'), ord('Z') + 1)]

# État de progression en mémoire (process unique, pas besoin de DB) - consulté par
# GET /api/bedetheque/catalog-index/status pendant qu'une construction tourne, même
# principe que _metadata_write_progress (bedetheque/routes.py).
_build_progress = {'running': False, 'current': None, 'done': 0, 'total': len(_LETTERS), 'error': None}


def _connect_db():
    os.makedirs(os.path.dirname(CATALOG_DB), exist_ok=True)
    conn = sqlite3.connect(CATALOG_DB, timeout=30.0)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS bedetheque_series (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            title_normalized TEXT NOT NULL
        )
    ''')
    # tokenize='trigram': substrings insensibles aux accents/casse/ponctuation une fois
    # title_normalized déjà normalisé à l'écriture - même principe qu'ed2k_links_fts/
    # telegram_files_fts (voir leurs commentaires respectifs, blueprints/ebdz/scraper.py
    # et blueprints/telegram_channels/scraper.py). Pas de triggers de synchronisation
    # incrémentale ici: cette table est TOUJOURS entièrement reconstruite (DELETE + repeuplement
    # + rebuild FTS), jamais modifiée ligne à ligne - voir build_bedetheque_catalog_index_sync.
    conn.execute('''
        CREATE VIRTUAL TABLE IF NOT EXISTS bedetheque_series_fts USING fts5(
            title_normalized, content='bedetheque_series', content_rowid='id', tokenize='trigram'
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS bedetheque_catalog_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    # "add the entry for indispensables in cache like the index" / "do all the entry in
    # enrichir get cache?" - un cache générique clé/valeur pour TOUTES les pages
    # "listing" scrapées de /bedetheque-enrich (Indispensables, Panthéon, Thèmes - et
    # Top 100 BDGest, blueprints/bdgest/routes.py, même mécanisme réutilisé bien que sur
    # un domaine différent) qui ne vivaient jusqu'ici que dans un dict Python en mémoire
    # par module (_INDISPENSABLES_CACHE/_PANTHEON_CACHE/_THEMES_LIST_CACHE/
    # _THEME_DETAIL_CACHE/_TOP_ANNUEL_CACHE), perdu à chaque redémarrage du conteneur -
    # un premier appel juste après un déploiement repayait tout le scrape (+ son risque
    # de 403 Cloudflare le temps qu'une session s'y réchauffe, voir
    # BedethequeScraper._ensure_session) au lieu de servir un résultat déjà connu. Une
    # seule table à clé libre (cache_key = "indispensables:franco-belge",
    # "themes:detail:aventure", "bdgest:top-annuel:2024:general", ...) plutôt qu'une
    # table dédiée par page - même DB que le catalogue (data/bedetheque_catalog.db,
    # "séparé de bullarr.db, entièrement reconstructible depuis Bédéthèque à tout
    # moment" - même principe ici, juste PAS le même site pour BDGest).
    conn.execute('''
        CREATE TABLE IF NOT EXISTS bedetheque_scrape_cache (
            cache_key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            built_at TEXT NOT NULL
        )
    ''')
    conn.commit()
    return conn


def get_cached_scrape(cache_key, max_age_seconds=None):
    """Renvoie la valeur déjà scrapée pour `cache_key` si elle existe, sinon None - signal
    distinct de "jamais scrapé" (routes.py: scrape + persiste via save_scrape_cache).

    "the data does not really change much so no need to update automatic. put only update
    manual" - PAS d'expiration automatique par défaut (max_age_seconds=None): un cache
    construit une fois reste servi indéfiniment jusqu'à un rafraîchissement explicite
    (bouton "Actualiser" de /bedetheque-enrich, ?refresh=1 côté route - voir
    indispensable_series/pantheon_authors/list_themes/theme_series, bedetheque/routes.py,
    et top_annuel, bdgest/routes.py). max_age_seconds reste un paramètre au cas où un futur
    appelant voudrait une expiration ponctuelle, mais aucun ne le fait aujourd'hui."""
    conn = _connect_db()
    row = conn.execute(
        'SELECT value_json, built_at FROM bedetheque_scrape_cache WHERE cache_key = ?', (cache_key,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    value_json, built_at = row
    if max_age_seconds is not None:
        try:
            built_ts = time.mktime(time.strptime(built_at, '%Y-%m-%d %H:%M:%S'))
        except ValueError:
            return None
        if time.time() - built_ts > max_age_seconds:
            return None
    try:
        return json.loads(value_json)
    except (TypeError, ValueError):
        return None


def save_scrape_cache(cache_key, value):
    conn = _connect_db()
    conn.execute(
        'INSERT INTO bedetheque_scrape_cache (cache_key, value_json, built_at) VALUES (?, ?, ?) '
        'ON CONFLICT(cache_key) DO UPDATE SET value_json = excluded.value_json, built_at = excluded.built_at',
        (cache_key, json.dumps(value), time.strftime('%Y-%m-%d %H:%M:%S'))
    )
    conn.commit()
    conn.close()


def get_catalog_status():
    """Alimente GET /api/bedetheque/catalog-index/status (carte Bédéthèque de /settings)."""
    conn = _connect_db()
    count = conn.execute('SELECT COUNT(*) FROM bedetheque_series').fetchone()[0]
    row = conn.execute("SELECT value FROM bedetheque_catalog_meta WHERE key = 'built_at'").fetchone()
    conn.close()
    return {
        'built': count > 0,
        'count': count,
        'built_at': row[0] if row else None,
        'running': _build_progress['running'],
        'progress': dict(_build_progress),
    }


INDEX_REFRESH_DEFAULTS = {'enabled': True, 'interval_days': 7, 'hour': 4}


def _refresh_config_path():
    return os.path.join(os.path.dirname(CATALOG_DB), 'bedetheque_index_config.json')


def _sanitize_refresh_config(raw):
    config = dict(INDEX_REFRESH_DEFAULTS)
    raw = raw if isinstance(raw, dict) else {}
    if isinstance(raw.get('enabled'), bool):
        config['enabled'] = raw['enabled']
    try:
        days = int(raw.get('interval_days', config['interval_days']))
        if 1 <= days <= 90:
            config['interval_days'] = days
    except (TypeError, ValueError):
        pass
    try:
        hour = int(raw.get('hour', config['hour']))
        if 0 <= hour <= 23:
            config['hour'] = hour
    except (TypeError, ValueError):
        pass
    return config


def load_index_refresh_config():
    """Réglage de la mise à jour périodique de l'index (activée, tous les N jours, à H heures)."""
    try:
        with open(_refresh_config_path(), encoding='utf-8') as handle:
            return _sanitize_refresh_config(json.load(handle))
    except (OSError, ValueError):
        return dict(INDEX_REFRESH_DEFAULTS)


def save_index_refresh_config(raw):
    """Valide puis enregistre le réglage; lève ValueError si une valeur est hors limites."""
    if not isinstance(raw, dict) or not isinstance(raw.get('enabled'), bool):
        raise ValueError("« enabled » doit être vrai ou faux")
    try:
        days, hour = int(raw.get('interval_days')), int(raw.get('hour'))
    except (TypeError, ValueError):
        raise ValueError('Intervalle et heure doivent être des nombres entiers')
    if not 1 <= days <= 90:
        raise ValueError("L'intervalle doit être compris entre 1 et 90 jours")
    if not 0 <= hour <= 23:
        raise ValueError("L'heure doit être comprise entre 0 et 23")
    config = {'enabled': raw['enabled'], 'interval_days': days, 'hour': hour}
    os.makedirs(os.path.dirname(CATALOG_DB), exist_ok=True)
    with open(_refresh_config_path(), 'w', encoding='utf-8') as handle:
        json.dump(config, handle, indent=2)
    return config


def refresh_catalog_index_if_stale(max_age_days=7, scraper=None):
    """Reconstruit l'index du catalogue s'il n'existe pas ou a plus de `max_age_days` jours.
    Appelée par le planificateur (voir index_scheduler.py). Un verrou de fichier évite que
    deux processus (workers) reconstruisent en même temps. Retourne True si une
    reconstruction a eu lieu."""
    import fcntl
    status = get_catalog_status()
    if status['running']:
        return False
    if status['built'] and status['built_at']:
        try:
            age = time.time() - time.mktime(time.strptime(status['built_at'], '%Y-%m-%d %H:%M:%S'))
            if age < max_age_days * 86400:
                return False
        except ValueError:
            pass
    os.makedirs(os.path.dirname(CATALOG_DB), exist_ok=True)
    with open(CATALOG_DB + '.build.lock', 'w') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        if scraper is None:
            from blueprints.bedetheque.scraper import BedethequeScraper
            scraper = BedethequeScraper()
        build_bedetheque_catalog_index_sync(scraper)
    return True


def search_catalog_index(query, limit=30):
    """Résultats au même format que _search_series_raw ({'title', 'url', 'genre': None} -
    genre indisponible depuis un simple listing, jamais utilisé par _match_score/
    search_and_get_best_match de toute façon) - permet à search_series de s'en servir
    comme remplacement direct de la recherche live.

    Retourne None (pas []) si l'index n'est pas encore construit - signal distinct de
    "construit mais aucun résultat", pour que search_series sache qu'il doit retomber sur
    la recherche live plutôt que de faire semblant qu'il n'y a vraiment aucun résultat.

    Filtre par mot (>=3 caractères, minimum trigramme) avec tous les mots requis - le TRI
    par pertinence final reste _match_score (scraper.py), déjà
    appliqué par tous les appelants de search_series exactement comme pour la recherche
    live (Bédéthèque elle-même n'étant pas fiable non plus sur ce point, voir CLAUDE.md) :
    ici aussi, mieux vaut un filtre FTS large + un re-tri Python fiable qu'un ordre SQL
    brut faussement pertinent."""
    from blueprints.search.routes import normalize_search_text
    from blueprints.bedetheque.scraper import BedethequeScraper

    q = normalize_search_text(query or '').strip()
    if not q:
        return []

    conn = _connect_db()
    conn.row_factory = sqlite3.Row
    total = conn.execute('SELECT COUNT(*) FROM bedetheque_series').fetchone()[0]
    if total == 0:
        conn.close()
        return None

    words = [w for w in q.split() if len(w) >= 3]
    if not words:
        conn.close()
        return []

    fts_query = ' AND '.join('"' + w.replace('"', '""') + '"' for w in words)
    try:
        rows = conn.execute('''
            SELECT s.title, s.url FROM bedetheque_series s
            JOIN bedetheque_series_fts fts ON fts.rowid = s.id
            WHERE bedetheque_series_fts MATCH ?
            LIMIT 5000
        ''', (fts_query,)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()

    candidates = [{'title': r['title'], 'url': r['url'], 'genre': None} for r in rows]
    candidates.sort(key=lambda c: BedethequeScraper._match_score(query, c['title']), reverse=True)
    return candidates[:limit]


def build_bedetheque_catalog_index_sync(scraper):
    """Reconstruit entièrement l'index (DELETE + repeuplement depuis les 27 pages de
    listing) - synchrone, appelée depuis un thread dédié par la route qui déclenche la
    construction (voir /api/bedetheque/catalog-index/build, bedetheque/routes.py), sur
    le même principe que _write_series_volumes_metadata_async pour la MAJ métadonnées en
    masse (job long -> thread + endpoint de progression séparé, pas de blocage HTTP).

    scraper: instance BedethequeScraper déjà prête (session/headers) - réutilisée telle
    quelle pour ces 27 requêtes plutôt que d'en recréer une."""
    global _build_progress
    _build_progress = {'running': True, 'current': None, 'done': 0, 'total': len(_LETTERS), 'error': None}
    from blueprints.search.routes import normalize_search_text
    from blueprints.bedetheque.scraper import _anti_bot_delay
    from bs4 import BeautifulSoup

    try:
        scraper._ensure_session()
        entries = []
        for letter in _LETTERS:
            _build_progress['current'] = letter
            resp = scraper.session.get(f'https://www.bedetheque.com/bandes_dessinees_{letter}.html', timeout=30)
            resp.encoding = 'utf-8'
            soup = BeautifulSoup(resp.content, 'html.parser')
            for a in soup.select('a[href*="/serie-"]'):
                title = a.get_text(strip=True)
                href = a.get('href')
                if title and href:
                    entries.append((title, href))
            _build_progress['done'] += 1
            _anti_bot_delay()

        conn = _connect_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM bedetheque_series')
        seen = set()
        for title, url in entries:
            if url in seen:
                continue
            seen.add(url)
            cursor.execute(
                'INSERT INTO bedetheque_series (title, url, title_normalized) VALUES (?, ?, ?)',
                (title, url, normalize_search_text(title))
            )
        # Reconstruction complète de l'index FTS depuis la table de contenu - même
        # commande que telegram_files_fts/ed2k_links_fts (voir ensure_telegram_search_index).
        cursor.execute("INSERT INTO bedetheque_series_fts(bedetheque_series_fts) VALUES('rebuild')")
        cursor.execute(
            "INSERT INTO bedetheque_catalog_meta (key, value) VALUES ('built_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (time.strftime('%Y-%m-%d %H:%M:%S'),)
        )
        conn.commit()
        conn.close()
        print(f"✓ Index catalogue Bédéthèque reconstruit: {len(seen)} séries")
    except Exception as e:
        _build_progress['error'] = 'Erreur interne'
        print(f"⚠️ Échec construction index catalogue Bédéthèque: {e}")
    finally:
        _build_progress['running'] = False
