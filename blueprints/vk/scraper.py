"""
Forum VK « Au Phil des Bulles » (vk.com/board203785966) comme flux RSS des Nouveautés.

Le forum n'a pas d'API utilisable sans compte (board.getComments exige un jeton), mais ses
pages publiques embarquent la réponse de l'API en JSON : board.getTopics sur la page du
forum (sujets triés par dernière mise à jour) et board.getComments sur chaque sujet (texte
des messages et pièces jointes « doc » avec titre, taille, date et lien signé). Vkomic
(github.com/G-kylexy/vkomic) lit les mêmes données avec un jeton ; il n'a pas de recherche
côté serveur non plus (il synchronise tout l'arbre puis filtre localement).

On ne parcourt donc pas tout le forum : chaque lecture du flux relit la page du forum et ne
recharge que les sujets dont la date de mise à jour a changé. Les fichiers se téléchargent
en HTTP direct, sans compte (lien vk.com/doc… redirigé vers userapi.com), dans un thread
interne comme fourtoutici, avec reprise du fichier .part.
"""
import html
import json
import os
import re
import threading
import time
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlparse

from network_safety import safe_external_get

VK_DEFAULT_BASE_URL = 'https://vk.com/board203785966'
VK_GROUP_ID = '203785966'
VK_BOARD_URL = f'https://vk.com/board{VK_GROUP_ID}'
VK_TOPIC_URL = 'https://vk.com/topic-' + VK_GROUP_ID + '_{topic_id}'
_BOARD_ADDRESS = re.compile(r'^https://(?P<host>(?:m\.)?vk\.(?:com|ru))/board(?P<group>\d+)/?$')


def normalize_base_url(url):
    """Adresse du forum (« https://vk.com/board203785966 », vk.ru accepté). Vide -> défaut."""
    url = str(url or '').strip().rstrip('/')
    if not url:
        return VK_DEFAULT_BASE_URL
    if not url.startswith('http'):
        url = 'https://' + url
    url = url.replace('http://', 'https://', 1).replace('://m.vk.', '://vk.', 1)
    if not _BOARD_ADDRESS.match(url):
        raise ValueError('Adresse VK invalide : https://vk.com/board<numéro du groupe> attendu')
    return url


def configure(base_url):
    """Applique l'adresse configurée (domaine vk.com/vk.ru et numéro du groupe)."""
    global VK_GROUP_ID, VK_BOARD_URL, VK_TOPIC_URL, _INDEX_LINK
    match = _BOARD_ADDRESS.match(normalize_base_url(base_url))
    host, group = match.group('host'), match.group('group')
    if (VK_BOARD_URL, VK_GROUP_ID) == (f'https://{host}/board{group}', group):
        return
    VK_GROUP_ID = group
    VK_BOARD_URL = f'https://{host}/board{group}'
    VK_TOPIC_URL = f'https://{host}/topic-{group}' + '_{topic_id}'
    _INDEX_LINK = _index_link_pattern(group)
    with _topic_cache_lock:
        _topic_cache.clear()
    _topic_docs_cache.clear()


def configure_from_app():
    """Relit l'adresse dans la configuration de l'application, quand elle est disponible."""
    try:
        from flask import has_app_context
        if has_app_context():
            from .routes import load_vk_config
            configure(load_vk_config().get('base_url'))
    except ValueError as exc:
        print(f'Adresse VK ignorée : {exc}')
VK_FEED_NAME = 'VK – Au Phil des Bulles'

_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0',
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.5',
}
MAX_PAGE_BYTES = 8 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024
SUPPORTED_EXTENSIONS = {'pdf', 'cbz', 'cbr', 'zip', 'rar'}
# Un fichier n'apparaît dans le flux que s'il a été posté dans cette fenêtre.
VK_MAX_AGE_DAYS = 30
# Pause entre deux pages VK : rester un lecteur discret.
VK_REQUEST_INTERVAL_SECONDS = 0.8
VK_COMMENTS_PER_PAGE = 20
# Sujet court : on lit toutes ses pages (un fichier peut remplacer une « carte » réservée
# dans un ancien message) ; sujet long : seulement la dernière page.
VK_FULL_TOPIC_MAX_COMMENTS = 60

# Sujets d'organisation, de demandes ou d'autres langues : jamais de nouveautés BD.
_SKIPPED_TOPICS = re.compile(
    r'DEMANDES|REQUESTS|ANNONCES|README|LIRE EN PREMIER|FONCTIONNEMENT|OTHER LANGUAGES|'
    r'COMICS IN ENGLISH|AUF DEUTSCH|FUMETTI|BDS EN FRAN', re.IGNORECASE)
