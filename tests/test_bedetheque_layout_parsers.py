import unittest
from pathlib import Path
from unittest import mock

from bs4 import BeautifulSoup

from blueprints.bedetheque import parsers, scraper as scraper_module
from blueprints.bedetheque.scraper import BedethequeScraper

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'bedetheque'


def soup(name):
    return BeautifulSoup((FIXTURES / f'{name}.html').read_text(encoding='utf-8'), 'html.parser')


class _Response:
    status_code = 200

    def __init__(self, html):
        self.content = html.encode('utf-8')
        self.encoding = None


def make_scraper(html):
    s = BedethequeScraper.__new__(BedethequeScraper)
    s.base_url = 'https://www.bedetheque.com'
    s.csrf_token = 'token'
    s._ensure_session = lambda: None
    s.session = mock.Mock()
    s.session.get.return_value = _Response(html)
    return s


class SearchTest(unittest.TestCase):
    def test_series_names_keep_highlighted_words_and_genre(self):
        results = parsers.parse_search_links(soup('search_series'), 'serie')
        self.assertEqual([r['name'] for r in results], ["Journal d'un noob", 'NOOB', 'Noob Reroll', 'Noobz!'])
        self.assertEqual(results[1]['meta'], 'Humour')
        self.assertTrue(results[1]['href'].endswith('serie-24038-BD-NOOB.html'))

    def test_author_names_do_not_include_country(self):
        results = parsers.parse_search_links(soup('search_authors'), 'auteur')
        self.assertEqual(results[0]['name'], 'Alain-Fournier')
        self.assertEqual(results[1]['name'], 'Fournier, Adrien')
        self.assertEqual(results[0]['meta'], 'FRANCE')
        self.assertTrue(all('FRANCE' not in r['name'] for r in results))

    def test_scraper_series_search_end_to_end(self):
        s = make_scraper((FIXTURES / 'search_series.html').read_text(encoding='utf-8'))
        with mock.patch.object(scraper_module, '_anti_bot_delay'):
            results = s._search_series_raw('noob')
        self.assertEqual(results[1], {
            'title': 'NOOB', 'url': 'https://www.bedetheque.com/serie-24038-BD-NOOB.html', 'genre': 'Humour',
        })

    def test_scraper_author_search_end_to_end_respects_limit(self):
        s = make_scraper((FIXTURES / 'search_authors.html').read_text(encoding='utf-8'))
        with mock.patch.object(scraper_module, '_anti_bot_delay'):
            results = s.search_authors('Fournier', limit=3)
        self.assertEqual([r['name'] for r in results], ['Alain-Fournier', 'Fournier, Adrien', 'Fournier, Alexandre'])


class ReviewsTest(unittest.TestCase):
    def test_reviews_parsed_with_author_date_rating_and_line_breaks(self):
        reviews = parsers.parse_album_reviews(soup('album_reviews'))
        self.assertEqual(len(reviews), 7)
        first = reviews[0]
        self.assertEqual((first['author'], first['date'], first['rating']), ('Lecteur 1', '2022-01-11', 5))
        self.assertEqual(first['text'], 'Premier paragraphe de l avis fictif 1.\n\nSecond paragraphe de l avis fictif 1.')
        self.assertIn('\n\n', first['text'])
        self.assertEqual(reviews[1]['rating'], 3)

    def test_scraper_reviews_end_to_end(self):
        s = make_scraper((FIXTURES / 'album_reviews.html').read_text(encoding='utf-8'))
        with mock.patch.object(scraper_module, '_anti_bot_delay'):
            self.assertEqual(len(s.get_album_reviews('https://www.bedetheque.com/x.html')), 7)


