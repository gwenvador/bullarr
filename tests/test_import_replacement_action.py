from pathlib import Path


def test_owned_album_conflict_exposes_an_explicit_manual_replace_action():
    source = (Path(__file__).resolve().parents[1] / 'static' / 'js' / 'import.js').read_text()
    assert 'Remplacer le fichier existant' in source
    assert "openDestinationModal(${index})" in source


def test_database_volume_flag_is_scoped_to_the_import_row_renderer():
    source = Path("static/js/import.js").read_text()
    start = source.index("function _importFileRowHtml(file, index)")
    end = source.index("function _pendingPackGroupRowHtml", start)
    renderer = source[start:end]
    assert "const hasDatabaseVolume = !!file.destination?.volume_id;" in renderer
