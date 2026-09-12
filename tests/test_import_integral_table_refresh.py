from pathlib import Path

def test_active_import_rows_propagate_integral_fields_and_render_them():
    root = Path(__file__).resolve().parents[1]
    routes = (root / "blueprints/activity/routes.py").read_text()
    js = (root / "static/js/import.js").read_text()
    assert "item['is_integral'] = linked['is_integral']" in routes
    assert "item['integral_number'] = linked['integral_number']" in routes
    assert "_pendingVolumeLabel(item)" in js
