import unittest
from datetime import datetime, timedelta
from unittest import mock

from blueprints.missing_monitor import request_throttler
from blueprints.missing_monitor.request_throttler import RequestThrottler, SearchResultCache


class RequestThrottlerTest(unittest.TestCase):
    def test_interval_is_derived_from_the_requests_per_minute(self):
        self.assertEqual(RequestThrottler(requests_per_minute=30).min_interval, 2.0)
        self.assertEqual(RequestThrottler().min_interval, 10.0)

    def test_second_request_to_the_same_source_waits_for_the_remaining_interval(self):
        throttler = RequestThrottler(requests_per_minute=60)                  # 1 s entre requêtes
        with mock.patch.object(request_throttler.time, 'time', side_effect=[100.0, 100.0, 100.25, 101.0]), \
                mock.patch.object(request_throttler.time, 'sleep') as sleep:
            throttler.wait_if_needed('prowlarr')
            sleep.assert_not_called()
            throttler.wait_if_needed('prowlarr')
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args[0][0], 0.75)

    def test_sources_are_throttled_independently_and_enough_time_means_no_wait(self):
        throttler = RequestThrottler(requests_per_minute=60)
        with mock.patch.object(request_throttler.time, 'time', side_effect=[100.0, 100.0, 100.1, 100.1, 105.0, 105.0]), \
                mock.patch.object(request_throttler.time, 'sleep') as sleep:
            throttler.wait_if_needed('prowlarr')
            throttler.wait_if_needed('ebdz')           # autre source: aucune attente
            throttler.wait_if_needed('prowlarr')       # 5 s plus tard: aucune attente
        sleep.assert_not_called()


class SearchResultCacheTest(unittest.TestCase):
    def test_keys_are_case_insensitive_and_distinguish_thread_and_label(self):
        cache = SearchResultCache()
        self.assertEqual(cache.generate_key('ebdz', 'Thorgal', 5), 'ebdz:thorgal:vol5')
        self.assertEqual(cache.generate_key('ebdz', 'Thorgal', 5, thread_id=12), 'ebdz:thorgal:vol5:t12')
        self.assertEqual(cache.generate_key('ebdz', 'Thorgal', 5, label='Intégrale 6'), 'ebdz:thorgal:vol5:intégrale 6')
        self.assertNotEqual(cache.generate_key('ebdz', 'Thorgal', 5), cache.generate_key('prowlarr', 'Thorgal', 5))

    def test_stored_results_are_returned_until_they_expire(self):
        cache = SearchResultCache(cache_duration_minutes=60)
        self.assertIsNone(cache.get('missing'))
        cache.set('k', [{'title': 'A'}])
        self.assertEqual(cache.get('k'), [{'title': 'A'}])
        cache.cache['k'] = ([{'title': 'A'}], datetime.now() - timedelta(minutes=61))
        self.assertIsNone(cache.get('k'))
        self.assertNotIn('k', cache.cache)            # l'entrée expirée est supprimée

    def test_stats_and_clear(self):
        cache = SearchResultCache()
        cache.set('a', [1])
        cache.set('b', [2, 3])
        stats = cache.stats()
        self.assertEqual(stats['total_entries'], 2)
        self.assertGreater(stats['cache_size_bytes'], 0)
        cache.clear()
        self.assertEqual(cache.stats()['total_entries'], 0)


if __name__ == '__main__':
    unittest.main()
