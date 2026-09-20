from pathlib import Path
import unittest


class ForceReplacementVisibilityTests(unittest.TestCase):
    def test_force_replacement_is_hidden_until_existing_series_selected(self):
        template = (Path(__file__).resolve().parents[1] / 'templates/import.html').read_text()
        source = (Path(__file__).resolve().parents[1] / 'static/js/import.js').read_text()
        self.assertIn('id="force-replace-group" style="display: none;"', template)
        self.assertIn('function updateForceReplaceVisibility(seriesValue)', source)
        self.assertIn("seriesValue !== '__new__'", source)

    def test_new_series_selection_clears_force_replacement(self):
        source = (Path(__file__).resolve().parents[1] / 'static/js/import.js').read_text()
        self.assertIn("if (!show) forceCheckbox.checked = false;", source)
        self.assertIn('updateForceReplaceVisibility(this.value);', source)


if __name__ == '__main__':
    unittest.main()
