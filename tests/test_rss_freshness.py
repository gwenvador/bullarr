"""Behavioral regressions for Nouveautés RSS freshness and cache isolation."""
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from blueprints.settings import rss
from blueprints.ebdz import routes


class RssFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'rss_entries_cache.json')
        self.feed = {'name': 'Demo', 'url': 'https://example.test/rss'}
        self.key = rss.feed_cache_key(self.feed)
        patcher = patch.object(routes, '_rss_library_fingerprint', return_value='fp')
        patcher.start()
        self.addCleanup(patcher.stop)

    def seed(self, entries, fetched_at=1):
        routes._update_rss_persistent_cache(self.path, lambda c: c.update({
            self.key: {'entries': entries, 'fetched_at': fetched_at}}))

    def test_expired_cache_returns_fresh_data_in_same_request(self):
        self.seed([{'title': 'Old'}])
        with patch.object(routes, 'fetch_feed', return_value=[{'title': 'New'}]) as fetch:
            _, result, error = routes._read_rss_feed(self.feed, self.path)
        self.assertIsNone(error)
        self.assertEqual(result['entries'][0]['title'], 'New')
        fetch.assert_called_once_with(self.feed, force_refresh=True)

    def test_explicit_refresh_bypasses_fresh_persistent_cache(self):
        self.seed([{'title': 'Old'}], time.time())
        with patch.object(routes, 'fetch_feed', return_value=[{'title': 'New'}]):
            _, result, _ = routes._read_rss_feed(self.feed, self.path, True)
        self.assertEqual(result['entries'][0]['title'], 'New')

    def test_refresh_merges_only_new_entries_and_preserves_old_annotations(self):
        old = {'title': 'Old', 'link': 'https://example.test/old', 'date': '2026-09-28'}
        annotated_old = {**old, 'series_id': 7,
                         '_rss_annotation_version': routes.RSS_ANNOTATION_VERSION,
                         '_library_fingerprint': 'fp'}
        routes._update_rss_persistent_cache(self.path, lambda c: c.update({
            self.key: {'entries': [old], 'annotated_entries': [annotated_old],
                       'fetched_at': 1}}))
        fresh = {'title': 'New', 'link': 'https://example.test/new', 'date': '2026-09-29'}
        with patch.object(routes, 'fetch_feed', return_value=[fresh, old]):
            snapshot = routes._refresh_rss_persistent_feed(self.feed, self.path)
        self.assertEqual([e['title'] for e in snapshot['entries']], ['New', 'Old'])
        self.assertEqual([e['title'] for e in snapshot['annotated_entries']], ['Old'])
        self.assertEqual(snapshot['annotated_entries'][0]['series_id'], 7)

    def test_failed_refresh_preserves_old_timestamp_and_reports_error(self):
        self.seed([{'title': 'Old'}])
        with patch.object(routes, 'fetch_feed', side_effect=RuntimeError('secret-url')):
            _, result, error = routes._read_rss_feed(self.feed, self.path)
        self.assertEqual(result['fetched_at'], 1)
        self.assertEqual(result['entries'][0]['title'], 'Old')
        self.assertTrue(error)
        self.assertNotIn('secret-url', error)

    def test_concurrent_feed_refreshes_preserve_both_feeds(self):
        barrier = threading.Barrier(2)
        other = {'name': 'Other', 'url': 'https://other.test/rss'}
        def fetch(feed, **kw):
            barrier.wait(timeout=5)
            return [{'title': feed['name']}]
        with patch.object(routes, 'fetch_feed', side_effect=fetch):
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda feed: routes._read_rss_feed(feed, self.path), [self.feed, other]))
        cache = routes._load_rss_persistent_cache(self.path)
        self.assertEqual(set(cache), {self.key, rss.feed_cache_key(other)})

    def call_latest(self, snapshots, annotate=None, query='limit=1'):
        app = Flask(__name__)
        app.config['RSS_CONFIG_FILE'] = 'unused'
        app.config['LIBRARY_IMPORT_CONFIG_FILE'] = str(Path(self.tmp.name) / 'import_config.json')
        app.config['LIBRARY_IMPORT_CONFIG'] = {'blocked_search_extensions': []}
        feeds = [row[0] for row in snapshots]
        with app.test_request_context('/rss/latest?' + query), \
                patch.object(routes, 'load_feeds', return_value=feeds), \
                patch.object(routes, '_rss_persistent_cache_path', return_value=self.path), \
                patch.object(routes, '_read_rss_feed', side_effect=lambda f, *a: next(row for row in snapshots if row[0] == f)), \
                patch.object(routes, '_annotate_rss_entries', side_effect=annotate or (lambda entries: entries)):
            return routes.rss_latest().get_json()

    def test_limit_is_per_feed_even_with_identical_names(self):
        other = {'name': 'Demo', 'url': 'https://other.test/rss'}
        rows = [(self.feed, {'entries': [{'title': 'New', 'date': '2026-09-29'}, {'title': 'Also new', 'date': '2026-09-28'}]}, None),
                (other, {'entries': [{'title': 'Older feed', 'date': '2026-09-01'}]}, None)]
        result = self.call_latest(rows)
        self.assertEqual([e['title'] for e in result['entries']], ['New', 'Older feed'])

    def test_disabled_extensions_are_hidden_before_limit_and_use_current_settings(self):
        entries = [
            {'title': 'Newest.EPUB', 'date': '2026-09-30'},
            {'title': 'Another release', 'filename': 'another.epub', 'date': '2026-09-29'},
            {'title': 'Allowed.cbz', 'date': '2026-09-28'},
        ]
        snapshot = {'entries': entries, 'fetched_at': 1}
        settings = Path(self.tmp.name) / 'import_config.json'
        settings.write_text(json.dumps({'blocked_search_extensions': ['epub']}))
        result = self.call_latest([(self.feed, snapshot, None)])
        self.assertEqual([entry['title'] for entry in result['entries']], ['Allowed.cbz'])

        settings.write_text(json.dumps({'blocked_search_extensions': []}))
        result = self.call_latest([(self.feed, snapshot, None)])
        self.assertEqual([entry['title'] for entry in result['entries']], ['Newest.EPUB'])

    def test_annotation_write_does_not_restore_a_stale_snapshot(self):
        self.seed([{'title': 'Old'}])
        old = routes._load_rss_persistent_cache(self.path)[self.key]
        def annotate(entries):
            self.seed([{'title': 'Fresh'}], 2)
            return entries
        self.call_latest([(self.feed, old, None)], annotate)
        cached = routes._load_rss_persistent_cache(self.path)[self.key]
        self.assertEqual(cached['entries'][0]['title'], 'Fresh')
        self.assertNotIn('annotated_entries', cached)

    def test_only_requested_and_unseen_entries_are_annotated(self):
        old = {'title': 'Old', 'link': 'https://example.test/old', 'date': '2026-09-28'}
        annotated_old = {**old, 'series_id': 7,
                         '_rss_annotation_version': routes.RSS_ANNOTATION_VERSION,
                         '_library_fingerprint': 'fp'}
        new = {'title': 'New', 'link': 'https://example.test/new', 'date': '2026-09-29'}
        outside_limit = {'title': 'Outside', 'link': 'https://example.test/outside',
                         'date': '2026-09-27'}
        snapshot = {'entries': [new, old, outside_limit],
                    'annotated_entries': [annotated_old], 'fetched_at': 1}
        annotated_calls = []

        def annotate(entries):
            annotated_calls.append(entries)
            return [{**entry, 'series_id': 9,
                     '_rss_annotation_version': routes.RSS_ANNOTATION_VERSION,
                         '_library_fingerprint': 'fp'}
                    for entry in entries]

        result = self.call_latest([(self.feed, snapshot, None)], annotate, 'limit=2')
        self.assertEqual([[e['title'] for e in call] for call in annotated_calls], [['New']])
        self.assertEqual([e['title'] for e in result['entries']], ['New', 'Old'])
        self.assertEqual([e['series_id'] for e in result['entries']], [9, 7])

    def test_iso_and_malformed_dates_do_not_discard_feed(self):
        payload = b'<rss><channel><item><title>A</title><pubDate>2026-09-29T08:00:00Z</pubDate></item><item><title>B</title><pubDate>invalid</pubDate></item></channel></rss>'
        with patch.object(rss, '_validate_url', side_effect=lambda url: url):
            entries = rss.parse_feed(payload, self.feed['url'])
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]['date'], '2026-09-29T08:00:00+00:00')
        self.assertEqual(entries[1]['date'], '')

    def test_atom_namespace_fields(self):
        payload = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Recent</title><updated>2026-09-29T08:00:00Z</updated><summary>Details</summary><link href="https://example.test/item"/></entry></feed>'
        with patch.object(rss, '_validate_url', side_effect=lambda url: url):
            entry = rss.parse_feed(payload, self.feed['url'])[0]
        self.assertEqual(entry['title'], 'Recent')
        self.assertEqual(entry['description'], 'Details')
        self.assertEqual(entry['date'], '2026-09-29T08:00:00+00:00')

    def test_force_refresh_bypasses_memory_cache(self):
        rss._rss_cache[self.feed['url']] = (time.monotonic(), [{'title': 'Old'}])
        self.addCleanup(rss._rss_cache.clear)
        with patch.object(rss, '_validate_url', side_effect=lambda url: url), \
                patch.object(rss, 'safe_external_get') as opened:
            response = opened.return_value.__enter__.return_value
            response.status_code = 200
            response.content = b'<rss><channel><item><title>Fresh</title></item></channel></rss>'
            entries = rss.fetch_feed(self.feed, force_refresh=True)
        self.assertEqual(entries[0]['title'], 'Fresh')


if __name__ == '__main__':
    unittest.main()
