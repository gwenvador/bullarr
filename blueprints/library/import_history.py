"""
Historique des imports de fichiers
"""
import sqlite3
import json
import os
from datetime import datetime
from flask import current_app


def init_import_history_table():
    """Initialise la table d'historique des imports"""
    try:
        conn = sqlite3.connect(current_app.config['DATABASE'], timeout=120.0, check_same_thread=False)
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS import_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id TEXT UNIQUE,
                operation_type TEXT,
                status TEXT,
                import_path TEXT,
                files_processed INTEGER DEFAULT 0,
                files_imported INTEGER DEFAULT 0,
                files_replaced INTEGER DEFAULT 0,
                files_skipped INTEGER DEFAULT 0,
                files_failed INTEGER DEFAULT 0,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS import_history_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id TEXT,
                filename TEXT,
                source_path TEXT,
                destination_path TEXT,
                series_id INTEGER,
                series_title TEXT,
                action TEXT,
                status TEXT,
                message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (operation_id) REFERENCES import_history(operation_id),
                FOREIGN KEY (series_id) REFERENCES series(id) ON DELETE SET NULL
            )
        ''')

        # Numéro/type de tome RÉELLEMENT retenu pour cet import ("historique des imports
        # n'indique pas le numero de volume a coté du nom de l'album quand c'est importé
        # manuelement") - voir log_import_file/get_operation_details. Un import manuel
        # avec correction de tome (volume_override, execute_import) porte un fichier dont
        # le NOM ne permet justement pas de retrouver le numéro par un simple re-parsing a
        # posteriori (c'est précisément pourquoi une correction manuelle était nécessaire à
        # l'import) - sans cette colonne, ce cas ne peut jamais être comblé rétroactivement.
        cursor.execute("PRAGMA table_info(import_history_files)")
        existing_columns = {row[1] for row in cursor.fetchall()}
        if 'parsed_volume_json' not in existing_columns:
            cursor.execute("ALTER TABLE import_history_files ADD COLUMN parsed_volume_json TEXT")

        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Erreur lors de la création de la table d'historique: {e}")
        return False


def cleanup_stale_operations():
    """Marque comme 'failed' les opérations restées bloquées à 'started'

    Les imports s'exécutent de façon synchrone dans une requête HTTP (ou un tick du
    scheduler): une opération encore à 'started' au démarrage de l'application signifie
    forcément qu'elle a été interrompue (crash, redémarrage du conteneur...) et ne se
    terminera jamais toute seule.

    "pourquoi Aucun fichier : Le loup.BD [Jean-... et échec de l'import. pourtant le
    fichier est bien la" - une opération interrompue APRÈS avoir déjà déplacé/committé un
    fichier avec succès (le per-file loop d'execute_import/execute_auto_import écrit sa
    propre ligne import_history_files et commit AVANT le travail de fin d'opération -
    stats, renommage, sync Komga/EBDZ - qui, lui, peut être interrompu par un redémarrage)
    gardait ses compteurs files_imported/replaced/skipped/failed figés à leur valeur
    d'INSERT initiale (tous à 0, voir log_import_operation) au lieu de refléter les
    fichiers réellement traités avant l'interruption - la ligne Historique affichait alors
    "✗ Échec" + "0 importé(s)... 0 échec(s)" à CÔTÉ du nom du fichier réellement importé
    (file_names, sous-requête indépendante du statut de l'opération, voir
    get_import_history), une combinaison contradictoire qui donnait l'impression à tort
    que le fichier affiché avait lui-même échoué. Recalculés ici depuis les VRAIES lignes
    import_history_files de chaque opération bloquée avant de la marquer 'failed' - le
    statut 'failed' reste correct au niveau de l'OPÉRATION (son travail de fin, potentiel-
    lement incomplet pour d'autres fichiers encore en cours au moment du kill, n'a jamais
    pu se terminer), mais les compteurs et le détail par fichier redeviennent honnêtes.

    Délai de grâce de 5 minutes sur created_at avant de considérer une opération 'started'
    comme bloquée: cette fonction tourne à CHAQUE create_app() (voir app.py), pas
    seulement au vrai démarrage du conteneur - un appel ad hoc (script de diagnostic via
    docker exec, par ex.) qui partage la même base que le process réel peut tomber pile
    pendant qu'un import est encore légitimement en cours (jusqu'à ~30s entre son
    log_import_operation('started') et son update_import_operation('completed') final,
    voir execute_import/execute_auto_import) - sans ce délai, il stompait à tort le statut
    d'un import qui allait très bien se terminer tout seul quelques secondes plus tard
    (constaté en pratique: un /api/import/scan de diagnostic a marqué 'failed' un import
    "Le pouvoir des Innocents" réussi 2 secondes après son démarrage)."""
    conn = None
    try:
        conn = sqlite3.connect(current_app.config['DATABASE'], timeout=120.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute('''
            SELECT operation_id FROM import_history
            WHERE status = 'started' AND created_at < datetime('now', '-5 minutes')
        ''')
        stale_operation_ids = [row['operation_id'] for row in cursor.fetchall()]

        for operation_id in stale_operation_ids:
            cursor.execute('''
                UPDATE import_history_files
                SET status = 'error', action = 'error',
                    message = 'Interrompu par un redémarrage de l''application avant la fin de son traitement'
                WHERE operation_id = ? AND status = 'processing'
            ''', (operation_id,))

            cursor.execute('''
                SELECT
                    SUM(CASE WHEN action = 'imported' AND status = 'success' THEN 1 ELSE 0 END) AS imported_count,
                    SUM(CASE WHEN action = 'replaced' AND status = 'success' THEN 1 ELSE 0 END) AS replaced_count,
                    SUM(CASE WHEN action = 'skipped' AND status = 'success' THEN 1 ELSE 0 END) AS skipped_count,
                    SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS failed_count
                FROM import_history_files
                WHERE operation_id = ?
            ''', (operation_id,))
            counts = cursor.fetchone()
            details = (
                "Opération interrompue par un redémarrage de l'application avant sa "
                "finalisation - les fichiers listés ci-dessous ont déjà été traités avec "
                "succès avant l'interruption."
                if (counts['imported_count'] or counts['replaced_count'] or counts['skipped_count'])
                else None
            )
            cursor.execute('''
                UPDATE import_history
                SET status = 'failed', completed_at = CURRENT_TIMESTAMP,
                    files_imported = ?, files_replaced = ?, files_skipped = ?, files_failed = ?,
                    details = COALESCE(?, details)
                WHERE operation_id = ?
            ''', (
                counts['imported_count'] or 0, counts['replaced_count'] or 0,
                counts['skipped_count'] or 0, counts['failed_count'] or 0,
                details, operation_id
            ))

        if stale_operation_ids:
            conn.commit()
            print(f"✓ {len(stale_operation_ids)} import(s) bloqué(s) sur 'en cours' marqué(s) comme échoué(s)")

        cursor.execute('''
            SELECT f.id FROM import_history_files f
            JOIN import_history h ON h.operation_id = f.operation_id
            WHERE f.status = 'processing' AND h.status != 'started'
        ''')
        orphaned_file_ids = [row['id'] for row in cursor.fetchall()]
        if orphaned_file_ids:
            cursor.executemany('''
                UPDATE import_history_files
                SET status = 'error', action = 'error',
                    message = 'Ligne orpheline: opération parente déjà terminée sans que ce fichier n''ait été finalisé'
                WHERE id = ?
            ''', [(fid,) for fid in orphaned_file_ids])
            conn.commit()
            print(f"✓ {len(orphaned_file_ids)} ligne(s) fichier orpheline(s) sur 'processing' nettoyée(s) (opération parente déjà terminée)")

        return len(stale_operation_ids)
    except Exception as e:
        print(f"Erreur lors du nettoyage des opérations bloquées: {e}")
        return 0
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


def log_import_operation(operation_id, operation_type, import_path, status='started', details=None):
    """Enregistre une opération d'import"""
    conn = None
    try:
        conn = sqlite3.connect(current_app.config['DATABASE'], timeout=120.0, check_same_thread=False)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO import_history 
            (operation_id, operation_type, import_path, status, details)
            VALUES (?, ?, ?, ?, ?)
        ''', (operation_id, operation_type, import_path, status, details))
        
        conn.commit()
        return True
    except Exception as e:
        print(f"Erreur lors de l'enregistrement de l'opération: {e}")
        return False
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