# Sujets « catégorie » : le titre du sujet n'est pas le nom de la série.
_CATEGORY_TOPICS = re.compile(r'^(ONE[ -]?SHOTS?|INTEGRALES?|INTÉGRALES?|DIVERS|COLLECTIF)\b', re.IGNORECASE)
_DOC_PATH = re.compile(r'^/doc-?\d+_\d+$')

_topic_cache = {}
_topic_cache_lock = threading.Lock()
_last_request = [0.0]


def _throttle():
    wait = _last_request[0] + VK_REQUEST_INTERVAL_SECONDS - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_request[0] = time.monotonic()


def _fetch_page(url):
    _throttle()
    with safe_external_get(url, headers=_HEADERS, timeout=30, max_bytes=MAX_PAGE_BYTES) as response:
        if response.status_code != 200:
            raise RuntimeError(f'VK a répondu HTTP {response.status_code} pour {url}')
        return response.content.decode('cp1251', errors='replace')


def embedded_api_responses(page, method):
    """Réponses de l'API VK embarquées dans une page publique (« "method":"board.getTopics" »)."""
    decoder = json.JSONDecoder()
    found = []
    marker = f'"method":"{method}"'
    index = page.find(marker)
    while index >= 0:
        start = page.rfind('{', 0, index)
        try:
            value = decoder.raw_decode(page[start:])[0]
            if isinstance(value, dict) and isinstance(value.get('response'), dict):
                found.append(value)
        except ValueError:
            pass
        index = page.find(marker, index + len(marker))
    return found


def _normalize(text):
    text = unicodedata.normalize('NFD', str(text or ''))
    text = ''.join(c for c in text if unicodedata.category(c) != 'Mn').lower()
    return re.sub(r'[\W_]+', ' ', text).strip()


def entry_title(topic_title, doc_title, extension=''):
    """Nom de fichier du flux : le titre du document, précédé du nom de la série (titre du
    sujet) quand le document ne le contient pas (« T01 - Volume 1.pdf » dans SCARLET QUEEN)."""
    topic = html.unescape(str(topic_title or '')).strip()
    name = html.unescape(str(doc_title or '')).strip() or 'Sans titre'
    ext = str(extension or '').lower().strip('.')
    if ext and not name.lower().endswith('.' + ext):
        name = f'{name}.{ext}'
    if not topic or _CATEGORY_TOPICS.search(topic) or _title_key(topic) in _title_key(name):
        return name
    if topic.isupper():
        topic = topic.title()
    return f'{topic} - {name}'


def safe_filename(name):
    name = os.path.basename(str(name or '')).strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    return name[:200].strip(' .')


def is_vk_doc_url(url):
    parsed = urlparse(str(url or ''))
    host = (parsed.hostname or '').lower()
    return (parsed.scheme == 'https' and host in {'vk.com', 'vk.ru', 'm.vk.com', 'm.vk.ru'}
            and bool(_DOC_PATH.match(parsed.path)))


def _iso(timestamp):
    return datetime.fromtimestamp(int(timestamp or 0), tz=timezone.utc).isoformat()


def _topic_entries(topic, cutoff):
    """Fichiers récents d'un sujet, sous forme d'entrées de flux RSS."""
    topic_id = int(topic['id'])
    title = html.unescape(str(topic.get('title') or ''))
    comments = int(topic.get('comments') or 0)
    last_offset = max(0, comments - VK_COMMENTS_PER_PAGE)
    offsets = (range(0, last_offset + 1, VK_COMMENTS_PER_PAGE)
               if comments <= VK_FULL_TOPIC_MAX_COMMENTS else [last_offset])
    entries = []
    for offset in offsets:
        page = _fetch_page(VK_TOPIC_URL.format(topic_id=topic_id) + (f'?offset={offset}' if offset else ''))
        for block in embedded_api_responses(page, 'board.getComments'):
            for comment in block['response'].get('items') or []:
                for attachment in comment.get('attachments') or []:
                    doc = attachment.get('doc') if attachment.get('type') == 'doc' else None
                    if not doc or not is_vk_doc_url(doc.get('url')):
                        continue
                    ext = str(doc.get('ext') or '').lower()
                    posted = max(int(doc.get('date') or 0), int(comment.get('date') or 0))
                    if ext not in SUPPORTED_EXTENSIONS or posted < cutoff:
                        continue
                    name = entry_title(title, doc.get('title'), ext)
                    link = (VK_TOPIC_URL.format(topic_id=topic_id)
                            + f"?post={comment.get('id')}#doc{doc.get('owner_id')}_{doc.get('id')}")
                    size = int(doc.get('size') or 0)
                    entries.append({
                        'title': name,
                        'filename': name,
                        'link': link,
                        'page_link': link,
                        'comments_link': '',
                        'description': f"{title} · {round(size / 1048576)} Mo" if size else title,
                        'download_links': [{'url': doc['url'], 'label': 'Télécharger depuis VK'}],
                        'date': _iso(posted),
                        'feed_title': VK_FEED_NAME,
                        'size': size,
                        'vk': True,
                        'vk_topic': title,
                    })
    return entries


