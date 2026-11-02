"""The current Bédéthèque series layout must produce an actionable series."""
import unittest
from unittest.mock import Mock, patch

from blueprints.bedetheque.scraper import BedethequeScraper


class BedethequeSeriesLayoutTests(unittest.TestCase):
    def test_current_layout_extracts_title_and_numbered_albums(self):
        page = '''<section class="bdt-ah bdt-ah--serie">
          <div class="bdt-ah-top"><h1><a href="/serie-48266-BD-Stern.html">Stern</a></h1></div>
          <div class="bdt-ah-by"><span class="bdt-sh-genres"><a class="bdt-sh-genre">Western</a></span>
            <b class="bdt-sh-parution">Série en cours</b> 2015-2026</div>
          <ul class="bdt-ah-pastilles"><li title="Nombre de tomes de la série">6 tomes</li>
            <li title="Origine de la série">Europe</li>
            <li title="Langue de parution">Français</li></ul>
        </section>
        <p class="bdt-sh-resume">Résumé de Stern.</p>
        <div class="bdt-ah-cover"><div class="bdt-sh-tomes"><img src="/cover.jpg"></div></div>
        <article class="bdt-edition" itemtype="https://schema.org/Book">
          <a name="251298"></a>
          <div class="bdt-edition-head"><h3><a itemprop="url" href="/BD-Stern-Tome-1-251298.html">
            <span itemprop="name">1<span class="bdt-numa"></span>. Le Croque-mort</span></a></h3></div>
          <div class="bdt-ecov"><img itemprop="image" src="/t1.jpg"></div>
          <dl class="bdt-sheet"><dt>Scénario</dt><dd><a href="/auteur-1"><span itemprop="author">Maffre, Frédéric</span></a></dd>
            <dt>Dessin</dt><dd><a href="/auteur-2"><span itemprop="illustrator">Maffre, Julien</span></a></dd>
            <dt>Éditeur</dt><dd><span itemprop="publisher">Dargaud</span></dd>
            <dt>EAN/ISBN</dt><dd><span itemprop="isbn">9782205073171</span></dd>
            <dt>Planches</dt><dd><span itemprop="numberOfPages">62</span></dd></dl>
          <meta itemprop="datePublished" content="2015-08-26">
        </article>
        <article class="bdt-edition" itemtype="https://schema.org/Book">
          <a name="294813"></a>
          <div class="bdt-edition-head"><h3><a itemprop="url" href="/BD-Stern-Tome-2-294813.html">
            <span itemprop="name">2<span class="bdt-numa"></span>. La Cit&eacute; des sauvages</span></a></h3></div>
        </article>'''.encode('utf-8')
        scraper = BedethequeScraper()
        response = Mock(status_code=200, content=page)
        with patch.object(scraper.session, 'get', return_value=response), \
             patch.object(scraper, '_download_cover', return_value='covers/stern.jpg'), \
             patch('blueprints.bedetheque.scraper._anti_bot_delay'):
            info = scraper.get_series_info('https://www.bedetheque.com/serie-48266-BD-Stern.html')

        self.assertEqual(info['title'], 'Stern')
        self.assertEqual(info['total_volumes'], 6)
        self.assertEqual(info['genre'], 'Western')
        self.assertEqual(info['year_start'], 2015)
        self.assertEqual(info['year_end'], 2026)
        self.assertEqual([v['number'] for v in info['volumes']], [1, 2])
        self.assertEqual(info['volumes'][0]['scenario'], 'Frédéric Maffre')
        self.assertEqual(info['volumes'][0]['dessin'], 'Julien Maffre')
        self.assertEqual(info['volumes'][0]['pages'], '62')


if __name__ == '__main__':
    unittest.main()
