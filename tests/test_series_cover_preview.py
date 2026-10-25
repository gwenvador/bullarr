import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SeriesCoverPreviewTest(unittest.TestCase):
    """Vignette de la couverture au survol du titre dans le tableau de la bibliothèque."""

    def setUp(self):
        self.script = (ROOT / 'static/js/library.js').read_text()
        self.css = (ROOT / 'static/css/style-library-search.css').read_text()

    def test_title_cell_carries_the_cover_or_falls_back_to_the_text_tooltip(self):
        start = self.script.index('function _seriesTableTitleCellHtml(')
        body = self.script[start:self.script.index('let _seriesCoverPreviewEl', start)]
        self.assertIn('pickPosterCoverPath(s)', body)
        self.assertIn('data-cover=', body)
        self.assertIn('data-tooltip=', body)  # sans couverture : infobulle texte conservée

    def test_row_uses_the_title_cell_helper(self):
        start = self.script.index('function buildTableRowHtml(')
        self.assertIn('_seriesTableTitleCellHtml(s)', self.script[start:start + 2500])

    def test_preview_is_bound_once_and_never_blocks_the_pointer(self):
        self.assertEqual(self.script.count('_bindSeriesCoverPreview();'), 1)
        self.assertIn('.series-table-title[data-cover]', self.script)
        rule = self.css[self.css.index('.series-cover-preview {'):]
        self.assertIn('pointer-events: none', rule.split('}')[0])


if __name__ == '__main__':
    unittest.main()
