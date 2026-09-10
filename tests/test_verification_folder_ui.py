import unittest
from pathlib import Path

WORKTREE = Path('[REDACTED_PATH]')


class VerificationFolderUiTest(unittest.TestCase):
    def test_folder_card_uses_icon_launch_button_and_short_description(self):
        html = (WORKTREE / 'templates/verification.html').read_text()
        card = html[html.index('id="verificationFolderPlacementCard"'):html.index('id="verificationMatchingCard"')]
        self.assertIn('id="verifRunMisplacedFoldersBtn"', card)
        self.assertIn('<svg class="icon"', card)
        self.assertIn('Vérifie que chaque dossier de série correspond au template et à son univers.</p>', card)
        self.assertNotIn('Le scan ne déplace rien', card)

    def test_folder_renderer_uses_collapsible_universe_headers_with_group_checkbox(self):
        source = (WORKTREE / 'static/js/verification.js').read_text()
        start = source.index('function renderMisplacedFolders(data)')
        end = source.index('async function _verifRequestFolderReconciliation', start)
        renderer = source[start:end]
        self.assertIn('verif-folder-move-group-select', renderer)
        self.assertIn('verifToggleFolderMoveGroup', renderer)
        self.assertIn('chevron', renderer)
        self.assertNotIn("svgIcon('folder')", renderer)

    def test_folder_check_runs_automatically_on_page_load(self):
        source = (WORKTREE / 'static/js/verification.js').read_text()
        dom_ready = source[source.index("document.addEventListener('DOMContentLoaded'"):]
        self.assertIn("runVerificationCategory('misplaced_folders');", dom_ready)


if __name__ == '__main__':
    unittest.main()
