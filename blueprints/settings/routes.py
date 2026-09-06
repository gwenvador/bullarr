"""
Routes pour la page de configuration
"""
import os
import io
import json
import shutil
import sqlite3
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from flask import render_template, request, jsonify, current_app, send_file
from . import settings_bp
from .rename_config_store import load_rename_config, save_rename_config, DEFAULT_RENAME_CONFIG


@settings_bp.route('/settings')
def settings_page():
    """Page de configuration"""
    return render_template('settings.html')


@settings_bp.route('/history')
def history_page():
    """Page Historique: imports + téléchargements automatiques, page à part entière
    (au-dessus de Configuration dans la barre latérale) plutôt qu'un onglet des
    paramètres - voir static/js/history.js pour le chargement des données."""
    return render_template('history.html')


@settings_bp.route('/verification')
def verification_page():
    """Page Vérification: sortie de l'onglet Configuration vers une page à part entière
    (même raison que /history ci-dessus - un diagnostic qu'on consulte directement, pas
    un réglage qu'on configure une fois) - voir static/js/verification.js pour le
    chargement des données et GET /api/settings/verification pour l'analyse."""
    return render_template('verification.html')


@settings_bp.route('/api/settings/rename', methods=['GET', 'POST'])
def rename_config():
    """Format de renommage personnalisable des volumes et du dossier de série,
    consommé par rename_handler.py au lieu du format standard fixe d'origine."""
    if request.method == 'GET':
        config = load_rename_config()
        config['defaults'] = DEFAULT_RENAME_CONFIG
        return jsonify(config)

    try:
        new_config = request.get_json() or {}
        config = load_rename_config()

        volume_template = (new_config.get('volume_template') or '').strip()
        oneshot_template = (new_config.get('oneshot_template') or '').strip()
        series_template = (new_config.get('series_template') or '').strip()
        config['volume_template'] = volume_template or DEFAULT_RENAME_CONFIG['volume_template']
        config['oneshot_template'] = oneshot_template or DEFAULT_RENAME_CONFIG['oneshot_template']
        config['series_template'] = series_template or DEFAULT_RENAME_CONFIG['series_template']

        if save_rename_config(config):
            return jsonify({'success': True, **config})
        return jsonify({'success': False, 'error': 'Erreur de sauvegarde'}), 500

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


BACKUP_MANIFEST_NAME = 'backup_manifest.json'
MAX_BACKUP_FILE_SIZE = 2 * 1024 * 1024 * 1024
MAX_BACKUP_TOTAL_SIZE = 3 * 1024 * 1024 * 1024
MAX_BACKUP_COMPRESSION_RATIO = 200


def _backup_file_specs():
    """Fichiers inclus dans un backup: (nom dans l'archive, chemin absolu, obligatoire).
    Uniquement les bases de données et fichiers de configuration - jamais les fichiers de
    la bibliothèque elle-même (trop volumineux, hors du périmètre d'un backup de config)
    ni les couvertures en cache (régénérées au prochain scan). La clé de chiffrement est
    incluse: sans elle, les secrets (mots de passe/API keys) dans les fichiers de config
    restaurés seraient illisibles (voir encryption.py, Fernet)."""
    cfg = current_app.config
    return [
        ('bullarr.db', cfg['DATABASE'], True),
        ('ebdz.db', cfg['DB_FILE'], False),
        ('.encryption_key', os.path.join(cfg['DATA_DIR'], '.encryption_key'), False),
        ('emule_config.json', cfg['CONFIG_FILE'], False),
        ('ebdz_config.json', cfg['EBDZ_CONFIG_FILE'], False),
        ('prowlarr_config.json', cfg['PROWLARR_CONFIG_FILE'], False),
        ('komga_config.json', cfg['KOMGA_CONFIG_FILE'], False),
        ('oidc_config.json', cfg['OIDC_CONFIG_FILE'], False),
        ('missing_monitor_config.json', cfg['MISSING_MONITOR_CONFIG_FILE'], False),
        ('library_import_config.json', cfg['LIBRARY_IMPORT_CONFIG_FILE'], False),
        ('rename_config.json', cfg['RENAME_CONFIG_FILE'], False),
        ('qbittorrent_config.json', cfg['QBITTORRENT_CONFIG_FILE'], False),
    ]


