import sqlite3
import unittest

from blueprints.library.routes import apply_tracked_volume_and_gate, _find_existing_volume_for_import


class TrackedSpecialVolumeTests(unittest.TestCase):
    """« Herr Doktor : je peux pas choisir spéciaux dans corriger la série »."""

    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.execute('CREATE TABLE volumes (id INTEGER PRIMARY KEY, series_id INTEGER, volume_number INTEGER, '
                          'is_bis INTEGER, bis_suffix TEXT, is_integral INTEGER, integral_number INTEGER, '
                          'is_hs INTEGER, hs_number INTEGER, is_episode INTEGER, episode_number INTEGER, '
                          'is_special INTEGER, special_label TEXT, filepath TEXT, file_size INTEGER, format TEXT)')
        self.conn.execute("INSERT INTO volumes (id, series_id, volume_number, is_special) VALUES (1, 7, 1, 0)")
        self.conn.execute("INSERT INTO volumes (id, series_id, is_special) VALUES (2, 7, 1)")
        self.conn.execute("INSERT INTO volumes (id, series_id, is_special) VALUES (3, 7, 1)")

    def test_tracked_special_overrides_parsed_number_and_targets_its_row(self):
        parsed = {'volume': 1}
        destination = {'special_volume_id': 3, 'special_label': None, 'volume_number': None}
        self.assertTrue(apply_tracked_volume_and_gate(parsed, destination))
        self.assertIsNone(parsed['volume'])
        self.assertTrue(parsed['is_special'])
        row = _find_existing_volume_for_import(self.conn.cursor(), 7, parsed)
        self.assertEqual(row[0], 3)

    def test_target_id_ignored_once_user_picks_another_type(self):
        parsed = {'volume': 1, 'is_special': False, 'target_volume_id': 3}
        row = _find_existing_volume_for_import(self.conn.cursor(), 7, parsed)
        self.assertEqual(row[0], 1)

    def test_target_id_from_another_series_is_ignored(self):
        parsed = {'volume': None, 'is_special': True, 'special_label': None, 'target_volume_id': 3}
        row = _find_existing_volume_for_import(self.conn.cursor(), 8, parsed)
        self.assertTrue(row is None or row[0] != 3)


if __name__ == '__main__':
    unittest.main()
