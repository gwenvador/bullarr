import sqlite3
import unittest
from unittest import mock

from blueprints.library import routes
from blueprints.library.routes import (
    UnsafePathError, apply_tracked_volume_and_gate, can_auto_assign, find_active_download_destination,
    get_format_priority, is_better_volume, sanitize_path_component,
)


class DuplicateDecisionTest(unittest.TestCase):
    def test_a_new_file_must_be_strictly_more_than_5_percent_bigger_to_replace(self):
        self.assertFalse(is_better_volume(100, 100))
        self.assertFalse(is_better_volume(90, 100))
        self.assertFalse(is_better_volume(105, 100))        # exactement +5 %: pas assez
        self.assertTrue(is_better_volume(106, 100))
        self.assertTrue(is_better_volume(1, 0))             # rien d'existant de taille connue
        self.assertFalse(is_better_volume(0, 0))

    def test_format_preference_order(self):
        self.assertEqual([get_format_priority(f) for f in ('cbz', 'ZIP', 'cbr', 'rar', 'pdf')], [0, 0, 1, 1, 2])
        self.assertEqual((get_format_priority('epub'), get_format_priority(None), get_format_priority('')), (99, 99, 99))


class PathComponentTest(unittest.TestCase):
    def test_plain_names_are_accepted(self):
        self.assertEqual(sanitize_path_component('Tome 01 - Titre.cbz'), 'Tome 01 - Titre.cbz')

    def test_names_that_could_escape_their_directory_are_refused(self):
        for name in ('', None, '.', '..', 'a/b', '../etc', '/etc/passwd', 'dossier/../../x'):
            with self.assertRaises(UnsafePathError, msg=repr(name)):
                sanitize_path_component(name, 'nom de fichier')

    def test_the_error_names_what_was_invalid(self):
        with self.assertRaisesRegex(UnsafePathError, 'nom de série invalide'):
            sanitize_path_component('../x', 'nom de série')


class AutoAssignTest(unittest.TestCase):
    def test_needs_a_title_and_the_feature_enabled(self):
        self.assertTrue(can_auto_assign({'title': 'Thorgal'}, {}))
        self.assertFalse(can_auto_assign({'title': 'Thorgal'}, {'auto_assign_enabled': False}))
        self.assertFalse(can_auto_assign({'title': ''}, {'auto_assign_enabled': True}))
        self.assertFalse(can_auto_assign({}, {}))


class ExistingVolumeLookupTest(unittest.TestCase):
    """_find_existing_volume_for_import décide quel tome un fichier remplace ou duplique."""

    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.execute('''CREATE TABLE volumes (
            id INTEGER PRIMARY KEY, series_id INTEGER, volume_number INTEGER, is_bis INTEGER DEFAULT 0,
            is_integral INTEGER DEFAULT 0, integral_number INTEGER, is_hs INTEGER DEFAULT 0, hs_number INTEGER,
            is_episode INTEGER DEFAULT 0, episode_number INTEGER, is_special INTEGER DEFAULT 0, special_label TEXT,
            filepath TEXT, file_size INTEGER, format TEXT)''')
        self.addCleanup(self.conn.close)

    def add(self, **values):
        defaults = {'series_id': 1, 'filepath': '/BD/x.cbz', 'file_size': 100, 'format': 'cbz'}
        defaults.update(values)
        columns = ', '.join(defaults)
        self.conn.execute(f'INSERT INTO volumes ({columns}) VALUES ({", ".join("?" for _ in defaults)})', list(defaults.values()))
        return self.conn.execute('SELECT last_insert_rowid()').fetchone()[0]

    def find(self, **parsed):
        return routes._find_existing_volume_for_import(self.conn.cursor(), 1, parsed, single_album=parsed.pop('single_album', False))

    def test_nothing_known_gives_an_empty_result(self):
        self.assertEqual(self.find(volume=3), (None, None, 0, None))

    def test_a_regular_volume_matches_its_number_and_never_the_bis_edition(self):
        main = self.add(volume_number=13, filepath='/BD/13.cbz', file_size=500)
        self.add(volume_number=13, is_bis=1, filepath=None, file_size=None)
        self.assertEqual(self.find(volume=13), (main, '/BD/13.cbz', 500, 'cbz'))

    def test_other_series_are_ignored(self):
        self.add(series_id=2, volume_number=3)
        self.assertEqual(self.find(volume=3)[0], None)

    def test_integrals_halves_and_episodes_match_their_own_numbering(self):
        integral = self.add(volume_number=None, is_integral=1, integral_number=2)
        hs = self.add(is_hs=1, hs_number=4)
        episode = self.add(is_episode=1, episode_number=6)
        self.add(volume_number=2)                                      # un tome 2 ne doit pas être pris pour l'intégrale 2
        self.assertEqual(self.find(is_integral=True, integral_number=2)[0], integral)
        self.assertEqual(self.find(is_hs=True, hs_number=4)[0], hs)
        self.assertEqual(self.find(is_episode=True, episode_number=6)[0], episode)
        self.assertEqual(self.find(is_integral=True, integral_number=9)[0], None)

    def test_an_unnumbered_integral_only_matches_an_unnumbered_integral(self):
        numbered = self.add(is_integral=1, integral_number=1)
        unnumbered = self.add(is_integral=1, integral_number=None)
        self.assertEqual(self.find(is_integral=True, integral_number=None)[0], unnumbered)
        self.assertNotEqual(self.find(is_integral=True, integral_number=None)[0], numbered)

    def test_specials_need_a_label_to_be_matched(self):
        special = self.add(is_special=1, special_label='Carnet de croquis')
        self.assertEqual(self.find(is_special=True, special_label='Carnet de croquis')[0], special)
        self.assertEqual(self.find(is_special=True, special_label=None)[0], None)

    def test_a_one_shot_matches_the_only_unnumbered_regular_volume(self):
        one_shot = self.add(volume_number=None)
        self.add(is_hs=1, hs_number=1)
        self.assertEqual(self.find()[0], one_shot)

    def test_a_numbered_file_can_fill_an_empty_episode_placeholder_but_not_a_filled_one(self):
        placeholder = self.add(is_episode=1, episode_number=2, filepath=None, file_size=None)
        self.assertEqual(self.find(volume=2)[0], placeholder)
        self.conn.execute('UPDATE volumes SET filepath = ? WHERE id = ?', ('/BD/ep2.cbz', placeholder))
        self.assertEqual(self.find(volume=2)[0], None)

    def test_single_album_series_returns_the_album_whatever_the_number(self):
        album = self.add(volume_number=1, filepath='/BD/album.cbz', file_size=300)
        self.assertEqual(self.find(volume=7, single_album=True), (album, '/BD/album.cbz', 300, 'cbz'))


