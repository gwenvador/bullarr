"""Fichiers ed2k retirés d'un sujet EBDZ : on les garde en base, marqués `removed_at`.

Quand un sujet est relu, les liens qui n'y figurent plus sont marqués retirés (jamais supprimés) ; un
lien qui réapparaît est de nouveau actif. Les comptes, les recherches, Nouveautés et le téléchargement
automatique ne voient que les liens actifs ; la liste des fichiers d'un sujet montre aussi les retirés.
Les aides ci-dessous restent valables sur une base ancienne sans la colonne (aucun filtre alors)."""


def has_removed_column(db):
    """db : curseur ou connexion SQLite."""
    return any(row[1] == 'removed_at' for row in db.execute('PRAGMA table_info(ed2k_links)'))


def active_and(db):
    """' AND removed_at IS NULL' à ajouter à une clause WHERE existante."""
    return ' AND removed_at IS NULL' if has_removed_column(db) else ''


def active_where(db):
    """' WHERE removed_at IS NULL' pour une requête sans autre condition."""
    return ' WHERE removed_at IS NULL' if has_removed_column(db) else ''