def _validate_staged_backup_file(path, name):
    """Reject a structurally invalid backup before any live file is replaced."""
    if name.endswith('.db'):
        conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        try:
            row = conn.execute('PRAGMA quick_check').fetchone()
            if not row or row[0] != 'ok':
                raise ValueError(f'Base SQLite invalide: {name}')
        finally:
            conn.close()
    elif name.endswith('.json'):
        with open(path, 'r', encoding='utf-8') as config_file:
            parsed = json.load(config_file)
        if not isinstance(parsed, dict):
            raise ValueError(f'Configuration JSON invalide: {name}')
    elif name == '.encryption_key':
        from cryptography.fernet import Fernet
        with open(path, 'rb') as key_file:
            key = key_file.read()
        try:
            Fernet(key)
        except (TypeError, ValueError) as exc:
            raise ValueError("Clé de chiffrement invalide") from exc


@settings_bp.route('/api/settings/backup', methods=['GET'])
def download_backup():
    """Télécharge un .zip avec les bases de données + tous les fichiers de config (voir
    _backup_file_specs) - de quoi restaurer l'installation ailleurs ou après un incident.
    Construit en mémoire (BytesIO), rien n'est écrit sur disque côté serveur."""
    buf = io.BytesIO()
    included = []
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, path, required in _backup_file_specs():
            if path and os.path.exists(path):
                zf.write(path, name)
                included.append(name)
            elif required:
                return jsonify({'success': False, 'error': f'Fichier requis introuvable: {path}'}), 500
        zf.writestr(BACKUP_MANIFEST_NAME, json.dumps({
            'created_at': datetime.now().isoformat(),
            'files': included,
        }, indent=2))

    buf.seek(0)
    filename = f"bullarr_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    return send_file(buf, mimetype='application/zip', as_attachment=True, download_name=filename)


@settings_bp.route('/api/settings/backup/restore', methods=['POST'])
def restore_backup():
    """Restaure un backup .zip précédemment téléchargé (voir download_backup):
    écrit ses fichiers dans data/, en gardant une copie de sécurité de chaque fichier
    remplacé (suffixe .before_restore) au cas où l'archive importée serait invalide.
    N'écrit QUE les fichiers reconnus (noms fixes de _backup_file_specs, jamais les
    chemins tels que fournis dans le zip) - pas de risque de zip-slip, et un zip
    contenant autre chose est ignoré pour ces entrées.

    Ne redémarre PAS l'app (connexions DB/scheduler déjà en cours, clé de chiffrement
    déjà chargée en mémoire ailleurs dans le code) - l'utilisateur doit redémarrer le
    conteneur pour repartir proprement avec les fichiers restaurés."""
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'Aucun fichier fourni'}), 400

    uploaded = request.files['file']
    if not uploaded.filename:
        return jsonify({'success': False, 'error': 'Aucun fichier sélectionné'}), 400

    try:
        zf = zipfile.ZipFile(uploaded.stream)
    except zipfile.BadZipFile:
        return jsonify({'success': False, 'error': "Le fichier n'est pas une archive .zip valide"}), 400

    specs = _backup_file_specs()
    allowed_names = {name for name, _, _ in specs} | {BACKUP_MANIFEST_NAME}
    infos = [info for info in zf.infolist() if info.filename in allowed_names]
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        zf.close()
        return jsonify({'success': False, 'error': "L'archive contient des entrées dupliquées"}), 400

    total_size = sum(info.file_size for info in infos)
    unsafe_size = total_size > MAX_BACKUP_TOTAL_SIZE or any(
        info.file_size > MAX_BACKUP_FILE_SIZE for info in infos
    )
    unsafe_ratio = any(
        info.file_size > 1024 * 1024
        and info.file_size / max(info.compress_size, 1) > MAX_BACKUP_COMPRESSION_RATIO
        for info in infos
    )
    if unsafe_size or unsafe_ratio:
        zf.close()
        return jsonify({'success': False, 'error': "Archive trop volumineuse ou taux de compression suspect"}), 400

    names_in_zip = set(names)
    if not any(name in names_in_zip for name, _, required in specs if required):
        zf.close()
        return jsonify({'success': False, 'error': "L'archive ne contient pas de base de données reconnue - restauration annulée"}), 400

    restored = []
    data_dir = current_app.config['DATA_DIR']
    os.makedirs(data_dir, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix='.restore_', dir=data_dir) as staging_dir:
            staged = []
            for name, path, required in specs:
                if name not in names_in_zip:
                    continue
                staged_path = os.path.join(staging_dir, name)
                with zf.open(name) as src, open(staged_path, 'wb') as dst:
                    shutil.copyfileobj(src, dst)
                _validate_staged_backup_file(staged_path, name)
                staged.append((name, path, staged_path))

            for name, path, staged_path in staged:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                if os.path.exists(path):
                    shutil.copy2(path, path + '.before_restore')
                os.replace(staged_path, path)
                os.chmod(path, 0o600)
                restored.append(name)
    except (OSError, ValueError, json.JSONDecodeError, sqlite3.DatabaseError) as exc:
        return jsonify({'success': False, 'error': f'Backup invalide: {exc}'}), 400
    finally:
        zf.close()

    return jsonify({
        'success': True,
        'restored': restored,
        'message': "Backup restauré. Redémarre le conteneur (docker compose restart) pour que les changements prennent effet."
    })


