import os
import tempfile
import unittest
import zipfile

from flask import Flask

from blueprints.library import library_bp
from blueprints.library import routes


def make_zip(path, names):
    with zipfile.ZipFile(path, "w") as archive:
        for name in names:
            archive.writestr(name, b"x")


class SinglePackagedComicTest(unittest.TestCase):
    """Un ZIP de pages = un album; un ZIP avec un dossier d images par tome = un PACK."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def check(self, names):
        path = os.path.join(self.tmp.name, "a.zip")
        make_zip(path, names)
        return routes._zip_is_single_packaged_comic(path)

    def test_flat_or_single_folder_pages_are_one_album(self):
        self.assertTrue(self.check(["p001.jpg", "p002.jpg"]))
        self.assertTrue(self.check(["Album/p001.jpg", "Album/p002.png"]))
        self.assertTrue(self.check(["Serie/", "Serie/p001.jpg", "Serie/p002.jpg"]))

    def test_one_folder_per_volume_is_a_pack_not_one_album(self):
        self.assertFalse(self.check([
            "L age de Bronze/01 - Un millier de navires/T01 - Page 001.jpg",
            "L age de Bronze/02 - Le Sacrifice/T02 - Page 001.jpg",
            "L age de Bronze/HS - Les coulisses/HS - Page 001.jpg",
        ]))

    def test_mixed_content_empty_or_unreadable_is_not_a_single_album(self):
        self.assertFalse(self.check(["p001.jpg", "tome2.cbz"]))
        self.assertFalse(self.check(["dossier/"]))
        bad = os.path.join(self.tmp.name, "bad.zip")
        open(bad, "wb").write(b"not a zip")
        self.assertFalse(routes._zip_is_single_packaged_comic(bad))


class ArchiveContentRouteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(os.path.join(self.tmp.name, "imports"))
        os.makedirs(self.root)
        make_zip(os.path.join(self.root, "pack.zip"), ["01/p1.jpg", "02/p1.jpg"])
        open(os.path.join(self.root, "notes.txt"), "w").write("x")
        outside = os.path.join(self.tmp.name, "outside.zip")
        make_zip(outside, ["a.jpg"])
        app = Flask(__name__)
        app.config["IMPORT_DIRECTORIES"] = [self.root]
        app.register_blueprint(library_bp)
        self.client = app.test_client()

    def get(self, **params):
        return self.client.get("/api/import/archive-content", query_string=params)

    def test_lists_the_archive_without_extracting_it(self):
        response = self.get(import_root=self.root, relative_path="pack.zip")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual((data["success"], data["format"], data["total_count"], data["filename"]), (True, "zip", 2, "pack.zip"))
        self.assertEqual(sorted(os.listdir(self.root)), ["notes.txt", "pack.zip"])

    def test_errors_are_json_never_html(self):
        cases = [({}, 400), (dict(import_root=self.root, relative_path="absent.zip"), 404),
                 (dict(import_root=self.root, relative_path="notes.txt"), 422),
                 (dict(import_root=self.tmp.name, relative_path="outside.zip"), 403),
                 (dict(import_root=self.root, relative_path="../outside.zip"), 403)]
        for params, status in cases:
            response = self.get(**params)
            self.assertEqual(response.status_code, status, params)
            self.assertTrue(response.is_json, params)
            self.assertIn("error", response.get_json())


if __name__ == "__main__":
    unittest.main()


class ContainerArchiveIdentityTest(unittest.TestCase):
    """Un pack ne doit pas apparaître comme un album choisi (cas « 01 à 03 + HS »)."""

    def test_pack_download_and_multi_folder_zip_are_containers_single_album_zip_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack = os.path.join(tmp, "pack.zip")
            make_zip(pack, ["01/p1.jpg", "02/p1.jpg"])
            album = os.path.join(tmp, "album.zip")
            make_zip(album, ["p1.jpg", "p2.jpg"])
            self.assertTrue(routes._is_container_archive(pack, ".zip", None))
            self.assertFalse(routes._is_container_archive(album, ".zip", None))
            self.assertTrue(routes._is_container_archive(album, ".zip", {"is_pack": True}))
            self.assertTrue(routes._is_container_archive("x.rar", ".rar", None))
            self.assertFalse(routes._is_container_archive("x.cbz", ".cbz", {"is_pack": True}))

    def test_container_keeps_the_series_but_loses_every_album_identity(self):
        parsed = {"title": "L age de Bronze", "volume": 1, "is_hs": True, "hs_number": 2, "is_integral": True}
        destination = {"series_id": 1336, "tracking_id": 4696, "volume_number": 3, "volume_id": 9,
                       "is_hs": True, "is_pack": True}
        cleaned = routes._strip_container_identity(parsed, destination)
        self.assertEqual(cleaned, {"series_id": 1336, "tracking_id": 4696, "is_pack": True})
        self.assertEqual((parsed["volume"], parsed["is_hs"], parsed["hs_number"], parsed["is_integral"], parsed["is_pack"]),
                         (None, False, None, False, True))
        self.assertEqual(destination["volume_number"], 3)           # la destination d origine n est pas modifiée
        self.assertIsNone(routes._strip_container_identity({}, None))


class PackageFoldersTest(unittest.TestCase):
    """« Voir le contenu » -> cocher des dossiers -> un CBZ par dossier, source intacte."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.archive = os.path.join(self.tmp.name, "pack.zip")
        make_zip(self.archive, ["Serie/01 - Un/p1.jpg", "Serie/01 - Un/p2.jpg", "Serie/02 - Deux/p1.png",
                                "Serie/HS - Extra/p1.jpg", "Serie/HS - Extra/notes.txt"])
        self.out = os.path.join(self.tmp.name, "out")

    def test_each_selected_folder_becomes_a_cbz_with_only_its_images(self):
        from blueprints.library.zip_converter import package_zip_folders_to_cbz
        created = package_zip_folders_to_cbz(self.archive, self.out, ["Serie/01 - Un", "Serie/HS - Extra"])
        self.assertEqual([(os.path.basename(c["path"]), c["file_count"]) for c in created],
                         [("01 - Un.cbz", 2), ("HS - Extra.cbz", 1)])
        with zipfile.ZipFile(created[1]["path"]) as cbz:
            self.assertEqual(cbz.namelist(), ["p1.jpg"])
        self.assertTrue(os.path.exists(self.archive))

    def test_a_parent_folder_expands_to_its_album_folders(self):
        from blueprints.library.zip_converter import package_zip_folders_to_cbz
        created = package_zip_folders_to_cbz(self.archive, self.out, ["Serie"])
        self.assertEqual(sorted(c["folder"] for c in created), ["Serie/01 - Un", "Serie/02 - Deux", "Serie/HS - Extra"])

    def test_nothing_selected_or_no_images_is_an_error(self):
        from blueprints.library.zip_converter import package_zip_folders_to_cbz, ZipConversionError
        with self.assertRaises(ZipConversionError):
            package_zip_folders_to_cbz(self.archive, self.out, [])
        with self.assertRaises(ZipConversionError):
            package_zip_folders_to_cbz(self.archive, self.out, ["Serie/absent"])


class PackageRouteTest(ArchiveContentRouteTest):
    def test_route_packages_into_the_private_temp_dir_and_returns_json_errors(self):
        from unittest import mock
        out = os.path.join(self.tmp.name, "private-out")
        with mock.patch.object(routes, "_PACKAGE_TEMP_DIR", out),                 mock.patch("blueprints.library.import_history.mark_import_file_manual") as mark,                 mock.patch("blueprints.missing_monitor.downloader.get_trackable_active_downloads", return_value=[]):
            ok = self.client.post("/api/import/archive-package-folders", json={
                "import_root": self.root, "relative_path": "pack.zip", "folder_paths": ["01"]})
            self.assertEqual(ok.status_code, 200)
            created = ok.get_json()["created"]
            self.assertEqual(os.path.dirname(created[0]["path"]), out)
            mark.assert_called_once()
            bad = [({"import_root": self.tmp.name, "relative_path": "outside.zip", "folder_paths": ["a"]}, 403),
                   ({"import_root": self.root, "relative_path": "../outside.zip", "folder_paths": ["a"]}, 403),
                   ({"import_root": self.root, "relative_path": "absent.zip", "folder_paths": ["a"]}, 404),
                   ({"import_root": self.root, "relative_path": "pack.zip", "folder_paths": []}, 422)]
            for body, status in bad:
                response = self.client.post("/api/import/archive-package-folders", json=body)
                self.assertEqual(response.status_code, status, body)
                self.assertTrue(response.is_json)


