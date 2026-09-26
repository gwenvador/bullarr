from pathlib import Path


def test_missing_monitor_uses_effective_missing_volumes_for_filter_and_display():
    source = (Path(__file__).resolve().parents[1] / 'static/js/missing-monitor.js').read_text()
    assert 'function effectiveMissingVolumes(series)' in source
    assert 'filtered = filtered.filter(s => effectiveMissingVolumes(s).length > 0)' in source
    assert 'const effectiveMissingVols = effectiveMissingVolumes(s);' in source
