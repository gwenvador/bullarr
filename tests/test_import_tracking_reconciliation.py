from pathlib import Path


def test_reconciliation_never_marks_completed_or_pending_downloads_imported_from_an_existing_volume():
    source = (Path(__file__).resolve().parents[1] / 'blueprints' / 'missing_monitor' / 'downloader.py').read_text()
    start = source.index('def reconcile_stale_active_downloads()')
    end = source.index('\ndef update_download_progress', start)
    reconciliation = source[start:end]
    assert "WHERE ad.status IN ('importing')" in reconciliation
    assert "WHERE ad.status IN ('completed', 'importing', 'pending')" not in reconciliation
