"""
Historique des renommages et suppressions de séries/tomes.

Contrairement aux imports (import_history.py, avec undo et détail fichier par fichier)
et aux téléchargements (missing_monitor), le renommage et la suppression sont des actions
plus ponctuelles - un seul événement par action suffit, pas besoin d'une table de détail
séparée. Suppression en particulier est irréversible (voir delete_series) et n'a jusqu'ici
laissé aucune trace consultable: un utilisateur qui se demande "qu'est-ce qui a disparu
et quand" n'avait que les logs du conteneur.
"""
import sqlite3
from flask import current_app


def init_action_history_table():
    """Initialise la table d'historique des actions (idempotent, comme
    init_import_history_table)."""
    try:
        conn = sqlite3.connect(current_app.config['DATABASE'], timeout=120.0, check_same_thread=False)
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS action_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_type TEXT,
                series_id INTEGER,
                series_title TEXT,
                detail TEXT,
                success INTEGER DEFAULT 1,
                error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # files_json: détail fichier par fichier d'un renommage ({'files': [...], 'folder':
        # {...}}), consulté à la demande depuis Historique ("ouvrir le nom pour avoir le
        # detail du renommage comme dans import") - la ligne `detail` reste le résumé
        # affiché directement, ce champ ne sert qu'au dépliage. Migration défensive
        # (PRAGMA table_info), même convention que le reste du schéma (voir CLAUDE.md).
        cursor.execute("PRAGMA table_info(action_history)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'files_json' not in columns:
            cursor.execute('ALTER TABLE action_history ADD COLUMN files_json TEXT')

        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Erreur lors de la création de la table d'historique des actions: {e}")
        return False


def log_action(action_type, series_id, series_title, detail, success=True, error=None, files_json=None):
    """Enregistre un événement de renommage ('rename') ou de suppression ('delete').
    Best-effort: une erreur ici ne doit jamais faire échouer l'action elle-même (même
    logique que trigger_scan_async pour Komga - la journalisation est secondaire par
    rapport à l'action réelle sur le disque/la base)."""
    try:
        conn = sqlite3.connect(current_app.config['DATABASE'], timeout=30.0)
        conn.execute('''
            INSERT INTO action_history (action_type, series_id, series_title, detail, success, error, files_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (action_type, series_id, series_title, detail, 1 if success else 0, error, files_json))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Erreur lors de la journalisation de l'action '{action_type}' (série #{series_id}): {e}")


def get_action_history(limit=100, action_type=None, series_id=None):
    conn = sqlite3.connect(current_app.config['DATABASE'], timeout=30.0)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    conditions = []
    params = []
    if action_type:
        conditions.append('action_type = ?')
        params.append(action_type)
    if series_id is not None:
        conditions.append('series_id = ?')
        params.append(series_id)
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ''
    cursor.execute(
        f'SELECT * FROM action_history {where_clause} ORDER BY created_at DESC LIMIT ?',
        (*params, limit)
    )

    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows
