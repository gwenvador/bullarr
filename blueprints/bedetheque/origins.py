"""
Origine d'une série Bédéthèque (champ « Origine » de la fiche : Europe, USA, Asie, Autre),
pour marquer BD / Comics / Manga dans Nouveautés et sur la fiche série.

Ordre de recherche : séries de la bibliothèque (series.bedetheque_origin, déjà lu au
rattachement), puis cache (table bedetheque_origin_cache), puis lecture de la page série,
au plus `fetch_limit` pages par appel pour rester discret avec Bédéthèque. L'origine d'une
série ne change pas : une valeur trouvée est gardée ; une page sans origine lisible est
retentée après ORIGIN_RETRY_DAYS.
"""
import re
import sqlite3
import threading
import time
from collections import deque
from urllib.parse import urlparse

ORIGIN_KINDS = {'europe': 'bd', 'usa': 'comics', 'asie': 'manga'}
# Libellés affichés par Bédéthèque (ex. « Franco-Belge » pour Europe, « Manga » pour Asie).
ORIGIN_LABELS = {'franco-belge': 'Europe', 'europe': 'Europe', 'comics': 'USA', 'usa': 'USA',
                 'manga': 'Asie', 'asie': 'Asie', 'autre': 'Autre'}
ORIGIN_RETRY_DAYS = 30
ORIGIN_FETCH_INTERVAL_SECONDS = 0.5
# Lectures de fiches au plus par heure pour cet usage (Nouveautés) ; le reste attend l'heure suivante.
ORIGIN_FETCH_MAX_PER_HOUR = 60
_recent_fetches = deque()
_recent_fetches_lock = threading.Lock()
# Mise en page actuelle : <li title="Origine de la série"><i class="… bdt-ori-N"></i> Manga</li>
_ORIGIN_RE = re.compile(r'title="Origine de la s(?:é|&eacute;|e)rie"[^>]*>(?:\s*<i[^>]*></i>)?\s*([^<]+?)\s*<',
                        re.IGNORECASE)
_LABEL_RE = re.compile(r'<label>\s*Origine\s*:\s*</label>\s*([^<]+?)\s*<', re.IGNORECASE)


def origin_budget_left(now=None):
    """Lectures de fiches encore permises cette heure pour les origines."""
    now = time.time() if now is None else now
    with _recent_fetches_lock:
        while _recent_fetches and now - _recent_fetches[0] > 3600:
            _recent_fetches.popleft()
        return max(0, ORIGIN_FETCH_MAX_PER_HOUR - len(_recent_fetches))


def origin_fetch_limited():
    """True si les origines manquantes ne seront pas lues maintenant : budget horaire épuisé,
    pause du site ou limite des 24 h (voir guard.py)."""
    if origin_budget_left() <= 0:
        return True
    try:
        from .guard import get_guard
        return get_guard().is_limited()
    except Exception:
        return False


def store_origin(database, url, origin):
    """Mémorise l'origine d'une fiche déjà lue pour une autre raison (ajout, mise à jour)."""
    canonical = normalize_series_url(url)
    value = ORIGIN_LABELS.get((origin or '').strip().lower(), (origin or '').strip())
    if not canonical or not value:
        return False
    conn = sqlite3.connect(database, timeout=30.0)
    try:
        _ensure_cache(conn)
        conn.execute('INSERT OR REPLACE INTO bedetheque_origin_cache (url, origin, fetched_at) VALUES (?, ?, ?)',
                     (canonical, value, time.time()))
        conn.commit()
    finally:
        conn.close()
    _save_to_catalog([(canonical, value)])
    return True


def origin_kind(origin):
    """'bd', 'comics', 'manga' ou None (Autre, inconnue)."""
    value = (origin or '').strip().lower()
    return ORIGIN_KINDS.get(ORIGIN_LABELS.get(value, value).lower())


def parse_origin(html):
    """Origine lue dans le HTML d'une fiche série (pastille « Origine de la série », ou
    ancienne mise en page « Origine : »)."""
    for pattern in (_ORIGIN_RE, _LABEL_RE):
        match = pattern.search(html or '')
        if match:
            value = match.group(1).strip()
            if value:
                return ORIGIN_LABELS.get(value.lower(), value)
    return None


