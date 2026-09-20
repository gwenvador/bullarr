import tempfile
import unittest
from pathlib import Path

from blueprints.library.routes import _folder_has_subdirectories


class NestedFolderSafetyTests(unittest.TestCase):
    def test_flat_download_folder_is_not_nested(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "volume.cbz").write_bytes(b"x")
            self.assertFalse(_folder_has_subdirectories(str(root)))

    def test_download_folder_with_subdirectory_requires_manual_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "volume.cbz").write_bytes(b"x")
            (root / "Spin-off").mkdir()
            (root / "Spin-off" / "other.cbz").write_bytes(b"x")
            self.assertTrue(_folder_has_subdirectories(str(root)))


if __name__ == "__main__":
    unittest.main()
