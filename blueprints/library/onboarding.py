"""Import guidé d'une bibliothèque à partir de fichiers déjà existants sur disque.

"I want to set up adding a new bibliothèque from existing files. it will need to match
series and checking current comicinfo" - la création d'une bibliothèque (POST
/api/libraries), le scan complet (LibraryScanner.scan_directory) et le matching Bédéthèque
par titre (BedethequeScraper.search_and_get_best_match, via _perform_series_match côté
bedetheque/routes.py) existent déjà chacun séparément et fonctionnent très bien seuls -
mais rien ne les enchaîne : aujourd'hui, ajouter une bibliothèque qui contient déjà des BD
demande de créer la bibliothèque (POST /api/libraries, aucun scan), puis cliquer
"Scanner" séparément, puis aller sur /verification chercher les séries non matchées, puis
les matcher une par une ou via la sélection en masse là-bas. Ce module fait juste
l'enchaînement scan -> matching -> résumé ComicInfo pour CE cas d'usage précis (une
bibliothèque qui a déjà du contenu), sans dupliquer la logique de scan/matching/
vérification elle-même.

Tourne en thread d'arrière-plan (même raison que _write_series_volumes_metadata_async,
bedetheque/routes.py: une recherche Bédéthèque par série, à ~1s d'anti-bot delay chacune
- déjà géré à l'intérieur de search_and_get_best_match, pas besoin de re-paçer ici -,
dépasserait vite le timeout d'un reverse proxy pour une bibliothèque de plus de quelques
séries). Un matching réussi n'utilise QUE write_volumes=False (voir _perform_series_match) :
aligne series.bedetheque_url/title et crée les tomes placeholder Bédéthèque, mais
n'écrase JAMAIS le ComicInfo.xml déjà présent dans les fichiers - "checking current
comicinfo" est un diagnostic, pas une réécriture automatique en masse, même logique que
write_volumes=false ailleurs dans l'app pour un matching non confirmé par un humain."""
import threading

# État de progression en mémoire, une entrée par library_id - même principe que
# _metadata_write_progress (bedetheque/routes.py)/_build_progress (catalog_index.py) :
# consultable via GET /api/libraries/<id>/onboard/status pendant qu'un import tourne.
_onboard_progress = {}


def get_onboard_status(library_id):
    return _onboard_progress.get(library_id)


def start_library_onboarding(app, library_id):
    """Démarre l'import guidé en arrière-plan. Garde-fou anti-double-lancement (comme
    _start_metadata_write_thread) : un library_id déjà 'running' n'en relance pas un
    second par-dessus, qui courrait la même bibliothèque deux fois en parallèle."""
    existing = _onboard_progress.get(library_id)
    if existing and existing.get('running'):
        return False
    _onboard_progress[library_id] = {
        'running': True, 'phase': 'scanning', 'done': 0, 'total': 0,
        'current_series': None, 'matched': [], 'uncertain': [],
        'comicinfo_summary': None, 'error': None,
    }
    thread = threading.Thread(target=_run_library_onboarding, args=(app, library_id), daemon=True)
    thread.start()
    return True


def _run_library_onboarding(app, library_id):
    # app.app_context() explicite: un thread d'arrière-plan n'hérite jamais du contexte
    # de la requête qui l'a lancé (voir CLAUDE.md - a déjà mordu deux fois dans cette
    # session pour des threads similaires) - toute la chaîne (scan, current_app.config
    # dans le scanner/le matching, requêtes SQLite) en a besoin.
    progress = _onboard_progress[library_id]
    try:
        with app.app_context():
            library_path = _fetch_library_path(library_id)
            _run_scan_phase(library_id, library_path, progress)
            _run_match_phase(library_id, progress)
            _run_comicinfo_phase(library_id, progress)
        progress['phase'] = 'done'
    except Exception as e:
        progress['error'] = str(e)
        progress['phase'] = 'error'
    finally:
        progress['running'] = False
        progress['current_series'] = None