class TrackedVolumeGateTest(unittest.TestCase):
    """Un téléchargement suivi (série/tome connus au clic) prime sur le nom du fichier."""

    def test_tracked_volume_fills_a_missing_number(self):
        parsed = {'volume': None}
        self.assertTrue(apply_tracked_volume_and_gate(parsed, {'volume_number': 38}))
        self.assertEqual(parsed['volume'], 38)

    def test_tracked_volume_wins_over_a_conflicting_number_in_the_file_name(self):
        parsed = {'volume': 2}
        self.assertTrue(apply_tracked_volume_and_gate(parsed, {'volume_number': 38}))
        self.assertEqual((parsed['volume'], parsed['tracked_volume_overridden_from']), (38, 2))

    def test_matching_numbers_change_nothing(self):
        parsed = {'volume': 5}
        self.assertTrue(apply_tracked_volume_and_gate(parsed, {'volume_number': 5}))
        self.assertNotIn('tracked_volume_overridden_from', parsed)

    def test_a_detected_special_type_conflicts_with_a_tracked_volume_number(self):
        for kind in ('is_integral', 'is_hs', 'is_episode'):
            parsed = {'volume': None, kind: True}
            self.assertFalse(apply_tracked_volume_and_gate(parsed, {'volume_number': 4}), kind)
            self.assertEqual(parsed['tracked_volume_conflict'], 4)

    def test_tracked_type_is_applied_to_a_file_without_number_or_type(self):
        parsed = {'volume': None}
        self.assertTrue(apply_tracked_volume_and_gate(parsed, {'is_integral': True, 'integral_number': 3}))
        self.assertEqual((parsed['is_integral'], parsed['integral_number']), (True, 3))
        parsed = {'volume': None}
        self.assertTrue(apply_tracked_volume_and_gate(parsed, {'is_hs': True, 'hs_number': 1}))
        self.assertTrue(parsed['is_hs'])
        parsed = {'volume': None}
        self.assertTrue(apply_tracked_volume_and_gate(parsed, {'is_episode': True, 'episode_number': 7}))
        self.assertEqual(parsed['episode_number'], 7)

    def test_without_any_number_only_one_shots_single_albums_and_typed_files_pass(self):
        self.assertFalse(apply_tracked_volume_and_gate({'volume': None}, {}))
        self.assertTrue(apply_tracked_volume_and_gate({'volume': None}, {'is_oneshot': True}))
        self.assertTrue(apply_tracked_volume_and_gate({'volume': None}, {'is_single_album': True}))
        self.assertTrue(apply_tracked_volume_and_gate({'volume': None, 'is_hs': True}, {}))


class DestinationFromTrackedDownloadTest(unittest.TestCase):
    ROWS = [{'id': 7, 'title': 'Thorgal - 12 - La cité du dieu perdu', 'series_id': 42, 'volume_id': None,
             'volume_number': 12, 'force_replace': True},
            {'id': 8, 'title': 'Sans série identifiée - tome 1', 'series_id': None, 'volume_id': None, 'volume_number': 1}]

    def test_destination_is_built_from_the_tracked_series_and_options(self):
        rows = routes.prepare_trackable_downloads_for_matching([dict(r) for r in self.ROWS]) \
            if hasattr(routes, 'prepare_trackable_downloads_for_matching') else [dict(r) for r in self.ROWS]
        with mock.patch.object(routes, '_build_active_download_destination', return_value={'series_id': 42}) as build:
            result = find_active_download_destination('Thorgal - 12 - La cité du dieu perdu.cbr', rows)
        self.assertEqual(result, {'series_id': 42})
        args, kwargs = build.call_args
        self.assertEqual(args[:3], (42, 12, 7))
        self.assertTrue(kwargs['force_replace'])

    def test_no_match_or_a_match_without_series_gives_none(self):
        rows = [dict(r) for r in self.ROWS]
        with mock.patch.object(routes, '_build_active_download_destination') as build:
            self.assertIsNone(find_active_download_destination('Autre chose.cbz', rows))
            self.assertIsNone(find_active_download_destination('Sans série identifiée - tome 1.cbz', rows))
        build.assert_not_called()


if __name__ == '__main__':
    unittest.main()
