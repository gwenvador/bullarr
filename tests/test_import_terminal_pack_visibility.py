import unittest

from blueprints.library import routes


class TerminalPackVisibilityTest(unittest.TestCase):
    def item(self, name, status, is_pack_download=False, manual_override=False):
        destination = {'download_status': status}
        if is_pack_download:
            destination['is_pack_download'] = True
        return {'filename': name, 'destination': destination, 'manual_override': manual_override}

    def test_unprocessed_files_of_a_finished_pack_stay_visible(self):
        shown = routes._exclude_terminal_without_manual_override([
            self.item('T01.cbr', 'skipped', is_pack_download=True),
            self.item('T02.cbr', 'imported', is_pack_download=True),
        ])
        self.assertEqual([i['filename'] for i in shown], ['T01.cbr', 'T02.cbr'])

    def test_finished_single_downloads_stay_hidden(self):
        shown = routes._exclude_terminal_without_manual_override([
            self.item('single.cbz', 'imported'),
            self.item('other.cbz', 'skipped'),
            self.item('waiting.cbz', 'completed'),
            self.item('manual.cbz', 'imported', manual_override=True),
        ])
        self.assertEqual([i['filename'] for i in shown], ['waiting.cbz', 'manual.cbz'])


if __name__ == '__main__':
    unittest.main()
