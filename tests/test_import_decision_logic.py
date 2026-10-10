import os
import sqlite3
import json
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


class AutomaticHoldTest(unittest.TestCase):
    """Cas Blake et Mortimer: mis de côté à tort, avec un motif « assignation faite à la main »."""

    def test_bedetheque_inverted_titles_match_the_file_title(self):
        dest = {"series_title": "Blake et Mortimer (Les Aventures de)"}
        self.assertTrue(routes._pack_file_matches_destination({"title": "Les aventures de Blake et Mortimer"}, dest))
        self.assertTrue(routes._pack_file_matches_destination({"title": "Blake et Mortimer"}, dest))
        self.assertTrue(routes._pack_file_matches_destination({"title": "Le Gang"}, {"series_title": "Gang (Le)"}))
        self.assertFalse(routes._pack_file_matches_destination({"title": "Les aventures de Tintin"}, dest))

    def test_reason_explains_why_the_import_held_the_file(self):
        dest = {"series_title": "Thorgal"}
        reason = routes._automatic_hold_reason("/downloads/torrents/p/x.cbz", {"title": "Aldebaran"}, dest)
        self.assertIn("Aldebaran", reason)
        self.assertIn("Thorgal", reason)
        self.assertIn("sous-dossiers", routes._automatic_hold_reason("/downloads/torrents/p/x.cbz", {"title": "Thorgal"}, dest))
        packaged = os.path.join(routes._PACKAGE_TEMP_DIR, "01.cbz")
        self.assertIn("empaqueté", routes._automatic_hold_reason(packaged, {"title": "x"}, None))


class GluedSeparatorAndAlbumTitleTest(unittest.TestCase):
    """Cas Blake et Mortimer « -12- »: n importe quel numéro entre deux tirets collés."""

    def test_any_number_between_glued_dashes_is_the_volume(self):
        from blueprints.library.scanner import LibraryScanner
        for name, title, volume in (("Blake et Mortimer-8-Le Piege diabolique.cbz", "Blake et Mortimer", 8),
                                    ("Blake Et Mortimer-11-Les 3 Formules.cbz", "Blake Et Mortimer", 11),
                                    ("Blake et Mortimer-21-Le serment des cinq lords.cbz", "Blake et Mortimer", 21),
                                    ("Okko-09-Le cycle du vide I.cbr", "Okko", 9),
                                    ("Spider-Man - 03.cbz", "Spider-Man", 3)):
            parsed = LibraryScanner.parse_filename(name)
            self.assertEqual((parsed["title"], parsed["volume"]), (title, volume), name)

    def test_album_title_in_the_name_gives_the_volume_number(self):
        import json, sqlite3, tempfile
        from flask import Flask
        from blueprints.library.scanner import LibraryScanner
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "b.db")
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE volumes (id INTEGER PRIMARY KEY, series_id INTEGER, volume_number INTEGER, is_bis INTEGER DEFAULT 0, comicinfo TEXT)")
            for number, title in ((2, "Le Secret de l\x27Espadon - Tome 2"), (11, "Les 3 Formules du Professeur Satô - Tome 1"),
                                  (12, "Les 3 Formules du Professeur Satô - Tome 2")):
                conn.execute("INSERT INTO volumes (series_id, volume_number, comicinfo) VALUES (715, ?, ?)", (number, json.dumps({"title": title})))
            conn.commit()
            conn.close()
            app = Flask(__name__)
            app.config["DATABASE"] = db
            with app.app_context():
                name = "Blake Et Mortimer-12-Les 3 Formules Du Professeur Sato - Tome 2.cbz"
                parsed = LibraryScanner.parse_filename(name)
                routes.refine_volume_from_album_title(parsed, 715, name, "Blake et Mortimer (Les Aventures de)")
                self.assertEqual(parsed["volume"], 12)
                other = LibraryScanner.parse_filename("Blake et Mortimer-21-Le serment des cinq lords.cbz")
                routes.refine_volume_from_album_title(other, 715, "Blake et Mortimer-21-Le serment des cinq lords.cbz", "Blake et Mortimer")
                self.assertEqual(other["volume"], 21)                                   # aucun titre reconnu: inchangé