def fetch_vk_feed(_feed=None, max_age_days=VK_MAX_AGE_DAYS):
    """Entrées du flux VK : fichiers postés récemment dans les sujets mis à jour récemment.
    Seuls les sujets dont la date de mise à jour a changé depuis la dernière lecture sont
    rechargés."""
    configure_from_app()
    board = _fetch_page(VK_BOARD_URL)
    topics = [item for block in embedded_api_responses(board, 'board.getTopics')
              for item in block['response'].get('items') or []]
    if not topics:
        raise RuntimeError('Forum VK illisible : aucun sujet trouvé sur la page publique')
    cutoff = time.time() - max_age_days * 86400
    entries, errors = [], []
    for topic in topics:
        if int(topic.get('updated') or 0) < cutoff or _SKIPPED_TOPICS.search(str(topic.get('title') or '')):
            continue
        key = int(topic['id'])
        with _topic_cache_lock:
            cached = _topic_cache.get(key)
        if cached and cached[0] == topic.get('updated') and cached[1] == topic.get('comments'):
            entries.extend(cached[2])
            continue
        try:
            fresh = _topic_entries(topic, cutoff)
        except Exception as exc:
            errors.append(f"{topic.get('title')}: {exc}")
            if cached:
                entries.extend(cached[2])
            continue
        with _topic_cache_lock:
            _topic_cache[key] = (topic.get('updated'), topic.get('comments'), fresh)
        entries.extend(fresh)
    if errors and not entries:
        raise RuntimeError('VK : ' + '; '.join(errors[:3]))
    entries.sort(key=lambda entry: entry['date'], reverse=True)
    return entries


def download_vk_file(url, filename, target_dir, progress_callback=None, attempts=3):
    """Télécharge un document VK dans target_dir. Le fichier s'écrit dans « .part » (ignoré
    par l'import automatique) et reprend là où il s'est arrêté (en-tête Range) après une
    coupure, comme Telegram."""
    if not is_vk_doc_url(url):
        raise ValueError('Lien VK non autorisé (vk.com/doc… attendu)')
    target_root = os.path.realpath(target_dir)
    name = safe_filename(filename)
    if not name:
        raise ValueError('Nom de fichier non autorisé')
    target_path = os.path.realpath(os.path.join(target_root, name))
    if os.path.dirname(target_path) != target_root:
        raise ValueError('Nom de fichier non autorisé')
    if os.path.exists(target_path):
        base, ext = os.path.splitext(name)
        target_path = os.path.join(target_root, f'{base}_vk{ext}')
    temp_path = target_path + '.part'

    last_error = None
    for attempt in range(attempts):
        start = os.path.getsize(temp_path) if os.path.exists(temp_path) else 0
        headers = dict(_HEADERS, Referer='https://vk.com/')
        if start:
            headers['Range'] = f'bytes={start}-'
        try:
            with safe_external_get(url, headers=headers, timeout=60) as response:
                if response.status_code == 416 and start:
                    os.remove(temp_path)
                    continue
                if response.status_code not in (200, 206):
                    raise RuntimeError(
                        f'Fichier indisponible sur VK (HTTP {response.status_code})'
                        if response.status_code in (404, 410, 502, 503, 504)
                        else f'Téléchargement VK impossible (HTTP {response.status_code})')
                content_type = (response.headers.get('Content-Type') or '').lower()
                if content_type.startswith('text/html'):
                    raise RuntimeError('VK a renvoyé une page HTML au lieu du fichier')
                if response.status_code == 200 and start:
                    start = 0  # Range ignoré : on repart du début
                length = int(response.headers.get('Content-Length') or 0)
                total = start + length if length else None
                if total and total > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError('Téléchargement refusé : fichier trop volumineux')
                received = start
                with open(temp_path, 'ab' if start else 'wb') as out:
                    for chunk in response.iter_content(chunk_size=256 * 1024):
                        if not chunk:
                            continue
                        out.write(chunk)
                        received += len(chunk)
                        if received > MAX_DOWNLOAD_BYTES:
                            raise RuntimeError('Téléchargement interrompu : fichier trop volumineux')
                        if progress_callback:
                            progress_callback(received, total)
                if total and received != total:
                    raise RuntimeError(f'Téléchargement tronqué : {received} octets reçus sur {total}')
            os.replace(temp_path, target_path)
            return target_path
        except RuntimeError as exc:
            last_error = exc
            if 'indisponible' in str(exc) or 'trop volumineux' in str(exc) or 'HTML' in str(exc):
                break
        except Exception as exc:  # coupure réseau : on reprend depuis le .part
            last_error = exc
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(str(last_error) if last_error else 'Téléchargement VK impossible')


