"""
Routes pour la gestion des bibliothèques
"""
from flask import render_template, request, jsonify, current_app, redirect, url_for
from . import library_bp
from .scanner import LibraryScanner, SeriesDirectoryMissingError, COMICINFO_FIELDS, scan_import_lock
from blueprints.bedetheque.cbr_converter import convert_cbr_to_cbz, CbrConversionError
from blueprints.bedetheque.pdf_converter import convert_pdf_to_cbz, PdfConversionError
from .zip_converter import convert_zip_to_cbz, ZipConversionError, IMAGE_EXTENSIONS as _ZIP_IMAGE_EXTENSIONS
from blueprints.bedetheque.comicinfo_writer import write_comicinfo_cbz, build_comicinfo_fields, WRITABLE_FORMATS, derive_author_year_from_comicinfo
from blueprints.bedetheque.scraper import match_bedetheque_volume
import sqlite3
import json
import os
import re
import threading
import time
import shutil
import unicodedata
import zipfile
import rarfile
from collections import Counter
from .import_worker import (
    ImportWorkerError, ImportWorkerTimeout, prepare_import_file,
    transfer_import_file, cleanup_stale_staging,
)


# Potentially blocking filesystem work runs in a separate OS process. A Python
# thread timeout cannot interrupt a hard NFS read, and ThreadPoolExecutor.shutdown()
# waits for the blocked thread. The process boundary lets the scheduler release its
# per-file lock at the deadline even if the killed child remains briefly in kernel I/O.
IMPORT_STEP_TIMEOUT_SECONDS = 300


def _import_staging_directory():
    path = current_app.config.get(
        'IMPORT_STAGING_DIRECTORY',
        os.path.join(os.path.dirname(current_app.config['DATABASE']), 'import-staging')
    )
    os.makedirs(path, exist_ok=True)
    return path



_import_execution_lock = scan_import_lock

# "faire un check rapide de vérification d'intégrité du fichier avant de pouvoir
# l'importer" - le test réel (_check_volume_file_validity, CRC zip/testrar/ouverture pdf)
# est fait dès le SCAN (/api/import/scan, voir _append_scanned_file), pas seulement à
# l'exécution de l'import comme le garde-fou déjà en place dans _execute_import_batch -
# "35 fichiers pret a importer" doit refléter la réalité du fichier, pas juste sa
# présence sur disque. Un scan porte sur les mêmes fichiers en boucle (Actualiser, ou le
# rattrapage automatique de loadActiveDownloads) tant qu'ils n'ont pas été importés/
# supprimés - re-tester le CRC d'un gros fichier à CHAQUE scan serait coûteux pour rien
# tant qu'il n'a pas changé sur disque. Cache mémoire (filepath, taille) -> résultat,
# même principe size-keyed que le cache de /verification (CLAUDE.md) mais pour des
# fichiers qui ne sont pas encore dans la table volumes - pas de colonne à réutiliser, un
# simple dict process suffit (ces fichiers sont transitoires: importés ou supprimés en
# quelques scans, jamais besoin de survivre à un redémarrage).
_import_file_validity_cache = {}

# "l'import automatique du fichier etait en cours donc il ne devrait pas y avoir de
# fichier corrompu" - un fichier encore activement écrit par le client de téléchargement
# n'a, PAR DÉFINITION, pas encore une archive complète à cet instant précis : le tester
# EN PLEIN TÉLÉCHARGEMENT renvoie une "corruption" qui n'est en réalité qu'un transfert pas
# terminé, affichée à tort comme si le fichier ne serait JAMAIS valide. Même philosophie de
# stabilité que self._file_size_history (scheduler.py, "toujours besoin d'au moins DEUX
# passages consécutifs à taille inchangée avant tout import") mais pour CE test-ci
# spécifiquement, qui ne la partageait pas jusqu'ici (scan_import_directory/_append_
# scanned_file, exécuté par /api/import/scan, est un chemin séparé du scheduler
# périodique). Retient la taille vue au scan PRÉCÉDENT par chemin - le vrai test CRC/testrar
# n'est lancé qu'une fois la taille identique à deux scans de suite, sinon le fichier reste
# simplement "pas encore vérifié" (validation_error=None) plutôt que faussement corrompu.
_import_file_size_history = {}


def _check_import_file_validity_cached(filepath, fmt):
    try:
        size = os.path.getsize(filepath)
    except OSError:
        return None
    previous_size = _import_file_size_history.get(filepath)
    _import_file_size_history[filepath] = size
    if previous_size != size:
        return None
    cache_key = (filepath, size)
    if cache_key in _import_file_validity_cache:
        return _import_file_validity_cache[cache_key]
    from blueprints.settings.routes import _check_volume_file_validity
    error = _check_volume_file_validity(filepath, fmt)
    _import_file_validity_cache[cache_key] = error
    return error

_conversion_lock = threading.Lock()