class ContainerScanEntryTest(unittest.TestCase):
    """Le pack apparaît comme placeholder (is_container), ses CBZ comme membres du pack."""

    def scan_entry(self, tmp, name, names, destination):
        from blueprints.library.scanner import LibraryScanner
        path = os.path.join(tmp, name)
        make_zip(path, names)
        app = Flask(__name__)
        app.config["IMPORT_DIRECTORIES"] = [tmp]
        app.config["DATABASE"] = os.path.join(tmp, "none.db")
        files = []
        with app.app_context():
            routes._append_scanned_file(path, tmp, name, destination, LibraryScanner(), set(),
                                        {"auto_import_enabled": True}, files, pack_download_id=4696, validate_file=False)
        return files[0]

    def test_pack_zip_is_a_non_importable_placeholder_without_album_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = self.scan_entry(tmp, "Comics.FR.-.L age.de.Bronze.-.01.a.03.+.HS.-.BDPACK.zip",
                                    ["01/p1.jpg", "02/p1.jpg", "HS/p1.jpg"],
                                    {"series_id": 1336, "tracking_id": 4696, "is_pack": True})
        self.assertTrue(entry["is_container"])
        self.assertEqual(entry["pack_download_id"], 4696)
        self.assertIsNone(entry["parsed"]["volume"])
        self.assertFalse(entry["parsed"]["is_hs"])
        self.assertIn("pack", entry["auto_import_skip_reason"])
        self.assertNotIn("volume_number", entry["destination"] or {})

    def test_single_album_zip_is_not_a_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = self.scan_entry(tmp, "Album - 05.zip", ["p1.jpg", "p2.jpg"], {"series_id": 7, "volume_number": 5})
        self.assertFalse(entry["is_container"])
        self.assertEqual(entry["parsed"]["volume"], 5)


