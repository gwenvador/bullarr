"""Mises à jour disponibles: dernières releases GitHub de Bullarr et de son application Android.

Lecture de l'API publique de GitHub (sans compte), mise en cache 6 h: la page Configuration n'appelle GitHub qu'au plus
quelques fois par jour, et un échec réseau n'empêche jamais l'affichage (la dernière valeur connue est conservée).
Bullarr ne se met pas à jour tout seul (image Docker): le bouton mène à la page de la release."""
import json
import re
import threading
import time

from network_safety import safe_external_get

CACHE_SECONDS = 6 * 3600
RELEASES = {
    'bullarr': 'https://api.github.com/repos/gwenvador/bullarr/releases/latest',
    'android': 'https://api.github.com/repos/gwenvador/bullarr-android/releases/latest',
}
_cache = {}
_lock = threading.Lock()


def version_tuple(value):
    """« v9.1 » -> (9, 1); () si le texte ne contient aucun numéro."""
    return tuple(int(part) for part in re.findall(r'\d+', str(value or '').split('-')[0]))


def is_newer(latest, current):
    latest_tuple, current_tuple = version_tuple(latest), version_tuple(current)
    return bool(latest_tuple) and latest_tuple > current_tuple


def _fetch_release(url):
    response = safe_external_get(url, timeout=10, max_bytes=2 * 1024 * 1024,
                                 headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'Bullarr'})
    response.raise_for_status()
    data = json.loads(response.content)
    apk = next((asset.get('browser_download_url') for asset in data.get('assets') or []
                if str(asset.get('name') or '').lower().endswith('.apk')), None)
    return {'tag': data.get('tag_name') or '', 'url': data.get('html_url') or '',
            'published_at': data.get('published_at'), 'apk_url': apk}


def latest_release(name, now=None):
    """Dernière release publiée (cache 6 h), ou la dernière valeur connue si GitHub ne répond pas."""
    now = time.time() if now is None else now
    with _lock:
        cached = _cache.get(name)
        if cached and now - cached['at'] < CACHE_SECONDS:
            return cached['value']
    try:
        value = _fetch_release(RELEASES[name])
    except Exception:
        with _lock:
            stale = _cache.get(name)
            if stale:
                stale['at'] = now - CACHE_SECONDS + 600  # réessai dans 10 minutes
                return stale['value']
        return None
    with _lock:
        _cache[name] = {'at': now, 'value': value}
    return value


def update_status(current_version):
    bullarr = latest_release('bullarr')
    android = latest_release('android')
    return {
        'current': current_version,
        'bullarr': None if not bullarr else {
            'latest': bullarr['tag'].lstrip('v'), 'url': bullarr['url'],
            'update_available': is_newer(bullarr['tag'], current_version)},
        'android': None if not android else {
            'latest': android['tag'].lstrip('v'), 'url': android['url'], 'apk_url': android['apk_url']},
    }