def download_vk_file_background(url, filename, target_dir, app=None, pending_title=None, series_id=None,
                                volume_id=None, volume_number=None, force_replace=False, source_link=None):
    """Même cycle que fourtoutici : ligne « en attente » dans active_downloads, progression,
    historique, puis import automatique immédiat du fichier terminé."""
    def run():
        download_id = None
        if app:
            try:
                from blueprints.missing_monitor.downloader import mark_download_pending
                with app.app_context():
                    download_id = mark_download_pending(
                        pending_title or filename, 'vk', series_id=series_id, volume_id=volume_id,
                        volume_number=volume_number, force_replace=force_replace)
            except Exception as exc:
                print(f'Erreur enregistrement téléchargement VK en attente: {exc}')

        last_pct = [-1]

        def on_progress(current, total):
            if not app or not download_id or not total:
                return
            pct = int(current * 100 / total)
            if pct == last_pct[0] and pct < 100:
                return
            last_pct[0] = pct
            try:
                from blueprints.missing_monitor.downloader import update_download_progress
                with app.app_context():
                    update_download_progress(download_id, current, total)
            except Exception as exc:
                print(f'Erreur progression téléchargement VK: {exc}')

        def log(success, message):
            if not app:
                return
            try:
                from blueprints.missing_monitor.downloader import log_manual_download
                with app.app_context():
                    log_manual_download(filename, 'vk', success, message, source='vk',
                                        source_link=source_link, tracking_id=download_id)
            except Exception as exc:
                print(f'Erreur journalisation téléchargement VK: {exc}')

        try:
            os.makedirs(target_dir, exist_ok=True)
            target_path = download_vk_file(url, filename, target_dir, progress_callback=on_progress)
            log(True, 'Téléchargé depuis VK')
            if app and download_id:
                from blueprints.missing_monitor.downloader import mark_download_completed
                with app.app_context():
                    mark_download_completed(download_id)
            if app:
                try:
                    from blueprints.library.routes import attempt_immediate_auto_import
                    with app.app_context():
                        attempt_immediate_auto_import(target_path, target_dir)
                except Exception as exc:
                    print(f'Erreur import automatique après téléchargement VK: {exc}')
        except Exception as exc:
            log(False, str(exc))
            if app and download_id:
                from blueprints.missing_monitor.downloader import mark_download_failed
                with app.app_context():
                    mark_download_failed(download_id)

    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------------------
# Recherche : index local des séries du forum
# ---------------------------------------------------------------------------
# Le forum n'a pas de recherche utilisable sans compte (la page act=search ne renvoie rien
# en anonyme). Les archives sont en revanche des listes « Titre : lien du sujet », classées
# par genre puis par lettre : racine (BD / mangas en français) -> sujets de genre -> séries.
VK_INDEX_ROOTS = {
    '47386771': 'BD',
    '47423270': 'Mangas',
}
VK_INDEX_MAX_AGE_SECONDS = 24 * 3600
VK_TOPIC_MAX_PAGES = 15
VK_SEARCH_MAX_TOPICS = 3
VK_TOPIC_DOCS_TTL_SECONDS = 1800

# Une ligne d'archive : « Titre : lien », parfois « Titre . lien », « Titre ; lien » ou
# « Titre lien ». Le lien peut viser un message précis (?post=N) : plusieurs séries
# partagent alors un même sujet (Champignac, Super Groom… dans SPIROU ET FANTASIO).
def _index_link_pattern(group):
    return re.compile(r'https?://(?:m\.)?vk\.(?:com|ru)/topic-' + group + r'_(?P<topic>\d+)(?:\?post=(?P<post>\d+))?')