class PackagedFileLifecycleTest(unittest.TestCase):
    """Un CBZ empaqueté (hors répertoires d import) doit pouvoir être supprimé, comme les autres."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.imports = os.path.realpath(os.path.join(self.tmp.name, "imports"))
        self.package_dir = os.path.realpath(os.path.join(self.tmp.name, "package-temp"))
        os.makedirs(self.imports)
        os.makedirs(self.package_dir)
        self.cbz = os.path.join(self.package_dir, "01 - Un millier de navires (2).cbz")
        make_zip(self.cbz, ["p1.jpg"])
        app = Flask(__name__)
        app.config["IMPORT_DIRECTORIES"] = [self.imports]
        app.register_blueprint(library_bp)
        self.app = app

    def test_allowed_roots_include_the_package_dir(self):
        from unittest import mock
        with mock.patch.object(routes, "_PACKAGE_TEMP_DIR", self.package_dir), self.app.app_context():
            roots = routes._allowed_import_roots()
        self.assertEqual(roots, [self.imports, self.package_dir])

    def test_delete_accepts_a_packaged_file_and_still_refuses_other_directories(self):
        from unittest import mock
        client = self.app.test_client()
        with mock.patch.object(routes, "_PACKAGE_TEMP_DIR", self.package_dir):
            refused = client.delete("/api/import/file", json={"import_root": self.tmp.name, "relative_path": "package-temp/x.cbz"})
            self.assertEqual(refused.status_code, 403)
            ok = client.delete("/api/import/file", json={
                "import_root": self.package_dir, "relative_path": os.path.basename(self.cbz),
                "filename": os.path.basename(self.cbz), "client": ""})
        self.assertEqual(ok.status_code, 200, ok.get_data(as_text=True))
        self.assertFalse(os.path.exists(self.cbz))


class CompletePackTest(unittest.TestCase):
    """« Valider le pack »: clôture le téléchargement et masque l archive de /import."""

    def setUp(self):
        import sqlite3
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "bullarr.db")
        self.imports = os.path.realpath(os.path.join(self.tmp.name, "imports"))
        os.makedirs(self.imports)
        self.archive = os.path.join(self.imports, "pack.zip")
        make_zip(self.archive, ["01/p1.jpg", "02/p1.jpg"])
        conn = sqlite3.connect(self.db)
        conn.executescript(
            "CREATE TABLE series (id INTEGER PRIMARY KEY, title TEXT);"
            "CREATE TABLE active_downloads (id INTEGER PRIMARY KEY, status TEXT, is_pack INTEGER, series_id INTEGER);"
            "INSERT INTO series VALUES (1, \x27L Age de bronze\x27);"
            "INSERT INTO active_downloads VALUES (10, \x27completed\x27, 1, 1), (11, \x27completed\x27, 0, 1);")
        conn.commit()
        conn.close()
        app = Flask(__name__)
        app.config["DATABASE"] = self.db
        app.config["IMPORT_DIRECTORIES"] = [self.imports]
        app.register_blueprint(library_bp)
        self.app = app
        self.client = app.test_client()

    def status(self, download_id):
        import sqlite3
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT status FROM active_downloads WHERE id = ?", (download_id,)).fetchone()[0]
        finally:
            conn.close()

    def post(self, **body):
        from unittest import mock
        with mock.patch("blueprints.library.import_history.log_import_operation"),                 mock.patch("blueprints.library.import_history.update_import_operation") as update_op, mock.patch("blueprints.library.import_history.log_import_file") as log_file,                 mock.patch("blueprints.missing_monitor.downloader._set_pending_download_status",
                           side_effect=lambda i, st: self._set(i, st)):
            response = self.client.post("/api/import/pack/complete", json=body)
        self.update_op = update_op
        return response, log_file

    def _set(self, download_id, status):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE active_downloads SET status = ? WHERE id = ?", (status, download_id))
        conn.commit()
        conn.close()

    def test_validation_marks_the_pack_imported_hides_the_archive_and_keeps_it_on_disk(self):
        response, log_file = self.post(download_id=10, archive_paths=[self.archive])
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(self.status(10), "imported")
        args = log_file.call_args[0]
        self.assertEqual((args[3], args[5], args[6]), ("", "skipped", "success"))      # masque le fichier source
        self.assertEqual(args[2], self.archive)
        self.assertEqual(self.update_op.call_args[0][1:], ("completed", 0, 0, 1, 0))    # compteurs de l'historique
        self.assertTrue(os.path.exists(self.archive))                                  # jamais supprimée
        response, _ = self.post(download_id=10, archive_paths=[self.archive], delete_archive=True)
        self.assertEqual(response.get_json(), {"success": True})
        self.assertTrue(os.path.exists(self.archive))                                  # l option n existe plus

    def test_refuses_non_packs_unknown_downloads_and_foreign_paths(self):
        self.assertEqual(self.post(download_id=11, archive_paths=[])[0].status_code, 400)
        self.assertEqual(self.post(download_id=99, archive_paths=[])[0].status_code, 404)
        self.assertEqual(self.post(archive_paths=[])[0].status_code, 400)
        outside = os.path.join(self.tmp.name, "outside.zip")
        make_zip(outside, ["a.jpg"])
        self.assertEqual(self.post(download_id=10, archive_paths=[outside])[0].status_code, 403)
        self.assertEqual(self.status(10), "completed")

