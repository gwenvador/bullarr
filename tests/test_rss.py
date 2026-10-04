import unittest
from pathlib import Path
from blueprints.settings.rss import normalize_feeds, parse_feed, sanitize_prowlarr_entries

class RssParserTests(unittest.TestCase):
    def test_parses_rss_items_and_atom_links(self):
        payload = b'''<?xml version="1.0"?><rss><channel><title>DupeFR</title><item><title>Livre A</title><link>https://dupefr.com/a</link><pubDate>Tue, 24 Sep 2026 12:00:00 GMT</pubDate><description><![CDATA[Desc]]></description></item></channel></rss>'''
        self.assertEqual(parse_feed(payload, "https://dupefr.com/feed")[0]["title"], "Livre A")
        self.assertEqual(parse_feed(payload, "https://dupefr.com/feed")[0]["link"], "https://dupefr.com/a")

    def test_uses_comments_as_page_and_extracts_download_links(self):
        payload = b'''<rss><channel><item><title>Demo</title><comments>https://example/comments/1</comments><link>https://example/demo.torrent</link><description><![CDATA[<a href="https://example/ebdz/download/1">EBDZ</a>]]></description><enclosure url="https://example/extra.torrent"/></item></channel></rss>'''
        item = parse_feed(payload, "https://dupefr.com/feed")[0]
        self.assertEqual(item["link"], "https://example/comments/1")
        self.assertEqual([link["url"] for link in item["download_links"]], [
            "https://example/ebdz/download/1",
            "https://example/demo.torrent",
            "https://example/extra.torrent",
        ])

    def test_rejects_non_http_feed_url(self):
        with self.assertRaises(ValueError):
            parse_feed(b'<rss/>', 'file:///etc/passwd')

    def test_prowlarr_search_endpoint_applies_selected_categories(self):
        from blueprints.settings.rss import prowlarr_search_endpoint

        url = prowlarr_search_endpoint({"id": 17}, "https://prowlarr.test", [7000, 7030])
        self.assertIn("cat=7000%2C7030", url)

    def test_prowlarr_feed_can_limit_indexers_explicitly(self):
        from blueprints.settings.rss import prowlarr_rss_indexers

        indexers = [{"id": 1, "name": "One"}, {"id": 2, "name": "Two"}]
        self.assertEqual(prowlarr_rss_indexers(indexers, {"indexer_ids": [2]}), [indexers[1]])

    def test_preserves_server_managed_prowlarr_feed_when_rss_settings_are_saved(self):
        from blueprints.settings.routes import preserve_server_managed_feeds

        existing = {"name": "Prowlarr", "provider": "prowlarr", "enabled": True}
        submitted = [{"name": "DupeFR", "url": "https://example.test/feed", "enabled": True}]
        result = preserve_server_managed_feeds([existing], submitted)
        self.assertEqual([feed.get("provider") for feed in result], [None, "prowlarr"])

    def test_provider_cache_key_changes_when_rss_indexers_change(self):
        from blueprints.settings.rss import feed_cache_key

        base = {"provider": "prowlarr", "name": "Prowlarr"}
        self.assertNotEqual(
            feed_cache_key({**base, "indexer_ids": [1]}),
            feed_cache_key({**base, "indexer_ids": [2]}),
        )

    def test_provider_cache_key_changes_when_categories_change(self):
        from blueprints.settings.rss import feed_cache_key

        base = {"provider": "prowlarr", "name": "Prowlarr", "indexer_ids": [4]}
        self.assertNotEqual(
            feed_cache_key({**base, "category_ids": {"4": [7000]}}),
            feed_cache_key({**base, "category_ids": {"4": [100003]}}),
        )

    def test_provider_feed_has_a_stable_cache_key_without_url(self):
        from blueprints.settings.rss import feed_cache_key

        self.assertEqual(feed_cache_key({"provider": "prowlarr", "name": "Prowlarr"}), "provider:prowlarr:Prowlarr")

    def test_background_refresh_uses_provider_cache_key_without_url(self):
        import json
        import tempfile
        from unittest.mock import patch
        from blueprints.ebdz.routes import _refresh_rss_persistent_feed

        feed = {"provider": "prowlarr", "name": "Prowlarr"}
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "rss_entries_cache.json")
            with patch("blueprints.ebdz.routes.fetch_feed", return_value=[{"title": "Fresh"}]):
                _refresh_rss_persistent_feed(feed, path)
            cache = json.loads(Path(path).read_text())
        self.assertEqual(cache["provider:prowlarr:Prowlarr"]["entries"][0]["title"], "Fresh")

    def test_accepts_server_side_prowlarr_feed_without_exposing_credentials(self):
        feeds = normalize_feeds([{'name': 'Prowlarr', 'provider': 'prowlarr', 'enabled': True}])
        self.assertEqual(feeds, [{'name': 'Prowlarr', 'provider': 'prowlarr', 'enabled': True}])

        entries = sanitize_prowlarr_entries([{'title': 'X', 'link': 'http://prowlarr/17/download?apikey=secret', 'description': 'apikey=secret', 'download_links': [{'url': 'http://prowlarr/17/download?apikey=secret', 'label': 'Torrent'}]}], 'http://prowlarr/17/newznab', 'secret')
        self.assertEqual(entries[0]['download_links'], [])
        self.assertNotIn('secret', entries[0]['description'])
        self.assertNotIn('apikey=', entries[0]['link'])

if __name__ == '__main__':
    unittest.main()

class ProwlarrIndexerRssUiTest(unittest.TestCase):
    def test_indexer_checkbox_updates_row_style_immediately(self):
        js = (Path(__file__).resolve().parents[1] / "static/js/settings.js").read_text()
        self.assertIn("toggleProwlarrIndexerActive(this)", js)
        self.assertIn("row.style.opacity", js)

    def test_inactive_indexer_rows_are_visually_muted(self):
        js = (Path(__file__).resolve().parents[1] / "static/js/settings.js").read_text()
        self.assertIn("indexer.selected ?", js)
        self.assertIn("opacity", js)

    def test_indexer_settings_expose_rss_yes_no_and_submit_selection(self):
        js = (Path(__file__).resolve().parents[1] / 'static/js/settings.js').read_text()
        routes = (Path(__file__).resolve().parents[1] / 'blueprints/prowlarr/routes.py').read_text()
        self.assertIn('rss-indexer-radio', js)
        self.assertIn('rss_indexers', js)
        self.assertIn('rss_indexers', routes)