_INDEX_LINK = _index_link_pattern(VK_GROUP_ID)
_index_lock = threading.Lock()
_topic_docs_cache = {}


def parse_index_lines(text):
    """Liens « Titre : https://vk.com/topic-203785966_N[?post=M] » d'un message d'archive :
    liste de (titre, sujet, message ou None)."""
    found = []
    for line in html.unescape(str(text or '')).splitlines():
        match = _INDEX_LINK.search(line)
        if not match:
            continue
        title = re.sub(r'\s+', ' ', line[:match.start()])
        title = re.sub(r'^[\s\-–•►]+|[\s:;.,\-–]+$', '', title)
        # « RanXerox : RanXerox : lien » -> « RanXerox »
        parts = [part.strip() for part in title.split(' : ')]
        if len(parts) > 1 and len(set(parts)) == 1:
            title = parts[0]
        if 2 <= len(title) <= 160:
            found.append((title, match.group('topic'), match.group('post')))
    return found


def _topic_comments(topic_id, max_pages=VK_TOPIC_MAX_PAGES):
    """Tous les messages d'un sujet (au plus max_pages pages de 20)."""
    items, offset, count = [], 0, None
    for _ in range(max_pages):
        page = _fetch_page(VK_TOPIC_URL.format(topic_id=topic_id) + (f'?offset={offset}' if offset else ''))
        blocks = embedded_api_responses(page, 'board.getComments')
        if not blocks:
            break
        response = blocks[0]['response']
        count = int(response.get('count') or 0)
        batch = response.get('items') or []
        items.extend(batch)
        offset += VK_COMMENTS_PER_PAGE
        if not batch or offset >= count:
            break
    return items


def build_vk_index():
    """Parcourt les archives (racine -> genres -> séries) et retourne la liste des séries."""
    series, seen = [], set()
    for root_id, section in VK_INDEX_ROOTS.items():
        genres = []
        for comment in _topic_comments(root_id, max_pages=3):
            genres.extend(parse_index_lines(comment.get('text')))
        for genre, genre_id, _post in genres:
            try:
                comments = _topic_comments(genre_id)
            except Exception as exc:
                print(f'Index VK : genre « {genre} » illisible : {exc}')
                continue
            for comment in comments:
                for title, topic_id, post in parse_index_lines(comment.get('text')):
                    key = (_title_key(title), topic_id)
                    if key in seen or topic_id == genre_id:
                        continue
                    seen.add(key)
                    series.append({'title': title, 'topic_id': topic_id, 'post': post,
                                   'genre': genre, 'section': section})
    return series


def _index_path(data_dir):
    return os.path.join(data_dir, 'vk_index.json')


def load_vk_index(data_dir, max_age=VK_INDEX_MAX_AGE_SECONDS):
    """Index des séries VK, reconstruit s'il a plus d'un jour (une seule reconstruction à la fois)."""
    path = _index_path(data_dir)

    def read():
        try:
            with open(path, encoding='utf-8') as handle:
                value = json.load(handle)
            return value if isinstance(value.get('series'), list) else None
        except (OSError, ValueError, AttributeError):
            return None

    def fresh(value):
        return (value and value.get('group', VK_GROUP_ID) == VK_GROUP_ID
                and time.time() - float(value.get('built_at') or 0) < max_age)

    current = read()
    if fresh(current):
        return current['series']
    with _index_lock:
        current = read()
        if fresh(current):
            return current['series']
        if current and current.get('group', VK_GROUP_ID) != VK_GROUP_ID:
            current = None
        try:
            series = build_vk_index()
        except Exception as exc:
            if current:
                print(f'Index VK non reconstruit, ancien index conservé : {exc}')
                return current['series']
            raise
        if not series and current:
            return current['series']
        temp = path + '.tmp'
        with open(temp, 'w', encoding='utf-8') as handle:
            json.dump({'built_at': time.time(), 'group': VK_GROUP_ID, 'series': series}, handle, ensure_ascii=False)
        os.replace(temp, path)
        return series


def _title_key(title):
    """« Ailes du Singe (Les) » et « Les Ailes du Singe » donnent la même clé."""
    text = _normalize(re.sub(r'\((?:le|la|les|l|un|une|des|the)\)', ' ', str(title or ''), flags=re.IGNORECASE))
    words = [w for w in text.split() if w not in {'le', 'la', 'les', 'l', 'un', 'une', 'des', 'the', 'de', 'du', 'd'}]
    return ' '.join(words)


