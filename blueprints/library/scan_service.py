"""Manual scan orchestration shared by routes, jobs, and onboarding."""
import time

from .scanner import LibraryScanner, scan_import_lock


def _helpers():
    # Matching lives in routes until its larger set of dependencies is extracted.
    from . import routes
    return routes


def _connection():
    return _helpers().get_db_connection()

def scan_library_and_sync(library_id, library_path, force_metadata_refresh=False):
    """Cœur de scan_library (scan + synchronisation Komga/EBDZ) - factorisé pour être
    appelé aussi par run_library_onboarding (library/onboarding.py: "add a new
    bibliothèque from existing files"), qui a besoin exactement du même comportement
    qu'un clic manuel sur "Scanner" plutôt qu'un scan appauvri qui sauterait la
    synchronisation Komga/EBDZ. Retourne series_count, lève sur erreur (chemin
    introuvable...) - au lieu de renvoyer une réponse Flask, pour rester appelable hors
    requête HTTP."""
    if not scan_import_lock.acquire(timeout=60):
        raise RuntimeError('Un import est déjà en cours, réessayez plus tard')
    try:
        scanner = LibraryScanner()
        series_count = scanner.scan_directory(
            library_id, library_path, auto_enrich=False, force_metadata_refresh=force_metadata_refresh
        )
    finally:
        scan_import_lock.release()

    # Déclenché tôt, avant toute tentative de matching/sync Komga ci-dessous: comme à
    # l'import (voir execute_import), Komga doit d'abord avoir eu le temps de
    # rescanner et de connaître les fichiers que ce scan vient de découvrir sur le
    # disque, sinon une recherche par titre pour une série neuve ne trouve rien.
    # Best-effort, ne bloque pas le scan si Komga est indisponible/non configuré.
    changed_series_ids = scanner.series_created_or_changed
    new_series_ids = scanner.newly_created_series
    if changed_series_ids or new_series_ids:
        from blueprints.komga.client import trigger_scan_async
        trigger_scan_async()
        time.sleep(5)

    # Les volumes inchangés gardent déjà leur komga_book_id/url (repris depuis le
    # cache par scan_directory). Seules les séries nouvelles ou ayant au moins un
    # volume ajouté/modifié ont besoin d'une re-synchronisation Komga (appel réseau),
    # ce qui évite de refaire ~275 requêtes HTTP à chaque scan rapide sans rien de
    # changé. Best-effort: n'échoue pas le scan si Komga est indisponible/non configuré.
    if changed_series_ids:
        conn = _connection()
        cursor = conn.cursor()
        placeholders = ','.join('?' * len(changed_series_ids))
        cursor.execute(
            f'SELECT id, komga_series_id FROM series '
            f'WHERE library_id = ? AND komga_series_id IS NOT NULL AND id IN ({placeholders})',
            (library_id, *changed_series_ids)
        )
        matched_series = cursor.fetchall()
        conn.close()

        if matched_series:
            from blueprints.komga.client import KomgaClient, KomgaError
            try:
                client = KomgaClient()
                for series_row in matched_series:
                    _helpers()._sync_komga_books(series_row['id'], series_row['komga_series_id'], client)
            except KomgaError:
                pass

    # Tentative de matching Komga automatique pour les séries qui viennent d'être
    # créées par ce scan (pas les séries existantes: celles-ci restent à matcher
    # manuellement). Best-effort: n'échoue pas le scan si Komga est indisponible/non
    # configuré, ou si le titre ne désigne pas un résultat unique/exact.
    if new_series_ids:
        conn = _connection()
        cursor = conn.cursor()
        placeholders = ','.join('?' * len(new_series_ids))
        cursor.execute(
            f'SELECT id, title FROM series WHERE id IN ({placeholders})',
            tuple(new_series_ids)
        )
        new_series = cursor.fetchall()
        conn.close()

        if new_series:
            from blueprints.komga.client import KomgaClient, KomgaError
            try:
                client = KomgaClient()
                for series_row in new_series:
                    try:
                        _helpers()._try_komga_title_match(series_row['id'], series_row['title'], client)
                    except KomgaError:
                        continue
            except KomgaError:
                pass

    # Tentative de matching EBDZ automatique par titre pour TOUTES les séries de la
    # bibliothèque pas encore matchées (pas seulement les nouvelles: contrairement à
    # Komga, c'est une recherche locale en base, sans appel réseau). Fait en un seul
    # lot plutôt qu'un appel par série (voir _bulk_ebdz_autodetect): avec une table
    # ed2k_links de plusieurs dizaines de milliers de lignes, refaire une requête SQL
    # avec fonction Python par ligne pour CHAQUE série non matchée pouvait prendre
    # plusieurs minutes et donnait l'impression que le scan ne terminait jamais.
    try:
        _helpers()._bulk_ebdz_autodetect(library_id)
    except Exception:
        pass

    return series_count



def scan_series_and_sync(series_id, force_metadata_refresh=False):
    """Rescan a series and refresh its Komga/EBDZ links."""
    scanner = LibraryScanner()
    volumes_count = scanner.scan_single_series(series_id, force_metadata_refresh=force_metadata_refresh)

    # Un scan remplace tous les volumes (delete + insert): si la série était déjà
    # matchée à Komga, les liens directs par volume seraient sinon perdus jusqu'au
    # prochain matching manuel. Best-effort: n'échoue pas le scan si Komga est
    # indisponible/non configuré.
    conn = _connection()
    cursor = conn.cursor()
    cursor.execute('SELECT title, komga_series_id, ebdz_thread_id FROM series WHERE id = ?', (series_id,))
    row = cursor.fetchone()
    conn.close()

    if row and row['komga_series_id']:
        from blueprints.komga.client import KomgaClient, KomgaError
        try:
            client = KomgaClient()
            _helpers()._sync_komga_books(series_id, row['komga_series_id'], client)
        except KomgaError:
            pass
    elif row:
        # Pas encore matchée à Komga: tenter un matching automatique par titre
        from blueprints.komga.client import KomgaClient, KomgaError
        try:
            client = KomgaClient()
            _helpers()._try_komga_title_match(series_id, row['title'], client)
        except KomgaError:
            pass

    # Idem côté EBDZ: tenter/rafraîchir le matching automatique par titre si la série
    # n'est pas déjà matchée à un thread précis (recherche locale, pas d'appel réseau)
    if row and not row['ebdz_thread_id']:
        try:
            _helpers()._ebdz_enrich_series(series_id)
        except Exception:
            pass

    return volumes_count
