"""Configurable RSS feeds used by the Nouveautés page."""
import email.utils
import html
import re
import ipaddress
import json
import socket
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

DEFAULT_FEEDS = [{'name': 'DupeFR EBOOKS', 'url': 'https://dupefr.com/flux_rss/section/EBOOKS', 'enabled': True}]
MAX_FEEDS = 20
MAX_FEED_BYTES = 2 * 1024 * 1024


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


def _download_links(description, source_url):
    text = html.unescape(description or '')
    found = []
    seen = set()
    anchor_re = re.compile(r'<a[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a\s*>', re.I | re.S)
    for url, label_html in anchor_re.findall(text):
        label = re.sub(r'<[^>]+>', ' ', label_html)
        probe = f'{url} {label}'.lower()
        if any(token in probe for token in ('torrent', 'ebdz', '.torrent', 'magnet:', 'ed2k://', '/download')):
            if url not in seen:
                seen.add(url)
                found.append({'url': url, 'label': re.sub(r'\s+', ' ', label).strip() or 'Télécharger'})
    for url in re.findall(r'(?:(?:https?|magnet|ed2k)://[^\s<>"\']+)', text, re.I):
        probe = url.lower()
        if any(token in probe for token in ('torrent', 'ebdz', '.torrent', 'magnet:', 'ed2k://', '/download')) and url not in seen:
            seen.add(url)
            found.append({'url': url, 'label': 'Télécharger'})
    return found[:10]


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
        link = _text(entry, ['link'])
        if not link:
            for candidate in entry.findall('{*}link'):
                link = candidate.attrib.get('href', '')
                if link:
                    break
        date = _text(entry, ['pubDate', 'published', 'updated'])
        parsed_date = email.utils.parsedate_to_datetime(date) if date else None
        if parsed_date is None:
            try:
                parsed_date = datetime.fromisoformat(date.replace('Z', '+00:00')) if date else None
            except ValueError:
                parsed_date = None
        if parsed_date and parsed_date.tzinfo is None:
            parsed_date = parsed_date.replace(tzinfo=timezone.utc)
        description = _text(entry, ['description', 'summary', 'content'])
        results.append({'title': title or 'Sans titre', 'link': link or source_url, 'description': description, 'download_links': _download_links(description, source_url), 'date': parsed_date.astimezone(timezone.utc).isoformat() if parsed_date else '', 'feed_title': feed_title})
    return results


def fetch_feed(feed):
    url = _validate_url(feed['url'])
    request = Request(url, headers={'User-Agent': 'Bullarr RSS reader/1.0', 'Accept': 'application/rss+xml, application/atom+xml, application/xml, text/xml'})
    with urlopen(request, timeout=15) as response:
        payload = response.read(MAX_FEED_BYTES + 1)
    if len(payload) > MAX_FEED_BYTES:
        raise ValueError('Flux RSS trop volumineux')
    return parse_feed(payload, url)
