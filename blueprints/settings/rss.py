"""Configurable RSS feeds used by the Nouveautés page."""
import email.utils
import html
import re
import ipaddress
import json
import socket
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import xml.etree.ElementTree as ET

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
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump({'feeds': normalized}, handle, indent=2, ensure_ascii=False)
    return normalized


def _text(parent, names):
    for name in names:
        value = parent.findtext(name)
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
        parsed_date = email.utils.parsedate_to_datetime(date) if date else None
        if parsed_date is None:
            try:
                parsed_date = datetime.fromisoformat(date.replace('Z', '+00:00')) if date else None
            except ValueError:
                parsed_date = None
        if parsed_date and parsed_date.tzinfo is None:
            parsed_date = parsed_date.replace(tzinfo=timezone.utc)
        results.append({'title': title or 'Sans titre', 'link': page_link, 'page_link': page_link, 'comments_link': comments_link, 'description': raw_description, 'download_links': download_links, 'date': parsed_date.astimezone(timezone.utc).isoformat() if parsed_date else '', 'feed_title': feed_title})
    return results


def fetch_feed(feed):
    url = _validate_url(feed['url'])
    cached = _rss_cache.get(url)
    now = time.monotonic()
    if cached and now - cached[0] < RSS_CACHE_TTL_SECONDS:
        return cached[1]

    request = Request(url, headers={'User-Agent': 'Bullarr RSS reader/1.0', 'Accept': 'application/rss+xml, application/xml, text/xml'})
    last_error = None
    for attempt in range(3):
        try:
            with urlopen(request, timeout=15) as response:
                payload = response.read(MAX_FEED_BYTES + 1)
            if len(payload) > MAX_FEED_BYTES:
                raise ValueError('Flux RSS trop volumineux')
            entries = parse_feed(payload, url)
            _rss_cache[url] = (time.monotonic(), entries)
            return entries
        except HTTPError as exc:
            last_error = exc
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                break
            retry_after = exc.headers.get('Retry-After') if exc.headers else None
            try:
                delay = min(5, max(1, int(retry_after))) if retry_after else 2 ** attempt
            except (TypeError, ValueError):
                delay = 2 ** attempt
            time.sleep(delay)

    if cached:
        return cached[1]
    if last_error:
        raise last_error
    raise RuntimeError('Échec de lecture du flux RSS')
