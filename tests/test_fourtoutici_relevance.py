import unittest

from blueprints.fourtoutici.scraper import _looks_relevant


class FourtouticiRelevanceTest(unittest.TestCase):
    def test_keeps_matching_bd(self):
        self.assertTrue(_looks_relevant('Les vieux fourneaux', 'BD Les Vieux Fourneaux 07 – Lupano & Cauuet.cbz'))
        self.assertTrue(_looks_relevant('Blacksad', 'BD Blacksad 02 Arctic-Nation – Diaz Canales & Guarnido.pdf'))
        self.assertTrue(_looks_relevant('Les Schtroumpfs', 'Les.Schtroumpfs.T05.cbr'))

    def test_drops_unrelated_or_non_bd(self):
        self.assertFalse(_looks_relevant('Les vieux fourneaux', 'BD Harry Mickson 06 Les vieux copains pleins de pépins – Florence Cestac.pdf'))
        self.assertFalse(_looks_relevant('Le temps du reve', 'EBOOK Le temps du rêve – Norman Spinrad.pdf'))
        self.assertFalse(_looks_relevant('Harry Potter', 'BD Mickey Parade 12.pdf'))


if __name__ == '__main__':
    unittest.main()
