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
  'Chroniques_diplomatiques_T01_Iran_1953_Noir_&_Blanc@BD_fr.cbz'
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

    source = JS.read_text()
    assert 'Ne pas prioriser les formats Noir & Blanc' in source
    assert 'search-results-filter-black-and-white' in source
    assert "'blackAndWhite'" in source
