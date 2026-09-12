from pathlib import Path

def test_folder_placement_section_has_collapse_control():
    source = (Path(__file__).resolve().parents[1] / "templates/verification.html").read_text()
    assert 'id="folderPlacementToggle"' in source
    assert "verifToggleSection('misplacedFoldersList', this)" in source