def normalize_series_url(url):
    """Adresse canonique d'une fiche série Bédéthèque, ou None si ce n'en est pas une."""
    parsed = urlparse(str(url or '').strip())
    host = (parsed.hostname or '').lower()
    if parsed.scheme not in ('http', 'https') or host not in ('www.bedetheque.com', 'bedetheque.com'):
        return None
    if not re.fullmatch(r'/serie-\d+-[^/]+\.html', parsed.path or ''):
        return None
    return f'https://www.bedetheque.com{parsed.path}'


def _ensure_cache(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS bedetheque_origin_cache (
        url TEXT PRIMARY KEY, origin TEXT, fetched_at REAL NOT NULL)''')


def _save_to_catalog(pairs):
    """Ajoute des origines au catalogue Bédéthèque local (voir catalog_index.save_catalog_origins)."""
    try:
        from .catalog_index import save_catalog_origins
        save_catalog_origins(pairs)
    except Exception:
        pass  # le catalogue est un confort: une écriture ratée ne bloque jamais la lecture


_catalog_seeded_for = set()


def seed_catalog_origins(database, conn):
    """Une fois par processus et par base: verse au catalogue les origines déjà connues ailleurs
    (séries de la bibliothèque, ancien cache d'origines), pour ne jamais les relire sur Bédéthèque."""
    if database in _catalog_seeded_for:
        return
    pairs = []
    for url, origin in conn.execute("SELECT bedetheque_url, bedetheque_origin FROM series "
                                    "WHERE COALESCE(bedetheque_origin, '') != '' AND bedetheque_url IS NOT NULL"):
        canonical = normalize_series_url(url)
        value = ORIGIN_LABELS.get((origin or '').strip().lower(), (origin or '').strip())
        if canonical and value:
            pairs.append((canonical, value))
    for url, origin in conn.execute("SELECT url, origin FROM bedetheque_origin_cache WHERE COALESCE(origin, '') != ''"):
        pairs.append((url, origin))
    _save_to_catalog(pairs)
    _catalog_seeded_for.add(database)


def resolve_origins(database, urls, fetch_page=None, fetch_limit=6):
    """{url: origine ou None} pour chaque adresse de série valide, et le nombre d'adresses
    restées sans réponse faute d'avoir pu les lire dans cet appel (« pending »).

    fetch_page(url) -> HTML : lecture d'une page (None : aucune lecture réseau)."""
    wanted = []
    for url in urls or []:
        canonical = normalize_series_url(url)
        if canonical and canonical not in wanted:
            wanted.append(canonical)
    result = {}
    if not wanted:
        return result, 0
    conn = sqlite3.connect(database, timeout=30.0)
    try:
        _ensure_cache(conn)
        placeholders = ','.join('?' * len(wanted))
        for url, origin in conn.execute(
                f"SELECT bedetheque_url, bedetheque_origin FROM series WHERE bedetheque_url IN ({placeholders}) "
                "AND COALESCE(bedetheque_origin, '') != ''", wanted):
            result[url] = origin
        seed_catalog_origins(database, conn)
        try:
            from .catalog_index import get_catalog_origins
            for url, origin in get_catalog_origins([u for u in wanted if u not in result]).items():
                result[url] = origin
        except Exception:
            pass  # catalogue indisponible: repli sur le cache puis sur la page
        retry_before = time.time() - ORIGIN_RETRY_DAYS * 86400
        for url, origin, fetched_at in conn.execute(
                f"SELECT url, origin, fetched_at FROM bedetheque_origin_cache WHERE url IN ({placeholders})", wanted):
            if url in result:
                continue
            if origin or fetched_at >= retry_before:
                result[url] = origin or None
        missing = [url for url in wanted if url not in result]
        fetched = 0
        allowed = min(fetch_limit, origin_budget_left())
        from .guard import BedethequeGuardError
        for url in missing:
            if fetch_page is None or fetched >= allowed:
                break
            if fetched:
                time.sleep(ORIGIN_FETCH_INTERVAL_SECONDS)
            fetched += 1
            with _recent_fetches_lock:
                _recent_fetches.append(time.time())
            try:
                origin = parse_origin(fetch_page(url))
            except BedethequeGuardError:
                break  # pause du site ou limite atteinte : on s'arrête, rien n'est mémorisé
            except Exception:
                continue  # réessayé au prochain appel
            conn.execute('INSERT OR REPLACE INTO bedetheque_origin_cache (url, origin, fetched_at) VALUES (?, ?, ?)',
                         (url, origin, time.time()))
            conn.commit()
            if origin:
                _save_to_catalog([(url, origin)])
            result[url] = origin
        pending = len([url for url in wanted if url not in result])
        return result, pending
    finally:
        conn.close()