def _check_volume_file_validity(filepath, fmt):
    """Vérifie qu'un fichier tome n'est pas corrompu/tronqué ("add in verification
    checking the validity of files. like Foudroyants ... is not valid file") - un test
    d'intégrité rapide (CRC des membres pour cbz/zip, testrar pour cbr/rar, simple
    ouverture pour pdf) plutôt qu'une lecture complète des pages: suffisant pour
    détecter un téléchargement tronqué (voir l'incident Capitaine Crown, deux
    téléchargements Telegram tronqués en silence côté taille de fichier) sans alourdir
    excessivement un scan qui porte déjà sur toute la bibliothèque à chaque lancement.
    Retourne None si le fichier est valide, sinon un message d'erreur explicite."""
    if not filepath or not os.path.exists(filepath):
        return 'Fichier introuvable sur le disque'

    if os.path.getsize(filepath) == 0:
        return 'Fichier vide (0 octet) - téléchargement tronqué'

    from archive_utils import detect_actual_format
    fmt = detect_actual_format(filepath, fmt)
    try:
        if fmt in ('cbz', 'zip'):
            with zipfile.ZipFile(filepath, 'r') as zf:
                bad_file = zf.testzip()
                if bad_file:
                    return f"Archive corrompue (membre invalide: {bad_file})"
        elif fmt in ('cbr', 'rar'):
            import rarfile
            with rarfile.RarFile(filepath) as rf:
                bad_file = rf.testrar()
                if bad_file:
                    return f"Archive corrompue (membre invalide: {bad_file})"
        elif fmt == 'pdf':
            from pypdf import PdfReader
            with open(filepath, 'rb') as f:
                pdf = PdfReader(f)
                if len(pdf.pages) == 0:
                    return 'PDF sans pages'
    except Exception as e:
        return f"Fichier corrompu ou illisible ({e})"

    return None


