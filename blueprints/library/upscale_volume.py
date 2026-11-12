"""Upscaling hors ligne d'un tome sur le serveur : les pages en basse définition sont
agrandies x2 (modèles de upscale.py) et le CBZ est remplacé par la version HD. Lancé depuis
la molette d'un tome (« Upscaler en HD ») ou Vérification > Albums non HD.

L'original n'est jamais perdu : il est déplacé (même disque, donc instantané) dans
<bibliothèque>/.bullarr-sd-backup/<chemin du tome>, ignoré par le scan, et noté dans la
table upscale_backups. « Restaurer la version SD » le remet en place.

Un seul tome à la fois (un fil de travail) : sur un petit serveur, une page prend de
quelques secondes à plusieurs minutes selon le modèle. La file est aussi notée en base
(upscale_queue) et chaque page calculée est gardée dans <data>/upscale-work/ : après un
redémarrage de Bullarr, la file repart et le tome interrompu reprend à sa dernière page. Le
nouveau fichier est écrit à côté de l'original, vérifié (archive lisible, même nombre de
pages), puis le remplace d'un coup ; une annulation ou une erreur laisse l'original intact.
"""
import io
import json
import os
import shutil
import sqlite3
import threading
import time
import zipfile
from collections import OrderedDict

from . import upscale

_lock = threading.Lock()
_wake = threading.Condition(_lock)
_jobs = OrderedDict()   # volume_id -> état (dict)
_worker = None
_app = None          # application Flask du fil de travail (file en base)
_resumed = set()     # tomes remis dans la file au démarrage (historique : « repris »)
_MAX_FINISHED = 20
BACKUP_DIR_NAME = '.bullarr-sd-backup'


class UpscaleCancelled(Exception):
    pass


# ----- Réduction (« downscale ») des tomes trop lourds -----
# Même file, même reprise, même sauvegarde que l'upscaling : une réduction est un « modèle »
# de la forme downscale:<px>:keep|drop (keep = original gardé et restaurable, drop = original
# supprimé une fois la version réduite vérifiée).
DOWNSCALE_PREFIX = 'downscale:'
DOWNSCALE_CHOICES = (2000, 2400, 2800, 3200)
DOWNSCALE_QUALITY = 88
HEAVY_PAGE_BYTES = 3 * 1024 * 1024      # plus de 3 Mo par page en moyenne : « fichier lourd »
REENCODE_PAGE_BYTES = 1024 * 1024       # page PNG/WebP… de plus d'1 Mo : réencodée en JPEG


def downscale_model(target, keep):
    return f'{DOWNSCALE_PREFIX}{int(target)}:{"keep" if keep else "drop"}'


def parse_downscale(model):
    """(taille cible, original gardé) pour un modèle downscale:…, sinon None."""
    if not isinstance(model, str) or not model.startswith(DOWNSCALE_PREFIX):
        return None
    parts = model[len(DOWNSCALE_PREFIX):].split(':')
    try:
        return int(parts[0]), (parts[1] if len(parts) > 1 else 'keep') != 'drop'
    except (ValueError, IndexError):
        return None


def downscale_choices():
    """Tailles proposées : toujours au-dessus du seuil de basse définition, pour qu'un tome
    réduit ne redevienne pas « Basse déf. » (et upscalable)."""
    return [side for side in DOWNSCALE_CHOICES if side > upscale.low_def_max_side()]


def job_label(model):
    """Libellé d'un travail : modèle d'upscaling, ou « Réduction à 2400 px »."""
    spec = parse_downscale(model)
    if spec:
        return f'Réduction à {spec[0]} px'
    return upscale.MODEL_LABELS.get(model, model)


def _texts(model):
    if parse_downscale(model):
        return {'noun': 'réduction', 'requested': 'réduction demandée', 'cancelled': 'réduction annulée',
                'impossible': 'réduction impossible', 'type': 'downscale'}
    return {'noun': 'upscaling', 'requested': 'upscaling demandé', 'cancelled': 'upscaling annulé',
            'impossible': 'upscaling impossible', 'type': 'upscale'}


def downscale_image(data, target):
    """Page réduite (plus grand côté ramené à `target` px) et réencodée en JPEG, ou None si
    elle reste telle quelle. Une page déjà à la bonne taille mais lourde (PNG…) est seulement
    réencodée, si ça la rend plus légère."""
    from PIL import Image
    try:
        image = Image.open(io.BytesIO(data))
    except Image.DecompressionBombError:
        # Scan géant (plus de 180 Mpx) : c'est justement ce qu'on veut réduire.
        limit, Image.MAX_IMAGE_PIXELS = Image.MAX_IMAGE_PIXELS, None
        try:
            image = Image.open(io.BytesIO(data))
        finally:
            Image.MAX_IMAGE_PIXELS = limit
    with image:
        width, height = image.size
        too_big = max(width, height) > target
        heavy = (image.format or '').upper() not in ('JPEG', 'JPG') and len(data) > REENCODE_PAGE_BYTES
        if not too_big and not heavy:
            return None
        image.load()
        page = image
        if page.mode not in ('RGB', 'L'):
            page = page.convert('RGBA') if 'A' in page.getbands() or page.mode == 'P' else page.convert('RGB')
            if page.mode == 'RGBA':
                flat = Image.new('RGB', page.size, (255, 255, 255))
                flat.paste(page, mask=page.split()[-1])
                page = flat
        if too_big:
            scale = target / max(width, height)
            page = page.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.LANCZOS)
        buffer = io.BytesIO()
        page.save(buffer, 'JPEG', quality=DOWNSCALE_QUALITY, optimize=True, progressive=True)
    result = buffer.getvalue()
    if not too_big and len(result) >= len(data):
        return None
    return result


