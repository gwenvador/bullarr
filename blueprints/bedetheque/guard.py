"""
Garde des appels vers Bédéthèque et BDGest : un seul limiteur pour TOUS les fils d'exécution.

Avant ce module chaque tâche créait son propre client et ne s'imposait qu'un délai local
(_anti_bot_delay) : deux ou trois tâches simultanées (import, mise à jour des métadonnées,
Nouveautés) pouvaient frapper le site plusieurs fois par seconde, et rien ne s'arrêtait si le
site se mettait à refuser les requêtes. La garde apporte :

- un écart minimum partagé entre deux requêtes, quel que soit le fil qui les émet ;
- une limite glissante sur 24 h (par défaut 2000 requêtes, réglable) ;
- une pause automatique quand le site bloque (429, défi Cloudflare, deux 403 de suite, ou
  plusieurs erreurs de suite), de 30 min doublant à chaque nouvelle pause jusqu'à 4 h ;
- des compteurs (dernière heure / 24 h) et l'état de la pause, consultables dans la configuration.

Les erreurs levées héritent de requests.exceptions.RequestException : les appelants qui gèrent déjà
une erreur réseau (ils sont nombreux) continuent de fonctionner sans changement.
"""
import json
import logging
import os
import random
import sqlite3
import threading
import time
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

GUARD_DB = './data/bedetheque_guard.db'
GUARD_CONFIG = './data/bedetheque_guard.json'
DEFAULTS = {'min_interval_seconds': 1.5, 'daily_limit': 2000, 'block_pause_minutes': 30}
GUARDED_HOST_SUFFIXES = ('bedetheque.com', 'bdgest.com')
MAX_PAUSE_MINUTES = 240
CONSECUTIVE_FAILURES_BEFORE_PAUSE = 5
FORBIDDEN_WINDOW_SECONDS = 600
_IMAGE_SUFFIXES = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.ico')


class BedethequeGuardError(requests.exceptions.RequestException):
    """Appel refusé par la garde (pas de requête envoyée)."""


class BedethequeBlockedError(BedethequeGuardError):
    """Pause en cours : le site a bloqué les requêtes."""


class BedethequeBudgetError(BedethequeGuardError):
    """Limite des 24 h atteinte."""


def _clamp(value, low, high, default):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(value, low), high)


