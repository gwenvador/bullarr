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
    return _stop_event.is_set() or _cancel_event.is_set() or (not manual and is_paused())


# ===== Annulation (« Annuler l'import ») =====
# Contrairement à l'arrêt, l'annulation interrompt AUSSI le fichier en cours : le
# sous-processus de préparation (copie, conversion, validation) est tué et le retour arrière
# de _execute_import_batch remet tout en place - la source n'est jamais touchée avant le
# commit. Une fois le fichier entré dans sa finalisation (déplacement + écriture en base,
# quelques secondes), il va jusqu'au bout pour ne rien laisser à moitié. L'import automatique
# n'est PAS mis en pause.

class ImportCancelled(Exception):
    pass


_cancel_event = threading.Event()
_active_lock = threading.Lock()
_active_operations = set()


def register_operation(operation_id):
    with _active_lock:
        _active_operations.add(operation_id)


def unregister_operation(operation_id):
    with _active_lock:
        _active_operations.discard(operation_id)
        if not _active_operations:
            _cancel_event.clear()


def active_operations():
    """Opérations d'import réellement en train de tourner dans ce processus."""
    with _active_lock:
        return set(_active_operations)


def request_cancel():
    """Annule les imports en cours. False si aucun ne tourne (rien à interrompre)."""
    with _active_lock:
        if not _active_operations:
            return False
        _cancel_event.set()
        return True


def cancel_requested():
    return _cancel_event.is_set()


def raise_if_cancelled():
    if _cancel_event.is_set():
        raise ImportCancelled("Import annulé à la demande - le fichier est resté à sa place")
