import unittest
from unittest.mock import Mock, patch

from blueprints.bedetheque.scraper import BedethequeScraper


class BedethequeAlbumUrlTest(unittest.TestCase):
    def test_album_url_is_resolved_before_series_fetch(self):
        album_url = 'https://www.bedetheque.com/BD-AUT-Juillard-Tome-13-Pele-Mele-Monographie-26901.html'
        series_url = 'https://www.bedetheque.com/serie-123-BD-Juillard.html'
        response = Mock(status_code=200, content=b'<div class="bandeau-info serie"><h1><a>Juillard</a></h1></div>')
        scraper = BedethequeScraper()

        with patch.object(scraper, 'get_series_url_from_album_url', return_value=series_url) as resolve, \
             patch.object(scraper.session, 'get', return_value=response) as fetch, \
             patch('blueprints.bedetheque.scraper._anti_bot_delay'):
            info = scraper.get_series_info(album_url)

        resolve.assert_called_once_with(album_url)
        fetch.assert_called_once_with(series_url.replace('.html', '__10000.html'), timeout=15)
        self.assertEqual(info['url'], series_url)
        self.assertEqual(info['title'], 'Juillard')


if __name__ == '__main__':
    unittest.main()
