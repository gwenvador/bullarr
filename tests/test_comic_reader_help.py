import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ComicReaderHelpTest(unittest.TestCase):
    """Bouton ? du lecteur : liste des raccourcis, cohérente avec les touches réellement gérées."""

    def setUp(self):
        self.js = (ROOT / 'static/js/reader.js').read_text()

    def test_help_button_panel_and_keys_exist(self):
        for needle in ('data-reader-action="help"', 'id="comic-reader-help"', 'function toggleComicReaderHelp(',
                       "'?': () => toggleComicReaderHelp()"):
            self.assertIn(needle, self.js)

    def test_escape_closes_the_help_before_the_reader(self):
        start = self.js.index('function _comicReaderKeydown(')
        body = self.js[start:self.js.index('function _applyComicReaderFit(', start)]
        self.assertLess(body.index("toggleComicReaderHelp(false)"), body.index('document.fullscreenElement'))

    def test_documented_keys_are_really_handled(self):
        handler = self.js[self.js.index('function _comicReaderKeydown('):self.js.index('function _applyComicReaderFit(')]
        shortcuts = self.js[self.js.index('const COMIC_READER_SHORTCUTS'):self.js.index('function _comicReaderHelpHtml(')]
        for label, key in (('D', "d:"), ('Z', "z:"), ('M', "m:"), ('L', "l:"), ('V', "v:"), ('C', "c:"), ('T', "t:"), ('F', "f:"), ('Début', 'Home:'), ('Fin', 'End:'),
                           ('PageDown', 'PageDown:'), ('PageUp', 'PageUp:'), ('Échap', 'Escape:'), ('0', '0:'),
                           ('Espace', "' ':"), ('+', "'+':"), ('−', "'-':")):
            self.assertIn(label, shortcuts, label)
            self.assertIn(key, handler, key)


if __name__ == '__main__':
    unittest.main()
