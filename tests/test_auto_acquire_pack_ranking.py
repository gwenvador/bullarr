import unittest

from flask import Flask

from blueprints.bedetheque import auto_acquire

SERIES = 'Les grands Classiques de la littérature en bande dessinée'
BASE = 'Les.Grands.Classiques.de.la.Litterature.en.Bande.Dessinee'


def _torrent(name, seeders):
    return {'title': name, 'filename': name, 'source': 'prowlarr', 'seeders': seeders, 'indexer': 'Demo'}


class PackRankingTest(unittest.TestCase):
    """Un lot qui couvre toute la série se choisit par format puis par seeders."""

    def _best(self, results, missing_count=48):
        with Flask(__name__).app_context():
            return auto_acquire._best_pack_result(results, SERIES, missing_count=missing_count)

    def test_integral_with_more_seeders_beats_a_pack_that_only_announces_its_range(self):
        ranged = _torrent(f'{BASE}.FRENCH.HYBRiD.COMiC.CBZ.eBook-TONER (Tomes 01 à 48)', 3)
        integral = _torrent(f'{BASE}.Integrale.2016-2018.FR.[CBZ]-TONER', 40)
        self.assertIs(self._best([ranged, integral]), integral)
        self.assertIs(self._best([integral, ranged]), integral)

    def test_seeders_do_not_override_a_better_format(self):
        cbz = _torrent(f'{BASE}.Integrale.2016-2018.FR.[CBZ]-TONER', 2)
        pdf = _torrent(f'{BASE}.Integrale.2016-2018.FR.[PDF]-TONER', 90)
        self.assertIs(self._best([pdf, cbz]), cbz)

    def test_partial_bundle_never_beats_a_confirmed_full_pack(self):
        full = _torrent(f'{BASE}.FRENCH.CBZ.eBook-TONER (Tomes 01 à 48)', 2)
        cycle = _torrent(f'{BASE}.Integrale.Cycle.1.FR.[CBZ]-TONER', 80)
        self.assertIs(self._best([cycle, full]), full)

    def test_when_nothing_covers_everything_the_widest_range_wins(self):
        short = _torrent(f'{BASE}.CBZ (Tomes 01 à 10)', 90)
        wide = _torrent(f'{BASE}.CBZ (Tomes 01 à 30)', 4)
        self.assertIs(self._best([short, wide]), wide)

    def test_covers_series_rules(self):
        covers = auto_acquire._pack_covers_series
        self.assertTrue(covers(f'{BASE}.Integrale.2016-2018.FR.[CBZ]', None, 48))
        self.assertTrue(covers('X (Tomes 01 à 48)', 48, 48))
        self.assertFalse(covers('X (Tomes 01 à 10)', 10, 48))
        self.assertFalse(covers('X.Integrale.Cycle.2', None, 48))
        self.assertFalse(covers('X.Integrale.Partie.1', None, 48))
        self.assertFalse(covers('X.FRENCH.CBZ', None, 48))


class IntegralWithYearsIsAPackTest(unittest.TestCase):
    """« Integrale.2016-2018 » : les années ne sont pas un numéro d'intégrale."""

    def test_years_after_the_word_do_not_make_a_numbered_integral(self):
        from blueprints.library.scanner import LibraryScanner
        parse = LibraryScanner.parse_filename
        self.assertTrue(parse(f'{BASE}.Integrale.2016-2018.FR.[CBZ]-TONER')['is_pack'])
        self.assertTrue(parse('Serie.Integrale.1999.FR.[CBZ]')['is_pack'])
        self.assertTrue(parse('Serie.Integrale.FR.[CBZ]')['is_pack'])
        for numbered in ('Serie.Integrale.2.FR.[CBZ]', 'Serie.Integrale.12.FR', 'Serie.Integrale 3 FR', 'Serie.Integrale #4'):
            self.assertFalse(parse(numbered)['is_pack'], numbered)


if __name__ == '__main__':
    unittest.main()
