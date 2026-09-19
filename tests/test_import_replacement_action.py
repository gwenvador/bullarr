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


def test_ebdz_add_flows_confirm_db_conflict_before_download():
    for source_path in (Path("static/js/library.js"), Path("static/js/search.js")):
        source = source_path.read_text()
        start = source.index("async function addToEmule(")
        end = source.index("async function checkEmuleStatus(", start)
        assert "await confirmReplacementBeforeDownload(" in source[start:end]
        for function in ("addTorrentToQbittorrent", "addTorrentToClient", "downloadTelegramFile", "downloadFourtoutici"):
            fn_start = source.index(f"async function {function}(")
            assert "await confirmReplacementBeforeDownload(" in source[fn_start:fn_start + 500]
        assert "/api/import/replacement-required" in source

def test_replacement_check_endpoint_uses_database_volume_lookup():
    source = Path("blueprints/library/routes.py").read_text()
    assert "@library_bp.route('/api/import/replacement-required', methods=['POST'])" in source
    assert "_find_existing_volume_for_import(" in source
