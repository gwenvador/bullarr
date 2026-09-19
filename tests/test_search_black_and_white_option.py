import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / 'static' / 'js' / 'search-results-table.js'
CSS = ROOT / 'static' / 'css' / 'style-library-search.css'
BASE_CSS = ROOT / 'static' / 'css' / 'style.css'


def run_node(script):
    return subprocess.run(['node', '-e', script], cwd=ROOT, text=True, capture_output=True)


def test_black_and_white_detector_recognizes_requested_names():
    script = f"""
const fs = require('fs'), vm = require('vm');
const source = fs.readFileSync({str(JS)!r}, 'utf8');
const context = {{ console, fetch: () => Promise.resolve({{ ok: true, json: () => Promise.resolve({{}}) }}), document: {{ getElementById: () => null }}, window: {{}}, localStorage: {{ getItem: () => null, setItem: () => {{}} }} }};
vm.createContext(context); vm.runInContext(source, context);
const names = [
  'Chroniques diplomatiques - T01 (N&B) - Iran 1953 (Toner).cbz',
  'Chroniques_diplomatiques_T01_Iran_1953_Noir_&_Blanc@BD_fr.cbz',
  'Chroniques.Diplomatiques.NB.T01.2021.FRENCH.HYBRiD.COMiC.CBZ.eBook-TONER.cbz',
  'Chroniques diplomatiques - T01 (N&B) - Iran 1953 (Roulot-Simon) (Toner) 3274.cbz'
];
if (!names.every(name => context._isBlackAndWhiteSearchResult({{ title: name, name }}))) process.exit(1);
if (context._isBlackAndWhiteSearchResult({{ title: 'Chroniques diplomatiques T01 (Toner).cbz' }})) process.exit(3);
context._setSearchAvoidBlackAndWhitePriority(true);
const color = {{ source: 'ebdz', title: 'Chroniques diplomatiques T01 couleur.cbz' }};
const bw = {{ source: 'ebdz', title: names[0] }};
if (context.compareSearchResults(color, bw) >= 0) process.exit(2);
"""
    result = run_node(script)
    assert result.returncode == 0, result.stderr or result.stdout


def test_search_table_keeps_columns_and_wraps_long_filenames():
    source = CSS.read_text()
    assert 'min-width: 1100px' in BASE_CSS.read_text()
    assert '.replace-results-table .replace-results-filename' in source
    assert 'overflow-wrap: anywhere' in source
    assert 'display: table' in source
    assert 'display: table-row-group' in source
    assert 'display: table-cell' in source

    source = JS.read_text()
    assert 'Ne pas prioriser les formats Noir & Blanc' in source
    assert 'search-results-filter-black-and-white' in source
    assert "'blackAndWhite'" in source




def test_search_result_tbody_keeps_rows_when_asynchronous_refresh_rerenders():
    source = JS.read_text()
    assert "range.createContextualFragment(String(markup || ''))" in source
    assert '_replaceSearchResultsTbody(tbody' in source



def test_black_and_white_filter_selects_only_matching_results():
    script = f"""
const fs = require('fs'), vm = require('vm');
let source = fs.readFileSync({str(Path('/tmp/bullarr-rewrite/static/js/search-results-table.js'))!r}, 'utf8');
source = source.replace('let _searchTableAllResults = [];', 'var _searchTableAllResults = [];')
               .replace('let _searchTableFilters', 'var _searchTableFilters');
const context = {{ console, fetch: () => Promise.resolve({{ ok: true, json: () => Promise.resolve({{}}) }}), document: {{ getElementById: () => null }}, window: {{}}, localStorage: {{ getItem: () => null, setItem: () => {{}} }} }};
vm.createContext(context); vm.runInContext(source, context);
context._searchTableAllResults = [
  {{ source: 'ebdz', title: 'Album couleur.cbz' }},
  {{ source: 'ebdz', title: 'Album (N&B).cbz' }},
  {{ source: 'ebdz', title: 'Album Noir_&_Blanc.cbz' }}
];
context._applySearchTableFilter('blackAndWhite', 'yes');
if (context._filteredSearchTableResults().length !== 2) process.exit(1);
context._applySearchTableFilter('blackAndWhite', 'no');
if (context._filteredSearchTableResults().length !== 1) process.exit(2);
const ebdzFilename = {{ source: 'ebdz', title: 'Chroniques diplomatiques', filename: 'Chroniques_diplomatiques_T01_Noir_&_Blanc.cbz' }};
if (!context._isBlackAndWhiteSearchResult(ebdzFilename)) process.exit(3);
"""
    result = run_node(script)
    assert result.returncode == 0, result.stderr or result.stdout



def test_album_search_modal_preserves_inline_filter_handlers():
    library_js = (ROOT / 'static' / 'js' / 'library.js').read_text()
    assert 'searchModalBody.innerHTML = html;' in library_js



def test_album_search_keeps_owned_column_for_single_volume_searches():
    source = JS.read_text()
    assert "const ownedColumnHeaderHtml = seriesId" in source
    assert "if (seriesId && !preserveState)" in source


def test_black_and_white_preference_is_persisted_and_series_column_is_hidden_by_default():
    config = (ROOT / 'config.py').read_text()
    routes = (ROOT / 'blueprints' / 'library' / 'routes.py').read_text()
    settings_html = (ROOT / 'templates' / 'settings.html').read_text()
    settings_js = (ROOT / 'static' / 'js' / 'settings.js').read_text()
    library_js = (ROOT / 'static' / 'js' / 'library.js').read_text()
    assert "'prioritize_black_and_white': False" in config
    assert "config['prioritize_black_and_white'] = bool(data['prioritize_black_and_white'])" in routes
    assert 'id="prioritizeBlackAndWhite"' in settings_html
    assert 'saveBlackAndWhiteSearchPriority' in settings_js
    assert "key: 'blackAndWhite'" in library_js
    assert "let visibleVolumeTableColumns = new Set(JSON.parse(localStorage.getItem('volumeTableVisibleColumns') || '[]'));" in library_js


def test_single_volume_album_search_keeps_owned_column():
    source = JS.read_text()
    assert 'const ownedColumnHeaderHtml = seriesId' in source
    assert 'if (seriesId && !preserveState)' in source


def test_metadata_toolbar_does_not_render_codeql_suppression_text():
    library_js = (ROOT / 'static' / 'js' / 'library.js').read_text()
    suppression = 'lgtm [js/bad-code-sanitization]'
    toolbar_start = library_js.index('const toolbarHtml = `')
    toolbar_end = library_js.index('cleanupDetachedDropdownMenus();', toolbar_start)
    # The suppression belongs immediately before DOM sinks, never in markup that
    # becomes visible metadata-toolbar text.
    assert suppression not in library_js[toolbar_start:toolbar_end]
    assert library_js.count(suppression) == 2
    assert f'// {suppression} values are escaped for the exact HTML/JavaScript context before this fixed template is inserted.\n        modalBody.innerHTML =' in library_js


def test_noir_blanc_column_option_is_last_in_the_table_settings_menu():
    source = (ROOT / 'static' / 'js' / 'library.js').read_text()
    start = source.index('const VOLUME_TABLE_OPTIONAL_COLUMNS = [')
    end = source.index('];', start)
    options = source[start:end]
    assert options.rfind("key: 'blackAndWhite'") > options.rfind("key: 'releaser'")
