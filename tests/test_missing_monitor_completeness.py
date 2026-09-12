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


def test_scanner_extends_numbered_album_sequence_to_known_total():
    source = (Path(__file__).resolve().parents[1] / 'blueprints/library/scanner.py').read_text()
    assert 'max(max_vol, max(bedetheque_album_numbers), bedetheque_total or 0)' in source


def test_library_uses_server_completeness_when_current_collection_is_complete():
    source = (Path(__file__).resolve().parents[1] / 'static/js/library.js').read_text()
    assert 'if (isFullyOwned && !hasMissingVolumes)' in source


def test_incomplete_unclassified_items_expose_expected_missing_range():
    source = (Path(__file__).resolve().parents[1] / 'blueprints/library/scanner.py').read_text()
    assert 'owned_main_item_count < bedetheque_total and not missing_volumes' in source
