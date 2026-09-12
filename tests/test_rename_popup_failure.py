from pathlib import Path

def test_rename_popup_marks_failed_operation_with_cross():
    source = Path("/home/gwen/docker/bullarr-wt-rename-popup/static/js/library.js").read_text()
    assert "❌ Renommage échoué" in source
    assert "const renameFailed" in source
