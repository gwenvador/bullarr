import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class OneshotSameLayoutTest(unittest.TestCase):
    """Un one-shot a le même tableau d'albums qu'une série de plusieurs volumes."""

    def setUp(self):
        self.script = (ROOT / 'static/js/library.js').read_text()

    def test_volumes_section_does_not_branch_on_oneshot(self):
        start = self.script.index('function buildVolumesSectionHtml(')
        body = self.script[start:self.script.index('function toggleVolumeTableRowSelection(', start)]
        self.assertNotIn('!data.is_oneshot', body)
        self.assertIn('buildVolumesTableHtml(filteredVolumes, false)', body)

    def test_header_has_no_oneshot_specific_blocks(self):
        for marker in ('${oneshotMetaHtml}', "' 🔸 One-shot'", 'openComicReader(${Number(oneshotFile.id)})'):
            self.assertNotIn(marker, self.script)


if __name__ == '__main__':
    unittest.main()
