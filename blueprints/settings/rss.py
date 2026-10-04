"""Configurable RSS feeds used by the Nouveautés page."""
import email.utils
import html
import re
import ipaddress
import json
import os
import copy
from concurrent.futures import ThreadPoolExecutor
import socket
import time
import tempfile
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
from network_safety import safe_external_get

DEFAULT_FEEDS = [{'name': 'DupeFR EBOOKS', 'url': 'https://dupefr.com/flux_rss/section/EBOOKS', 'enabled': True}]
MAX_FEEDS = 20
MAX_FEED_BYTES = 2 * 1024 * 1024
RSS_CACHE_TTL_SECONDS = 300
_rss_cache = {}


def _validate_url(url):
    parsed = urlparse((url or '').strip())
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        raise ValueError('URL RSS invalide (HTTP ou HTTPS requis)')
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)
        addresses = {info[4][0] for info in infos}
        if any(ipaddress.ip_address(address).is_private or ipaddress.ip_address(address).is_loopback or ipaddress.ip_address(address).is_link_local for address in addresses):
            raise ValueError('Les adresses RSS internes ne sont pas autorisées')
    except socket.gaierror as exc:
        raise ValueError('Hôte RSS introuvable') from exc
    return parsed.geturl()


def normalize_feeds(value):
    feeds = []
    seen = set()
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        if item.get('provider') == 'prowlarr':
            key = ('provider', 'prowlarr')
            if key in seen:
                continue
            seen.add(key)
            feeds.append({'name': str(item.get('name') or 'Prowlarr').strip()[:120], 'provider': 'prowlarr', 'enabled': item.get('enabled', True) is not False})
            continue
        url = _validate_url(item.get('url', ''))
        if url in seen:
            continue
        seen.add(url)
        feeds.append({'name': str(item.get('name') or urlparse(url).hostname or 'RSS').strip()[:120], 'url': url, 'enabled': item.get('enabled', True) is not False})
        if len(feeds) >= MAX_FEEDS:
            break
    return feeds


def load_feeds(path):
    try:
        with open(path, encoding='utf-8') as handle:
            return normalize_feeds(json.load(handle).get('feeds', []))
    except FileNotFoundError:
        return normalize_feeds(DEFAULT_FEEDS)
    except (ValueError, json.JSONDecodeError):
        return []


