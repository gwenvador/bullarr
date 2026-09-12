import tarfile
import zipfile
from pathlib import Path

import pytest

from archive_utils import list_archive_members

def test_lists_nested_zip_members_without_extracting(tmp_path):
    archive = tmp_path / "pack.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Cubitus/01/cover.jpg", b"x")
        zf.writestr("Cubitus/01/info.nfo", b"info")
    result = list_archive_members(archive)
    assert result["format"] == "zip"
    assert result["entries"] == [{"path": "Cubitus/01/cover.jpg", "kind": "file", "size": 1}, {"path": "Cubitus/01/info.nfo", "kind": "file", "size": 4}]
    assert not (tmp_path / "Cubitus").exists()

def test_lists_tar_directories_and_files(tmp_path):
    archive = tmp_path / "pack.tar"
    folder = tmp_path / "source" / "Cubitus"
    folder.mkdir(parents=True)
    (folder / "01.jpg").write_bytes(b"page")
    with tarfile.open(archive, "w") as tf:
        tf.add(tmp_path / "source", arcname="Cubitus")
    result = list_archive_members(archive)
    assert result["format"] == "tar"
    assert {entry["path"] for entry in result["entries"]} == {"Cubitus", "Cubitus/Cubitus", "Cubitus/Cubitus/01.jpg"}
    assert next(e for e in result["entries"] if e["path"] == "Cubitus")["kind"] == "directory"

def test_rejects_unsupported_archive(tmp_path):
    path = tmp_path / "not-an-archive.bin"
    path.write_bytes(b"nope")
    with pytest.raises(ValueError, match="non supporté"):
        list_archive_members(path)
