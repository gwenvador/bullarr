import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import requests

from blueprints.bedetheque import guard as guard_module
from blueprints.bedetheque import origins
from blueprints.bedetheque.guard import (
    BedethequeBlockedError, BedethequeBudgetError, BedethequeGuard, BedethequeGuardError, GuardedSession)

PAGE = 'https://www.bedetheque.com/serie-1-BD-Test.html'
IMAGE = 'https://www.bedetheque.com/cache/thb_couv/Couv_1.jpg'


class FakeTime:
    """Horloge et sommeil factices : le sommeil fait avancer le temps."""

    def __init__(self):
        self.now = 1_000_000.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.time = FakeTime()
        self.guard = BedethequeGuard(
            db_path=os.path.join(self.tmp.name, 'guard.db'), config_path=os.path.join(self.tmp.name, 'guard.json'),
            clock=self.time.clock, monotonic=self.time.clock, sleep=self.time.sleep, jitter=lambda: 0.0)

    def test_two_requests_are_spaced_by_the_minimum_interval_whatever_the_thread(self):
        self.guard.before_request(PAGE)
        self.guard.before_request(PAGE)
        self.guard.before_request(PAGE)
        self.assertEqual(len(self.time.sleeps), 2)
        for slept in self.time.sleeps:
            self.assertAlmostEqual(slept, 1.5, places=2)

    def test_other_hosts_are_not_guarded_or_counted(self):
        self.guard.before_request('https://example.org/x')
        self.guard.after_response('https://example.org/x', 429, {})
        self.assertEqual(self.time.sleeps, [])
        self.assertEqual(self.guard.status()['calls_last_24h'], 0)
        self.assertFalse(self.guard.status()['blocked'])

    def test_429_pauses_and_each_new_pause_doubles(self):
        self.guard.after_response(PAGE, 429, {'Retry-After': '120'})
        with self.assertRaises(BedethequeBlockedError):
            self.guard.before_request(PAGE)
        status = self.guard.status()
        self.assertTrue(status['blocked'])
        self.assertAlmostEqual(status['blocked_until'] - self.time.now, 30 * 60, delta=2)
        self.time.now += 31 * 60
        self.guard.before_request(PAGE)  # pause finie
        self.guard.after_response(PAGE, 429, {})
        self.assertAlmostEqual(self.guard.status()['blocked_until'] - self.time.now, 60 * 60, delta=2)

    def test_success_after_a_pause_resets_the_doubling(self):
        self.guard.after_response(PAGE, 429, {})
        self.time.now += 31 * 60
        self.guard.after_response(PAGE, 200, {})
        self.guard.after_response(PAGE, 429, {})
        self.assertAlmostEqual(self.guard.status()['blocked_until'] - self.time.now, 30 * 60, delta=2)

    def test_forbidden_pauses_only_when_repeated_on_pages_never_for_images(self):
        self.guard.after_response(PAGE, 403, {})
        self.assertFalse(self.guard.status()['blocked'])
        self.guard.after_response(IMAGE, 403, {})
        self.guard.after_response(IMAGE, 403, {})
        self.assertFalse(self.guard.status()['blocked'])
        self.time.now += 30
        self.guard.after_response(PAGE, 403, {})
        self.assertTrue(self.guard.status()['blocked'])

    def test_a_single_forbidden_long_ago_does_not_count(self):
        self.guard.after_response(PAGE, 403, {})
        self.time.now += guard_module.FORBIDDEN_WINDOW_SECONDS + 5
        self.guard.after_response(PAGE, 403, {})
        self.assertFalse(self.guard.status()['blocked'])

    def test_cloudflare_challenge_pauses_at_once(self):
        self.guard.after_response(PAGE, 403, {'cf-mitigated': 'challenge'})
        self.assertTrue(self.guard.status()['blocked'])

    def test_repeated_failures_pause_and_one_success_resets_the_count(self):
        for _ in range(guard_module.CONSECUTIVE_FAILURES_BEFORE_PAUSE - 1):
            self.guard.after_response(PAGE, 503, {})
        self.guard.after_response(PAGE, 200, {})
        for _ in range(guard_module.CONSECUTIVE_FAILURES_BEFORE_PAUSE - 1):
            self.guard.after_exception(PAGE, requests.exceptions.ConnectionError('x'))
        self.assertFalse(self.guard.status()['blocked'])
        self.guard.after_exception(PAGE, requests.exceptions.Timeout('x'))
        self.assertTrue(self.guard.status()['blocked'])

    def test_daily_limit_blocks_until_old_calls_leave_the_window(self):
        self.guard.save_config({'daily_limit': 50})
        for _ in range(50):
            self.guard.after_response(PAGE, 200, {})
        with self.assertRaises(BedethequeBudgetError):
            self.guard.before_request(PAGE)
        self.time.now += 86400 + 5
        self.guard.before_request(PAGE)

    def test_counters_config_bounds_and_reset(self):
        self.guard.after_response(PAGE, 200, {})
        self.time.now += 4000
        self.guard.after_response(PAGE, 200, {})
        status = self.guard.status()
        self.assertEqual((status['calls_last_hour'], status['calls_last_24h']), (1, 2))
        config = self.guard.save_config({'min_interval_seconds': 0.01, 'daily_limit': 10**9, 'block_pause_minutes': 'x'})
        self.assertEqual((config['min_interval_seconds'], config['daily_limit'], config['block_pause_minutes']), (0.5, 50000, 30))
        self.guard.after_response(PAGE, 429, {})
        self.guard.reset_block()
        self.assertFalse(self.guard.status()['blocked'])
        self.assertFalse(self.guard.is_limited())

    def test_guard_errors_are_network_errors_so_existing_handlers_keep_working(self):
        self.assertTrue(issubclass(BedethequeBlockedError, requests.exceptions.RequestException))
        self.assertTrue(issubclass(BedethequeBudgetError, BedethequeGuardError))


class GuardedSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.time = FakeTime()
        self.guard = BedethequeGuard(
            db_path=os.path.join(self.tmp.name, 'guard.db'), config_path=os.path.join(self.tmp.name, 'guard.json'),
            clock=self.time.clock, monotonic=self.time.clock, sleep=self.time.sleep, jitter=lambda: 0.0)
        patcher = patch.object(guard_module, '_guard', self.guard)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _response(self, status=200, headers=None):
        return MagicMock(status_code=status, headers=headers or {})

    def test_guarded_hosts_are_counted_and_paced_other_hosts_pass_through(self):
        with patch.object(requests.Session, 'request', return_value=self._response()) as send:
            session = GuardedSession()
            session.get(PAGE)
            session.get(PAGE)
            session.get('https://example.org/')
        self.assertEqual(send.call_count, 3)
        self.assertEqual(self.guard.status()['calls_last_24h'], 2)
        self.assertEqual(len(self.time.sleeps), 1)

    def test_blocking_response_pauses_the_next_calls_without_sending_them(self):
        with patch.object(requests.Session, 'request', return_value=self._response(429, {'Retry-After': '60'})) as send:
            session = GuardedSession()
            session.get(PAGE)
            with self.assertRaises(BedethequeBlockedError):
                session.get(PAGE)
        self.assertEqual(send.call_count, 1)

    def test_network_errors_are_recorded_and_reraised(self):
        with patch.object(requests.Session, 'request', side_effect=requests.exceptions.ConnectionError('boom')):
            with self.assertRaises(requests.exceptions.ConnectionError):
                GuardedSession().get(PAGE)
        self.assertEqual(self.guard.status()['consecutive_failures'], 1)


class OriginsSavingCallsTest(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        sqlite3.connect(self.db).executescript(
            "CREATE TABLE series (id INTEGER PRIMARY KEY, bedetheque_url TEXT, bedetheque_origin TEXT);")
        origins._recent_fetches.clear()
        self.addCleanup(lambda: os.remove(self.db))
        self.addCleanup(origins._recent_fetches.clear)
        self.urls = [f'https://www.bedetheque.com/serie-{n}-BD-Test.html' for n in range(1, 8)]

    def _page(self, label='Manga'):
        return f'<li title="Origine de la série"><i class="bdt-ori"></i> {label}</li>'

    def test_origin_read_for_another_reason_is_stored_and_not_fetched_again(self):
        self.assertTrue(origins.store_origin(self.db, self.urls[0], 'Franco-Belge'))
        calls = []
        result, pending = origins.resolve_origins(self.db, self.urls[:1], lambda u: calls.append(u) or self._page())
        self.assertEqual((result[self.urls[0]], pending, calls), ('Europe', 0, []))
        self.assertFalse(origins.store_origin(self.db, 'https://example.org/x', 'Manga'))
        self.assertFalse(origins.store_origin(self.db, self.urls[1], ''))

    def test_hourly_budget_caps_the_pages_read(self):
        with patch.object(origins, 'ORIGIN_FETCH_INTERVAL_SECONDS', 0), patch.object(origins, 'ORIGIN_FETCH_MAX_PER_HOUR', 3):
            calls = []
            _, pending = origins.resolve_origins(self.db, self.urls, lambda u: calls.append(u) or self._page(), fetch_limit=6)
            self.assertEqual((len(calls), pending), (3, 4))
            self.assertTrue(origins.origin_fetch_limited())
            more = []
            _, pending = origins.resolve_origins(self.db, self.urls, lambda u: more.append(u) or self._page(), fetch_limit=6)
            self.assertEqual((len(more), pending), (0, 4))

    def test_guard_refusal_stops_reading_and_caches_nothing(self):
        def refused(url):
            raise BedethequeBlockedError('pause')
        with patch.object(origins, 'ORIGIN_FETCH_INTERVAL_SECONDS', 0):
            result, pending = origins.resolve_origins(self.db, self.urls, refused)
        self.assertEqual((result, pending), ({}, 7))
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM bedetheque_origin_cache').fetchone()[0], 0)

    def test_unreadable_page_is_retried_after_a_month_not_a_week(self):
        self.assertEqual(origins.ORIGIN_RETRY_DAYS, 30)
        with patch.object(origins, 'ORIGIN_FETCH_INTERVAL_SECONDS', 0):
            calls = []
            origins.resolve_origins(self.db, self.urls[:1], lambda u: calls.append(u) or '<html></html>')
            conn = sqlite3.connect(self.db)
            conn.execute('UPDATE bedetheque_origin_cache SET fetched_at = ?', (time.time() - 8 * 86400,))
            conn.commit()
            conn.close()
            origins.resolve_origins(self.db, self.urls[:1], lambda u: calls.append(u) or '<html></html>')
            self.assertEqual(len(calls), 1)
            conn = sqlite3.connect(self.db)
            conn.execute('UPDATE bedetheque_origin_cache SET fetched_at = ?', (time.time() - 31 * 86400,))
            conn.commit()
            conn.close()
            origins.resolve_origins(self.db, self.urls[:1], lambda u: calls.append(u) or self._page())
            self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()
