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


def test_replacement_confirmation_requires_an_existing_library_file_not_only_a_db_placeholder():
    source = Path("blueprints/library/routes.py").read_text()
    start = source.index("def import_replacement_required_route")
    end = source.index("def mark_import_file_manual_route", start)
    endpoint = source[start:end]
    assert "'has_file': bool(row and row[1] and os.path.isfile(row[1]))" in endpoint
    for source_path in (Path("static/js/library.js"), Path("static/js/search.js")):
        assert "!data.has_file" in source_path.read_text()


def test_explicit_replacement_search_still_requires_confirmation_when_a_file_exists():
    for source_path in (Path("static/js/library.js"), Path("static/js/search.js")):
        source = source_path.read_text()
        start = source.index("async function confirmReplacementBeforeDownload")
        end = source.index("async function addToEmule", start)
        helper = source[start:end]
        assert "if (!Number.isInteger(Number(seriesId))) return !!forceReplace;" in helper
        assert "window.confirm('Cet album existe déjà dans Bullarr" in helper