class AlreadyInLibraryTest(unittest.TestCase):
    def test_owned_volume_is_reported_with_the_import_decision(self):
        from unittest import mock
        dest = {"series_id": 1, "series_title": "Thorgal"}
        with mock.patch.object(routes, "get_db_connection") as conn, \
                mock.patch.object(routes, "_find_existing_volume_for_import", return_value=(5, "/BD/T01.cbz", 100, "cbz")):
            self.assertEqual(routes._existing_volume_info(dest, {"volume": 1}, "T01.cbz", 104),
                             {"size": 100, "format": "cbz", "will_replace": False})           # doublon: ignoré
            self.assertTrue(routes._existing_volume_info(dest, {"volume": 1}, "T01.cbz", 200)["will_replace"])
        with mock.patch.object(routes, "get_db_connection"), \
                mock.patch.object(routes, "_find_existing_volume_for_import", return_value=(5, None, None, None)):
            self.assertIsNone(routes._existing_volume_info(dest, {"volume": 1}, "T01.cbz", 100))   # placeholder sans fichier
        self.assertIsNone(routes._existing_volume_info({"series_id": 1, "is_new_series": True}, {}, "x", 1))


class SpecialTagTitleTest(unittest.TestCase):
    """« Titre-HS-Album »: le titre de la série s arrête au marqueur retiré."""

    def test_title_stops_at_the_removed_tag(self):
        from blueprints.library.scanner import LibraryScanner
        for name, title, hs in (("Blake Et Mortimer-HS-Le Rayon U.cbz", "Blake Et Mortimer", None),
                                ("Blake Et Mortimer - HS - Le Rayon U.cbz", "Blake Et Mortimer", None),
                                ("Thorgal-HS2-La saga.cbz", "Thorgal", 2),
                                ("Ivor - HS 03 - Le retour du roi (Zoran).cbr", "Ivor", 3)):
            parsed = LibraryScanner.parse_filename(name)
            self.assertEqual((parsed["title"], parsed["is_hs"], parsed["hs_number"]), (title, True, hs), name)

    def test_a_too_short_prefix_is_not_taken_as_the_title(self):
        from blueprints.library.scanner import LibraryScanner
        parsed = LibraryScanner.parse_filename("FT-[BD.FR].[Jije]-Valhardi-T08-L.affaire.Barnes.zip")
        self.assertIn("Valhardi", parsed["title"])


class LeadingVolumeNumberTest(unittest.TestCase):
    """Cas Blake et Mortimer T26 importé en tome 2: le numéro qui suit le titre l emporte."""

    def test_number_right_after_the_series_title_wins_over_a_subtitle_tome(self):
        from blueprints.library.scanner import LibraryScanner
        for name, volume in (("Blake Et Mortimer-26-La Vallée Des Immortels - T2 - Le Millième Bras Du Mékong (Edition Bibliophile).cbz", 26),
                             ("Blake Et Mortimer-20-La Malédiction Des Trente Deniers - Tome 02 - La Porte D Or.cbz", 20),
                             ("X-13 Agent Secret - 187 - (1968-08) (Guymauve).cbr", 187),
                             ("14-18 - Corbeyran-Le Roux - 05 - Le colosse d ébène.cbr", 5),
                             ("Tanguy et Laverdure - 03 - Le Vol 501.cbr", 3),
                             ("Blake Et Mortimer-02-Le Secret De L Espadon 2.cbz", 2),
                             ("Thorgal - Tome 03.cbz", 3)):
            self.assertEqual(LibraryScanner.parse_filename(name)["volume"], volume, name)

    def test_t2_and_tome_2_are_the_same_in_album_titles(self):
        self.assertEqual(routes._album_words("La Vallée des Immortels - Tome 2 - Le Millième Bras du Mékong"),
                         routes._album_words("La Vallee Des Immortels - T2 - Le Millieme Bras Du Mekong"))


