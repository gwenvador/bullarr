import unittest
from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[1] / 'static' / 'js' / 'bedetheque-enrich.js').read_text(encoding='utf-8')


class AuthorPickerScrollTest(unittest.TestCase):
    """Le sélecteur d'auteur affichait au plus 100 auteurs: le défilement s'arrêtait net."""

    def test_no_fixed_cap_on_the_number_of_listed_authors(self):
        start = SOURCE.index('function renderDatabaseAuthorDropdown')
        end = SOURCE.index('function selectDatabaseAuthor')
        self.assertNotIn('.slice(0, 100)', SOURCE[start:end])

    def test_more_authors_are_appended_while_scrolling(self):
        self.assertIn("addEventListener('scroll', _onAuthorPickerScroll)", SOURCE)
        self.assertIn('_appendAuthorPickerItems(list)', SOURCE)
        self.assertIn('AUTHOR_PICKER_PAGE_SIZE', SOURCE)

    def test_selection_still_uses_the_index_in_the_full_author_list(self):
        self.assertIn('selectDatabaseAuthor(${index})', SOURCE)
        self.assertIn('databaseAuthors[index].name', SOURCE)


if __name__ == '__main__':
    unittest.main()
