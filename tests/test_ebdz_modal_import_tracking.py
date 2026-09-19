from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIBRARY_JS = (ROOT / 'static' / 'js' / 'library.js')
IMPORT_JS = (ROOT / 'static' / 'js' / 'import.js')


def test_ebdz_series_files_download_keeps_series_and_album_context():
    source = LIBRARY_JS.read_text()
    # Opening another series must overwrite the modal context used by its Add button.
    assert 'modal.dataset.seriesId = seriesId;' in source
    start = source.index('async function loadEbdzThreadFiles(')
    end = source.index('// ===== KOMGA =====', start)
    modal_renderer = source[start:end]

    assert "const seriesId = Number(document.getElementById('ebdz-files-modal')?.dataset.seriesId) || null;" in modal_renderer
    assert "addToEmule('${escapeForAttribute(f.link)}', this, '${escapeForAttribute(decodedFilename)}', ${seriesId ?? 'null'}, null, ${volumeForAdd ?? 'null'}" in modal_renderer


def test_pending_downloads_do_not_offer_an_unreliable_import_now_action():
    source = IMPORT_JS.read_text()
    assert 'data-tooltip="Importer ce fichier maintenant"' not in source
    assert 'importPendingDownload(${pending.id}, this)' not in source


def test_ebdz_inline_add_handler_embeds_series_context_not_lexical_variable():
    source = Path("static/js/library.js").read_text()
    start = source.index("async function loadEbdzThreadFiles")
    end = source.index("function formatBytes", start)
    block = source[start:end]
    # Buttons use inline onclick attributes, which run in global scope and cannot
    # access this function's local `seriesId` or `volume` variables.
    assert "', seriesId, null, volume ?? null)" not in block
    assert "${seriesId ?? 'null'}" in block
    assert "${volumeForAdd ?? 'null'}" in block
