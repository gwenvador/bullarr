import tempfile
import unittest
from pathlib import Path

from blueprints.library.routes import _download_folder_identities, _folder_has_subdirectories


class NestedFolderSafetyTests(unittest.TestCase):
    def test_flat_download_folder_is_not_nested(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "volume.cbz").write_bytes(b"x")
            self.assertFalse(_folder_has_subdirectories(str(root)))

    def test_duplicate_qbittorrent_folder_suffix_is_tracked_as_same_pack(self):
        identities = _download_folder_identities(
            {
                "client": "qbittorrent",
                "client_item_id": "hash",
                "title": "ignored",
            },
            {"hash": "7.VIES.DE.LEPERVIER.INTEGRALE.FRENCH.BD.CBZ.Ebook-NoTag"},
        )
        self.assertIn(
            "7.vies.de.lepervier.integrale.french.bd.cbz.ebook-notag.2",
            identities,
        )

    def test_download_folder_with_subdirectory_requires_manual_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "volume.cbz").write_bytes(b"x")
            (root / "Spin-off").mkdir()
            (root / "Spin-off" / "other.cbz").write_bytes(b"x")
            self.assertTrue(_folder_has_subdirectories(str(root)))


if __name__ == "__main__":
    unittest.main()