def log_import_file(operation_id, filename, source_path, destination_path, series_title, action, status, message='', parsed_volume=None):
    """Enregistre l'import d'un fichier. Retourne l'id de la ligne insérée (ou None en
    échec) - voir log_import_file_started/update_import_file: un appel initial ('en
    cours', dès qu'on connaît le nom du fichier, AVANT de savoir son sort) suivi d'une
    mise à jour EN PLACE de cette même ligne par son id plutôt que d'une seconde ligne.

    parsed_volume: dict optionnel ({'volume': ..., 'is_integral': ..., ...}, voir
    file_data['parsed'] côté execute_import/execute_auto_import) - le numéro/type de
    tome RÉELLEMENT retenu pour cet import, y compris après une correction manuelle
    (volume_override) que le nom de fichier seul ne permettrait pas de retrouver plus
    tard (voir get_operation_details)."""
    conn = None
    try:
        conn = sqlite3.connect(current_app.config['DATABASE'], timeout=120.0, check_same_thread=False)
        cursor = conn.cursor()

        # Chercher le series_id si la série existe
        series_id = None
        if series_title:
            cursor.execute('SELECT id FROM series WHERE title = ?', (series_title,))
            result = cursor.fetchone()
            if result:
                series_id = result[0]

        parsed_volume_json = json.dumps(parsed_volume) if parsed_volume else None
        cursor.execute('''
            INSERT INTO import_history_files
            (operation_id, filename, source_path, destination_path, series_id, series_title, action, status, message, parsed_volume_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (operation_id, filename, source_path, destination_path, series_id, series_title, action, status, message, parsed_volume_json))

        conn.commit()
        return cursor.lastrowid
    except Exception as e:
        print(f"Erreur lors de l'enregistrement du fichier: {e}")
        return None
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


def log_import_file_started(operation_id, filename):
    """Insère immédiatement une ligne 'en cours' pour ce fichier, dès que son nom est
    connu et AVANT tout traitement (déplacement/conversion/écriture ComicInfo...) -
    "Import en cours... detail lequel": jusqu'ici, la ligne d'un fichier n'était écrite
    qu'à la toute fin de TOUTE l'opération (par lot, "pour éviter les problèmes de verrou
    SQLite"), donc file_names (la sous-requête GROUP_CONCAT de get_import_history)
    restait vide tant que l'import tournait - impossible de savoir lequel était en cours.
    Retourne l'id de cette ligne, à passer à update_import_file une fois le sort du
    fichier connu (mise à jour EN PLACE, pas une seconde ligne)."""
    return log_import_file(operation_id, filename, '', '', '', 'processing', 'processing', '')


def get_currently_processing_file():
    """Retourne la ligne 'processing' la plus récente encore ouverte (si une existe),
    avec le nombre de secondes écoulées depuis son log_import_file_started - "even if the
    process is stuck I should still be able to see the current download" / "Import en
    cours" existe déjà par fichier (voir statusBadge, import.js) mais ne distingue jamais
    LEQUEL des fichiers affichant ce même badge est réellement en train d'être traité
    MAINTENANT par _execute_import_batch, ni depuis combien de temps - impossible de
    repérer un blocage réel (incident 'série #924'/'un import est déjà en cours' du
    2026-08-28: le fichier fautif n'était identifiable qu'en fouillant les logs
    conteneur). Utilisée par /api/import/state (routes.py) pour exposer ce fichier
    explicitement au badge "🔄 En cours de traitement (Ns)" côté import.js, distinct du
    badge générique existant.

    "pourquoi bullar est frozen pour l'import" - faux positif réel constaté le
    2026-08-28: plusieurs lignes import_history_files restent bloquées à 'processing'
    depuis le 1er août alors que leur OPÉRATION PARENTE (import_history.status) est déjà
    'completed' - bug préexistant où update_import_file n'a pas été appelé pour ces
    fichiers précis avant la fin de leur batch (execute_auto_import a continué malgré
    une exception isolée sur eux). Sans le JOIN ci-dessous, la ligne 'processing' la plus
    récente pouvait être un de ces fantômes vieux de 27 jours plutôt que le vrai fichier
    actif - "toujours frozen" alors que rien n'était réellement bloqué. Ne retenir QUE
    les lignes dont l'opération parente est ENCORE 'started': c'est la seule garantie
    qu'un batch la traite réellement à l'instant T (voir cleanup_stale_operations, qui
    bascule déjà 'started' -> 'failed' après 5 minutes pour l'opération elle-même, mais
    ne touchait pas ses lignes fichier 'processing' individuelles - cause racine du même
    bug, corrigée séparément plus bas)."""
    conn = None
    try:
        conn = sqlite3.connect(current_app.config['DATABASE'], timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT f.id, f.operation_id, f.filename, f.created_at,
                   (strftime('%s', 'now') - strftime('%s', f.created_at)) AS elapsed_seconds
            FROM import_history_files f
            JOIN import_history h ON h.operation_id = f.operation_id
            WHERE f.status = 'processing' AND h.status = 'started'
            ORDER BY f.created_at DESC
            LIMIT 1
        ''')
        row = cursor.fetchone()
        if not row:
            return None
        return {
            'file_id': row['id'],
            'operation_id': row['operation_id'],
            'filename': row['filename'],
            'started_at': row['created_at'],
            'elapsed_seconds': row['elapsed_seconds'],
        }
    except Exception as e:
        print(f"Erreur lecture du fichier en cours de traitement: {e}")
        return None
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def update_import_file(file_id, source_path, destination_path, series_title, action, status, message='', parsed_volume=None):
    """Met à jour EN PLACE la ligne 'en cours' créée par log_import_file_started, une fois
    le sort réel du fichier connu (imported/replaced/skipped/failed) - remplace l'INSERT
    différé d'origine pour ce même fichier. parsed_volume: voir log_import_file."""
    if file_id is None:
        return False
    conn = None
    try:
        conn = sqlite3.connect(current_app.config['DATABASE'], timeout=120.0, check_same_thread=False)
        cursor = conn.cursor()

        series_id = None
        if series_title:
            cursor.execute('SELECT id FROM series WHERE title = ?', (series_title,))
            result = cursor.fetchone()
            if result:
                series_id = result[0]

        parsed_volume_json = json.dumps(parsed_volume) if parsed_volume else None
        cursor.execute('''
            UPDATE import_history_files
            SET source_path = ?, destination_path = ?, series_id = ?, series_title = ?,
                action = ?, status = ?, message = ?, parsed_volume_json = ?
            WHERE id = ?
        ''', (source_path, destination_path, series_id, series_title, action, status, message, parsed_volume_json, file_id))

        conn.commit()
        return True
    except Exception as e:
        print(f"Erreur lors de la mise à jour du fichier: {e}")
        return False
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


