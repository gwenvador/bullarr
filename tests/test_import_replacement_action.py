from pathlib import Path


def test_owned_album_conflict_exposes_an_explicit_manual_replace_action():
    source = (Path(__file__).resolve().parents[1] / 'static' / 'js' / 'import.js').read_text()
    assert 'Remplacer le fichier existant' in source
    assert "openDestinationModal(${index})" in source
