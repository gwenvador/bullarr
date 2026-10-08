import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from archive_utils import detect_actual_format, list_archive_members


class ArchiveContentListingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_lists_nested_zip_members_without_extracting(self):
        archive = self.tmp_path / "pack.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("Cubitus/01/cover.jpg", b"x")
            zf.writestr("Cubitus/01/info.nfo", b"info")
        result = list_archive_members(archive)
        self.assertEqual(result["format"], "zip")
        self.assertEqual(result["entries"], [
            {"path": "Cubitus/01/cover.jpg", "kind": "file", "size": 1},
            {"path": "Cubitus/01/info.nfo", "kind": "file", "size": 4},
        ])
        self.assertFalse((self.tmp_path / "Cubitus").exists())

    def test_lists_tar_directories_and_files(self):
        archive = self.tmp_path / "pack.tar"
        folder = self.tmp_path / "source" / "Cubitus"
        folder.mkdir(parents=True)
        (folder / "01.jpg").write_bytes(b"page")
        with tarfile.open(archive, "w") as tf:
            tf.add(self.tmp_path / "source", arcname="Cubitus")
        result = list_archive_members(archive)
        self.assertEqual(result["format"], "tar")
        self.assertEqual({entry["path"] for entry in result["entries"]},
                         {"Cubitus", "Cubitus/Cubitus", "Cubitus/Cubitus/01.jpg"})
        self.assertEqual(next(e for e in result["entries"] if e["path"] == "Cubitus")["kind"], "directory")

    def test_rejects_unsupported_archive(self):
        path = self.tmp_path / "not-an-archive.bin"
        path.write_bytes(b"nope")
        with self.assertRaisesRegex(ValueError, "non supporté"):
            list_archive_members(path)

    def test_listing_is_truncated_at_the_limit_but_reports_the_total(self):
        archive = self.tmp_path / "many.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            for index in range(5):
                zf.writestr(f"page{index}.jpg", b"x")
        result = list_archive_members(archive, limit=2)
        self.assertEqual((len(result["entries"]), result["total_count"], result["truncated"]), (2, 5, True))

    def test_a_zip_named_cbr_is_detected_as_cbz_and_declared_formats_are_normalised(self):
        fake_cbr = self.tmp_path / "mislabeled.cbr"
        with zipfile.ZipFile(fake_cbr, "w") as zf:
            zf.writestr("01.jpg", b"x")
        self.assertEqual(detect_actual_format(str(fake_cbr), "CBR"), "cbz")
        real_cbz = self.tmp_path / "real.cbz"
        with zipfile.ZipFile(real_cbz, "w") as zf:
            zf.writestr("01.jpg", b"x")
        self.assertEqual(detect_actual_format(str(real_cbz), "CBZ"), "cbz")
        self.assertEqual(detect_actual_format(str(self.tmp_path / "missing.pdf"), "PDF"), "pdf")
        self.assertEqual(detect_actual_format(str(real_cbz), None), "")

    def test_a_tar_named_cbz_is_detected_as_tar(self):
        archive = self.tmp_path / "mislabeled.cbz"
        source = self.tmp_path / "page.jpg"
        source.write_bytes(b"page")
        with tarfile.open(archive, "w") as tf:
            tf.add(source, arcname="page.jpg")
        self.assertEqual(detect_actual_format(str(archive), "cbz"), "tar")


if __name__ == '__main__':
    unittest.main()
