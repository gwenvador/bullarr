import unittest
from pathlib import Path
from unittest import mock

from blueprints.bedetheque import scraper as scraper_module
from blueprints.bedetheque.scraper import BedethequeScraper

FIXTURE = Path(__file__).with_name('fixtures_author_biblio.html')


class _Response:
    status_code = 200

    def __init__(self, content):
        self.content = content
        self.encoding = None


class AuthorBibliographyLayoutTest(unittest.TestCase):
    def parse(self, html):
        s = BedethequeScraper.__new__(BedethequeScraper)
        s.base_url = 'https://www.bedetheque.com'
        s._ensure_session = lambda: None
        s.session = mock.Mock()
        s.session.get.return_value = _Response(html.encode('utf-8'))
        with mock.patch.object(scraper_module, '_anti_bot_delay'), \
                mock.patch.object(scraper_module, '_safe_bedetheque_url', side_effect=lambda u: u):
            return s.get_author_bibliography('https://www.bedetheque.com/auteur-21250-BD-Fournier-Fabien.html')

    def test_current_layout_returns_main_series_only(self):
        results = self.parse(FIXTURE.read_text(encoding='utf-8'))
        self.assertEqual(
            [r['title'] for r in results],
            ['NOOB', 'Noob Reroll', 'Néogicia', 'Néogicia : Arc - Les Origines de Tabris'],
        )
        noob = results[0]
        self.assertEqual(noob['bedetheque_url'], 'https://www.bedetheque.com/serie-24038-BD-NOOB.html')
        self.assertEqual((noob['year_start'], noob['year_end']), (2010, 2022))
        self.assertTrue(noob['is_french'])
        self.assertEqual(noob['flag_country'], 'France')

    def test_legacy_layout_still_parsed(self):
        html = '''<table class="biblio-auteur"><tbody><tr>
            <td><span class="ico"><img src="x/Italy.png"></span><span class="serie"><a href="/serie-1-BD-X.html">X</a></span></td>
            <td>1990</td><td>1995</td></tr></tbody></table>'''
        results = self.parse(html)
        self.assertEqual(len(results), 1)
        self.assertEqual((results[0]['year_start'], results[0]['year_end']), (1990, 1995))
        self.assertFalse(results[0]['is_french'])
        self.assertEqual(results[0]['flag_country'], 'Italy')


if __name__ == '__main__':
    unittest.main()
