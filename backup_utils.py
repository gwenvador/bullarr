"""Consistent SQLite snapshots and restores applied before the web server starts."""

import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path


PENDING_RESTORE_DIR = '.pending_restore'
BACKUP_NAMES = frozenset({
    'bullarr.db', 'ebdz.db', '.encryption_key', 'emule_config.json',
    'ebdz_config.json', 'prowlarr_config.json', 'komga_config.json',
    'oidc_config.json', 'missing_monitor_config.json',
    'library_import_config.json', 'rename_config.json', 'rss_feeds.json',
    'qbittorrent_config.json',
})


def snapshot_sqlite(source, destination):
    """Copy a live SQLite database, including transactions still in its WAL."""
    source_uri = Path(source).resolve().as_uri() + '?mode=ro'
    live = sqlite3.connect(source_uri, uri=True, timeout=30)
    try:
        snapshot = sqlite3.connect(destination, timeout=30)
        try:
            live.backup(snapshot)
            snapshot.commit()
        finally:
            snapshot.close()
    finally:
        live.close()


def apply_pending_restore(data_dir):
    """Apply a validated restore before Gunicorn opens any database connection."""
    data_dir = Path(data_dir)
    pending = data_dir / PENDING_RESTORE_DIR
    if not pending.exists():
        return []
    names = json.loads((pending / 'manifest.json').read_text(encoding='utf-8'))['files']
    if not isinstance(names, list) or not names or len(names) != len(set(names)):
        raise ValueError('Invalid pending restore manifest')
    if any(name not in BACKUP_NAMES or not (pending / name).is_file() for name in names):
        raise ValueError('Pending restore is incomplete or contains unknown files')

    previous = pending / '.previous'
    previous.mkdir(exist_ok=True)
    installed = []
    moved = []
    try:
        for name in names:
            target = data_dir / name
            if name.endswith('.db') and target.exists():
                snapshot_sqlite(target, previous / f'{name}.snapshot')
            # A WAL from the old database must never be replayed on a restored one.
            old_paths = [target]
            if name.endswith('.db'):
                old_paths.extend([Path(str(target) + '-wal'), Path(str(target) + '-shm')])
            for old_path in old_paths:
                if old_path.exists():
                    saved = previous / old_path.name
                    os.replace(old_path, saved)
                    moved.append((saved, old_path))
            os.replace(pending / name, target)
            installed.append((target, pending / name))
            os.chmod(target, 0o600)
        for saved, old_path in moved:
            if old_path.name in names:
                backup_source = previous / f'{old_path.name}.snapshot' if old_path.suffix == '.db' else saved
                shutil.copy2(backup_source, str(old_path) + '.before_restore')
    except Exception:
        for target, staged in reversed(installed):
            os.replace(target, staged)
        for saved, old_path in reversed(moved):
            os.replace(saved, old_path)
        raise

    completed = data_dir / f'.restore_applied_{uuid.uuid4().hex}'
    os.replace(pending, completed)
    try:
        shutil.rmtree(completed)
    except OSError as exc:
        print(f'Warning: restored files applied, but temporary cleanup failed: {exc}')
    return names


if __name__ == '__main__':
    import sys
    if len(sys.argv) != 3 or sys.argv[1] != 'apply-pending':
        raise SystemExit('usage: backup_utils.py apply-pending DATA_DIR')
    apply_pending_restore(sys.argv[2])
