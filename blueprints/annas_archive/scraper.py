"""Recherche HTML publique sur Anna's Archive, sans contournement de protection."""
import re
from urllib.parse import quote_plus, urljoin
from bs4 import BeautifulSoup
from network_safety import safe_external_get

ANNA_ARCHIVE_BASE_URL = "https://annas-archive.gl"

def get_annas_archive_base_url():
    try:
        from .routes import load_annas_archive_config
        return (load_annas_archive_config().get('base_url') or ANNA_ARCHIVE_BASE_URL).rstrip('/')
    except RuntimeError:
        return ANNA_ARCHIVE_BASE_URL

_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}

def search_annas_archive_raw(query, limit=30):
    query = (query or "").strip()
    if not query: return []
    url = f"{get_annas_archive_base_url()}/search?index=&page=1&sort=&content=book_comic&display=&q={quote_plus(query)}"
    response = safe_external_get(url, headers=_HEADERS, timeout=30, max_bytes=8 * 1024 * 1024)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "html.parser")
    results = []
    seen = set()
    for link in soup.select('a[href^="/md5/"]'):
        href = link.get("href", "")
        if href in seen: continue
        title = " ".join(link.get_text(" ", strip=True).split())
        if not title: continue
        seen.add(href)
        card = link
        text = ""
        size_match = None
        # La métadonnée (PDF · 38.1MB · année · Comic book) se trouve plusieurs
        # niveaux au-dessus du lien de titre dans le HTML Anna's Archive.
        for _ in range(8):
            card = card.parent if card else None
            text = " ".join((card or link).get_text(" ", strip=True).split())
            size_match = re.search(r"(?i)(\d+(?:[.,]\d+)?)\s*(KB|MB|GB)", text)
            if size_match:
                break
        size = None
        if size_match:
            value = float(size_match.group(1).replace(",", "."))
            size = int(value * {"KB":1024,"MB":1024**2,"GB":1024**3}[size_match.group(2).upper()])
        info_url = urljoin(get_annas_archive_base_url(), href)
        md5 = href.rsplit('/', 1)[-1]
        # Serveur partenaire lent #5: pas de file d'attente, mais débit variable.
        # Le lien reste ouvert manuellement dans le navigateur; aucun téléchargement
        # silencieux ni contournement de vérification n'est effectué par Bullarr.
        partner_url = urljoin(get_annas_archive_base_url(), f"/slow_download/{md5}/0/4")
        results.append({"title": title, "info_url": info_url, "size": size, "partner_url": partner_url, "md5": md5})
        if len(results) >= limit: break
    return results
