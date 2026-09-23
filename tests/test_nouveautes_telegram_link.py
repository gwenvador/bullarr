import unittest
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[1]


class NouveautesTelegramLinkTest(unittest.TestCase):
    def setUp(self):
        source = (WORKTREE / 'static/js/ebdz-latest.js').read_text()
        start = source.index('function _nouveautesTelegramRowHtml(event)')
        end = source.index('// Menu de filtre "Origine"', start)
        self.renderer = source[start:end]

    def test_telegram_row_links_to_source_post(self):
        self.assertIn('https://t.me/${encodeURIComponent', self.renderer)
        self.assertIn('data-tooltip="Voir le post Telegram"', self.renderer)
        self.assertIn('target="_blank"', self.renderer)
        self.assertIn('event.message_id != null', self.renderer)

    def test_missing_telegram_identity_keeps_non_linked_icon(self):
        self.assertIn('telegramIconHtml', self.renderer)
        self.assertIn('data-tooltip="Telegram"', self.renderer)


if __name__ == '__main__':
    unittest.main()
