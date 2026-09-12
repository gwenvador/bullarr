from pathlib import Path


def test_manual_bedetheque_match_rescans_series_before_loading_match():
    source = (Path(__file__).resolve().parents[1] / "blueprints/bedetheque/routes.py").read_text()
    start = source.index("def enrich_series(series_id):")
    end = source.index("def _get_series_missing_match", start)
    section = source[start:end]
    assert "LibraryScanner(current_app.config['DATABASE']).scan_single_series(series_id)" in section
    assert section.index("scan_single_series(series_id)") < section.index("cursor.execute('SELECT title FROM series")