class BedethequeGuard:
    def __init__(self, db_path=GUARD_DB, config_path=GUARD_CONFIG, clock=time.time,
                 monotonic=time.monotonic, sleep=time.sleep, jitter=random.random):
        self.db_path = db_path
        self.config_path = config_path
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._jitter = jitter
        self._slot_lock = threading.Lock()
        self._next_slot = 0.0
        self._state_lock = threading.Lock()
        self._config_cache = (0.0, None)
        self._initialised = False

    # ---- configuration -------------------------------------------------------------------
    def config(self):
        loaded_at, cached = self._config_cache
        now = self._monotonic()
        if cached is not None and now - loaded_at < 15:
            return cached
        raw = {}
        try:
            with open(self.config_path, encoding='utf-8') as handle:
                raw = json.load(handle)
            if not isinstance(raw, dict):
                raw = {}
        except (OSError, ValueError):
            raw = {}
        config = {
            'min_interval_seconds': _clamp(raw.get('min_interval_seconds'), 0.5, 30, DEFAULTS['min_interval_seconds']),
            'daily_limit': int(_clamp(raw.get('daily_limit'), 50, 50000, DEFAULTS['daily_limit'])),
            'block_pause_minutes': int(_clamp(raw.get('block_pause_minutes'), 5, MAX_PAUSE_MINUTES, DEFAULTS['block_pause_minutes'])),
        }
        self._config_cache = (now, config)
        return config

    def save_config(self, values):
        current = self.config()
        merged = {
            'min_interval_seconds': _clamp(values.get('min_interval_seconds', current['min_interval_seconds']), 0.5, 30, current['min_interval_seconds']),
            'daily_limit': int(_clamp(values.get('daily_limit', current['daily_limit']), 50, 50000, current['daily_limit'])),
            'block_pause_minutes': int(_clamp(values.get('block_pause_minutes', current['block_pause_minutes']), 5, MAX_PAUSE_MINUTES, current['block_pause_minutes'])),
        }
        directory = os.path.dirname(self.config_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temp = self.config_path + '.tmp'
        with open(temp, 'w', encoding='utf-8') as handle:
            json.dump(merged, handle, indent=2)
        os.replace(temp, self.config_path)
        self._config_cache = (0.0, None)
        return self.config()

    # ---- stockage ------------------------------------------------------------------------
    def _connect(self):
        directory = os.path.dirname(self.db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        if not self._initialised:
            conn.execute('CREATE TABLE IF NOT EXISTS calls (ts REAL NOT NULL, host TEXT, status INTEGER)')
            conn.execute('CREATE INDEX IF NOT EXISTS calls_ts ON calls (ts)')
            conn.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)')
            conn.commit()
            self._initialised = True
        return conn

    def _get_state(self, conn, key, default=None):
        row = conn.execute('SELECT value FROM state WHERE key = ?', (key,)).fetchone()
        return row[0] if row else default

    def _set_state(self, conn, **values):
        for key, value in values.items():
            conn.execute('INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)', (key, str(value)))

    # ---- hôtes concernés -----------------------------------------------------------------
    @staticmethod
    def is_guarded(url):
        host = (urlparse(str(url or '')).hostname or '').lower()
        return any(host == suffix or host.endswith('.' + suffix) for suffix in GUARDED_HOST_SUFFIXES)

    @staticmethod
    def _is_image(url):
        return urlparse(str(url or '')).path.lower().endswith(_IMAGE_SUFFIXES)

    # ---- avant la requête ----------------------------------------------------------------
    def before_request(self, url):
        """Lève BedethequeBlockedError / BedethequeBudgetError, sinon attend son tour."""
        if not self.is_guarded(url):
            return
        config = self.config()
        now = self._clock()
        with self._state_lock:
            conn = self._connect()
            try:
                blocked_until = float(self._get_state(conn, 'blocked_until', 0) or 0)
                if blocked_until > now:
                    reason = self._get_state(conn, 'blocked_reason', 'blocage du site')
                    until = time.strftime('%H:%M', time.localtime(blocked_until))
                    raise BedethequeBlockedError(f'Bédéthèque en pause jusqu\'à {until} ({reason})')
                used = conn.execute('SELECT COUNT(*) FROM calls WHERE ts > ?', (now - 86400,)).fetchone()[0]
            finally:
                conn.close()
        if used >= config['daily_limit']:
            raise BedethequeBudgetError(
                f"Limite de {config['daily_limit']} requêtes par 24 h vers Bédéthèque atteinte (réglable)")
        with self._slot_lock:
            wait = self._next_slot - self._monotonic()
            if wait > 0:
                self._sleep(wait)
            interval = config['min_interval_seconds']
            self._next_slot = self._monotonic() + interval + self._jitter() * interval * 0.3

    # ---- après la requête ----------------------------------------------------------------
    def after_response(self, url, status, headers=None):
        if not self.is_guarded(url):
            return
        headers = headers or {}
        now = self._clock()
        with self._state_lock:
            conn = self._connect()
            try:
                conn.execute('INSERT INTO calls (ts, host, status) VALUES (?, ?, ?)',
                             (now, (urlparse(url).hostname or '').lower(), int(status)))
                conn.execute('DELETE FROM calls WHERE ts < ?', (now - 8 * 86400,))
                retry_after = self._retry_after_seconds(headers)
                challenged = str(headers.get('cf-mitigated', '')).lower() == 'challenge'
                if status == 429 or challenged:
                    self._trip(conn, now, f'HTTP {status}' + (' (défi Cloudflare)' if challenged else ''), retry_after)
                elif status == 403 and not self._is_image(url):
                    last = float(self._get_state(conn, 'last_403', 0) or 0)
                    if now - last <= FORBIDDEN_WINDOW_SECONDS:
                        self._trip(conn, now, 'HTTP 403 répétés', retry_after)
                    else:
                        self._set_state(conn, last_403=now)
                elif status in (500, 502, 503, 504):
                    self._count_failure(conn, now, f'HTTP {status}', retry_after)
                elif status < 400 or status == 404:
                    self._set_state(conn, consecutive_failures=0)
                    if status < 400 and float(self._get_state(conn, 'blocked_until', 0) or 0) <= now:
                        self._set_state(conn, trips=0)
                conn.commit()
            finally:
                conn.close()

    def after_exception(self, url, exc):
        if not self.is_guarded(url):
            return
        now = self._clock()
        with self._state_lock:
            conn = self._connect()
            try:
                conn.execute('INSERT INTO calls (ts, host, status) VALUES (?, ?, 0)',
                             (now, (urlparse(url).hostname or '').lower()))
                self._count_failure(conn, now, f'erreurs réseau ({exc.__class__.__name__})', None)
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _retry_after_seconds(headers):
        try:
            return max(0.0, float(headers.get('Retry-After', 0) or 0))
        except (TypeError, ValueError):
            return 0.0

    def _count_failure(self, conn, now, reason, retry_after):
        failures = int(self._get_state(conn, 'consecutive_failures', 0) or 0) + 1
        self._set_state(conn, consecutive_failures=failures)
        if failures >= CONSECUTIVE_FAILURES_BEFORE_PAUSE:
            self._trip(conn, now, f'{failures} échecs de suite : {reason}', retry_after)

    def _trip(self, conn, now, reason, retry_after):
        trips = int(self._get_state(conn, 'trips', 0) or 0) + 1
        base = self.config()['block_pause_minutes']
        pause = min(base * (2 ** (trips - 1)), MAX_PAUSE_MINUTES) * 60
        if retry_after:
            pause = max(pause, min(retry_after, MAX_PAUSE_MINUTES * 60))
        self._set_state(conn, trips=trips, blocked_until=now + pause, blocked_reason=reason,
                        consecutive_failures=0, last_403=0)
        logger.warning('Bédéthèque : pause de %d min (%s)', round(pause / 60), reason)

    # ---- état et réglages ----------------------------------------------------------------
    def status(self):
        config = self.config()
        now = self._clock()
        with self._state_lock:
            conn = self._connect()
            try:
                hour = conn.execute('SELECT COUNT(*) FROM calls WHERE ts > ?', (now - 3600,)).fetchone()[0]
                day = conn.execute('SELECT COUNT(*) FROM calls WHERE ts > ?', (now - 86400,)).fetchone()[0]
                blocked_until = float(self._get_state(conn, 'blocked_until', 0) or 0)
                reason = self._get_state(conn, 'blocked_reason', '')
                failures = int(self._get_state(conn, 'consecutive_failures', 0) or 0)
            finally:
                conn.close()
        blocked = blocked_until > now
        return {
            'calls_last_hour': hour,
            'calls_last_24h': day,
            'daily_limit': config['daily_limit'],
            'min_interval_seconds': config['min_interval_seconds'],
            'block_pause_minutes': config['block_pause_minutes'],
            'blocked': blocked,
            'blocked_until': blocked_until if blocked else None,
            'blocked_reason': reason if blocked else '',
            'consecutive_failures': failures,
        }

    def is_limited(self):
        """True si un appel serait refusé (pause en cours ou limite des 24 h atteinte)."""
        status = self.status()
        return bool(status['blocked'] or status['calls_last_24h'] >= status['daily_limit'])

    def blocked_message(self):
        """Message « en pause jusqu'à HH:MM (raison) » si la garde refuse les appels, sinon None."""
        status = self.status()
        if not status['blocked']:
            return None
        until = time.strftime('%H:%M', time.localtime(status['blocked_until']))
        return f"Bédéthèque en pause jusqu'à {until} ({status['blocked_reason']})"

    def reset_block(self):
        with self._state_lock:
            conn = self._connect()
            try:
                self._set_state(conn, blocked_until=0, blocked_reason='', trips=0,
                                consecutive_failures=0, last_403=0)
                conn.commit()
            finally:
                conn.close()


class GuardedSession(requests.Session):
    """Session requests dont chaque appel vers Bédéthèque / BDGest passe par la garde."""

    def request(self, method, url, *args, **kwargs):
        guard = get_guard()
        if not guard.is_guarded(url):
            return super().request(method, url, *args, **kwargs)
        guard.before_request(url)
        try:
            response = super().request(method, url, *args, **kwargs)
        except requests.exceptions.RequestException as exc:
            guard.after_exception(url, exc)
            raise
        guard.after_response(url, response.status_code, response.headers)
        return response


_guard = None
_guard_lock = threading.Lock()


def get_guard():
    global _guard
    if _guard is None:
        with _guard_lock:
            if _guard is None:
                _guard = BedethequeGuard()
    return _guard