class UnnumberedSpecialAlbumsTest(unittest.TestCase):
    """Cas Blake et Mortimer: 4 hors-séries sans numéro ignorés comme « doublons » du premier."""

    def setUp(self):
        import sqlite3
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript("""
            CREATE TABLE series (id INTEGER PRIMARY KEY, title TEXT);
            CREATE TABLE volumes (id INTEGER PRIMARY KEY, series_id INTEGER, volume_number INTEGER, is_bis INTEGER DEFAULT 0,
                is_integral INTEGER DEFAULT 0, integral_number INTEGER, is_hs INTEGER DEFAULT 0, hs_number INTEGER,
                is_episode INTEGER DEFAULT 0, episode_number INTEGER, is_special INTEGER DEFAULT 0, special_label TEXT,
                filename TEXT, filepath TEXT, file_size INTEGER, format TEXT, comicinfo TEXT);
            INSERT INTO series VALUES (715, \x27Blake et Mortimer (Les Aventures de)\x27);
            INSERT INTO volumes (id, series_id, is_hs, filename, filepath, file_size, format)
                VALUES (1, 715, 1, \x27Blake Et Mortimer-HS-Le Rayon U.cbz\x27, \x27/BD/hs-rayon.cbz\x27, 100, \x27cbz\x27);
        """)
        self.addCleanup(self.conn.close)

    def find(self, name):
        return routes._find_existing_volume_for_import(self.conn.cursor(), 715, {"is_hs": True, "hs_number": None}, source_name=name)

    def test_another_unnumbered_special_is_a_new_album(self):
        self.assertIsNone(self.find("Blake Et Mortimer-HS-Le Théâtre Du Mystère.pdf")[0])
        self.assertIsNone(self.find("Blake Et Mortimer-HS-L Aventure Immobile.pdf")[0])

    def test_the_same_special_is_still_recognised(self):
        self.assertEqual(self.find("Blake et Mortimer - HS - Le Rayon U (édition 2020).cbz")[0], 1)


class LeadingTomePrefixTest(unittest.TestCase):
    def test_t_prefixed_leading_number_with_inverted_series_title(self):
        from blueprints.library.scanner import LibraryScanner
        name = "Blake Et Mortimer (Les Aventures De) - T12 - Les 3 Formules Du Professeur Satô - Tome 2 (Edgar Pierre Jacobs) (1990) (TONER 3199px).cbz"
        self.assertEqual(LibraryScanner.parse_filename(name)["volume"], 12)
        self.assertEqual(LibraryScanner.parse_filename("Empire USA - T07 - Saison 2 - Tome 1.cbz")["volume"], 7)


class MainAlbumTitleFallbackTest(unittest.TestCase):
    ROWS = [
        (1, json.dumps({"title": "Le Secret de l\x27Espadon - Tome 1"})),
        (2, json.dumps({"title": "Le Secret de l\x27Espadon - Tome 2"})),
        (25, json.dumps({"title": "La Vallée des Immortels - Tome 1 - Menace sur Hong Kong"})),
        (26, json.dumps({"title": "La Vallée des Immortels - Tome 2 - Le Millième Bras du Mékong"})),
        (27, json.dumps({"title": "Le Cri du Moloch"})),
    ]

    def _pick(self, volume, name):
        from blueprints.library import routes
        common = routes._album_words("Blake et Mortimer") | routes._ALBUM_TITLE_STOPWORDS
        return routes._volume_from_main_album_title(volume, self.ROWS, routes._album_words(name), common)

    def test_other_album_title_with_tome_tiebreak(self):
        self.assertEqual(self._pick(1, "Blake et Mortimer - T1 - La vallée des Immortels"), 25)
        self.assertEqual(self._pick(2, "Blake et Mortimer - T2 - La vallée des Immortels"), 26)
        self.assertEqual(self._pick(1, "Blake.et.Mortimer.T01.Menace.sur.Hong.Kong"), 25)

    def test_keeps_volume_when_name_matches_its_album_or_has_no_title(self):
        self.assertIsNone(self._pick(1, "Blake et Mortimer - T1 - Le Secret de l Espadon"))
        self.assertIsNone(self._pick(3, "Blake et Mortimer T03"))
        self.assertIsNone(self._pick(None, "Le Cri du Moloch"))
