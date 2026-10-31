import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class NewSeriesFromMatchModalTest(unittest.TestCase):
    """Les fenêtres « Matcher la série » de Nouveautés permettent aussi de créer une nouvelle série."""

    def setUp(self):
        self.script = (ROOT / 'static/js/ebdz-latest.js').read_text()

    def test_both_match_modals_use_the_import_style_series_picker(self):
        self.assertIn("_renderMatchSeriesPicker('telegram-match'", self.script)
        self.assertIn("_renderMatchSeriesPicker('ebdz-match'", self.script)

    def test_uses_the_same_interface_as_the_import_destination_window(self):
        # Champ « Série » avec recherche, première entrée « ➕ Créer une nouvelle série », champ « Nom de la
        # nouvelle série » + bouton Bédéthèque, Annuler / Valider; fenêtre de recherche avec couvertures.
        for expected in ('Tapez pour rechercher une série...', "'➕ Créer une nouvelle série'", 'Nom de la nouvelle série',
                         'Matcher sur Bédéthèque', '>Annuler</button>', '>Valider</button>',
                         'buildMatchModalBodyHtml(', 'loadBedethequeMatchCandidateCovers(', 'Matché Bédéthèque :'):
            self.assertIn(expected, self.script)
        self.assertNotIn('Créer la série et rattacher', self.script)
        template = (ROOT / 'templates/ebdz-latest.html').read_text()
        self.assertLess(template.index('js/match-modal.js'), template.index('js/ebdz-latest.js'))

    def test_series_is_only_created_on_validation_then_the_file_is_matched(self):
        start = self.script.index('function _renderMatchSeriesPicker(')
        body = self.script[start:self.script.index('async function openTelegramMatchOverrideModal(', start)]
        validate = body[body.index("validateButton.addEventListener('click'"):]
        self.assertIn("_resolveSeriesForAutoAdd(name, null, pick ? pick.url : null, true)", validate)
        self.assertIn("await onChosen(created.seriesId, name)", validate)
        self.assertIn("confirmTelegramMatchOverride(filename, seriesId, title", self.script)
        self.assertIn("confirmEbdzThreadMatch(threadId, seriesId)", self.script)

    def test_bedetheque_search_only_runs_when_the_user_opens_the_match_window(self):
        self.assertIn("addEventListener('click', () => openNouveautesBedethequeMatchModal(prefix))", self.script)

if __name__ == '__main__':
    unittest.main()
