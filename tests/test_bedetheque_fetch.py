"""The Bédéthèque scraper must keep its session without following unsafe links."""
import unittest
from unittest.mock import Mock

from blueprints.bedetheque.routes import _fetch_bedetheque_page


class FakeResponse:
    def __init__(self, status=200, chunks=(b'<html>ok</html>',), headers=None):
        self.status_code = status
        self.chunks = chunks
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError('HTTP failure')

    def iter_content(self, chunk_size):
        return iter(self.chunks)


class BedethequeFetchTests(unittest.TestCase):
    def test_uses_existing_session_and_disables_redirects(self):
        scraper = Mock()
        scraper.session.get.return_value = FakeResponse()
        page = _fetch_bedetheque_page(scraper, 'https://www.bedetheque.com/theme-BD-humour-noir.html')
        self.assertEqual(page, b'<html>ok</html>')
        scraper.session.get.assert_called_once_with(
            'https://www.bedetheque.com/theme-BD-humour-noir.html',
            timeout=20, stream=True, allow_redirects=False,
        )

    def test_rejects_other_hosts_before_network(self):
        scraper = Mock()
        with self.assertRaises(ValueError):
            _fetch_bedetheque_page(scraper, 'https://127.0.0.1/private')
        scraper.session.get.assert_not_called()

    def test_rejects_redirect_and_oversized_response(self):
        scraper = Mock()
        scraper.session.get.return_value = FakeResponse(status=302, headers={'Location': 'http://127.0.0.1'})
        with self.assertRaises(ValueError):
            _fetch_bedetheque_page(scraper, 'https://www.bedetheque.com/theme')
        scraper.session.get.return_value = FakeResponse(chunks=(b'abc', b'def'))
        with self.assertRaises(ValueError):
            _fetch_bedetheque_page(scraper, 'https://www.bedetheque.com/theme', max_bytes=5)


if __name__ == '__main__':
    unittest.main()