def _db(app):
    conn = sqlite3.connect(app.config['DATABASE'], timeout=120.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_table(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS upscale_backups (
        volume_id INTEGER PRIMARY KEY,
        series_id INTEGER,
        backup_path TEXT NOT NULL,
        original_size INTEGER,
        page_width INTEGER,
        page_height INTEGER,
        model TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS upscale_queue (
        volume_id INTEGER PRIMARY KEY,
        model TEXT NOT NULL,
        queued_at REAL NOT NULL)''')


def _queue_add(app, volume_id, model, queued_at):
    conn = _db(app)
    try:
        ensure_table(conn)
        conn.execute('INSERT OR REPLACE INTO upscale_queue (volume_id, model, queued_at) VALUES (?, ?, ?)',
                     (volume_id, model, queued_at))
        conn.commit()
    finally:
        conn.close()


def _queue_remove(app, volume_id):
    if app is None:
        return
    try:
        conn = _db(app)
        try:
            ensure_table(conn)
            conn.execute('DELETE FROM upscale_queue WHERE volume_id = ?', (volume_id,))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as e:
        print(f'File d\'upscaling : tome #{volume_id} non retiré ({e})')


def _work_root(app):
    return os.path.join(os.path.dirname(os.path.abspath(app.config['DATABASE'])), 'upscale-work')


def work_dir_for(app, row, model):
    """Dossier des pages déjà calculées d'un tome. Son nom dépend du fichier (date, taille)
    et du modèle : si l'un change, les anciennes pages ne sont pas reprises."""
    try:
        info = os.stat(row['filepath'])
        key = f"{row['id']}-{model}-{int(info.st_mtime)}-{info.st_size}"
    except OSError:
        key = f"{row['id']}-{model}"
    return os.path.join(_work_root(app), key)


def _clear_work(app, volume_id, keep=None):
    """Supprime les pages gardées d'un tome (sauf le dossier `keep`)."""
    root = _work_root(app)
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        path = os.path.join(root, name)
        if name.split('-', 1)[0] == str(volume_id) and path != keep:
            shutil.rmtree(path, ignore_errors=True)


def resume(app, delay=20):
    """Au démarrage : remet dans la file les tomes qui y étaient avant l'arrêt de Bullarr
    (le tome interrompu reprend à sa dernière page) et nettoie les pages orphelines."""
    def run():
        time.sleep(delay)
        with app.app_context():   # enqueue lit la configuration (current_app)
            _resume_now(app)
    threading.Thread(target=run, name='upscale-resume', daemon=True).start()


def _resume_now(app):
    """Reprise immédiate (dans un contexte Flask) : voir resume()."""
    try:
        conn = _db(app)
        try:
            ensure_table(conn)
            rows = conn.execute('SELECT volume_id, model FROM upscale_queue ORDER BY queued_at').fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        print(f'Reprise de l\'upscaling impossible : {e}')
        return
    queued = {row['volume_id'] for row in rows}
    root = _work_root(app)
    for name in (os.listdir(root) if os.path.isdir(root) else []):
        try:
            if int(name.split('-', 1)[0]) not in queued:
                shutil.rmtree(os.path.join(root, name), ignore_errors=True)
        except ValueError:
            shutil.rmtree(os.path.join(root, name), ignore_errors=True)
    resumed = 0
    for row in rows:
        volume = _volume_row(app, row['volume_id'])
        if volume and volume['filepath']:
            directory, name = os.path.split(volume['filepath'])
            temp = os.path.join(directory, f'.{name}.hd-tmp')   # fichier HD à moitié écrit
            if os.path.exists(temp):
                try:
                    os.remove(temp)
                except OSError:
                    pass
        state, error = enqueue(app, row['volume_id'], row['model'])
        if not error:
            _resumed.add(row['volume_id'])
        if error:
            print(f'Upscaling du tome #{row["volume_id"]} non repris : {error}')
            _queue_remove(app, row['volume_id'])
            _clear_work(app, row['volume_id'])
        else:
            resumed += 1
    if resumed:
        print(f'✓ Upscaling : {resumed} tome(s) remis dans la file après le redémarrage')


def _volume_row(app, volume_id):
    conn = _db(app)
    try:
        return conn.execute('''
            SELECT v.id, v.series_id, v.filename, v.filepath, v.format, v.page_width, v.page_height,
                   v.file_size, v.page_count, v.volume_number, s.title AS series_title, l.path AS library_path
            FROM volumes v JOIN series s ON s.id = v.series_id
            LEFT JOIN libraries l ON l.id = s.library_id WHERE v.id = ?''', (volume_id,)).fetchone()
    finally:
        conn.close()


def _match_owner(path, reference):
    """Donne à `path` le mode et le propriétaire de `reference` : le conteneur tourne en
    root, mais Komga et les autres outils doivent pouvoir lire le fichier HD."""
    try:
        info = os.stat(reference)
        os.chmod(path, info.st_mode & 0o7777)
        os.chown(path, info.st_uid, info.st_gid)
    except OSError:
        pass


def _makedirs_like(directory, reference_dir):
    """Crée `directory` (et ses parents) avec le propriétaire de `reference_dir`."""
    missing = []
    current = directory
    while current and not os.path.isdir(current):
        missing.append(current)
        current = os.path.dirname(current)
    os.makedirs(directory, exist_ok=True)
    for path in missing:
        _match_owner(path, reference_dir)


def backup_path_for(filepath, library_path):
    """Emplacement de la sauvegarde SD : sous la racine de la bibliothèque (même disque que
    le tome) ; à côté du tome s'il n'est pas dans sa bibliothèque."""
    filepath = os.path.abspath(filepath)
    root = os.path.abspath(library_path) if library_path else ''
    if root and filepath.startswith(root.rstrip(os.sep) + os.sep):
        return os.path.join(root, BACKUP_DIR_NAME, os.path.relpath(filepath, root))
    directory, name = os.path.split(filepath)
    return os.path.join(directory, BACKUP_DIR_NAME, name)


def backups(app, volume_ids=None):
    """Sauvegardes SD connues : {volume_id: ligne}, avec la taille réelle du fichier."""
    conn = _db(app)
    try:
        ensure_table(conn)
        rows = conn.execute('''
            SELECT b.*, v.filename, v.series_id AS current_series_id, s.title AS series_title
            FROM upscale_backups b LEFT JOIN volumes v ON v.id = b.volume_id
            LEFT JOIN series s ON s.id = v.series_id ORDER BY b.created_at DESC''').fetchall()
    finally:
        conn.close()
    result = {}
    for row in rows:
        if volume_ids is not None and row['volume_id'] not in volume_ids:
            continue
        item = dict(row)
        item['exists'] = os.path.isfile(row['backup_path'])
        item['bytes'] = os.path.getsize(row['backup_path']) if item['exists'] else 0
        result[row['volume_id']] = item
    return result


def has_backup(app, volume_id):
    return bool(backups(app, {volume_id}))


def check_volume(row, model=None):
    """Message d'erreur si le tome ne peut pas être upscalé (ou réduit), sinon None."""
    if not row:
        return 'Tome introuvable'
    if not row['filepath'] or not os.path.isfile(row['filepath']):
        return "Ce tome n'a pas de fichier sur le disque"
    spec = parse_downscale(model)
    if (row['format'] or '').lower() != 'cbz':
        return f"Seuls les tomes cbz peuvent être {'réduits' if spec else 'upscalés'} : convertissez-le d'abord en cbz"
    side = max(row['page_width'] or 0, row['page_height'] or 0)
    if spec:
        if side and side <= spec[0] and not _is_heavy(row):
            return f'Rien à réduire : pages de {side} px, déjà sous {spec[0]} px, et fichier pas lourd'
        return None
    if side and side > upscale.low_def_max_side():
        return f'Ce tome est déjà en bonne définition (pages de {side} px)'
    return None


def _is_heavy(row):
    try:
        pages = row['page_count'] if 'page_count' in row.keys() else None
    except (AttributeError, IndexError):
        pages = None
    size = row['file_size'] or 0
    return bool(pages) and size / pages >= HEAVY_PAGE_BYTES


def jobs():
    """États des tomes en attente, en cours et récemment terminés."""
    with _lock:
        return [dict(job) for job in _jobs.values()]


def job(volume_id):
    with _lock:
        state = _jobs.get(volume_id)
        return dict(state) if state else None


def enqueue(app, volume_id, model):
    """Ajoute un tome à la file. Renvoie (état, erreur)."""
    global _worker, _app
    spec = parse_downscale(model)
    if spec:
        if spec[0] not in downscale_choices():
            return None, f'Taille cible invalide : {spec[0]} px (au-dessus du seuil de basse définition)'
    else:
        if model not in upscale.MODELS:
            return None, 'Modèle inconnu'
        if not upscale.runtime_available():
            return None, "onnxruntime n'est pas disponible sur ce serveur"
        if not upscale.load_config().get('server_enabled', True):
            return None, "L'upscaling sur le serveur est désactivé (Configuration > Upscaling)"
    row = _volume_row(app, volume_id)
    error = check_volume(row, model)
    if error:
        return None, error
    if has_backup(app, volume_id):
        kept = backups(app, {volume_id}).get(volume_id) or {}
        if parse_downscale(kept.get('model')):
            return None, "Ce tome a déjà été réduit : restaurez l'original ou supprimez-le d'abord"
        return None, 'Ce tome a déjà été upscalé : restaurez d\'abord la version SD'
    size = os.path.getsize(row['filepath'])
    free = shutil.disk_usage(os.path.dirname(row['filepath'])).free
    if free < size * (2 if spec else 5):
        return None, f"Pas assez de place sur le disque pour écrire la version {'réduite' if spec else 'HD'}"
    with _lock:
        current = _jobs.get(volume_id)
        if current and current['status'] in ('queued', 'running'):
            return dict(current), None
        _jobs.pop(volume_id, None)
        _jobs[volume_id] = {
            'volume_id': volume_id, 'series_id': row['series_id'], 'series_title': row['series_title'],
            'filename': row['filename'], 'model': model, 'status': 'queued',
            'page': 0, 'total': 0, 'upscaled': 0, 'error': None, 'cancel': False,
            'queued_at': time.time(), 'started_at': None, 'finished_at': None,
        }
        state = dict(_jobs[volume_id])
        _app = app
        _queue_add(app, volume_id, model, state['queued_at'])
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, args=(app,), name='upscale-volumes', daemon=True)
            _worker.start()
        _wake.notify_all()
    return state, None


def cancel(volume_id):
    with _lock:
        state = _jobs.get(volume_id)
        if not state or state['status'] not in ('queued', 'running'):
            return False
        if state['status'] == 'queued':
            state['status'] = 'cancelled'
            state['finished_at'] = time.time()
        else:
            state['cancel'] = True
    if state['status'] == 'cancelled':
        _queue_remove(_app, volume_id)
    return True


def _next_job():
    with _lock:
        while True:
            for volume_id, state in _jobs.items():
                if state['status'] == 'queued':
                    state['status'] = 'running'
                    state['started_at'] = time.time()
                    return volume_id, state['model']
            _wake.wait(timeout=300)


def _update(volume_id, **values):
    with _lock:
        if volume_id in _jobs:
            _jobs[volume_id].update(values)


def _cancelled(volume_id):
    with _lock:
        return bool(_jobs.get(volume_id, {}).get('cancel'))


def _prune_finished():
    with _lock:
        finished = [vid for vid, state in _jobs.items() if state['status'] not in ('queued', 'running')]
        for vid in finished[:-_MAX_FINISHED]:
            _jobs.pop(vid, None)


def _run(app):
    while True:
        volume_id, model = _next_job()
        _log_start(app, volume_id, model)
        try:
            with app.app_context():
                _process(app, volume_id, model)
            _update(volume_id, status='done', finished_at=time.time())
        except UpscaleCancelled:
            _update(volume_id, status='cancelled', finished_at=time.time())
        except Exception as e:  # noqa: BLE001 - l'erreur est montrée dans l'interface
            _update(volume_id, status='error', error=str(e) or e.__class__.__name__, finished_at=time.time())
            print(f"{_texts(model)['noun'].capitalize()} du tome #{volume_id} impossible : {e}")
            _log_failure(app, volume_id, model, e)
        # Terminé, annulé ou en échec : il sort de la file en base et ses pages gardées partent.
        _queue_remove(app, volume_id)
        _clear_work(app, volume_id)
        _prune_finished()


def _log_start(app, volume_id, model):
    """Le calcul d'un tome commence (ou reprend après un redémarrage) : noté dans l'historique."""
    resumed = volume_id in _resumed
    _resumed.discard(volume_id)
    try:
        from .action_history import log_action
        with app.app_context():
            row = _volume_row(app, volume_id)
            if not row:
                return
            label = job_label(model)
            what = 'calcul repris après le redémarrage de Bullarr' if resumed else 'calcul commencé'
            log_action(_texts(model)['type'] + '_start', row['series_id'], row['series_title'],
                       f"{row['filename']} : {what} ({label})",
                       files_json=_volumes_json(_volume_entry(row, model, note=what[0].upper() + what[1:])))
    except Exception:  # noqa: BLE001
        pass


def _volumes_json(*volumes):
    """files_json d'une entrée d'historique : les albums concernés, affichés en dépliant la ligne."""
    return json.dumps({'volumes': [v for v in volumes if v]})


def _volume_kind(volume_id):
    """Type du tome (intégrale, HS, épisode) pour l'étiquette « Tome 3 » / « Hors-série 1 »."""
    try:
        from flask import current_app
        conn = sqlite3.connect(current_app.config['DATABASE'], timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute('''SELECT is_integral, integral_number, is_hs, hs_number, is_episode, episode_number
                                  FROM volumes WHERE id = ?''', (volume_id,)).fetchone()
        finally:
            conn.close()
        return {k: row[k] for k in row.keys()} if row else {}
    except Exception:  # noqa: BLE001 - colonnes absentes (tests) ou hors contexte
        return {}


def _volume_entry(row, model=None, **extra):
    if not row:
        return None
    entry = {'volume_id': row['id'], 'filename': row['filename'], 'volume_number': row['volume_number'],
             'series_id': row['series_id'], 'series_title': row['series_title'],
             'model': job_label(model) if model else None}
    entry.update({k: v for k, v in _volume_kind(row['id']).items() if v})
    if row['page_width'] and row['page_height']:
        entry['size'] = f"{row['page_width']} × {row['page_height']}"
    entry.update({k: v for k, v in extra.items() if v is not None})
    return entry


def log_queued(app, items):
    """Tomes mis dans la file (molette, page Upscaling) : une entrée par série.
    `items` = [(volume_id, model)]."""
    try:
        from .action_history import log_action
        by_series = OrderedDict()
        for volume_id, model in items:
            row = _volume_row(app, volume_id)
            if row:
                kind = _texts(model)['type']
                by_series.setdefault((row['series_id'], row['series_title'], kind), []).append((row, model))
        for (series_id, series_title, kind), volumes in by_series.items():
            texts = _texts(volumes[0][1])
            labels = sorted({job_label(model) for _row, model in volumes})
            if len(volumes) == 1:
                detail = f"{volumes[0][0]['filename']} : {texts['requested']} ({labels[0]})"
            else:
                detail = f"{len(volumes)} tomes mis en file de {texts['noun']} ({', '.join(labels)})"
            log_action(kind + '_queue', series_id, series_title, detail,
                       files_json=_volumes_json(*(_volume_entry(row, model) for row, model in volumes)))
    except Exception:  # noqa: BLE001
        pass


def log_cancelled(app, volume_id):
    try:
        from .action_history import log_action
        row = _volume_row(app, volume_id)
        if row:
            state = job(volume_id) or {}
            texts = _texts(state.get('model'))
            log_action(texts['type'] + '_cancel', row['series_id'], row['series_title'],
                       f"{row['filename']} : {texts['cancelled']}, fichier inchangé",
                       files_json=_volumes_json(_volume_entry(row, state.get('model'),
                                                              note='Annulé, fichier inchangé')))
    except Exception:  # noqa: BLE001
        pass


def log_settings(before, after):
    """Configuration > Upscaling modifiée : noté dans l'historique (sans série)."""
    changes = []
    if before.get('max_side') != after.get('max_side'):
        changes.append(f"seuil de basse définition {before.get('max_side')} → {after.get('max_side')} px")
    if before.get('model') != after.get('model'):
        changes.append('modèle par défaut : ' + upscale.MODEL_LABELS.get(after.get('model'), after.get('model')))
    if bool(before.get('server_enabled')) != bool(after.get('server_enabled')):
        changes.append('upscaling sur le serveur ' + ('activé' if after.get('server_enabled') else 'désactivé'))
    if not changes:
        return
    try:
        from .action_history import log_action
        log_action('upscale_settings', None, None, 'Réglages d\'upscaling : ' + ', '.join(changes))
    except Exception:  # noqa: BLE001
        pass


def _log_failure(app, volume_id, model, error):
    """Un upscaling en échec apparaît aussi dans l'historique."""
    try:
        from .action_history import log_action
        with app.app_context():
            row = _volume_row(app, volume_id)
            label = job_label(model)
            texts = _texts(model)
            name = row['filename'] if row else f'Tome #{volume_id}'
            log_action(texts['type'], row['series_id'] if row else None, row['series_title'] if row else None,
                       f"{name} : {texts['impossible']} ({label}), fichier inchangé",
                       success=False, error=str(error) or error.__class__.__name__,
                       files_json=_volumes_json(_volume_entry(row, model, error=str(error) or error.__class__.__name__)))
    except Exception:  # noqa: BLE001
        pass


def upscale_cbz(filepath, model, progress=None, cancelled=None, upscale_page=None, work_dir=None):
    """Écrit la version HD de `filepath` (cbz) dans un fichier temporaire voisin et le
    renvoie avec (pages agrandies, taille médiane (w, h) des pages). Les pages de plus de
    low_def_max_side() px et les autres fichiers (ComicInfo.xml…) sont recopiés tels quels.
    Avec `work_dir`, chaque page calculée y est gardée (NNNNN.jpg, ou NNNNN.same si la page
    reste telle quelle) et reprise au lieu d'être recalculée : un tome interrompu repart
    de là où il s'était arrêté."""
    from PIL import Image
    from .reader import page_index
    upscale_page = upscale_page or upscale.upscale_image
    _real, pages = page_index(filepath, 'cbz')
    page_set = set(pages)
    directory, name = os.path.split(filepath)
    temp = os.path.join(directory, f'.{name}.hd-tmp')
    upscaled = 0
    sizes = []
    try:
        with zipfile.ZipFile(filepath) as source, zipfile.ZipFile(temp, 'w') as target:
            for info in source.infolist():
                if info.is_dir() or info.filename in page_set:
                    continue
                target.writestr(info, source.read(info.filename), compress_type=info.compress_type)
            if progress:
                progress(0, len(pages), 0)   # nombre de pages connu dès le départ
            for index, page in enumerate(pages, 1):
                if cancelled and cancelled():
                    raise UpscaleCancelled()
                data = source.read(page)
                result = _cached_page(work_dir, index)
                if result is False:
                    result = upscale_page(data, model)
                    _store_page(work_dir, index, result)
                stem, ext = os.path.splitext(page)
                if result is not None:
                    upscaled += 1
                    data = result
                    page = f'{stem}.jpg'
                with Image.open(io.BytesIO(data)) as image:
                    sizes.append(image.size)
                target.writestr(page, data, compress_type=zipfile.ZIP_STORED)
                if progress:
                    progress(index, len(pages), upscaled)
        with zipfile.ZipFile(temp) as check:
            if check.testzip() is not None:
                raise ValueError('Archive HD corrompue')
            written = [n for n in check.namelist() if n.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp'))]
            if len(written) < len(pages):
                raise ValueError('Pages manquantes dans la version HD')
    except BaseException:
        if os.path.exists(temp):
            os.remove(temp)
        raise
    sizes.sort(key=lambda size: max(size))
    return temp, upscaled, sizes[len(sizes) // 2] if sizes else None


def _cached_page(work_dir, index):
    """Page déjà calculée : ses octets, None (page gardée telle quelle) ou False (à faire)."""
    if not work_dir:
        return False
    base = os.path.join(work_dir, f'{index:05d}')
    if os.path.isfile(base + '.same'):
        return None
    try:
        with open(base + '.jpg', 'rb') as handle:
            data = handle.read()
        return data or False
    except OSError:
        return False


def _store_page(work_dir, index, result):
    if not work_dir:
        return
    try:
        os.makedirs(work_dir, exist_ok=True)
        base = os.path.join(work_dir, f'{index:05d}')
        temp = base + '.part'
        with open(temp, 'wb') as handle:
            handle.write(result or b'')
        os.replace(temp, base + ('.jpg' if result is not None else '.same'))
    except OSError as e:   # disque plein… : on continue, la page sera juste recalculée
        print(f'Page {index} non gardée pour la reprise : {e}')


def _process(app, volume_id, model):
    row = _volume_row(app, volume_id)
    error = check_volume(row, model)
    if error:
        raise ValueError(error)
    spec = parse_downscale(model)
    filepath = row['filepath']
    work_dir = work_dir_for(app, row, model.replace(':', '_'))
    _clear_work(app, volume_id, keep=work_dir)   # pages d'un autre modèle ou d'un fichier modifié
    page_function = (lambda data, _model: downscale_image(data, spec[0])) if spec else None
    temp, upscaled, median = upscale_cbz(
        filepath, model,
        progress=lambda page, total, done: _update(volume_id, page=page, total=total, upscaled=done),
        cancelled=lambda: _cancelled(volume_id), work_dir=work_dir, upscale_page=page_function)
    if not upscaled:
        os.remove(temp)
        raise ValueError(f'Aucune page à réduire sous {spec[0]} px dans ce tome' if spec
                         else 'Aucune page en basse définition dans ce tome')
    if _cancelled(volume_id):
        os.remove(temp)
        raise UpscaleCancelled()
    original_size = os.path.getsize(filepath)
    if spec and os.path.getsize(temp) >= original_size:
        os.remove(temp)
        raise ValueError("La version réduite n'est pas plus légère que l'original : fichier inchangé")
    try:
        shutil.copymode(filepath, temp)
    except OSError:
        pass
    if spec and not spec[1]:
        _replace_without_backup(app, row, temp, upscaled, median, model, original_size)
        return
    # L'original part dans la sauvegarde (déplacement sur le même disque), puis la version HD
    # prend sa place ; en cas d'échec entre les deux, l'original revient.
    backup = backup_path_for(filepath, row['library_path'])
    if os.path.exists(backup):
        os.remove(temp)
        raise ValueError(f'Une sauvegarde existe déjà : {backup}')
    _makedirs_like(os.path.dirname(backup), os.path.dirname(filepath))
    _match_owner(temp, filepath)
    shutil.move(filepath, backup)
    try:
        os.replace(temp, filepath)
    except BaseException:
        shutil.move(backup, filepath)
        if os.path.exists(temp):
            os.remove(temp)
        raise
    size = os.path.getsize(filepath)
    conn = _db(app)
    try:
        ensure_table(conn)
        conn.execute('''INSERT OR REPLACE INTO upscale_backups
            (volume_id, series_id, backup_path, original_size, page_width, page_height, model)
            VALUES (?, ?, ?, ?, ?, ?, ?)''', (volume_id, row['series_id'], backup, original_size,
                                           row['page_width'], row['page_height'], model))
        conn.execute('UPDATE volumes SET file_size = ?, page_width = ?, page_height = ?, page_size_file_size = ? WHERE id = ?',
                     (size, median[0] if median else None, median[1] if median else None, size, volume_id))
        conn.commit()
    finally:
        conn.close()
    _after_change(row)
    _log_done(row, model, upscaled, median, original_size, size, kept=True)


def _mb(size):
    return f'{size / 1048576:.0f} Mo' if size >= 10 * 1048576 else f'{size / 1048576:.1f} Mo'


def _log_done(row, model, changed, median, before_size, after_size, kept):
    try:
        from .action_history import log_action
        label = job_label(model)
        after = f'{median[0]} × {median[1]}' if median else None
        spec = parse_downscale(model)
        if spec:
            what = f"{changed} pages réduites ({_mb(before_size)} → {_mb(after_size)})"
            note = what + (', original gardé' if kept else ', original supprimé')
        else:
            what = f'{changed} pages agrandies x2'
            note = what + ', original SD gardé'
        log_action('downscale' if spec else 'upscale', row['series_id'], row['series_title'],
                   f"{row['filename']} : {what} ({label}), {'original gardé' if kept else 'original supprimé'}",
                   files_json=_volumes_json(_volume_entry(row, model, after=after, note=note)))
    except Exception:  # noqa: BLE001
        pass


def _replace_without_backup(app, row, temp, changed, median, model, original_size):
    """Réduction sans garder l'original (choisi par l'utilisateur) : la version réduite,
    déjà vérifiée (archive lisible, toutes les pages), remplace le fichier d'un coup."""
    filepath = row['filepath']
    _match_owner(temp, filepath)
    os.replace(temp, filepath)
    size = os.path.getsize(filepath)
    conn = _db(app)
    try:
        conn.execute('UPDATE volumes SET file_size = ?, page_width = ?, page_height = ?, page_size_file_size = ? WHERE id = ?',
                     (size, median[0] if median else None, median[1] if median else None, size, row['id']))
        conn.commit()
    finally:
        conn.close()
    _after_change(row)
    _log_done(row, model, changed, median, original_size, size, kept=False)


def _after_change(row):
    try:
        from .scanner import LibraryScanner
        LibraryScanner().update_series_stats(row['series_id'])
    except Exception as e:  # noqa: BLE001
        print(f'Statistiques de la série non mises à jour : {e}')
    try:
        from blueprints.komga.client import trigger_scan_async
        trigger_scan_async()
    except Exception:  # noqa: BLE001
        pass


def restore(app, volume_id):
    """Remet la version SD gardée à la place de la version HD. Renvoie une erreur ou None."""
    with _lock:
        state = _jobs.get(volume_id)
        if state and state['status'] in ('queued', 'running'):
            return 'Traitement en cours pour ce tome'
    backup = backups(app, {volume_id}).get(volume_id)
    if not backup:
        return "Aucune version d'origine gardée pour ce tome"
    if not backup['exists']:
        return f"La sauvegarde n'est plus sur le disque : {backup['backup_path']}"
    row = _volume_row(app, volume_id)
    if not row or not row['filepath']:
        return 'Tome introuvable'
    shutil.move(backup['backup_path'], row['filepath'])
    _remove_empty_dirs(os.path.dirname(backup['backup_path']))
    conn = _db(app)
    try:
        size = os.path.getsize(row['filepath'])
        conn.execute('UPDATE volumes SET file_size = ?, page_width = ?, page_height = ?, page_size_file_size = ? WHERE id = ?',
                     (size, backup['page_width'], backup['page_height'], size, volume_id))
        conn.execute('DELETE FROM upscale_backups WHERE volume_id = ?', (volume_id,))
        conn.commit()
    finally:
        conn.close()
    with _lock:
        _jobs.pop(volume_id, None)
    _after_change(row)
    try:
        from .action_history import log_action
        reduced = parse_downscale(backup.get('model'))
        log_action('downscale_restore' if reduced else 'upscale_restore', row['series_id'], row['series_title'],
                   f"{row['filename']} : {'original remis' if reduced else 'version SD remise'}",
                   files_json=_volumes_json(_volume_entry(dict(row, page_width=backup['page_width'], page_height=backup['page_height']),
                                                          backup.get('model'),
                                                          note='Original remis à la place de la version réduite' if reduced
                                                          else 'Version SD remise à la place de la HD')))
    except Exception:  # noqa: BLE001
        pass
    return None


def delete_backup(app, volume_id):
    """Supprime la version SD gardée (la version HD reste). Renvoie une erreur ou None."""
    backup = backups(app, {volume_id}).get(volume_id)
    if not backup:
        return "Aucune version d'origine gardée pour ce tome"
    if backup['exists']:
        os.remove(backup['backup_path'])
        _remove_empty_dirs(os.path.dirname(backup['backup_path']))
    conn = _db(app)
    try:
        conn.execute('DELETE FROM upscale_backups WHERE volume_id = ?', (volume_id,))
        conn.commit()
    finally:
        conn.close()
    try:
        from .action_history import log_action
        row = _volume_row(app, volume_id)
        if row:
            reduced = parse_downscale(backup.get('model'))
            log_action('downscale_backup_delete' if reduced else 'upscale_backup_delete', row['series_id'], row['series_title'],
                       f"{row['filename']} : " + ('original gardé supprimé, la version réduite reste' if reduced
                                                  else 'version SD gardée supprimée, la version HD reste'),
                       files_json=_volumes_json(_volume_entry(row, backup.get('model'),
                                                              note='Original supprimé, la version réduite reste' if reduced
                                                              else 'Version SD supprimée, la HD reste')))
    except Exception:  # noqa: BLE001
        pass
    return None


def _remove_empty_dirs(directory):
    """Retire les dossiers vides de la sauvegarde, jusqu'à .bullarr-sd-backup compris."""
    while directory and BACKUP_DIR_NAME in directory.split(os.sep):
        try:
            os.rmdir(directory)
        except OSError:
            return
        if os.path.basename(directory) == BACKUP_DIR_NAME:
            return
        directory = os.path.dirname(directory)


def low_definition_volumes(app):
    """Tomes possédés en basse définition (côté le plus grand des pages ≤ seuil réglable, 1600 px par défaut, comme
    l'étiquette « Basse déf. »), proposés à l'upscaling sur la page Upscaling."""
    from . import upscale
    conn = _db(app)
    try:
        ensure_table(conn)
        rows = conn.execute('''
            SELECT v.id, v.series_id, v.filename, v.format, v.page_width, v.page_height,
                   v.file_size, v.page_count, s.title AS series_title,
                   (SELECT 1 FROM upscale_backups b WHERE b.volume_id = v.id) AS has_backup
            FROM volumes v JOIN series s ON s.id = v.series_id
            WHERE v.filepath IS NOT NULL AND v.page_width > 0
              AND MAX(v.page_width, v.page_height) <= ?
            ORDER BY s.title COLLATE NOCASE, v.volume_number, v.filename''', (upscale.low_def_max_side(),)).fetchall()
    finally:
        conn.close()
    items = []
    for row in rows:
        state = job(row['id'])
        fmt = (row['format'] or '').lower()
        reason = None
        if row['has_backup']:
            reason = 'Déjà upscalé (version SD gardée)'
        elif fmt != 'cbz':
            reason = "Convertir d'abord en CBZ"
        items.append({
            'volume_id': row['id'], 'series_id': row['series_id'], 'series_title': row['series_title'],
            'filename': row['filename'], 'format': fmt, 'page_width': row['page_width'],
            'page_height': row['page_height'], 'file_size': row['file_size'], 'page_count': row['page_count'],
            'upscalable': reason is None, 'reason': reason,
            'job': {k: state.get(k) for k in ('status', 'page', 'total', 'error', 'model')} if state else None,
        })
    return items


def heavy_volumes(app):
    """Tomes possédés trop lourds (plus de 3 Mo par page en moyenne, ou pages de plus de
    3200 px), proposés à la réduction sur la page Upscaling."""
    conn = _db(app)
    try:
        ensure_table(conn)
        rows = conn.execute('''
            SELECT v.id, v.series_id, v.filename, v.format, v.page_width, v.page_height,
                   v.file_size, v.page_count, s.title AS series_title,
                   (SELECT model FROM upscale_backups b WHERE b.volume_id = v.id) AS backup_model
            FROM volumes v JOIN series s ON s.id = v.series_id
            WHERE v.filepath IS NOT NULL AND v.page_count > 0 AND v.file_size > 0
              AND (v.file_size * 1.0 / v.page_count >= ? OR MAX(v.page_width, v.page_height) > ?)
            ORDER BY v.file_size * 1.0 / v.page_count DESC''', (HEAVY_PAGE_BYTES, max(DOWNSCALE_CHOICES))).fetchall()
    finally:
        conn.close()
    items = []
    for row in rows:
        state = job(row['id'])
        fmt = (row['format'] or '').lower()
        reason = None
        if row['backup_model']:
            reason = ('Déjà réduit (original gardé)' if parse_downscale(row['backup_model'])
                      else 'Upscalé (version SD gardée)')
        elif fmt != 'cbz':
            reason = "Convertir d'abord en CBZ"
        items.append({
            'volume_id': row['id'], 'series_id': row['series_id'], 'series_title': row['series_title'],
            'filename': row['filename'], 'format': fmt, 'page_width': row['page_width'],
            'page_height': row['page_height'], 'file_size': row['file_size'], 'page_count': row['page_count'],
            'reducible': reason is None, 'reason': reason,
            'job': {k: state.get(k) for k in ('status', 'page', 'total', 'error', 'model')} if state else None,
        })
    return items
