import unittest
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[1]


class NouveautesEbdzMatchControlsTest(unittest.TestCase):
    def setUp(self):
        self.source = (WORKTREE / 'static/js/ebdz-latest.js').read_text()
        start = self.source.index('function _nouveautesMatchPencilHtml(event)')
        self.pencil_renderer = self.source[start:self.source.index('function _nouveautesSeriesHtml(event)', start)]
        start = self.source.index('function _nouveautesSeriesHtml(event)')
        self.series_renderer = self.source[start:self.source.index('function _nouveautesMatchedHtml(event)', start)]
        start = self.source.index('function _nouveautesMatchedHtml(event)')
        self.matched_renderer = self.source[start:self.source.index('// "dans nouveautés ca a match', start)]

    def test_pencil_is_next_to_series_name_even_when_unmatched(self):
        self.assertIn('_nouveautesMatchPencilHtml(event)', self.series_renderer)
        self.assertIn('if (!seriesTitle && !bedethequeUrl)', self.series_renderer)
        self.assertNotIn('pencil', self.matched_renderer)

    def test_matched_ebdz_edit_button_opens_modal_without_navigating_to_series_page(self):
        self.assertIn('openEbdzThreadMatchModal(', self.pencil_renderer)
        self.assertNotIn("/series/${seriesId}?open_ebdz_match=1", self.pencil_renderer)

    def test_modal_persists_selected_series_against_the_ebdz_thread(self):
        start = self.source.index('async function confirmEbdzThreadMatch')
        modal_logic = self.source[start:self.source.index('function _nouveautesImportStatusHtml', start)]
        self.assertIn("/api/series/${seriesId}/ebdz-match", modal_logic)
        self.assertIn('thread_id', modal_logic)
        self.assertIn('loadNouveautesEvents(false, false)', modal_logic)


if __name__ == '__main__':
    unittest.main()