def _load_verification_series_and_volumes():
    """Fetch commun aux 4 catégories de /verification (voir VERIFICATION_TYPES plus bas) -
    une seule requête rapide (pas d'I/O disque, pas de test d'intégrité), partagée pour
    ne pas répéter ces deux SELECT à chaque clic sur une catégorie différente."""
    conn = sqlite3.connect(current_app.config['DATABASE'], timeout=30.0)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute('''
        SELECT s.id, s.library_id, s.title, s.path, s.is_oneshot, s.bedetheque_url, s.komga_series_id,
               s.ebdz_thread_id, s.ebdz_matched_title, s.ebdz_match_status, s.ebdz_volumes_count,
               u.name AS universe_name
        FROM series s
        LEFT JOIN universes u ON u.id = s.universe_id
        ORDER BY s.title COLLATE NOCASE
    ''')
    series_rows = cursor.fetchall()

    cursor.execute('''
        SELECT id, series_id, filename, filepath, volume_number, is_integral, integral_number,
               is_hs, hs_number, year, comicinfo, format, komga_book_id,
               validated_size, validation_valid, validation_error, resolution, release_group
        FROM volumes
        ORDER BY series_id, volume_number
    ''')
    volume_rows = cursor.fetchall()
    conn.close()

    volumes_by_series = {}
    for v in volume_rows:
        volumes_by_series.setdefault(v['series_id'], []).append(v)

    return series_rows, volume_rows, volumes_by_series


def _verify_missing_metadata(series_rows, volumes_by_series):
    """Séries non matchées Bédéthèque + tomes sans ComicInfo.xml (ou résumé manquant)."""
    from blueprints.bedetheque.comicinfo_writer import WRITABLE_FORMATS

    missing_metadata = []
    for s in series_rows:
        if not s['bedetheque_url']:
            missing_metadata.append({
                'type': 'series', 'series_id': s['id'], 'series_title': s['title'],
                'volume_id': None, 'filename': None,
                'reason': 'Série non matchée sur Bédéthèque',
            })

        for v in volumes_by_series.get(s['id'], []):
            comicinfo = json.loads(v['comicinfo']) if v['comicinfo'] else {}
            fmt = (v['format'] or '').lower()

            if not comicinfo:
                if fmt in WRITABLE_FORMATS:
                    missing_metadata.append({
                        'type': 'volume', 'series_id': s['id'], 'series_title': s['title'],
                        'volume_id': v['id'], 'filename': v['filename'],
                        'reason': 'Pas de ComicInfo.xml (métadonnées Bédéthèque jamais écrites)',
                    })
                else:
                    missing_metadata.append({
                        'type': 'volume', 'series_id': s['id'], 'series_title': s['title'],
                        'volume_id': v['id'], 'filename': v['filename'],
                        'reason': f"Format {fmt or '?'} non réinscriptible"
                                  + (" - conversion cbz requise avant MAJ métadonnées" if fmt == 'cbr' else ''),
                    })
            else:
                # Un ComicInfo partiellement rempli doit rester visible dans cette
                # catégorie: la présence du fichier XML seule ne garantit pas que les
                # métadonnées Bédéthèque ont été écrites.  On vérifie les champs que
                # build_comicinfo_fields peut renseigner (Colorist et Number restent
                # optionnels: certains albums n'ont pas de coloriste ou sont one-shot).
                required_fields = (
                    ('title', 'titre'),
                    ('summary', 'résumé'),
                    ('writer', 'scénariste'),
                    ('penciller', 'dessinateur'),
                    ('publisher', 'éditeur'),
                    ('genre', 'genre'),
                    ('web', 'lien Bédéthèque'),
                    ('year', 'année'),
                )
                empty_fields = [
                    label for key, label in required_fields
                    if not str(comicinfo.get(key) or '').strip()
                ]
                if empty_fields:
                    missing_metadata.append({
                        'type': 'volume', 'series_id': s['id'], 'series_title': s['title'],
                        'volume_id': v['id'], 'filename': v['filename'],
                        'reason': 'Champs ComicInfo manquants : ' + ', '.join(empty_fields),
                    })

    return missing_metadata