def _fetch_library_path(library_id):
    import sqlite3
    from flask import current_app
    conn = sqlite3.connect(current_app.config['DATABASE'], timeout=30.0)
    row = conn.execute('SELECT path FROM libraries WHERE id = ?', (library_id,)).fetchone()
    conn.close()
    if not row:
        raise ValueError('Bibliothèque introuvable')
    return row[0]


def _run_scan_phase(library_id, library_path, progress):
    import os
    progress['phase'] = 'scanning'
    if not library_path or not os.path.isdir(library_path):
        raise ValueError(f"Chemin introuvable ou inaccessible : {library_path}")
    # _scan_library_and_sync (routes.py) plutôt que LibraryScanner().scan_directory() nu:
    # même comportement qu'un clic manuel sur "Scanner" (synchronisation Komga/EBDZ
    # incluse), pas juste le scan de fichiers seul - voir son docstring.
    from .routes import _scan_library_and_sync
    _scan_library_and_sync(library_id, library_path)


def _run_match_phase(library_id, progress):
    """Tente un matching par titre pour chaque série tout juste scannée qui n'a pas déjà
    de bedetheque_url - une bibliothèque réimportée peut très bien avoir des séries
    déjà matchées par ailleurs (mêmes chemins qu'une bibliothèque existante rescannée),
    inutile de les re-matcher."""
    import sqlite3
    from flask import current_app
    progress['phase'] = 'matching'

    conn = sqlite3.connect(current_app.config['DATABASE'], timeout=30.0)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, title FROM series WHERE library_id = ? AND bedetheque_url IS NULL "
        "ORDER BY title COLLATE NOCASE",
        (library_id,)
    ).fetchall()
    conn.close()

    progress['total'] = len(rows)
    from blueprints.bedetheque.routes import _perform_series_match

    for row in rows:
        series_id, title = row['id'], row['title']
        progress['current_series'] = title
        try:
            # write_volumes=False: voir le docstring du module en tête de fichier - un
            # matching automatique/non confirmé n'écrase jamais le ComicInfo déjà présent.
            success, info, error = _perform_series_match(series_id, title, 'title', title, write_volumes=False)
        except Exception as e:
            success, info, error = False, None, str(e)
        if success:
            progress['matched'].append({
                'series_id': series_id, 'title': title,
                'bedetheque_title': info.get('title'), 'bedetheque_url': info.get('url'),
            })
        else:
            progress['uncertain'].append({'series_id': series_id, 'title': title, 'reason': error})
        progress['done'] += 1
    progress['current_series'] = None


def _run_comicinfo_phase(library_id, progress):
    """Résumé ComicInfo scopé à cette seule bibliothèque - réutilise telle quelle
    _verify_missing_metadata (settings/routes.py, alimente /verification), filtrée sur
    library_id après coup plutôt que dupliquer sa logique de vérification par champ."""
    progress['phase'] = 'checking_comicinfo'
    from blueprints.settings.routes import _load_verification_series_and_volumes, _verify_missing_metadata

    series_rows, _volume_rows, volumes_by_series = _load_verification_series_and_volumes()
    library_series = [s for s in series_rows if s['library_id'] == library_id]
    library_series_ids = {s['id'] for s in library_series}
    library_volumes_by_series = {sid: vols for sid, vols in volumes_by_series.items() if sid in library_series_ids}

    issues = _verify_missing_metadata(library_series, library_volumes_by_series)
    series_issue_ids = {i['series_id'] for i in issues if i['type'] == 'series'}
    volume_issues = [i for i in issues if i['type'] == 'volume']
    progress['comicinfo_summary'] = {
        'series_count': len(library_series),
        'series_unmatched': len(series_issue_ids),
        'volumes_with_issues': len(volume_issues),
        'issues': issues[:200],  # aperçu - le détail complet reste consultable sur /verification
    }
