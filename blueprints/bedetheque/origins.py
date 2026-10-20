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
import time
from urllib.parse import urlparse

ORIGIN_KINDS = {'europe': 'bd', 'usa': 'comics', 'asie': 'manga'}
ORIGIN_RETRY_DAYS = 7
ORIGIN_FETCH_INTERVAL_SECONDS = 0.5
_ORIGIN_RE = re.compile(r'title="Origine de la s(?:é|&eacute;|e)rie"[^>]*>\s*([^<]+?)\s*<', re.IGNORECASE)
_LABEL_RE = re.compile(r'<label>\s*Origine\s*:\s*</label>\s*([^<]+?)\s*<', re.IGNORECASE)


def origin_kind(origin):
    """'bd', 'comics', 'manga' ou None (Autre, inconnue)."""
    return ORIGIN_KINDS.get((origin or '').strip().lower())


def parse_origin(html):
    """Origine lue dans le HTML d'une fiche série (pastille « Origine de la série », ou
    ancienne mise en page « Origine : »)."""
    for pattern in (_ORIGIN_RE, _LABEL_RE):
        match = pattern.search(html or '')
        if match:
            value = match.group(1).strip()
            if value:
                return value
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
        retry_before = time.time() - ORIGIN_RETRY_DAYS * 86400
        for url, origin, fetched_at in conn.execute(
                f"SELECT url, origin, fetched_at FROM bedetheque_origin_cache WHERE url IN ({placeholders})", wanted):
            if url in result:
                continue
            if origin or fetched_at >= retry_before:
                result[url] = origin or None
        missing = [url for url in wanted if url not in result]
        fetched = 0
        for url in missing:
            if fetch_page is None or fetched >= fetch_limit:
                break
            if fetched:
                time.sleep(ORIGIN_FETCH_INTERVAL_SECONDS)
            fetched += 1
            try:
                origin = parse_origin(fetch_page(url))
            except Exception:
                continue  # réessayé au prochain appel
            conn.execute('INSERT OR REPLACE INTO bedetheque_origin_cache (url, origin, fetched_at) VALUES (?, ?, ?)',
                         (url, origin, time.time()))
            conn.commit()
            result[url] = origin
        pending = len([url for url in wanted if url not in result])
        return result, pending
    finally:
        conn.close()
