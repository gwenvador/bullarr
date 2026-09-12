import unittest
from pathlib import Path

WORKTREE = Path('[REDACTED_PATH]')


class NouveautesEbdzMatchControlsTest(unittest.TestCase):
    def setUp(self):
        self.source = (WORKTREE / 'static/js/ebdz-latest.js').read_text()
        start = self.source.index('function _nouveautesMatchedHtml(event)')
        self.matched_renderer = self.source[start:self.source.index('// "dans nouveautés ca a match', start)]

    def test_unmatched_ebdz_row_has_manual_match_button(self):
        self.assertIn("const ebdzMatchHtml = event.type === 'ebdz'", self.matched_renderer)
        self.assertIn('openEbdzThreadMatchModal(', self.matched_renderer)

    def test_matched_ebdz_edit_button_opens_modal_without_navigating_to_series_page(self):
        self.assertIn('openEbdzThreadMatchModal(', self.matched_renderer)
        self.assertNotIn("/series/${seriesId}?open_ebdz_match=1", self.matched_renderer)

    def test_modal_persists_selected_series_against_the_ebdz_thread(self):
        start = self.source.index('async function confirmEbdzThreadMatch')
        modal_logic = self.source[start:self.source.index('// Icône/texte par fichier', start)]
        self.assertIn("/api/series/${seriesId}/ebdz-match", modal_logic)
        self.assertIn('thread_id', modal_logic)
        self.assertIn('loadNouveautesEvents(false, false)', modal_logic)


if __name__ == '__main__':
    unittest.main()
