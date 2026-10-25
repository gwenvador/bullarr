import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ComicReaderSpreadTest(unittest.TestCase):
    """Lecteur : deux pages côte à côte (couverture seule, puis 2-3, 4-5...)."""

    def setUp(self):
        self.js = (ROOT / 'static/js/reader.js').read_text()
        self.css = (ROOT / 'static/css/style-library-search.css').read_text()

    def test_reader_has_a_second_image_a_toggle_and_a_shortcut(self):
        for needle in ('id="comic-reader-image-2"', 'data-reader-action="spread"', 'function toggleComicReaderSpread(',
                       "'comic-reader-spread'", 'd: () => toggleComicReaderSpread()'):
            self.assertIn(needle, self.js)

    def test_cover_stays_alone_and_pairs_start_on_even_pages(self):
        start = self.js.index('function _comicReaderNormalizePage(')
        body = self.js[start:self.js.index('function _comicReaderStorageKey(', start)]
        self.assertIn('spread && page > 1 && page % 2 === 1 ? page - 1 : page', body)
        self.assertIn('spread && start > 1 && start + 1 <= count', body)
        self.assertIn('page === 1 ? 2 : page + 2', body)

    def test_zoom_and_layout_work_on_the_pages_container(self):
        self.assertIn("getElementById('comic-reader-pages')", self.js)
        self.assertIn('function _layoutComicReaderPages(', self.js)
        self.assertIn('.comic-reader-pages', self.css)
        self.assertIn('.comic-reader-stage.zoomed .comic-reader-pages', self.css)

    def test_thumbnails_highlight_every_displayed_page(self):
        start = self.js.index('function _highlightComicReaderThumb(')
        body = self.js[start:self.js.index('function _layoutComicReaderPages', start) if 'function _layoutComicReaderPages' in self.js[start:start + 5000] else start + 1500]
        self.assertIn('_comicReaderSpreadPages(comicReader.page', body)


if __name__ == '__main__':
    unittest.main()
