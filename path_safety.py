"""Confinement des chemins fournis par le client à des répertoires autorisés.

Les trois fonctions suivent le même schéma - résoudre (``realpath``), puis exiger que le
résultat soit la racine elle-même ou commence par ``racine + os.sep`` - et ne renvoient
que le chemin ainsi vérifié (ou une racine issue de la configuration). Ce schéma est celui
que l'analyse CodeQL (py/path-injection) reconnaît comme garde: ``os.path.commonpath`` ne
l'est pas.
"""
import os


class UnsafePathError(ValueError):
    """Levée quand un chemin/nom fourni par le client tenterait d'échapper au
    répertoire autorisé (import root ou bibliothèque) - typiquement via '../'"""
    pass


def resolve_within(path, root):
    """Résout `path` et vérifie qu'il reste bien contenu dans `root` (répertoire
    d'import ou de bibliothèque connu/configuré). Lève UnsafePathError sinon.
    Retourne le chemin réel (symlinks résolus) de `path`."""
    root_real = os.path.realpath(root)
    path_real = os.path.realpath(path)
    if path_real == root_real:
        return root_real
    if not path_real.startswith(root_real.rstrip(os.sep) + os.sep):
        raise UnsafePathError(f"Chemin en dehors du répertoire autorisé: {path}")
    return path_real


def resolve_within_any(path, roots):
    """Comme resolve_within, pour un chemin qui doit se trouver sous l'une des `roots`."""
    for root in roots:
        try:
            return resolve_within(path, root)
        except UnsafePathError:
            continue
    raise UnsafePathError(f"Chemin en dehors des répertoires autorisés: {path}")


def configured_root(requested, allowed_roots):
    """Renvoie la racine (réelle) de `allowed_roots` qui correspond à `requested`, ou None.

    La valeur renvoyée vient de la configuration, jamais de la requête: ce que le client
    envoie ne sert qu'à choisir parmi les racines autorisées.
    """
    wanted = os.path.realpath(requested)
    for root in allowed_roots:
        root_real = os.path.realpath(root)
        if root_real == wanted:
            return root_real
    return None


def series_folder_conflict(series_path, library_path, other_series_paths):
    """Raison pour laquelle le dossier d'une série ne lui appartient PAS en propre (None s'il
    lui appartient) : racine de la bibliothèque (fichiers posés directement à la racine, voir
    le scan), dossier partagé avec une autre série, ou dossier qui contient celui d'une autre
    série. Dans ces cas, supprimer ou déplacer ce dossier emporterait d'autres séries - toute
    la bibliothèque pour la racine."""
    if not series_path or not library_path:
        return None
    real = os.path.realpath(series_path)
    if real == os.path.realpath(library_path):
        return 'dossier racine de la bibliothèque'
    normalized = os.path.normpath(series_path)
    prefixes = tuple(p.rstrip(os.sep) + os.sep for p in {normalized, real})
    for other in other_series_paths:
        if not other:
            continue
        other_norm = os.path.normpath(other)
        if other_norm in (normalized, real):
            return 'dossier partagé avec une autre série'
        if other_norm.startswith(prefixes):
            return "dossier qui contient celui d'une autre série"
    return None
