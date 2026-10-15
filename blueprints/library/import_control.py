"""Arrêt de l'import demandé depuis /import (« Arrêter l'import »).

L'arrêt agit entre deux fichiers: le fichier en cours (conversion PDF, déplacement) va
jusqu'au bout, les suivants ne sont pas pris. Il met aussi l'import automatique en pause
jusqu'à « Reprendre » - un simple arrêt serait annulé 30 secondes plus tard par le
planificateur. La pause est un fichier drapeau: elle survit à un redémarrage.
"""
import os
import threading

from flask import current_app

_stop_event = threading.Event()


def _pause_flag_path():
    return os.path.join(os.path.dirname(current_app.config['LIBRARY_IMPORT_CONFIG_FILE']), 'import_paused.flag')


def is_paused():
    return os.path.exists(_pause_flag_path())


def request_stop():
    """Arrête l'import en cours après le fichier courant et met l'import automatique en pause."""
    _stop_event.set()
    path = _pause_flag_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w'):
        pass


def resume():
    _stop_event.clear()
    try:
        os.remove(_pause_flag_path())
    except FileNotFoundError:
        pass


def clear_stop():
    """Un import manuel lancé volontairement repart d'un arrêt vierge (la pause automatique reste)."""
    _stop_event.clear()


def should_stop(manual):
    """True si le lot en cours doit s'arrêter avant son prochain fichier."""
    return _stop_event.is_set() or (not manual and is_paused())
