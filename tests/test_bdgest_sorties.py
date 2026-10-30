import unittest
from pathlib import Path

from bs4 import BeautifulSoup

from blueprints.bedetheque.parsers import parse_bdgest_sorties

ROOT = Path(__file__).resolve().parents[1]

PAGE = """
<section class="bdg-cal-day" id="j2110"><h3><span class="d">Mercredi 21 octobre</span></h3>
<ol class="bdg-cal-list">
 <li data-album="543573"><a class="cv bdt-img" href="https://www.bedetheque.com/BD-Zouk-Tome-25-543573.html"><img src="https://www.bedetheque.com/cache/thb_couv/Couv_543573.jpg"></a>
  <div class="t"><a href="https://www.bedetheque.com/BD-Zouk-Tome-25-543573.html" title="La musique">Zouk <em>#25</em></a>
  <small class="ti">La musique, c&#039;est magique !</small><small>Hubesch / Bloch</small></div><span class="ed">Bayard</span></li>
 <li data-album="543561"><a class="cv" href="https://www.bedetheque.com/BD-Illiberal-543561.html"></a>
  <div class="t"><a href="https://www.bedetheque.com/BD-Illiberal-543561.html">Illibéral !</a><small>Alani</small></div><span class="ed">Albin Michel</span></li>
</ol></section>
<section class="bdg-cal-day" id="j0211"><ol class="bdg-cal-list">
 <li><div class="t"><a href="https://www.bedetheque.com/BD-X-1.html">X</a></div></li></ol></section>
"""


class BdgestSortiesParserTest(unittest.TestCase):
    def test_parses_days_titles_volumes_and_publishers(self):
        items = parse_bdgest_sorties(BeautifulSoup(PAGE, 'html.parser'), 2026)
        self.assertEqual([i['date'] for i in items], ['2026-10-21', '2026-10-21', '2026-11-02'])
        zouk = items[0]
        self.assertEqual((zouk['title'], zouk['volume_label'], zouk['publisher']), ('Zouk', '#25', 'Bayard'))
        self.assertEqual(zouk['subtitle'], "La musique, c'est magique !")
        self.assertEqual(zouk['authors'], 'Hubesch / Bloch')
        self.assertEqual(zouk['url'], 'https://www.bedetheque.com/BD-Zouk-Tome-25-543573.html')
        self.assertTrue(zouk['cover'].endswith('Couv_543573.jpg'))
        self.assertEqual(items[1]['authors'], 'Alani')
        self.assertIsNone(items[1]['cover'])


class EmptySeriesHiddenTest(unittest.TestCase):
    def test_library_grid_skips_series_without_albums(self):
        script = (ROOT / 'static/js/library.js').read_text()
        body = script[script.index('function filterSeries()'):script.index('function sortSeriesList(')]
        self.assertIn('seriesHasAlbums(s)', body)
        helper = script[script.index('function seriesHasAlbums('):script.index('function filterSeries()')]
        for field in ('owned_tomes', 'owned_integrals', 'owned_hs', 'owned_episodes', 'owned_specials'):
            self.assertIn(field, helper)


if __name__ == '__main__':
    unittest.main()
