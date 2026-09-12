from pathlib import Path


def test_missing_monitor_uses_effective_missing_volumes_for_filter_and_display():
    source = (Path(__file__).resolve().parents[1] / 'static/js/missing-monitor.js').read_text()
    assert 'function effectiveMissingVolumes(series)' in source
    assert 'filtered = filtered.filter(s => effectiveMissingVolumes(s).length > 0)' in source
    assert 'const effectiveMissingVols = effectiveMissingVolumes(s);' in source


def test_scanner_marks_unowned_bedetheque_one_shot_as_missing_album_one():
    source = (Path(__file__).resolve().parents[1] / 'blueprints/library/scanner.py').read_text()
    start = source.index("bedetheque_status_is_oneshot")
    section = source[start:start + 900]
    assert 'missing_volumes = [] if total_volumes > 0 else [1]' in section


def test_scanner_counts_episodes_as_owned_primary_items_and_uses_oneshot_label():
    source = (Path(__file__).resolve().parents[1] / 'blueprints/library/scanner.py').read_text()
    assert 'owned_main_album_count = cursor.fetchone()[0]' not in source
    section = source[source.index('def update_series_stats'):source.index('def get_library_stats')]
    assert 'owned_main_item_count' in section
    assert 'One-Shot' in section
