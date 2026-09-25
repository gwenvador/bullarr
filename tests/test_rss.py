import unittest
from blueprints.settings.rss import parse_feed

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

if __name__ == '__main__':
    unittest.main()