def get_db_connection():
    """Retourne une connexion à la base de données"""
    conn = sqlite3.connect(current_app.config['DATABASE'], timeout=120.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _notify_import_completed(imported_count, replaced_count, source, logs_to_record=None):
    """Notification Telegram best-effort de fin d'import ("notification pour import
    effectué") - appelée depuis execute_import (manuel) et execute_auto_import
    (planificateur). N'envoie rien si imported_count est 0 (import déclenché mais qui n'a
    au final rien importé, ex: tous les fichiers en échec/doublons), si l'utilisateur a
    décoché ce type de notification dans Settings > Telegram (notify_import_completed),
    ni si Telegram n'est pas configuré/activé (send_telegram_notification gère ce dernier
    cas silencieusement). Ne doit jamais faire échouer l'import appelant : toute erreur
    ici est avalée.

    logs_to_record: la même liste de dicts (filename/series_title/action/volume_id) déjà
    construite par l'appelant pour import_history - réutilisée ici pour afficher, par
    tome, son nom RÉEL une fois renommé (_rename_new_volumes_after_import tourne avant cet
    appel, voir son commentaire) et un lien direct vers sa fiche Bédéthèque plutôt qu'un
    simple compte de fichiers ("BD Importé: Name of the volume (the rename version), Lien
    to bedetheque")."""
    if imported_count <= 0:
        return
    try:
        from blueprints.telegram.routes import load_telegram_config, send_telegram_notification
        if not load_telegram_config().get('notify_import_completed', True):
            return
        relevant_entries = [
            entry for entry in (logs_to_record or [])
            if entry.get('action') in ('imported', 'replaced')
        ]

        conn = get_db_connection()
        bedetheque_url_by_series = {}
        komga_url_by_series = {}
        blocks = []
        for entry in relevant_entries:
            # volume_id: posé juste après l'INSERT/UPDATE de la ligne volumes (voir
            # _execute_import_batch) - filename y est déjà celui APRÈS renommage
            # automatique, pas le nom brut du fichier déplacé (souvent illisible, ex:
            # "...@9-art-BD.cbr").
            display_name = None
            volume_id = entry.get('volume_id')
            if volume_id:
                row = conn.execute('SELECT filename FROM volumes WHERE id = ?', (volume_id,)).fetchone()
                if row and row['filename']:
                    display_name = os.path.splitext(row['filename'])[0]
            if not display_name:
                display_name = f"{entry.get('series_title', '')} - {entry.get('filename', '')}".strip(' -')

            series_id = entry.get('series_id')
            bedetheque_url = ''
            komga_url = ''
            if series_id:
                if series_id not in bedetheque_url_by_series:
                    srow = conn.execute(
                        'SELECT bedetheque_url, komga_url FROM series WHERE id = ?', (series_id,)
                    ).fetchone()
                    bedetheque_url_by_series[series_id] = (srow['bedetheque_url'] if srow else None) or ''
                    komga_url_by_series[series_id] = (srow['komga_url'] if srow else None) or ''
                bedetheque_url = bedetheque_url_by_series[series_id]
                komga_url = komga_url_by_series[series_id]

            links = '\n'.join(url for url in (bedetheque_url, komga_url) if url)
            blocks.append(f"{display_name}\n{links}" if links else display_name)
        conn.close()

        message = "BD Importé :"
        if blocks:
            sample = blocks[:10]
            message += "\n\n" + "\n\n".join(sample)
            if len(blocks) > len(sample):
                message += f"\n\n… et {len(blocks) - len(sample)} autre(s)"

        photo_path = None
        series_ids = {entry['series_id'] for entry in relevant_entries if entry.get('series_id')}
        if len(series_ids) == 1:
            conn = get_db_connection()
            row = conn.execute(
                'SELECT local_cover_path, bedetheque_cover_path, komga_cover_path FROM series WHERE id = ?',
                (next(iter(series_ids)),)
            ).fetchone()
            conn.close()
            # Même ordre de priorité que le reste de l'app (voir library.js,
            # pickPosterCoverPath): une vignette locale extraite du fichier prime sur
            # celle de Bédéthèque, elle-même préférée à celle de Komga.
            cover_path = (row and (row['local_cover_path'] or row['bedetheque_cover_path'] or row['komga_cover_path'])) or None
            if cover_path:
                photo_path = os.path.join(current_app.config['DATA_DIR'], cover_path)

        success, error = send_telegram_notification(message, photo_path=photo_path)
        if not success:
            print(f"Erreur notification Telegram (import terminé): {error}")
    except Exception as e:
        print(f"Erreur notification Telegram (import terminé): {e}")


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
    if os.path.commonpath([path_real, root_real]) != root_real:
        raise UnsafePathError(f"Chemin en dehors du répertoire autorisé: {path}")
    return path_real


def sanitize_path_component(name, label='nom'):
    """Valide qu'une chaîne fournie par le client et destinée à devenir un unique
    composant de chemin (nom de série, nom de fichier...) ne contient ni séparateur
    ni séquence d'évasion ('..'), pour empêcher un nom comme '../../etc' de sortir
    du répertoire dans lequel il est censé être joint. Lève UnsafePathError sinon."""
    if not name or name in ('.', '..') or os.sep in name or (os.altsep and os.altsep in name):
        raise UnsafePathError(f"{label} invalide: {name!r}")
    return name


# Ordre de préférence des formats en cas de doublon (le plus petit numéro gagne)
FORMAT_PRIORITY = {'cbz': 0, 'zip': 0, 'cbr': 1, 'rar': 1, 'pdf': 2}

# "why were [file] replaced? the file was not much bigger so no need to replace" (cas
# réel: conversion pdf->cbz dont le .cbz obtenu ne dépassait que de peu le fichier déjà
# possédé) - is_better_volume comparait juste new_size > existing_size, sans marge : le
# moindre octet supplémentaire suffisait à déclencher un remplacement (suppression de
# l'ancien fichier) pour un gain invisible. 5% choisi pour rester cohérent avec l'esprit
# du correctif ci-dessous (comparer par taille, pas par format) sans pour autant relâcher
# la comparaison au point de rater un vrai meilleur scan.
MIN_REPLACE_SIZE_MARGIN = 0.05


def get_format_priority(fmt):
    """Retourne le rang de préférence d'un format (plus petit = préféré)"""
    return FORMAT_PRIORITY.get((fmt or '').lower(), 99)


def is_better_volume(new_size, existing_size):
    """Détermine si un nouveau fichier doit remplacer un fichier existant du même volume -
    uniquement par taille, plus par format ("HS1a etait plus gros que le HS1. tu devrais
    donc l'importer? [...] especially the HS was a pdf before. so the format priority
    does not add value here"): un .cbz déjà possédé est très souvent lui-même issu d'une
    conversion depuis un .pdf/.cbr (voir convert_pdf_to_cbz/convert_cbr_to_cbz) - le
    CONTENEUR ne dit donc rien de la qualité du scan d'origine, contrairement à ce que
    l'ancienne priorité de format (cbz > cbr/zip/rar > pdf) laissait supposer. Cas réel:
    un HS1a en .pdf (143 Mo, scan visiblement bien meilleur) supprimé à tort au profit du
    HS1 déjà possédé, lui-même simplement CONVERTI depuis un pdf en .cbz (14 Mo) - les deux
    étaient à l'origine des scans pdf, comparer leur conteneur n'avait jamais de sens.

    "yes marginal size difference would make sense" - un nouveau fichier ne serait alors
    que quelques octets plus gros (ex: réencodage pdf->cbz proche de la taille d'origine)
    déclenchait quand même un remplacement (ancien fichier supprimé) pour un gain nul en
    pratique. Exige désormais une marge minimale (MIN_REPLACE_SIZE_MARGIN) plutôt qu'une
    stricte inégalité."""
    return new_size > existing_size * (1 + MIN_REPLACE_SIZE_MARGIN)


def _find_existing_volume_for_import(cursor, series_id, parsed, single_album=False):
    """Cherche un tome déjà en base (fichier réel OU placeholder Bédéthèque sans fichier,
    filepath NULL - voir add_series_from_bedetheque) correspondant au même tome que
    `parsed`, pour que l'import METTE À JOUR cette ligne au lieu d'en insérer une nouvelle
    en double. Partagé par execute_import (manuel, y compris avec une correction manuelle
    du type de tome - voir volume_override) et execute_auto_import.

    Numérotation propre à chaque type de tome (volume_number / integral_number /
    hs_number ne se substituent jamais l'un à l'autre) - et pour chacun, un numéro NULL
    doit être cherché avec "IS NULL", pas "= ?": une égalité SQL contre NULL est toujours
    fausse, donc "WHERE integral_number = ?" avec un paramètre None ne retrouve JAMAIS un
    placeholder à integral_number NULL, le laissant fantôme et dupliqué à chaque import
    (constaté à la fois sur des one-shots sans numéro ET, séparément, sur une intégrale
    sans numéro - une seule édition intégrale pour toute la série - qui n'était encore
    couverte par aucun des cas déjà corrigés ici).

    Retourne (id, filepath, file_size, format) ou (None, None, 0, None) si rien trouvé.
    """
    if single_album:
        cursor.execute("SELECT id, filepath, file_size, format FROM volumes WHERE series_id = ? ORDER BY (filepath IS NOT NULL), id DESC LIMIT 1", (series_id,))
        existing_volume = cursor.fetchone()

        if existing_volume:
            return existing_volume

    volume_number = parsed.get('volume')
    is_integral = parsed.get('is_integral')
    integral_number = parsed.get('integral_number')
    is_hs = parsed.get('is_hs')
    hs_number = parsed.get('hs_number')
    is_episode = parsed.get('is_episode')
    episode_number = parsed.get('episode_number')

    if is_episode:
        if episode_number is not None:
            query = 'SELECT id, filepath, file_size, format FROM volumes WHERE series_id = ? AND is_episode = 1 AND episode_number = ?'
            params = (series_id, episode_number)
        else:
            query = 'SELECT id, filepath, file_size, format FROM volumes WHERE series_id = ? AND is_episode = 1 AND episode_number IS NULL'
            params = (series_id,)
    elif volume_number is not None:
        query = 'SELECT id, filepath, file_size, format FROM volumes WHERE series_id = ? AND volume_number = ?'
        params = (series_id, volume_number)
    elif is_integral:
        if integral_number is not None:
            query = 'SELECT id, filepath, file_size, format FROM volumes WHERE series_id = ? AND is_integral = 1 AND integral_number = ?'
            params = (series_id, integral_number)
        else:
            query = 'SELECT id, filepath, file_size, format FROM volumes WHERE series_id = ? AND is_integral = 1 AND integral_number IS NULL'
            params = (series_id,)
    elif is_hs:
        if hs_number is not None:
            query = 'SELECT id, filepath, file_size, format FROM volumes WHERE series_id = ? AND is_hs = 1 AND hs_number = ?'
            params = (series_id, hs_number)
        else:
            query = 'SELECT id, filepath, file_size, format FROM volumes WHERE series_id = ? AND is_hs = 1 AND hs_number IS NULL'
            params = (series_id,)
    elif parsed.get('is_special'):
        special_label = parsed.get('special_label')
        if special_label:
            query = 'SELECT id, filepath, file_size, format FROM volumes WHERE series_id = ? AND is_special = 1 AND special_label = ?'
            params = (series_id, special_label)
        else:
            query = None
            params = ()
    else:
        # One-shot (ni numéro de tome, ni intégrale, ni hors-série, ni épisode - un
        # placeholder épisode partage lui aussi volume_number NULL/is_integral=0/is_hs=0,
        # il ne doit pas être pris à tort pour "le" one-shot de la série)
        query = 'SELECT id, filepath, file_size, format FROM volumes WHERE series_id = ? AND volume_number IS NULL AND is_integral = 0 AND is_hs = 0 AND is_episode = 0 AND is_special = 0'
        params = (series_id,)

    if query is None:
        existing_volume = None
    else:
        cursor.execute(query + ' ORDER BY id DESC LIMIT 1', params)
        existing_volume = cursor.fetchone()

    if not existing_volume and not is_episode and volume_number is not None:
        # "Bellatrix - 02 - ...cbr": un fichier dont le NOM ne dit pas "Épisode" (donc
        # parsé comme un tome ordinaire ci-dessus) peut quand même viser le MÊME album
        # qu'un placeholder Bédéthèque déjà connu comme épisode - Bédéthèque numérote un
        # épisode et un tome dans le même champ (voir commentaire plus haut sur le
        # branchement is_episode), mais une release ne reprend pas toujours le mot
        # "Épisode" dans son nom de fichier. Repli sur ce placeholder SEULEMENT s'il est
        # encore vide (filepath IS NULL): un épisode déjà rempli par un vrai fichier ne
        # doit jamais être pris pour "le même tome" sur la seule foi d'un numéro qui
        # coïncide, trop fragile une fois du contenu réel en place des deux côtés.
        cursor.execute('''
            SELECT id, filepath, file_size, format FROM volumes
            WHERE series_id = ? AND is_episode = 1 AND episode_number = ? AND filepath IS NULL
            ORDER BY id DESC LIMIT 1
        ''', (series_id, volume_number))
        existing_volume = cursor.fetchone()

    if existing_volume:
        return existing_volume[0], existing_volume[1], existing_volume[2], existing_volume[3]
    return None, None, 0, None


# ========== ROUTES HTML ==========

@library_bp.route('/')
def index():
    """Page d'accueil - Liste des bibliothèques. Une seule bibliothèque configurée: on y
    va directement plutôt que de forcer un aller-retour par ce sélecteur ("evite de
    repasser par la fenetre /all quand je clique bibliotheque... quand il y a qu'une seule
    bibliotheque ca devrait aller directement a la bibliotheque") - le sélecteur ne sert
    à choisir entre plusieurs bibliothèques, il n'a aucune utilité s'il n'y en a qu'une.

    "appuyer sur + ca fait rien" - bug réel: ce redirect ignorait ?all=1 inconditionnellement,
    alors que templates/library.html a bien un lien "+" (title="Nouvelle bibliothèque") vers
    /?all=1 pour justement échapper à ce raccourci et atteindre le formulaire de création -
    ce lien renvoyait donc silencieusement l'utilisateur sur la même page qu'il venait de
    quitter (redirect -> redirect immédiat), qui n'a elle-même aucun bouton "+" de création.
    static/js/index.js avait déjà ce même garde-fou côté client (`!params.has('all')`) mais
    ne pouvait jamais s'exécuter puisque index.html n'était jamais rendu avant lui dans ce cas."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM libraries')
    libraries = cursor.fetchall()
    conn.close()

    if len(libraries) == 1 and not request.args.get('all'):
        return redirect(url_for('library.library_detail', library_id=libraries[0]['id']))

    return render_template('index.html')


@library_bp.route('/library/<int:library_id>')
def library_detail(library_id):
    """Détails d'une bibliothèque"""
    return render_template('library.html', library_id=library_id)


@library_bp.route('/series/<int:series_id>')
def series_detail_page(series_id):
    """Page dédiée au détail d'une série (remplace l'ancienne vue modale)"""
    return render_template('series-detail.html', series_id=series_id)


@library_bp.route('/import')
def import_page():
    """Page d'import de BD"""
    return render_template('import.html')


@library_bp.route('/missing-monitor')
def missing_monitor_page():
    """Page de surveillance des volumes manquants"""
    return render_template('missing-monitor.html')


@library_bp.route('/validation')
def auto_acquire_review_page():
    """File centrale des décisions nécessitant une intervention manuelle."""
    return render_template('validation.html')


@library_bp.route('/bedetheque-enrich')
def bedetheque_enrich_page():
    """Page dédiée à l'enrichissement Bédéthèque (série unique, par lot, ou bibliothèque
    entière) - restauration de l'ancienne page /nautiljon, adaptée à Bédéthèque. Vit dans
    library_bp (et non bedetheque_bp) car ce dernier est enregistré avec le préfixe
    /api/bedetheque: une route de page ne peut pas en être exemptée individuellement."""
    return render_template('bedetheque-enrich.html')


# ========== API ==========

@library_bp.route('/api/auto-acquire/reviews', methods=['GET'])
def auto_acquire_reviews():
    from blueprints.bedetheque.auto_acquire import get_manual_reviews
    return jsonify(get_manual_reviews())


@library_bp.route('/api/auto-acquire/reviews/count', methods=['GET'])
def auto_acquire_reviews_count():
    from blueprints.bedetheque.auto_acquire import get_manual_reviews
    return jsonify({'count': len(get_manual_reviews())})


@library_bp.route('/api/auto-acquire/reviews/series-match', methods=['POST'])
def queue_series_match_review_route():
    """Record a Nouveautés item whose series resolver could not match it."""
    data = request.get_json(silent=True) or {}
    title = (data.get('series_title') or data.get('title') or '').strip()
    candidates = data.get('candidates') or []
    if not title or not candidates:
        return jsonify({'success': False, 'error': 'Titre et candidat requis'}), 400
    from blueprints.bedetheque.auto_acquire import queue_series_match_review
    queue_series_match_review(title, candidates)
    return jsonify({'success': True})


@library_bp.route('/api/auto-acquire/reviews/<int:review_id>/download', methods=['POST'])
def download_auto_acquire_review_candidate(review_id):
    data = request.get_json(silent=True) or {}
    from blueprints.bedetheque.auto_acquire import download_manual_review_candidate
    success, message = download_manual_review_candidate(review_id, data.get('candidate_index'))
    return jsonify({'success': success, 'message': message}), (200 if success else 400)


@library_bp.route('/api/auto-acquire/reviews/<int:review_id>/resolve', methods=['POST'])
def resolve_auto_acquire_review(review_id):
    from blueprints.bedetheque.auto_acquire import resolve_manual_review
    if not resolve_manual_review(review_id):
        return jsonify({'success': False, 'error': 'Validation introuvable ou déjà traitée'}), 404
    return jsonify({'success': True})


@library_bp.route('/api/auto-acquire/reviews/<int:review_id>/match-series', methods=['POST'])
def match_auto_acquire_review_series(review_id):
    """Rattache une ligne "Série à matcher" de /validation à une fiche Bédéthèque choisie
    manuellement - voir match_manual_review_series."""
    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'success': False, 'error': 'URL Bédéthèque requise'}), 400
    from blueprints.bedetheque.auto_acquire import match_manual_review_series
    success, error, series_id = match_manual_review_series(review_id, url, data.get('library_id'))
    if not success:
        return jsonify({'success': False, 'error': error}), 400
    return jsonify({'success': True, 'series_id': series_id})