class ListingsTest(unittest.TestCase):
    def test_indispensables_podium_then_ranking_with_genres(self):
        items = parsers.parse_indispensables(soup('indispensables'))
        self.assertEqual(len(items), 100)
        self.assertEqual([i['title'] for i in items[:2]], ['Blacksad', 'Astérix'])
        tintin = next(i for i in items if i['title'] == 'Tintin')
        self.assertEqual(tintin['genre'], 'Aventure')
        self.assertEqual(len({i['url'] for i in items}), 100)

    def test_pantheon_authors(self):
        items = parsers.parse_pantheon(soup('pantheon'))
        self.assertEqual(len(items), 72)
        self.assertEqual(items[0]['name'], 'Andreas')
        self.assertEqual(items[0]['dates'], '1951')
        self.assertEqual(items[0]['country'], 'Allemagne')
        self.assertEqual((items[0]['category'], items[0]['year']), ('Franco-Belge', '2022'))
        self.assertEqual(items[0]['notable_works'], 'Rork, Cromwell Stone, Capricorne, Arq')
        self.assertTrue(items[0]['photo'].endswith('Photo_267.jpg'))
        self.assertEqual(items[-1]['rank'], 72)

    def test_theme_groups_report_truncated_previews_and_full_pages_list_everything(self):
        groups = parsers.parse_theme_groups(soup('theme_list'))
        self.assertEqual([g['name'] for g in groups], ['Les genres', 'Les styles', 'Les périodes historiques', 'La géographie'])
        genres = groups[0]
        self.assertEqual((len(genres['themes']), genres['declared']), (6, 25))
        self.assertTrue(genres['more_url'].endswith('/theme/super/id/1'))
        full = parsers.parse_theme_tiles(soup('theme_super_1'))
        self.assertEqual(len(full), 25)
        self.assertEqual(full[0], {'name': 'Anticipation', 'slug': 'anticipation',
                                   'url': 'https://www.bedetheque.com/theme-BD-anticipation.html'})

    def test_theme_page(self):
        title, items = parsers.parse_theme_page(soup('theme_page'), fallback_title='fallback')
        self.assertEqual(title, 'Humour')
        self.assertEqual(len(items), 8)
        first = items[0]
        self.assertEqual(first['title'], 'Achille Talon')
        self.assertEqual((first['origin'], first['authors'], first['note']), ('Franco-belge', 'Greg', 'Note : 4.06/5'))
        self.assertEqual(first['summary'], 'Résumé de la série.')

    def test_bdgest_top_podium_and_list(self):
        items = parsers.parse_bdgest_top(soup('bdgest_top'))
        self.assertEqual(len(items), 100)
        self.assertEqual([i['rank'] for i in items], list(range(1, 101)))
        first = items[0]
        self.assertEqual((first['title'], first['volume_label'], first['votes']), ('Lucky Luke (vu par...)', '#8', '101'))
        row = items[5]
        self.assertEqual(row['title'], 'Les 5 Terres')
        self.assertEqual(row['volume_label'], '#17 « Ces foutues terres gelées »')
        self.assertEqual((row['publisher'], row['authors'], row['votes']), ('Delcourt', 'Lewelyn, Lereculey', '64'))

    def test_unrecognised_layout_yields_nothing(self):
        empty = BeautifulSoup('<html><body><p>nouvelle mise en page</p></body></html>', 'html.parser')
        self.assertEqual(parsers.parse_indispensables(empty), [])
        self.assertEqual(parsers.parse_pantheon(empty), [])
        self.assertEqual(parsers.parse_theme_groups(empty), [])
        self.assertEqual(parsers.parse_theme_page(empty, 'slug'), ('slug', []))
        self.assertEqual(parsers.parse_bdgest_top(empty), [])
        self.assertEqual(parsers.parse_album_reviews(empty), [])


class EmptyResultsAreNeverCachedTest(unittest.TestCase):
    def test_routes_return_an_error_before_saving_an_empty_result(self):
        root = Path(__file__).resolve().parents[1] / 'blueprints'
        for path in ('bedetheque/routes.py', 'bdgest/routes.py'):
            source = (root / path).read_text(encoding='utf-8')
            self.assertIn('la mise en page', source)
        routes = (root / 'bedetheque/routes.py').read_text(encoding='utf-8')
        for guard in ("_layout_changed_response('indispensables')", "_layout_changed_response('panthéon')",
                      "_layout_changed_response('thèmes')", "_layout_changed_response(f'thème {slug}')"):
            index = routes.index(guard)
            self.assertLess(index, routes.index('save_scrape_cache(', index))


if __name__ == '__main__':
    unittest.main()