def find_vk_series(query, index, limit=VK_SEARCH_MAX_TOPICS):
    """Séries de l'index qui correspondent au titre cherché, les plus proches d'abord."""
    wanted = _title_key(query)
    if not wanted:
        return []
    wanted_words = set(wanted.split())
    scored = []
    for entry in index:
        key = _title_key(re.sub(r'\([^)]*\)', ' ', entry['title'])) or _title_key(entry['title'])
        full_key = _title_key(entry['title'])
        if wanted in (key, full_key):
            score = 3
        elif re.search(r'(^| )' + re.escape(wanted) + r'( |$)', full_key):
            score = 2
        elif key and set(key.split()) <= wanted_words and len(key) >= 4:
            score = 1
        else:
            continue
        scored.append((score, -abs(len(key) - len(wanted)), entry))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best = scored[0][0] if scored else 0
    return [entry for score, _, entry in scored if score == best][:limit]


_VOLUME_LINE = re.compile(r'^\s*(?:T|Tome|HS|Int|Vol)?\s*\d', re.IGNORECASE)


def _comment_series(text):
    """Nom de série en tête d'un message (« Champignac (Scénario : BéKa…) » -> « Champignac »),
    ou None si le message commence directement par une liste de tomes."""
    first = next((line.strip() for line in html.unescape(str(text or '')).splitlines() if line.strip()), '')
    if not first or _VOLUME_LINE.match(first) or first.lower().startswith(('genre', 'origine', 'langue')):
        return None
    name = re.sub(r'\s*\(.*$', '', first).strip(' :-–')
    return name[:120] or None


def vk_topic_files(topic_id, topic_title, wanted_title=None):
    """Fichiers d'un sujet (cache de 30 min). Un sujet peut regrouper plusieurs séries
    (SPIROU ET FANTASIO contient Champignac, Seccotine…) : chaque message commence par le
    nom de sa série. Si un message porte la série cherchée, seuls ses messages (et ceux
    qui le prolongent sans en-tête) sont gardés ; les fichiers sont nommés d'après lui."""
    cache_key = topic_id
    cached = _topic_docs_cache.get(cache_key)
    if cached and time.time() - cached[0] < VK_TOPIC_DOCS_TTL_SECONDS:
        files = cached[1]
    else:
        files = []
        current_series = None
        for comment in _topic_comments(topic_id):
            docs = [a.get('doc') for a in comment.get('attachments') or []
                    if a.get('type') == 'doc' and a.get('doc')]
            header = _comment_series(comment.get('text'))
            if header and docs:
                current_series = header
            for doc in docs:
                if not is_vk_doc_url(doc.get('url')):
                    continue
                ext = str(doc.get('ext') or '').lower()
                if ext not in SUPPORTED_EXTENSIONS:
                    continue
                link = (VK_TOPIC_URL.format(topic_id=topic_id)
                        + f"?post={comment.get('id')}#doc{doc.get('owner_id')}_{doc.get('id')}")
                files.append({
                    'doc_title': doc.get('title'),
                    'ext': ext,
                    'series': current_series,
                    'size': int(doc.get('size') or 0),
                    'download_url': doc['url'],
                    'link': link,
                    'date': _iso(max(int(doc.get('date') or 0), int(comment.get('date') or 0))),
                })
        _topic_docs_cache[cache_key] = (time.time(), files)

    wanted = _title_key(wanted_title or topic_title)
    selected = [f for f in files if f['series'] and _title_key(f['series']) == wanted]
    # Aucun message ne porte la série cherchée : tout le sujet est la série, nommée
    # d'après le titre du sujet (les en-têtes sont alors des éditions, « Collection Niffle »).
    by_header = bool(selected)
    if not selected:
        selected = files
    return [{
        'filename': entry_title(f['series'] if by_header else topic_title, f['doc_title'], f['ext']),
        'size': f['size'],
        'download_url': f['download_url'],
        'link': f['link'],
        'topic_title': topic_title,
        'date': f['date'],
    } for f in selected]


def search_vk_raw(query, data_dir, limit=400):
    """Fichiers des sujets VK dont le titre correspond à la série cherchée."""
    configure_from_app()
    index = load_vk_index(data_dir)
    results = []
    for entry in find_vk_series(query, index):
        results.extend(vk_topic_files(entry['topic_id'], entry['title'], entry['title']))
    return results[:limit]
