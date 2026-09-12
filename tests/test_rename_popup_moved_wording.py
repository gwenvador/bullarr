from pathlib import Path

def test_rename_popup_reports_moved_files_for_existing_destination():
    source = (Path(__file__).resolve().parents[1] / "static/js/library.js").read_text()
    assert "moved_files" in source
    assert "fichier(s) déplacé(s)" in source
