"""Historique des traitements IA (Configuration → IA → Historique).

Fichier JSON à côté de ai_config.json, les entrées les plus récentes d'abord.
Chaque entrée garde le détail des changements (tags ajoutés par série, tags fusionnés)
pour pouvoir relire ce que l'IA a fait.
"""
import json
import os
import threading
import time
import uuid

from flask import current_app

HISTORY_MAX_ENTRIES = 200
DETAILS_MAX_ITEMS = 1000

_lock = threading.Lock()


def _path():
    return os.path.join(os.path.dirname(current_app.config['AI_CONFIG_FILE']), 'ai_history.json')


def load_history():
    try:
        with open(_path(), encoding='utf-8') as handle:
            entries = json.load(handle)
    except (OSError, ValueError):
        return []
    return entries if isinstance(entries, list) else []


def _write(entries):
    path = _path()
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(entries, handle, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def add_entry(kind, status, message, *, provider=None, model=None, started_at=None, details=None,
              extra=None):
    details = list(details or [])
    entry = {
        'id': uuid.uuid4().hex,
        'kind': kind,
        'status': status,
        'message': message or '',
        'provider': provider,
        'model': model,
        'started_at': started_at,
        'finished_at': time.time(),
        'details': details[:DETAILS_MAX_ITEMS],
        'details_total': len(details),
        **(extra or {}),
    }
    with _lock:
        entries = load_history()
        entries.insert(0, entry)
        try:
            _write(entries[:HISTORY_MAX_ENTRIES])
        except OSError:
            current_app.logger.exception('Historique IA : écriture impossible')
    return entry


def clear_history():
    with _lock:
        _write([])
