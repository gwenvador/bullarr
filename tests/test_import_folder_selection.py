from pathlib import Path
import unittest


class ImportFolderSelectionTests(unittest.TestCase):
    def test_each_pack_folder_renders_a_select_all_checkbox(self):
        source = (Path(__file__).resolve().parents[1] / 'static/js/import.js').read_text()
        self.assertIn('function _packFolderSelectableFiles(group)', source)
        self.assertIn('data-tooltip="Sélectionner tous les fichiers de ce dossier"', source)
        self.assertIn("toggleSubfolderSelection(${pending.id}, '${escapeForAttribute(subfolder)}', this.checked)", source)

    def test_folder_selection_covers_unassigned_and_assigned_files(self):
        source = (Path(__file__).resolve().parents[1] / 'static/js/import.js').read_text()
        self.assertIn("if (file.destination) file.selected = checked;", source)
        self.assertIn("else file._bulkSelected = checked;", source)


if __name__ == '__main__':
    unittest.main()
