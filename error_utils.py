"""Messages d'erreur renvoyés par l'API.

Bullarr est un outil privé (derrière SSO): le texte réel de l'erreur est renvoyé à
l'interface pour faciliter le diagnostic, et la trace complète est en plus écrite dans les
journaux du serveur. ``error_message`` centralise ce choix à un seul endroit.
"""
import logging

logger = logging.getLogger('bullarr.errors')

# Erreurs de validation attendues (entrée utilisateur): inutile d'en écrire la trace.
_EXPECTED = (ValueError, FileNotFoundError, FileExistsError, PermissionError)


def error_message(exc, prefix=None):
    """Journalise ``exc`` et renvoie son message, précédé de ``prefix`` s'il est fourni."""
    detail = str(exc) or exc.__class__.__name__
    text = f'{prefix}: {detail}' if prefix else detail
    if isinstance(exc, _EXPECTED):
        logger.warning('%s', text)
    else:
        logger.error('%s', text, exc_info=exc if isinstance(exc, BaseException) else None)
    return text
