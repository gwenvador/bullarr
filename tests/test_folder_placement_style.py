from pathlib import Path

def test_folder_placement_matches_standard_header_and_right_controls():
    root = Path(__file__).resolve().parents[1]
    template = (root / "templates/verification.html").read_text()
    js = (root / "static/js/verification.js").read_text()
    assert 'id="folderPlacementToggle"' in template
    assert '<span>Emplacement des dossiers' in template
    assert 'id="verifRunMisplacedFoldersBtn"' in template
    assert 'verification-folder-universe-controls' in js
    assert 'verification-folder-universe-summary' in js
    assert "['misplacedFoldersList', 'folderPlacementToggle']" in js