def _verify_misnamed(series_rows, volumes_by_series):
    """Fichiers/dossiers dont le nom ne correspond pas au format configuré (voir
    rename_handler.FileRenamer)."""
    from rename_handler import FileRenamer, render_series_folder_name

    rename_cfg = load_rename_config()
    misnamed = []

    for s in series_rows:
        svols = volumes_by_series.get(s['id'], [])
        vol_dicts = []
        for v in svols:
            if not v['filename']:
                continue
            comicinfo = json.loads(v['comicinfo']) if v['comicinfo'] else {}
            vol_dicts.append({
                'id': v['id'], 'filename': v['filename'], 'volume_number': v['volume_number'],
                'is_integral': bool(v['is_integral']), 'integral_number': v['integral_number'],
                'is_hs': bool(v['is_hs']), 'hs_number': v['hs_number'], 'year': v['year'],
                'comicinfo': comicinfo,
                # Le diagnostic doit utiliser les mêmes tags que l'action réelle de
                # renommage, sinon il masque les qualités/releaseurs présents en base.
                'resolution': v['resolution'], 'release_group': v['release_group'],
            })

        if vol_dicts:
            try:
                plan = FileRenamer.build_rename_plan(
                    s['title'], vol_dicts, bool(s['is_oneshot']), rename_cfg['volume_template'],
                    s['universe_name'], rename_cfg['oneshot_template']
                )
                for item in plan:
                    if item['changed']:
                        misnamed.append({
                            'is_folder': False, 'series_id': s['id'], 'series_title': s['title'],
                            'volume_id': item['volume_id'],
                            'current_name': item['old_name'], 'expected_name': item['new_name'],
                        })
            except Exception:
                pass

        if s['path']:
            # render_series_folder_name peut rendre un chemin à plusieurs segments quand
            # le format utilise <univers> (ex: "Nordheim/Dans Les Forêts De Bambous", voir
            # _rename_series_folder côté library/routes.py qui gère réellement ce niveau
            # de dossier supplémentaire) - seul le DERNIER segment (le nom du dossier de
            # la série elle-même) est comparé ici : ce diagnostic ne vérifie donc que le
            # nom du dossier, pas son EMPLACEMENT sous le bon dossier d'univers.
            expected_folder = render_series_folder_name(s['title'], rename_cfg['series_template'], s['universe_name'])
            expected_folder = expected_folder.rsplit('/', 1)[-1] if expected_folder else expected_folder
            current_folder = os.path.basename(s['path'].rstrip('/'))
            if expected_folder and current_folder != expected_folder:
                misnamed.append({
                    'is_folder': True, 'series_id': s['id'], 'series_title': s['title'],
                    'volume_id': None,
                    'current_name': current_folder, 'expected_name': expected_folder,
                })

    return misnamed


def _verify_invalid_files(series_rows, volume_rows):
    ""
    try:
        from blueprints.komga.client import KomgaClient
        komga_client = KomgaClient()
    except Exception:
        return [], False

    try:
        komga_media = komga_client.list_book_media_status()
    except Exception:
        return [], True

    series_title_by_id = {s['id']: s['title'] for s in series_rows}
    volumes_with_komga_id = {v['id']: v for v in volume_rows if v['komga_book_id']}

    invalid_files = []
    for volume_id, v in volumes_with_komga_id.items():
        info = komga_media.get(v['komga_book_id'])
        if not info:
            continue
        if info['deleted']:
            reason = "Komga: fichier introuvable lors de son dernier scan"
        elif info['status'] and info['status'] != 'READY':
            reason = f"Komga: analyse média en échec ({info['comment'] or info['status']})"
        elif info['pages_count'] == 0:
            reason = "Komga: aucune page valide détectée dans l'archive"
        else:
            continue
        invalid_files.append({
            'series_id': v['series_id'], 'series_title': series_title_by_id.get(v['series_id'], '?'),
            'volume_id': volume_id, 'filename': v['filename'],
            'reason': reason,
        })

    return invalid_files, True


def _verify_unmatched_owned_volumes(series_rows, volumes_by_series):
    """Tomes RÉELLEMENT possédés (un vrai fichier sur disque) dont le ComicInfo n'a
    aucun lien Bédéthèque (comicinfo.web) - "ca doit etre fichier non detecté de
    bedetheque. ca regarde si le volume est bien matché avec une entrée bedetheque":
    contrairement à "Métadonnées manquantes" (ComicInfo absent ou format non
    réinscriptible), un tome peut très bien avoir un ComicInfo déjà écrit (résumé,
    auteur...) sans jamais avoir été rattaché à UN album Bédéthèque précis - "MAJ
    métadonnées" par tome corrige ça une fois qu'un lien existe pour au moins une
    ressemblance de titre, mais rien ne signalait jusqu'ici qu'un tome n'a jamais eu ce
    lien du tout. Uniquement les séries déjà matchées elles-mêmes (bedetheque_url défini)
    - une série jamais matchée fait déjà remonter TOUS ses tomes via "Métadonnées
    manquantes", pas la peine de les compter deux fois ici."""
    unmatched = []
    for s in series_rows:
        if not s['bedetheque_url']:
            continue
        for v in volumes_by_series.get(s['id'], []):
            if not v['filepath']:
                continue
            try:
                comicinfo = json.loads(v['comicinfo']) if v['comicinfo'] else {}
            except (TypeError, ValueError):
                comicinfo = {}
            if comicinfo.get('web'):
                continue
            unmatched.append({
                'series_id': s['id'], 'series_title': s['title'],
                'volume_id': v['id'], 'filename': v['filename'],
            })
    unmatched.sort(key=lambda x: x['series_title'].casefold())
    return unmatched


@settings_bp.route('/api/settings/verification', methods=['GET'])
def run_verification():
    """Scanne toute la bibliothèque pour repérer les tomes/séries à métadonnées
    manquantes et les fichiers/dossiers dont le nom ne correspond pas au format standard
    configuré (voir rename_handler.FileRenamer) - diagnostic en LECTURE SEULE, ne modifie
    rien. Chaque élément listé renvoie de quoi le corriger via les actions déjà
    existantes (MAJ métadonnées, renommer) plutôt que d'introduire un nouveau mécanisme
    de correction (voir décision produit: rapport + boutons existants, pas de correction
    de masse intégrée pour ce premier passage).

    "au lieu d'avoir toutes les verifications lancés en meme temps. groupe par different
    types de verification et on peut cliquer dans chacune d'une" - ?type=<...> permet de
    ne (re)calculer qu'UNE des 4 catégories (voir _verify_* ci-dessus) plutôt que les 4 à
    chaque appel, notamment invalid_files, la plus lente (I/O disque + décompression) - un
    clic sur "métadonnées manquantes" n'a plus à l'attendre. Sans ?type (compatibilité
    d'éventuels autres appelants), les 4 sont calculées et renvoyées comme avant."""
    verif_type = request.args.get('type')
    valid_types = {'missing_metadata', 'misnamed', 'invalid_files', 'unmatched_owned_volumes'}
    if verif_type is not None and verif_type not in valid_types:
        return jsonify({'error': f"type invalide, attendu l'un de {sorted(valid_types)}"}), 400

    series_rows, volume_rows, volumes_by_series = _load_verification_series_and_volumes()

    result = {
        'success': True,
        'series_count': len(series_rows),
        'volume_count': len(volume_rows),
    }

    if verif_type in (None, 'missing_metadata'):
        result['missing_metadata'] = _verify_missing_metadata(series_rows, volumes_by_series)
    if verif_type in (None, 'misnamed'):
        result['misnamed'] = _verify_misnamed(series_rows, volumes_by_series)
    if verif_type in (None, 'invalid_files'):
        invalid_files, komga_configured = _verify_invalid_files(series_rows, volume_rows)
        result['invalid_files'] = invalid_files
        result['komga_configured'] = komga_configured
    if verif_type in (None, 'unmatched_owned_volumes'):
        result['unmatched_owned_volumes'] = _verify_unmatched_owned_volumes(series_rows, volumes_by_series)

    return jsonify(result)
