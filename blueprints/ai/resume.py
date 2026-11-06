"""Reprise de la création des tags (Configuration → IA → Créer des tags).

Pendant le traitement, la liste des séries qui restent est enregistrée après chaque lot
dans ai_generate_state.json, à côté de ai_config.json. Si le traitement s'arrête (erreur
de toutes les IA, bouton Arrêter, redémarrage de Bullarr), il reprend là où il en était.
Le fichier est effacé quand toutes les séries sont traitées, ou quand on abandonne.
"""
import json
import os
import threading
import time

from flask import current_app

_lock = threading.Lock()


def _path():
    return os.path.join(os.path.dirname(current_app.config['AI_CONFIG_FILE']), 'ai_generate_state.json')


def load_state():
    try:
        with open(_path(), encoding='utf-8') as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or not isinstance(state.get('remaining'), list):
        return None
    return state


def save_state(state):
    state['updated_at'] = time.time()
    path = _path()
    tmp = f'{path}.tmp'
    with _lock:
        with open(tmp, 'w', encoding='utf-8') as handle:
            json.dump(state, handle)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)


def clear_state():
    with _lock:
        try:
            os.remove(_path())
        except FileNotFoundError:
            pass


def new_state(scope, library_id, series_ids):
    return {'scope': scope, 'library_id': library_id, 'total': len(series_ids),
            'remaining': list(series_ids), 'changed': 0, 'added': 0, 'started_at': time.time()}


def summary(state=None):
    """Ce que l'interface affiche pour proposer de reprendre, ou None."""
    state = state if state is not None else load_state()
    if not state or not state.get('remaining'):
        return None
    total = int(state.get('total') or 0)
    return {
        'scope': state.get('scope'),
        'total': total,
        'done': max(0, total - len(state['remaining'])),
        'remaining': len(state['remaining']),
        'changed': state.get('changed', 0),
        'added': state.get('added', 0),
        'updated_at': state.get('updated_at'),
    }