def save_feeds(path, feeds):
    normalized = normalize_feeds(feeds)
    directory = os.path.dirname(path) or '.'
    fd, temporary = tempfile.mkstemp(prefix='.rss-feeds-', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump({'feeds': normalized}, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return normalized


def _text(parent, names):
    for name in names:
        value = next((node.text for node in parent if node.tag.rsplit('}', 1)[-1] == name), None)
        if value and value.strip():
            return value.strip()
    return ''


def _is_download_link(url, label=''):
    probe = f'{url} {label}'.lower()
    return any(token in probe for token in ('torrent', 'ebdz', '.torrent', 'magnet:', 'ed2k://', '/download'))


def _clean_link(url, source_url):
    url = html.unescape((url or '').strip()).rstrip(chr(34) + chr(39))
    if not url:
        return ''
    if url.startswith(('http://', 'https://', 'magnet:', 'ed2k://')):
        return url
    return urljoin(source_url, url)


def _download_links(description, source_url):
    text = html.unescape(description or '')
    found = []
    seen = set()
    anchor_re = re.compile(r"<a\b[^>]*\bhref=['\"]([^'\"]+)['\"][^>]*>(.*?)</a\s*>", re.I | re.S)
    for url, label_html in anchor_re.findall(text):
        label = re.sub(r'<[^>]+>', ' ', label_html)
        url = _clean_link(url, source_url)
        if _is_download_link(url, label) and url not in seen:
            seen.add(url)
            found.append({'url': url, 'label': re.sub(r'\s+', ' ', label).strip() or 'Télécharger'})
    for url in re.findall(r'(?:(?:https?|magnet|ed2k)://[^\s<>]+)', text, re.I):
        url = _clean_link(url, source_url)
        if _is_download_link(url) and url not in seen:
            seen.add(url)
            found.append({'url': url, 'label': 'Télécharger'})
    return found[:10]


def _entry_links(entry, description, source_url):
    page_candidates = []
    downloads = _download_links(description, source_url)
    seen_downloads = {item['url'] for item in downloads}
    comments_link = ''
    guid_link = ''
    for node in entry.iter():
        name = node.tag.rsplit('}', 1)[-1].lower()
        if name == 'comments' and (node.text or '').strip():
            comments_link = _clean_link(node.text, source_url)
            continue
        if name == 'guid' and (node.text or '').strip():
            candidate = _clean_link(node.text, source_url)
            if node.attrib.get('isPermaLink', 'true').lower() != 'false' and not _is_download_link(candidate):
                guid_link = candidate
            continue
        if name == 'link':
            candidate = _clean_link(node.attrib.get('href') or node.text, source_url)
            rel = (node.attrib.get('rel') or '').lower()
            label = node.attrib.get('type') or rel
            if not candidate:
                continue
            if rel in {'enclosure', 'download'} or _is_download_link(candidate, label):
                if candidate not in seen_downloads:
                    seen_downloads.add(candidate)
                    downloads.append({'url': candidate, 'label': 'Télécharger'})
            else:
                page_candidates.append(candidate)
            continue
        if name in {'enclosure', 'content', 'download', 'downloadurl', 'torrent', 'torrenturl', 'magneturi', 'ed2k'}:
            candidate = _clean_link(node.attrib.get('url') or node.attrib.get('href') or node.text, source_url)
            if candidate and candidate not in seen_downloads:
                seen_downloads.add(candidate)
                downloads.append({'url': candidate, 'label': 'Télécharger'})
    page_link = comments_link or (page_candidates[0] if page_candidates else '') or guid_link or source_url
    return page_link, comments_link, downloads[:10]


def parse_feed(payload, source_url):
    _validate_url(source_url)
    root = ET.fromstring(payload)
    channel = root.find('channel')
    entries = list(channel.findall('item')) if channel is not None else []
    if not entries:
        entries = [node for node in root.iter() if node.tag.rsplit('}', 1)[-1] == 'entry']
    results = []
    feed_title = _text(channel, ['title']) if channel is not None else ''
    for entry in entries:
        title = _text(entry, ['title'])
        raw_description = _text(entry, ['description', 'summary', 'content'])
        page_link, comments_link, download_links = _entry_links(entry, raw_description, source_url)
        date = _text(entry, ['pubDate', 'published', 'updated'])
        try:
            parsed_date = email.utils.parsedate_to_datetime(date) if date else None
        except (ValueError, TypeError, OverflowError):
            parsed_date = None
        if parsed_date is None:
            try:
                parsed_date = datetime.fromisoformat(date.replace('Z', '+00:00')) if date else None
            except ValueError:
                parsed_date = None
        if parsed_date and parsed_date.tzinfo is None:
            parsed_date = parsed_date.replace(tzinfo=timezone.utc)
        results.append({'title': title or 'Sans titre', 'link': page_link, 'page_link': page_link, 'comments_link': comments_link, 'description': raw_description, 'download_links': download_links, 'date': parsed_date.astimezone(timezone.utc).isoformat() if parsed_date else '', 'feed_title': feed_title})
    return results


def _redact_prowlarr_url(url, source_url, api_key):
    parsed = urlparse(url or '')
    if not parsed.scheme:
        return url or '', False
    from urllib.parse import parse_qsl, urlencode
    query = []
    had_key = False
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in {'apikey', 'api_key', 'x-api-key'}:
            had_key = True
            continue
        query.append((key, value))
    redacted = parsed._replace(query=urlencode(query, doseq=True)).geturl()
    if api_key and api_key in redacted:
        redacted = redacted.replace(api_key, '[REDACTED]')
        had_key = True
    return redacted, had_key


def sanitize_prowlarr_entries(entries, source_url, api_key):
    sanitized = []
    prowlarr_host = urlparse(source_url).netloc
    for original in entries:
        item = copy.deepcopy(original)
        item['description'] = re.sub(r'(?i)(apikey|api_key|x-api-key)=([^&\s]+)', r'\1=[REDACTED]', item.get('description') or '')
        for field in ('link', 'page_link', 'comments_link'):
            value, _ = _redact_prowlarr_url(item.get(field), source_url, api_key)
            item[field] = value
        clean_downloads = []
        for download in item.get('download_links') or []:
            value, had_key = _redact_prowlarr_url(download.get('url'), source_url, api_key)
            if had_key and urlparse(value).netloc == prowlarr_host:
                continue
            clean_downloads.append({**download, 'url': value})
        item['download_links'] = clean_downloads[:10]
        sanitized.append(item)
    return sanitized


def prowlarr_search_endpoint(indexer, base_url, category_ids=None):
    params = {'t': 'search', 'q': '', 'limit': 100}
    selected = [str(value) for value in (category_ids or []) if str(value).isdigit()]
    if selected:
        params['cat'] = ','.join(selected)
    return f"{base_url.rstrip('/')}/api/v1/indexer/{int(indexer['id'])}/newznab?{urlencode(params)}"


def _fetch_prowlarr_indexer(indexer, base_url, api_key, category_ids=None):
    endpoint = prowlarr_search_endpoint(indexer, base_url, category_ids)
    request = Request(endpoint, headers={'X-Api-Key': api_key, 'Accept': 'application/rss+xml, application/xml, text/xml'})
    with urlopen(request, timeout=20) as response:
        payload = response.read(MAX_FEED_BYTES + 1)
    if len(payload) > MAX_FEED_BYTES:
        raise ValueError('Flux Prowlarr trop volumineux')
    entries = parse_feed(payload, endpoint)
    for entry in entries:
        entry['feed_title'] = f"Prowlarr · {indexer.get('name') or indexer['id']}"
        entry['prowlarr_indexer_id'] = indexer['id']
    return sanitize_prowlarr_entries(entries, endpoint, api_key)


def prowlarr_rss_indexers(indexers, feed):
    selected = feed.get("indexer_ids")
    if selected is None:
        return indexers
    selected_ids = {str(value) for value in selected}
    return [item for item in indexers if str(item.get("id")) in selected_ids]


def fetch_prowlarr_feed(_feed):
    # La sélection Oui/Non est stockée avec la configuration Prowlarr, pas dans
    # l'URL RSS publique. La lire dans le contexte Flask garde la clé et le choix
    # côté serveur tout en laissant les tests/harnesses fournir indexer_ids.
    if _feed.get('indexer_ids') is None:
        try:
            from flask import has_app_context, current_app
            if has_app_context():
                from blueprints.prowlarr.config_store import load_prowlarr_config
                configured = load_prowlarr_config()
                configured_ids = configured.get('rss_indexers')
                if configured_ids is not None:
                    _feed = {**_feed, 'indexer_ids': configured_ids, 'category_ids': configured.get('selected_categories') or {}}
        except Exception:
            pass
    base_url = (os.environ.get('PROWLARR_URL') or '').strip().rstrip('/')
    api_key = (os.environ.get('PROWLARR_API_KEY') or '').strip()
    if not base_url or not api_key:
        raise ValueError('Prowlarr non configuré côté serveur')
    indexers_request = Request(f"{base_url}/api/v1/indexer", headers={'X-Api-Key': api_key, 'Accept': 'application/json'})
    with urlopen(indexers_request, timeout=20) as response:
        indexers = json.loads(response.read(MAX_FEED_BYTES))
    torrent_indexers = [item for item in prowlarr_rss_indexers(indexers, _feed) if isinstance(item, dict) and item.get('id') and item.get('protocol') == 'torrent']
    category_map = _feed.get('category_ids') or {}
    results = []
    errors = []
    with ThreadPoolExecutor(max_workers=min(5, max(1, len(torrent_indexers)))) as executor:
        futures = [executor.submit(_fetch_prowlarr_indexer, indexer, base_url, api_key, category_map.get(str(indexer.get('id')), [])) for indexer in torrent_indexers]
        for indexer, future in zip(torrent_indexers, futures):
            try:
                results.extend(future.result())
            except Exception as exc:
                errors.append(f"{indexer.get('name') or indexer.get('id')}: {exc}")
    if errors and not results:
        raise RuntimeError('Prowlarr: ' + '; '.join(errors[:3]))
    return results


def feed_cache_key(feed):
    key = feed.get("url") or f"provider:{feed.get('provider', 'unknown')}:{feed.get('name', 'unnamed')}"
    if feed.get('provider') == 'prowlarr' and feed.get('indexer_ids') is not None:
        selected = ','.join(sorted(str(value) for value in feed.get('indexer_ids') or []))
        key = f"{key}:indexers:{selected}"
        categories = feed.get('category_ids')
        if categories is not None:
            category_key = '|'.join(
                f"{indexer}:{','.join(sorted(str(value) for value in values or []))}"
                for indexer, values in sorted(categories.items(), key=lambda item: str(item[0]))
            )
            key = f"{key}:categories:{category_key}"
    return key


def fetch_feed(feed, force_refresh=False):
    if feed.get('provider') == 'prowlarr':
        return fetch_prowlarr_feed(feed)
    url = _validate_url(feed['url'])
    cached = _rss_cache.get(url)
    now = time.monotonic()
    if not force_refresh and cached and now - cached[0] < RSS_CACHE_TTL_SECONDS:
        return cached[1]

    headers = {'User-Agent': 'Bullarr RSS reader/1.0', 'Accept': 'application/rss+xml, application/xml, text/xml'}
    for attempt in range(3):
        with safe_external_get(url, timeout=10, max_bytes=MAX_FEED_BYTES, headers=headers) as response:
            if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                retry_after = response.headers.get('Retry-After')
                try:
                    delay = min(5, max(1, int(retry_after))) if retry_after else 2 ** attempt
                except (TypeError, ValueError):
                    delay = 2 ** attempt
                time.sleep(delay)
                continue
            response.raise_for_status()
            entries = parse_feed(response.content, url)
            _rss_cache[url] = (time.monotonic(), entries)
            return entries

    raise RuntimeError('Échec de lecture du flux RSS')
