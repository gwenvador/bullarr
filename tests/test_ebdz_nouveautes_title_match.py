import importlib
import sys
import unittest
from pathlib import Path

WORKTREE = Path('[REDACTED_PATH]')
sys.path.insert(0, str(WORKTREE))


class NouveautesTitleSeriesMatchTest(unittest.TestCase):
    def test_unambiguous_title_match_exposes_local_series_and_bedetheque_link(self):
        routes = importlib.import_module('blueprints.ebdz.routes')
        rows = [
            (421, None, 'https://www.bedetheque.com/serie-15032-BD-Carthago.html', 'Carthago'),
            (420, None, 'https://www.bedetheque.com/serie-27171-BD-Carthago-Adventures.html', 'Carthago Adventures'),
        ]
        matched = routes._build_nouveautes_title_match_index(rows, lambda title: title.casefold())
        self.assertEqual(
            (421, 'https://www.bedetheque.com/serie-15032-BD-Carthago.html', 'Carthago'),
            matched['carthago'],
        )

    def test_ambiguous_title_match_does_not_guess_a_series(self):
        routes = importlib.import_module('blueprints.ebdz.routes')
        rows = [
            (1, None, 'https://example.test/a', 'Alpha'),
            (2, None, 'https://example.test/b', 'Alpha'),
        ]
        matched = routes._build_nouveautes_title_match_index(rows, lambda title: title.casefold())
        self.assertIsNone(matched['alpha'])


if __name__ == '__main__':
    unittest.main()