@library_bp.route('/api/libraries', methods=['GET', 'POST'])
def libraries():
    """Liste ou crée des bibliothèques"""
    
    if request.method == 'GET':
        # Lister toutes les bibliothèques
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT 
                l.id,
                l.name,
                l.path,
                l.description,
                l.created_at,
                l.last_scanned,
                COUNT(DISTINCT s.id) as series_count,
                COUNT(v.id) as volumes_count
            FROM libraries l
            LEFT JOIN series s ON s.library_id = l.id
            LEFT JOIN volumes v ON v.series_id = s.id
            GROUP BY l.id
            ORDER BY l.name
        ''')
        
        libraries = []
        for row in cursor.fetchall():
            libraries.append({
                'id': row['id'],
                'name': row['name'],
                'path': row['path'],
                'description': row['description'],
                'created_at': row['created_at'],
                'last_scanned': row['last_scanned'],
                'series_count': row['series_count'],
                'volumes_count': row['volumes_count']
            })
        
        conn.close()
        return jsonify(libraries)
    
    else:  # POST
        # Créer une nouvelle bibliothèque
        data = request.get_json()
        name = data.get('name')
        path = data.get('path')
        description = data.get('description', '')
        
        if not name or not path:
            return jsonify({'success': False, 'error': 'Nom et chemin requis'}), 400
        
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO libraries (name, path, description)
                VALUES (?, ?, ?)
            ''', (name, path, description))
            
            library_id = cursor.lastrowid
            conn.commit()
            conn.close()
            
            return jsonify({'success': True, 'id': library_id})
        
        except sqlite3.IntegrityError:
            return jsonify({'success': False, 'error': 'Une bibliothèque avec ce nom existe déjà'}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': 'Erreur interne'}), 500


@library_bp.route('/api/libraries/<int:library_id>/onboard', methods=['POST'])
def start_library_onboard(library_id):
    """"I want to set up adding a new bibliothèque from existing files. it will need to
    match series and checking current comicinfo" - enchaîne scan + matching Bédéthèque
    par titre + résumé ComicInfo pour une bibliothèque qui contient déjà des fichiers
    (voir library/onboarding.py pour le raisonnement complet). Tourne en arrière-plan
    (même contrat que /api/bedetheque/update-metadata/... - démarre puis répond
    immédiatement, la progression se suit via GET .../onboard/status) : un scan +
    plusieurs recherches Bédéthèque séquentielles dépasserait le timeout d'un reverse
    proxy si on attendait la réponse HTTP directement."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM libraries WHERE id = ?', (library_id,))
    if not cursor.fetchone():
        conn.close()
        return jsonify({'success': False, 'error': 'Bibliothèque non trouvée'}), 404
    conn.close()

    from . import onboarding
    started = onboarding.start_library_onboarding(current_app._get_current_object(), library_id)
    if not started:
        return jsonify({'success': False, 'error': 'Un import est déjà en cours pour cette bibliothèque'}), 409
    return jsonify({'success': True, 'started': True})


@library_bp.route('/api/libraries/<int:library_id>/onboard/status', methods=['GET'])
def get_library_onboard_status(library_id):
    """Progression de l'import guidé démarré par POST .../onboard - voir
    library/onboarding.py. running:false + phase:'done'/'error' signale la fin (succès ou
    échec, voir error). null tant qu'aucun import n'a jamais été lancé pour cette
    bibliothèque dans ce process."""
    from . import onboarding
    status = onboarding.get_onboard_status(library_id)
    if status is None:
        return jsonify({'success': True, 'status': None})
    return jsonify({'success': True, 'status': status})


@library_bp.route('/api/libraries/<int:library_id>', methods=['GET', 'PUT', 'DELETE'])
def library_operations(library_id):
    """Récupère, renomme ou supprime une bibliothèque"""

    if request.method == 'PUT':
        # Renomme la bibliothèque (le chemin sur le disque n'est pas modifié, seulement
        # le nom affiché - le déplacer nécessiterait de bouger tous ses fichiers)
        data = request.get_json(silent=True) or {}
        name = (data.get('name') or '').strip()

        if not name:
            return jsonify({'success': False, 'error': 'Le nom est requis'}), 400

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM libraries WHERE id = ?', (library_id,))
        if not cursor.fetchone():
            conn.close()
            return jsonify({'success': False, 'error': 'Bibliothèque non trouvée'}), 404

        cursor.execute('UPDATE libraries SET name = ? WHERE id = ?', (name, library_id))
        conn.commit()
        conn.close()

        return jsonify({'success': True, 'name': name})

    if request.method == 'GET':
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT * FROM libraries WHERE id = ?', (library_id,))
        library = cursor.fetchone()
        conn.close()

        if not library:
            return jsonify({'error': 'Bibliothèque non trouvée'}), 404

        return jsonify({
            'id': library['id'],
            'name': library['name'],
            'path': library['path'],
            'description': library['description']
        })
    
    else:  # DELETE
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute('DELETE FROM libraries WHERE id = ?', (library_id,))
            if cursor.rowcount == 0:
                conn.close()
                return jsonify({'success': False, 'error': 'Bibliothèque non trouvée'}), 404
            
            conn.commit()
            conn.close()
            
            return jsonify({'success': True})
        
        except Exception as e:
            return jsonify({'success': False, 'error': 'Erreur interne'}), 500


def _scan_library_and_sync(library_id, library_path, force_metadata_refresh=False):
    """Cœur de scan_library (scan + synchronisation Komga/EBDZ) - factorisé pour être
    appelé aussi par run_library_onboarding (library/onboarding.py: "add a new
    bibliothèque from existing files"), qui a besoin exactement du même comportement
    qu'un clic manuel sur "Scanner" plutôt qu'un scan appauvri qui sauterait la
    synchronisation Komga/EBDZ. Retourne series_count, lève sur erreur (chemin
    introuvable...) - au lieu de renvoyer une réponse Flask, pour rester appelable hors
    requête HTTP."""
    scanner = LibraryScanner()
    series_count = scanner.scan_directory(
        library_id, library_path, auto_enrich=False, force_metadata_refresh=force_metadata_refresh
    )

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
        conn = get_db_connection()
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
                    _sync_komga_books(series_row['id'], series_row['komga_series_id'], client)
            except KomgaError:
                pass

    # Tentative de matching Komga automatique pour les séries qui viennent d'être
    # créées par ce scan (pas les séries existantes: celles-ci restent à matcher
    # manuellement). Best-effort: n'échoue pas le scan si Komga est indisponible/non
    # configuré, ou si le titre ne désigne pas un résultat unique/exact.
    if new_series_ids:
        conn = get_db_connection()
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
                        _try_komga_title_match(series_row['id'], series_row['title'], client)
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
        _bulk_ebdz_autodetect(library_id)
    except Exception:
        pass

    return series_count


@library_bp.route('/api/scan/<int:library_id>', methods=['GET', 'POST'])
def scan_library(library_id):
    """Scanne une bibliothèque (détecte séries et volumes, sans enrichissement)"""

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT name, path FROM libraries WHERE id = ?', (library_id,))
        result = cursor.fetchone()
        conn.close()

        if not result:
            return jsonify({'success': False, 'error': 'Bibliothèque non trouvée'}), 404

        library_name = result['name']
        library_path = result['path']

        # Vérifier que le chemin est accessible avant de scanner
        if not os.path.exists(library_path):
            return jsonify({
                'success': False,
                'error': f'Le dossier de la bibliothèque "{library_name}" n\'existe pas ou n\'est pas accessible.\nChemin: {library_path}'
            }), 400

        if not os.path.isdir(library_path):
            return jsonify({
                'success': False,
                'error': f'Le chemin n\'est pas un répertoire: {library_path}'
            }), 400

        # force=1 déclenche un scan complet (ré-extrait page_count/ComicInfo.xml/
        # couverture de tous les fichiers, même inchangés); par défaut le scan est
        # rapide et ne retraite que les fichiers nouveaux/modifiés
        force_metadata_refresh = request.args.get('force', '').lower() in ('1', 'true')

        series_count = _scan_library_and_sync(library_id, library_path, force_metadata_refresh=force_metadata_refresh)

        return jsonify({'success': True, 'series_count': series_count})

    except Exception as e:
        error_msg = 'Erreur interne'
        print(f"❌ Erreur lors du scan de la bibliothèque {library_id}: {error_msg}")
        return jsonify({
            'success': False,
            'error': f'Erreur lors du scan: {error_msg}'
        }), 500


@library_bp.route('/api/scan/series/<int:series_id>', methods=['POST'])
def scan_series(series_id):
    """Scanne une seule série (met à jour ses volumes)"""

    try:
        force_metadata_refresh = request.args.get('force', '').lower() in ('1', 'true')

        check_conn = get_db_connection()
        check_row = check_conn.execute('SELECT id FROM series WHERE id = ?', (series_id,)).fetchone()
        check_conn.close()
        if not check_row:
            return jsonify({'success': False, 'error': 'Série non trouvée'}), 404

        scanner = LibraryScanner()
        volumes_count = scanner.scan_single_series(series_id, force_metadata_refresh=force_metadata_refresh)

        # Un scan remplace tous les volumes (delete + insert): si la série était déjà
        # matchée à Komga, les liens directs par volume seraient sinon perdus jusqu'au
        # prochain matching manuel. Best-effort: n'échoue pas le scan si Komga est
        # indisponible/non configuré.
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT title, komga_series_id, ebdz_thread_id FROM series WHERE id = ?', (series_id,))
        row = cursor.fetchone()
        conn.close()

        if row and row['komga_series_id']:
            from blueprints.komga.client import KomgaClient, KomgaError
            try:
                client = KomgaClient()
                _sync_komga_books(series_id, row['komga_series_id'], client)
            except KomgaError:
                pass
        elif row:
            # Pas encore matchée à Komga: tenter un matching automatique par titre
            from blueprints.komga.client import KomgaClient, KomgaError
            try:
                client = KomgaClient()
                _try_komga_title_match(series_id, row['title'], client)
            except KomgaError:
                pass

        # Idem côté EBDZ: tenter/rafraîchir le matching automatique par titre si la série
        # n'est pas déjà matchée à un thread précis (recherche locale, pas d'appel réseau)
        if row and not row['ebdz_thread_id']:
            try:
                _ebdz_enrich_series(series_id)
            except Exception:
                pass

        return jsonify({'success': True, 'volumes_count': volumes_count})

    except SeriesDirectoryMissingError as e:
        return jsonify({
            'success': True,
            'deleted': True,
            'message': f'Répertoire introuvable, série "{e}" supprimée de la bibliothèque'
        })

    except Exception as e:
        error_msg = 'Erreur interne'
        print(f"❌ Erreur lors du scan de la série {series_id}: {error_msg}")
        return jsonify({
            'success': False,
            'error': f'Erreur lors du scan: {error_msg}'
        }), 500


def _get_owned_volumes(cursor, series_id):
    """Récupère les volumes possédés d'une série (numérotés + indicateur one-shot/non numéroté)"""
    # Les fichiers one-shot / non reconnus n'ont pas de volume_number: on les compte
    # quand même comme "1 entrée possédée" plutôt que de les ignorer silencieusement
    cursor.execute('''
        SELECT DISTINCT volume_number FROM volumes
        WHERE series_id = ? AND volume_number IS NOT NULL
    ''', (series_id,))
    owned_volumes = sorted(row['volume_number'] for row in cursor.fetchall())

    cursor.execute('''
        SELECT COUNT(*) FROM volumes WHERE series_id = ? AND volume_number IS NULL
    ''', (series_id,))
    owned_has_unnumbered = cursor.fetchone()[0] > 0

    return owned_volumes, owned_has_unnumbered


def _bulk_ebdz_autodetect(library_id):
    """Version optimisée de la détection EBDZ automatique par titre (voir
    _ebdz_enrich_series) pour TOUTES les séries non matchées d'une bibliothèque en une
    seule passe, utilisée par le scan de bibliothèque.

    _ebdz_enrich_series refait, pour chaque série, une requête SQL sur ed2k_links avec une
    fonction Python appelée pour chaque ligne (search_normalize): sur une table de
    plusieurs dizaines de milliers de lignes, ça coûte plus d'une seconde par série. Avec
    ne serait-ce que quelques dizaines de séries non matchées, la boucle finissait par
    prendre plusieurs minutes, donnant l'impression que le scan ne terminait jamais.

    Ici, la table ed2k_links est chargée et normalisée UNE SEULE FOIS en mémoire, puis
    chaque série non matchée est comparée par une simple recherche de sous-chaîne en Python
    pur (rapide, pas de traversée Python<->SQLite par ligne). Ne fait rien si aucune série
    non matchée ou si la base EBDZ n'est pas disponible (best-effort, comme l'original).
    """
    from blueprints.search.routes import normalize_search_text, ebdz_title_variants

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT id, title, is_oneshot FROM series WHERE library_id = ? AND ebdz_thread_id IS NULL',
        (library_id,)
    )
    unmatched = cursor.fetchall()
    if not unmatched:
        conn.close()
        return

    owned_by_series = {row['id']: _get_owned_volumes(cursor, row['id']) for row in unmatched}

    ebdz_conn = sqlite3.connect(current_app.config['DB_FILE'], timeout=30.0)
    ebdz_cursor = ebdz_conn.cursor()
    ebdz_cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ed2k_links'")
    if ebdz_cursor.fetchone() is None:
        ebdz_conn.close()
        conn.close()
        return

    ebdz_cursor.execute('SELECT volume, thread_url, thread_id, thread_title, filename FROM ed2k_links')
    # (volume, thread_url, thread_id, thread_title, norm_title, norm_filename)
    normalized_rows = [
        (volume, thread_url, thread_id, thread_title,
         normalize_search_text(thread_title or ''), normalize_search_text(filename or ''))
        for volume, thread_url, thread_id, thread_title, filename in ebdz_cursor.fetchall()
    ]
    ebdz_conn.close()

    for series_row in unmatched:
        series_id = series_row['id']
        series_title = series_row['title']
        is_oneshot = bool(series_row['is_oneshot'])
        owned_volumes, owned_has_unnumbered = owned_by_series[series_id]

        # ebdz_title_variants: voir _ebdz_enrich_series ci-dessus pour le raisonnement
        # complet - même point d'entrée unique que le reste de l'app pour cette logique.
        normalized_terms = [normalize_search_text(term) for term in ebdz_title_variants(series_title)]

        matching_rows = [
            row for row in normalized_rows
            if any(term in row[4] or term in row[5] for term in normalized_terms)
        ]
        distinct_thread_ids = {row[2] for row in matching_rows if row[2] is not None}

        if len(distinct_thread_ids) == 1:
            matched_thread_id = next(iter(distinct_thread_ids))
            matched_title = next((row[3] for row in matching_rows if row[3]), None)
            ebdz_thread_url = next((row[1] for row in matching_rows if row[1]), None)
            ebdz_volumes = sorted({row[0] for row in matching_rows if row[0] is not None})
            ebdz_has_unnumbered = any(row[0] is None for row in matching_rows)

            missing_volumes = [] if is_oneshot else sorted(set(ebdz_volumes) - set(owned_volumes))
            ebdz_count = len(ebdz_volumes) + (1 if ebdz_has_unnumbered else 0)

            cursor.execute('''
                UPDATE series
                SET ebdz_volumes_count = ?, ebdz_missing_volumes = ?, ebdz_checked_at = CURRENT_TIMESTAMP,
                    ebdz_thread_url = ?, ebdz_thread_id = ?, ebdz_matched_title = ?, ebdz_match_status = 'matched'
                WHERE id = ?
            ''', (ebdz_count, json.dumps(missing_volumes), ebdz_thread_url, matched_thread_id, matched_title, series_id))
        else:
            cursor.execute('''
                UPDATE series
                SET ebdz_volumes_count = NULL, ebdz_missing_volumes = NULL, ebdz_checked_at = CURRENT_TIMESTAMP,
                    ebdz_thread_url = NULL, ebdz_thread_id = NULL, ebdz_matched_title = NULL, ebdz_match_status = 'unmatched'
                WHERE id = ?
            ''', (series_id,))

    conn.commit()
    conn.close()


def _ebdz_enrich_series(series_id):
    """Compare les volumes possédés d'une série au nombre d'entrées uniques trouvées sur EBDZ.

    Si la série est déjà matchée manuellement (ou automatiquement lors d'un précédent
    appel) à un thread EBDZ précis, la comparaison se fait directement sur ce thread
    (simple et fiable). Sinon, une détection automatique par titre est tentée: si elle
    désigne un unique thread sans ambiguïté, il est adopté comme match; sinon la série
    reste "non matchée" et les candidats trouvés sont renvoyés pour un matching manuel.

    Factorisé hors de la route pour être aussi appelable depuis un scan (matching
    automatique d'une série nouvellement créée). Lève ValueError si la série n'existe
    pas, RuntimeError si la base EBDZ n'est pas disponible.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('SELECT title, ebdz_thread_id, is_oneshot FROM series WHERE id = ?', (series_id,))
    series_row = cursor.fetchone()

    if not series_row:
        conn.close()
        raise ValueError('Série introuvable')

    series_title = series_row['title']
    matched_thread_id = series_row['ebdz_thread_id']
    is_oneshot = bool(series_row['is_oneshot'])

    owned_volumes, owned_has_unnumbered = _get_owned_volumes(cursor, series_id)
    conn.close()

    ebdz_conn = sqlite3.connect(current_app.config['DB_FILE'], timeout=30.0)
    ebdz_cursor = ebdz_conn.cursor()

    ebdz_cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ed2k_links'")
    if ebdz_cursor.fetchone() is None:
        ebdz_conn.close()
        raise RuntimeError('Base EBDZ non disponible')

    match_status = 'matched'
    matched_title = None
    candidates = []

    if matched_thread_id is not None:
        # Série déjà matchée à un thread précis: comparaison directe, sans recherche floue
        ebdz_cursor.execute('''
            SELECT volume, thread_url, thread_title FROM ed2k_links WHERE thread_id = ?
        ''', (matched_thread_id,))
        rows = ebdz_cursor.fetchall()
        matched_title = next((row[2] for row in rows if row[2]), None)
    else:
        from blueprints.search.routes import normalize_search_text, ebdz_core_title, ebdz_title_variants
        ebdz_conn.create_function('search_normalize', 1, normalize_search_text)

        search_terms = ebdz_title_variants(series_title)

        normalized_terms = [f'%{normalize_search_text(term)}%' for term in search_terms]

        # En priorité: threads dont le TITRE correspond réellement à un des termes de
        # recherche. Une correspondance sur le nom de fichier seul est bien plus
        # bruitée qu'on ne s'y attend (mot du titre présent par coïncidence dans un nom
        # de fichier sans rapport, tag de collection partagé entre plusieurs séries...
        # voir "Sagesse"/"Echecs"): on ne s'y replie que si AUCUN titre ne correspond
        title_where_clause = ' OR '.join(['search_normalize(thread_title) LIKE ?'] * len(normalized_terms))
        ebdz_cursor.execute(f'''
            SELECT volume, thread_url, thread_id, thread_title FROM ed2k_links
            WHERE {title_where_clause}
        ''', normalized_terms)
        all_rows = ebdz_cursor.fetchall()

        if not all_rows:
            filename_where_clause = ' OR '.join(['search_normalize(filename) LIKE ?'] * len(normalized_terms))
            ebdz_cursor.execute(f'''
                SELECT volume, thread_url, thread_id, thread_title FROM ed2k_links
                WHERE {filename_where_clause}
            ''', normalized_terms)
            all_rows = ebdz_cursor.fetchall()

        distinct_thread_ids = {row[2] for row in all_rows if row[2] is not None}

        # Un titre EBDZ est souvent suivi d'un suffixe entre parenthèses/crochets (nom
        # d'auteur(s), article déplacé pour le tri...), ex: "Echecs [Victor L. Pinel]".
        # On tente donc d'abord une correspondance stricte sur le titre nettoyé de ce
        # suffixe avant de retomber sur la détection par sous-chaîne classique
        # (plusieurs threads peuvent matcher par sous-chaîne sans être le même titre).
        normalized_series_terms = {normalize_search_text(term) for term in search_terms}
        exact_title_thread_ids = {
            row[2] for row in all_rows
            if row[2] is not None
            and normalize_search_text(ebdz_core_title(row[3] or '')) in normalized_series_terms
        }

        if len(exact_title_thread_ids) == 1:
            matched_thread_id = next(iter(exact_title_thread_ids))
            rows = [row for row in all_rows if row[2] == matched_thread_id]
            matched_title = next((row[3] for row in rows if row[3]), None)
        elif len(distinct_thread_ids) == 1:
            # Un seul thread trouvé, sans ambiguïté: on l'adopte automatiquement comme match
            matched_thread_id = next(iter(distinct_thread_ids))
            rows = all_rows
            matched_title = next((row[3] for row in rows if row[3]), None)
        else:
            match_status = 'unmatched'
            rows = []
            seen = set()
            for row in all_rows:
                thread_id = row[2]
                if thread_id is None or thread_id in seen:
                    continue
                seen.add(thread_id)
                candidates.append({'thread_id': thread_id, 'thread_title': row[3], 'thread_url': row[1]})

    ebdz_conn.close()

    ebdz_volumes = sorted({row[0] for row in rows if row[0] is not None})
    ebdz_has_unnumbered = any(row[0] is None for row in rows)
    ebdz_thread_url = next((row[1] for row in rows if row[1]), None)

    # Sur un one-shot, il n'y a pas de numérotation de tomes à comparer: le fichier
    # unique possédé EST l'oeuvre complète, même si EBDZ la référence en plusieurs
    # entrées numérotées. On ne calcule/persiste donc aucun volume "manquant"
    missing_volumes = [] if is_oneshot else sorted(set(ebdz_volumes) - set(owned_volumes))

    owned_count = len(owned_volumes) + (1 if owned_has_unnumbered else 0)
    ebdz_count = len(ebdz_volumes) + (1 if ebdz_has_unnumbered else 0)
    # Une release one-shot/non numérotée trouvée sur EBDZ mais absente de la collection
    unnumbered_missing = not is_oneshot and ebdz_has_unnumbered and not owned_has_unnumbered

    # Persister le résultat pour qu'il reste visible après un rechargement de la page
    conn = get_db_connection()
    if match_status == 'matched':
        conn.execute('''
            UPDATE series
            SET ebdz_volumes_count = ?, ebdz_missing_volumes = ?, ebdz_checked_at = CURRENT_TIMESTAMP,
                ebdz_thread_url = ?, ebdz_thread_id = ?, ebdz_matched_title = ?, ebdz_match_status = 'matched'
            WHERE id = ?
        ''', (ebdz_count, json.dumps(missing_volumes), ebdz_thread_url, matched_thread_id, matched_title, series_id))
    else:
        conn.execute('''
            UPDATE series
            SET ebdz_volumes_count = NULL, ebdz_missing_volumes = NULL, ebdz_checked_at = CURRENT_TIMESTAMP,
                ebdz_thread_url = NULL, ebdz_thread_id = NULL, ebdz_matched_title = NULL, ebdz_match_status = 'unmatched'
            WHERE id = ?
        ''', (series_id,))
    conn.commit()
    conn.close()

    return {
        'series_title': series_title,
        'match_status': match_status,
        'matched_title': matched_title,
        'ebdz_thread_id': matched_thread_id if match_status == 'matched' else None,
        'candidates': candidates,
        'owned_count': owned_count,
        'ebdz_count': ebdz_count if match_status == 'matched' else None,
        'owned_volumes': owned_volumes,
        'ebdz_volumes': ebdz_volumes if match_status == 'matched' else [],
        'missing_volumes': missing_volumes if match_status == 'matched' else [],
        'unnumbered_missing': unnumbered_missing,
        'ebdz_thread_url': ebdz_thread_url if match_status == 'matched' else None
    }


@library_bp.route('/api/series/<int:series_id>/ebdz-enrich', methods=['POST'])
def enrich_series_ebdz(series_id):
    """Compare les volumes possédés d'une série au nombre d'entrées uniques trouvées sur EBDZ
    (voir _ebdz_enrich_series pour le détail du matching automatique par titre)."""
    try:
        result = _ebdz_enrich_series(series_id)
    except ValueError as e:
        return jsonify({'success': False, 'error': 'Erreur interne'}), 404
    except Exception as e:
        return jsonify({'success': False, 'error': 'Erreur interne'}), 500

    result['success'] = True
    return jsonify(result)


@library_bp.route('/api/series/<int:series_id>/ebdz-candidates', methods=['GET'])
def ebdz_match_candidates(series_id):
    """Recherche des threads EBDZ candidats pour un matching manuel.

    Par défaut recherche sur le titre de la série, mais accepte un paramètre ?q= pour
    affiner la recherche si le titre local ne correspond pas au titre EBDZ.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT title FROM series WHERE id = ?', (series_id,))
        series_row = cursor.fetchone()
        conn.close()

        if not series_row:
            return jsonify({'success': False, 'error': 'Série introuvable'}), 404

        query = request.args.get('q', '').strip() or series_row['title']

        from blueprints.search.routes import search_ebdz_threads
        try:
            candidates = search_ebdz_threads(query)
        except LookupError as e:
            return jsonify({'success': False, 'error': 'Erreur interne'}), 500

        return jsonify({'success': True, 'candidates': candidates})

    except Exception as e:
        return jsonify({'success': False, 'error': 'Erreur interne'}), 500


@library_bp.route('/api/series/<int:series_id>/ebdz-match', methods=['POST'])
def ebdz_match_series(series_id):
    """Matche manuellement une série à un thread EBDZ précis (choisi par l'utilisateur, ou
    collé sous forme d'URL de thread - le tid= y est extrait, même regex que le scraper).
    Le thread doit déjà avoir été scrapé (présent dans ed2k_links): contrairement à
    Bedetheque, il n'y a pas de requête live sur une URL arbitraire."""
    try:
        data = request.get_json(silent=True) or {}
        thread_id = data.get('thread_id')
        thread_url = (data.get('thread_url') or '').strip()

        if thread_id is None and thread_url:
            tid_match = re.search(r'tid=(\d+)', thread_url)
            if not tid_match:
                return jsonify({'success': False, 'error': "Impossible de trouver le tid dans cette URL"}), 400
            thread_id = int(tid_match.group(1))

        if thread_id is None:
            return jsonify({'success': False, 'error': 'thread_id ou thread_url requis'}), 400

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT title FROM series WHERE id = ?', (series_id,))
        series_row = cursor.fetchone()

        if not series_row:
            conn.close()
            return jsonify({'success': False, 'error': 'Série introuvable'}), 404

        owned_volumes, owned_has_unnumbered = _get_owned_volumes(cursor, series_id)
        conn.close()

        ebdz_conn = sqlite3.connect(current_app.config['DB_FILE'], timeout=30.0)
        ebdz_cursor = ebdz_conn.cursor()
        ebdz_cursor.execute('''
            SELECT volume, thread_url, thread_title FROM ed2k_links WHERE thread_id = ?
        ''', (thread_id,))
        rows = ebdz_cursor.fetchall()
        ebdz_conn.close()

        if not rows:
            return jsonify({'success': False, 'error': 'Thread EBDZ introuvable'}), 404

        matched_title = next((row[2] for row in rows if row[2]), None)
        ebdz_thread_url = next((row[1] for row in rows if row[1]), None)

        ebdz_volumes = sorted({row[0] for row in rows if row[0] is not None})
        ebdz_has_unnumbered = any(row[0] is None for row in rows)

        missing_volumes = sorted(set(ebdz_volumes) - set(owned_volumes))
        owned_count = len(owned_volumes) + (1 if owned_has_unnumbered else 0)
        ebdz_count = len(ebdz_volumes) + (1 if ebdz_has_unnumbered else 0)
        unnumbered_missing = ebdz_has_unnumbered and not owned_has_unnumbered

        conn = get_db_connection()
        conn.execute('''
            UPDATE series
            SET ebdz_volumes_count = ?, ebdz_missing_volumes = ?, ebdz_checked_at = CURRENT_TIMESTAMP,
                ebdz_thread_url = ?, ebdz_thread_id = ?, ebdz_matched_title = ?, ebdz_match_status = 'matched'
            WHERE id = ?
        ''', (ebdz_count, json.dumps(missing_volumes), ebdz_thread_url, thread_id, matched_title, series_id))
        conn.commit()
        conn.close()

        return jsonify({
            'success': True,
            'match_status': 'matched',
            'matched_title': matched_title,
            'ebdz_thread_id': thread_id,
            'owned_count': owned_count,
            'ebdz_count': ebdz_count,
            'missing_volumes': missing_volumes,
            'unnumbered_missing': unnumbered_missing,
            'ebdz_thread_url': ebdz_thread_url
        })

    except Exception as e:
        return jsonify({'success': False, 'error': 'Erreur interne'}), 500


@library_bp.route('/api/series/<int:series_id>/ebdz-unmatch', methods=['POST'])
def ebdz_unmatch_series(series_id):
    """Retire le matching EBDZ d'une série (pour la re-matcher manuellement ensuite)"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM series WHERE id = ?', (series_id,))
        if not cursor.fetchone():
            conn.close()
            return jsonify({'success': False, 'error': 'Série introuvable'}), 404

        conn.execute('''
            UPDATE series
            SET ebdz_volumes_count = NULL, ebdz_missing_volumes = NULL, ebdz_checked_at = CURRENT_TIMESTAMP,
                ebdz_thread_url = NULL, ebdz_thread_id = NULL, ebdz_matched_title = NULL, ebdz_match_status = 'unmatched'
            WHERE id = ?
        ''', (series_id,))
        conn.commit()
        conn.close()

        return jsonify({'success': True, 'match_status': 'unmatched'})

    except Exception as e:
        return jsonify({'success': False, 'error': 'Erreur interne'}), 500


def _download_komga_cover(komga_series_id, client):
    """Télécharge la vignette d'une série Komga et la sauvegarde localement.
    Retourne le chemin relatif (servi via /covers/...) ou None en cas d'échec."""
    content, content_type = client.download_thumbnail(komga_series_id)
    if not content:
        return None

    ext = '.jpg'
    if content_type and 'png' in content_type:
        ext = '.png'
    elif content_type and 'webp' in content_type:
        ext = '.webp'

    covers_dir = os.path.join(current_app.config['COVERS_DIR'], 'komga')
    os.makedirs(covers_dir, exist_ok=True)
    filename = f"{komga_series_id}{ext}"
    filepath = os.path.join(covers_dir, filename)

    with open(filepath, 'wb') as f:
        f.write(content)

    return f"covers/komga/{filename}"


def _sync_komga_books(series_id, komga_series_id, client):
    """Associe chaque volume local de la série au livre Komga correspondant, PAR NOM DE
    FICHIER plutôt que par numéro de tome: Komga nomme chaque livre depuis le fichier
    lui-même, donc son "name" correspond exactement au nom de fichier local sans son
    extension (constaté sur toute la bibliothèque, aucun écart) - le fichier est
    strictement le même des deux côtés, pas besoin de recalculer une correspondance de
    numéro qui peut différer (Komga peut numéroter une "édition"/collection différemment
    des numéros absolus locaux, voir "Boule et Bill -02-"), ou être totalement absente
    pour un omnibus/hors-série/anthologie sans "number" ni "#INT"/"#HS" dans le nom
    (voir "Le Gaulois (Autres)", livres nommés "#201610 - ..." - un code date, pas un
    numéro de séquence). L'ancienne logique par numéro laissait ~550 tomes sans lien sur
    cette bibliothèque avant ce changement.

    Repli sur une similarité de mots (même principe que match_bedetheque_volume côté
    Bédéthèque) si aucune correspondance exacte de nom - rattrape un écart mineur
    (normalisation Unicode, espace en trop...), toujours par nom, jamais par numéro.

    Best-effort: en cas d'erreur, les volumes restent simplement sans lien direct (pas
    bloquant pour le matching de la série elle-même)."""
    from blueprints.komga.client import KomgaError, KomgaSeriesNotFoundError

    def _resync_stale_match():
        conn = get_db_connection()
        row = conn.execute('SELECT title FROM series WHERE id = ?', (series_id,)).fetchone()
        conn.close()
        if row and _try_komga_title_match(series_id, row['title'], client):
            return
        _clear_komga_match(series_id)

    try:
        books = client.get_series_books(komga_series_id)
    except KomgaSeriesNotFoundError:
        _resync_stale_match()
        return
    except KomgaError:
        return

    if not books:
        # Une série avec un komga_series_id périmé (recréée sous un nouvel id côté Komga)
        # ne renvoie pas toujours une 404 sur /books - constaté: liste simplement vide,
        # contrairement à /series/{id} qui, lui, 404 correctement. Une série qui a
        # pourtant de vrais fichiers locaux ne devrait jamais avoir zéro livre Komga -
        # revérifier l'existence de la série elle-même avant d'abandonner.
        conn = get_db_connection()
        local_files = conn.execute('SELECT filename FROM volumes WHERE series_id = ?', (series_id,)).fetchall()
        conn.close()
        has_local_files = any(v['filename'] for v in local_files)
        if has_local_files:
            try:
                client.get_series(komga_series_id)
            except KomgaSeriesNotFoundError:
                _resync_stale_match()
                return
            except KomgaError:
                pass

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT id, filename FROM volumes WHERE series_id = ?', (series_id,))
    local_volumes = cursor.fetchall()

    cursor.execute(
        'UPDATE volumes SET komga_book_id = NULL, komga_book_url = NULL WHERE series_id = ?',
        (series_id,)
    )

    import os as _os
    from blueprints.bedetheque.scraper import _local_title_from_filename, BedethequeScraper

    books_by_exact_name = {}
    for book in books:
        name = (book.get('name') or '').strip()
        if name:
            books_by_exact_name.setdefault(name, book)

    for vol in local_volumes:
        if not vol['filename']:
            continue

        book = books_by_exact_name.get(_os.path.splitext(vol['filename'])[0])

        if book is None:
            if len(books) == 1:
                book = books[0]
            else:
                local_title = _local_title_from_filename(vol['filename'])
                if local_title:
                    best_book, best_score = None, 0.0
                    for candidate in books:
                        if not candidate.get('name'):
                            continue
                        score = BedethequeScraper._match_score(local_title, candidate['name'])
                        if score > best_score:
                            best_book, best_score = candidate, score
                    if best_book is not None and best_score >= 0.35:
                        book = best_book

        if book:
            cursor.execute(
                'UPDATE volumes SET komga_book_id = ?, komga_book_url = ? WHERE id = ?',
                (book['komga_book_id'], book['url'], vol['id'])
            )

    conn.commit()
    conn.close()


def _apply_komga_match(series_id, series_info, client):
    """Persiste un match Komga (identité + vignette de repli) pour une série et renvoie
    les champs Komga tels que stockés en base (source unique de vérité pour la réponse).

    Ne récupère plus le résumé/statut/auteurs de Komga (komga_summary/komga_status/
    komga_total_volumes/komga_authors) - Bédéthèque est la seule source de métadonnées
    descriptives de l'appli (voir CLAUDE.md), Komga n'est qu'un serveur de lecture en
    aval à garder synchronisé (liens "Ouvrir sur Komga", komga_book_id par tome via
    _sync_komga_books) - récupérer ses propres métadonnées par-dessus n'avait plus
    d'usage réel. La vignette (komga_cover_path) reste en repli si aucune couverture
    locale/Bédéthèque n'existe."""
    cover_path = _download_komga_cover(series_info['komga_series_id'], client)

    conn = get_db_connection()
    conn.execute('''
        UPDATE series
        SET komga_series_id = ?, komga_match_status = 'matched', komga_matched_title = ?,
            komga_url = ?, komga_cover_path = COALESCE(?, komga_cover_path), komga_checked_at = CURRENT_TIMESTAMP
        WHERE id = ?
    ''', (
        series_info['komga_series_id'], series_info['title'], series_info['url'],
        cover_path, series_id
    ))
    conn.commit()
    conn.close()

    _sync_komga_books(series_id, series_info['komga_series_id'], client)

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT komga_series_id, komga_match_status, komga_matched_title, komga_url,
               komga_cover_path
        FROM series WHERE id = ?
    ''', (series_id,))
    row = cursor.fetchone()
    conn.close()

    return {
        'komga_series_id': row['komga_series_id'],
        'match_status': row['komga_match_status'],
        'matched_title': row['komga_matched_title'],
        'komga_url': row['komga_url'],
        'komga_cover_path': row['komga_cover_path']
    }


def _clear_komga_match(series_id):
    conn = get_db_connection()
    conn.execute('''
        UPDATE series
        SET komga_series_id = NULL, komga_match_status = 'unmatched', komga_matched_title = NULL,
            komga_url = NULL, komga_status = NULL, komga_total_volumes = NULL, komga_summary = NULL,
            komga_authors = NULL, komga_cover_path = NULL, komga_checked_at = CURRENT_TIMESTAMP
        WHERE id = ?
    ''', (series_id,))
    conn.execute(
        'UPDATE volumes SET komga_book_id = NULL, komga_book_url = NULL WHERE series_id = ?',
        (series_id,)
    )
    conn.commit()
    conn.close()


# Retire les suffixes entre parenthèses/crochets (auteurs, article déplacé pour le tri
# comme "[Le]"...) qui empêchent la recherche Komga d'aboutir quand ils contiennent des
# caractères que son moteur de recherche ne gère pas bien (ex: "/" entre deux auteurs)
def _strip_bracketed_suffix(title):
    result = []
    value = title or ''
    i = 0
    while i < len(value):
        if value[i] in '([':
            close = ')' if value[i] == '(' else ']'
            end = value.find(close, i + 1)
            if end >= 0:
                result.append(' ')
                i = end + 1
                continue
        result.append(value[i])
        i += 1
    return ' '.join(''.join(result).split())