def delete_import_file_row(file_id):
    """"don't put the error in the log until the file has finished download" - une
    "Fichier corrompu" encore dans son budget de tentatives rapprochées (voir
    AUTO_IMPORT_CORRUPTION_RETRY_DELAYS, scheduler.py) est très probablement juste un
    fichier encore en train d'être écrit sur disque, pas une vraie erreur à montrer -
    _execute_import_batch (routes.py) appelle cette fonction plutôt que
    update_import_file pour supprimer la ligne 'processing' posée par
    log_import_file_started au lieu de la faire apparaître comme "Échec" dans
    Historique, tant que le fichier a encore une vraie chance de devenir lisible tout
    seul. Une fois le budget épuisé (le fichier semble alors réellement bloqué, pas
    juste en cours de copie), update_import_file reprend le dessus normalement."""
    if file_id is None:
        return False
    conn = None
    try:
        conn = sqlite3.connect(current_app.config['DATABASE'], timeout=120.0, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM import_history_files WHERE id = ?', (file_id,))
        conn.commit()
        return True
    except Exception as e:
        print(f"Erreur lors de la suppression de la ligne d'import: {e}")
        return False
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


def update_import_operation(operation_id, status, imported_count, replaced_count, skipped_count, failed_count, details=None):
    """Met à jour le statut d'une opération d'import.

    details: message d'erreur (str(e), voir execute_import/execute_auto_import) quand
    l'opération entière plante avant même d'avoir traité un seul fichier ("pourquoi
    l'échec" alors que tous les compteurs sont à 0 - jusqu'ici cette exception n'était
    imprimée que dans les logs du container, invisible depuis /import ou /history)."""
    conn = None
    try:
        conn = sqlite3.connect(current_app.config['DATABASE'], timeout=120.0, check_same_thread=False)
        cursor = conn.cursor()

        if details is not None:
            cursor.execute('''
                UPDATE import_history
                SET status = ?, files_imported = ?, files_replaced = ?,
                    files_skipped = ?, files_failed = ?, completed_at = CURRENT_TIMESTAMP,
                    details = ?
                WHERE operation_id = ?
            ''', (status, imported_count, replaced_count, skipped_count, failed_count, details, operation_id))
        else:
            cursor.execute('''
                UPDATE import_history
                SET status = ?, files_imported = ?, files_replaced = ?,
                    files_skipped = ?, files_failed = ?, completed_at = CURRENT_TIMESTAMP
                WHERE operation_id = ?
            ''', (status, imported_count, replaced_count, skipped_count, failed_count, operation_id))
        
        conn.commit()
        return True
    except Exception as e:
        print(f"Erreur lors de la mise à jour de l'opération: {e}")
        return False
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


def delete_import_operation(operation_id):
    ""
    conn = None
    try:
        conn = sqlite3.connect(current_app.config['DATABASE'], timeout=120.0, check_same_thread=False)
        conn.execute('DELETE FROM import_history WHERE operation_id = ?', (operation_id,))
        conn.commit()
    except Exception as e:
        print(f"Erreur lors de la suppression de l'opération vide {operation_id}: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


def get_import_history(limit=50, series_id=None):
    """Récupère l'historique des imports.

    series_id: "ajoute une fiche histoire par série" - filtre optionnel (fiche historique
    d'une série précise) - une opération reste renvoyée dès qu'AU MOINS UN de ses fichiers
    concerne cette série (import_history reste au niveau OPÉRATION, pas fichier - un lot
    peut mélanger plusieurs séries ; le détail par fichier existe déjà via
    GET /api/import/history/<operation_id>)."""
    conn = None
    try:
        conn = sqlite3.connect(current_app.config['DATABASE'], timeout=120.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # file_names: aperçu des fichiers de l'opération directement dans la liste
        # ("voir le nom du volume importé sans avoir à ouvrir le detail") - sous-requête
        # corrélée plutôt qu'un JOIN+GROUP BY, plus simple à lire pour un champ qui reste
        # secondaire par rapport aux colonnes de import_history elles-mêmes.
        cursor.execute(f'''
            SELECT h.*,
                   (SELECT GROUP_CONCAT(filename, ' · ') FROM import_history_files WHERE operation_id = h.operation_id) AS file_names
            FROM import_history h
            {"WHERE EXISTS (SELECT 1 FROM import_history_files f WHERE f.operation_id = h.operation_id AND f.series_id = ?)" if series_id is not None else ""}
            ORDER BY h.created_at DESC
            LIMIT ?
        ''', ((series_id, limit) if series_id is not None else (limit,)))

        history = [dict(row) for row in cursor.fetchall()]
        return history
    except Exception as e:
        print(f"Erreur lors de la récupération de l'historique: {e}")
        return []
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


def get_operation_details(operation_id):
    """Récupère les détails d'une opération"""
    conn = None
    try:
        conn = sqlite3.connect(current_app.config['DATABASE'], timeout=120.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Récupérer l'opération
        cursor.execute('SELECT * FROM import_history WHERE operation_id = ?', (operation_id,))
        row = cursor.fetchone()
        operation = dict(row) if row else None
        
        # Récupérer les fichiers de cette opération. Le nom de série de la série actuelle
        # (s.title) est préféré à l'instantané figé au moment de l'import
        # (import_history_files.series_title): une série peut être renommée/rematchée sur
        # Bedetheque après coup (voir _align_title_and_start_metadata_write), et
        # l'historique doit refléter le nom à jour plutôt qu'un nom potentiellement
        # obsolète (ex: ancienne convention de tri "Titre (Le)"). On ne retombe sur
        # l'instantané que si la série a depuis été supprimée (series_id devient NULL,
        # voir ON DELETE SET NULL).
        cursor.execute('''
            SELECT f.id, f.operation_id, f.filename, f.source_path, f.destination_path,
                   f.series_id, f.action, f.status, f.message, f.created_at,
                   f.parsed_volume_json,
                   COALESCE(s.title, f.series_title) AS series_title
            FROM import_history_files f
            LEFT JOIN series s ON s.id = f.series_id
            WHERE f.operation_id = ?
            ORDER BY f.created_at
        ''', (operation_id,))

        files = [dict(row) for row in cursor.fetchall()]

        from .scanner import LibraryScanner
        for f in files:
            stored = f.pop('parsed_volume_json', None)
            parsed = json.loads(stored) if stored else LibraryScanner.parse_filename(f['filename'] or '')
            f['volume_number'] = parsed.get('volume')
            f['is_integral'] = parsed.get('is_integral')
            f['integral_number'] = parsed.get('integral_number')
            f['is_hs'] = parsed.get('is_hs')
            f['hs_number'] = parsed.get('hs_number')
            f['is_episode'] = parsed.get('is_episode')
            f['episode_number'] = parsed.get('episode_number')

        return {'operation': operation, 'files': files}
    except Exception as e:
        print(f"Erreur lors de la récupération des détails: {e}")
        return None
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


def undo_import_operation(operation_id):
    """Annule une opération d'import en déplaçant les fichiers"""
    try:
        import os
        import shutil
        from datetime import datetime
        
        operation_details = get_operation_details(operation_id)
        if not operation_details or not operation_details['operation']:
            return False, "Opération non trouvée", []
        
        operation = operation_details['operation']
        files = operation_details['files']
        
        if operation['status'] != 'completed':
            return False, "Seules les opérations complétées peuvent être annulées", []
        
        undo_count = 0
        error_count = 0
        errors = []

        # Les fichiers annulés sont replacés dans un dossier _undo_<id> à la racine
        # du répertoire d'import (aMule ou torrents) dont provient chaque fichier
        import_roots = current_app.config['IMPORT_DIRECTORIES']
        undo_dirs = {}

        def get_undo_dir(source_path):
            root = next(
                (r for r in import_roots if source_path.startswith(r + os.sep)),
                import_roots[0] if import_roots else os.path.dirname(source_path)
            )
            if root not in undo_dirs:
                undo_dirs[root] = os.path.join(root, f'_undo_{operation_id}')
                os.makedirs(undo_dirs[root], exist_ok=True)
            return undo_dirs[root]

        for file_record in files:
            try:
                if file_record['status'] in ['imported', 'replaced']:
                    # Le fichier a été importé/remplacé, le déplacer vers undo
                    if os.path.exists(file_record['destination_path']):
                        undo_dir = get_undo_dir(file_record['source_path'])
                        shutil.move(file_record['destination_path'],
                                   os.path.join(undo_dir, os.path.basename(file_record['destination_path'])))
                        undo_count += 1
                        
                        # Mettre à jour le statut
                        conn = None
                        try:
                            conn = sqlite3.connect(current_app.config['DATABASE'], timeout=120.0, check_same_thread=False)
                            cursor = conn.cursor()
                            # id de ligne, pas (operation_id, filename): deux fichiers de
                            # même nom de base dans une même opération (séries/dossiers
                            # source différents) partageraient sinon cette clé - les DEUX
                            # lignes passeraient à 'undone' même si une seule a réellement
                            # été redéplacée ci-dessus.
                            cursor.execute('''
                                UPDATE import_history_files
                                SET status = 'undone'
                                WHERE id = ?
                            ''', (file_record['id'],))
                            conn.commit()
                        finally:
                            if conn:
                                try:
                                    conn.close()
                                except:
                                    pass
            except Exception as e:
                error_count += 1
                errors.append(f"{file_record['filename']}: {str(e)}")
        
        # Mettre à jour l'opération: 'undone' seulement si tous les fichiers ont pu être
        # déplacés - sinon 'undo_partial', pour ne pas afficher une annulation comme
        # entièrement réussie alors que certains fichiers sont restés à leur destination
        conn = None
        try:
            conn = sqlite3.connect(current_app.config['DATABASE'], timeout=120.0, check_same_thread=False)
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE import_history
                SET status = ?
                WHERE operation_id = ?
            ''', ('undone' if error_count == 0 else 'undo_partial', operation_id))
            conn.commit()
        finally:
            if conn:
                try:
                    conn.close()
                except:
                    pass
        
        message = f"Annulation complétée: {undo_count} fichier(s) déplacé(s) vers {', '.join(undo_dirs.values())}"
        if error_count > 0:
            message += f" ({error_count} erreur(s))"
        
        return True, message, errors
        
    except Exception as e:
        print(f"Erreur lors de l'annulation: {e}")
        return False, str(e), []


def init_import_manual_overrides_table():
    """Fichiers marqués "assignation manuelle" ("si jai a faire manuelement un matching
    alors met un flag pas dimport automayique pour ce vokume. import manuel seulement") -
    quand l'utilisateur assigne/corrige une destination à la main sur /import
    (assignDestination, import.js), l'import automatique planifié (scheduler.py) ne doit
    plus jamais reprendre ce fichier de lui-même : seul un clic explicite sur "Importer"
    doit le finaliser avec la correction humaine, pas un re-matching automatique qui a
    justement échoué (ou qu'on a délibérément corrigé) au tour précédent. Clé = chemin
    absolu du fichier, stable tant qu'il reste dans le répertoire d'import surveillé
    (jusqu'à son import réel, qui le déplace ailleurs)."""
    try:
        conn = sqlite3.connect(current_app.config['DATABASE'], timeout=120.0, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS import_manual_overrides (
                filepath TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Erreur lors de la création de la table import_manual_overrides: {e}")
        return False


def mark_import_file_manual(filepath):
    """Marque un fichier comme assigné/corrigé manuellement - voir
    init_import_manual_overrides_table. Idempotent (INSERT OR IGNORE)."""
    conn = None
    try:
        conn = sqlite3.connect(current_app.config['DATABASE'], timeout=120.0, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute('INSERT OR IGNORE INTO import_manual_overrides (filepath) VALUES (?)', (filepath,))
        conn.commit()
        return True
    except Exception as e:
        print(f"Erreur lors du marquage manuel de {filepath}: {e}")
        return False
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


def remove_import_file_manual(filepath):
    # Un fichier finalisé ne doit plus rester retenu par le hold manuel.
    conn = None
    try:
        conn = sqlite3.connect(current_app.config['DATABASE'], timeout=120.0, check_same_thread=False)
        conn.execute('DELETE FROM import_manual_overrides WHERE filepath = ?', (filepath,))
        conn.commit()
        return True
    except Exception as e:
        print(f"Erreur lors du retrait de l'assignation manuelle de {filepath}: {e}")
        return False
    finally:
        if conn:
            conn.close()


def mark_import_file_packaged(filepath, destination=None):
    conn = None
    try:
        conn = sqlite3.connect(current_app.config['DATABASE'], timeout=120.0, check_same_thread=False)
        conn.execute('CREATE TABLE IF NOT EXISTS import_packaged_files (filepath TEXT PRIMARY KEY, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, destination_json TEXT)')
        import json
        encoded = json.dumps(destination, ensure_ascii=False) if destination else None
        conn.execute('INSERT OR IGNORE INTO import_packaged_files (filepath, destination_json) VALUES (?, ?)', (filepath, encoded))
        if encoded:
            conn.execute('UPDATE import_packaged_files SET destination_json = ? WHERE filepath = ?', (encoded, filepath))
        conn.commit()
        return True
    except Exception as e:
        print(f'Erreur marquage fichier empaqueté {filepath}: {e}')
        return False
    finally:
        if conn: conn.close()


def get_packaged_filepaths():
    conn = None
    try:
        conn = sqlite3.connect(current_app.config['DATABASE'], timeout=120.0, check_same_thread=False)
        conn.execute('CREATE TABLE IF NOT EXISTS import_packaged_files (filepath TEXT PRIMARY KEY, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, destination_json TEXT)')
        rows = [row[0] for row in conn.execute('SELECT filepath FROM import_packaged_files').fetchall()]
        stale = [p for p in rows if not os.path.exists(p)]
        if stale:
            conn.executemany('DELETE FROM import_packaged_files WHERE filepath = ?', [(p,) for p in stale])
            conn.commit()
        return {p for p in rows if p not in stale}
    except Exception as e:
        print(f'Erreur lecture fichiers empaquetés: {e}')
        return set()
    finally:
        if conn: conn.close()


def get_packaged_destinations():
    import json
    conn = None
    try:
        conn = sqlite3.connect(current_app.config['DATABASE'], timeout=120.0, check_same_thread=False)
        conn.execute("CREATE TABLE IF NOT EXISTS import_packaged_files (filepath TEXT PRIMARY KEY, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, destination_json TEXT)")
        cols = {row[1] for row in conn.execute('PRAGMA table_info(import_packaged_files)').fetchall()}
        if 'destination_json' not in cols: conn.execute('ALTER TABLE import_packaged_files ADD COLUMN destination_json TEXT')
        result = {}
        for filepath, encoded in conn.execute('SELECT filepath, destination_json FROM import_packaged_files').fetchall():
            if os.path.exists(filepath) and encoded:
                try: result[filepath] = json.loads(encoded)
                except (TypeError, ValueError): pass
        return result
    except Exception as e: print(f'Erreur lecture destinations empaquetées: {e}'); return {}
    finally:
        if conn: conn.close()

def get_finalized_import_source_paths():
    conn = None
    try:
        conn = sqlite3.connect(current_app.config['DATABASE'], timeout=120.0, check_same_thread=False)
        rows = conn.execute("SELECT DISTINCT source_path FROM import_history_files WHERE source_path IS NOT NULL AND source_path != '' AND action IN ('imported','replaced','skipped') AND status = 'success'").fetchall()
        return {row[0] for row in rows}
    except Exception as e:
        print(f'Erreur lecture des imports finalisés: {e}')
        return set()
    finally:
        if conn: conn.close()

def get_manual_override_filepaths():
    """Ensemble des chemins actuellement marqués "assignation manuelle" - nettoie au
    passage les entrées dont le fichier n'existe plus (déjà importé/déplacé/supprimé
    depuis), pour ne jamais accumuler indéfiniment des lignes mortes sans jamais avoir à y
    penser explicitement ailleurs."""
    conn = None
    try:
        conn = sqlite3.connect(current_app.config['DATABASE'], timeout=120.0, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute('SELECT filepath FROM import_manual_overrides')
        rows = [row[0] for row in cursor.fetchall()]
        finalized_names = {row[0] for row in cursor.execute("SELECT DISTINCT filename FROM import_history_files WHERE action IN ('imported','replaced','skipped') AND status = 'success'")}
        finalized_overrides = [p for p in rows if os.path.basename(p) in finalized_names]
        if finalized_overrides:
            cursor.executemany('DELETE FROM import_manual_overrides WHERE filepath = ?', [(p,) for p in finalized_overrides])
            conn.commit()
            rows = [p for p in rows if p not in set(finalized_overrides)]

        stale = [p for p in rows if not os.path.exists(p)]
        if stale:
            cursor.executemany('DELETE FROM import_manual_overrides WHERE filepath = ?', [(p,) for p in stale])
            conn.commit()

        stale_set = set(stale)
        return {p for p in rows if p not in stale_set}
    except Exception as e:
        print(f"Erreur lors de la lecture des assignations manuelles: {e}")
        return set()
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


def init_import_in_progress_table():
    """"it should be in database in import so only one can try importing" - le scheduler
    périodique (interval 5s, library/scheduler.py) et le déclenchement immédiat Telegram
    construisent chacun leur PROPRE liste de fichiers à importer en scannant le disque
    AVANT d'acquérir _import_execution_lock (routes.py) - ce verrou sérialise bien le
    traitement réel, mais pas la phase de scan qui le précède. Deux passages assez
    rapprochés (ex: deux tours du scheduler à 5s d'intervalle si le premier prend plus de
    5s) peuvent donc chacun retenir le même fichier avant que l'un des deux ne l'ait
    déplacé - le second échoue alors avec "Fichier introuvable sur le disque" au moment de
    la conversion/vérification d'intégrité, sur un fichier déjà importé avec succès par
    l'autre (incident réel: BD.FR.-.Nordheim.-.14.-.Aaricia..., deux opérations à 2s
    d'intervalle, la seconde en échec sur un fichier déjà déplacé par la première).
    Cette table comble l'angle mort: un fichier est réclamé ICI dès qu'il entre dans
    files_to_import d'une opération (_execute_import_batch, une fois le verrou acquis,
    donc jamais deux réclamations concurrentes pour le même chemin), et toute construction
    ultérieure d'une liste de fichiers à importer (scan périodique, déclenchement immédiat)
    exclut simplement tout chemin déjà réclamé - le même principe que
    import_manual_overrides ci-dessus, appliqué à "en cours de traitement" plutôt qu'à
    "corrigé à la main". claimed_at permet de purger une réclamation orpheline (crash en
    cours d'import) sans jamais bloquer un chemin indéfiniment."""
    try:
        conn = sqlite3.connect(current_app.config['DATABASE'], timeout=120.0, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS import_in_progress (
                filepath TEXT PRIMARY KEY,
                operation_id TEXT,
                claimed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Erreur lors de la création de la table import_in_progress: {e}")
        return False


# Filet de sécurité si release_in_progress_files n'est jamais appelée pour une opération
# (crash serveur en plein import) - une conversion PDF peut légitimement prendre plusieurs
# minutes (voir CLAUDE.md), ce seuil reste largement au-dessus de toute durée normale d'un
# seul fichier pour ne jamais libérer une réclamation encore réellement en cours.
_IN_PROGRESS_STALE_MINUTES = 30


def claim_import_files(filepaths, operation_id):
    """Atomically claim paths and return only those owned by this operation.

    INSERT OR IGNORE plus an owner readback is the cross-thread/process lock. This
    replaces the old batch-wide Python lock and remains correct with multiple Gunicorn
    workers because SQLite, not process memory, owns the claim.
    """
    if not filepaths:
        return set()
    conn = None
    try:
        conn = sqlite3.connect(current_app.config['DATABASE'], timeout=120.0, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute('BEGIN IMMEDIATE')
        cursor.executemany(
            'INSERT OR IGNORE INTO import_in_progress (filepath, operation_id) VALUES (?, ?)',
            [(p, operation_id) for p in filepaths]
        )
        placeholders = ','.join('?' for _ in filepaths)
        cursor.execute(
            f'SELECT filepath FROM import_in_progress '
            f'WHERE operation_id = ? AND filepath IN ({placeholders})',
            (operation_id, *filepaths)
        )
        claimed = {row[0] for row in cursor.fetchall()}
        conn.commit()
        return claimed
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        print(f"Erreur lors de la réclamation des fichiers en cours d'import: {e}")
        return set()
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def release_import_file(filepath, operation_id):
    """Release one claim only when it is still owned by this operation."""
    conn = sqlite3.connect(current_app.config['DATABASE'], timeout=120.0, check_same_thread=False)
    try:
        conn.execute(
            'DELETE FROM import_in_progress WHERE filepath = ? AND operation_id = ?',
            (filepath, operation_id)
        )
        conn.commit()
    finally:
        conn.close()


def release_import_files(operation_id):
    """Libère toutes les réclamations d'une opération - à appeler une fois
    _execute_import_batch terminé (succès ou échec), avant de relâcher
    _import_execution_lock."""
    conn = None
    try:
        conn = sqlite3.connect(current_app.config['DATABASE'], timeout=120.0, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM import_in_progress WHERE operation_id = ?', (operation_id,))
        conn.commit()
    except Exception as e:
        print(f"Erreur lors de la libération des fichiers en cours d'import: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


def get_in_progress_filepaths():
    """Ensemble des chemins actuellement réclamés par une opération d'import en cours -
    purge au passage les réclamations plus vieilles que _IN_PROGRESS_STALE_MINUTES
    (opération qui a planté sans passer par release_import_files) pour ne jamais bloquer
    un chemin indéfiniment."""
    conn = None
    try:
        conn = sqlite3.connect(current_app.config['DATABASE'], timeout=120.0, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute(f'''
            DELETE FROM import_in_progress
            WHERE claimed_at < datetime('now', '-{_IN_PROGRESS_STALE_MINUTES} minutes')
        ''')
        conn.commit()
        cursor.execute('SELECT filepath FROM import_in_progress')
        return {row[0] for row in cursor.fetchall()}
    except Exception as e:
        print(f"Erreur lors de la lecture des fichiers en cours d'import: {e}")
        return set()
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass
