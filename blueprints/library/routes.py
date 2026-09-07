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
from pathlib import Path
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


# Empêche un import manuel (execute_import, requête HTTP) et l'import automatique
# (execute_auto_import, tourne sur son propre thread APScheduler - voir
# blueprints/library/scheduler.py) de s'exécuter en même temps : chacun ouvre plusieurs
# connexions SQLite successives pendant sa boucle par fichier, et les faire cohabiter a
# déjà laissé une opération bloquée sur 'started' sans aucun fichier journalisé ("pourquoi
# il y a Aucun fichier détaillé pour cet import"), les deux ayant démarré à 2 secondes
# d'écart. Un simple verrou en mémoire process suffit (les deux tournent dans le même
# process Python, threads différents) - voir son acquisition dans execute_import/
# execute_auto_import.
#
# MÊME OBJET que scan_import_lock (blueprints/library/scanner.py), pas une simple
# coïncidence de nom : scan_single_series fait un DELETE+rebuild complet des volumes
# réellement possédés d'une série depuis un instantané os.listdir(), ce qui perd
# silencieusement une ligne fraîchement écrite par un import concurrent pour cette même
# série si un fichier est renommé pile entre l'instantané et sa lecture (voir le
# commentaire de scan_import_lock pour l'incident réel qui a révélé ce bug - "Les
# bidochon" tome 4). Réutiliser le même verrou ici plutôt qu'en créer un second garantit
# qu'aucun scan de série ne peut jamais s'intercaler au milieu d'un import, sans risque
# d'oublier de synchroniser les deux séparément.
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

# "the conversion to pdf is very slow and takes a lot of memory. how can you improve so
# the app don't get stucked" - une conversion pdf/cbr->cbz (rendu page par page en JPEG
# pour le pdf) est un travail CPU/mémoire non négligeable par fichier (des centaines de
# Mo de pixmaps successifs pour un gros scan, plusieurs minutes), et rien n'empêchait
# jusqu'ici plusieurs conversions de tourner VRAIMENT en parallèle (serveur de dev Flask
# threaded=True: un bouton "Convertir" cliqué sur plusieurs fichiers à la fois, ou un clic
# manuel pendant que l'import automatique convertit aussi son propre fichier) - constaté
# en réel: jusqu'à 3 fichiers temporaires de conversion actifs simultanément, chacun
# consommant sa propre mémoire de rendu. Un verrou global sérialise toute conversion
# (pdf ET cbr, voir son acquisition dans convert_import_file/_maybe_convert_import_file_to_cbz)
# à UNE SEULE à la fois pour tout le process, quelle que soit la source du déclenchement -
# n'accélère pas une conversion individuelle, mais plafonne le pic mémoire à celui d'une
# seule conversion au lieu de plusieurs cumulées.
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
        # "notification de telegram met un lien vers komga" - komga_book_url (voir
        # buildVolumeLinksIconHtml, library.js, même champ) n'est quasi jamais déjà connu
        # pour un tome tout juste importé: le scan Komga qui le découvrirait tourne en
        # arrière-plan avec un délai (trigger_scan_async, voir CLAUDE.md), pas encore
        # passé au moment où cette notification part. series.komga_url (la fiche SÉRIE,
        # matchée une fois et stable d'un import à l'autre) est en revanche déjà connu -
        # moins précis qu'un lien direct vers CE tome, mais réellement disponible ici.
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
            # "je ne reçois plus de notification telegram" - un gros import (70 fichiers
            # d'un coup, constaté en réel) listant CHAQUE fichier peut dépasser la limite
            # Telegram (4096 caractères en texte, 1024 en légende de photo) - l'API rejette
            # alors le message ENTIER, et send_telegram_notification renvoyait ce rejet
            # sans que rien ici ne le vérifie ni le journalise (voir plus bas): la
            # notification disparaissait silencieusement pour tout gros import, jamais
            # pour un petit. Même convention d'échantillon que _check_new_files_available
            # (scheduler.py, "X nouveau(x) fichier(s)... et Y autre(s)") plutôt qu'une
            # troncature arbitraire à l'aveugle.
            sample = blocks[:10]
            message += "\n\n" + "\n\n".join(sample)
            if len(blocks) > len(sample):
                message += f"\n\n… et {len(blocks) - len(sample)} autre(s)"

        # "add the cover to telegram notification" - une couverture parle plus qu'une
        # liste de noms de fichiers, mais l'API Telegram n'accepte qu'UNE photo par
        # message (sendPhoto) : pertinent uniquement quand ce batch d'import concerne
        # une seule et même série, jamais choisie arbitrairement parmi plusieurs.
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
        # Numéro d'épisode jamais confondu avec volume_number: Bédéthèque numérote un
        # épisode et un tome dans le même champ (Tome 1/Épisode 1 partagent number=1,
        # série #70835 "La Bête") - voir _sync_bedetheque_placeholder_volumes.
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
        # "in rugby there is a file BO4... i cannot select it" - un bonus/promo (is_special,
        # voir volume_override plus haut) n'a par nature pas de numéro: partage la même
        # identité "vide" (volume_number/is_integral/is_hs/is_episode tous NULL/0) qu'un
        # one-shot OU qu'un AUTRE fichier spécial de la même série (BO1 et BO4 n'ont rien
        # de commun hormis "sans numéro") - la branche one-shot ci-dessous les aurait
        # fusionnés à tort. special_label distingue s'il est connu, sinon chaque import
        # "Spécial" reste unique (jamais fusionné au hasard avec un autre).
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
            return jsonify({'success': False, 'error': str(e)}), 500


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
        # "why loading the name of the bibliotheque on top take so long" - cette route
        # faisait un SELECT * sur TOUTES les séries de la bibliothèque (500+ lignes,
        # chaque colonne y compris les gros champs texte comme bedetheque_description/
        # bedetheque_albums) rien que pour répondre au nom/chemin affichés dans l'en-tête
        # (voir loadLibraryInfo, library.js) - le seul appelant GET de cette route,
        # jamais la clé `series` de la réponse. Aucun autre appelant ne la lit non plus
        # (vérifié: index.js n'appelle cette route qu'en DELETE, library.js qu'en GET/PUT).
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
            
            conn.commit()
            conn.close()
            
            return jsonify({'success': True})
        
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500


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
        error_msg = str(e)
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
        error_msg = str(e)
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
            # Rien trouvé, ou plusieurs threads différents possibles: laisse l'utilisateur
            # choisir manuellement plutôt que de deviner (même comportement que
            # _ebdz_enrich_series)
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

        # ebdz_title_variants: même point d'entrée unique que /api/search et
        # search_ebdz_threads (search/routes.py) - gère à la fois le suffixe
        # parenthèses/crochets local ("Virus (RicardRica)") et l'article en tête/fin de
        # titre ("Le Titre" <-> "Titre, Le"/"Titre (Le)"). "Le grand vide (Murawiec)
        # could not match ebdz only if i ask about grand vide without le": cette fonction
        # ne gérait jusqu'ici que l'article en FIN de titre (get_suffix_article_variants),
        # jamais l'article en TÊTE - voir la docstring d'ebdz_title_variants pour l'
        # historique complet de cette unification.
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
            # Rien trouvé, ou plusieurs threads différents possibles: on laisse
            # l'utilisateur choisir manuellement plutôt que de deviner
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
        return jsonify({'success': False, 'error': str(e)}), 404
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

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
            return jsonify({'success': False, 'error': str(e)}), 500

        return jsonify({'success': True, 'candidates': candidates})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


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
        return jsonify({'success': False, 'error': str(e)}), 500


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
        return jsonify({'success': False, 'error': str(e)}), 500


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
    (voir "Astérix (Autres)", livres nommés "#201610 - ..." - un code date, pas un
    numéro de séquence). L'ancienne logique par numéro laissait ~550 tomes sans lien sur
    cette bibliothèque avant ce changement.

    Repli sur une similarité de mots (même principe que match_bedetheque_volume côté
    Bédéthèque) si aucune correspondance exacte de nom - rattrape un écart mineur
    (normalisation Unicode, espace en trop...), toujours par nom, jamais par numéro.

    Best-effort: en cas d'erreur, les volumes restent simplement sans lien direct (pas
    bloquant pour le matching de la série elle-même)."""
    from blueprints.komga.client import KomgaError, KomgaSeriesNotFoundError

    def _resync_stale_match():
        # "j'ai ajouté un fichier manuellement mais ça a pas mis à jour les liens komga" -
        # komga_series_id pointait vers une série qui n'existe plus du tout côté Komga
        # (recréée avec un nouvel id après un rescan, ex: dossier renommé/déplacé) -
        # _sync_komga_books avalait cette erreur silencieusement à chaque appel sans
        # jamais se corriger. Retenter un matching par titre plutôt que d'abandonner:
        # _try_komga_title_match, si concluant, appelle _apply_komga_match qui relance
        # lui-même _sync_komga_books avec le nouvel id - pas de récursion si aucun match
        # n'est trouvé (retourne None sans jamais rappeler cette fonction).
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
        return 0
    except KomgaError:
        return 0

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
                return 0
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
    matched_count = 0

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
            matched_count += 1

    conn.commit()
    conn.close()
    return matched_count


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

    matched_books = _sync_komga_books(series_id, series_info['komga_series_id'], client)

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
        , 'matched_books': matched_books
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
    stripped = re.sub(r'\s*[\(\[][^\)\]]*[\)\]]\s*', ' ', title).strip()
    return re.sub(r'\s+', ' ', stripped)


# Normalise un titre pour la comparaison de matching: ignore les mêmes suffixes que
# ci-dessus ainsi que la casse/ponctuation, pour que "Titre (Auteur A/Auteur B)" et
# "Titre (Auteur A / Auteur B)" (ou tout autre variante de ce suffixe) soient
# considérés comme le même titre
def _normalize_title_for_match(title):
    """Accents/casse/ponctuation ignorés (même approche NFKD que
    BedethequeScraper._normalize_for_match) - un nom de fichier de scan reproduit
    rarement les accents exacts du titre en base (constaté: "recit" au lieu de "récit"
    dans un nom de fichier), sans quoi une comparaison stricte échoue en silence."""
    if not title:
        return ''
    normalized = _strip_bracketed_suffix(title)
    normalized = unicodedata.normalize('NFKD', normalized)
    normalized = ''.join(c for c in normalized if not unicodedata.combining(c))
    normalized = normalized.lower()
    normalized = re.sub(r'[^\w\s]', ' ', normalized, flags=re.UNICODE)
    return re.sub(r'\s+', ' ', normalized).strip()


def get_owned_volume_signatures(series_ids, conn=None):
    """Pour chaque série de `series_ids`, l'ensemble des numéros RÉELLEMENT POSSÉDÉS
    (filepath NOT NULL), répartis par nature - un tome classique (volume_number), une
    intégrale numérotée (integral_number), un hors-série (hs_number) ou un épisode
    (episode_number) ne partagent pas le même espace de numérotation (une intégrale et un
    tome peuvent porter le même numéro sans être le même objet - voir
    match_bedetheque_volume). Utilisé pour comparer un fichier candidat (EBDZ/Telegram,
    déjà parsé par LibraryScanner.parse_filename) à ce qui est déjà possédé, plutôt que de
    ne dire que "la série existe" (voir _nouveautesMatched côté Nouveautés,
    "compares les volumes existants avec ceux nouveaux et si ceux de nouveautés sont
    manquants ou non de la série"). Retourne {series_id: {'volumes': set, 'integrals':
    set, 'hs': set, 'episodes': set}} - une entrée par série demandée, même si vide."""
    result = {sid: {'volumes': set(), 'integrals': set(), 'hs': set(), 'episodes': set()} for sid in series_ids}
    if not series_ids:
        return result

    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cursor = conn.cursor()
    placeholders = ','.join('?' * len(series_ids))
    cursor.execute(f'''
        SELECT series_id, volume_number, is_integral, integral_number, is_hs, hs_number, is_episode, episode_number
        FROM volumes
        WHERE series_id IN ({placeholders}) AND filepath IS NOT NULL
    ''', list(series_ids))
    for series_id, volume_number, is_integral, integral_number, is_hs, hs_number, is_episode, episode_number in cursor.fetchall():
        sig = result[series_id]
        if is_integral and integral_number is not None:
            sig['integrals'].add(integral_number)
        elif is_hs and hs_number is not None:
            sig['hs'].add(hs_number)
        elif is_episode and episode_number is not None:
            sig['episodes'].add(episode_number)
        elif volume_number is not None:
            sig['volumes'].add(volume_number)
    if close_conn:
        conn.close()
    return result


def volume_possession_status(owned, volume=None, is_integral=False, integral_number=None,
                              is_hs=False, hs_number=None, is_episode=False, episode_number=None):
    """True si ce numéro (tome/intégrale/HS/épisode - un seul de ces 4 signaux doit être
    renseigné, priorité intégrale > HS > épisode > tome classique puisqu'un fichier ne
    peut être qu'une seule de ces natures à la fois) est déjà possédé dans `owned` (voir
    get_owned_volume_signatures), False si un numéro est connu mais absent de `owned`,
    None si aucun numéro n'a pu être déterminé (rien à comparer - ne pas deviner)."""
    if is_integral and integral_number is not None:
        return integral_number in owned['integrals']
    if is_hs and hs_number is not None:
        return hs_number in owned['hs']
    if is_episode and episode_number is not None:
        return episode_number in owned['episodes']
    if volume is not None:
        return volume in owned['volumes']
    return None


@library_bp.route('/api/series/<int:series_id>/komga-candidates', methods=['GET'])
def komga_match_candidates(series_id):
    """Recherche des séries Komga candidates pour un matching manuel.

    Par défaut recherche sur le titre de la série, mais accepte un paramètre ?q= pour
    affiner la recherche si le titre local ne correspond pas au titre Komga.
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

        from blueprints.komga.client import KomgaClient, KomgaError, KomgaSeriesNotFoundError
        try:
            client = KomgaClient()
            candidates = client.search_series(query)
        except KomgaError as e:
            return jsonify({'success': False, 'error': str(e)}), 400

        return jsonify({'success': True, 'candidates': candidates})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@library_bp.route('/api/series/<int:series_id>/komga-match', methods=['POST'])
def komga_match_series(series_id):
    """Matche manuellement une série à une série Komga précise (choisie par l'utilisateur,
    identifiée soit par komga_series_id soit par komga_url - URL de la fiche série collée
    directement, même fallback que pour EBDZ/Bédéthèque, voir _simplify_series côté
    KomgaClient qui construit ces URLs sous la forme <base_url>/series/<uuid>)"""
    try:
        data = request.get_json(silent=True) or {}
        komga_series_id = data.get('komga_series_id')
        komga_url = data.get('komga_url')

        if not komga_series_id and komga_url:
            komga_series_id = komga_url.rstrip('/').rsplit('/', 1)[-1]

        if not komga_series_id:
            return jsonify({'success': False, 'error': 'komga_series_id ou komga_url requis'}), 400

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM series WHERE id = ?', (series_id,))
        exists = cursor.fetchone()
        conn.close()
        if not exists:
            return jsonify({'success': False, 'error': 'Série introuvable'}), 404

        from blueprints.komga.client import KomgaClient, KomgaError, KomgaSeriesNotFoundError
        try:
            client = KomgaClient()
            series_info = client.get_series(komga_series_id)
        except KomgaError as e:
            return jsonify({'success': False, 'error': str(e)}), 400

        result = _apply_komga_match(series_id, series_info, client)
        result['success'] = True
        return jsonify(result)

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@library_bp.route('/api/series/<int:series_id>/komga-unmatch', methods=['POST'])
def komga_unmatch_series(series_id):
    """Retire le matching Komga d'une série (pour la re-matcher manuellement ensuite)"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM series WHERE id = ?', (series_id,))
        if not cursor.fetchone():
            conn.close()
            return jsonify({'success': False, 'error': 'Série introuvable'}), 404
        conn.close()

        _clear_komga_match(series_id)
        return jsonify({'success': True, 'match_status': 'unmatched'})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# Recherche par titre une série Komga candidate et l'adopte comme match si elle désigne
# un résultat unique (ou un résultat dont le titre est une correspondance exacte
# non-ambiguë). Utilisé pour le matching manuel (bouton "Enrichir") et pour la tentative
# de matching automatique des séries nouvellement créées par un scan.
# Retourne le dict de résultat de _apply_komga_match si matché, sinon None (avec
# candidates rempli en sortie via le paramètre out_candidates si fourni).
def _try_komga_title_match(series_id, series_title, client, out_candidates=None):
    candidates = client.search_series(series_title)
    if not candidates:
        # Le titre local contient souvent un suffixe entre parenthèses/crochets
        # (auteurs, article déplacé pour le tri...) que la recherche Komga ne
        # gère pas toujours bien (ex: "/" entre deux auteurs) -> on retente sans
        stripped_title = _strip_bracketed_suffix(series_title)
        if stripped_title and stripped_title != series_title:
            candidates = client.search_series(stripped_title)

    if out_candidates is not None:
        out_candidates.extend(candidates)

    normalized_title = _normalize_title_for_match(series_title)
    exact_matches = [c for c in candidates if _normalize_title_for_match(c.get('title')) == normalized_title]

    chosen = None
    if len(candidates) == 1:
        chosen = candidates[0]
    elif len(exact_matches) == 1:
        chosen = exact_matches[0]

    if not chosen:
        return None
    return _apply_komga_match(series_id, chosen, client)


@library_bp.route('/api/series/<int:series_id>/komga-enrich', methods=['POST'])
def komga_enrich_series(series_id):
    """(Re)synchronise le match Komga d'une série - plus une récupération de métadonnées
    (Bédéthèque est la seule source de résumé/statut/auteurs, voir _apply_komga_match),
    juste l'identité/lien + la resynchro des komga_book_id par tome.

    Si la série est déjà matchée à une série Komga précise, la resynchro se fait
    directement par son id (simple et fiable). Sinon, une recherche par titre est
    tentée: si elle désigne un unique résultat (ou un résultat dont le titre est une
    correspondance exacte non-ambiguë), il est adopté comme match; sinon la série
    reste "non matchée" et les candidats trouvés sont renvoyés pour un matching manuel.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT title, komga_series_id FROM series WHERE id = ?', (series_id,))
        series_row = cursor.fetchone()
        conn.close()

        if not series_row:
            return jsonify({'success': False, 'error': 'Série introuvable'}), 404

        series_title = series_row['title']
        matched_id = series_row['komga_series_id']

        from blueprints.komga.client import KomgaClient, KomgaError, KomgaSeriesNotFoundError
        try:
            client = KomgaClient()
        except KomgaError as e:
            return jsonify({'success': False, 'error': str(e)}), 400

        try:
            if matched_id:
                try:
                    series_info = client.get_series(matched_id)
                    result = _apply_komga_match(series_id, series_info, client)
                    if result.get('matched_books', 0) == 0:
                        return jsonify({'success': False, 'error': 'Aucun tome local ne correspond aux livres de cette série Komga.', 'match_status': 'unmatched'})
                    result.update({'success': True, 'candidates': []})
                    return jsonify(result)
                except KomgaSeriesNotFoundError:
                    # L’identifiant peut devenir obsolète après une recréation de série
                    # côté Komga : reprendre la recherche par titre au lieu d’abandonner.
                    matched_id = None

            candidates = []
            result = _try_komga_title_match(series_id, series_title, client, out_candidates=candidates)
        except KomgaError as e:
            return jsonify({'success': False, 'error': str(e)}), 400

        if result:
            result.update({'success': True, 'candidates': []})
            return jsonify(result)

        _clear_komga_match(series_id)
        return jsonify({'success': True, 'match_status': 'unmatched', 'candidates': candidates})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def _synthetic_bd_volume_entry(album, **kwargs):
    """Entrée au même format qu'une ligne `volumes` réelle (voir get_series_volumes) pour
    un album Bédéthèque pas encore possédé - filepath/comicinfo vides, seul le titre de
    l'album (s'il y en a un) est passé en 'comicinfo' pour l'affichage ("Tome 4 - Les
    disparus d'Apostrophes !", voir updateVolumeOverrideVisibility/_confirmUploadVolumeSlot
    côté JS, qui savent déjà lire ce champ)."""
    entry = {
        'volume_number': None, 'is_integral': False, 'integral_number': None,
        'is_hs': False, 'hs_number': None, 'is_episode': False, 'episode_number': None,
        'filepath': None,
        'comicinfo': {'title': album.get('title')} if album.get('title') else None,
    }
    entry.update(kwargs)
    return entry


@library_bp.route('/api/series/<int:series_id>/volumes', methods=['GET'])
def get_series_volumes(series_id):
    """Récupère tous les volumes d'une série - tomes déjà possédés (table `volumes`) fusionnés
    avec la liste COMPLÈTE des albums connus de Bédéthèque (series.bedetheque_albums, y
    compris ceux pas encore possédés) quand elle est disponible. "il faudrait que pour les
    volumes tu mettes une dropdown avec les volumes de la série. regarde dans la base de
    données tu auras tous les volumes de la serie" - sans cette fusion, un tome pas encore
    téléchargé n'apparaissait jamais dans le sélecteur de tome (voir
    updateVolumeOverrideVisibility/_confirmUploadVolumeSlot), qui ne listait que ce qui
    était déjà sur disque."""
    conn = get_db_connection()
    cursor = conn.cursor()

    # (volume_number IS NULL) avant volume_number: un tome numéroté a toujours
    # volume_number renseigné, une intégrale/hors-série non ("quand tu ordonnes les
    # volumes par numéro il faudrait que les INT soit à la fin") - sans ce tri, NULL se
    # classe AVANT toute valeur en SQLite par défaut, faisant remonter les
    # intégrales/hors-séries avant même le tome 1. Même correctif déjà appliqué à la
    # requête de la fiche série (voir get_series_detail) mais pas répercuté ici.
    cursor.execute('''
        SELECT * FROM volumes
        WHERE series_id = ?
        ORDER BY part_number, (volume_number IS NULL), volume_number, integral_number, hs_number
    ''', (series_id,))
    owned_volumes = [dict(row) for row in cursor.fetchall()]

    cursor.execute('SELECT bedetheque_albums FROM series WHERE id = ?', (series_id,))
    series_row = cursor.fetchone()
    conn.close()

    bd_albums = json.loads(series_row[0]) if series_row and series_row[0] else []
    if not bd_albums:
        return jsonify(owned_volumes)

    from blueprints.bedetheque.scraper import _index_bedetheque_volumes
    by_number, by_integral, by_hs, by_episode = _index_bedetheque_volumes(bd_albums)

    def _owned_web_url(v):
        try:
            ci = json.loads(v['comicinfo']) if v.get('comicinfo') else {}
        except (TypeError, ValueError):
            ci = {}
        return ci.get('web')

    # "épervier intégrale est mal référencé. et je peux pas le changer" - un numéro seul
    # (integral_number=1) ne suffit plus à identifier UN album précis depuis que
    # _index_bedetheque_volumes ne perd plus les collisions ("INT01TL"/"INT1" partagent
    # toutes deux integral_number=1, voir son commentaire) - candidates peut désormais
    # contenir plusieurs tomes possédés partageant le même (type, numéro). L'URL Bédéthèque
    # de CHAQUE tome déjà possédé (comicinfo.web) les distingue de façon fiable; repli sur
    # le premier trouvé seulement quand aucun ne porte encore d'URL (fichier réel jamais
    # passé par une MAJ métadonnées) - jamais un choix arbitraire entre deux URLs connues
    # et différentes.
    #
    # used_owned_ids: "il y a deux fois le nom du volume affiché" - quand Bédéthèque liste
    # DEUX albums pour le même numéro (réédition, entrée dupliquée côté scrape...) mais
    # qu'un seul tome possédé local n'a pas d'URL enregistrée pour les distinguer, chaque
    # appel de _find_owned pour ce numéro retombait sur le même candidats[0] à chaque fois
    # - le même tome possédé apparaissait alors deux fois dans le sélecteur (une fois par
    # album dupliqué). Un tome possédé déjà retenu pour un album n'est plus proposé à
    # nouveau pour un second album de la même identité (type, numéro) - la boucle des
    # tomes possédés orphelins plus bas continue de rattraper un DEUXIÈME tome RÉEL
    # partageant la même identité (cas différent, volontairement préservé - voir son
    # commentaire "un DOUBLON volontaire").
    used_owned_ids = set()

    def _find_owned(volume_number=None, is_integral=False, integral_number=None, is_hs=False, hs_number=None,
                    is_episode=False, episode_number=None, url=None):
        candidates = [
            v for v in owned_volumes
            if bool(v.get('is_integral')) == is_integral and bool(v.get('is_hs')) == is_hs
            and bool(v.get('is_episode')) == is_episode
            and v.get('volume_number') == volume_number and v.get('integral_number') == integral_number
            and v.get('hs_number') == hs_number and v.get('episode_number') == episode_number
            and v.get('id') not in used_owned_ids
        ]
        if not candidates:
            return None
        chosen = None
        if url:
            for v in candidates:
                if _owned_web_url(v) == url:
                    chosen = v
                    break
        if chosen is None:
            chosen = candidates[0]
        used_owned_ids.add(chosen['id'])
        return chosen

    # Tomes numérotés d'abord (par numéro croissant), intégrales, hors-séries puis
    # épisodes ensuite (même ordre que le tri SQL ci-dessus) - None trié en tête de son
    # propre groupe plutôt que de planter sur une comparaison int/None. Chaque bucket est
    # maintenant une LISTE d'albums (voir _index_bedetheque_volumes) - une entrée du
    # sélecteur par album réel, jamais un seul par numéro.
    # bedetheque_title: le VRAI titre Bédéthèque de cet album, distinct du comicinfo.title
    # du tome possédé (qui peut être erroné/périmé - voir renumber_volume et le cas
    # série #381 "Musiques" mal étiqueté "Chasse & Pêche") - "le dropdown de changer le
    # numéro doit etre le vrai nom des volumes de bedetheque pas les noms des volumes
    # modifié": un tome possédé garde ici quand même son propre comicinfo tel quel (pas
    # réécrit), seul ce champ supplémentaire porte la référence Bédéthèque fiable pour
    # que le sélecteur de renumérotation s'appuie dessus plutôt que sur un titre local
    # potentiellement faux.
    merged = []
    for number, albums in sorted(by_number.items()):
        for album in albums:
            entry = (_find_owned(volume_number=number, url=album.get('url'))
                     or _synthetic_bd_volume_entry(album, volume_number=number))
            entry['bedetheque_title'] = album.get('title')
            merged.append(entry)
    for number, albums in sorted(by_integral.items(), key=lambda kv: (kv[0] is None, kv[0])):
        for album in albums:
            entry = (_find_owned(is_integral=True, integral_number=number, url=album.get('url'))
                     or _synthetic_bd_volume_entry(album, is_integral=True, integral_number=number))
            entry['bedetheque_title'] = album.get('title')
            merged.append(entry)
    for number, albums in sorted(by_hs.items(), key=lambda kv: (kv[0] is None, kv[0])):
        for album in albums:
            entry = (_find_owned(is_hs=True, hs_number=number, url=album.get('url'))
                     or _synthetic_bd_volume_entry(album, is_hs=True, hs_number=number))
            entry['bedetheque_title'] = album.get('title')
            merged.append(entry)
    for number, albums in sorted(by_episode.items(), key=lambda kv: (kv[0] is None, kv[0])):
        for album in albums:
            entry = (_find_owned(is_episode=True, episode_number=number, url=album.get('url'))
                     or _synthetic_bd_volume_entry(album, is_episode=True, episode_number=number))
            entry['bedetheque_title'] = album.get('title')
            merged.append(entry)

    # "après choisir la série sélectionner l'album ça met Tome 1 mais pas de titre juste
    # le numéro" - un album Bédéthèque sans 'number' ET sans préfixe INT/HS/Ép dans son
    # titre (un vrai one-shot, ex: "Le Loup" de Rochette - un seul album sur toute la
    # fiche série) n'est indexé dans AUCUN des 4 buckets ci-dessus (voir
    # _index_bedetheque_volumes) - il tombait donc entièrement hors de cette fusion,
    # jamais matché à bedetheque_title. match_bedetheque_volume comble déjà ce même trou
    # ailleurs ("s'il n'y a qu'un seul album... c'est forcément celui-là", scraper.py) mais
    # ce repli n'était jamais répercuté ici: un one-shot déjà possédé sans ComicInfo Title
    # (placeholder jamais enrichi, ou écriture ratée) n'avait alors STRICTEMENT AUCUNE
    # source de titre dans ce dropdown - juste "Édition unique", sans nom.
    if len(bd_albums) == 1:
        indexed_ids = {id(a) for bucket in (by_number, by_integral, by_hs, by_episode) for albums in bucket.values() for a in albums}
        if id(bd_albums[0]) not in indexed_ids:
            oneshot_album = bd_albums[0]
            entry = _find_owned(url=oneshot_album.get('url')) or _synthetic_bd_volume_entry(oneshot_album)
            entry['bedetheque_title'] = oneshot_album.get('title')
            merged.append(entry)

    # Tomes possédés mais absents de la liste Bédéthèque (pas encore matchée/à jour, ou
    # tome ajouté manuellement) - ne pas les perdre du sélecteur pour autant. Dédupliqué
    # par id de tome (pas par identité number/is_integral/...): un DOUBLON volontaire
    # (deux tomes réels qui partagent le même numéro le temps que l'utilisateur règle une
    # chaîne de renumérotation, voir renumber_volume/CLAUDE.md) partage aussi la même clé
    # d'identité que celui déjà repris par _find_owned ci-dessus - le filtrer par identité
    # aurait fait disparaître silencieusement le second du sélecteur alors qu'il existe
    # bel et bien encore en base.
    for v in owned_volumes:
        if v.get('id') in used_owned_ids:
            continue
        if v.get('is_episode'):
            albums = by_episode.get(v.get('episode_number')) or []
        elif v.get('volume_number') is not None:
            albums = by_number.get(v.get('volume_number')) or []
        elif v.get('is_integral'):
            albums = by_integral.get(v.get('integral_number')) or []
        elif v.get('is_hs'):
            albums = by_hs.get(v.get('hs_number')) or []
        else:
            albums = []
        # Plusieurs albums pour ce même numéro (voir plus haut): préférer celui dont
        # l'URL correspond déjà à ce tome possédé, sinon le premier - un simple
        # affichage de secours, ce tome reste de toute façon listé tel quel.
        own_url = _owned_web_url(v)
        album = next((a for a in albums if a.get('url') == own_url), albums[0] if albums else None)
        if album:
            v['bedetheque_title'] = album.get('title')
        merged.append(v)

    return jsonify(merged)


def trigger_new_series_bedetheque_fetch(series_id, series_title, bedetheque_url):
    """"normalement des qu'on crée une série ça doit être crée [avec ses métadonnées]" -
    récupère la fiche Bédéthèque complète et écrit le ComicInfo de ses tomes (aucun effet
    si la série n'en a encore aucun) en arrière-plan, sans jamais bloquer la réponse HTTP
    de l'appelant. Partagé entre execute_import (une "nouvelle série" créée pendant un
    import, avec un match Bédéthèque déjà choisi côté UI) et POST /api/series/create
    ("creer une nouveau album devrait faire un match bedetheque" - même besoin pour une
    série créée sans aucun fichier, depuis #tracking-edit-modal)."""
    import threading as _threading
    app_obj = current_app._get_current_object()

    def _fetch_and_write(series_id, series_title, bd_url, app):
        try:
            with app.app_context():
                from blueprints.bedetheque.scraper import BedethequeScraper
                from blueprints.bedetheque.routes import _align_title_and_start_metadata_write
                info = BedethequeScraper().get_series_info(bd_url)
                if info:
                    _align_title_and_start_metadata_write(series_id, series_title, info, write_volumes=True)
        except Exception as e:
            print(f"Erreur récupération métadonnées Bedetheque pour la nouvelle série #{series_id}: {e}")

    _threading.Thread(
        target=_fetch_and_write,
        args=(series_id, series_title, bedetheque_url, app_obj),
        daemon=True
    ).start()


@library_bp.route('/api/series/create', methods=['POST'])
def create_series():
    """Crée une série VIDE (dossier + ligne `series`, sans aucun fichier) - "Corriger la
    série / le tome suivis... toujours pas creer une nouvelle série. est-ce que c'est 2
    implementations différentes?": seule execute_import savait jusqu'ici créer une série,
    au moment de déplacer un premier fichier dedans (voir sa branche `is_new_series`) -
    #tracking-edit-modal (corriger un téléchargement encore EN COURS, donc sans aucun
    fichier à déplacer) ne pouvait donc jamais créer de série, seulement en choisir une
    déjà existante. Même dédoublonnage par (library_id, title) qu'execute_import (un
    second appel avec le même titre réutilise la série déjà créée plutôt que d'en insérer
    une deuxième) - une copie DÉLIBÉRÉE de cette petite partie plutôt qu'une factorisation
    avec execute_import: cette dernière reste une des routes les plus critiques/fragiles
    de l'app (transaction ouverte sur toute la boucle d'import), la retoucher pour ce cas
    plus simple n'en vaut pas le risque de régression."""
    data = request.get_json() or {}
    library_id = data.get('library_id')
    title = (data.get('title') or '').strip()
    # "creer une nouveau album devrait faire un match bedetheque" - optionnel: choisi côté
    # UI via la même modale de matching que #select-destination-modal (voir
    # openBedethequeImportModal/getBedethequeMatchForTitle, import.js).
    bedetheque_url = data.get('bedetheque_url')
    if not library_id or not title:
        return jsonify({'success': False, 'error': 'Bibliothèque et titre requis'}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT path FROM libraries WHERE id = ?', (library_id,))
        library_row = cursor.fetchone()
        if not library_row:
            conn.close()
            return jsonify({'success': False, 'error': 'Bibliothèque introuvable'}), 404
        library_path = library_row[0]

        series_title = sanitize_path_component(title, 'Nom de série')
        series_path = resolve_within(os.path.join(library_path, series_title), library_path)

        # "tu as fais de la merde avec la série les mémés" - #Lesmémés / Les Mémés (589)
        # et #Lesmémés - Les Mémés (1084) coexistaient en base, MÊME path disque
        # (/BD/#Lesmémés - Les Mémés), même komga_series_id, 5 volumes strictement
        # identiques dupliqués - un simple changement de séparateur (" / " -> " - ")
        # dans le titre proposé faisait échouer le dédoublonnage par (library_id, title)
        # ci-dessous, créant une DEUXIÈME série vide pour un dossier déjà suivi (589
        # avait tout l'historique EBDZ/Bédéthèque/tome 6 en attente ; 1084 n'avait rien
        # de tout ça). Le titre est une chaîne libre reformulée à chaque scan/import/
        # match Bédéthèque - le `path` sur disque, lui, est LA seule identité stable
        # d'une série (un même dossier n'est jamais "une autre série"). Vérifie donc
        # aussi par path, en priorité sur le titre, avant de créer quoi que ce soit.
        cursor.execute(
            'SELECT id FROM series WHERE library_id = ? AND (title = ? OR path = ?)',
            (library_id, series_title, series_path),
        )
        existing = cursor.fetchone()
        if existing:
            conn.close()
            return jsonify({'success': True, 'series_id': existing[0], 'title': series_title})

        os.makedirs(series_path, exist_ok=True)
        cursor.execute('''
            INSERT INTO series (library_id, title, path, total_volumes, missing_volumes, has_parts)
            VALUES (?, ?, ?, 0, '[]', 0)
        ''', (library_id, series_title, series_path))
        series_id = cursor.lastrowid
        if bedetheque_url:
            cursor.execute('UPDATE series SET bedetheque_url = ? WHERE id = ?', (bedetheque_url, series_id))
        conn.commit()
        conn.close()
        if bedetheque_url:
            trigger_new_series_bedetheque_fetch(series_id, series_title, bedetheque_url)
        return jsonify({'success': True, 'series_id': series_id, 'title': series_title})
    except UnsafePathError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@library_bp.route('/api/library/<int:library_id>/series')
def get_library_series(library_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Récupérer toutes les séries avec les données Bédéthèque, EBDZ et Komga.
        # volumes_without_metadata/oneshot_is_integral: "should be in the database. no
        # need of complicated queries. just list the database" - colonnes stockées,
        # recalculées par update_series_stats (scanner.py) à chaque scan/import/écriture
        # ComicInfo, plutôt que deux sous-requêtes corrélées sur `volumes` exécutées ici
        # à chaque chargement (SCAN complet de la table par série, sans index).
        # year: "check why certain série don't have a year despite having one in their
        # série details" -> "i want one single table in the database with the
        # information. i don't want overcomplicated calculation" - la fiche série
        # affiche déjà une année via bd.year_start (Bédéthèque) même quand local_year
        # (dérivé uniquement du ComicInfo.xml LOCAL, jamais écrit tant qu'aucune "MAJ
        # métadonnées" n'a tourné) est vide. Un premier essai calculait ce repli côté
        # client (s._displayYear, library.js) - rejeté: un JS qui recombine 3 colonnes à
        # chaque chargement est exactement le genre de "calcul compliqué" à éviter, et ça
        # a cassé le tri/filtre (bedetheque_year_start/manual_year_start sont des
        # INTEGER, local_year un TEXT - un Set mélangeant nombres et chaînes fait planter
        # .localeCompare, "a.localeCompare is not a function"). Résolu ici en UNE seule
        # expression SQL (COALESCE, pas une vraie table séparée mais une seule colonne
        # DÉJÀ résolue dans la réponse) - CAST en TEXT pour que le JSON renvoyé soit
        # toujours du même type quelle que soit la source retenue, le frontend n'a plus
        # aucun repli à recalculer.
        cursor.execute('''
            SELECT s.id, s.title, s.path, s.total_volumes, s.missing_volumes, s.has_parts, s.last_scanned,
                   s.ebdz_volumes_count, s.ebdz_missing_volumes, s.ebdz_checked_at, s.ebdz_thread_url,
                   s.ebdz_thread_id, s.ebdz_matched_title, s.ebdz_match_status,
                   s.komga_series_id, s.komga_match_status, s.komga_matched_title, s.komga_url,
                   s.komga_cover_path,
                   s.bedetheque_url, s.bedetheque_cover_path, s.bedetheque_total_volumes, s.bedetheque_status,
                   s.bedetheque_editeurs,
                   s.local_cover_path, s.local_summary, s.local_genre, s.local_author, s.is_oneshot,
                   CAST(COALESCE(s.manual_year_start, s.bedetheque_year_start, s.local_year) AS TEXT) AS year,
                   s.volumes_without_metadata, s.oneshot_is_integral,
                   s.tags,
                   s.manual_complete_override, s.bedetheque_complete, s.bedetheque_complete_reason,
                   (SELECT COUNT(*) FROM volumes v WHERE v.series_id = s.id AND v.filepath IS NOT NULL
                     AND v.is_integral = 0 AND v.is_hs = 0 AND v.is_episode = 0 AND v.is_special = 0) AS owned_tomes,
                   (SELECT COUNT(*) FROM volumes v WHERE v.series_id = s.id AND v.filepath IS NOT NULL AND v.is_integral = 1) AS owned_integrals,
                   (SELECT COUNT(*) FROM volumes v WHERE v.series_id = s.id AND v.filepath IS NOT NULL AND v.is_hs = 1) AS owned_hs,
                   (SELECT COUNT(*) FROM volumes v WHERE v.series_id = s.id AND v.filepath IS NOT NULL AND v.is_episode = 1) AS owned_episodes,
                   (SELECT COUNT(*) FROM volumes v WHERE v.series_id = s.id AND v.filepath IS NOT NULL
                     AND v.is_special = 1 AND v.is_integral = 0 AND v.is_hs = 0 AND v.is_episode = 0) AS owned_specials,
                   u.name AS universe_name
            FROM series s
            LEFT JOIN universes u ON u.id = s.universe_id
            WHERE s.library_id = ?
            ORDER BY s.title
        ''', (library_id,))

        series_list = []
        for row in cursor.fetchall():
            try:
                tags = json.loads(row['tags']) if row['tags'] else []
            except (ValueError, TypeError):
                tags = []

            series_list.append({
                'id': row['id'],
                'title': row['title'],
                'path': row['path'],
                'total_volumes': row['total_volumes'],
                'missing_volumes': json.loads(row['missing_volumes']) if row['missing_volumes'] else [],
                'has_parts': bool(row['has_parts']),
                'last_scanned': row['last_scanned'],
                'bedetheque_status': row['bedetheque_status'],
                'bedetheque_total_volumes': row['bedetheque_total_volumes'],
                'bedetheque_url': row['bedetheque_url'],
                'bedetheque_cover_path': row['bedetheque_cover_path'],
                'ebdz_volumes_count': row['ebdz_volumes_count'],
                'ebdz_missing_volumes': json.loads(row['ebdz_missing_volumes']) if row['ebdz_missing_volumes'] else [],
                'ebdz_checked_at': row['ebdz_checked_at'],
                'ebdz_thread_url': row['ebdz_thread_url'],
                'ebdz_thread_id': row['ebdz_thread_id'],
                'ebdz_matched_title': row['ebdz_matched_title'],
                'ebdz_match_status': row['ebdz_match_status'],
                'komga_series_id': row['komga_series_id'],
                'komga_match_status': row['komga_match_status'],
                'komga_matched_title': row['komga_matched_title'],
                'komga_url': row['komga_url'],
                'komga_cover_path': row['komga_cover_path'],
                'bedetheque_url': row['bedetheque_url'],
                'bedetheque_cover_path': row['bedetheque_cover_path'],
                'bedetheque_total_volumes': row['bedetheque_total_volumes'],
                'bedetheque_editeurs': row['bedetheque_editeurs'],
                'local_cover_path': row['local_cover_path'],
                'local_summary': row['local_summary'],
                'local_genre': row['local_genre'],
                'local_author': row['local_author'],
                'year': row['year'],
                'is_oneshot': bool(row['is_oneshot']),
                'oneshot_is_integral': bool(row['oneshot_is_integral']),
                'volumes_without_metadata': row['volumes_without_metadata'],
                'tags': tags,
                'manual_complete_override': bool(row['manual_complete_override']),
                'bedetheque_complete': row['bedetheque_complete'] if row['bedetheque_complete'] is None else bool(row['bedetheque_complete']),
                'bedetheque_complete_reason': row['bedetheque_complete_reason'],
                'owned_tomes': row['owned_tomes'],
                'owned_integrals': row['owned_integrals'],
                'owned_hs': row['owned_hs'],
                'owned_episodes': row['owned_episodes'],
                'owned_specials': row['owned_specials'],
                'universe_name': row['universe_name']
            })

        conn.close()
        return jsonify(series_list)

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@library_bp.route('/api/series/<int:series_id>', methods=['DELETE'])
def delete_series(series_id):
    """Supprime définitivement une série: supprime d'abord son dossier du disque, puis
    retire ses lignes de la base (irréversible). Le chemin est vérifié comme étant bien
    contenu dans le répertoire de la bibliothèque à laquelle appartient la série (via
    resolve_within), pour ne jamais supprimer un dossier en dehors de ce périmètre.
    L'ordre (disque avant base) garantit qu'un échec de suppression disque n'efface pas
    la série de l'app en laissant les fichiers orphelins sans possibilité de les revoir."""
    from blueprints.library.action_history import log_action

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT s.title, s.path, s.library_id, l.path as library_path
            FROM series s JOIN libraries l ON s.library_id = l.id
            WHERE s.id = ?
        ''', (series_id,))
        row = cursor.fetchone()
        conn.close()

        if not row:
            return jsonify({'success': False, 'error': 'Série introuvable'}), 404

        series_title = row['title']
        series_path = row['path']
        library_id = row['library_id']
        library_path = row['library_path']

        conn = get_db_connection()
        volume_count = conn.execute('SELECT COUNT(*) FROM volumes WHERE series_id = ?', (series_id,)).fetchone()[0]
        conn.close()

        if series_path and os.path.isdir(series_path):
            try:
                safe_path = resolve_within(series_path, library_path)
            except UnsafePathError as e:
                log_action('delete', series_id, series_title, series_path, success=False, error=str(e))
                return jsonify({'success': False, 'error': f'Chemin de série invalide: {e}'}), 400
            shutil.rmtree(safe_path)

        conn = get_db_connection()
        conn.execute('DELETE FROM volumes WHERE series_id = ?', (series_id,))
        conn.execute('DELETE FROM series WHERE id = ?', (series_id,))
        conn.commit()
        conn.close()

        log_action('delete', series_id, series_title, f"{volume_count} tome(s) · {series_path}")

        return jsonify({'success': True, 'library_id': library_id})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@library_bp.route('/api/series/<int:series_id>/merge', methods=['POST'])
def merge_series(series_id):
    """Fusionne la série source dans une autre (target_series_id dans le corps JSON):
    déplace tous ses fichiers de tomes vers le dossier de la série cible, rattache leurs
    lignes en base à la cible, puis supprime la fiche de la série source (la cible garde
    ses propres métadonnées/matchings EBDZ/Komga/Bédéthèque). Refuse en bloc,
    avant tout déplacement, au moindre conflit de nom de fichier dans la cible. Le dossier
    source n'est supprimé ensuite que s'il ne reste aucun fichier de BD dedans (fichiers
    annexes type couvertures supprimés avec lui) - sinon il est laissé tel quel."""
    data = request.get_json() or {}
    target_series_id = data.get('target_series_id')

    if not target_series_id:
        return jsonify({'success': False, 'error': 'target_series_id requis'}), 400
    if int(target_series_id) == series_id:
        return jsonify({'success': False, 'error': 'La série cible doit être différente de la série source'}), 400
    target_series_id = int(target_series_id)

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT s.id, s.title, s.path, s.library_id, l.path AS library_path
            FROM series s JOIN libraries l ON s.library_id = l.id
            WHERE s.id IN (?, ?)
        ''', (series_id, target_series_id))
        rows = {row['id']: row for row in cursor.fetchall()}
        source = rows.get(series_id)
        target = rows.get(target_series_id)

        if not source or not target:
            conn.close()
            return jsonify({'success': False, 'error': 'Série source ou cible introuvable'}), 404

        try:
            source_dir = resolve_within(source['path'] or os.path.join(source['library_path'], source['title']),
                                        source['library_path'])
            target_dir = resolve_within(target['path'] or os.path.join(target['library_path'], target['title']),
                                        target['library_path'])
        except UnsafePathError as e:
            conn.close()
            return jsonify({'success': False, 'error': f'Chemin de série invalide: {e}'}), 400

        # "je ne peux pas fusionner les 2" - une série ajoutée depuis Bédéthèque sans
        # aucun fichier encore possédé (voir add_series_from_bedetheque, "SANS créer son
        # dossier physique - créé plus tard, au premier téléchargement/import réel") n'a
        # jamais eu de dossier créé sur le disque tant qu'aucun tome n'a été importé -
        # une cible parfaitement valide pour une fusion (on est justement en train d'y
        # déplacer des fichiers), pas une erreur. Créé ici au lieu d'échouer.
        if not os.path.isdir(target_dir):
            try:
                os.makedirs(target_dir, exist_ok=True)
            except OSError as e:
                conn.close()
                return jsonify({'success': False, 'error': f"Impossible de créer le dossier de la série cible: {e}"}), 500

        cursor.execute('''
            SELECT id, filename, filepath, volume_number, is_integral, integral_number, is_hs, hs_number,
                   is_episode, episode_number
            FROM volumes WHERE series_id = ?
        ''', (series_id,))
        volumes = cursor.fetchall()

        # Les tomes "placeholder" (ajoutés depuis Bédéthèque, sans fichier - filepath
        # NULL, voir add_series_from_bedetheque) n'ont ni filename ni fichier disque:
        # aucun conflit de nom possible pour eux, on ne les inclut pas dans cette
        # vérification (os.path.join(..., None) lèverait de toute façon une TypeError)
        conflicts = [v['filename'] for v in volumes
                     if v['filepath'] and os.path.exists(os.path.join(target_dir, v['filename']))]
        if conflicts:
            conn.close()
            return jsonify({
                'success': False,
                'error': 'Fichier(s) du même nom déjà présent(s) dans la série cible: ' + ', '.join(conflicts)
            }), 409

        # "serie 775 ca a creer d'autres tomes duplique" - avant ce correctif, TOUS les
        # tomes de la source étaient rattachés à la cible sans jamais vérifier qu'un même
        # tome (même volume_number, ou même intégrale/hors-série) n'existait déjà côté
        # cible: un placeholder Bédéthèque (filepath NULL) pour le tome 1 côté cible et un
        # tome 1 bien réel côté source (deux séries autrefois distinctes, fusionnées après
        # coup) finissaient tous les deux en base au lieu que le second prenne la place du
        # premier - doublon silencieux à chaque tome partagé par les deux séries.
        cursor.execute('''
            SELECT id, filename, filepath, volume_number, is_integral, integral_number, is_hs, hs_number,
                   is_episode, episode_number
            FROM volumes WHERE series_id = ?
        ''', (target_series_id,))
        target_volumes = cursor.fetchall()

        def _volume_identity(v):
            if v['is_episode']:
                return ('ep', v['episode_number'])
            if v['volume_number'] is not None:
                return ('vol', v['volume_number'])
            if v['is_integral']:
                return ('int', v['integral_number'])
            if v['is_hs']:
                return ('hs', v['hs_number'])
            return None  # one-shot informe ou album non classifié - pas d'identité fiable, jamais dédupliqué ici

        target_by_identity = {}
        for tv in target_volumes:
            key = _volume_identity(tv)
            if key is not None:
                target_by_identity.setdefault(key, []).append(tv)

        # Conflit réel seulement si LES DEUX côtés ont un vrai fichier pour le même tome -
        # un placeholder cède simplement la place (voir la boucle plus bas), rien à
        # bloquer dans ce cas.
        identity_conflicts = [
            v['filename'] for v in volumes
            if v['filepath'] and _volume_identity(v) is not None
            and any(m['filepath'] for m in target_by_identity.get(_volume_identity(v), []))
        ]
        if identity_conflicts:
            conn.close()
            return jsonify({
                'success': False,
                'error': 'Ce(s) tome(s) existent déjà (fichier réel) dans la série cible: ' + ', '.join(identity_conflicts)
            }), 409

        # Déplacement fichier par fichier, avec commit après chacun pour que la base
        # reste le reflet du disque même en cas d'échec en cours de route
        moved = 0
        for v in volumes:
            key = _volume_identity(v)
            matches = target_by_identity.get(key, []) if key is not None else []
            target_real_match = next((m for m in matches if m['filepath']), None)
            target_placeholder_match = next((m for m in matches if not m['filepath']), None)

            if v['filepath']:
                # Déjà refusé plus haut si target_real_match existe aussi - seul un
                # placeholder cible peut encore être présent ici, il cède la place.
                if target_placeholder_match:
                    cursor.execute('DELETE FROM volumes WHERE id = ?', (target_placeholder_match['id'],))
                new_path = os.path.join(target_dir, v['filename'])
                shutil.move(v['filepath'], new_path)
                cursor.execute('UPDATE volumes SET series_id = ?, filepath = ? WHERE id = ?',
                               (target_series_id, new_path, v['id']))
            elif target_real_match or target_placeholder_match:
                # Placeholder source pour un tome déjà représenté côté cible (réel ou
                # placeholder) - rien à apporter en plus, retiré plutôt que dupliqué.
                cursor.execute('DELETE FROM volumes WHERE id = ?', (v['id'],))
                conn.commit()
                continue
            else:
                # Placeholder: juste rattaché à la cible, aucun déplacement disque, pas
                # de filepath fabriqué (resterait NULL, toujours "manquant")
                cursor.execute('UPDATE volumes SET series_id = ? WHERE id = ?',
                               (target_series_id, v['id']))
            conn.commit()
            moved += 1

        cursor.execute('DELETE FROM series WHERE id = ?', (series_id,))
        # Une cible marquée one-shot qui se retrouve avec plusieurs tomes ne l'est plus
        cursor.execute('''
            UPDATE series SET is_oneshot = 0
            WHERE id = ? AND (SELECT COUNT(*) FROM volumes WHERE series_id = ?) > 1
        ''', (target_series_id, target_series_id))
        conn.commit()
        conn.close()

        # Supprime le dossier source seulement s'il ne contient plus aucun fichier de BD
        # (un fichier non scanné qui traînerait là ne doit jamais partir avec le dossier)
        source_dir_removed = False
        if os.path.isdir(source_dir) and source_dir != target_dir:
            comic_exts = {'.cbz', '.cbr', '.zip', '.rar', '.pdf'}
            leftover_comics = [
                f for _root, _dirs, files in os.walk(source_dir) for f in files
                if os.path.splitext(f)[1].lower() in comic_exts
            ]
            if not leftover_comics:
                shutil.rmtree(source_dir)
                source_dir_removed = True

        scanner = LibraryScanner()
        scanner.update_series_stats(target_series_id)

        from blueprints.komga.client import trigger_scan_async
        trigger_scan_async()

        # "il faudrait que dans l'historique on ai toutes les actions de l'utilisateur" -
        # une fusion déplace/supprime des fichiers et fait disparaître une série entière,
        # ça méritait déjà d'être journalisé (constaté par son absence lors de
        # l'investigation des doublons de la série 775 - non fautive ici, mais la
        # fusion n'aurait de toute façon laissé aucune trace si elle l'avait été).
        # Journalisée sur la série CIBLE (celle qui survit, id encore valide après coup) -
        # la source, elle, vient d'être supprimée de la table `series`.
        from blueprints.library.action_history import log_action
        log_action('merge', target_series_id, target['title'],
                    f"Fusion de « {source['title']} » ({moved} tome(s) déplacé(s))")

        return jsonify({
            'success': True,
            'moved': moved,
            'target_series_id': target_series_id,
            'target_title': target['title'],
            'source_dir_removed': source_dir_removed
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


def _set_series_universe(conn, series_id, universe_id):
    """Assign a series to an existing universe, or remove its assignment.

    Keep ``series.universe_id`` and the optional Bédéthèque-backed
    ``universe_series`` membership in sync. The latter is only possible when the
    local series has a stable Bédéthèque URL.
    """
    cursor = conn.cursor()
    cursor.execute('SELECT title, bedetheque_url FROM series WHERE id = ?', (series_id,))
    series_row = cursor.fetchone()
    if not series_row:
        raise ValueError('Série introuvable')

    if universe_id is not None:
        cursor.execute('SELECT id FROM universes WHERE id = ?', (universe_id,))
        if not cursor.fetchone():
            raise ValueError('Univers introuvable')

    title, bedetheque_url = series_row[0], series_row[1]
    cursor.execute('UPDATE universe_series SET series_id = NULL WHERE series_id = ?', (series_id,))
    cursor.execute('UPDATE series SET universe_id = ? WHERE id = ?', (universe_id, series_id))

    if universe_id is not None and bedetheque_url:
        cursor.execute('''
            INSERT INTO universe_series (universe_id, bedetheque_url, title, series_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(bedetheque_url) DO UPDATE SET
                universe_id = excluded.universe_id,
                title = excluded.title,
                series_id = excluded.series_id
        ''', (universe_id, bedetheque_url, title, series_id))


@library_bp.route('/api/series/<int:series_id>')
def get_series_details(series_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Récupérer les infos de la série avec les données Bédéthèque (l.id/l.name aliasés
        # car "series" a aussi une colonne "id"/pas de "name": sans alias, l'accès par nom
        # de colonne sur la ligne serait ambigu)
        cursor.execute('''
            SELECT s.id AS id, s.title, s.path, s.total_volumes, s.missing_volumes, s.has_parts,
                   l.id AS library_id, l.name AS library_name,
                   s.is_oneshot,
                   s.ebdz_volumes_count, s.ebdz_missing_volumes, s.ebdz_checked_at, s.ebdz_thread_url,
                   s.ebdz_thread_id, s.ebdz_matched_title, s.ebdz_match_status,
                   s.komga_series_id, s.komga_match_status, s.komga_matched_title, s.komga_url,
                   s.komga_cover_path, s.komga_checked_at, s.local_cover_path, s.local_summary, s.local_genre,
                   s.local_author, s.local_year, s.bedetheque_url,
                   s.bedetheque_cover_path, s.bedetheque_description, s.bedetheque_genre,
                   s.bedetheque_status, s.bedetheque_total_volumes, s.bedetheque_scenaristes,
                   s.bedetheque_dessinateurs, s.bedetheque_editeurs, s.bedetheque_author_links,
                   s.bedetheque_read_also,
                   s.bedetheque_year_start, s.bedetheque_year_end,
                   s.manual_summary, s.manual_genre, s.manual_status, s.manual_author,
                   s.manual_year_start, s.manual_year_end, s.manual_complete_override,
                   s.universe_id, u.name AS universe_name,
                   s.bedetheque_complete, s.bedetheque_complete_reason,
                   mm.enabled AS monitor_enabled, mm.auto_download_enabled AS monitor_auto_download
                   FROM series s
                   JOIN libraries l ON s.library_id = l.id
                   LEFT JOIN missing_volume_monitor mm ON mm.series_id = s.id
                   LEFT JOIN universes u ON u.id = s.universe_id
            WHERE s.id = ?
        ''', (series_id,))

        series_row = cursor.fetchone()

        if not series_row:
            conn.close()
            return jsonify({'error': 'Série introuvable'}), 404

        missing_volumes = json.loads(series_row['missing_volumes']) if series_row['missing_volumes'] else []
        ebdz_missing_volumes = json.loads(series_row['ebdz_missing_volumes']) if series_row['ebdz_missing_volumes'] else []

        # Récupérer tous les volumes
        cursor.execute('''
            SELECT id, part_number, part_name, volume_number, filename, filepath,
                   author, year, resolution, release_group, file_size, page_count, format, comicinfo, cover_path,
                   komga_book_url, is_integral, integral_number, is_hs, hs_number, is_episode, episode_number,
                   is_special, special_label
            FROM volumes
            WHERE series_id = ?
            ORDER BY part_number, (volume_number IS NULL), volume_number, integral_number, filename
        ''', (series_id,))

        volumes = []
        for vol in cursor.fetchall():
            volumes.append({
                'id': vol['id'],
                'part_number': vol['part_number'],
                'part_name': vol['part_name'],
                'volume_number': vol['volume_number'],
                'filename': vol['filename'],
                'filepath': vol['filepath'],
                'author': vol['author'],
                'year': vol['year'],
                'resolution': vol['resolution'],
                # "dans les pages one-shot je n'ai pas l'information du releaser et de la
                # qualité" / "pareil pour le tableau de série" - release_group était déjà
                # en base (voir LibraryScanner.parse_filename) mais jamais renvoyé par
                # cette route, donc invisible côté frontend quel que soit l'écran.
                'release_group': vol['release_group'],
                'file_size': vol['file_size'],
                'page_count': vol['page_count'],
                'format': vol['format'],
                'comicinfo': json.loads(vol['comicinfo']) if vol['comicinfo'] else {},
                'cover_path': vol['cover_path'],
                'komga_book_url': vol['komga_book_url'],
                'is_integral': bool(vol['is_integral']),
                'integral_number': vol['integral_number'],
                'is_hs': bool(vol['is_hs']),
                'hs_number': vol['hs_number'],
                'is_episode': bool(vol['is_episode']),
                'episode_number': vol['episode_number'],
                # "il y a un volume COF. ce n'est pas un volume, c'est un spécial" -
                # voir _parse_special_prefix (blueprints/bedetheque/scraper.py).
                'is_special': bool(vol['is_special']),
                'special_label': vol['special_label']
            })

        # Les recommandations « À lire aussi » sont mises en cache lors du dernier
        # scraping Bédéthèque, mais l'état « déjà en bibliothèque » se vérifie à chaque
        # affichage car une série peut avoir été ajoutée depuis.
        read_also = []
        try:
            read_also = json.loads(series_row['bedetheque_read_also'] or '[]')
        except (TypeError, ValueError):
            read_also = []
        if isinstance(read_also, list) and read_also:
            read_also = [item for item in read_also if isinstance(item, dict) and item.get('url')]
            urls = [item['url'].rstrip('/') for item in read_also]
            placeholders = ','.join('?' * len(urls))
            owned_rows = cursor.execute(
                f'SELECT id, bedetheque_url FROM series WHERE rtrim(bedetheque_url, \'/\') IN ({placeholders})',
                urls
            ).fetchall()
            owned_by_url = {((row['bedetheque_url'] or '').rstrip('/')): row['id'] for row in owned_rows}
            for item in read_also:
                item['series_id'] = owned_by_url.get(item['url'].rstrip('/'))

        cursor.execute('SELECT id, name FROM universes ORDER BY name COLLATE NOCASE')
        universes = [{'id': row['id'], 'name': row['name']} for row in cursor.fetchall()]
        conn.close()

        return jsonify({
            'id': series_row['id'],
            'title': series_row['title'],
            'path': series_row['path'],
            'total_volumes': series_row['total_volumes'],
            'missing_volumes': missing_volumes,
            'has_parts': bool(series_row['has_parts']),
            'is_oneshot': bool(series_row['is_oneshot']),
            'manual_complete_override': bool(series_row['manual_complete_override']),
            # "now we can validate if a serie is complete or no" - calculé par
            # update_series_stats (scanner.py), même source que celle utilisée côté liste
            # de bibliothèque (_seriesBadgeInfo, library.js) - la fiche série ne doit
            # jamais refaire son propre calcul divergent ("third implementation... ask
            # for unification").
            'bedetheque_complete': series_row['bedetheque_complete'] if series_row['bedetheque_complete'] is None else bool(series_row['bedetheque_complete']),
            'bedetheque_complete_reason': series_row['bedetheque_complete_reason'],
            'library': {
                'id': series_row['library_id'],
                'name': series_row['library_name']
            },
            'ebdz': {
                'volumes_count': series_row['ebdz_volumes_count'],
                'missing_volumes': ebdz_missing_volumes,
                'checked_at': series_row['ebdz_checked_at'],
                'thread_url': series_row['ebdz_thread_url'],
                'thread_id': series_row['ebdz_thread_id'],
                'matched_title': series_row['ebdz_matched_title'],
                'match_status': series_row['ebdz_match_status']
            },
            'komga': {
                'series_id': series_row['komga_series_id'],
                'match_status': series_row['komga_match_status'],
                'matched_title': series_row['komga_matched_title'],
                'url': series_row['komga_url'],
                'cover_path': series_row['komga_cover_path'],
                'checked_at': series_row['komga_checked_at']
            },
            'local_cover_path': series_row['local_cover_path'],
            'local_summary': series_row['local_summary'],
            'local_genre': series_row['local_genre'],
            'local_author': series_row['local_author'],
            'local_year': series_row['local_year'],
            'bedetheque': {
                'url': series_row['bedetheque_url'],
                'cover_path': series_row['bedetheque_cover_path'],
                'description': series_row['bedetheque_description'],
                'genre': series_row['bedetheque_genre'],
                'status': series_row['bedetheque_status'],
                'total_volumes': series_row['bedetheque_total_volumes'],
                'scenaristes': series_row['bedetheque_scenaristes'],
                'dessinateurs': series_row['bedetheque_dessinateurs'],
                'editeurs': series_row['bedetheque_editeurs'],
                # {nom: url fiche auteur} - item #25 improvement.txt, permet à la fiche
                # série de rendre scenaristes/dessinateurs cliquables (voir library.js,
                # buildAuthorAlbumsLink) uniquement pour les noms dont on a bien l'URL.
                'author_links': json.loads(series_row['bedetheque_author_links']) if series_row['bedetheque_author_links'] else {},
                'read_also': read_also,
                'year_start': series_row['bedetheque_year_start'],
                'year_end': series_row['bedetheque_year_end']
            },
            'manual_summary': series_row['manual_summary'],
            'manual_genre': series_row['manual_genre'],
            'manual_status': series_row['manual_status'],
            'manual_author': series_row['manual_author'],
            'manual_year_start': series_row['manual_year_start'],
            'manual_year_end': series_row['manual_year_end'],
            'universe': {
                'id': series_row['universe_id'],
                'name': series_row['universe_name']
            } if series_row['universe_id'] else None,
            'universes': universes,
            'monitored': bool(series_row['monitor_enabled']),
            'auto_download_enabled': bool(series_row['monitor_auto_download']),
            'volumes': volumes
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@library_bp.route('/api/series/<int:series_id>/manual-metadata', methods=['PUT'])
def update_series_manual_metadata(series_id):
    """Édite manuellement le titre + les métadonnées de référence d'une série
    (manual_summary/genre/status/author/year_start/year_end - voir
    _add_series_manual_metadata_columns pour pourquoi ce sont des colonnes séparées de
    local_*/bedetheque_*, jamais recalculées par un scan ou une MAJ Bédéthèque).

    Contrairement à un match/MAJ Bédéthèque, ceci ne touche à aucun ComicInfo.xml de
    tome (résumé/genre/statut/auteur/année de série n'ont pas d'équivalent qu'on puisse
    pousser sans risque dans CHAQUE tome - Résumé et Année en particulier sont propres à
    chaque album, les y écraser avec la valeur série corromprait des données correctes -
    décision produit, voir aussi update_volume_manual_metadata pour l'édition par tome).
    Déclenche quand même un rescan Komga en best-effort: la fiche série affichée y a
    changé même si aucun fichier n'a bougé. Body JSON, tous les champs optionnels
    (absent = inchangé, chaîne vide = effacé):
    {title, summary, genre, status, author, year_start, year_end, universe_id}
    (universe_id: null ou omis/absent = inchangé, "" ou null explicite = retire la
    série de son univers, entier = doit référencer un univers existant)"""
    data = request.get_json(silent=True) or {}

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM series WHERE id = ?', (series_id,))
        if not cursor.fetchone():
            conn.close()
            return jsonify({'success': False, 'error': 'Série introuvable'}), 404

        updates = []
        params = []

        if 'title' in data:
            title = (data.get('title') or '').strip()
            if not title:
                conn.close()
                return jsonify({'success': False, 'error': 'Le titre ne peut pas être vide'}), 400
            updates.append('title = ?')
            params.append(title)

        field_map = {
            'summary': 'manual_summary', 'genre': 'manual_genre', 'status': 'manual_status',
            'author': 'manual_author', 'year_start': 'manual_year_start', 'year_end': 'manual_year_end',
        }
        for body_key, column in field_map.items():
            if body_key in data:
                value = data.get(body_key)
                if isinstance(value, str):
                    value = value.strip() or None
                updates.append(f'{column} = ?')
                params.append(value)

        if updates:
            params.append(series_id)
            cursor.execute(f"UPDATE series SET {', '.join(updates)} WHERE id = ?", params)
            conn.commit()

        folder_result = None
        if 'universe_id' in data:
            raw_universe_id = data.get('universe_id')
            universe_id = int(raw_universe_id) if raw_universe_id not in (None, '') else None
            try:
                _set_series_universe(conn, series_id, universe_id)
                conn.commit()
                updates.append('universe_id')
            except ValueError as e:
                conn.close()
                return jsonify({'success': False, 'error': str(e)}), 400

            # "assigner un univers via _set_series_universe devrait déplacer les
            # fichiers. Pourquoi c'est pas?" - INCOHÉRENCE CORRIGÉE (2026-09-03):
            # _set_series_universe() ne fait QUE poser series.universe_id, jamais de
            # déplacement disque - alors que le matching Bédéthèque
            # (_align_title_and_start_metadata_write, blueprints/bedetheque/routes.py)
            # DÉPLACE bien automatiquement le dossier vers <univers>/<série> à chaque
            # changement d'univers, via le même _rename_series_folder que le bouton
            # "Renommer la série". Deux points d'entrée qui posent universe_id, un
            # seul qui en tirait les conséquences sur le disque - repris ici à
            # l'identique (même fonction, même signature, même best-effort: un
            # renommage échoué ne fait jamais échouer l'assignation d'univers déjà
            # commitée juste au-dessus).
            try:
                from blueprints.library.routes import _fetch_series_for_rename, _rename_series_folder, _log_rename_action
                from blueprints.settings.rename_config_store import load_rename_config
                series_for_rename = _fetch_series_for_rename(cursor, series_id)
                if series_for_rename and series_for_rename['path']:
                    rename_cfg = load_rename_config()
                    folder_result = _rename_series_folder(
                        conn, series_id, series_for_rename['path'], series_for_rename['title'],
                        series_for_rename['library_path'], {}, rename_cfg['series_template'],
                        universe_name=series_for_rename['universe_name']
                    )
                    if folder_result and folder_result.get('success') and folder_result.get('changed'):
                        _log_rename_action(series_id, series_for_rename['title'], [], folder_result)
                    elif not (folder_result and folder_result.get('success')):
                        current_app.logger.warning(
                            f"Renommage automatique du dossier échoué pour la série #{series_id} "
                            f"après changement d'univers: {folder_result}"
                        )
            except Exception as e:
                current_app.logger.warning(
                    f"Renommage automatique du dossier échoué pour la série #{series_id} "
                    f"après changement d'univers: {e}"
                )

        conn.close()

        if updates:
            from blueprints.komga.client import trigger_scan_async
            trigger_scan_async()

        return jsonify({'success': True})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@library_bp.route('/api/volumes/<int:volume_id>/refresh', methods=['POST'])
def refresh_volume(volume_id):
    """Actualise taille/nombre de pages d'UN tome directement depuis le fichier sur
    disque, sans repasser par un scan complet de la série (scan_single_series) -
    "seuls #16 indique size 1B. ajoute un bouton actualiser dans les actions des
    volumes": un fichier corrigé/re-téléchargé après coup peut laisser volumes.file_size
    désynchronisé de la réalité tant qu'aucun scan de la série n'a eu lieu; ce bouton
    corrige ça pour CE seul tome, sans attendre/déclencher un rescan de toute la série."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT filepath, format FROM volumes WHERE id = ?', (volume_id,))
        volume = cursor.fetchone()

        if not volume:
            conn.close()
            return jsonify({'success': False, 'error': 'Tome introuvable'}), 404

        filepath = volume['filepath']
        if not filepath or not os.path.exists(filepath):
            conn.close()
            return jsonify({'success': False, 'error': 'Fichier introuvable sur le disque'}), 404

        file_size = os.path.getsize(filepath)
        format_type = (volume['format'] or os.path.splitext(filepath)[1].lstrip('.')).lower()

        from .scanner import LibraryScanner
        page_count = LibraryScanner().get_page_count(filepath, format_type)

        cursor.execute('UPDATE volumes SET file_size = ?, page_count = ? WHERE id = ?',
                       (file_size, page_count, volume_id))
        conn.commit()
        conn.close()

        return jsonify({'success': True, 'file_size': file_size, 'page_count': page_count})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@library_bp.route('/api/volumes/<int:volume_id>/download', methods=['GET'])
def download_volume(volume_id):
    """Télécharge le fichier d'UN tome tel quel ("ajoute une option dans la molette pour
    telecharger le fichier pour le volume") - sert le fichier depuis son emplacement réel
    sur disque (jamais un chemin fourni par le client), avec son nom de fichier d'origine
    comme nom de téléchargement."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT filename, filepath FROM volumes WHERE id = ?', (volume_id,))
    volume = cursor.fetchone()
    conn.close()

    if not volume or not volume['filepath']:
        return jsonify({'success': False, 'error': 'Tome introuvable'}), 404
    if not os.path.exists(volume['filepath']):
        return jsonify({'success': False, 'error': 'Fichier introuvable sur le disque'}), 404

    from flask import send_file
    return send_file(volume['filepath'], as_attachment=True, download_name=volume['filename'])


@library_bp.route('/api/volumes/<int:volume_id>/manual-metadata', methods=['PUT'])
def update_volume_manual_metadata(volume_id):
    """Édite manuellement les métadonnées d'un tome (Title/Summary/Writer/Penciller/
    Colorist/Publisher/Genre/Year) - même mécanisme DB-first que la MAJ Bédéthèque
    (apply_volume_comicinfo: volumes.comicinfo d'abord, puis propagation dans le
    ComicInfo.xml du fichier si le format le permet) mais avec des valeurs saisies à la
    main plutôt que scrapées. Body JSON, tous les champs optionnels (absent = inchangé,
    chaîne vide = champ effacé):
    {title, summary, writer, penciller, colorist, publisher, genre, year,
    resolution, release_group}

    "add a way in editer de modify la qualite et releaser" - resolution/release_group
    sont déjà affichés partout (badges 🖼️/📀, colonnes de la vue liste) mais toujours
    auto-détectés depuis le nom de fichier (voir LibraryScanner._scan_*, qui préserve une
    valeur déjà en base lors d'un nouveau scan - le mécanisme de préservation existait
    déjà, seule la saisie manuelle manquait). Ce sont des colonnes `volumes` brutes, pas
    des balises ComicInfo.xml - mise à jour directe ici plutôt que via
    apply_volume_comicinfo, qui ne connaît que le sous-ensemble de balises XML standard."""
    from blueprints.bedetheque.comicinfo_writer import apply_volume_comicinfo, UnsupportedFormatError

    data = request.get_json(silent=True) or {}

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT id, series_id, filepath, format FROM volumes WHERE id = ?', (volume_id,))
        vol = cursor.fetchone()
        conn.close()

        if not vol:
            return jsonify({'success': False, 'error': 'Volume introuvable'}), 404

        field_map = {
            'title': 'Title', 'summary': 'Summary', 'writer': 'Writer', 'penciller': 'Penciller',
            'colorist': 'Colorist', 'publisher': 'Publisher', 'genre': 'Genre', 'year': 'Year',
        }
        fields = {tag: (data.get(key) or '').strip() for key, tag in field_map.items() if key in data}

        db_column_map = {'resolution': 'resolution', 'release_group': 'release_group'}
        db_updates = {
            col: ((data.get(key) or '').strip() or None)
            for key, col in db_column_map.items() if key in data
        }

        if not fields and not db_updates:
            return jsonify({'success': False, 'error': 'Aucun champ à mettre à jour'}), 400

        # DB-first (voir apply_volume_comicinfo): volumes.comicinfo est mis à jour avant
        # le fichier, qui n'en est qu'une projection - la DB reste la référence même si
        # le format ne permet pas d'écrire dans le fichier (UnsupportedFormatError)
        new_comicinfo = None
        if fields:
            new_comicinfo = apply_volume_comicinfo(current_app.config['DATABASE'], volume_id, vol['filepath'], vol['format'], fields)

        if db_updates:
            conn = get_db_connection()
            cursor = conn.cursor()
            set_clause = ', '.join(f'{col} = ?' for col in db_updates)
            cursor.execute(f'UPDATE volumes SET {set_clause} WHERE id = ?', [*db_updates.values(), volume_id])
            conn.commit()
            conn.close()

        scanner = LibraryScanner()
        scanner.update_series_stats(vol['series_id'])

        from blueprints.komga.client import trigger_scan_async
        trigger_scan_async()

        return jsonify({'success': True, 'comicinfo': new_comicinfo, **db_updates})

    except UnsupportedFormatError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@library_bp.route('/api/volumes/<int:volume_id>/move-to-series', methods=['PUT'])
def move_volume_to_series(volume_id):
    """Déplace UN tome vers une autre série existante (target_series_id dans le corps
    JSON) - "can you change edit to be able to move albums to another serie", pour un
    tome mal rattaché (ex: les tomes "Mag" d'une série mère qui appartiennent en réalité
    à sa version Magazine, une série Bédéthèque séparée). Analogue à merge_series
    ci-dessus mais pour un seul tome plutôt que toute une série: même vérification de
    conflit de nom/identité côté cible, même repli "un placeholder cible cède la place",
    mais la série source elle-même n'est jamais supprimée (elle peut garder d'autres
    tomes). Restreint à la même bibliothèque que merge_series, pour la même raison:
    resolve_within vérifie chaque chemin contre le library_path de SA série, un
    déplacement inter-bibliothèques mélangerait deux racines différentes."""
    from blueprints.library.action_history import log_action

    data = request.get_json() or {}
    target_series_id = data.get('target_series_id')
    if not target_series_id:
        return jsonify({'success': False, 'error': 'target_series_id requis'}), 400
    target_series_id = int(target_series_id)

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, series_id, filename, filepath, volume_number, is_integral, integral_number,
                   is_hs, hs_number, is_episode, episode_number, is_special, special_label
            FROM volumes WHERE id = ?
        ''', (volume_id,))
        vol = cursor.fetchone()
        if not vol:
            conn.close()
            return jsonify({'success': False, 'error': 'Volume introuvable'}), 404

        source_series_id = vol['series_id']
        if target_series_id == source_series_id:
            conn.close()
            return jsonify({'success': False, 'error': 'La série cible doit être différente de la série actuelle'}), 400

        cursor.execute('''
            SELECT s.id, s.title, s.path, s.library_id, l.path AS library_path
            FROM series s JOIN libraries l ON s.library_id = l.id
            WHERE s.id IN (?, ?)
        ''', (source_series_id, target_series_id))
        rows = {row['id']: row for row in cursor.fetchall()}
        source = rows.get(source_series_id)
        target = rows.get(target_series_id)

        if not source or not target:
            conn.close()
            return jsonify({'success': False, 'error': 'Série source ou cible introuvable'}), 404
        if target['library_id'] != source['library_id']:
            conn.close()
            return jsonify({'success': False, 'error': 'La série cible doit être dans la même bibliothèque'}), 400

        try:
            target_dir = resolve_within(target['path'] or os.path.join(target['library_path'], target['title']),
                                        target['library_path'])
        except UnsafePathError as e:
            conn.close()
            return jsonify({'success': False, 'error': f'Chemin de série invalide: {e}'}), 400

        # Même repli que merge_series: une série ajoutée depuis Bédéthèque sans aucun
        # tome possédé n'a pas encore de dossier physique.
        if not os.path.isdir(target_dir):
            try:
                os.makedirs(target_dir, exist_ok=True)
            except OSError as e:
                conn.close()
                return jsonify({'success': False, 'error': f"Impossible de créer le dossier de la série cible: {e}"}), 500

        if vol['filepath'] and os.path.exists(os.path.join(target_dir, vol['filename'])):
            conn.close()
            return jsonify({'success': False, 'error': f"Un fichier du même nom existe déjà dans la série cible: {vol['filename']}"}), 409

        key = _volume_row_identity(dict(vol))
        target_match = None
        if key is not None:
            cursor.execute('''
                SELECT id, filepath FROM volumes
                WHERE series_id = ? AND (
                    (? = 'ep' AND is_episode = 1 AND episode_number IS ?) OR
                    (? = 'vol' AND volume_number IS ?) OR
                    (? = 'int' AND is_integral = 1 AND integral_number IS ?) OR
                    (? = 'hs' AND is_hs = 1 AND hs_number IS ?)
                )
            ''', (target_series_id, key[0], key[1], key[0], key[1], key[0], key[1], key[0], key[1]))
            target_match = cursor.fetchone()
            if target_match and target_match['filepath']:
                conn.close()
                return jsonify({'success': False, 'error': 'Ce tome existe déjà (fichier réel) dans la série cible'}), 409

        if vol['filepath']:
            if target_match:
                # Placeholder cible pour ce même tome: cède la place plutôt que de créer
                # un doublon (même logique que merge_series).
                cursor.execute('DELETE FROM volumes WHERE id = ?', (target_match['id'],))
            new_path = os.path.join(target_dir, vol['filename'])
            shutil.move(vol['filepath'], new_path)
            cursor.execute('UPDATE volumes SET series_id = ?, filepath = ? WHERE id = ?',
                           (target_series_id, new_path, volume_id))
        elif target_match:
            # Placeholder source pour un tome déjà représenté côté cible - rien à
            # apporter en plus, retiré plutôt que dupliqué.
            cursor.execute('DELETE FROM volumes WHERE id = ?', (volume_id,))
        else:
            cursor.execute('UPDATE volumes SET series_id = ? WHERE id = ?', (target_series_id, volume_id))

        cursor.execute('''
            UPDATE series SET is_oneshot = 0
            WHERE id = ? AND (SELECT COUNT(*) FROM volumes WHERE series_id = ?) > 1
        ''', (target_series_id, target_series_id))
        conn.commit()
        conn.close()

        scanner = LibraryScanner()
        scanner.update_series_stats(source_series_id)
        scanner.update_series_stats(target_series_id)

        from blueprints.komga.client import trigger_scan_async
        trigger_scan_async()

        vol_label = _volume_identity_label({
            'is_episode': vol['is_episode'], 'episode_number': vol['episode_number'],
            'volume_number': vol['volume_number'],
            'is_integral': vol['is_integral'], 'integral_number': vol['integral_number'],
            'is_hs': vol['is_hs'], 'hs_number': vol['hs_number'],
        })
        log_action('move_volume', target_series_id, target['title'],
                    f"{vol_label} déplacé depuis « {source['title']} »")

        return jsonify({'success': True, 'target_series_id': target_series_id, 'target_title': target['title'],
                        'source_series_id': source_series_id})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


def _volume_identity_label(fields):
    """Libellé court (Tome N / Intégrale N / Hors-série N / Épisode N) d'un dict de champs
    d'identité (mêmes clés que _volume_row_identity) - utilisé pour le journal d'actions
    de renumber_volume."""
    if fields.get('is_episode'):
        return f"Épisode {fields['episode_number']}" if fields.get('episode_number') is not None else 'Épisode'
    if fields.get('volume_number') is not None:
        return f"Tome {fields['volume_number']}"
    if fields.get('is_integral'):
        return f"Intégrale {fields['integral_number']}" if fields.get('integral_number') is not None else 'Intégrale'
    if fields.get('is_hs'):
        return f"Hors-série {fields['hs_number']}" if fields.get('hs_number') is not None else 'Hors-série'
    return 'Édition unique'


def _volume_row_identity(row):
    """Identité d'un tome (même 4 clés que merge_series/_find_existing_volume_for_import/
    match_bedetheque_volume - voir CLAUDE.md): l'épisode est vérifié en premier et jamais
    croisé avec volume_number (un tome et un épisode peuvent partager le même numéro,
    voir _sync_bedetheque_placeholder_volumes)."""
    if row.get('is_episode'):
        return ('ep', row.get('episode_number'))
    if row.get('volume_number') is not None:
        return ('vol', row.get('volume_number'))
    if row.get('is_integral'):
        return ('int', row.get('integral_number'))
    if row.get('is_hs'):
        return ('hs', row.get('hs_number'))
    return None


@library_bp.route('/api/volumes/<int:volume_id>/renumber', methods=['PUT'])
def renumber_volume(volume_id):
    """Change manuellement le type/numéro de tome (Tome/Intégrale/Hors-série/Épisode +
    numéro) d'un volume déjà en base - "La caste des Méta-Barons - #01 ... n'est pas le
    bon volume. le correct c'est ... une HS1. met une option ... de pouvoir changer le
    numéro du volume": parse_filename/le matching Bédéthèque devinent ce type à partir du
    nom de fichier ou de la fiche Bédéthèque, mais peuvent se tromper (numéro ambigu,
    fichier mal nommé) - ceci permet une correction manuelle explicite depuis la modale
    "Éditer manuellement", sans repasser par un renommage de fichier ou un rematching
    complet.

    Body JSON: {type: 'volume'|'integral'|'hs'|'episode', number: int|null}.

    Si le nouveau type/numéro correspond déjà à un AUTRE tome de la même série (même
    logique d'identité que merge_series, voir _volume_row_identity): un placeholder
    (filepath NULL) cède simplement la place, retiré silencieusement - rien à perdre. Un
    tome RÉEL existant, en revanche, N'EST JAMAIS remplacé/supprimé automatiquement -
    "si plusieurs volumes sont mal notés ... il faudrait pouvoir changer le volume sans
    ecraser celui qui a ce numéro. donc par exemple volume 2 et 3 et volume 3 et 4. donc
    ne pas ecraser par défaut": corriger une CHAÎNE de tomes mal numérotés (le vrai tome 2
    est étiqueté 3, le vrai tome 3 est étiqueté 4...) exige de pouvoir renuméroter l'un
    SANS que l'autre disparaisse avant d'avoir pu être corrigé à son tour. Le tome édité
    prend son nouveau numéro tel quel, laissant un doublon temporaire du même numéro le
    temps que l'utilisateur règle la chaîne puis supprime lui-même celui qui est en trop
    ("il suffira que l'utilisateur supprime manuellement le volume mal numéroté" - bouton
    "Supprimer le fichier" déjà existant, voir delete_volume). Le nom de cet éventuel
    doublon est renvoyé dans la réponse (`duplicate_filename`) pour affichage, en simple
    avertissement non bloquant - pas une confirmation à valider."""
    data = request.get_json(silent=True) or {}
    new_type = data.get('type')
    if new_type not in ('volume', 'integral', 'hs', 'episode'):
        return jsonify({'success': False, 'error': 'Type de tome invalide'}), 400

    raw_number = data.get('number')
    try:
        new_number = int(raw_number) if raw_number not in (None, '') else None
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Numéro invalide'}), 400

    # is_special/special_label font partie du même jeu de champs mutuellement exclusifs
    # que is_integral/is_hs/is_episode (voir CLAUDE.md "is_special") mais n'étaient pas
    # réinitialisés ici: un tome d'abord classé "Spécial" (matché contre la MAUVAISE série
    # Bédéthèque, sans numéro) puis corrigé manuellement en tome numéroté normal restait
    # affiché "Spécial" indéfiniment - "MAJ métadonnées" n'y changeait rien non plus,
    # apply_volume_comicinfo (bedetheque/routes.py) n'écrit que le ComicInfo.xml, jamais
    # ces colonnes structurelles utilisées par le frontend pour le regroupement d'affichage.
    new_fields = {
        'volume_number': None, 'is_integral': 0, 'integral_number': None,
        'is_hs': 0, 'hs_number': None, 'is_episode': 0, 'episode_number': None,
        'is_special': 0, 'special_label': None,
    }
    if new_type == 'volume':
        new_fields['volume_number'] = new_number
    elif new_type == 'integral':
        new_fields['is_integral'] = 1
        new_fields['integral_number'] = new_number
    elif new_type == 'hs':
        new_fields['is_hs'] = 1
        new_fields['hs_number'] = new_number
    else:  # episode
        new_fields['is_episode'] = 1
        new_fields['episode_number'] = new_number

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT * FROM volumes WHERE id = ?', (volume_id,))
        vol = cursor.fetchone()
        if not vol:
            return jsonify({'success': False, 'error': 'Tome introuvable'}), 404
        series_id = vol['series_id']

        # "on ne devrait pas pouvoir changer le numéro au meme numero" - soumettre le
        # même type/numéro que l'actuel ne doit rien faire (pas de résolution Bédéthèque
        # ni de renommage inutiles, pas de faux "doublon" détecté avec soi-même) plutôt
        # que de silencieusement traverser tout le pipeline pour ne rien changer.
        if (vol['volume_number'] == new_fields['volume_number']
                and vol['is_integral'] == new_fields['is_integral']
                and vol['integral_number'] == new_fields['integral_number']
                and vol['is_hs'] == new_fields['is_hs']
                and vol['hs_number'] == new_fields['hs_number']
                and vol['is_episode'] == new_fields['is_episode']
                and vol['episode_number'] == new_fields['episode_number']
                and bool(vol['is_special']) == bool(new_fields['is_special'])
                and vol['special_label'] == new_fields['special_label']):
            return jsonify({'success': False, 'error': 'Ce tome a déjà ce numéro'}), 400

        # Un placeholder (filepath NULL) qui porte déjà ce numéro cède silencieusement la
        # place - rien à perdre. Un tome RÉEL, en revanche, n'est jamais touché: voir le
        # docstring pour la raison (corriger une chaîne de tomes mal numérotés sans
        # perdre l'un des deux avant d'avoir pu le corriger à son tour) - son nom est
        # juste renvoyé en avertissement (`duplicate_filename`).
        target_identity = _volume_row_identity(new_fields)
        duplicate_filename = None
        if target_identity is not None:
            cursor.execute('SELECT * FROM volumes WHERE series_id = ? AND id != ?', (series_id, volume_id))
            for row in cursor.fetchall():
                if _volume_row_identity(dict(row)) == target_identity:
                    other = dict(row)
                    if other['filepath']:
                        duplicate_filename = other['filename']
                    else:
                        cursor.execute('DELETE FROM volumes WHERE id = ?', (other['id'],))
                    break

        cursor.execute('''
            UPDATE volumes SET volume_number = ?, is_integral = ?, integral_number = ?,
                                is_hs = ?, hs_number = ?, is_episode = ?, episode_number = ?,
                                is_special = ?, special_label = ?
            WHERE id = ?
        ''', (new_fields['volume_number'], new_fields['is_integral'], new_fields['integral_number'],
              new_fields['is_hs'], new_fields['hs_number'], new_fields['is_episode'], new_fields['episode_number'],
              new_fields['is_special'], new_fields['special_label'],
              volume_id))
        conn.commit()
    finally:
        conn.close()

    scanner = LibraryScanner()
    scanner.update_series_stats(series_id)

    # "quand je change de volume ca reprend pas les metadatas du nouvel album ni renommé
    # au bon titre (officiel de bedetheque)" - le nouveau numéro/type DOIT refléter le
    # VRAI album Bédéthèque désormais associé à ce tome: Titre/Résumé/Auteurs/Année/Web
    # réalignés dessus, puis le fichier renommé en conséquence. Une PREMIÈRE version avait
    # retiré cette étape après un cas de corruption (un fichier "Musiques" renuméroté vers
    # un 17 déjà utilisé par un AUTRE tome réel avait hérité du titre "Chasse & Pêche") -
    # mais la vraie cause était que l'utilisateur choisissait alors un numéro à l'aveugle
    # (titre local, potentiellement faux, affiché dans le dropdown). Le dropdown affiche
    # maintenant le VRAI nom Bédéthèque de chaque tome (voir get_series_volumes,
    # bedetheque_title) - une fois le bon numéro choisi en connaissance de cause, aligner
    # ComicInfo+nom de fichier dessus est exactement le comportement voulu. Best-effort:
    # une série non matchée sur Bédéthèque, ou le nouvel album introuvable pour cette
    # identité (_resolve_volume_bedetheque_fields ne devine/matche jamais tout seul, voir
    # CLAUDE.md), ne remet pas en cause le changement de type/numéro lui-même (déjà
    # committé ci-dessus).
    if vol['filepath']:
        try:
            conn3 = get_db_connection()
            cursor3 = conn3.cursor()
            cursor3.execute('''
                SELECT v.*, s.title AS series_title, s.bedetheque_url
                FROM volumes v JOIN series s ON v.series_id = s.id
                WHERE v.id = ?
            ''', (volume_id,))
            vol_for_bd = cursor3.fetchone()
            from blueprints.bedetheque.routes import _resolve_volume_bedetheque_fields, _refresh_volume_cover
            fields, _bd_error, bd_volume = _resolve_volume_bedetheque_fields(cursor3, vol_for_bd)
            conn3.close()
            if fields:
                from blueprints.bedetheque.comicinfo_writer import apply_volume_comicinfo, UnsupportedFormatError
                try:
                    apply_volume_comicinfo(current_app.config['DATABASE'], volume_id, vol['filepath'], vol['format'], fields)
                except UnsupportedFormatError:
                    pass
            # "le cover tome 16 n'est pas bon et MAJ metadatas ... ne change rien" -
            # apply_volume_comicinfo ne touche jamais cover_path (voir son docstring), un
            # changement de numéro doit aussi rafraîchir la couverture pour le nouvel album.
            _refresh_volume_cover(volume_id, bd_volume)
        except Exception as e:
            print(f"MAJ ComicInfo après renumber_volume #{volume_id} échouée (non bloquant): {e}")

        # Renomme CE fichier au format standard configuré - même mécanisme que "Renommer
        # ce fichier" (execute_rename, volume_id) mais appelé directement plutôt que via
        # une seconde requête HTTP. Relit volumes_for_rename APRÈS la MAJ ComicInfo
        # ci-dessus: le template de renommage utilise le Titre officiel fraîchement aligné.
        try:
            conn4 = get_db_connection()
            cursor4 = conn4.cursor()
            series_for_rename = _fetch_series_for_rename(cursor4, series_id)
            if series_for_rename:
                from blueprints.settings.rename_config_store import load_rename_config
                from rename_handler import FileRenamer
                rename_cfg = load_rename_config()
                volumes_for_rename = _fetch_volumes_for_rename(cursor4, series_id, volume_id)
                if volumes_for_rename:
                    file_plan = FileRenamer.build_rename_plan(
                        series_for_rename['title'], volumes_for_rename, bool(series_for_rename['is_oneshot']),
                        rename_cfg['volume_template'], series_for_rename['universe_name'],
                        rename_cfg['oneshot_template']
                    )
                    file_results = FileRenamer.execute_rename_plan(series_for_rename['path'], file_plan)
                    for r in file_results:
                        if r.get('success') and not r.get('skipped'):
                            new_path = os.path.join(series_for_rename['path'], r['new_name'])
                            cursor4.execute('UPDATE volumes SET filename = ?, filepath = ? WHERE id = ?',
                                            (r['new_name'], new_path, r['volume_id']))
                    conn4.commit()
                    _log_rename_action(series_id, series_for_rename['title'], file_results, None)
            conn4.close()
        except Exception as e:
            print(f"Renommage après renumber_volume #{volume_id} échoué (non bloquant): {e}")

    from blueprints.komga.client import trigger_scan_async
    trigger_scan_async()

    from blueprints.library.action_history import log_action
    conn2 = get_db_connection()
    srow2 = conn2.execute('SELECT title FROM series WHERE id = ?', (series_id,)).fetchone()
    conn2.close()
    series_title = srow2['title'] if srow2 else f'Série #{series_id}'
    detail = f"{vol['filename'] or f'Tome #{volume_id}'} → {_volume_identity_label(new_fields)}"
    if duplicate_filename:
        detail += f" (numéro déjà utilisé par {duplicate_filename} - à corriger manuellement)"
    log_action('renumber_volume', series_id, series_title, detail)

    return jsonify({'success': True, 'duplicate_filename': duplicate_filename})


@library_bp.route('/api/volumes/<int:volume_id>', methods=['DELETE'])
def delete_volume(volume_id):
    """Retire un tome possédé: supprime son fichier du disque, mais PAS sa fiche - elle
    est réduite à l'état "placeholder" (comme un tome ajouté depuis Bédéthèque et jamais
    téléchargé, voir add_series_from_bedetheque) plutôt que retirée, pour qu'il reste
    suivi comme manquant (recherche/monitoring) au lieu de disparaître purement et
    simplement de la série - "quand je delete un volume ca retire le fichier... pas
    retiré la fiche du volume. tu peux retirer la fiche du volume quand je ne le possède
    pas": un tome déjà "placeholder" (filepath NULL, rien à supprimer sur disque), lui,
    n'a plus de raison d'être gardé et voit sa fiche réellement retirée de la base.
    Recalcule les stats de la série ensuite (total_volumes/missing_volumes) et déclenche
    un rescan Komga, comme toute action qui modifie le contenu d'une série."""
    from blueprints.library.action_history import log_action

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT v.filename, v.filepath, v.series_id, s.title AS series_title, s.path AS series_path,
                   l.path AS library_path
            FROM volumes v
            JOIN series s ON v.series_id = s.id
            JOIN libraries l ON s.library_id = l.id
            WHERE v.id = ?
        ''', (volume_id,))
        vol = cursor.fetchone()
        conn.close()

        if not vol:
            return jsonify({'success': False, 'error': 'Tome introuvable'}), 404

        series_id = vol['series_id']
        series_title = vol['series_title']
        was_placeholder = not vol['filepath']

        if vol['filepath']:
            try:
                safe_path = resolve_within(vol['filepath'], vol['library_path'])
            except UnsafePathError as e:
                log_action('delete_volume', series_id, series_title, vol['filename'] or f'#{volume_id}',
                           success=False, error=str(e))
                return jsonify({'success': False, 'error': f'Chemin de fichier invalide: {e}'}), 400
            if os.path.isfile(safe_path):
                os.remove(safe_path)

        conn = get_db_connection()
        if was_placeholder:
            # Rien d'autre à retirer qu'une fiche déjà vide - la garder ne servirait à rien
            conn.execute('DELETE FROM volumes WHERE id = ?', (volume_id,))
        else:
            # Redevient un placeholder: seuls les champs propres AU FICHIER supprimé sont
            # effacés (comicinfo/cover_path/author/year gardés - métadonnées Bédéthèque ou
            # descriptives, toujours valables pour ce tome même sans fichier). L'identité
            # du tome (volume_number/is_integral/.../hs_number) ne change pas.
            conn.execute('''
                UPDATE volumes SET
                    filename = NULL, filepath = NULL, format = NULL, file_size = NULL,
                    page_count = NULL, resolution = NULL, part_number = NULL, part_name = NULL,
                    komga_book_id = NULL, komga_book_url = NULL,
                    validated_size = NULL, validation_valid = NULL, validation_error = NULL
                WHERE id = ?
            ''', (volume_id,))
        conn.commit()
        conn.close()

        scanner = LibraryScanner()
        scanner.update_series_stats(series_id)

        log_action('delete_volume', series_id, series_title, vol['filename'] or f'Tome #{volume_id} (placeholder)')

        from blueprints.komga.client import trigger_scan_async
        trigger_scan_async()

        return jsonify({'success': True, 'series_id': series_id})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@library_bp.route('/api/series/<int:series_id>/toggle-oneshot', methods=['POST'])
def toggle_series_oneshot(series_id):
    """Bascule le statut one-shot d'une série"""
    try:
        conn = sqlite3.connect(current_app.config['DATABASE'])
        cursor = conn.cursor()

        # Récupérer le statut actuel
        cursor.execute('SELECT is_oneshot FROM series WHERE id = ?', (series_id,))
        result = cursor.fetchone()
        
        if not result:
            conn.close()
            return jsonify({'error': 'Série introuvable'}), 404

        current_oneshot = result[0] or 0
        new_oneshot = 1 - current_oneshot  # Basculer entre 0 et 1

        # Bascule manuelle: update_series_stats (scanner.py) préserve désormais TOUJOURS
        # la valeur actuelle de is_oneshot tant que Bédéthèque ne dit pas explicitement
        # le contraire (voir son commentaire "one-shot is only decided by bedetheque
        # metadata") - un choix fait ici n'est donc jamais remis en cause silencieusement
        # par un scan ultérieur, sans besoin d'un flag séparé pour le protéger.
        if new_oneshot == 1:
            cursor.execute(
                'UPDATE series SET is_oneshot = ?, missing_volumes = ? WHERE id = ?',
                (new_oneshot, None, series_id)
            )
            # Vider aussi les numéros de volume pour les volumes existants
            cursor.execute('UPDATE volumes SET volume_number = NULL WHERE series_id = ?', (series_id,))
        else:
            # Si on démarque le one-shot, juste mettre à jour le statut
            cursor.execute('UPDATE series SET is_oneshot = ? WHERE id = ?', (new_oneshot, series_id))
        
        conn.commit()
        conn.close()

        return jsonify({
            'success': True,
            'is_oneshot': bool(new_oneshot),
            'message': 'One-shot marqué' if new_oneshot else 'One-shot démarqué'
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@library_bp.route('/api/series/<int:series_id>/toggle-complete-override', methods=['POST'])
def toggle_series_complete_override(series_id):
    """Bascule la déclaration manuelle "cette série est complète" ("je voudrais ameliorer
    si un album est complet ou non... ca devient compliqué si j'ai un mix d'intégrales et
    d'albums. ajoute une option dans la molette pour déclarer cet album complet") - prend
    le pas sur le calcul automatique (_seriesBadgeInfo, library.js) quel qu'il soit, sans
    jamais être reconsidéré par un scan/une MAJ Bédéthèque ultérieurs (même principe que
    toggle_series_oneshot ci-dessus: un choix manuel explicite n'est jamais
    silencieusement écrasé)."""
    try:
        conn = sqlite3.connect(current_app.config['DATABASE'])
        cursor = conn.cursor()

        cursor.execute('SELECT manual_complete_override FROM series WHERE id = ?', (series_id,))
        result = cursor.fetchone()
        if not result:
            conn.close()
            return jsonify({'error': 'Série introuvable'}), 404

        new_value = 1 - (result[0] or 0)
        cursor.execute('UPDATE series SET manual_complete_override = ? WHERE id = ?', (new_value, series_id))
        conn.commit()
        conn.close()

        return jsonify({
            'success': True,
            'manual_complete_override': bool(new_value),
            'message': 'Série déclarée complète' if new_value else 'Déclaration manuelle retirée'
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@library_bp.route('/api/library/<int:library_id>/stats')
def get_library_stats_route(library_id):
    """Récupère les statistiques détaillées d'une bibliothèque"""
    try:
        conn = sqlite3.connect(current_app.config['DATABASE'])
        cursor = conn.cursor()

        # Nombre total de séries
        cursor.execute('''
            SELECT COUNT(*) FROM series WHERE library_id = ?
        ''', (library_id,))
        total_series = cursor.fetchone()[0]

        # Nombre total de volumes
        cursor.execute('''
            SELECT COUNT(*) 
            FROM volumes v
            JOIN series s ON v.series_id = s.id
            WHERE s.library_id = ?
        ''', (library_id,))
        total_volumes = cursor.fetchone()[0]

        # Taille totale
        cursor.execute('''
            SELECT COALESCE(SUM(v.file_size), 0)
            FROM volumes v
            JOIN series s ON v.series_id = s.id
            WHERE s.library_id = ?
        ''', (library_id,))
        total_size = cursor.fetchone()[0]

        # Moyenne de pages
        cursor.execute('''
            SELECT COALESCE(AVG(v.page_count), 0)
            FROM volumes v
            JOIN series s ON v.series_id = s.id
            WHERE s.library_id = ? AND v.page_count > 0
        ''', (library_id,))
        avg_pages = int(cursor.fetchone()[0])

        conn.close()

        return jsonify({
            'total_series': total_series,
            'total_volumes': total_volumes,
            'total_size': total_size,
            'avg_pages': avg_pages
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@library_bp.route('/api/library/<int:library_id>/last-updated')
def get_library_last_updated(library_id):
    """Marqueur léger de fraîcheur pour le sondage périodique côté frontend (voir
    pollLibraryFreshness, library.js) - "eviter d'avoir à le charger tout le temps [...]
    ca fera du polling pour rien": une requête bon marché (agrégats MAX/COUNT/SUM, pas de
    lecture ligne par ligne ni de jointure sur ComicInfo) plutôt que le fetch complet de
    la liste des séries, qui ne se redéclenche côté client que si CE marqueur a changé
    depuis le dernier sondage.

    Combine tous les horodatages "quelque chose a changé" déjà connus par série (scan,
    Bédéthèque, EBDZ, Komga) plutôt qu'une seule colonne updated_at - aucune n'existe qui
    couvre tous les cas de mise à jour dans cette base. Complété par series_count/
    volumes_count en filet de sécurité pour un ajout/suppression qui ne toucherait aucun
    de ces horodatages.

    Note: une édition manuelle pure (manual_summary/manual_genre/...) ne touche
    actuellement aucune de ces colonnes et ne fera donc pas bouger ce marqueur - lacune
    connue, pas corrigée ici (hors sujet de cette demande, qui portait sur les cas
    déclenchés par import/scan/backfill)."""
    try:
        conn = sqlite3.connect(current_app.config['DATABASE'])
        cursor = conn.cursor()
        cursor.execute('''
            SELECT MAX(last_scanned), MAX(bedetheque_updated_at), MAX(komga_checked_at),
                   MAX(ebdz_checked_at), COUNT(*), COALESCE(SUM(total_volumes), 0)
            FROM series WHERE library_id = ?
        ''', (library_id,))
        row = cursor.fetchone()
        conn.close()

        timestamps = [t for t in row[:4] if t]
        return jsonify({
            'marker': max(timestamps) if timestamps else None,
            'series_count': row[4],
            'volumes_count': row[5],
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@library_bp.route('/api/libraries/<int:library_id>')
def get_library_info(library_id):
    """Récupère les informations d'une bibliothèque spécifique"""
    try:
        conn = sqlite3.connect(current_app.config['DATABASE'])
        cursor = conn.cursor()

        cursor.execute('''
            SELECT id, name, path, description, created_at, last_scanned
            FROM libraries
            WHERE id = ?
        ''', (library_id,))
        
        result = cursor.fetchone()
        conn.close()

        if not result:
            return jsonify({'error': 'Bibliothèque introuvable'}), 404

        return jsonify({
            'id': result[0],
            'name': result[1],
            'path': result[2],
            'description': result[3],
            'created_at': result[4],
            'last_scanned': result[5]
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500
    
@library_bp.route('/api/series/<int:series_id>/download', methods=['GET'])
def download_series(series_id):
    """Télécharge tous les tomes possédés d'une série en une seule archive zip
    ("telecharger toute la serie" depuis la molette ⚙️ Actions de la fiche série) - un
    one-shot n'a qu'un seul fichier: préférer /api/volumes/<id>/download dans ce cas
    (voir buildSeriesToolbar côté library.js) plutôt que de zipper un seul fichier pour
    rien, cette route reste néanmoins utilisable telle quelle même sur un one-shot.

    Les fichiers sont stockés sans recompression (ZIP_STORED): un cbz/cbr est déjà une
    archive compressée, les re-compresser coûterait du temps CPU pour un gain de taille
    négligeable. Zippé dans un fichier temporaire (une série peut peser plusieurs Go,
    hors de question de tout garder en mémoire) nettoyé après l'envoi de la réponse."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT title FROM series WHERE id = ?', (series_id,))
    series = cursor.fetchone()
    if not series:
        conn.close()
        return jsonify({'success': False, 'error': 'Série introuvable'}), 404

    cursor.execute('SELECT filename, filepath FROM volumes WHERE series_id = ? AND filepath IS NOT NULL', (series_id,))
    volumes = [dict(row) for row in cursor.fetchall()]
    conn.close()

    volumes = [v for v in volumes if os.path.exists(v['filepath'])]
    if not volumes:
        return jsonify({'success': False, 'error': 'Aucun fichier à télécharger pour cette série'}), 404

    import tempfile
    from flask import send_file, after_this_request
    from blueprints.bedetheque.routes import _bedetheque_title_to_folder_name

    tmp_fd, tmp_path = tempfile.mkstemp(prefix='.series_dl_', suffix='.zip')
    os.close(tmp_fd)
    try:
        with zipfile.ZipFile(tmp_path, 'w', compression=zipfile.ZIP_STORED) as zf:
            for v in volumes:
                zf.write(v['filepath'], arcname=v['filename'])
    except Exception:
        os.remove(tmp_path)
        raise

    @after_this_request
    def _cleanup(response):
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return response

    zip_name = f"{_bedetheque_title_to_folder_name(series['title'])}.zip"
    return send_file(tmp_path, as_attachment=True, download_name=zip_name)


@library_bp.route('/api/volumes/download-zip', methods=['POST'])
def download_volumes_zip():
    """Télécharge une sélection arbitraire de tomes en une seule archive zip - action
    groupée "Télécharger" de la vue tableau des tomes (voir bulkDownloadSelectedVolumes
    côté library.js), même logique que download_series (ZIP_STORED, fichier temporaire
    nettoyé après envoi) mais restreinte à une liste d'ids plutôt qu'à toute une série.
    Les chemins viennent de la DB (jamais du client) donc pas de risque de traversée."""
    data = request.get_json(silent=True) or {}
    ids = [int(i) for i in (data.get('ids') or []) if str(i).isdigit()]
    if not ids:
        return jsonify({'success': False, 'error': 'Aucun tome sélectionné'}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    placeholders = ','.join('?' * len(ids))
    cursor.execute(
        f'''SELECT v.filename, v.filepath, v.volume_number, s.title AS series_title
            FROM volumes v JOIN series s ON v.series_id = s.id
            WHERE v.id IN ({placeholders}) AND v.filepath IS NOT NULL''',
        ids
    )
    volumes = [dict(row) for row in cursor.fetchall()]
    conn.close()

    volumes = [v for v in volumes if os.path.exists(v['filepath'])]
    if not volumes:
        return jsonify({'success': False, 'error': 'Aucun fichier à télécharger pour cette sélection'}), 404

    import tempfile
    from flask import send_file, after_this_request
    from blueprints.bedetheque.routes import _bedetheque_title_to_folder_name

    # Nom de l'archive = nom de l'album + tomes inclus ("nome le avec le nom de l'album
    # et tous les tomes" plutôt que le générique "tomes-selection.zip") - la sélection
    # groupée se fait toujours depuis la fiche d'UNE série (vue tableau des tomes), donc
    # un seul titre en pratique; le fallback générique ne sert qu'en cas d'appel API
    # direct avec des ids de séries différentes.
    series_titles = {v['series_title'] for v in volumes}
    base_name = _bedetheque_title_to_folder_name(next(iter(series_titles))) if len(series_titles) == 1 else 'Tomes sélectionnés'
    numbers = sorted({v['volume_number'] for v in volumes if v['volume_number'] is not None})
    if numbers:
        numbers_label = f"T{numbers[0]}" if len(numbers) == 1 else f"T{numbers[0]}-T{numbers[-1]}"
        zip_name = f"{base_name} - {numbers_label}.zip"
    else:
        zip_name = f"{base_name}.zip"

    tmp_fd, tmp_path = tempfile.mkstemp(prefix='.volumes_dl_', suffix='.zip')
    os.close(tmp_fd)
    try:
        with zipfile.ZipFile(tmp_path, 'w', compression=zipfile.ZIP_STORED) as zf:
            for v in volumes:
                zf.write(v['filepath'], arcname=v['filename'])
    except Exception:
        os.remove(tmp_path)
        raise

    @after_this_request
    def _cleanup(response):
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return response

    return send_file(tmp_path, as_attachment=True, download_name=zip_name)


###### ROUTE IMPORT ########

@library_bp.route('/api/series/<int:series_id>/upload-file', methods=['POST'])
def upload_series_file(series_id):
    """Upload direct d'un fichier depuis la fiche série ("ajoute une option upload
    fichier pour mettre à jour une série") - alternative au dépôt dans un répertoire
    surveillé (aMule/torrents) suivi d'une assignation manuelle sur /import, pour un
    ajout ponctuel d'un seul tome sans attendre un téléchargement externe. Le fichier est
    déposé dans un sous-dossier _uploads/<series_id> du premier répertoire d'import
    configuré (exclu du scan normal, voir les listes d'exclusion _uploads dans
    scan_import_directory/import_pending_count/scheduler._auto_import -
    sans cette exclusion, le fichier apparaîtrait aussi comme "en attente" sur /import ou
    serait candidat à un auto-import concurrent le temps que cette requête l'assigne et
    l'exécute), puis le frontend appelle /api/import/execute avec ce fichier + la série
    courante déjà pré-assignée comme destination - réutilise tout le pipeline d'import
    existant (dédoublonnage par format, conversion cbr->cbz, écriture ComicInfo, scan
    Komga...) au lieu de dupliquer sa logique ici."""
    from werkzeug.utils import secure_filename

    uploaded = request.files.get('file')
    if not uploaded or not uploaded.filename:
        return jsonify({'success': False, 'error': 'Aucun fichier reçu'}), 400

    import_roots = current_app.config['IMPORT_DIRECTORIES']
    if not import_roots:
        return jsonify({'success': False, 'error': "Aucun répertoire d'import configuré"}), 500

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT series.id, series.title, series.library_id, libraries.name AS library_name,
               libraries.path AS library_path
        FROM series JOIN libraries ON libraries.id = series.library_id
        WHERE series.id = ?
    ''', (series_id,))
    series_row = cursor.fetchone()
    conn.close()
    if not series_row:
        return jsonify({'success': False, 'error': 'Série introuvable'}), 404

    import_config = load_library_import_config()
    supported_extensions = set(import_config.get(
        'monitored_extensions', ['.cbz', '.cbr', '.zip', '.rar', '.pdf']
    ))
    ext = os.path.splitext(uploaded.filename)[1].lower()
    if ext not in supported_extensions:
        return jsonify({'success': False, 'error': f"Format non supporté: {ext or '(aucune extension)'}"}), 400

    # secure_filename retire aussi les séparateurs de chemin ('../', '/'): seule défense
    # nécessaire ici puisque le nom rejoint ensuite un chemin déjà construit côté serveur
    # (upload_dir, lui-même dérivé de series_id qui est un entier, pas d'une chaîne
    # cliente) - mais resolve_within() ci-dessous reste la garde faisant foi.
    filename = secure_filename(uploaded.filename)
    if not filename:
        return jsonify({'success': False, 'error': 'Nom de fichier invalide'}), 400

    import_root = import_roots[0]
    upload_dir = resolve_within(os.path.join(import_root, '_uploads', str(series_id)), import_root)
    os.makedirs(upload_dir, exist_ok=True)

    target_path = resolve_within(os.path.join(upload_dir, filename), import_root)
    # Évite d'écraser un envoi précédent resté en place sous le même nom (ex: un premier
    # upload jamais assigné/exécuté) plutôt que de silencieusement le remplacer
    if os.path.exists(target_path):
        base, ext2 = os.path.splitext(filename)
        target_path = resolve_within(os.path.join(upload_dir, f"{base}_{int(time.time())}{ext2}"), import_root)

    uploaded.save(target_path)

    scanner = LibraryScanner()
    parsed = scanner.parse_filename(os.path.basename(target_path))
    relative_path = os.path.relpath(target_path, import_root)

    file_data = {
        'filename': os.path.basename(target_path),
        'filepath': target_path,
        'import_root': import_root,
        'relative_path': relative_path,
        'folder_name': None,
        'file_size': os.path.getsize(target_path),
        'parsed': parsed,
        'destination': {
            'library_id': series_row['library_id'],
            'library_name': series_row['library_name'],
            'library_path': series_row['library_path'],
            'series_id': series_row['id'],
            'series_title': series_row['title'],
            'is_new_series': False,
            # Upload manuel explicite depuis la fiche série ("Remplacer le fichier"/
            # "Ajouter un fichier") - voir le commentaire sur force_replace dans
            # _execute_import_batch. Sans effet quand il n'y a pas de fichier existant à
            # remplacer (ajout d'un tome jusque-là manquant).
            'force_replace': True
        }
    }

    return jsonify({'success': True, 'file': file_data})


@library_bp.route('/api/import/pending-count', methods=['GET'])
def import_pending_count():
    """Compte les téléchargements en attente d'import pour le badge de la sidebar (voir
    nav.js, sondé toutes les 60s depuis n'importe quelle page). "the right way is to
    check the database... only the database is the source of truth" - comptait
    auparavant via _scan_tracked_import_files, qui marche par un LISTAGE DE DISQUE
    (répertoires surveillés, correspondance dossier/nom de fichier) - exactement le
    genre de dépendance au filesystem rejetée ailleurs dans ce fichier (voir
    _should_preserve_import_source/_execute_import_batch, CLAUDE.md "Working
    conventions").

    Un simple `COUNT(*) WHERE status='completed'` (premier correctif tenté ici) est
    revenu FAUX pour une raison différente: "an imported file should be considered
    complete only when volumes.filepath is populated" - des centaines de lignes
    active_downloads restent à 'completed' indéfiniment après un import réussi (voir
    mark_download_imported, ajouté seulement le 12/08 - tout ce qui l'a précédé n'a
    jamais pu en bénéficier, plus un gap encore actif pour les téléchargements
    redondants créés par les relances auto-acquire) - un count brut sur le statut
    seul aurait affiché ce passif entier au lieu du nombre réel de fichiers en attente.
    get_pending_downloads() (missing_monitor/downloader.py) a DÉJÀ ce garde-fou (un
    NOT EXISTS contre volumes.filepath) précisément pour ne pas réafficher un
    téléchargement déjà confirmé importé sur /import, quel que soit son status - appelée
    ici directement plutôt que dupliquer cette même requête, pour une garantie
    structurelle que badge et page affichent TOUJOURS le même nombre ("oui mais ce
    n'est pas ce que import affiche. il faut que ce soit la meme chose" - déjà la
    raison d'être de cette route avant même ce correctif)."""
    from blueprints.missing_monitor.downloader import get_pending_downloads
    return jsonify({'count': len(get_pending_downloads())})


def _extract_one_archive(archive_path, ext, target_dir):
    """Extrait archive_path (zip ou rar) dans target_dir, à plat (voir
    os.path.basename ci-dessous) - un membre d'archive n'est jamais recréé dans une
    sous-arborescence, à la fois pour rester cohérent avec le tri par dossier du scan
    d'import ET pour se prémunir d'un "zip slip" (nom de membre contenant "../.." pour
    s'échapper de target_dir - os.path.basename neutralise ça par construction, aucun
    composant de répertoire n'est jamais conservé).

    L'archive d'origine n'est supprimée qu'après extraction intégralement réussie -
    voir cbr_converter.py, même principe de prudence ("jamais toucher l'original avant
    que le résultat soit confirmé bon")."""
    os.makedirs(target_dir, exist_ok=True)

    if ext == '.zip':
        opener = zipfile.ZipFile
    else:
        opener = rarfile.RarFile

    with opener(archive_path) as archive:
        members = [m for m in archive.infolist() if not (m.is_dir() if ext == '.zip' else m.isdir())]
        for member in members:
            dest_name = os.path.basename(member.filename)
            if not dest_name:
                continue
            dest_path = os.path.join(target_dir, dest_name)
            with archive.open(member) as src, open(dest_path, 'wb') as dst:
                shutil.copyfileobj(src, dst)

    os.remove(archive_path)


def _zip_is_single_packaged_comic(archive_path):
    """True si ce .zip ne contient QUE des images (aucun .cbz/.cbr/.pdf, aucun
    autre type de fichier mélangé) - dans ce cas ce n'est pas un CONTENEUR de plusieurs
    tomes mais un album déjà empaqueté sous la mauvaise extension, que
    _extract_archive_containers ne doit pas exploser en centaines de .jpg individuels:
    voir zip_converter.convert_zip_to_cbz, la conversion cbz explicite proposée pour ce
    cas depuis /import (file.convertible == 'zip'). Best-effort: une archive illisible
    ici retombe sur le comportement précédent (traitée comme un conteneur normal, False)
    - l'échec réel sera de toute façon re-détecté proprement au moment de la conversion
    explicite demandée par l'utilisateur."""
    try:
        with zipfile.ZipFile(archive_path) as zf:
            names = [n for n in zf.namelist() if not n.endswith('/')]
            if not names:
                return False
            return all(os.path.splitext(n)[1].lower() in _ZIP_IMAGE_EXTENSIONS for n in names)
    except Exception:
        return False


def _extract_archive_containers(import_directories):
    """Un .zip/.rar posé dans un répertoire d'import surveillé est en général un
    CONTENEUR (le tracker/l'utilisateur l'a empaqueté ainsi, souvent pour regrouper
    plusieurs tomes ou pour la taille) - jamais lui-même un tome à importer tel quel,
    contrairement à un .cbz/.cbr ("il faudrait que tu geres les fichiers zip ou rar. donc
    à l'import tu dois les extraire"). Extrait AVANT le scan normal, dans un sous-dossier
    nommé d'après l'archive - ce sous-dossier devient alors le folder_name (signal de
    regroupement par série) des fichiers qu'il contenait, comme n'importe quel autre
    dossier de tomes déjà groupés. Best-effort par archive: une extraction échouée
    (corrompue...) laisse l'archive en place (jamais supprimée sans succès confirmé) et
    n'empêche pas les autres.

    Exception: un .zip qui ne contient QUE des images (voir
    _zip_is_single_packaged_comic) n'est PAS un conteneur mais un album déjà empaqueté
    sous la mauvaise extension - il est laissé intact ici pour rester proposable à la
    conversion cbz explicite (scan_import_directory / file.convertible == 'zip'), au
    lieu d'être irréversiblement dépaqueté en pages individuelles avant même que
    l'utilisateur ait pu choisir."""
    for import_path in import_directories:
        if not os.path.exists(import_path):
            continue

        for root, dirs, files in os.walk(import_path):
            dirs[:] = [d for d in dirs if d not in ('_uploads',)]

            for filename in files:
                ext = os.path.splitext(filename)[1].lower()
                if ext not in ('.zip', '.rar'):
                    continue

                archive_path = os.path.join(root, filename)

                if ext == '.zip' and _zip_is_single_packaged_comic(archive_path):
                    continue

                base_name = os.path.splitext(filename)[0]
                target_dir = os.path.join(root, base_name)

                try:
                    _extract_one_archive(archive_path, ext, target_dir)
                    print(f"✓ Archive extraite: {filename} -> {target_dir}")
                    # "dans import naufrage du temps alors que cest fini" - le
                    # téléchargement de CETTE archive est terminé dès qu'elle est
                    # extraite, indépendamment du sort de ses tomes individuels ensuite
                    # (voir clear_pending_download_by_title)
                    try:
                        from blueprints.missing_monitor.downloader import clear_pending_download_by_title
                        clear_pending_download_by_title(filename)
                    except Exception as e:
                        print(f"✗ Erreur nettoyage téléchargement en attente pour {filename}: {e}")

                    # "automatically convert rar to cbz. this should be done yourself
                    # before imported" - voir _auto_package_extracted_images_if_unambiguous
                    # pour les conditions exactes (jamais silencieux en cas d'ambiguïté).
                    _auto_package_extracted_images_if_unambiguous(target_dir)
                except Exception as e:
                    print(f"✗ Erreur extraction archive {filename}: {e}")


def _resolve_download_folder_identity(download, torrent_names_by_hash):
    """Nom de dossier/fichier RÉEL attendu pour un téléchargement suivi - nom du torrent
    via l'API qBittorrent si connu (voir torrent_names_by_hash, get_qbittorrent_torrent_names),
    sinon son propre titre suivi (repli client-agnostique pour aMule/rTorrent/Deluge/
    Telegram, dont l'API n'est pas interrogée ici)."""
    if download.get('client') == 'qbittorrent' and download.get('client_item_id'):
        name = torrent_names_by_hash.get(download['client_item_id'].lower())
        if name:
            return name
    return download.get('title')


def _download_folder_identities(download, torrent_names_by_hash):
    """Retourne le nom suivi et le nom sans extension créé après extraction."""
    identity = _resolve_download_folder_identity(download, torrent_names_by_hash)
    if not identity:
        return []
    identities = {identity.strip().lower()}
    stem, ext = os.path.splitext(identity)
    if ext.lower() in ('.rar', '.zip', '.cbz', '.cbr', '.pdf') and stem:
        identities.add(stem.strip().lower())
    return list(identities)


def _append_scanned_file(filepath, import_root, filename, destination, scanner, telegram_filenames,
                          manual_override_filepaths, import_config, files_found, pack_download_id=None,
                          validate_file=True):
    """Construit et ajoute une entrée files_found - factorisé entre le fichier isolé (à
    la racine d'un répertoire surveillé) et chaque fichier trouvé dans le dossier d'un
    téléchargement (voir _collect_download_folder_files)."""
    relative_path = os.path.relpath(filepath, import_root)
    parsed = scanner.parse_filename(filename)
    parent_dir = os.path.dirname(relative_path)
    folder_name = os.path.basename(parent_dir) if parent_dir else None
    ext = os.path.splitext(filename)[1].lower()

    # Une intégrale téléchargée dans un dossier explicitement nommé
    # « (L'intégrale) » doit rester une intégrale, même si le fichier interne
    # s'appelle « Volume 1.pdf ». Le nom du dossier est ici le contexte fiable
    # du téléchargement ; le numéro du fichier ne doit pas créer artificiellement
    # un tome classique.
    if folder_name and re.search(r"\bint(?:e|é)grale\b", folder_name, re.IGNORECASE):
        # « INTEGRALE » est une convention de nommage de dossier pour un pack
        # contenant toute la série, pas le type d'un album INT. Seul le tag INT/INT1
        # dans le nom du fichier désigne une intégrale individuelle.
        parsed['is_pack'] = True
        parsed['is_integral'] = False
        parsed['volume'] = None
        parsed['integral_number'] = None

    # Copie superficielle: plusieurs fichiers d'un même dossier de téléchargement
    # partagent le même `destination` de départ, jamais le même objet en sortie.
    file_destination = dict(destination) if destination else None
    # Complète parsed['volume'] depuis le tome connu au moment du téléchargement quand le
    # nom de fichier ne le fournit pas lui-même (voir apply_tracked_volume_and_gate) - pas
    # de "gate" ici contrairement à l'import automatique: cette page laisse de toute façon
    # l'utilisateur revoir/valider avant d'importer, mais autant pré-remplir le dropdown de
    # tome avec ce qu'on sait déjà plutôt que de laisser "—" alors que l'info est disponible.
    # Le résultat (True/False) est gardé pour expliquer, dans auto_import_skip_reason plus
    # bas, POURQUOI ce fichier reste "en attente" côté import automatique alors que
    # cette page-ci, elle, le montre déjà prêt pour un import manuel ("il faut que
    # l'interface indique pourquoi l'import n'est pas possible" - avant ce correctif, ce
    # cas retournait toujours None silencieusement, sans jamais remonter jusqu'à l'UI).
    gate_passed = True
    if file_destination:
        gate_passed = apply_tracked_volume_and_gate(parsed, file_destination)

    if filename in telegram_filenames:
        client = 'telegram'
    elif import_root == current_app.config.get('TELEGRAM_IMPORT_DIRECTORY'):
        # Filet de sécurité si jamais absent de telegram_filenames (ex: base
        # telegram_messages.db reconstruite/vidée) - ce répertoire n'est écrit que par les
        # téléchargements Telegram.
        client = 'telegram'
    elif import_root == current_app.config.get('AMULE_IMPORT_DIRECTORY'):
        client = 'amule'
    elif import_root == current_app.config.get('FOURTOUTICI_IMPORT_DIRECTORY'):
        client = 'fourtoutici'
    elif import_root == current_app.config.get('SHELFMARK_IMPORT_DIRECTORY'):
        client = 'shelfmark'
    else:
        client = 'torrent'

    files_found.append({
        'filename': filename,
        'filepath': filepath,
        'import_root': import_root,
        'relative_path': relative_path,
        'folder_name': folder_name,
        'file_size': os.path.getsize(filepath),
        # Date d'arrivée sur disque ("tableau comme historique", qui a une colonne Date) -
        # mtime plutôt que ctime: survit à un déplacement/renommage du fichier par le
        # client de téléchargement une fois l'écriture terminée.
        'mtime': os.path.getmtime(filepath),
        'parsed': parsed,
        'client': client,
        # "files inside the folder easy peasy" - simple appartenance au dossier du
        # téléchargement (voir _collect_download_folder_files), aucun matching de
        # série/titre nécessaire pour ce regroupement d'AFFICHAGE (voir _pendingPackGroups
        # côté import.js) - None pour un fichier isolé sans dossier conteneur.
        'pack_download_id': pack_download_id,
        # "si jai a faire manuelement un matching alors met un flag pas dimport
        # automayique" - persisté en base (import_manual_overrides) plutôt qu'un simple
        # état frontend, pour survivre à un rechargement de page.
        'manual_override': filepath in manual_override_filepaths,
        # "c'est redondant. c'est pas clair pourquoi l'import n'est pas fait" - le
        # frontend préfixe déjà ce texte avec "Pas repris par l'import automatique : "
        # (voir import.js) - répéter "import automatique désactivé"/"import automatique
        # désactivé dans les paramètres" ici formait une phrase redondante ("Pas repris
        # par l'import automatique : ... import automatique désactivé pour ce fichier").
        # Chaque raison complète maintenant directement la phrase du préfixe, sans se
        # répéter elle-même.
        'auto_import_skip_reason': (
            "assignation faite à la main - cliquez sur « Importer » pour valider"
            if filepath in manual_override_filepaths
            else _repeated_failure_skip_reason(filepath)
            or ("désactivé dans les paramètres" if not import_config.get('auto_import_enabled', False) else None)
            or (None if gate_passed else _no_volume_skip_reason(parsed, file_destination))
        ),
        # 'pdf'/'zip' si ce fichier peut être proposé à la conversion cbz (voir POST
        # /api/import/convert, jamais automatique - bouton "Convertir en CBZ" côté
        # /import) - None sinon.
        'convertible': 'pdf' if ext == '.pdf' else ('zip' if ext == '.zip' else None),
        # Connue avec certitude dès l'ajout au client (voir find_active_download_destination
        # - "get the volume number and album name not from a matching but from when the
        # file was added") - jamais devinée depuis le nom de fichier.
        'destination': file_destination,
        # None si le fichier est valide (voir _check_import_file_validity_cached) - un
        # message d'erreur explicite sinon ("Fichier corrompu, import refusé"). import.js
        # exclut un fichier avec cette valeur des fichiers "prêts" et empêche sa sélection.
        'validation_error': (
            _check_import_file_validity_cached(filepath, parsed.get('format'))
            if validate_file else None
        )
    })


def _collect_download_folder_files(folder_path, import_root, download, destination, scanner,
                                    supported_extensions, telegram_filenames, manual_override_filepaths,
                                    import_config, files_found, incompatible_folders,
                                    validate_files=True):
    """Ajoute à files_found/incompatible_folders tout ce qui se trouve DANS folder_path
    (récursivement - un sous-dossier "Bonus/" niché dedans reste légitime, voir
    order.md/CLAUDE.md) en le rattachant tel quel à `download`/`destination` - "you take
    all the files in the folder and they go under the placeholder no need to look at if
    this is matching the name of the placeholder. files inside the folder easy peasy":
    aucun matching de série/titre nécessaire, la seule appartenance au dossier du
    téléchargement suffit."""
    for root, dirs, files in os.walk(folder_path):
        dirs[:] = [d for d in dirs if d not in ('_uploads',) and not d.startswith('.')]

        folder_has_supported = False
        unsupported_in_folder = []

        for filename in files:
            ext = os.path.splitext(filename)[1].lower()
            # .part: téléchargement Telegram encore en cours (voir
            # download_channel_file_background) - ni supporté ni "incompatible", juste
            # pas encore fini, ignoré entièrement ici.
            if ext == '.part':
                continue
            if ext not in supported_extensions:
                unsupported_in_folder.append(filename)
                continue
            folder_has_supported = True
            filepath = os.path.join(root, filename)
            _append_scanned_file(
                filepath, import_root, filename, destination, scanner, telegram_filenames,
                manual_override_filepaths, import_config, files_found,
                pack_download_id=download['id'], validate_file=validate_files
            )

        # Seuil de 5 fichiers: un dossier qui n'a plus qu'un .nfo/cover.jpg/metadata.opf
        # isolé n'est pas "incompatible", c'est un débris normal laissé après qu'un import
        # ait déjà déplacé le vrai fichier ailleurs. Un vrai pack de pages scannées brutes
        # en contient toujours des dizaines à des centaines, jamais 1 ou 2.
        #
        # "pack prince de la nuit toujours la, alors que c'est bien importé" - bug réel:
        # l'exclusion "root != folder_path" ici recopiait celle de l'ANCIEN scanner, où
        # elle évitait de signaler la RACINE PARTAGÉE du répertoire surveillé entier comme
        # "incompatible" (des dizaines de téléchargements sans rapport y cohabitent).
        # folder_path DÉSIGNE ICI le dossier propre à CE téléchargement (jamais la racine
        # partagée, qui n'est même jamais parcourue par cette fonction) - un torrent dont le
        # dossier NE CONTIENT QUE des couvertures/.txt à son PROPRE niveau racine (constaté:
        # "Prince de la Nuit (Le) [HD]", que des .jpg/.txt, aucun comic) ne remontait donc
        # jamais comme incompatible, restant invisible alors qu'il n'y a justement rien à
        # importer pour lui - ni maintenant, ni jamais.
        if not folder_has_supported and len(unsupported_in_folder) >= 5:
            sample_extensions = sorted({
                os.path.splitext(f)[1].lower() or '(sans extension)'
                for f in unsupported_in_folder
            })
            incompatible_folders.append({
                'folder_name': os.path.basename(root),
                'relative_path': os.path.relpath(root, import_root),
                'folder_path': root,
                'import_root': import_root,
                'file_count': len(unsupported_in_folder),
                'sample_extensions': sample_extensions[:5],
                'pack_download_id': download['id'],
            })


def _scan_tracked_import_files(validate_files=True):
    """Cœur de scan_import_directory (voir sa docstring pour le principe: uniquement les
    téléchargements SUIVIS EN BASE, jamais de matching flou par titre/dossier) - factorisé
    pour être appelé aussi par GET /api/import/pending-count (badge de la sidebar). "oui
    mais ce n'est pas ce que import affiche. il faut que ce soit la meme chose" - le badge
    comptait auparavant TOUS les fichiers d'extension supportée présents sur le disque, un
    simple os.walk sans passer par cette logique de matching - un chiffre plus grand et
    incohérent avec /import, qui n'affiche QUE les fichiers rattachables à un
    active_downloads suivi. Ne fait PAS l'extraction d'archives-conteneurs
    (_extract_archive_containers) ni le nettoyage des caches de validation/taille: ce sont
    des effets de bord propres à une vraie visite de la page /import, pas souhaitables à
    chaque sondage du badge (toutes les 60s, depuis n'importe quelle page)."""
    scanner = LibraryScanner()
    import_config = load_library_import_config()
    supported_extensions = set(import_config.get(
        'monitored_extensions', ['.cbz', '.cbr', '.zip', '.rar', '.pdf']
    ))

    from blueprints.telegram_channels.scraper import get_downloaded_filenames
    telegram_filenames = get_downloaded_filenames()

    from blueprints.missing_monitor.downloader import get_trackable_active_downloads
    trackable_downloads = get_trackable_active_downloads()

    from blueprints.qbittorrent.routes import get_qbittorrent_torrent_names
    qbittorrent_hashes = {
        d['client_item_id'] for d in trackable_downloads
        if d.get('client') == 'qbittorrent' and d.get('client_item_id')
    }
    torrent_names_by_hash = get_qbittorrent_torrent_names(qbittorrent_hashes)

    # Identité de dossier -> téléchargement suivi (voir _resolve_download_folder_identity)
    # - le plus récent gagne en cas de collision (trackable_downloads déjà trié
    # created_at DESC, voir get_trackable_active_downloads).
    downloads_by_folder_name = {}
    for d in trackable_downloads:
        for identity in _download_folder_identities(d, torrent_names_by_hash):
            downloads_by_folder_name.setdefault(identity, d)

    from .import_history import get_manual_override_filepaths
    manual_override_filepaths = get_manual_override_filepaths()

    files_found = []
    incompatible_folders = []

    # Un seul listage NON récursif par répertoire surveillé (pas d'os.walk global) -
    # chaque entrée est soit le dossier d'un téléchargement suivi (repli sur son
    # contenu complet, voir _collect_download_folder_files), soit un fichier isolé
    # devant matcher un téléchargement suivi PAR NOM DE FICHIER exact
    # (find_active_download_destination - aMule/Telegram ne créent pas toujours un
    # dossier conteneur). Tout le reste (dossier/fichier non rattachable à aucune
    # ligne active_downloads) est ignoré, jamais signalé.
    for import_path in current_app.config['IMPORT_DIRECTORIES']:
        if not os.path.isdir(import_path):
            continue
        try:
            entries = list(os.scandir(import_path))
        except OSError:
            continue

        for entry in entries:
            if entry.name.startswith('.'):
                continue

            if entry.is_dir():
                download = downloads_by_folder_name.get(entry.name.strip().lower())
                if not download:
                    continue
                destination = _build_active_download_destination(
                    download['series_id'], download.get('volume_number'), download['id']
                )
                _collect_download_folder_files(
                    entry.path, import_path, download, destination, scanner,
                    supported_extensions, telegram_filenames, manual_override_filepaths,
                    import_config, files_found, incompatible_folders,
                    validate_files=validate_files
                )
            elif entry.is_file():
                ext = os.path.splitext(entry.name)[1].lower()
                if ext == '.part' or ext not in supported_extensions:
                    continue
                match = find_active_download_destination(entry.name, trackable_downloads)
                if not match:
                    # "will it happen again on another file" - un torrent single-
                    # file dont le nom réel (une fois ajouté chez le client) diverge
                    # trop du titre de release suivi en base (find_active_download_
                    # destination compare au TITRE, jamais assez proche après
                    # normalisation - constaté sur "Le.Vent.Dans.Les.Saules.T01...-
                    # NOTAG" suivi vs le fichier réel "[BD FR] Le vent dans les
                    # Saules - T01-...cbr") restait invisible pour toujours, alors
                    # même que downloads_by_folder_name (juste au-dessus, déjà
                    # calculé pour le cas dossier) contient déjà ce nom RÉEL résolu
                    # via l'API du client de téléchargement (torrent_names_by_hash) -
                    # jamais consulté ici pour un fichier isolé jusqu'à présent. Même
                    # repli, appliqué au fichier plutôt qu'à un nom de dossier.
                    torrent_download = downloads_by_folder_name.get(entry.name.strip().lower())
                    if torrent_download:
                        match = _build_active_download_destination(
                            torrent_download['series_id'], torrent_download.get('volume_number'), torrent_download['id']
                        )
                if not match:
                    continue
                _append_scanned_file(
                    entry.path, import_path, entry.name, match, scanner, telegram_filenames,
                    manual_override_filepaths, import_config, files_found,
                    pack_download_id=match.get('tracking_id'),
                    validate_file=validate_files
                )

    return files_found, incompatible_folders


@library_bp.route('/api/import/scan', methods=['POST'])
def scan_import_directory():
    """Scanne les téléchargements SUIVIS EN BASE (active_downloads) pour trouver les
    fichiers à importer - "scanner the files on the disk is a mess. remove it. scanner
    will only reload the database. simple as that": remplace l'ancien os.walk() global
    sur tout l'arbre + matching flou par titre/dossier (source d'une longue série de bugs
    cette session - fichiers non regroupés, dossiers imbriqués ignorés, faux
    positifs/négatifs de matching) par une lecture pilotée par la base. Pour CHAQUE
    téléchargement suivi (get_trackable_active_downloads), on résout son dossier RÉEL
    connu (voir _resolve_download_folder_identity) et on ne liste QUE ce dossier - aucune
    tentative de deviner la série d'un fichier qui n'est pas déjà rattaché à une ligne
    active_downloads. Un fichier/dossier jamais suivi par cette app n'apparaît plus du
    tout sur /import (choix explicite confirmé par l'utilisateur, conséquence assumée).

    Le scheduler périodique (blueprints/library/scheduler.py, import automatique
    aMule/qBittorrent/rTorrent/Deluge sans intervention) garde pour l'instant sa propre
    implémentation (fuzzy matching inclus) - seule CETTE route (/api/import/scan, ce que
    l'utilisateur voit réellement sur /import) est concernée par cette réécriture."""
    try:
        # Extraire tout .zip/.rar-conteneur AVANT le scan - ce qu'il contenait apparaît
        # alors comme des fichiers normaux dans files_found, pas l'archive elle-même. Ne
        # fait AUCUN matching de série - une extraction pure. Effet de bord (supprime
        # l'archive d'origine une fois extraite) volontairement absent de
        # _scan_tracked_import_files, réutilisée aussi par le badge sondé toutes les 60s
        # depuis n'importe quelle page - pas le bon endroit pour déclencher ça.
        _extract_archive_containers(current_app.config['IMPORT_DIRECTORIES'])

        files_found, incompatible_folders = _scan_tracked_import_files()

        # Le fichier est relié à active_downloads par destination.tracking_id. Un pack,
        # une extraction ou un renommage peut avoir un nom différent du titre suivi.
        try:
            from blueprints.missing_monitor.downloader import clear_pending_downloads_by_tracking_ids
            tracking_ids = [(f.get('destination') or {}).get('tracking_id') for f in files_found]
            clear_pending_downloads_by_tracking_ids(tracking_ids)
        except Exception as e:
            print(f"✗ Erreur nettoyage téléchargements en attente: {e}")

        # _import_file_validity_cache (voir plus haut) n'était jamais purgé - un fichier
        # importé/supprimé, ou dont la taille change encore pendant le téléchargement,
        # laissait derrière lui une entrée orpheline pour toujours (clé (filepath, taille)
        # jamais revisitée). Un process au long cours sur un répertoire d'import actif
        # grossissait ce dict sans limite. Ce scan est le point de passage obligé pour
        # tout fichier encore surveillé - ne garder que les clés dont le filepath est
        # toujours présent dans files_found suffit à le maintenir auto-purgé.
        still_present = {f['filepath'] for f in files_found}
        for cache_key in [k for k in _import_file_validity_cache if k[0] not in still_present]:
            del _import_file_validity_cache[cache_key]
        # Même purge pour _import_file_size_history (voir son commentaire) - un fichier
        # importé/supprimé n'a plus besoin d'y garder la taille de son dernier scan.
        for path in [p for p in _import_file_size_history if p not in still_present]:
            del _import_file_size_history[path]

        return jsonify({
            'success': True,
            'files': files_found,
            'count': len(files_found),
            'incompatible_folders': incompatible_folders
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@library_bp.route('/api/import/validation', methods=['GET'])
def import_validation_items():
    """Return import entries that still require a user's decision.

    This is read-only: unlike /api/import/scan it does not extract archives or
    clear pending-download rows. The Import page remains the place where the
    actual assignment, corruption override, packaging, or deletion is done.
    """
    try:
        files, incompatible_folders = _scan_tracked_import_files()
        items = []
        for file in files:
            destination = file.get('destination') or {}
            parsed = file.get('parsed') or {}
            reason = None
            kind = None
            if file.get('validation_error') and not file.get('forceImport'):
                kind, reason = 'corrupt', file['validation_error']
            elif parsed.get('tracked_volume_conflict') is not None:
                kind = 'volume_conflict'
                reason = 'Le type ou numéro du fichier ne correspond pas au volume recherché.'
            elif not destination:
                kind, reason = 'unassigned', 'Aucune série ou bibliothèque assignée.'
            elif not destination.get('volume_id') and not parsed.get('is_oneshot'):
                kind, reason = 'missing_volume', 'Série trouvée, mais tome à confirmer.'
            if kind:
                items.append({
                    'kind': kind, 'filename': file.get('filename'),
                    'client': file.get('client'), 'reason': reason,
                    'relative_path': file.get('relative_path'),
                    'import_root': file.get('import_root'),
                })
        folders = [{
            'kind': 'packaging', 'filename': folder.get('folder_name') or folder.get('relative_path'),
            'reason': 'Dossier incompatible nécessitant un choix de regroupement.',
            'relative_path': folder.get('relative_path'), 'import_root': folder.get('import_root'),
        } for folder in incompatible_folders]
        return jsonify({'success': True, 'items': items + folders})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@library_bp.route('/api/import/state', methods=['GET'])
def import_state_snapshot():
    """Retourne un instantané faisant autorité pour la page /import.

    L'ancienne page assemblait dans le navigateur trois réponses indépendantes : les
    éléments en direct du client, les lignes ``active_downloads`` et un scan du système
    de fichiers mis en cache séparément. Une ligne pouvait donc afficher un pourcentage
    récent à côté d'un album ou d'un statut ancien. Cette route compose côté serveur le
    scan et l'état du client/de la base, puis les renvoie avec un seul horodatage. Le
    scan est volontairement limité aux téléchargements suivis par
    ``_scan_tracked_import_files`` ; les fichiers sans rapport présents sur le point de
    montage d'import ne sont jamais exposés.
    """
    try:
        # UI polling must never decompress archives or open PDFs. Explicit scan,
        # validation and the isolated import worker retain the integrity checks.
        files, incompatible_folders = _scan_tracked_import_files(validate_files=False)

        # La présence d'un fichier suivi sur le disque est la transition de référence
        # entre téléchargement et prêt. Utiliser l'identifiant de suivi exact fourni
        # par le scan, jamais une correspondance approximative sur le nom, et effectuer
        # cette étape avant de lire l'instantané final afin que ses lignes en attente
        # correspondent au même instant.
        tracking_ids = [
            (f.get('destination') or {}).get('tracking_id')
            for f in files
        ]
        try:
            from blueprints.missing_monitor.downloader import clear_pending_downloads_by_tracking_ids
            clear_pending_downloads_by_tracking_ids(tracking_ids)
        except Exception as exc:
            print(f"✗ Erreur transition téléchargement prêt: {exc}")

        # Le scan a été effectué juste avant la transition ci-dessus ; sa copie du champ
        # destination indique donc encore ``pending``. Refléter la transition exacte dans
        # cette même réponse au lieu de faire attendre le navigateur pour un autre scan.
        for file in files:
            destination = file.get('destination') or {}
            if destination.get('download_status') == 'pending':
                destination['download_status'] = 'completed'

        from blueprints.activity.routes import activity_status
        activity_response = activity_status()
        activity_data = activity_response.get_json() or {}
        if not activity_data.get('success'):
            return jsonify({
                'success': False,
                'error': activity_data.get('error', 'Statut des téléchargements indisponible')
            }), 502

        # "even if the process is stuck I should still be able to see the current
        # download" - fichier réellement en train d'être traité par _execute_import_batch
        # à cet instant précis (voir get_currently_processing_file, import_history.py),
        # distinct du badge générique "Import en cours" déjà affiché sur tous les
        # fichiers d'un batch en vol (voir statusBadge, import.js) - celui-ci ne dit
        # jamais LEQUEL est le fichier actif ni depuis combien de temps.
        from .import_history import get_currently_processing_file
        currently_processing = get_currently_processing_file()

        return jsonify({
            'success': True,
            'snapshot_at': time.time(),
            'clients': activity_data.get('clients', []),
            'pending': activity_data.get('pending', []),
            'completed_pending_ids': activity_data.get('completed_pending_ids', []),
            'files': files,
            'incompatible_folders': incompatible_folders,
            'currently_processing': currently_processing,
        })
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(exc)}), 500


@library_bp.route('/api/import/file', methods=['DELETE'])
def delete_import_file():
    """Supprime définitivement un fichier des répertoires d'import surveillés (aMule/torrents)
    - "si dans la page import il y a des fichiers non importés clique supprimer doit
    supprimer dans le client de telechargement et les fichiers du dossier de
    telechargement" (order.md): tente D'ABORD de retrouver ce fichier chez son client
    (aMule sans ambiguïté ; 'torrent' reste partagé entre qBittorrent/rTorrent/Deluge, voir
    CLAUDE.md - les trois sont essayés) pour l'annuler, puis supprime le fichier local dans
    tous les cas (best-effort: rien à annuler côté client - déjà terminé, ou pas retrouvé -
    n'empêche jamais la suppression locale demandée)."""
    data = request.get_json() or {}
    import_root = data.get('import_root', '')
    relative_path = data.get('relative_path', '')
    filename = data.get('filename', '')
    client = data.get('client', '')

    if not import_root or not relative_path:
        return jsonify({'error': 'import_root et relative_path requis'}), 400

    import_directories = current_app.config['IMPORT_DIRECTORIES']
    import_root = os.path.realpath(import_root)
    if import_root not in [os.path.realpath(d) for d in import_directories]:
        return jsonify({'error': "Répertoire d'import non autorisé"}), 403

    filepath = os.path.realpath(os.path.join(import_root, relative_path))
    if os.path.commonpath([filepath, import_root]) != import_root:
        return jsonify({'error': 'Chemin de fichier invalide'}), 403

    if not os.path.isfile(filepath):
        return jsonify({'error': 'Fichier introuvable'}), 404

    cancelled_at_client = False
    if filename and client in ('amule', 'torrent'):
        try:
            import requests
            from blueprints.activity.routes import _CLIENT_STATUS_FNS, _CLIENT_REMOVE_URLS
            from blueprints.missing_monitor.downloader import _filenames_match
            candidates = ['amule'] if client == 'amule' else ['qbittorrent', 'rtorrent', 'deluge']
            for candidate in candidates:
                status = _CLIENT_STATUS_FNS[candidate]()
                match = next((i for i in (status or {}).get('items', []) if _filenames_match(i['name'], filename)), None)
                if match and match.get('id'):
                    resp = requests.post(_CLIENT_REMOVE_URLS[candidate], json={'id': match['id']}, timeout=15)
                    if resp.status_code == 200 and resp.json().get('success'):
                        cancelled_at_client = True
                        break
        except Exception as e:
            print(f"Erreur annulation téléchargement client pour '{filename}': {e}")

    try:
        os.remove(filepath)
        cleanup_empty_directories(import_root)
        return jsonify({'success': True, 'cancelled_at_client': cancelled_at_client})
    except OSError as e:
        return jsonify({'error': str(e)}), 500


@library_bp.route('/api/import/incompatible-folder/files', methods=['GET'])
def list_incompatible_folder_files():
    """Liste les fichiers d'un dossier de téléchargement "incompatible" (voir
    scan_import_directory, incompatible_folders) - "je voudrais voir les noms des fichiers
    comme ca je peux estimer que faire": jusqu'ici seul un échantillon de 5 extensions
    (sample_extensions) était visible depuis /import, pas de quoi juger si le pack vaut la
    peine d'être récupéré manuellement (ex: repackagé en .cbz) ou simplement supprimé.
    Même validation de chemin que delete_incompatible_folder (lecture seule ici, mais même
    prudence: import_root/relative_path viennent du client)."""
    import_root = request.args.get('import_root', '')
    relative_path = request.args.get('relative_path', '')

    if not import_root or not relative_path:
        return jsonify({'error': 'import_root et relative_path requis'}), 400

    import_directories = current_app.config['IMPORT_DIRECTORIES']
    import_root = os.path.realpath(import_root)
    if import_root not in [os.path.realpath(d) for d in import_directories]:
        return jsonify({'error': "Répertoire d'import non autorisé"}), 403

    folder_path = os.path.realpath(os.path.join(import_root, relative_path))
    if folder_path == import_root or os.path.commonpath([folder_path, import_root]) != import_root:
        return jsonify({'error': 'Chemin de dossier invalide'}), 403

    if not os.path.isdir(folder_path):
        return jsonify({'error': 'Dossier introuvable'}), 404

    try:
        entries = sorted(os.listdir(folder_path))
    except OSError as e:
        return jsonify({'error': str(e)}), 500

    # Plafonné: certains packs contiennent des centaines de pages scannées individuelles
    # (voir le cas qui a motivé cette fonctionnalité, ~370 .jpg) - au-delà, le nombre total
    # suffit à juger, la liste complète n'apporterait rien de plus qu'un payload énorme.
    LIMIT = 300
    files = [
        {'name': name, 'size': os.path.getsize(os.path.join(folder_path, name))}
        for name in entries[:LIMIT]
        if os.path.isfile(os.path.join(folder_path, name))
    ]
    return jsonify({'success': True, 'files': files, 'total_count': len(entries), 'truncated': len(entries) > LIMIT})


def _resolve_incompatible_folder_path(import_root, relative_path):
    """Valide et résout (import_root, relative_path) fournis par le client vers un chemin
    de dossier réel - factorisé pour convert-to-cbz (preview + exécution), même contrôle
    que list_incompatible_folder_files/delete_incompatible_folder ci-dessus/dessous.
    Retourne (folder_path, None) ou (None, (payload, status)) en cas d'erreur."""
    if not import_root or not relative_path:
        return None, ({'error': 'import_root et relative_path requis'}, 400)

    import_directories = current_app.config['IMPORT_DIRECTORIES']
    import_root_real = os.path.realpath(import_root)
    if import_root_real not in [os.path.realpath(d) for d in import_directories]:
        return None, ({'error': "Répertoire d'import non autorisé"}, 403)

    folder_path = os.path.realpath(os.path.join(import_root_real, relative_path))
    if folder_path == import_root_real or os.path.commonpath([folder_path, import_root_real]) != import_root_real:
        return None, ({'error': 'Chemin de dossier invalide'}, 403)

    if not os.path.isdir(folder_path):
        return None, ({'error': 'Dossier introuvable'}, 404)

    return folder_path, None


# Pages scannées individuellement (.jpg/.jpeg/.png/.webp) - le cas concret qui a motivé
# "incompatible_folders" (voir scan_import_directory) et ce convertisseur : un pack qui
# n'est justement PAS déjà empaqueté en .cbz/.cbr.
_LOOSE_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}

# Numéro de page final du nom de fichier ("Monstre - 001.jpg" -> préfixe "Monstre -",
# numéro 001) - séparateurs espace/underscore/tiret/point optionnels, numéro
# éventuellement entre parenthèses ("Monstre (12).jpg"), avec une lettre de variante
# optionnelle après le chiffre ("NDT01-00b.jpg" -> même page "00", variante "b" -
# planche alternative/redessinée, convention courante des scans BD). "le matching est
# pas correct... il n'y a que NDT01 jusqu'a NDT10... le matching est trop large" -
# sans le "[a-zA-Z]?" final, "NDT01-00b" ne matchait PAS ce pattern du tout (la lettre
# finale casse l'ancrage `\d{1,4}$`), tombant dans le repli "stem entier comme préfixe"
# et formant son propre groupe à un seul fichier au lieu de rejoindre le groupe "NDT01"
# où sont toutes les autres pages de cet album - constaté en réel sur un pack "Les
# Naufragés du Temps" (10 albums réels NDT01..NDT10, 15 pages variantes en trop
# formant chacune un groupe fantôme).
_PAGE_NUMBER_RE = re.compile(r'^(.*?)[\s_.\-]*\(?(\d{1,4})\)?[a-zA-Z]?$')

# Consolidation finale de _group_loose_images_by_album (voir son commentaire "Cubitus
# T22 - 50 couv") - PAS le critère principal de regroupement (voir _numeric_variant_clusters
# ci-dessous pour ça), seulement un signal de fusion en dernier recours entre deux groupes
# déjà formés dont le label mentionne le même numéro de tome malgré des noms différents.
_TOME_NUMBER_RE = re.compile(r'\bt(\d{1,3})\b', re.IGNORECASE)

_TOKEN_RE = re.compile(r'\d+|\D+')


def _token_skeleton(tokens):
    """Les segments non-numériques d'un nom tokenisé, dans l'ordre - deux noms avec le
    même squelette ont la même structure de mots, seuls leurs nombres diffèrent
    potentiellement. Sert de clé de bucket rapide avant la comparaison paire-à-paire
    (voir _numeric_variant_clusters) plutôt que de comparer TOUS les noms entre eux."""
    return tuple(t for t in tokens if not t.isdigit())


def _numeric_variant_clusters(stems):
    """"pour cubitus tu parses mal les albums... surtout parse par nom de fichier qui se
    ressemblent et où il y a un chiffre qui varie" - regroupe des noms de fichiers
    (sans extension) identiques trait pour trait SAUF un seul nombre qui diffère, quelle
    que soit sa position dans le nom (pas seulement en fin de nom comme
    _PAGE_NUMBER_RE). Constaté en réel sur un pack Cubitus: "16 - Cubitus - T11 -
    LOGiTEAM.jpg" et "03 - Cubitus - T11 - LOGiTEAM.jpg" sont deux scans de couverture du
    même tome (le nombre en tête est un numéro de scan/page sans rapport, "T11" reste
    identique) - aucune regex fixe ("T0x") n'est nécessaire ici: les deux noms ne
    diffèrent QUE sur ce premier nombre, ce qui suffit à les rattacher au même album quel
    que soit le marqueur utilisé par la série (T, INT, HS, ou aucun).

    Retourne {stem: template} - `template` est la clé de regroupement commune à tous les
    membres d'un même cluster (le nom avec sa position variable neutralisée), un stem
    sans jumeau garde template == lui-même. Bucketés par squelette (mots non-numériques)
    d'abord: la comparaison paire-à-paire ne coûte cher qu'à l'intérieur d'un bucket, pas
    sur l'ensemble du dossier (potentiellement plusieurs centaines de fichiers)."""
    tokenized = {stem: _TOKEN_RE.findall(stem) for stem in stems}
    buckets = {}
    for stem, tokens in tokenized.items():
        buckets.setdefault(_token_skeleton(tokens), []).append(stem)

    parent = {stem: stem for stem in stems}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for bucket_stems in buckets.values():
        for i in range(len(bucket_stems)):
            for j in range(i + 1, len(bucket_stems)):
                a, b = bucket_stems[i], bucket_stems[j]
                ta, tb = tokenized[a], tokenized[b]
                if len(ta) != len(tb):
                    continue
                diff_indices = [k for k in range(len(ta)) if ta[k] != tb[k]]
                # Exactement UN token diffère, et c'est bien un nombre des deux côtés -
                # un mot différent ("GorcRip" vs "LOGiTEAM") ne doit jamais suffire à
                # rattacher deux noms au même album.
                if len(diff_indices) == 1 and ta[diff_indices[0]].isdigit() and tb[diff_indices[0]].isdigit():
                    union(a, b)

    clusters = {}
    for stem in stems:
        clusters.setdefault(find(stem), []).append(stem)

    result = {}
    for members in clusters.values():
        if len(members) == 1:
            result[members[0]] = members[0]
            continue
        tokens_list = [tokenized[m] for m in members]
        template_tokens = list(tokens_list[0])
        for k in range(len(template_tokens)):
            if len({t[k] for t in tokens_list}) > 1:
                template_tokens[k] = ''
        template = ''.join(template_tokens).strip(' _-.') or members[0]
        for m in members:
            result[m] = template
    return result


def _natural_sort_key(filename):
    """Tri naturel ("page 2" avant "page 10") - un tri alphabétique brut classerait
    "page 10" avant "page 2"."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r'(\d+)', filename)]


def _group_loose_images_by_album(filenames):
    """Regroupe des noms de fichiers image en albums probables ("aussi il faudra faire
    attention que les noms des fichiers ne soient pas differents albums") - retire le
    numéro de page final de chaque nom pour ne garder que le préfixe, les fichiers
    partageant le même préfixe (normalisé casse/espaces) forment un même album. Un nom
    sans préfixe reconnaissable (page purement numérotée, "001.jpg") tombe dans un groupe
    "(sans titre)" commun plutôt que de former autant de groupes à un seul fichier que de
    pages - le cas le plus courant (un seul album, pages numérotées sans titre répété)
    doit rester un seul groupe.

    Retourne une liste de {'label', 'files'} dans l'ordre de première apparition,
    `files` trié en ordre naturel de page. Best-effort, jamais un dossier vide ou une
    exception: un nom qui ne matche même pas _PAGE_NUMBER_RE et n'a aucun jumeau
    détecté par _numeric_variant_clusters forme son propre groupe par son nom complet."""
    stem_by_filename = {f: os.path.splitext(f)[0] for f in filenames}
    unmatched_stems = []
    prefix_by_filename = {}
    for filename, stem in stem_by_filename.items():
        match = _PAGE_NUMBER_RE.match(stem)
        if match:
            prefix_by_filename[filename] = match.group(1).strip(' _-.')
        else:
            unmatched_stems.append(stem)

    # "surtout parse par nom de fichier qui se ressemblent et où il y a un chiffre qui
    # varie" - pour tout ce que _PAGE_NUMBER_RE n'a pas su rattacher (le nom ne se
    # termine pas par un numéro de page), cherche un jumeau parmi les AUTRES noms non
    # reconnus dont le seul écart est un nombre (peu importe sa position/son marqueur).
    templates_by_stem = _numeric_variant_clusters(unmatched_stems) if unmatched_stems else {}
    for filename, stem in stem_by_filename.items():
        if filename not in prefix_by_filename:
            prefix_by_filename[filename] = templates_by_stem[stem].strip(' _-.')

    groups = {}
    order = []
    for filename in filenames:
        prefix = prefix_by_filename[filename]
        if prefix:
            key, label = prefix.lower(), prefix
        else:
            key, label = '__untitled__', '(sans titre)'
        if key not in groups:
            groups[key] = {'label': label, 'files': []}
            order.append(key)
        groups[key]['files'].append(filename)

    # "Cubitus T22 - 50 couv et Cubitus T22 - 51 couv ne sont pas ensemble... il faudrait
    # une option pour les matcher avec Cubitus T22" - une couverture nommée séparément des
    # pages elles-mêmes ("Cubitus T22 - 50 couv.jpg" vs "16 - Cubitus - T22 -
    # LOGiTEAM.jpg") ne "ressemble" pas assez à leur nom pour que _numeric_variant_clusters
    # les rapproche (plusieurs mots diffèrent, pas seulement un nombre) - mais un même
    # numéro de tome ("T22") mentionné dans les DEUX labels reste un signal fort et sûr,
    # réutilisé ici en dernier passage pour fusionner ces groupes structurellement
    # différents mais du même album. Le groupe avec le PLUS de fichiers fait foi pour le
    # label final (le nom des pages elles-mêmes, pas celui d'une couverture isolée).
    groups_by_tome = {}
    for key in order:
        tome_match = _TOME_NUMBER_RE.search(groups[key]['label'])
        if tome_match:
            groups_by_tome.setdefault(tome_match.group(1), []).append(key)
    for keys in groups_by_tome.values():
        if len(keys) < 2:
            continue
        canonical = max(keys, key=lambda k: len(groups[k]['files']))
        for key in keys:
            if key != canonical:
                groups[canonical]['files'].extend(groups[key]['files'])
                del groups[key]
                order.remove(key)

    return [
        {'label': groups[key]['label'], 'files': sorted(groups[key]['files'], key=_natural_sort_key)}
        for key in order
    ]


def _apply_folder_name_fallback(groups, folder_path):
    """Remplace le label sentinelle "(sans titre)" (voir _group_loose_images_by_album)
    par le nom du DOSSIER contenant les images - "j'ai empaqueter en cbz. ca a perdu le
    titre": des pages nommées juste par leur numéro ("001.jpg", "002.jpg"...) ne portent
    aucun titre à extraire du nom de fichier lui-même, mais le dossier qui les contient,
    lui, est presque toujours nommé d'après l'album/la série
    ("Monstre (Enki Bilal)/001.jpg"...) - sans ce repli, le .cbz produit se retrouvait
    littéralement nommé "(sans titre).cbz" alors qu'un vrai titre était disponible juste
    un niveau au-dessus."""
    folder_label = os.path.basename(folder_path.rstrip(os.sep)) or 'Album'
    for group in groups:
        if group['label'] == '(sans titre)':
            group['label'] = folder_label
    return groups


def _package_loose_image_groups_as_cbz(folder_path, groups):
    """Empaquette chaque groupe d'images de `groups` (voir _group_loose_images_by_album)
    en un .cbz écrit directement dans folder_path - un fichier par groupe, jamais un
    mélange. Vérifie l'intégrité (`testzip`) avant de supprimer les images d'origine,
    jamais avant - un .cbz corrompu ne doit jamais coûter les pages originales. Partagé
    par convert_incompatible_folder_to_cbz (empaquetage manuel confirmé par
    l'utilisateur, voir sa route) et _auto_package_extracted_images_if_unambiguous
    (empaquetage automatique silencieux juste après extraction d'archive, voir
    _extract_archive_containers) - même logique d'écriture/vérification/nettoyage dans
    les deux cas plutôt que deux implémentations divergentes. Retourne (created, errors)."""
    from blueprints.bedetheque.routes import _bedetheque_title_to_folder_name

    created = []
    errors = []
    for group in groups:
        base_name = _bedetheque_title_to_folder_name(group['label']) or 'Album'
        cbz_name = f"{base_name}.cbz"
        cbz_path = os.path.join(folder_path, cbz_name)
        # Un nom déjà pris (conversion relancée, ou nom d'album coïncidant avec un
        # fichier existant) suffixé plutôt qu'écrasé, même convention que
        # download_channel_file_background/upload_series_file.
        if os.path.exists(cbz_path):
            counter = 1
            while os.path.exists(cbz_path):
                cbz_path = os.path.join(folder_path, f"{base_name}_{counter}.cbz")
                counter += 1

        try:
            with zipfile.ZipFile(cbz_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
                for filename in group['files']:
                    zf.write(os.path.join(folder_path, filename), arcname=filename)

            with zipfile.ZipFile(cbz_path, 'r') as check:
                bad_file = check.testzip()
            if bad_file is not None:
                raise ValueError(f"Archive corrompue produite (membre invalide: {bad_file})")

            for filename in group['files']:
                try:
                    os.remove(os.path.join(folder_path, filename))
                except OSError:
                    pass

            created.append({'filename': os.path.basename(cbz_path), 'page_count': len(group['files'])})
        except Exception as e:
            if os.path.exists(cbz_path):
                try:
                    os.remove(cbz_path)
                except OSError:
                    pass
            errors.append(f"{group['label']}: {e}")

    return created, errors


def _auto_package_extracted_images_if_unambiguous(target_dir):
    """Empaquette immédiatement en .cbz juste après extraction d'un .zip/.rar
    (_extract_archive_containers) si le contenu est SANS AMBIGUÏTÉ un album unique -
    "automatically convert rar to cbz. this should be done yourself before imported":
    jusqu'ici un .rar contenant les pages d'un seul tome (le cas le plus courant, pas un
    conteneur de plusieurs tomes) finissait juste extrait en pages en vrac, laissé comme
    dossier "incompatible" tant qu'un clic manuel "Empaqueter en .cbz" n'avait pas eu
    lieu - le tome n'était donc jamais réellement importable tout seul.

    Jamais silencieux quand il y a la moindre ambiguïté, même prudence que
    preview_convert_incompatible_folder_to_cbz (confirmation manuelle avant d'écrire) :
    - le dossier ne contient QUE des images (rien d'autre - un mélange avec des .cbz/.cbr
      déjà là, ou des sous-dossiers, indique un vrai conteneur multi-tomes plutôt qu'un
      album unique tout juste extrait, laissé pour le scan/regroupement normal) ;
    - _group_loose_images_by_album n'y détecte qu'UN SEUL groupe (plusieurs groupes =
      plusieurs albums mélangés dans la même archive, cas justement pensé pour rester
      soumis à confirmation humaine via le dossier "incompatible" habituel)."""
    try:
        entries = os.listdir(target_dir)
    except OSError:
        return
    if not entries:
        return
    if not all(
        os.path.isfile(os.path.join(target_dir, name))
        and os.path.splitext(name)[1].lower() in _LOOSE_IMAGE_EXTENSIONS
        for name in entries
    ):
        return

    groups = _apply_folder_name_fallback(_group_loose_images_by_album(entries), target_dir)
    if len(groups) != 1:
        return

    created, errors = _package_loose_image_groups_as_cbz(target_dir, groups)
    folder_label = os.path.basename(target_dir.rstrip(os.sep))
    if created:
        print(f"✓ Auto-empaqueté en cbz: {target_dir} -> {created[0]['filename']}")
    if errors:
        print(f"✗ Erreur auto-empaquetage cbz {target_dir}: {errors}")

    # "l'empaquetage devrait être dans l'historique" (même demande déjà appliquée à
    # l'empaquetage manuel, voir convert_incompatible_folder_to_cbz) - ce dossier n'est
    # rattaché à aucune série/volume en base à ce stade, series_id reste NULL.
    from blueprints.library.action_history import log_action
    if created:
        summary = ', '.join(c['filename'] for c in created)
        detail = f"{folder_label} → {summary} (auto, à l'extraction)"
    else:
        detail = folder_label
    log_action(
        'convert', None, folder_label, detail,
        success=len(errors) == 0,
        error='; '.join(errors) if errors else None,
    )


@library_bp.route('/api/import/incompatible-folder/convert-to-cbz/preview', methods=['GET'])
def preview_convert_incompatible_folder_to_cbz():
    """Calcule le regroupement par album probable d'un dossier de pages scannées en vrac
    (.jpg/.jpeg/.png/.webp), SANS RIEN ÉCRIRE - "options pour convertir une liste de
    fichier .jpg en .cbz. aussi il faudra faire attention que les noms des fichiers ne
    soient pas differents albums". Affiché à l'utilisateur pour confirmation avant
    l'exécution réelle (voir POST .../convert-to-cbz juste en dessous) plutôt que de
    créer silencieusement des .cbz sur un regroupement potentiellement faux - même
    prudence que partout ailleurs dans l'app où un matching pourrait se tromper
    (Bédéthèque, EBDZ...)."""
    import_root = request.args.get('import_root', '')
    relative_path = request.args.get('relative_path', '')

    folder_path, error = _resolve_incompatible_folder_path(import_root, relative_path)
    if error:
        payload, status = error
        return jsonify(payload), status

    try:
        entries = sorted(os.listdir(folder_path))
    except OSError as e:
        return jsonify({'error': str(e)}), 500

    image_files = [
        name for name in entries
        if os.path.isfile(os.path.join(folder_path, name))
        and os.path.splitext(name)[1].lower() in _LOOSE_IMAGE_EXTENSIONS
    ]
    if not image_files:
        return jsonify({'success': False, 'error': "Aucune image (.jpg/.jpeg/.png/.webp) dans ce dossier"}), 400

    groups = _apply_folder_name_fallback(_group_loose_images_by_album(image_files), folder_path)
    return jsonify({
        'success': True,
        # "c'etait quoi (sans titre).cbz ? je ne peux pas savoir" - un label + un compte
        # ne suffit pas pour juger si un regroupement est correct avant de confirmer;
        # sample_files (quelques noms réels, premiers ET derniers de chaque groupe -
        # utile pour repérer un regroupement "trop large" qui mélangerait le début d'un
        # album avec la fin d'un autre) permet de vérifier visuellement le contenu réel
        # d'un groupe, pas seulement son étiquette devinée.
        'groups': [
            {
                'label': g['label'],
                'file_count': len(g['files']),
                # "fenetre modale d'empaquetage est trop petit pour voir quelque chose...
                # ecrit d'abord les fichiers qui se ressemblent" - échantillon élargi (8+4
                # au lieu de 3+3) maintenant que la modale affiche chaque fichier sur sa
                # propre ligne dans une zone scrollable plutôt qu'une seule ligne condensée.
                'sample_files': (g['files'][:8] + (['…'] if len(g['files']) > 12 else []) + g['files'][-4:])
                                if len(g['files']) > 12 else g['files'],
            }
            for g in groups
        ],
        'other_file_count': len(entries) - len(image_files),
    })


@library_bp.route('/api/import/incompatible-folder/convert-to-cbz', methods=['POST'])
def convert_incompatible_folder_to_cbz():
    """Empaquette les images en vrac d'un dossier "incompatible" en un .cbz par album
    détecté (voir preview ci-dessus pour le calcul du regroupement, appelé une seconde
    fois ici à l'exécution plutôt que de faire confiance à un regroupement calculé côté
    client puis renvoyé - le dossier a pu changer entre les deux appels). Un .cbz par
    groupe est écrit directement DANS ce même dossier (le scan normal le retrouvera
    ensuite comme n'importe quel pack déjà empaqueté, via le nom de fichier ou le nom du
    dossier comme indice de série) ; les images d'origine ne sont supprimées qu'après
    vérification d'intégrité de l'archive produite (`testzip`), jamais avant - un .cbz
    corrompu ne doit jamais coûter les pages originales."""
    data = request.get_json() or {}
    import_root = data.get('import_root', '')
    relative_path = data.get('relative_path', '')

    folder_path, error = _resolve_incompatible_folder_path(import_root, relative_path)
    if error:
        payload, status = error
        return jsonify(payload), status

    try:
        entries = sorted(os.listdir(folder_path))
    except OSError as e:
        return jsonify({'error': str(e)}), 500

    image_files = [
        name for name in entries
        if os.path.isfile(os.path.join(folder_path, name))
        and os.path.splitext(name)[1].lower() in _LOOSE_IMAGE_EXTENSIONS
    ]
    if not image_files:
        return jsonify({'success': False, 'error': "Aucune image (.jpg/.jpeg/.png/.webp) dans ce dossier"}), 400

    groups = _apply_folder_name_fallback(_group_loose_images_by_album(image_files), folder_path)

    # "ajoute une option pour aussi empaqueter manuellement. je voudrais choisir
    # manuellement si ca marche pas automatiquement" - manual_groups (envoyé par la
    # modale d'ajustement manuel, import.js) redéfinit label/fusion/exclusion PAR-DESSUS
    # le regroupement recalculé ci-dessus (jamais celui, potentiellement périmé, affiché
    # au preview) - chaque entrée référence les groupes auto-détectés par INDEX
    # (`group_indices`, positions dans `groups` ci-dessus) plutôt que par liste de
    # fichiers, pour rester correct même si le dossier a légèrement changé entre le
    # preview et cette exécution (un fichier disparu dans l'intervalle est juste ignoré
    # sans faire échouer tout l'empaquetage). Un groupe auto-détecté non référencé par la
    # moindre entrée (l'utilisateur l'a laissé de côté / "Ignorer") reste tel quel en
    # pages non empaquetées, jamais supprimé.
    manual_groups = data.get('manual_groups')
    if manual_groups:
        resolved_groups = []
        for manual_group in manual_groups:
            label = (manual_group.get('label') or '').strip()
            indices = manual_group.get('group_indices') or []
            files = []
            for idx in indices:
                if isinstance(idx, int) and 0 <= idx < len(groups):
                    files.extend(groups[idx]['files'])
            if files:
                resolved_groups.append({
                    'label': label or (groups[indices[0]]['label'] if indices and 0 <= indices[0] < len(groups) else 'album'),
                    'files': sorted(set(files), key=_natural_sort_key),
                })
        groups = resolved_groups

    if not groups:
        return jsonify({'success': False, 'error': "Aucun groupe à empaqueter"}), 400

    created, errors = _package_loose_image_groups_as_cbz(folder_path, groups)

    # "lempaquetage devrait être dans l'historique" - ce dossier n'est encore rattaché à
    # aucune série/volume en base (comme convert_import_file juste au-dessus), donc
    # series_id/series_title restent NULL ; le détail liste chaque .cbz produit pour que
    # Historique puisse afficher ce qui a réellement été empaqueté.
    from blueprints.library.action_history import log_action
    folder_label = os.path.basename(folder_path.rstrip(os.sep)) or relative_path
    if created:
        summary = ', '.join(c['filename'] for c in created)
        detail = f"{folder_label} → {summary}"
    else:
        detail = folder_label
    log_action(
        'convert',
        None,
        folder_label,
        detail,
        success=len(errors) == 0,
        error='; '.join(errors) if errors else None,
    )

    return jsonify({'success': len(errors) == 0, 'created': created, 'errors': errors})


@library_bp.route('/api/import/incompatible-folder', methods=['DELETE'])
def delete_incompatible_folder():
    """Supprime un dossier de téléchargement "incompatible" (voir scan_import_directory,
    incompatible_folders) - un pack dont aucun fichier n'est dans un format supporté (ex:
    pages scannées en .jpg brutes au lieu de .cbz/.cbr déjà empaquetés) n'a rien à faire
    dans les répertoires surveillés, ce bouton permet de le nettoyer directement depuis
    /import plutôt que de devoir aller le supprimer à la main sur le disque."""
    data = request.get_json() or {}
    import_root = data.get('import_root', '')
    relative_path = data.get('relative_path', '')

    if not import_root or not relative_path:
        return jsonify({'error': 'import_root et relative_path requis'}), 400

    import_directories = current_app.config['IMPORT_DIRECTORIES']
    import_root = os.path.realpath(import_root)
    if import_root not in [os.path.realpath(d) for d in import_directories]:
        return jsonify({'error': "Répertoire d'import non autorisé"}), 403

    folder_path = os.path.realpath(os.path.join(import_root, relative_path))
    # != (pas juste commonpath) - refuse explicitement de supprimer le répertoire
    # surveillé lui-même si relative_path était vide/'.', même si un appelant buggé
    # l'envoyait par erreur (scan_import_directory ne génère jamais cette entrée pour la
    # racine, voir "root != import_path" dans sa boucle, mais mieux vaut une double garde
    # ici vu qu'on appelle rmtree juste après).
    if folder_path == import_root or os.path.commonpath([folder_path, import_root]) != import_root:
        return jsonify({'error': 'Chemin de dossier invalide'}), 403

    if not os.path.isdir(folder_path):
        # "Dossier introuvable" pour tome 8... j'ai supprimé l'entrée joe bar team mais
        # ça a gardé les fichiers internes" - bug réel: removePendingPack (import.js)
        # supprime chaque dossier "incompatible" sélectionné un par un, dans l'ordre de la
        # liste - si le dossier RACINE du pack ("Joe Bar Team [HD]") est aussi listé comme
        # incompatible (contenu direct non supporté) et supprimé AVANT un de ses propres
        # sous-dossiers ("Bonus/Tome 8"), ce sous-dossier disparaît déjà avec lui
        # (shutil.rmtree récursif) - la tentative suivante pour CE sous-dossier précis ne
        # trouvait donc plus rien et échouait avec une 404, alors que le résultat voulu
        # (ce dossier n'existe plus) est déjà atteint. Idempotent plutôt qu'une erreur:
        # l'absence du dossier EST le succès demandé, peu importe qui l'a fait disparaître.
        return jsonify({'success': True})

    try:
        shutil.rmtree(folder_path)
        return jsonify({'success': True})
    except OSError as e:
        return jsonify({'error': str(e)}), 500


def _convert_single_import_file(import_root, relative_path):
    """Coeur de la conversion d'un .pdf/.zip nu en .cbz, partagé par convert_import_file
    (une conversion, synchrone) et convert_import_files_batch (un lot, en arrière-plan -
    voir sa docstring). Retourne (file_dict, error_message, status_code) - file_dict est
    None en cas d'erreur, error_message est None en cas de succès."""
    if not import_root or not relative_path:
        return None, 'import_root et relative_path requis', 400

    import_directories = current_app.config['IMPORT_DIRECTORIES']
    import_root = os.path.realpath(import_root)
    if import_root not in [os.path.realpath(d) for d in import_directories]:
        return None, "Répertoire d'import non autorisé", 403

    try:
        filepath = resolve_within(os.path.join(import_root, relative_path), import_root)
    except UnsafePathError as e:
        return None, str(e), 403

    if not os.path.isfile(filepath):
        return None, 'Fichier introuvable', 404

    ext = os.path.splitext(filepath)[1].lower()
    if ext not in ('.pdf', '.zip'):
        return None, f"Conversion non supportée pour l'extension {ext}", 400

    # Voir _conversion_lock: sérialise avec toute AUTRE conversion (bouton groupé,
    # import automatique en cours) plutôt que de les laisser tourner en parallèle et
    # cumuler leur pic mémoire.
    with _conversion_lock:
        try:
            if ext == '.pdf':
                new_path = convert_pdf_to_cbz(filepath)
            else:
                new_path = convert_zip_to_cbz(filepath)
        except (PdfConversionError, ZipConversionError) as e:
            return None, str(e), 500

    scanner = LibraryScanner()
    new_filename = os.path.basename(new_path)

    # "il faudrait que dans l'historique on ai toutes les actions de l'utilisateur:
    # import, renommage, telechargement, conversion, suppression" - contrairement aux
    # autres actions journalisées ici (rename/delete/merge/renumber), ce fichier n'est
    # encore rattaché à AUCUNE série/volume (le fichier attend justement d'être importé) -
    # series_id/series_title restent donc NULL (colonnes nullable, voir
    # action_history.py), le nom du fichier fait office de "titre" affiché côté
    # Historique. Rien à rescanner côté Komga ici non plus (mêmes raisons que
    # delete_import_file/delete_incompatible_folder).
    from blueprints.library.action_history import log_action
    log_action('convert', None, new_filename, f"{os.path.basename(filepath)} → {new_filename} ({ext.lstrip('.')} → cbz)")
    return {
        # Même forme qu'une entrée de scan_import_directory (voir files_found) pour que
        # le frontend puisse remplacer l'entrée existante in-place sans reconstruire
        # toute la liste (ce qui perdrait les destinations déjà assignées aux AUTRES
        # fichiers du batch en cours).
        'filename': new_filename,
        'filepath': new_path,
        'relative_path': os.path.relpath(new_path, import_root),
        'file_size': os.path.getsize(new_path),
        'mtime': os.path.getmtime(new_path),
        'parsed': scanner.parse_filename(new_filename),
    }, None, 200


@library_bp.route('/api/import/convert', methods=['POST'])
def convert_import_file():
    """Convertit à la demande un .pdf ou un .zip nu sur le point d'être importé en
    .cbz (voir _convert_single_import_file ci-dessus) - jamais automatique, seulement
    déclenché par le bouton "Convertir en CBZ" de /import (voir file.convertible dans
    scan_import_directory). Le type de conversion est déduit de l'extension réelle du
    fichier sur disque, jamais d'un champ fourni par le client, pour ne jamais dépendre
    de ce qu'affirme l'appelant. Conservée pour une conversion unique synchrone - voir
    convert_import_files_batch pour plusieurs fichiers d'un coup, en arrière-plan."""
    data = request.get_json() or {}
    file_dict, error, status = _convert_single_import_file(data.get('import_root', ''), data.get('relative_path', ''))
    if error:
        return jsonify({'error': error}), status
    return jsonify({'success': True, 'file': file_dict})


@library_bp.route('/api/import/convert-batch', methods=['POST'])
def convert_import_files_batch():
    """Version "en arrière-plan" de /api/import/convert - "convertir to pdf only works
    if i am on the same page. if i quit the page it does not go background": l'ancienne
    UI (bouton groupé "Convertir en CBZ") pilotait la séquence de conversions DEPUIS LE
    NAVIGATEUR (une boucle JS qui attend chaque fetch avant de lancer le suivant, voir
    l'ancienne bulkConvertSelectedImportFiles côté import.js) - fermer l'onglet ou
    naviguer ailleurs interrompait cette boucle JS elle-même, laissant tous les fichiers
    pas encore lancés jamais convertis (contrairement au fichier EN COURS de conversion
    au moment de la fermeture, qui lui continue bien côté serveur - Flask threaded=True,
    voir app.py, ne tue pas le thread de la requête juste parce que le client s'est
    déconnecté; seul l'envoi de la réponse finale échoue alors silencieusement). Cette
    route lance UN SEUL thread serveur qui traite toute la liste dans l'ordre, sans plus
    jamais dépendre du navigateur pour enchaîner d'un fichier au suivant - répond
    immédiatement (fire-and-forget), le résultat de chaque conversion n'est visible
    qu'au prochain scan (Actualiser) une fois le thread terminé, exactement comme pour
    n'importe quel autre fichier déjà présent sur disque."""
    data = request.get_json() or {}
    files = data.get('files') or []
    if not files:
        return jsonify({'error': 'files requis'}), 400

    app = current_app._get_current_object()

    def _run_batch():
        with app.app_context():
            for f in files:
                _, error, _ = _convert_single_import_file(f.get('import_root', ''), f.get('relative_path', ''))
                if error:
                    print(f"✗ Erreur conversion en arrière-plan ({f.get('relative_path', '?')}): {error}")

    threading.Thread(target=_run_batch, daemon=True).start()
    return jsonify({'success': True, 'count': len(files)})


# "regarde le statut de la conversion si ce n'est pas en cbz. met une option pour
# automatiquement convertir pour cbz dans l'import automatique. si c'est desactivé
# l'utilisateur doit manuellement convertir": convertisseur commun cbr/rar/pdf/zip nu
# utilisé par execute_import (manuel) ET execute_auto_import - avant cette option, la
# conversion cbr->cbz était inconditionnelle et pdf/zip n'étaient jamais convertis à
# l'import (seulement via le bouton "Convertir en CBZ" de /import, voir
# convert_import_file). Gouverné par library_import_config.auto_convert_to_cbz - si
# désactivé, le fichier est importé tel quel et la conversion doit être faite à la main
# (bouton "Convertir en CBZ" de /import pour un fichier pas encore importé, action
# "Convertir en cbz" de la molette d'un tome pour un fichier déjà importé, voir
# buildVolumeActionsGearHtml côté library.js).
_IMPORT_CBZ_CONVERTERS = {
    'cbr': (convert_cbr_to_cbz, CbrConversionError),
    'rar': (convert_cbr_to_cbz, CbrConversionError),
    'pdf': (convert_pdf_to_cbz, PdfConversionError),
    'zip': (convert_zip_to_cbz, ZipConversionError),
}


def _maybe_convert_import_file_to_cbz(file_data, source_path, import_config):
    """Convertit `source_path` en cbz si `import_config.auto_convert_to_cbz` l'autorise
    et si son format est convertible - modifie `file_data` en place (filename/filepath/
    parsed.format) comme le faisaient les deux blocs qu'elle remplace. Retourne
    (nouveau chemin, message de conversion pour l'historique - vide si rien n'a été
    fait). Une conversion échouée (archive corrompue...) n'empêche jamais l'import: le
    fichier d'origine est alors importé tel quel, exactement comme avant.

    pdf: converti ici que l'import soit manuel OU automatique (scheduler périodique,
    déclenchement immédiat Telegram) - "never mentionned that. remove it", en réaction à
    la découverte que le scheduler automatique laissait silencieusement des PDF non
    convertis malgré `auto_convert_to_cbz` activé. Un pdf peut compter des centaines de
    pages à rendre une par une (plusieurs MINUTES par fichier, constaté en réel) et
    bloquait donc le scheduler en tâche de fond tout ce temps - accepté explicitement en
    échange d'un comportement cohérent avec ce que le réglage laisse attendre. Si un
    conflit de permissions reréapparaît avec un outil tiers qui surveille le même
    répertoire pendant la conversion (constaté une fois avec Syncthing, voir
    tempfile.mkstemp/0600 dans convert_pdf_to_cbz), c'est le signe qu'il faut revoir cette
    permission plutôt que réintroduire l'exclusion silencieuse."""
    if not import_config.get('auto_convert_to_cbz', True):
        return source_path, ''

    fmt = file_data['parsed'].get('format')
    entry = _IMPORT_CBZ_CONVERTERS.get(fmt)
    if not entry:
        return source_path, ''

    convert_fn, error_cls = entry
    # Voir _conversion_lock: sérialise avec toute AUTRE conversion (bouton "Convertir en
    # CBZ", groupé ou non) plutôt que de les laisser tourner en parallèle et cumuler leur
    # pic mémoire.
    with _conversion_lock:
        try:
            new_path = convert_fn(source_path)
        except error_cls as e:
            print(f"Conversion {fmt}->cbz échouée pour {file_data['filename']}: {e}")
            return source_path, ''

    file_data['filename'] = os.path.basename(new_path)
    file_data['filepath'] = new_path
    file_data['parsed']['format'] = 'cbz'
    return new_path, f'Converti {fmt.upper()} → CBZ'


@library_bp.route('/api/import/mark-manual', methods=['POST'])
def mark_import_file_manual_route():
    """Marque un fichier de /import comme assigné/corrigé manuellement ("si jai a faire
    manuelement un matching alors met un flag pas dimport automayique pour ce vokume.
    import manuel seulement") - appelé par assignDestination() (import.js) après toute
    assignation/correction manuelle réussie. L'import automatique planifié
    (scheduler.py:_auto_import) consulte ensuite cette table pour ne plus jamais
    reprendre ce fichier de lui-même tant qu'il n'a pas été réellement importé (ou
    supprimé du répertoire surveillé) - voir get_manual_override_filepaths."""
    data = request.get_json(silent=True) or {}
    filepath = data.get('filepath')
    if not filepath:
        return jsonify({'success': False, 'error': 'filepath manquant'}), 400

    # Même garde que execute_import: ne marquer que des fichiers réellement dans un
    # répertoire d'import surveillé, pas un chemin arbitraire fourni par le client.
    import_roots = current_app.config['IMPORT_DIRECTORIES']
    filepath_real = os.path.realpath(filepath)
    if not any(
        os.path.commonpath([filepath_real, os.path.realpath(r)]) == os.path.realpath(r)
        for r in import_roots
    ):
        return jsonify({'success': False, 'error': 'Chemin hors des répertoires d\'import autorisés'}), 400

    from .import_history import mark_import_file_manual
    if mark_import_file_manual(filepath):
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Erreur lors de l\'enregistrement'}), 500


@library_bp.route('/api/import/rescan-file', methods=['POST'])
def rescan_import_file_route():
    """"add a manual rescan. why the import automatic was triggered if the file was not
    good. a lot of error in the logs" - un fichier "Fichier corrompu" qui épuise son
    budget de tentatives rapprochées (voir AUTO_IMPORT_CORRUPTION_RETRY_DELAYS,
    scheduler.py) reste exclu de tout passage automatique jusqu'à son prochain cooldown
    de 30 min (AUTO_IMPORT_CORRUPTION_COOLDOWN_SECONDS) - incident réel ayant motivé ce
    bouton: un pack qBittorrent (3 volumes) tombé en "Fichier corrompu" à répétition
    pendant sa copie réseau, budget épuisé bien avant la fin du transfert réel, fichiers
    redevenus parfaitement valides quelques minutes plus tard mais plus aucune tentative
    automatique - jusqu'ici seul un redémarrage complet de l'app vidait
    self._failure_counts et leur redonnait une chance. Ce bouton force une
    revérification IMMÉDIATE sans attendre le cooldown: vide tout l'état d'échec mémorisé
    pour CE fichier (scheduler ET cache de validité de /import, voir
    _import_file_validity_cache plus haut) puis relance tout de suite le test
    d'intégrité réel. "don't make a difference between the download type" - générique,
    keyé uniquement par filepath, sans savoir ni se soucier d'où vient le fichier
    (Telegram/qBittorrent/aMule/EBDZ)."""
    data = request.get_json(silent=True) or {}
    filepath = data.get('filepath')
    if not filepath:
        return jsonify({'success': False, 'error': 'filepath manquant'}), 400

    import_roots = current_app.config['IMPORT_DIRECTORIES']
    filepath_real = os.path.realpath(filepath)
    if not any(
        os.path.commonpath([filepath_real, os.path.realpath(r)]) == os.path.realpath(r)
        for r in import_roots
    ):
        return jsonify({'success': False, 'error': 'Chemin hors des répertoires d\'import autorisés'}), 400

    if not os.path.exists(filepath):
        return jsonify({'success': False, 'error': 'Fichier introuvable sur le disque'}), 404

    from .scheduler import library_import_scheduler
    library_import_scheduler._failure_counts.pop(filepath, None)
    library_import_scheduler._failure_last_error.pop(filepath, None)
    library_import_scheduler._failure_last_at.pop(filepath, None)
    library_import_scheduler._file_size_history.pop(filepath, None)

    _import_file_size_history.pop(filepath, None)
    for key in [key for key in list(_import_file_validity_cache.keys()) if key[0] == filepath]:
        _import_file_validity_cache.pop(key, None)

    from blueprints.settings.routes import _check_volume_file_validity
    parsed = LibraryScanner().parse_filename(os.path.basename(filepath))
    error = _check_volume_file_validity(filepath, parsed.get('format'))
    current_size = os.path.getsize(filepath)
    _import_file_size_history[filepath] = current_size
    _import_file_validity_cache[(filepath, current_size)] = error

    return jsonify({'success': True, 'valid': error is None, 'error': error})


# Sous-ensemble de file_data['parsed'] à persister dans import_history_files.
# parsed_volume_json ("historique des imports n'indique pas le numero de volume a coté
# du nom de l'album quand c'est importé manuelement") - le numéro/type de tome
# RÉELLEMENT retenu pour cet import, y compris après une correction manuelle
# (volume_override, voir plus haut dans execute_import) que le nom de fichier seul ne
# permet justement pas de retrouver plus tard par un simple re-parsing (voir
# get_operation_details côté import_history.py).
def _volume_fields_for_history(parsed):
    return {
        k: parsed.get(k) for k in
        ('volume', 'is_integral', 'integral_number', 'is_hs', 'hs_number', 'is_episode', 'episode_number')
    }


def _should_preserve_import_source(source_path):
    """True si le fichier source doit être COPIÉ (préservé) plutôt que déplacé -
    déterminé par un test d'écriture réel sur son dossier parent, pas par une liste de
    chemins connus codée en dur. "should we add an option to say move or copy file" -
    délibérément PAS une option Settings ni un simple contrôle du chemin (ex: "c'est le
    dossier aMule") : que le fichier source soit supprimable est un fait du système de
    fichiers (montage NFS en lecture seule, permissions), pas une préférence utilisateur
    qui pourrait être mal réglée (ex: "move" choisi par erreur sur une source read-only
    ferait échouer l'import). S'adapte automatiquement si une AUTRE source devient
    read-only plus tard (ex: le dossier aMule lui-même, actuellement un montage NFS
    ro - voir docker-compose.yml), sans nouveau code ni réglage. os.access plutôt qu'un
    essai d'écriture réel: suffisant ici (pas de fenêtre de concurrence critique - le
    pire cas d'une réponse os.access incorrecte est un échec explicite de shutil.move
    plus bas, pas une corruption silencieuse)."""
    try:
        return not os.access(os.path.dirname(os.path.realpath(source_path)), os.W_OK)
    except OSError:
        return True


def _maybe_complete_tracking_after_move(source_path, destination, outcome='imported', source_was_copied=False):
    """"add an explicit import result state... skipped, cancelled" - outcome distingue
    un tome RÉELLEMENT importé/remplacé ('imported', mark_download_imported) d'un
    doublon reconnu et volontairement ignoré ('skipped', mark_download_skipped) - avant
    ce paramètre, un "Doublon ignoré" réutilisait 'imported' par commodité (rien
    n'avait alors été importé), rendant impossible de distinguer les deux cas
    a posteriori depuis active_downloads.status seul (il fallait recouper
    import_history_files.action). Les deux restent des états TERMINAUX équivalents
    pour la logique de nettoyage ci-dessous (un pack encore non vide n'est retiré dans
    aucun des deux cas) - seul le statut final posé change.

    "pourquoi les fichiers importés ne sont pas retirés de la liste de l'import" -
    bug réel: un pack (plusieurs fichiers sous le même active_downloads, voir
    destination['tracking_id']) n'était jamais nettoyé une fois TOUS ses fichiers
    importés. Le seul nettoyage existant, clear_pending_downloads_by_filenames
    (scan_import_directory), ne matche que par NOM DE FICHIER exact contre le titre
    suivi - qui ne ressemble jamais aux noms individuels des tomes extraits d'un pack
    ("Prince de la Nuit PACK" vs "Prince de la nuit - Tome 3.cbz"). Une fois le DERNIER
    fichier du pack déplacé hors de son dossier, plus aucun scan ultérieur ne retrouve
    quoi que ce soit à comparer - la ligne "en attente" restait donc fantôme
    indéfiniment. Ici, on connaît avec certitude à quel active_downloads ce fichier
    appartenait (tracking_id, posé par find_active_download_destination/
    find_active_download_destination_by_torrent_name) - si son dossier ne contient plus
    AUCUN fichier supporté après ce déplacement, ce téléchargement n'a plus rien à
    offrir : sa ligne est retirée immédiatement (remove_pending_download) plutôt que de
    compter sur un matching par nom qui ne fonctionnera jamais pour ce cas.

    Ne s'applique qu'à un fichier venu d'un DOSSIER dédié (pack/torrent multi-fichiers) -
    un fichier isolé à la racine d'un répertoire surveillé (aMule/Telegram) est déjà
    couvert par le nettoyage par nom de fichier existant, et "le dossier" y désignerait à
    tort la racine entière, partagée par des dizaines d'autres téléchargements sans
    rapport."""
    tracking_id = destination.get('tracking_id') if destination else None
    if not tracking_id:
        return
    try:
        from blueprints.missing_monitor.downloader import (
            mark_download_imported, mark_download_skipped, mark_download_completed
        )
        finalize = mark_download_skipped if outcome == 'skipped' else mark_download_imported

        # Une source en lecture seule ne peut pas être vidée par l'import. Pour une ligne
        # de suivi représentant un seul fichier, le volume enregistré dans la bibliothèque
        # est donc la preuve de fin ; attendre la disparition de la source la laisserait
        # indéfiniment bloquée dans ``importing``. Les packs restent ``completed`` jusqu'à
        # traitement de leurs autres membres : marquer la ligne partagée comme importée
        # après le premier fichier copié masquerait le reste du pack aux scans suivants.
        if source_was_copied:
            db_path = current_app.config.get('DATABASE')
            conn = sqlite3.connect(db_path, timeout=30.0) if db_path else None
            row = conn.execute(
                'SELECT is_pack FROM active_downloads WHERE id = ?', (tracking_id,)
            ).fetchone() if conn else None
            if conn:
                conn.close()
            if not row or not bool(row[0]):
                finalize(tracking_id)
            else:
                mark_download_completed(tracking_id)
            return

        import_directories = current_app.config.get('IMPORT_DIRECTORIES', [])
        top_level_dir = None
        for import_root in import_directories:
            rel = os.path.relpath(source_path, import_root)
            if rel == os.pardir or rel.startswith(os.pardir + os.sep):
                continue
            parts = rel.split(os.sep)
            if len(parts) > 1:
                top_level_dir = os.path.join(import_root, parts[0])
            break
        if not top_level_dir or not os.path.isdir(top_level_dir):
            # Le client torrent peut supprimer le conteneur juste après le déplacement
            # du dernier fichier. Le sort de ce fichier (importé ou reconnu doublon)
            # suffit alors à prouver que ce suivi n'a plus rien à offrir - "it should
            # have in db the all cycle... imported": marqué terminal plutôt que
            # supprimé, pour garder une trace durable de ce téléchargement suivi (mêmes
            # principe et no-expiry que 'completed'/'failed', voir get_pending_downloads)
            # au lieu de le faire disparaître silencieusement de la base.
            finalize(tracking_id)
            return

        supported_extensions = {'.cbz', '.cbr', '.zip', '.rar', '.pdf'}
        for root, _dirs, files in os.walk(top_level_dir):
            if any(os.path.splitext(f)[1].lower() in supported_extensions for f in files):
                return  # il reste au moins un tome à importer, ne rien faire

        # Le client peut laisser le conteneur torrent vide après le déplacement du
        # dernier fichier. Nettoyer uniquement les répertoires réellement vides : un
        # fichier auxiliaire ou un tome non supporté empêche volontairement toute
        # suppression, afin de ne jamais effacer un contenu encore présent.
        for root, dirs, files in os.walk(top_level_dir, topdown=False):
            if not dirs and not files:
                try:
                    os.rmdir(root)
                except OSError:
                    pass
        if os.path.isdir(top_level_dir) and not os.listdir(top_level_dir):
            try:
                os.rmdir(top_level_dir)
            except OSError:
                pass

        finalize(tracking_id)
    except Exception as e:
        # Best-effort: un souci ici ne doit jamais faire échouer l'import lui-même,
        # seule la ligne "en attente" resterait affichée un peu plus longtemps que
        # nécessaire.
        print(f"Erreur nettoyage du téléchargement suivi #{tracking_id} après import: {e}")


def _execute_import_batch(files_to_import, *, operation_type, lock_timeout, strict_missing_file, notify_source):
    """Cœur partagé de l'exécution d'un import - déplace/convertit/insère chaque fichier
    de `files_to_import` vers sa destination, met à jour Historique/stats/renommage/
    Komga/EBDZ. Factorisé le 2026-07-25 ("import a été overengineered, review et
    simplifie") : execute_import (manuel, route HTTP) et execute_auto_import (planificateur
    + déclenchement immédiat Telegram) étaient ~700 lignes chacune, quasi identiques,
    copiées-collées plutôt que partagées - le bug de fuite de connexion SQLite trouvé plus
    tôt dans cette même session existait dans LES DEUX indépendamment, et plusieurs
    commentaires ("même correctif que execute_import, voir son commentaire") documentaient
    déjà le fait qu'un fix appliqué à l'un devait être pensé à recopier dans l'autre à la
    main - source directe de dérive entre les deux chemins (ex: 'parsed_volume' présent
    dans les logs Historique côté manuel mais oublié côté auto avant ce correctif).

    Différences entre les deux appelants, réduites à des paramètres explicites plutôt qu'à
    deux implémentations divergentes :
    - lock_timeout: 180s (manuel, bloque une requête HTTP interactive) vs 60s (auto, se
      représente de lui-même 5s plus tard - voir library/scheduler.py, pas la peine de
      bloquer longtemps un thread planifié).
    - strict_missing_file: un fichier disparu entre le scan et l'exécution compte comme un
      VRAI échec côté manuel (l'utilisateur a cliqué sur une sélection précise) mais est
      ignoré silencieusement côté auto (presque toujours le signe qu'un appel concurrent -
      scan périodique + déclenchement immédiat Telegram, tous deux protégés par le même
      _import_execution_lock mais avec chacun sa propre liste déjà scannée avant de
      l'acquérir - vient de l'importer avec succès entre-temps).
    - notify_source: libellé ('manuel'/'automatique') pour la notification Telegram de fin
      d'import (_notify_import_completed).

    Tout le reste (résolution de série/dossier, remplacement par priorité de format,
    détection de doublon, transformation de placeholder Bédéthèque, écriture ComicInfo
    DB-first, renommage, sync Komga/EBDZ, opération vide supprimée plutôt que gardée comme
    "réussie" sans rien dedans) est strictement identique pour les deux, y compris le
    volume_override (correction manuelle du tome) et la création de nouvelle série
    matchée Bédéthèque (new_series_with_bedetheque) - ce dernier bloc ne se déclenche
    jamais côté auto en pratique aujourd'hui (find_active_download_destination/
    find_auto_assign_destination ne posent jamais is_new_series=True), mais le garder
    commun évite justement qu'une future évolution de l'un des deux appelants ait à
    redécouvrir/recopier cette logique.

    Retourne (lock_acquired: bool, operation_id: str|None, stats: dict). stats contient
    toujours imported_count/replaced_count/skipped_count/failed_count/failures/
    cleaned_directories, plus 'error' (message, seulement en cas d'échec de l'opération
    entière) si applicable. lock_acquired=False signifie qu'aucun travail n'a été tenté du
    tout (un autre import tournait déjà) - à distinguer d'un stats['error'] (l'opération a
    démarré mais a planté en cours de route)."""
    import uuid
    from .import_history import (
        log_import_operation, log_import_file, update_import_operation, log_import_file_started,
        update_import_file, delete_import_operation, claim_import_files,
        release_import_file, release_import_files
    )
    from blueprints.missing_monitor.downloader import mark_download_importing, mark_download_completed

    # Slow preparation runs without the shared scan lock. Atomic per-file claims below
    # prevent duplicate work across scheduler/manual triggers; the shared lock is acquired
    # only for each file's short final destination/SQLite mutation.
    import_roots = current_app.config['IMPORT_DIRECTORIES']
    import_config = load_library_import_config()
    hardlink_requested = import_config.get('import_mode') == 'hardlink'
    cleanup_stale_staging(_import_staging_directory())
    operation_id = None
    imported_count = 0
    replaced_count = 0
    skipped_count = 0
    failed_count = 0
    failures = []
    cleaned_dirs = 0
    # "il faut faire 3 essais. pas plus" - suivi separe du compteur failed_count/failures
    # (qui restent inconditionnels, le scheduler en a besoin pour son propre backoff par
    # fichier, voir suppress_log ci-dessous): True des qu AU MOINS un echec du lot est
    # encore un retry silencieux de corruption dans son budget - dans ce cas, ne PAS
    # creer/finaliser de ligne import_history du tout pour cette tentative-ci, plutot que
    # d en dupliquer une identique a chaque passage du scheduler (5-10s) jusqu a
    # epuisement du budget.
    any_suppressed_failure = False

    try:
        # Generate the operation row; each source path is claimed atomically immediately
        # before its own preparation and released in that iteration's finally block.
        operation_id = str(uuid.uuid4())
        log_import_operation(operation_id, operation_type, ','.join(import_roots), 'started')

        # Liste pour accumuler les logs à enregistrer (pour éviter les problèmes de verrou SQLite)
        logs_to_record = []
        # Finaliser active_downloads uniquement après la validation de la transaction des
        # volumes correspondante. Appeler le suivi alors que la source a été déplacée ou
        # copiée mais avant l'écriture en base pourrait laisser une ligne ``imported`` sans
        # volume après un rollback.
        tracking_finalizations = []

        scanner = LibraryScanner()
        new_volume_ids = []
        # "normalement des qu'on crée une série ça doit être crée [avec ses métadonnées]"
        # - série_id -> titre local, pour les séries fraîchement créées par CET import qui
        # ont un bedetheque_url (déjà matchées côté UI, voir openBedethequeImportModal
        # dans import.js) : déclenche l'écriture complète du ComicInfo une fois la boucle
        # d'import terminée (voir plus bas), au lieu de laisser la série sans aucune
        # métadonnée tant que l'utilisateur ne clique pas "MAJ métadonnées" à la main.
        new_series_with_bedetheque = {}

        # Traiter chaque fichier
        for file_data in files_to_import:
            file_id = None  # défini par log_import_file_started plus bas - None si l'exception (bloc except) survient avant cette ligne
            conn = None  # ouverte plus bas (série existante/nouvelle) - voir le bloc except: fermeture/rollback garantis même si l'exception survient après le déplacement du fichier mais avant le commit
            rollback_source_path = None
            moved_target_path = None
            # The original is intentionally preserved until the destination file and
            # SQLite transaction are both durable. `source_was_copied` retains the
            # historical meaning used by tracking finalization (read-only source).
            source_was_copied = False
            original_source_path = None
            staged_source_path = None
            per_file_lock_acquired = False
            tracking_finalization = None
            old_backup_path = None
            existing_file_path = None
            try:
                original_source_path = file_data['filepath']
                if original_source_path not in claim_import_files(
                    [original_source_path], operation_id
                ):
                    continue
                source_path = original_source_path
                destination = file_data.get('destination')
                source_size = file_data.get('file_size', 0)

                if not destination:
                    failed_count += 1
                    failures.append({
                        'file': file_data.get('filename', source_path),
                        'filepath': source_path,
                        'error': 'Pas de destination définie'
                    })
                    continue

                # "it should have in db the all cycle of importing... importing" - un
                # téléchargement suivi (destination.tracking_id, posé par
                # find_active_download_destination/_by_torrent_name) passe de 'completed'
                # ("waiting to import" - fichier arrivé, voir clear_pending_downloads_by_
                # filenames/mark_download_completed) à 'importing' pile à cet instant :
                # avant, active_downloads ne distinguait pas "en attente de son tour" de
                # "en cours de traitement RIGHT NOW", pourtant une conversion PDF peut
                # légitimement prendre plusieurs minutes (voir CLAUDE.md) pendant
                # lesquelles ce fichier précis est bloqué ici. Remis à 'completed' dans le
                # bloc except plus bas si CE fichier échoue, pour qu'il soit repris au
                # prochain passage plutôt que de rester bloqué en 'importing' pour de bon.
                tracking_id = destination.get('tracking_id')
                if tracking_id:
                    mark_download_importing(tracking_id)

                # Le fichier peut avoir disparu entre le scan qui a rempli files_to_import
                # et ce point précis (déjà importé automatiquement par le scheduler
                # entre-temps, ou supprimé depuis un autre onglet) - voir strict_missing_file
                # dans la docstring de cette fonction pour le traitement différent
                # manuel/auto de ce même cas.
                if not os.path.exists(source_path):
                    if strict_missing_file:
                        failed_count += 1
                        failures.append({
                            'file': file_data.get('filename', source_path),
                            'filepath': source_path,
                            'error': "Fichier introuvable - déjà importé ou supprimé depuis le dernier scan"
                        })
                    continue

                # Ligne "en cours" dès que le nom du fichier est connu, AVANT tout
                # traitement - "Import en cours... detail lequel": sans ça, file_names
                # (get_import_history) reste vide tant que l'opération tourne, puisque
                # logs_to_record n'est écrit en base qu'à la toute fin (voir plus bas).
                # Mise à jour EN PLACE (update_import_file) une fois le sort du fichier
                # connu, pas une seconde ligne.
                file_id = log_import_file_started(operation_id, file_data['filename'])

                # Correction manuelle du numéro/type de tome ("je ne peux pas ajouter le
                # volume car c'est une intégrale") - le nom de fichier ne porte pas
                # toujours l'info (ex: "Monstre - Enki Bilal.cbz" ne mentionne ni tome ni
                # INT), sans quoi ce fichier ne pouvait être assigné qu'à un numéro erroné
                # ou pas du tout. Envoyé par la modale d'assignation (voir
                # openDestinationModal/assignDestination dans import.js) uniquement si
                # l'utilisateur a explicitement corrigé le type - remplace les valeurs
                # DEVINÉES par parse_filename avant toute la suite (recherche d'un
                # placeholder existant ET écriture finale utilisent toutes deux
                # file_data['parsed'], une seule correction ici suffit pour les deux).
                # Sans effet côté auto-import: destination n'a jamais de volume_override
                # (aucune UI de correction manuelle pour un fichier auto-détecté).
                volume_override = destination.get('volume_override')
                if volume_override:
                    parsed_override_target = file_data['parsed']
                    for field in ('volume', 'is_integral', 'integral_number', 'is_hs', 'hs_number', 'is_episode', 'episode_number',
                                  # "in rugby there is a file BO4. i cannot change in the
                                  # dropdown. nor I can select it" - un bonus/promo sans
                                  # numéro (voir is_special, CLAUDE.md "Anything numbered/
                                  # typed neither as a plain tome, an intégrale, an HS, nor
                                  # an episode gets classified is_special") n'avait aucune
                                  # correction possible: les 4 types proposés ici (Tome/
                                  # Intégrale/HS/Épisode) exigent tous un numéro qui n'existe
                                  # pas pour ce genre de fichier.
                                  'is_special', 'special_label'):
                        if field in volume_override:
                            parsed_override_target[field] = volume_override[field]

                # Le fichier source doit être physiquement dans un répertoire d'import
                # configuré: sans ce contrôle, un appel direct à cette API (sans passer par
                # l'UI) pourrait faire déplacer/écraser n'importe quel fichier accessible au
                # conteneur en fournissant un filepath arbitraire
                source_path_real = os.path.realpath(source_path)
                if not any(
                    os.path.commonpath([source_path_real, os.path.realpath(r)]) == os.path.realpath(r)
                    for r in import_roots
                ):
                    raise UnsafePathError(f"Fichier source hors des répertoires d'import autorisés: {source_path}")

                # "pour l'import doublon ignoré tu devrais faire la recherche d'abord si
                # c'est un doublon. ca devrait etre rapide. la ca prend 5 minutes pour
                # verifier" - bug réel: la conversion cbr/pdf/zip -> cbz (voir plus bas,
                # jusqu'à plusieurs MINUTES pour un pdf, _maybe_convert_import_file_to_cbz)
                # tournait AVANT toute comparaison de taille, payée en entier même pour un
                # fichier qui allait de toute façon être jeté comme doublon juste après -
                # exactement le cas d'un fichier déjà possédé qui traîne encore côté aMule
                # (montage read-only, jamais nettoyé - voir _should_preserve_import_source):
                # reconverti pour rien à CHAQUE cycle du scheduler. Connexion SQLite dédiée
                # et refermée immédiatement plutôt que d'ouvrir tôt celle réutilisée plus
                # bas: éviter de la garder ouverte pendant une conversion qui peut durer
                # plusieurs minutes (contention "database is locked" avec le reste de
                # l'app). Une "nouvelle série" (is_new_series) n'a par définition ENCORE
                # AUCUN volume existant - jamais un doublon possible, pas la peine de
                # chercher ni d'ouvrir de connexion pour ce cas.
                if not _import_execution_lock.acquire(timeout=lock_timeout):
                    raise TimeoutError("Pré-vérification reportée: un scan/import modifie déjà la bibliothèque")
                per_file_lock_acquired = True
                if not destination.get('is_new_series'):
                    early_conn = sqlite3.connect(current_app.config['DATABASE'], timeout=120.0, check_same_thread=False)
                    try:
                        early_existing = _find_existing_volume_for_import(
                            early_conn.cursor(), destination['series_id'], file_data['parsed'],
                            single_album=bool(destination.get('is_single_album'))
                        )
                    finally:
                        early_conn.close()
                    _, early_existing_path, early_existing_size, _ = early_existing
                    if early_existing_path and os.path.exists(early_existing_path) \
                            and not destination.get('force_replace') and not is_better_volume(source_size, early_existing_size):
                        # "once Doublon ignoré -> state will be imported. so nothing
                        # should go after that" - bug réel: _maybe_complete_tracking_
                        # after_move (qui sort tracking_id d'active_downloads.status=
                        # 'importing', posé juste avant conversion) ne tournait QUE dans
                        # la branche "source supprimée" - pour une source préservée
                        # (aMule, non-inscriptible), le fichier ne quitte jamais
                        # 'importing', réévalué (et donc reclaqué en 'importing') à
                        # chaque cycle du scheduler sans jamais en ressortir. Appelée
                        # maintenant inconditionnellement: la source est supprimée ou
                        # non selon le cas, le suivi est TOUJOURS clos une fois le sort
                        # du fichier connu.
                        source_was_copied = _should_preserve_import_source(original_source_path) or hardlink_requested
                        if not source_was_copied and os.path.exists(original_source_path):
                            os.remove(original_source_path)
                        if staged_source_path and os.path.exists(staged_source_path):
                            os.remove(staged_source_path)
                            try:
                                os.rmdir(os.path.dirname(staged_source_path))
                            except OSError:
                                pass
                        tracking_finalizations.append((original_source_path, destination, 'skipped', source_was_copied))
                        action_taken = 'skipped_duplicate'
                        skipped_count += 1
                        logs_to_record.append({
                            'file_id': file_id,
                            'operation_id': operation_id,
                            'filename': file_data['filename'],
                            'source_path': original_source_path or source_path,
                            'destination_path': '',
                            'series_title': destination.get('series_title', ''),
                            'action': 'skipped',
                            'status': 'success',
                            'message': 'Doublon ignoré',
                            'parsed_volume': _volume_fields_for_history(file_data['parsed'])
                        })
                        continue

                _import_execution_lock.release()
                per_file_lock_acquired = False

                # Copy to a local atomic staging directory, optionally convert, and
                # validate in a killable subprocess. No NFS/archive/PDF work runs in the
                # Flask process. The original is untouched until the final DB commit.
                source_was_copied = _should_preserve_import_source(original_source_path) or hardlink_requested
                original_format = (file_data['parsed'].get('format') or Path(original_source_path).suffix.lstrip('.')).lower()
                preparation = prepare_import_file(
                    original_source_path,
                    file_data['parsed'].get('format'),
                    import_config.get('auto_convert_to_cbz', True),
                    _import_staging_directory(),
                    timeout=IMPORT_STEP_TIMEOUT_SECONDS,
                )
                staged_source_path = preparation['path']
                # Converted files cannot be hardlinked; use the staged result instead.
                hardlink_source_path = (
                    original_source_path
                    if hardlink_requested and preparation.get('format', '').lower() == original_format
                    else None
                )
                source_path = staged_source_path
                file_data['filename'] = preparation['filename']
                file_data['parsed']['format'] = preparation['format']
                conversion_message = preparation.get('conversion_message', '')
                integrity_error = preparation.get('validation_error')
                try:
                    _import_file_validity_cache[(
                        original_source_path, os.path.getsize(original_source_path)
                    )] = integrity_error
                except OSError:
                    pass
                if integrity_error and not file_data.get('forceImport'):
                    raise ValueError(f"Fichier corrompu, import refusé : {integrity_error}")
                elif integrity_error:
                    conversion_message = (conversion_message + ' - ' if conversion_message else '') \
                        + f"Importé malgré une corruption connue : {integrity_error}"

                # The shared scan/import lock now covers only final destination and DB
                # mutation for this one file. Slow staging/conversion/validation above
                # does not block another series scan or monopolize an entire batch.
                if not _import_execution_lock.acquire(timeout=lock_timeout):
                    raise TimeoutError("Finalisation reportée: un scan/import modifie déjà la bibliothèque")
                per_file_lock_acquired = True
                rollback_source_path = original_source_path

                # "so you should match it to volume 38 not volume 2 as the title says" -
                # visibilité dans Historique quand apply_tracked_volume_and_gate a corrigé
                # le numéro de tome au profit de celui suivi (recherche/remplacement) plutôt
                # que de laisser croire silencieusement que le fichier portait ce numéro
                # depuis le départ.
                overridden_from = file_data['parsed'].get('tracked_volume_overridden_from')
                if overridden_from is not None:
                    conversion_message = (conversion_message + ' - ' if conversion_message else '') \
                        + f"Numéro de tome corrigé : {file_data['parsed'].get('volume')} (recherché) au lieu de {overridden_from} (nom de fichier)"

                # Récupérer ou créer la série
                conn = sqlite3.connect(current_app.config['DATABASE'], timeout=120.0, check_same_thread=False)
                cursor = conn.cursor()

                if destination.get('is_new_series'):
                    # Créer une nouvelle série
                    series_title = sanitize_path_component(destination['series_title'], 'Nom de série')
                    library_id = destination['library_id']

                    # Ne jamais faire confiance au library_path fourni par le client: on le
                    # re-résout depuis la base à partir du library_id, sans quoi un
                    # library_path arbitraire permettrait de créer/déplacer des fichiers
                    # n'importe où sur le système accessible au conteneur
                    cursor.execute('SELECT path FROM libraries WHERE id = ?', (library_id,))
                    library_row = cursor.fetchone()
                    if not library_row:
                        raise UnsafePathError(f"Bibliothèque introuvable: {library_id}")
                    library_path = library_row[0]

                    # Créer le dossier de la série DANS le dossier de la bibliothèque
                    series_path = resolve_within(os.path.join(library_path, series_title), library_path)
                    os.makedirs(series_path, exist_ok=True)

                    # Éviter les doublons: si plusieurs fichiers d'un même import sont
                    # marqués "nouvelle série" avec le même titre, réutiliser la série
                    # créée par le premier fichier plutôt que d'en insérer une deuxième.
                    # Vérifie AUSSI par path (voir create_series ci-dessus pour le
                    # contexte complet: régression réelle #Lesmémés 589/1084, un titre
                    # reformulé différemment - même dossier disque - créait une
                    # deuxième série vide) - le path est la seule identité stable,
                    # jamais le titre.
                    cursor.execute('''
                        SELECT id FROM series WHERE library_id = ? AND (title = ? OR path = ?)
                    ''', (library_id, series_title, series_path))
                    existing_series = cursor.fetchone()

                    if existing_series:
                        series_id = existing_series[0]
                    else:
                        cursor.execute('''
                            INSERT INTO series (library_id, title, path, total_volumes, missing_volumes, has_parts)
                            VALUES (?, ?, ?, 0, '[]', 0)
                        ''', (library_id, series_title, series_path))
                        series_id = cursor.lastrowid

                        # Le match Bédéthèque choisi côté client au moment de créer la
                        # série ("➕ Créer une nouvelle série" + icône Bédéthèque de la
                        # modale, voir openBedethequeImportModal dans import.js) était
                        # jusqu'ici silencieusement perdu: execute_import créait bien la
                        # ligne series, mais bedetheque_url n'était jamais lu depuis
                        # destination. "normalement des qu'on crée une série ça doit être
                        # crée [avec ses métadonnées]" - le scrape complet + l'écriture
                        # ComicInfo sont maintenant déclenchés juste après la boucle
                        # d'import (voir new_series_with_bedetheque plus bas), en
                        # arrière-plan (même mécanisme que le bouton "MAJ métadonnées"):
                        # pas ici même, dans la boucle, où le fichier vient tout juste
                        # d'être déplacé et n'a pas encore sa ligne volumes committée.
                        bedetheque_url = destination.get('bedetheque_url')
                        if bedetheque_url:
                            cursor.execute('UPDATE series SET bedetheque_url = ? WHERE id = ?', (bedetheque_url, series_id))
                            new_series_with_bedetheque[series_id] = {'title': series_title, 'url': bedetheque_url}

                    # Enregistrer le series_id dans destination pour mise à jour ultérieure
                    destination['series_id'] = series_id
                    target_dir = series_path
                else:
                    # Utiliser une série existante
                    series_id = destination['series_id']
                    series_title = destination['series_title']

                    # "import il n'y a pas de renommage. import c'est juste une copie" -
                    # RÉGRESSION CORRIGÉE (2026-09-03): ce code recalculait AUPARAVANT le
                    # dossier cible depuis le titre + template à CHAQUE import, au lieu
                    # d'utiliser series.path déjà stocké en base. Le raisonnement d'origine
                    # ("ne pas faire confiance au path en base, il peut être incorrect")
                    # partait d'un vrai bug ("XIII Trilogy tome 3 not added to the right
                    # folder") mais la corrigeait au mauvais endroit: un IMPORT n'est
                    # jamais l'occasion de renommer/déplacer une série existante - c'est
                    # le rôle exclusif et séparé de _rename_series_folder (bouton dédié,
                    # déplace réellement les fichiers). Recalculer un nom de dossier à
                    # l'import, à partir d'un TITRE reformulable à tout moment (rescan,
                    # match Bédéthèque...), ne peut par construction correspondre au
                    # dossier existant que par coïncidence de format - c'est exactement
                    # le mécanisme qui a produit TOUS les doublons de série découverts et
                    # nettoyés ce jour (589/1084 "#Lesmémés / Les Mémés" vs "#Lesmémés -
                    # Les Mémés", et 21 autres groupes identiques). Un import est une
                    # copie vers le dossier DÉJÀ ATTACHÉ à la série, point final.
                    cursor.execute('SELECT path FROM series WHERE id = ?', (series_id,))
                    series_row = cursor.fetchone()
                    if not series_row or not series_row[0]:
                        raise UnsafePathError(f"Série introuvable ou sans dossier: {series_id}")
                    target_dir = series_row[0]

                    # os.path.isdir plutôt que resolve_within ici: series.path vient de
                    # la base (pas d'une entrée utilisateur à valider), mais un dossier
                    # peut avoir été supprimé/déplacé manuellement hors de l'app depuis le
                    # dernier scan - le recréer silencieusement au même endroit reste sûr
                    # et attendu (comportement identique à l'ancien code sur ce point).
                    os.makedirs(target_dir, exist_ok=True)

                # Construire le chemin de destination
                target_path = os.path.join(target_dir, file_data['filename'])

                # Vérifier si un fichier/placeholder existe déjà pour ce même tome
                import shutil
                existing_volume_id, existing_file_path, existing_file_size, existing_format = \
                    _find_existing_volume_for_import(cursor, series_id, file_data['parsed'], single_album=bool(destination.get('is_single_album')))

                action_taken = None
                placeholder_volume_id = None
                # Comicinfo/cover du tome remplacé, capturés avant sa suppression - une
                # meilleure qualité de fichier ne doit pas faire perdre les métadonnées
                # déjà connues (Bédéthèque ou autre) du tome qu'il remplace (voir plus bas,
                # utilisé comme repli au moment de l'INSERT du nouveau fichier)
                carried_comicinfo = None
                carried_cover_path = None

                # Si un fichier existe déjà pour ce volume
                if existing_file_path and os.path.exists(existing_file_path):
                    # "I did remplacer le fichier but it did not work because got
                    # duplicate error. this should be bypassed when I click remplacer" -
                    # is_better_volume (comparaison de taille) est pensée pour un import
                    # AUTOMATIQUE sans humain dans la boucle, où un fichier plus petit/égal
                    # est très probablement un doublon sans intérêt. "Remplacer le fichier"
                    # (upload_series_file, series-detail) est une action manuelle
                    # explicite: l'utilisateur a déjà décidé que CE fichier doit remplacer
                    # l'actuel, quelle que soit sa taille (ex: remplacer un gros fichier
                    # corrompu par un plus petit mais valide) - la comparaison de taille
                    # n'a plus lieu d'être ici, voir destination['force_replace'].
                    if destination.get('force_replace') or is_better_volume(source_size, existing_file_size):
                        # Le nouveau fichier est préféré : remplacer. L'ancien fichier est
                        # supprimé directement ("remove the logic of old and doublons
                        # files [...] the safety net is not useful") - plus de copie de
                        # sauvegarde dans _old_files.
                        print(f"Remplacement: {file_data['filename']} > {os.path.basename(existing_file_path)}")
                        # Backup temporaire dans le même dossier: l'ancien fichier reste
                        # récupérable jusqu'au commit du nouveau volume.
                        old_backup_path = f"{existing_file_path}.bullarr-backup-{uuid.uuid4().hex}"
                        os.replace(existing_file_path, old_backup_path)

                        # Récupérer comicinfo/cover de l'ancien tome avant de le supprimer
                        cursor.execute('SELECT comicinfo, cover_path FROM volumes WHERE filepath = ?', (existing_file_path,))
                        carried_row = cursor.fetchone()
                        if carried_row:
                            carried_comicinfo = carried_row[0]
                            carried_cover_path = carried_row[1]

                        # Supprimer l'ancien volume de la base de données
                        cursor.execute('DELETE FROM volumes WHERE filepath = ?', (existing_file_path,))

                        # Copier les fichiers dont la source n'est pas inscriptible
                        # (montage NFS en lecture seule, ex: aMule); déplacer les autres.
                        # "the reliable fix would be: after the library volumes row and
                        # import-history record are committed, always mark the tracking
                        # row imported, regardless of whether the source was moved or
                        # copied" - même correctif que le "Doublon ignoré" plus bas:
                        # appelé inconditionnellement, pas seulement dans la branche
                        # "déplacé".
                        moved_target_path = target_path
                        transfer_import_file(
                            source_path, moved_target_path, _import_staging_directory(),
                            timeout=IMPORT_STEP_TIMEOUT_SECONDS,
                            link_source_path=hardlink_source_path,
                        )
                        tracking_finalization = (original_source_path, destination, 'imported', source_was_copied)
                        action_taken = 'replaced'
                        replaced_count += 1
                        
                        # Ajouter à la liste de logs
                        logs_to_record.append({
                            'file_id': file_id,
                            'operation_id': operation_id,
                            'filename': file_data['filename'],
                            'source_path': original_source_path or source_path,
                            'destination_path': target_path,
                            'series_title': series_title,
                            'series_id': series_id,
                            'action': 'replaced',
                            'status': 'success',
                            'message': conversion_message,
                            'parsed_volume': _volume_fields_for_history(file_data['parsed'])
                        })

                    else:
                        # Le nouveau fichier est plus petit ou égal : pas d'intérêt à
                        # l'importer, supprimé directement ("remove the logic of old and
                        # doublons files [...] remove also the explanation when
                        # importing") - plus de copie dans _doublons ni de message détaillé
                        # (format/taille comparés) dans Historique, juste l'action.
                        # Voir le même correctif dans la vérification anticipée plus
                        # haut ("once Doublon ignoré -> state will be imported. so
                        # nothing should go after that") - le suivi doit toujours être
                        # clos ici, que la source ait été supprimée ou préservée.
                        source_was_copied = _should_preserve_import_source(original_source_path) or hardlink_requested
                        if not source_was_copied and os.path.exists(original_source_path):
                            os.remove(original_source_path)
                        if staged_source_path and os.path.exists(staged_source_path):
                            os.remove(staged_source_path)
                            try:
                                os.rmdir(os.path.dirname(staged_source_path))
                            except OSError:
                                pass
                        tracking_finalizations.append((original_source_path, destination, 'skipped', source_was_copied))
                        action_taken = 'skipped_duplicate'
                        skipped_count += 1

                        # Ajouter à la liste de logs
                        logs_to_record.append({
                            'file_id': file_id,
                            'operation_id': operation_id,
                            'filename': file_data['filename'],
                            'source_path': original_source_path or source_path,
                            'destination_path': '',
                            'series_title': series_title,
                            'action': 'skipped',
                            'status': 'success',
                            'message': 'Doublon ignoré',
                            'parsed_volume': _volume_fields_for_history(file_data['parsed'])
                        })

                        conn.close()
                        continue
                else:
                    # Pas de fichier existant : import normal - ou tome "placeholder"
                    # Bédéthèque (filepath NULL, voir add_series_from_bedetheque) pour
                    # ce numéro, que ce fichier transforme en tome réellement possédé
                    if existing_volume_id:
                        placeholder_volume_id = existing_volume_id

                    # Si le fichier de destination existe déjà (même nom de fichier)
                    if os.path.exists(target_path):
                        base, ext = os.path.splitext(file_data['filename'])
                        counter = 1
                        while os.path.exists(target_path):
                            target_path = os.path.join(target_dir, f"{base}_{counter}{ext}")
                            counter += 1

                    # Copier les fichiers dont la source n'est pas inscriptible (montage
                    # NFS en lecture seule, ex: aMule); déplacer les autres. Même
                    # correctif que la branche "replaced" ci-dessus: le suivi est
                    # toujours clos une fois le volume réellement committé, que la
                    # source ait été déplacée ou copiée.
                    moved_target_path = target_path
                    transfer_import_file(
                        source_path, moved_target_path, _import_staging_directory(),
                        timeout=IMPORT_STEP_TIMEOUT_SECONDS,
                        link_source_path=hardlink_source_path,
                    )
                    tracking_finalization = (original_source_path, destination, 'imported', source_was_copied)
                    action_taken = 'imported'
                    imported_count += 1

                    # Ajouter à la liste de logs
                    logs_to_record.append({
                        'file_id': file_id,
                        'operation_id': operation_id,
                        'filename': file_data['filename'],
                        'source_path': original_source_path or source_path,
                        'destination_path': target_path,
                        'series_title': series_title,
                        'series_id': series_id,
                        'action': 'imported',
                        'status': 'success',
                        'message': conversion_message,
                        'parsed_volume': _volume_fields_for_history(file_data['parsed'])
                    })

                # Ajouter le volume à la base de données (sauf si skipped)
                if action_taken != 'skipped_duplicate':
                    parsed = file_data['parsed']
                    page_count = scanner.get_page_count(target_path, parsed.get('format'))

                    if placeholder_volume_id:
                        # Transforme le placeholder en tome possédé (UPDATE, même id) -
                        # conserve son titre/couverture Bédéthèque tant que le fichier
                        # importé n'en fournit pas lui-même (comme au scan, voir
                        # scan_directory)
                        cursor.execute('SELECT comicinfo, cover_path FROM volumes WHERE id = ?', (placeholder_volume_id,))
                        placeholder_row = cursor.fetchone()
                        placeholder_comicinfo = placeholder_row[0] if placeholder_row else None
                        placeholder_cover_path = placeholder_row[1] if placeholder_row else None
                        volume_comicinfo = scanner.read_comicinfo(target_path, parsed.get('format'))
                        volume_cover_path = scanner.extract_volume_cover(target_path, parsed.get('format'))

                        # Bédéthèque fait référence (CLAUDE.md: "Bédéthèque est la source de
                        # vérité"), donc ses champs (déjà en base sur le placeholder)
                        # doivent toujours GAGNER sur le ComicInfo.xml embarqué dans le
                        # fichier importé, pas seulement combler ce qu'il lui manque - "le
                        # comicinfo.xml doit toujours être corrigé par les métadonnées
                        # bedetheque importées de la base". Le fichier ne comble plus que
                        # les champs que Bédéthèque n'a lui-même pas fournis (ex: rien
                        # scrapé pour ce champ précis).
                        placeholder_dict = json.loads(placeholder_comicinfo) if placeholder_comicinfo else {}
                        resolved_dict = dict(volume_comicinfo or {})
                        resolved_dict.update({k: v for k, v in placeholder_dict.items() if v not in (None, '')})
                        resolved_comicinfo = json.dumps(resolved_dict) if resolved_dict else None
                        # "all the data from an album is taken from bedetheque not
                        # from the file itself. unless it is releaser and quality" -
                        # author/year suivent la même règle "Bédéthèque gagne" que le
                        # comicinfo juste au-dessus, jamais la devinette brute du nom
                        # de fichier (parsed.get('author')/('year')) - même fonction
                        # que apply_volume_comicinfo, voir derive_author_year_from_comicinfo.
                        author_value, year_value = derive_author_year_from_comicinfo(resolved_dict)

                        cursor.execute('''
                            UPDATE volumes SET
                                part_number = ?, part_name = ?, filename = ?, filepath = ?,
                                author = ?, year = ?, resolution = ?, release_group = ?, file_size = ?, page_count = ?,
                                format = ?, comicinfo = ?, cover_path = ?
                            WHERE id = ?
                        ''', (
                            parsed.get('part_number'), parsed.get('part_name'),
                            os.path.basename(target_path), target_path, author_value, year_value,
                            parsed.get('resolution'), parsed.get('group'), source_size, page_count, parsed.get('format'),
                            resolved_comicinfo,
                            volume_cover_path or placeholder_cover_path,
                            placeholder_volume_id
                        ))
                        new_volume_ids.append(placeholder_volume_id)
                        # "BD Importé: Name of the volume (the rename version), Lien to
                        # bedetheque" - _notify_import_completed a besoin de l'id du
                        # volume pour relire son nom APRÈS renommage (_rename_new_volumes_
                        # after_import tourne avant la notification) plutôt que le nom brut
                        # déjà périmé de logs_to_record. logs_to_record[-1] est bien
                        # l'entrée de CE fichier: un seul append par fichier par itération,
                        # toujours avant ce point.
                        logs_to_record[-1]['volume_id'] = placeholder_volume_id

                        # DB-first (voir apply_volume_comicinfo/CLAUDE.md): la DB est déjà à
                        # jour ci-dessus, on projette maintenant ces mêmes données dans le
                        # fichier qui vient d'être importé - sans ça le ComicInfo.xml du
                        # placeholder (déjà connu depuis la création de la série via
                        # Bédéthèque) ne serait jamais réellement écrit dans le fichier avant
                        # un futur "MAJ métadonnées" manuel, alors que la donnée est déjà là.
                        if resolved_comicinfo and (parsed.get('format') or '').lower() in WRITABLE_FORMATS:
                            try:
                                merged = json.loads(resolved_comicinfo)
                                title_case_fields = {
                                    field: merged[field.lower()]
                                    for field in COMICINFO_FIELDS if field.lower() in merged
                                }
                                if title_case_fields:
                                    write_comicinfo_cbz(target_path, title_case_fields)
                            except Exception as e:
                                print(f"⚠️ Écriture ComicInfo.xml impossible pour {target_path}: {e}")
                    else:
                        # Couverture extraite tout de suite plutôt que d'attendre un scan
                        # ultérieur - sinon un volume tout juste importé (série neuve, pas
                        # de placeholder à transformer) reste sans cover_path et
                        # update_series_stats() n'a rien à mettre dans local_cover_path (la
                        # fiche retombe alors sur la cover Bédéthèque/Komga jusqu'au
                        # prochain scan complet). Pas de lecture du ComicInfo.xml du fichier
                        # ici en revanche: Bédéthèque fait référence (voir CLAUDE.md), le
                        # ComicInfo est ÉCRIT depuis les données déjà en base (la série est
                        # censée avoir déjà été créée/matchée à partir de Bédéthèque en
                        # amont, voir add_series_from_bedetheque), jamais lu depuis le
                        # fichier à l'import. carried_comicinfo/carried_cover_path (non vides
                        # uniquement si ce tome remplace un fichier existant, voir plus haut)
                        # reprennent ce qui était déjà connu pour CE numéro de tome plutôt que
                        # de perdre ces métadonnées simplement parce que le fichier a changé.
                        volume_cover_path = scanner.extract_volume_cover(target_path, parsed.get('format')) or carried_cover_path
                        resolved_comicinfo = carried_comicinfo
                        title_case_fields = None

                        # Repli ultime: aucune donnée à reporter (ni placeholder, ni
                        # ancien volume remplacé) - reconstruit le ComicInfo directement
                        # depuis series.bedetheque_albums (copie DB de la fiche Bédéthèque
                        # complète, voir BedethequeDatabase.update_series_bedetheque_info)
                        # plutôt que de laisser le tome sans métadonnées jusqu'à un futur
                        # "MAJ métadonnées" manuel. Aucun appel réseau: tout vient de la DB.
                        if not resolved_comicinfo:
                            cursor.execute('''
                                SELECT bedetheque_url, bedetheque_albums, bedetheque_description,
                                       bedetheque_genre, bedetheque_scenaristes, bedetheque_dessinateurs,
                                       bedetheque_editeurs
                                FROM series WHERE id = ?
                            ''', (series_id,))
                            series_bd_row = cursor.fetchone()
                            if series_bd_row and series_bd_row[0] and series_bd_row[1]:
                                bd_albums = json.loads(series_bd_row[1])
                                local_volume_adapter = {
                                    'volume_number': parsed.get('volume'),
                                    'is_integral': parsed.get('is_integral'),
                                    'integral_number': parsed.get('integral_number'),
                                    'is_hs': parsed.get('is_hs'),
                                    'hs_number': parsed.get('hs_number'),
                                    'is_episode': parsed.get('is_episode'),
                                    'episode_number': parsed.get('episode_number'),
                                }
                                bd_volume = match_bedetheque_volume(bd_albums, local_volume_adapter)
                                if bd_volume:
                                    series_info_dict = {
                                        'url': series_bd_row[0],
                                        'description': series_bd_row[2],
                                        'genre': series_bd_row[3],
                                        'scenaristes': (series_bd_row[4] or '').split(', ') if series_bd_row[4] else [],
                                        'dessinateurs': (series_bd_row[5] or '').split(', ') if series_bd_row[5] else [],
                                        'editeurs': (series_bd_row[6] or '').split(', ') if series_bd_row[6] else [],
                                    }
                                    title_case_fields = build_comicinfo_fields(
                                        parsed.get('volume'), series_title, series_info_dict, bd_volume
                                    )
                                    if title_case_fields:
                                        resolved_comicinfo = json.dumps(
                                            {k.lower(): v for k, v in title_case_fields.items()}
                                        )

                        # "all the data from an album is taken from bedetheque not
                        # from the file itself. unless it is releaser and quality" -
                        # même règle que le site UPDATE ci-dessus: author/year ne
                        # viennent jamais de parsed.get('author')/('year') (devinette
                        # du nom de fichier), voir derive_author_year_from_comicinfo.
                        author_value, year_value = derive_author_year_from_comicinfo(
                            json.loads(resolved_comicinfo) if resolved_comicinfo else {}
                        )

                        cursor.execute('''
                            INSERT INTO volumes
                            (series_id, part_number, part_name, volume_number, filename, filepath,
                             author, year, resolution, release_group, file_size, page_count, format,
                             is_integral, integral_number, is_hs, hs_number, is_episode, episode_number,
                             is_special, special_label, comicinfo, cover_path)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            series_id, parsed.get('part_number'), parsed.get('part_name'), parsed.get('volume'),
                            os.path.basename(target_path), target_path, author_value, year_value,
                            parsed.get('resolution'), parsed.get('group'), source_size, page_count, parsed.get('format'),
                            int(bool(parsed.get('is_integral'))), parsed.get('integral_number'),
                            int(bool(parsed.get('is_hs'))), parsed.get('hs_number'),
                            int(bool(parsed.get('is_episode'))), parsed.get('episode_number'),
                            # "in rugby there is a file BO4... i cannot select it" - un
                            # bonus/promo sans numéro (voir volume_override plus haut,
                            # is_special) doit pouvoir être importé tel quel.
                            int(bool(parsed.get('is_special'))), parsed.get('special_label'),
                            resolved_comicinfo,
                            volume_cover_path
                        ))
                        new_volume_ids.append(cursor.lastrowid)
                        # Voir commentaire jumeau ci-dessus (branche placeholder->possédé).
                        logs_to_record[-1]['volume_id'] = cursor.lastrowid

                        # Projection DB-first dans le fichier (voir apply_volume_comicinfo/
                        # CLAUDE.md) - même logique que pour un placeholder transformé
                        if resolved_comicinfo and (parsed.get('format') or '').lower() in WRITABLE_FORMATS:
                            try:
                                if title_case_fields is None:
                                    merged = json.loads(resolved_comicinfo)
                                    title_case_fields = {
                                        field: merged[field.lower()]
                                        for field in COMICINFO_FIELDS if field.lower() in merged
                                    }
                                if title_case_fields:
                                    write_comicinfo_cbz(target_path, title_case_fields)
                            except Exception as e:
                                print(f"⚠️ Écriture ComicInfo.xml impossible pour {target_path}: {e}")

                conn.commit()
                conn.close()

                # Staging guaranteed that the original was untouched throughout the
                # transaction. Remove a writable source only after the destination and
                # volume row are durable; read-only/NFS sources remain preserved.
                if tracking_finalization and not source_was_copied \
                        and original_source_path and os.path.exists(original_source_path):
                    try:
                        os.remove(original_source_path)
                    except OSError as cleanup_error:
                        print(f"⚠️ Source importée non supprimée {original_source_path}: {cleanup_error}")

                # Mettre ceci en file uniquement après la réussite de la transaction des
                # volumes. Si une étape précédente lève une exception, le chemin d'erreur
                # restaure la source et le suivi reste réessayable au lieu d'être marqué
                # importé à tort.
                if tracking_finalization:
                    tracking_finalizations.append(tracking_finalization)

                # Le nouveau volume est maintenant durable; le backup de remplacement
                # peut être supprimé sans risque.
                if old_backup_path and os.path.exists(old_backup_path):
                    try:
                        os.remove(old_backup_path)
                    except OSError as cleanup_error:
                        print(f"⚠️ Backup temporaire non supprimé {old_backup_path}: {cleanup_error}")

            except Exception as e:
                # "database is locked" par accumulation: conn (ouverte plus haut dès
                # qu'une série existante/nouvelle a été résolue) n'était jamais fermée sur
                # ce chemin - une exception survenant APRÈS le shutil.move mais AVANT le
                # commit final laissait une transaction ouverte (parfois avec un DELETE
                # déjà exécuté dessus, cas du remplacement) livrée au ramasse-miettes
                # Python au lieu d'un rollback+close déterministe, avec pour pire
                # conséquence un fichier réellement déplacé dans la bibliothèque sans
                # aucune ligne volumes pour le représenter (orphelin jusqu'au prochain
                # scan complet). rollback() annule proprement toute écriture partielle
                # avant de fermer.
                if conn is not None:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    try:
                        conn.close()
                    except Exception:
                        pass

                # SQLite ne peut pas annuler un déplacement filesystem. Restaurer le
                # fichier source lorsque l'échec survient après shutil.move() - ou
                # simplement supprimer la copie orpheline lorsque la source a été
                # PRÉSERVÉE (source_was_copied, voir _should_preserve_import_source):
                # le fichier d'origine est toujours intact à son emplacement, rien à
                # restaurer.
                try:
                    if moved_target_path and os.path.exists(moved_target_path) and rollback_source_path:
                        # The original is always preserved until commit by the staging
                        # design, so rollback only removes the uncommitted destination.
                        os.remove(moved_target_path)
                except Exception as restore_error:
                    print(f"⚠️ Restauration du fichier importé impossible: {restore_error}")

                # Restaurer l'ancien fichier remplacé s'il existe encore en backup.
                try:
                    if old_backup_path and os.path.exists(old_backup_path):
                        if existing_file_path and not os.path.exists(existing_file_path):
                            os.replace(old_backup_path, existing_file_path)
                        else:
                            os.remove(old_backup_path)
                except Exception as restore_old_error:
                    print(f"⚠️ Restauration de l'ancien volume impossible: {restore_old_error}")

                failed_count += 1
                failures.append({
                    'file': file_data['filename'],
                    # file_data.get(...), pas la variable locale source_path: si l'exception
                    # vient de son AFFECTATION elle-même (source_path = file_data['filepath']
                    # tout en haut du bloc try), la variable ne serait pas encore définie.
                    'filepath': file_data.get('filepath'),
                    'error': str(e)
                })

                # "don't put the error in the log until the file has finished download" -
                # une "Fichier corrompu" encore dans son budget de tentatives rapprochées
                # (voir AUTO_IMPORT_CORRUPTION_RETRY_DELAYS/MAX_AUTO_IMPORT_CORRUPTION_RETRIES,
                # scheduler.py) est très probablement juste un fichier encore en train
                # d'être copié sur disque, pas une vraie erreur digne d'apparaître dans
                # Historique ou les logs - failures.append ci-dessus reste inconditionnel
                # (le scheduler en a besoin pour son propre suivi d'échecs/backoff), mais
                # l'AFFICHAGE utilisateur (print, ligne Historique) attend que le fichier
                # ait vraiment épuisé sa chance de "juste pas encore fini" avant de le
                # montrer comme un échec.
                from .scheduler import library_import_scheduler, MAX_AUTO_IMPORT_CORRUPTION_RETRIES
                is_corruption = str(e).startswith('Fichier corrompu')
                prior_failures = library_import_scheduler._failure_counts.get(file_data.get('filepath'), 0)
                # "pourquoi autant d'entrées alors que ca devrait en mettre qu'une
                # entrée" - bug réel: suppress_log ne couvrait QUE les 3 premières
                # tentatives rapprochées (prior_failures < MAX_AUTO_IMPORT_CORRUPTION_
                # RETRIES). Une fois ce budget épuisé, chaque nouvelle tentative du filet
                # de sécurité (AUTO_IMPORT_CORRUPTION_COOLDOWN_SECONDS, une fois toutes
                # les 30 min, VOULU pour un fichier encore en copie réseau lente - voir
                # scheduler.py) redevenait "loggée" à chaque passage, créant une ligne
                # d'historique identique toutes les 30 minutes indéfiniment - exactement
                # les entrées dupliquées "22:13/21:57/21:38/21:31" observées.
                # just_became_exhausted (prior_failures == MAX_AUTO_IMPORT_CORRUPTION_
                # RETRIES pile, PAS >) identifie le TICK EXACT où le fichier bascule de
                # "encore en retry rapproché" à "définitivement épuisé, ce fichier a
                # besoin d'une action humaine" - une seule ligne d'historique y est créée
                # (voir suppress_log ci-dessous), plus aucune ensuite malgré les retries
                # du filet de sécurité qui continuent en tâche de fond sans bruit.
                just_became_exhausted = is_corruption and prior_failures == MAX_AUTO_IMPORT_CORRUPTION_RETRIES
                suppress_log = is_corruption and not just_became_exhausted
                if suppress_log:
                    any_suppressed_failure = True

                # Ce fichier précis a échoué après être passé en 'importing' plus haut -
                # revenir à 'completed' ("waiting to import") plutôt que de rester bloqué
                # en 'importing' pour de bon, pour qu'un prochain passage le reprenne.
                failed_tracking_id = (file_data.get('destination') or {}).get('tracking_id')
                if failed_tracking_id:
                    mark_download_completed(failed_tracking_id)

                if suppress_log:
                    from .import_history import delete_import_file_row
                    delete_import_file_row(file_id)
                else:
                    print(f"Erreur import {file_data['filename']}: {e}")
                    # Ajouter à la liste de logs
                    logs_to_record.append({
                        'file_id': file_id,
                        'operation_id': operation_id,
                        'filename': file_data['filename'],
                        # source_path (variable locale, pas file_data['filepath']): reflète
                        # le chemin RÉEL au moment de l'échec, y compris après une conversion
                        # cbr/pdf/zip->cbz qui l'a déjà réassigné vers un nouveau fichier -
                        # file_data['filepath'] reste figé sur le chemin D'ORIGINE, pointant
                        # vers un fichier qui n'existe alors déjà plus.
                        'source_path': original_source_path or source_path,
                        'destination_path': '',
                        'series_title': '',
                        'action': 'failed',
                        'status': 'error',
                        'message': str(e)
                    })
                    import traceback
                    traceback.print_exc()
            finally:
                if per_file_lock_acquired:
                    _import_execution_lock.release()
                claimed_path = original_source_path or file_data.get('filepath')
                if claimed_path:
                    release_import_file(claimed_path, operation_id)
                if staged_source_path and os.path.exists(staged_source_path):
                    try:
                        os.remove(staged_source_path)
                        os.rmdir(os.path.dirname(staged_source_path))
                    except OSError:
                        pass

        # Mettre à jour EN PLACE les lignes "en cours" créées par log_import_file_started
        # (voir plus haut) avec le sort réel de chaque fichier, APRÈS la fin de la boucle
        # d'import pour éviter les problèmes de verrou SQLite - remplace l'ancien INSERT
        # différé (log_import_file) par un UPDATE sur l'id déjà connu (file_id), sauf si
        # ce fichier n'a jamais eu de ligne "en cours" (file_id absent/None, ex: log_import_file_started
        # lui-même en échec) où l'on retombe sur l'ancien comportement (nouvelle ligne).
        for log_entry in logs_to_record:
            try:
                if log_entry.get('file_id') is not None:
                    update_import_file(
                        log_entry['file_id'],
                        log_entry['source_path'],
                        log_entry['destination_path'],
                        log_entry['series_title'],
                        log_entry['action'],
                        log_entry['status'],
                        log_entry.get('message', ''),
                        log_entry.get('parsed_volume')
                    )
                    continue
                log_import_file(
                    log_entry['operation_id'],
                    log_entry['filename'],
                    log_entry['source_path'],
                    log_entry['destination_path'],
                    log_entry['series_title'],
                    log_entry['action'],
                    log_entry['status'],
                    log_entry.get('message', ''),
                    log_entry.get('parsed_volume')
                )
            except Exception as log_err:
                print(f"Erreur lors de l'enregistrement du log: {log_err}")

        # "Blast - Intégrale@BD_fr.cbz" importé avec succès mais resté visible en
        # 'completed'/is_pack=1 pour toujours - bug réel: is_pack est un texte deviné à
        # la création (scanner.py: "Intégrale" SANS numéro de tome => is_pack=True,
        # supposant une release multi-fichiers), jamais confirmé/corrigé contre la
        # réalité. Sans numéro de tomes détecté (expected_volume_count resté NULL),
        # aucun filet existant ne peut jamais lever ce suivi : remove_completed_pack_if_owned
        # exige expected_volume_count, reconcile_stale_active_downloads ne sait matcher
        # qu'un tome/intégrale/HS/épisode NUMÉROTÉ, et _reconcile_stuck_completed_download
        # exclut explicitement tout is_pack=1 (pack potentiellement encore incomplet).
        # Ici, tracking_finalizations contient déjà TOUS les fichiers réellement
        # scannés/traités dans CE batch pour chaque tracking_id - le compte réel plutôt
        # que le texte du titre. Un seul fichier partageant un tracking_id => ce n'était
        # jamais un pack ; plusieurs => c'en est bien un. Restreint aux lignes SANS
        # expected_volume_count confirmé pour ne jamais toucher un pack numérique
        # légitime (dont les tomes peuvent arriver en plusieurs batches séparés).
        tracking_id_file_counts = Counter(
            d.get('tracking_id') for _, d, _, _ in tracking_finalizations
            if d and d.get('tracking_id') is not None
        )
        if tracking_id_file_counts:
            conn = sqlite3.connect(current_app.config['DATABASE'], timeout=30.0)
            conn.executemany(
                "UPDATE active_downloads SET is_pack = ? "
                "WHERE id = ? AND expected_volume_count IS NULL",
                [(1 if count > 1 else 0, tid) for tid, count in tracking_id_file_counts.items()]
            )
            conn.commit()
            conn.close()

        # C'est seulement maintenant, après l'écriture de la transaction des volumes et
        # de sa ligne d'historique, que le suivi du téléchargement est finalisé. Cela
        # couvre aussi les sources en lecture seule : la source peut rester sur le point
        # de montage après copy(), mais la destination est déjà un volume durable et le
        # suivi ne doit pas rester bloqué dans ``importing``.
        for source_path, destination, outcome, source_was_copied in tracking_finalizations:
            _maybe_complete_tracking_after_move(
                source_path, destination, outcome=outcome,
                source_was_copied=source_was_copied
            )

        # Mettre à jour les statistiques des séries concernées
        conn = sqlite3.connect(current_app.config['DATABASE'], timeout=120.0, check_same_thread=False)
        cursor = conn.cursor()

        # Récupérer toutes les séries uniques qui ont reçu des fichiers
        series_ids = set()
        for file_data in files_to_import:
            dest = file_data.get('destination')
            if dest and dest.get('series_id'):
                series_ids.add(dest['series_id'])

        # Mettre à jour chaque série
        for series_id in series_ids:
            scanner.update_series_stats(series_id, conn)

        # Renommer au format standard les fichiers tout juste importés (voir
        # _rename_new_volumes_after_import) - fait ici, après update_series_stats (qui
        # vient de fixer series.is_oneshot, utilisé par le gabarit de nom) et avant la
        # connexion suivante ci-dessous qui lit déjà le nom de fichier final
        _rename_new_volumes_after_import(conn, new_volume_ids)

        # Séries importées pas encore matchées à Komga - liste récupérée maintenant (avant
        # de fermer la connexion), mais le matching lui-même n'est tenté qu'à la toute fin
        # (voir plus bas): Komga doit d'abord avoir eu le temps de rescanner et de
        # connaître les fichiers qu'on vient de lui déplacer, sinon la recherche par titre
        # ne trouve rien pour une série toute neuve.
        #
        # Séries DÉJÀ matchées à Komga (komga_series_id IS NOT NULL): pas besoin de
        # matching par titre, mais chaque fichier tout juste importé/remplacé a quand même
        # besoin de son propre komga_book_id/url (_sync_komga_books, par livre) pour que
        # les liens directs "Ouvrir sur Komga" par tome existent. Rien d'autre ne rattrape
        # ça après coup: un scan complet ultérieur ne re-synchronise que les séries qu'IL
        # détecte comme changées (scanner.series_created_or_changed), et l'import écrit
        # les lignes `volumes` directement sans jamais passer par le scanner - une série
        # déjà matchée avant l'import restait donc avec des tomes fraîchement importés
        # sans aucun lien Komga, indéfiniment, jusqu'à un rescan manuel de cette série
        # précise (constaté sur "Imbattable"/"Carthago": tomes ajoutés par import jamais
        # liés malgré la série déjà matchée et les livres bien présents côté Komga).
        unmatched_komga_series = []
        matched_komga_series = []
        touched_library_ids = set()
        if series_ids:
            placeholders = ','.join('?' * len(series_ids))
            cursor.execute(
                f'SELECT id, title FROM series WHERE id IN ({placeholders}) AND komga_series_id IS NULL',
                tuple(series_ids)
            )
            unmatched_komga_series = cursor.fetchall()
            cursor.execute(
                f'SELECT id, komga_series_id FROM series WHERE id IN ({placeholders}) AND komga_series_id IS NOT NULL',
                tuple(series_ids)
            )
            matched_komga_series = cursor.fetchall()
            cursor.execute(
                f'SELECT DISTINCT library_id FROM series WHERE id IN ({placeholders})',
                tuple(series_ids)
            )
            touched_library_ids = {row[0] for row in cursor.fetchall()}

        conn.close()

        # "normalement des qu'on crée une série ça doit être crée [avec ses métadonnées]"
        # - une série créée pendant CET import avec un bedetheque_url déjà choisi côté UI
        # récupère maintenant sa fiche complète et écrit le ComicInfo de ses tomes tout de
        # suite, plutôt que de rester sans aucune métadonnée tant que l'utilisateur ne
        # clique pas "MAJ métadonnées" à la main. Voir trigger_new_series_bedetheque_fetch
        # (même fonction réutilisée par POST /api/series/create - "creer une nouveau album
        # devrait faire un match bedetheque").
        for series_id, series_info in new_series_with_bedetheque.items():
            trigger_new_series_bedetheque_fetch(series_id, series_info['title'], series_info['url'])

        # Déclenché ici (tôt) plutôt qu'en toute fin de fonction: le rescan Komga tourne en
        # arrière-plan côté serveur Komga, donc le lancer maintenant lui laisse le temps de
        # tourner pendant le reste de l'import (nettoyage...) avant la tentative de
        # matching Komga ci-dessous
        if imported_count or replaced_count:
            from blueprints.komga.client import trigger_scan_async
            trigger_scan_async()

        # Matching EBDZ automatique par titre pour les séries touchées par cet import (pas
        # de matching Bédéthèque ici: la série a déjà été créée/matchée à partir des
        # données Bédéthèque en amont, voir add_series_from_bedetheque - refaire un
        # matching ici serait redondant). Recherche locale en mémoire sur ed2k_links (voir
        # _bulk_ebdz_autodetect), pas d'appel réseau.
        for library_id in touched_library_ids:
            try:
                _bulk_ebdz_autodetect(library_id)
            except Exception:
                pass

        # Nettoyer les répertoires vides dans les répertoires d'import
        for root_dir in import_roots:
            if os.path.exists(root_dir):
                cleaned_dirs += cleanup_empty_directories(root_dir)

        # "j'ai l'entrée aucun fichier. vire ça" / "aucun fichier détaillé pour cet import" -
        # un lot dont TOUS les fichiers ont fini "continue" silencieusement (déjà pris en
        # charge par un import concurrent entre le scan et l'exécution, voir le
        # commentaire sur "os.path.exists(source_path)" plus haut) termine avec les 4
        # compteurs à zéro et aucune ligne import_history_files - rien de significatif à
        # montrer, juste du bruit sans même un nom de fichier auquel se raccrocher.
        # Supprimée plutôt que marquée 'completed' dans ce seul cas précis.
        if imported_count == 0 and replaced_count == 0 and skipped_count == 0 and failed_count == 0:
            delete_import_operation(operation_id)
        elif imported_count == 0 and replaced_count == 0 and skipped_count == 0 and failed_count > 0 and any_suppressed_failure:
            # "il faut faire 3 essais. pas plus" - un lot dont le SEUL échec est un retry
            # silencieux de corruption encore dans son budget (any_suppressed_failure) ne
            # crée pas non plus de ligne visible dans Historique, symétrique au cas 100%
            # "continue" ci-dessus - failed_count/failures restent inconditionnels pour le
            # scheduler (self._failure_counts, backoff), mais rien à montrer côté UI tant
            # que ce budget n'est pas épuisé (sinon une ligne identique par tentative,
            # jusqu'à autant de doublons que de passages du scheduler).
            delete_import_operation(operation_id)
        elif imported_count == 0 and replaced_count == 0 and skipped_count == 0 and failed_count > 0:
            # "erreur statut doit être échec pas ok" - un lot dont AUCUN fichier n'a
            # abouti (que des échecs, ex: permission NAS refusée) marquait quand même
            # l'opération 'completed' (la boucle elle-même n'avait pas planté) - le badge
            # ✓ vert dans l'historique (import.js/history.js: success = status==='completed')
            # laissait croire à tort que tout s'était bien passé. Réservé au cas pur
            # échec/échec (aucun succès mélangé) pour ne pas priver un lot partiellement
            # réussi de son statut 'completed', dont dépend l'annulation (undo_import_operation
            # exige ce statut).
            #
            # "il faudrait quand meme mettre le nom du fichier" - failures[0]['error'] ne
            # portait que le message d'erreur brut, jamais le nom du fichier concerné -
            # préfixé ici avec failures[0]['file'] pour que l'historique identifie
            # immédiatement DE QUEL fichier il s'agit, sans devoir ouvrir le détail par
            # fichier (souvent vide justement pour ce cas, voir suppress_log ci-dessus).
            update_import_operation(operation_id, 'failed', imported_count, replaced_count, skipped_count, failed_count,
                                     details=f"{failures[0]['file']} : {failures[0]['error']}" if failures else None)
        else:
            update_import_operation(operation_id, 'completed', imported_count, replaced_count, skipped_count, failed_count)

        # Tentative de matching Komga automatique par titre pour les séries pas encore
        # matchées, et (re)synchronisation par livre pour celles qui l'étaient déjà (voir
        # commentaire plus haut) - en tout dernier, avec un délai supplémentaire pour
        # laisser le rescan Komga déclenché ci-dessus le temps de se terminer côté serveur
        # Komga. Même logique/best-effort que pour les séries créées par un scan (voir
        # scan_library).
        if unmatched_komga_series or matched_komga_series:
            time.sleep(5)
            from blueprints.komga.client import KomgaClient, KomgaError
            try:
                client = KomgaClient()
                for row_id, row_title in unmatched_komga_series:
                    try:
                        _try_komga_title_match(row_id, row_title, client)
                    except KomgaError:
                        continue
                for row_id, row_komga_series_id in matched_komga_series:
                    try:
                        _sync_komga_books(row_id, row_komga_series_id, client)
                    except KomgaError:
                        continue
            except KomgaError:
                pass

        print(f"✓ Import {operation_type} terminé: {imported_count} importés, {replaced_count} remplacés, {skipped_count} ignorés, {failed_count} erreurs")
        _notify_import_completed(imported_count, replaced_count, notify_source, logs_to_record)

        return True, operation_id, {
            'imported_count': imported_count,
            'replaced_count': replaced_count,
            'skipped_count': skipped_count,
            'failed_count': failed_count,
            'failures': failures,
            'cleaned_directories': cleaned_dirs
        }

    except Exception as e:
        print(f"✗ Erreur lors de l'import {operation_type}: {e}")
        import traceback
        traceback.print_exc()
        if operation_id:
            try:
                update_import_operation(operation_id, 'failed', imported_count, replaced_count, skipped_count, failed_count, details=str(e)[:500])
            except Exception as update_err:
                print(f"Erreur lors du marquage de l'opération en échec: {update_err}")
        return True, operation_id, {
            'imported_count': imported_count,
            'replaced_count': replaced_count,
            'skipped_count': skipped_count,
            'failed_count': failed_count,
            'failures': failures,
            'cleaned_directories': cleaned_dirs,
            'error': str(e)
        }
    finally:
        if operation_id:
            release_import_files(operation_id)


@library_bp.route('/api/import/execute', methods=['POST'])
def execute_import():
    """Exécute l'import (manuel) des fichiers sélectionnés depuis /import - voir
    _execute_import_batch pour le cœur partagé avec execute_auto_import."""
    data = request.json
    files_to_import = data.get('files', [])
    if not files_to_import:
        return jsonify({'error': 'Aucun fichier à importer'}), 400

    # "pourquoi il y a Aucun fichier détaillé pour cet import" - un import manuel et un
    # import automatique démarrés à 2 secondes d'écart (voir execute_auto_import, appelé
    # depuis un thread APScheduler séparé) ont déjà laissé une opération bloquée sur
    # 'started' sans un seul fichier journalisé : chacun ouvre plusieurs connexions
    # SQLite successives. _import_execution_lock (dans _execute_import_batch) sérialise
    # les deux plutôt que de les laisser se percuter - timeout généreux mais fini, pour ne
    # jamais bloquer indéfiniment une requête si l'autre import est réellement resté coincé.
    lock_acquired, operation_id, stats = _execute_import_batch(
        files_to_import, operation_type='manual_import', lock_timeout=180,
        strict_missing_file=True, notify_source='manuel'
    )
    if not lock_acquired:
        return jsonify({'error': "Un autre import est déjà en cours, réessayez dans quelques instants"}), 409
    if stats.get('error'):
        return jsonify({'error': stats['error']}), 500

    return jsonify({
        'success': True,
        'imported_count': stats['imported_count'],
        'replaced_count': stats['replaced_count'],
        'skipped_count': stats['skipped_count'],
        'failed_count': stats['failed_count'],
        'failures': stats['failures'],
        'cleaned_directories': stats['cleaned_directories']
    })


@library_bp.route('/api/series/<int:series_id>/tags', methods=['GET', 'PUT'])
def manage_series_tags(series_id):
    """Récupère ou met à jour les tags d'une série"""
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if request.method == 'GET':
        # Récupérer les tags
        cursor.execute('SELECT tags FROM series WHERE id = ?', (series_id,))
        result = cursor.fetchone()
        conn.close()
        
        if not result:
            return jsonify({'error': 'Série non trouvée'}), 404
        
        tags = []
        if result['tags']:
            try:
                tags = json.loads(result['tags'])
            except (json.JSONDecodeError, TypeError):
                tags = []
        
        return jsonify({'tags': tags})
    
    elif request.method == 'PUT':
        # Mettre à jour les tags
        data = request.get_json()
        tags = data.get('tags', [])
        
        # Valider que c'est une liste de strings
        if not isinstance(tags, list):
            conn.close()
            return jsonify({'error': 'Les tags doivent être une liste'}), 400
        
        tags_list = [str(tag).strip() for tag in tags if tag]
        
        try:
            # Mettre à jour les tags comme JSON
            cursor.execute(
                'UPDATE series SET tags = ? WHERE id = ?',
                (json.dumps(tags_list), series_id)
            )
            
            conn.commit()
            conn.close()
            
            return jsonify({
                'success': True,
                'tags': tags_list,
                'message': 'Tags mis à jour avec succès'
            })
        
        except Exception as e:
            conn.close()
            return jsonify({'error': str(e)}), 500

def cleanup_empty_directories(base_path):
    """
    Nettoie les répertoires vides dans le chemin d'import
    Retourne le nombre de répertoires supprimés
    """
    if not os.path.exists(base_path):
        return 0

    deleted_count = 0

    # Parcourir en ordre inverse (du plus profond au plus superficiel)
    # pour supprimer les sous-répertoires vides avant les parents
    for root, dirs, files in os.walk(base_path, topdown=False):
        # Ne pas supprimer le répertoire de base lui-même
        if root == base_path:
            continue

        # Vérifier si le répertoire est vide (pas de fichiers, pas de sous-répertoires)
        try:
            if not os.listdir(root):  # Répertoire complètement vide
                print(f"Suppression du répertoire vide: {root}")
                os.rmdir(root)
                deleted_count += 1
        except (OSError, PermissionError) as e:
            print(f"Impossible de supprimer {root}: {e}")

    return deleted_count

@library_bp.route('/api/import/cleanup', methods=['POST'])
def cleanup_import_directory():
    """Nettoie les répertoires vides des répertoires d'import surveillés (aMule et torrents)"""
    try:
        cleaned_count = 0
        for import_path in current_app.config['IMPORT_DIRECTORIES']:
            if os.path.exists(import_path):
                cleaned_count += cleanup_empty_directories(import_path)

        return jsonify({
            'success': True,
            'cleaned_directories': cleaned_count
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ========== RENOMMAGE DE FICHIERS/DOSSIER AU FORMAT CONFIGURABLE ==========
# Format par défaut (voir rename_handler.DEFAULT_VOLUME_TEMPLATE/DEFAULT_SERIES_TEMPLATE),
# personnalisable dans Paramètres > Bibliothèque (blueprints/settings/rename_config_store.py).
# Sans `volume_id` dans le corps de la requête: toute la série (tous les fichiers + le
# dossier de la série). Avec `volume_id`: un seul tome, jamais le dossier.

def _fetch_series_for_rename(cursor, series_id):
    """Récupère titre/chemin/one-shot de la série + le chemin de sa bibliothèque,
    nécessaires à la fois pour renommer les fichiers et, le cas échéant, le dossier.
    universe_name: voir BedethequeDatabase.sync_series_universe - NULL si cette série
    n'appartient à aucun univers connu, pour le tag <univers> (rename_handler.py)."""
    cursor.execute('''
        SELECT s.id, s.title, s.path, s.is_oneshot, l.path AS library_path, u.name AS universe_name
        FROM series s
        JOIN libraries l ON s.library_id = l.id
        LEFT JOIN universes u ON u.id = s.universe_id
        WHERE s.id = ?
    ''', (series_id,))
    return cursor.fetchone()


def _fetch_volumes_for_rename(cursor, series_id, volume_id=None):
    """Récupère les tomes à renommer (tous, ou un seul si `volume_id` est fourni) avec
    tous les champs nécessaires au calcul du nom standard (voir compute_volume_tokens).
    Exclut les tomes "placeholder" (ajoutés depuis Bédéthèque, sans fichier - filepath
    NULL, voir add_series_from_bedetheque): un nom de fichier vide dans le plan de
    renommage résoudrait vers le dossier de la série lui-même
    (Path(series_path) / '' == series_path) et tenterait de le renommer comme si
    c'était un fichier - il n'y a de toute façon rien à renommer pour un tome sans fichier."""
    query = '''
        SELECT id, filename, volume_number, is_integral, integral_number,
               is_hs, hs_number, year, comicinfo, resolution, release_group
        FROM volumes
        WHERE series_id = ? AND filepath IS NOT NULL
    '''
    params = [series_id]
    if volume_id is not None:
        query += ' AND id = ?'
        params.append(volume_id)
    query += ' ORDER BY part_number, (volume_number IS NULL), volume_number, integral_number, filename'

    cursor.execute(query, params)
    volumes = []
    for row in cursor.fetchall():
        volumes.append({
            'id': row['id'],
            'filename': row['filename'],
            'volume_number': row['volume_number'],
            'is_integral': bool(row['is_integral']),
            'integral_number': row['integral_number'],
            'is_hs': bool(row['is_hs']),
            'hs_number': row['hs_number'],
            'year': row['year'],
            'comicinfo': json.loads(row['comicinfo']) if row['comicinfo'] else {},
            'resolution': row['resolution'],
            'release_group': row['release_group']
        })
    return volumes


def _rename_new_volumes_after_import(conn, new_volume_ids):
    """Renomme au format standard configuré (Paramètres > Bibliothèque) les fichiers
    tout juste importés, en réutilisant exactement FileRenamer (même mécanisme que le
    bouton manuel "Renommer tous les tomes", voir execute_rename) - sans ça un fichier
    importé gardait son nom de release brut (tags de groupe, résolution, etc.) jusqu'à ce
    que l'utilisateur pense à cliquer sur ce bouton séparément pour chaque série.

    Appelé après update_series_stats, qui fixe series.is_oneshot, utilisé par le
    template. Ne renomme QUE les volumes fraîchement insérés par cet import
    (new_volume_ids), jamais les autres tomes
    déjà présents dans la série - portée volontairement limitée à ce que cet import vient
    de toucher. Best-effort: un échec de renommage n'invalide jamais l'import déjà
    effectué (fichier déjà déplacé avec succès sous son nom d'origine)."""
    if not new_volume_ids:
        return

    from blueprints.settings.rename_config_store import load_rename_config
    from rename_handler import FileRenamer

    # _fetch_series_for_rename/_fetch_volumes_for_rename accèdent aux colonnes par nom
    # (row['title']...) - row_factory doit être positionné AVANT de créer le curseur
    # utilisé ci-dessous, un curseur déjà existant ne reprend pas un row_factory changé
    # après coup
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    placeholders = ','.join('?' * len(new_volume_ids))
    cursor.execute(f'SELECT id, series_id FROM volumes WHERE id IN ({placeholders})', tuple(new_volume_ids))
    volume_ids_by_series = {}
    for row in cursor.fetchall():
        volume_ids_by_series.setdefault(row[1], []).append(row[0])

    if not volume_ids_by_series:
        return

    rename_cfg = load_rename_config()

    for series_id, volume_ids in volume_ids_by_series.items():
        try:
            series = _fetch_series_for_rename(cursor, series_id)
            if not series:
                continue
            volumes = _fetch_volumes_for_rename(cursor, series_id, None)
            volumes = [v for v in volumes if v['id'] in volume_ids]
            if not volumes:
                continue

            file_plan = FileRenamer.build_rename_plan(
                series['title'], volumes, bool(series['is_oneshot']), rename_cfg['volume_template'],
                series['universe_name'], rename_cfg['oneshot_template']
            )
            results = FileRenamer.execute_rename_plan(series['path'], file_plan)

            for r in results:
                if r.get('success') and not r.get('skipped'):
                    new_path = os.path.join(series['path'], r['new_name'])
                    cursor.execute(
                        'UPDATE volumes SET filename = ?, filepath = ? WHERE id = ?',
                        (r['new_name'], new_path, r['volume_id'])
                    )
                elif not r.get('success'):
                    print(f"⚠️ Renommage à l'import échoué pour le volume #{r['volume_id']}: {r.get('error')}")
        except Exception as e:
            print(f"❌ Erreur renommage à l'import pour la série #{series_id}: {e}")
            import traceback
            traceback.print_exc()

    conn.commit()


def _apply_custom_volume_name(file_plan, custom_name):
    """Remplace le nom calculé par le template dans un plan d'un seul fichier (tome) par
    un nom choisi manuellement par l'utilisateur - même logique que le nom de dossier
    personnalisé (voir _rename_series_folder), pour permettre de renommer un tome même
    quand il est déjà au format standard.

    Si `custom_name` n'a pas d'extension, celle du fichier d'origine est conservée:
    sans quoi taper juste un nouveau titre sans réfléchir à l'extension casserait le
    fichier (un cbz renommé sans extension ne s'ouvrirait plus comme une archive)."""
    if not file_plan:
        return file_plan
    old_name = file_plan[0]['old_name']
    ext = os.path.splitext(old_name)[1]
    has_ext = os.path.splitext(custom_name)[1] != ''
    new_name = sanitize_path_component(custom_name if has_ext else custom_name + ext, 'Nom de fichier')
    file_plan[0]['new_name'] = new_name
    file_plan[0]['changed'] = new_name != old_name
    return file_plan


def _rename_series_folder(conn, series_id, series_path, series_title, library_path, final_name_by_volume, series_template=None, custom_name=None, universe_name=None):
    """Renomme le dossier de la série vers son titre assaini, puis met à jour
    series.path et volumes.filepath en conséquence dans la même connexion/commit - pour
    ne jamais laisser la base pointer vers un chemin qui n'existe plus si le re-scan
    (best-effort, tenté séparément après cet appel) venait à échouer.

    `final_name_by_volume`: dict {volume_id: nom_de_fichier_final}, calculé à partir des
    résultats du renommage de fichiers déjà effectué (la colonne volumes.filename en
    base est encore l'ancien nom tant que le re-scan n'a pas eu lieu).

    `custom_name`: si fourni, utilisé tel quel comme nom de dossier au lieu du nom
    calculé depuis le template - permet à l'utilisateur de renommer manuellement même
    quand le dossier est déjà au format standard (rien à renommer via le template).

    Un nom invalide ou une collision n'annule pas les renommages de fichiers déjà
    commités séparément: seule cette étape échoue, avec une erreur explicite.
    """
    from rename_handler import render_series_folder_name
    cursor = conn.cursor()

    try:
        raw_name = custom_name if custom_name else render_series_folder_name(series_title, series_template, universe_name)
        # Les modèles peuvent produire plusieurs segments : valider chaque composant
        # séparément conserve la protection contre l'évasion du dossier de bibliothèque.
        folder_segments = [sanitize_path_component(part, 'Titre de série') for part in raw_name.split('/') if part]
        if not folder_segments:
            raise UnsafePathError(f"Titre de série invalide: {raw_name!r}")
        new_series_path = resolve_within(os.path.join(library_path, *folder_segments), library_path)
    except UnsafePathError as e:
        return {'success': False, 'old_path': series_path, 'error': str(e)}

    current_path_real = os.path.realpath(series_path)

    if current_path_real == new_series_path:
        return {'success': True, 'changed': False, 'old_path': series_path, 'new_path': series_path}

    if os.path.exists(new_series_path):
        return {
            'success': False, 'old_path': series_path, 'new_path': new_series_path,
            'error': 'Un dossier existe déjà à cet emplacement'
        }

    try:
        # os.rename() ne crée pas les dossiers intermédiaires : les créer avant le
        # déplacement permet aux modèles comportant un niveau supplémentaire de fonctionner.
        os.makedirs(os.path.dirname(new_series_path), exist_ok=True)
        # Si la destination se trouve dans le dossier actuel, le noyau refuse le
        # déplacement direct ; une étape temporaire permet de libérer l'ancien chemin.
        if os.path.commonpath([current_path_real, new_series_path]) == current_path_real:
            tmp_path = current_path_real + '.rename_tmp'
            os.rename(current_path_real, tmp_path)
            try:
                os.makedirs(os.path.dirname(new_series_path), exist_ok=True)
                os.rename(tmp_path, new_series_path)
            except OSError:
                # Restaure l'état d'origine plutôt que de laisser le dossier "disparu"
                # sous un nom temporaire si la seconde étape échoue.
                os.rename(tmp_path, current_path_real)
                raise
        else:
            os.rename(current_path_real, new_series_path)
    except OSError as e:
        return {'success': False, 'old_path': series_path, 'new_path': new_series_path, 'error': str(e)}

    # Le dossier a bien été renommé sur disque à ce stade: la base DOIT refléter le
    # nouveau chemin, indépendamment du succès du re-scan tenté juste après
    cursor.execute('UPDATE series SET path = ? WHERE id = ?', (new_series_path, series_id))
    cursor.execute('SELECT id, filename FROM volumes WHERE series_id = ?', (series_id,))
    for row in cursor.fetchall():
        filename = final_name_by_volume.get(row['id'], row['filename'])
        # Tome "placeholder" (ajouté depuis Bédéthèque, pas encore de fichier réel - voir
        # _sync_bedetheque_placeholder_volumes) : filename/filepath restent NULL, rien à
        # recalculer. Sans ce garde, os.path.join(new_series_path, None) plantait dès
        # qu'une série avec au moins un tome manquant listé (quasi toutes, une fois
        # matchées sur Bédéthèque) était renommée - le dossier était déjà renommé sur
        # disque à ce stade (voir plus haut), mais series.path/volumes.filepath du(des)
        # tome(s) réel(s) restaient donc sur l'ancien chemin, désynchronisés.
        if filename is None:
            continue
        cursor.execute(
            'UPDATE volumes SET filepath = ? WHERE id = ?',
            (os.path.join(new_series_path, filename), row['id'])
        )
    conn.commit()

    return {'success': True, 'changed': True, 'old_path': series_path, 'new_path': new_series_path}


@library_bp.route('/api/series/<int:series_id>/rename/preview', methods=['POST'])
def preview_rename(series_id):
    """Aperçu (sans rien modifier) du renommage au format standard.

    Avec `volume_id`: renomme uniquement ce fichier (jamais le dossier). Avec
    `all_volumes`: renomme tous les tomes de la série en une seule action (toujours pas
    le dossier). Sans aucun des deux ("Renommer la série"): renomme uniquement le
    dossier de la série, jamais les fichiers qu'il contient - ce sont des actions
    distinctes avec leurs propres paramètres (voir Paramètres > Bibliothèque: format
    des tomes vs format du dossier), jamais combinées automatiquement."""
    try:
        data = request.get_json(silent=True) or {}
        volume_id = data.get('volume_id')
        all_volumes = bool(data.get('all_volumes'))
        custom_name = (data.get('custom_name') or '').strip() or None

        conn = get_db_connection()
        cursor = conn.cursor()

        series = _fetch_series_for_rename(cursor, series_id)
        if not series:
            conn.close()
            return jsonify({'error': 'Série introuvable'}), 404

        series_title = series['title']
        series_path = series['path']
        is_oneshot = bool(series['is_oneshot'])
        library_path = series['library_path']

        from blueprints.settings.rename_config_store import load_rename_config
        rename_cfg = load_rename_config()

        if all_volumes or volume_id is not None:
            volumes = _fetch_volumes_for_rename(cursor, series_id, None if all_volumes else volume_id)
            conn.close()
            if not volumes:
                return jsonify({'error': 'Aucun volume trouvé'}), 404

            from rename_handler import FileRenamer
            file_plan = FileRenamer.build_rename_plan(series_title, volumes, is_oneshot, rename_cfg['volume_template'], series['universe_name'], rename_cfg['oneshot_template'])
            # "ca met deja au format standard (donc je peux pas) mais j'aimerais quand
            # meme le renommer manuellement" - _apply_custom_volume_name suppose un plan
            # d'UN SEUL fichier (elle réécrit file_plan[0] sans condition) : le garde
            # d'origine excluait tout `all_volumes=True`, mais un one-shot envoie
            # justement all_volumes=True pour son unique tome ("Renommer tomes", pas de
            # bouton dédié "un seul tome" pour ce cas) - len(file_plan) == 1 couvre les
            # deux à la fois (volume_id précis, OU all_volumes qui se résout à un seul
            # volume) sans jamais risquer d'appliquer un nom unique à plusieurs fichiers.
            if custom_name and len(file_plan) == 1:
                file_plan = _apply_custom_volume_name(file_plan, custom_name)
            return jsonify({
                'success': True, 'series_title': series_title, 'series_path': series_path,
                'files': file_plan, 'folder': None
            })

        conn.close()
        from rename_handler import render_series_folder_name
        try:
            raw_name = custom_name if custom_name else render_series_folder_name(series_title, rename_cfg['series_template'], series['universe_name'])
            folder_segments = [sanitize_path_component(part, 'Titre de série') for part in raw_name.split('/') if part]
            if not folder_segments:
                raise UnsafePathError(f"Titre de série invalide: {raw_name!r}")
            new_series_path = resolve_within(os.path.join(library_path, *folder_segments), library_path)
            folder_change = {
                'old_path': series_path,
                'new_path': new_series_path,
                'changed': os.path.realpath(series_path) != new_series_path
            }
        except UnsafePathError as e:
            folder_change = {'old_path': series_path, 'changed': False, 'error': str(e)}

        return jsonify({
            'success': True,
            'series_title': series_title,
            'series_path': series_path,
            'files': [],
            'folder': folder_change
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@library_bp.route('/api/series/<int:series_id>/rename/execute', methods=['POST'])
def execute_rename(series_id):
    """Effectue le renommage au format standard.

    Avec `volume_id`: renomme uniquement ce fichier (jamais le dossier). Avec
    `all_volumes`: renomme tous les tomes de la série en une seule action (toujours pas
    le dossier). Sans aucun des deux ("Renommer la série"): renomme uniquement le
    dossier de la série, jamais les fichiers qu'il contient - voir preview_rename
    ci-dessus pour le détail de cette séparation volontaire en actions distinctes."""
    try:
        data = request.get_json(silent=True) or {}
        volume_id = data.get('volume_id')
        all_volumes = bool(data.get('all_volumes'))
        custom_name = (data.get('custom_name') or '').strip() or None

        conn = get_db_connection()
        cursor = conn.cursor()

        series = _fetch_series_for_rename(cursor, series_id)
        if not series:
            conn.close()
            return jsonify({'error': 'Série introuvable'}), 404

        series_title = series['title']
        series_path = series['path']
        is_oneshot = bool(series['is_oneshot'])
        library_path = series['library_path']

        from blueprints.settings.rename_config_store import load_rename_config
        rename_cfg = load_rename_config()

        file_results = []
        final_name_by_volume = {}
        if all_volumes or volume_id is not None:
            volumes = _fetch_volumes_for_rename(cursor, series_id, None if all_volumes else volume_id)
            if not volumes:
                conn.close()
                return jsonify({'error': 'Aucun volume trouvé'}), 404

            from rename_handler import FileRenamer
            file_plan = FileRenamer.build_rename_plan(series_title, volumes, is_oneshot, rename_cfg['volume_template'], series['universe_name'], rename_cfg['oneshot_template'])
            # Même condition que preview_rename ci-dessus - voir son commentaire.
            if custom_name and len(file_plan) == 1:
                file_plan = _apply_custom_volume_name(file_plan, custom_name)
            file_results = FileRenamer.execute_rename_plan(series_path, file_plan)

            # Nom de fichier final par tome (utile pour recalculer filepath après un
            # éventuel renommage de dossier): en cas d'échec sur un fichier, il garde
            # son ancien nom, donc old_name plutôt que new_name
            final_name_by_volume = {
                r['volume_id']: (r['new_name'] if r.get('success') else r['old_name'])
                for r in file_results
            }

        folder_result = None
        if volume_id is None and not all_volumes:
            folder_result = _rename_series_folder(
                conn, series_id, series_path, series_title, library_path, final_name_by_volume, rename_cfg['series_template'],
                custom_name=custom_name, universe_name=series['universe_name']
            )
        conn.commit()
        conn.close()

        # Best-effort: rescanner la série pour resynchroniser complètement la base
        # (couvertures, ComicInfo.xml, tailles...). Un échec ici ne remet pas en cause
        # les renommages de fichiers ni la mise à jour series.path/volumes.filepath déjà
        # commitée ci-dessus.
        try:
            from .scanner import LibraryScanner
            LibraryScanner().scan_single_series(series_id)
        except Exception as e:
            print(f"Erreur lors du re-scan: {e}")
            import traceback
            traceback.print_exc()

        from blueprints.komga.client import trigger_scan_async
        trigger_scan_async()

        _log_rename_action(series_id, series_title, file_results, folder_result)

        return jsonify({
            'success': True,
            'files': file_results,
            'folder': folder_result
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


def _log_rename_action(series_id, series_title, file_results, folder_result):
    """Journalise un renommage dans l'historique (voir action_history.py) - best-effort,
    ne doit jamais faire échouer le renommage lui-même. Un seul événement par appel à
    execute_rename, qu'il ait renommé un tome, tous les tomes, ou le dossier."""
    try:
        from blueprints.library.action_history import log_action

        renamed_files = [r for r in file_results if r.get('success') and r.get('changed', True)]
        failed_files = [r for r in file_results if not r.get('success')]

        parts = []
        if file_results:
            parts.append(f"{len(renamed_files)} fichier(s) renommé(s)")
            if failed_files:
                parts.append(f"{len(failed_files)} échec(s)")
        if folder_result:
            if folder_result.get('success') and folder_result.get('changed'):
                parts.append(f"dossier: {os.path.basename(folder_result['old_path'])} → {os.path.basename(folder_result['new_path'])}")
            elif not folder_result.get('success'):
                parts.append(f"dossier: échec ({folder_result.get('error', '?')})")

        success = not failed_files and (not folder_result or folder_result.get('success'))
        error = None if success else '; '.join(
            [f.get('error', '?') for f in failed_files] +
            ([folder_result['error']] if folder_result and not folder_result.get('success') else [])
        )

        # files_json: détail fichier par fichier consulté à la demande depuis Historique
        # ("ouvrir le nom pour avoir le detail du renommage comme dans import") - `detail`
        # ci-dessus reste le résumé affiché directement dans la liste.
        files_json = json.dumps({'files': file_results, 'folder': folder_result})

        log_action('rename', series_id, series_title, ' · '.join(parts) or 'Rien à renommer', success=success, error=error, files_json=files_json)
    except Exception as e:
        print(f"Erreur lors de la journalisation du renommage (série #{series_id}): {e}")


# ========== FONCTIONS D'IMPORT AUTOMATIQUE ==========

def load_library_import_config():
    """Charge la configuration d'import automatique"""
    config_file = current_app.config['LIBRARY_IMPORT_CONFIG_FILE']
    defaults = current_app.config['LIBRARY_IMPORT_CONFIG'].copy()

    if os.path.exists(config_file):
        with open(config_file, 'r') as f:
            saved = json.load(f)
        # Fusionné PAR-DESSUS les défauts (pas juste renvoyé tel quel): un fichier déjà
        # sauvegardé avant l'ajout d'une nouvelle clé aux défauts (ex: monitored_extensions)
        # n'a sinon jamais cette clé, faute de merge - constaté sur la clé elle-même,
        # ajoutée après que la plupart des installations aient déjà un fichier existant.
        defaults.update(saved)
        return defaults

    return defaults


def save_library_import_config(config):
    """Sauvegarde la configuration d'import automatique"""
    config_file = current_app.config['LIBRARY_IMPORT_CONFIG_FILE']
    
    try:
        os.makedirs(os.path.dirname(config_file), exist_ok=True)
        with open(config_file, 'w') as f:
            json.dump(config, f, indent=4)
        return True
    except Exception as e:
        print(f"Erreur lors de la sauvegarde de la configuration d'import: {e}")
        return False


def can_auto_assign(parsed, config):
    """Détermine si un fichier peut être auto-assigné
    
    Args:
        parsed: Dictionnaire de données parsées du nom de fichier
        config: Configuration d'import
        
    Returns:
        True si le fichier peut être auto-assigné, False sinon
    """
    if not config.get('auto_assign_enabled', True):
        return False

    # Le titre est la seule info toujours requise. Le numéro de tome n'est PAS exigé ici:
    # un one-shot (parsed['volume'] est None, aucun numéro dans le nom de fichier) doit
    # pouvoir s'auto-assigner aussi ("pourquoi l'import automatique ne marche pas" - un
    # one-shot posé dans /amule n'était jamais repris, faute de numéro de tome à parser).
    # find_auto_assign_destination applique le vrai garde-fou dans ce cas: un fichier sans
    # numéro n'est accepté que s'il matche une série DÉJÀ marquée one-shot en base, jamais
    # de création automatique.
    if not parsed.get('title'):
        return False

    return True


def _match_series_for_auto_import(normalized_title, all_series):
    """Trouve la série correspondant à `normalized_title` parmi `all_series` (lignes
    (id, library_id, library_path, title, is_oneshot, library_name)) - égalité stricte d'abord (voir
    _normalize_title_for_match), puis en repli un préfixe NON AMBIGU: le titre parsé du
    fichier commence par le titre de la série suivi d'un mot supplémentaire. Couvre le
    sous-titre d'un hors-série/intégrale non séparé du nom de la série par le parsing (ex:
    "Yoko Tsuno - L'écume de l'aube" pour la série "Yoko Tsuno") et un nom d'auteur en fin
    de nom de fichier qu'aucun pattern de parse_filename n'a pu isoler faute de marqueur
    de tome à côté duquel s'ancrer (ex: "Un espoir sans papiers Ingrid Chabbert" pour la
    série "Un espoir sans papiers"). "pourquoi 4 imports ne marchent pas automatiquement".
    Retourne None si aucune égalité ET que 0 ou PLUSIEURS séries correspondent au repli
    préfixe (ambigu - mieux vaut ne rien assigner automatiquement que de deviner). Même
    principe pour l'égalité stricte elle-même: si le titre correspond à PLUSIEURS séries
    (doublon en base, voir add_series_from_bedetheque/_import_execution_lock - un doublon
    ne devrait normalement plus se produire, mais un ancien pris avant ce correctif ne
    doit pas faire deviner silencieusement laquelle des deux importer)."""
    exact_matches = [row for row in all_series if _normalize_title_for_match(row[3]) == normalized_title]
    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(exact_matches) > 1:
        return None

    prefix_matches = [
        row for row in all_series
        if _normalize_title_for_match(row[3])
        and normalized_title.startswith(_normalize_title_for_match(row[3]) + ' ')
    ]
    return prefix_matches[0] if len(prefix_matches) == 1 else None


def _auto_import_has_self_numbering(parsed):
    """Un hors-série ou une intégrale numéroté(e) (hs_number/integral_number, voir
    parse_filename) porte son propre numéro indépendant de la numérotation normale des
    tomes (parsed['volume']) - ne devrait pas être traité comme "sans numéro" au même
    titre qu'un one-shot informe, qui lui exige que la série cible soit déjà marquée
    one-shot en base (voir find_auto_assign_destination/_auto_import_skip_reason).

    "import en cours mais pas sur que ca marche" - une intégrale/HS SANS numéro (ex:
    "INT - Cycle II - Edition Intégrale", l'intégrale unique d'un cycle plutôt qu'une
    parmi plusieurs numérotées INT1/INT2) exigeait jusqu'ici quand même un numéro pour
    passer ce garde-fou, alors que is_integral/is_hs SEUL est déjà un signal suffisant
    (même principe que is_oneshot juste au-dessus: le TYPE identifie le fichier, pas
    besoin d'un numéro en plus) - un tome par ailleurs déjà matché avec certitude via
    active_downloads (série connue, voir apply_tracked_volume_and_gate) restait bloqué
    indéfiniment pour cette seule raison, jamais repris par l'auto-import.

    "ca met bellatrix import en cours... ca a l'air un peu long" - is_episode manquait
    ici (le TYPE, comme is_hs/is_integral, identifie déjà le fichier sans avoir besoin
    d'un volume_number) : un fichier "Épisode N" téléchargé depuis une recherche/
    surveillance (destination connue via active_downloads mais SANS volume_number
    suivi, cas des 3 tomes Bellatrix) repassait ce garde-fou en échec à CHAQUE passage
    du scheduler (toutes les 5s), indéfiniment - jamais un vrai blocage temporaire, un
    rejet permanent qui ressemblait à un import bloqué."""
    return bool(parsed.get('is_hs') or parsed.get('is_integral') or parsed.get('is_episode'))


def _build_active_download_destination(series_id, volume_number, tracking_id, force_replace=False,
                                        is_integral=False, integral_number=None,
                                        is_hs=False, hs_number=None,
                                        is_episode=False, episode_number=None):
    """Construit le dict destination pour une série déjà identifiée avec certitude via un
    active_downloads (par nom de fichier - find_active_download_destination - ou par nom de
    torrent qBittorrent - find_active_download_destination_by_torrent_name). Retourne None si
    series_id ne pointe plus vers une série existante (série supprimée entretemps).

    force_replace: repris tel quel de la ligne active_downloads (voir mark_download_pending)
    - "Remplacer quand même", consommé par apply_tracked_volume_and_gate/
    _execute_import_batch pour faire sauter la comparaison de taille is_better_volume."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT s.id, s.library_id, l.path, l.name, s.title, s.is_oneshot,
                   s.bedetheque_total_volumes
            FROM series s
            JOIN libraries l ON s.library_id = l.id
            WHERE s.id = ?
        ''', (series_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return None

        # Conserver l'état de cycle de vie enregistré avec la destination. L'interface
        # d'import ne doit pas déduire que le fichier est prêt uniquement parce qu'une
        # destination existe : un fichier suivi peut encore être téléchargé, en cours
        # d'import ou déjà terminé.
        download_status = None
        if tracking_id is not None:
            status_row = cursor.execute(
                'SELECT status FROM active_downloads WHERE id = ?', (tracking_id,)
            ).fetchone()
            download_status = status_row[0] if status_row else None
        conn.close()

        series_id, library_id, library_path, library_name, series_title, is_oneshot, bedetheque_total_volumes = row
        return {
            'series_id': series_id,
            'library_id': library_id,
            'library_path': library_path,
            'library_name': library_name,
            'series_title': series_title,
            'is_new_series': False,
            'is_oneshot': bool(is_oneshot),
            # Bédéthèque définit parfois une série à album unique comme « Série finie »
            # plutôt que « One shot ». Un fichier téléchargé et déjà rattaché à cette
            # série peut néanmoins être importé sans numéro inventé.
            'is_single_album': bedetheque_total_volumes == 1,
            # Transmis tel quel (voir find_active_download_match) - consommé par
            # /api/activity/status pour afficher "Série - Tome N" sur une ligne de
            # téléchargement déjà EN COURS chez le client, pas seulement sur la ligne
            # "en attente" (voir get_pending_downloads, qui l'affichait déjà), et par
            # scan_import_directory/library/scheduler.py pour compléter parsed['volume']
            # quand le nom de fichier lui-même ne le fournit pas (voir order.md, étape 5-6:
            # l'import automatique ne doit jamais se faire sans numéro de tome connu).
            'volume_number': volume_number,
            # "pourquoi je ne peux pas choisir intégrale" - une correction manuelle
            # (update_active_download_tracking) peut avoir marqué ce téléchargement comme
            # une Intégrale/HS/Épisode plutôt qu'un tome classique - transmis tel quel
            # jusqu'à apply_tracked_volume_and_gate, qui les injecte dans parsed comme le
            # ferait le nom de fichier lui-même (voir parse_filename).
            'is_integral': bool(is_integral),
            'integral_number': integral_number,
            'is_hs': bool(is_hs),
            'hs_number': hs_number,
            'is_episode': bool(is_episode),
            'episode_number': episode_number,
            # id de la ligne active_downloads elle-même (pas item['id'] côté /api/activity/
            # status, qui reste le hash/id CLIENT) - "editer les volumes et album dans
            # l'import meme quand c'est pas terminé": nécessaire pour que le frontend
            # puisse cibler POST /api/activity/update-tracking sur CETTE ligne précise.
            'tracking_id': tracking_id,
            'download_status': download_status,
            'force_replace': bool(force_replace),
        }
    except Exception as e:
        print(f"Erreur résolution destination depuis un téléchargement suivi: {e}")
        return None


def find_active_download_destination(filename, trackable_downloads=None):
    """Destination déterministe pour un fichier qui correspond à un téléchargement suivi
    (voir mark_download_pending/find_active_download_match côté downloader.py) - "get the
    volume number and album name not from a matching but from when the file was added":
    la série était déjà connue avec certitude au moment du clic (recherche/remplacement
    depuis une fiche série, ou monitoring automatique de volumes manquants), pas besoin de
    la re-deviner ici depuis le nom de fichier parsé comme le fait
    find_auto_assign_destination (repli utilisé seulement si CETTE fonction ni
    find_active_download_destination_by_torrent_name ne trouvent rien - voir
    library/scheduler.py). Retourne None si aucun active_downloads correspondant par nom de
    fichier, ou si son series_id ne pointe plus vers une série existante.

    trackable_downloads: liste déjà chargée (voir get_trackable_active_downloads côté
    downloader.py) - à fournir quand cette fonction est appelée en boucle sur plusieurs
    fichiers (scan_import_directory, library/scheduler.py) pour ne PAS relire
    active_downloads à chaque fichier ("pourquoi ca met scanner en loading" - un scan de
    dossier d'import avec des dizaines de fichiers ouvrait autant de connexions SQLite en
    plus pour ce seul matching). None (par défaut) déclenche un chargement à la demande,
    pour un appel isolé sur un seul fichier."""
    from blueprints.missing_monitor.downloader import get_trackable_active_downloads, match_filename_against_trackable_downloads

    if trackable_downloads is None:
        trackable_downloads = get_trackable_active_downloads()
    match = match_filename_against_trackable_downloads(filename, trackable_downloads)
    if not match or not match.get('series_id'):
        return None

    return _build_active_download_destination(
        match['series_id'], match.get('volume_number'), match.get('tracking_id'),
        force_replace=match.get('force_replace', False),
        is_integral=match.get('is_integral', False), integral_number=match.get('integral_number'),
        is_hs=match.get('is_hs', False), hs_number=match.get('hs_number'),
        is_episode=match.get('is_episode', False), episode_number=match.get('episode_number'),
    )


def find_active_download_destination_by_torrent_name(folder_name, trackable_downloads, torrent_names_by_hash):
    """Repli pour "Les.Rugbymen.PDF-NoTag" et consorts: un fichier dont le NOM ne contient
    aucun indice exploitable (extrait d'un torrent multi-fichiers dont les tomes portent des
    noms sans rapport avec la série/le titre suivi) ne peut jamais matcher via
    find_active_download_destination (comparaison par nom de FICHIER). "can the clients api
    tell you the location of the files so it will be easier to match" - on demande à l'API
    qBittorrent le nom RÉEL du torrent pour chaque active_downloads suivi ayant un
    client_item_id (le hash), et on compare ce nom au nom du DOSSIER trouvé sur disque - PAS
    aux chemins absolus ("but we might need to setup a remote path mapping. check this if it
    is needed": qBittorrent tourne potentiellement sur un tout autre hôte que cette app, donc
    comparer des chemins nécessiterait un mapping chemin distant <-> local façon Sonarr/Radarr;
    comparer seulement des NOMS de dossier/torrent évite ce problème entièrement).

    torrent_names_by_hash: dict hash (minuscule) -> nom de torrent, voir
    get_qbittorrent_torrent_names (qbittorrent/routes.py) - à construire UNE SEULE FOIS par
    scan (scan_import_directory), pas par fichier, même raisonnement que trackable_downloads."""
    if not folder_name or not torrent_names_by_hash:
        return None

    folder_name_lower = folder_name.strip().lower()
    for download in trackable_downloads:
        if download.get('client') != 'qbittorrent' or not download.get('client_item_id'):
            continue
        torrent_name = torrent_names_by_hash.get(download['client_item_id'].lower())
        if torrent_name and torrent_name.strip().lower() == folder_name_lower:
            return _build_active_download_destination(
                download['series_id'], download.get('volume_number'), download['id'],
                force_replace=download.get('force_replace', False),
                is_integral=download.get('is_integral', False), integral_number=download.get('integral_number'),
                is_hs=download.get('is_hs', False), hs_number=download.get('hs_number'),
                is_episode=download.get('is_episode', False), episode_number=download.get('episode_number'),
            )
    return None


def apply_tracked_volume_and_gate(parsed, destination):
    """Complète parsed['volume'] depuis le tome connu au moment du téléchargement (voir
    find_active_download_destination) quand le nom de fichier ne le fournit pas lui-même,
    PUIS confirme que l'import automatique peut avoir lieu - "l'import automatique est fait
    si on a toutes les informations... si on n'a pas toutes les informations... il ne faut
    pas que l'import automatique soit activé" (order.md). Un destination trouvé via
    active_downloads (série connue avec certitude) ne suffit pas à lui seul: sans numéro de
    tome nulle part (ni le nom de fichier, ni le suivi, ni un one-shot/hors-série/intégrale
    déjà auto-numéroté), importer quand même laisserait un tome en base sans numéro.

    Retourne True si l'import automatique peut procéder (parsed a été mis à jour au
    passage), False sinon - l'appelant doit alors traiter `destination` comme absent (repli
    sur find_auto_assign_destination, ou laissé pour assignation manuelle sur /import).

    "j'ai quand meme importé cette integrale. mais pourtant dans l'import il n'a pas fait
    de remarque. juste à valider tome 1" - le fichier réellement téléchargé peut ne pas
    être le tome demandé au moment de la recherche (voir unconfirmed_volume/
    _confirms_requested_volume côté searcher.py, même mismatch possible ici si l'
    utilisateur télécharge quand même un résultat déjà signalé comme non confirmé): si le
    fichier s'avère être une intégrale/hors-série/one-shot d'après son PROPRE nom, injecter
    quand même le numéro de tome suivi par-dessus créait un état incohérent
    (is_integral=True ET volume=1 en même temps) affiché comme un simple "Tome 1" sans
    rien pour signaler le problème. Ce type détecté dans le fichier prime désormais sur le
    numéro suivi - le conflit est mémorisé (tracked_volume_conflict) pour que l'appelant
    l'affiche, et l'import automatique refuse ce cas (comme pour "pas de numéro trouvé
    nulle part") plutôt que d'importer à l'aveugle sous un mauvais numéro. is_episode inclus
    dans ce même conflit: un fichier téléchargé qui s'avère être un épisode (et non le tome
    suivi) ne doit pas non plus se voir attribuer le numéro de tome par-dessus."""
    conflicting_type = parsed.get('is_integral') or parsed.get('is_hs') or parsed.get('is_episode')
    if conflicting_type and destination.get('volume_number') is not None:
        parsed['tracked_volume_conflict'] = destination['volume_number']
        return False

    # "pourquoi je ne peux pas choisir intégrale, seulement le tome normal" - une
    # correction manuelle (update_active_download_tracking) a pu marquer CE
    # téléchargement suivi comme Intégrale/HS/Épisode plutôt qu'un tome classique
    # (voir _build_active_download_destination) - si le fichier arrivé ne porte lui-même
    # AUCUN type/numéro reconnu dans son propre nom, le type suivi fait autorité, même
    # principe que tracked_volume ci-dessous pour un simple volume_number. Un type déjà
    # détecté dans le fichier lui-même (conflicting_type ci-dessus) reste prioritaire et
    # bloque déjà l'import automatique en cas de désaccord.
    if not conflicting_type and parsed.get('volume') is None:
        if destination.get('is_integral'):
            parsed['is_integral'] = True
            parsed['integral_number'] = destination.get('integral_number')
            return True
        if destination.get('is_hs'):
            parsed['is_hs'] = True
            parsed['hs_number'] = destination.get('hs_number')
            return True
        if destination.get('is_episode'):
            parsed['is_episode'] = True
            parsed['episode_number'] = destination.get('episode_number')
            return True

    tracked_volume = destination.get('volume_number')
    if parsed.get('volume') is None and tracked_volume is not None:
        parsed['volume'] = tracked_volume
    elif tracked_volume is not None and parsed.get('volume') is not None and parsed['volume'] != tracked_volume:
        # "I am downloading this file... but i am doing it from volume 38 search. so you
        # should match it to volume 38 not volume 2 as the title says" - un fichier peut
        # porter sa PROPRE numérotation interne, sans rapport avec la numérotation globale
        # de la collection (ex: "Gilgamesh 02" = 2e fascicule d'un diptyque thématique
        # d'une collection Hachette, alors que ce diptyque occupe le tome 38 de LA
        # COLLECTION) - le numéro suivi vient d'une action utilisateur explicite (clic sur
        # une recherche/un remplacement depuis une fiche série précise, ou d'un match
        # Surveillance déjà confirmé, voir find_active_download_destination) et prime donc
        # sur celui parsé du nom de fichier, contrairement au cas conflicting_type
        # ci-dessus (un type incohérent - intégrale/HS/épisode détecté - reste, lui, un
        # vrai signal d'ambiguïté qui bloque encore l'import automatique).
        parsed['tracked_volume_overridden_from'] = parsed['volume']
        parsed['volume'] = tracked_volume

    if parsed.get('volume') is None and not destination.get('is_oneshot') and not destination.get('is_single_album') and not _auto_import_has_self_numbering(parsed):
        return False
    return True


def _load_all_series_for_auto_import():
    """SELECT s.id, s.library_id, l.path, s.title, s.is_oneshot, l.name FROM series JOIN libraries -
    factorisé hors de find_auto_assign_destination/_auto_import_skip_reason pour être
    chargé UNE SEULE FOIS par tick plutôt qu'une fois par fichier candidat (voir leur
    paramètre all_series). Même correctif que get_trackable_active_downloads pour
    active_downloads ("pourquoi ca met scanner en loading") - jamais appliqué à CETTE
    requête-ci malgré le même risque en pratique: le scheduler périodique (toutes les 5s,
    voir library/scheduler.py) rouvrait une connexion et re-fetchait les ~360+ séries de
    la base pour chaque fichier encore présent dans les répertoires surveillés, à chaque
    passage, indéfiniment."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT s.id, s.library_id, l.path, s.title, s.is_oneshot, l.name
        FROM series s
        JOIN libraries l ON s.library_id = l.id
    ''')
    all_series = cursor.fetchall()
    conn.close()
    return all_series


def _resolve_auto_import_series_match(title, folder_name, all_series):
    """Résout la série correspondant à `title` (titre parsé du fichier) ou, en repli, à
    `folder_name` - point d'entrée UNIQUE pour cette résolution, partagé entre
    find_auto_assign_destination ET _auto_import_skip_reason pour que la raison affichée
    sur /import ("Aucune série existante") ne puisse jamais diverger de ce que
    l'auto-import ferait réellement pour ce même fichier.

    "why Pas repris par l'import automatique : Aucune série existante" alors que la série
    est clairement en base ("Les rugbymen") - constaté sur un dossier nommé selon la
    convention de tri très répandue localement "Titre (Article)" ("Rugbymen (Les) [HD]").
    _strip_bracketed_suffix (voir _normalize_title_for_match) retire ce groupe ENTIER
    plutôt que de le réordonner, perdant l'article - "rugbymen" normalisé ne matche alors
    jamais "les rugbymen". get_suffix_article_variants (search/routes.py, déjà utilisée
    pour le matching EBDZ/Bédéthèque - "Le grand vide (Murawiec) could not match ebdz only
    if i ask about grand vide without le", même bug déjà corrigé là-bas) réordonne
    "Titre (Article)" en "Article Titre" - jamais appliquée jusqu'ici à ce matching-ci.

    Un simple ebdz_title_variants(folder_name) NE SUFFIT PAS ici: son "cœur" (ebdz_core_title)
    retire TOUS les groupes crochets/parenthèses de fin D'UN COUP avant d'essayer
    get_suffix_article_variants dessus, qui exige que le groupe article soit le TOUT
    DERNIER de la chaîne - avec un second tag après lui ("Rugbymen (Les) [HD]", le "[HD]"
    de qualité), l'article a déjà disparu avec le reste avant que cette fonction ait pu le
    voir. On essaie donc get_suffix_article_variants à CHAQUE étape intermédiaire du
    pelage (un groupe à la fois), pas seulement sur la chaîne brute et le cœur final."""
    from blueprints.search.routes import get_suffix_article_variants

    def _candidates(text):
        states = [text]
        current = text
        while True:
            stripped = re.sub(r'\s*[\(\[][^\)\]]*[\)\]]\s*$', '', current).strip()
            if stripped == current:
                break
            current = stripped
            states.append(current)
        variants = list(states)
        for state in states:
            variants.extend(get_suffix_article_variants(state))
        return list(dict.fromkeys(v for v in variants if v))

    for candidate in _candidates(title):
        match = _match_series_for_auto_import(_normalize_title_for_match(candidate), all_series)
        if match:
            return match
    if folder_name:
        for candidate in _candidates(folder_name):
            match = _match_series_for_auto_import(_normalize_title_for_match(candidate), all_series)
            if match:
                return match
    return None


def find_auto_assign_destination(parsed, config, folder_name=None, all_series=None):
    """Trouve la destination automatique pour un fichier

    Args:
        parsed: Dictionnaire de données parsées du nom de fichier
        config: Configuration d'import
        folder_name: nom du dossier contenant directement le fichier (voir
            scan_import_directory) - repli si le titre parsé du FICHIER ne matche aucune
            série. Un pack multi-tomes nommé d'après la série ("Jack Palmer (CBZ)/15 Palmer
            en Bretagne.cbz") a des noms de fichiers individuels qui ne contiennent souvent
            pas le titre de la série du tout, alors que le nom du dossier, lui, le porte -
            sans ce repli ces fichiers restaient "Aucune série existante" malgré une série
            déjà bien présente en base.
        all_series: liste déjà chargée (voir _load_all_series_for_auto_import) à fournir
            quand cette fonction est appelée en boucle sur plusieurs fichiers (scheduler
            périodique) - None (par défaut) déclenche un chargement à la demande, pour un
            appel isolé sur un seul fichier (attempt_immediate_auto_import).

    Returns:
        Dictionnaire avec destination ou None
    """
    try:
        title = parsed.get('title', '').strip()

        # Rechercher une série existante avec un titre similaire - comparaison tolérante
        # (accents/ponctuation/casse ignorés, voir _normalize_title_for_match) plutôt
        # qu'une égalité SQL stricte: un nom de fichier de scan ne reproduit pas toujours
        # exactement le titre en base (ex: "recit" au lieu de "récit", tiret manquant) -
        # sans quoi un fichier par ailleurs correctement identifié n'auto-s'assignait
        # jamais. Comparaison faite en Python (pas de NFKD en SQL) sur l'ensemble des
        # séries, comme d'autres matchings de l'app (recherche EBDZ locale en mémoire).
        if all_series is None:
            all_series = _load_all_series_for_auto_import()

        result = _resolve_auto_import_series_match(title, folder_name, all_series)

        if result:
            series_id, library_id, library_path, series_title, is_oneshot, library_name = result
            # Bédéthèque peut définir une série à album unique sans la marquer
            # « one-shot ». Elle reste néanmoins une destination non ambiguë.
            conn = get_db_connection()
            single_album_row = conn.execute(
                'SELECT bedetheque_total_volumes FROM series WHERE id = ?', (series_id,)
            ).fetchone()
            conn.close()
            is_single_album = bool(single_album_row and single_album_row[0] == 1)

            # Fichier sans numéro de tome (one-shot, voir can_auto_assign): à n'auto-
            # assigner que si la série trouvée est elle-même déjà marquée one-shot en
            # base, OU si le fichier porte son propre numéro de hors-série/intégrale
            # (_auto_import_has_self_numbering) - sinon un fichier dont le numéro n'a
            # simplement pas pu être parsé (nom de fichier mal formé) pourrait
            # s'auto-importer sans tome associé dans une série numérotée normale.
            if parsed.get('volume') is None and not is_oneshot and not is_single_album and not _auto_import_has_self_numbering(parsed):
                return None

            return {
                'series_id': series_id,
                'library_id': library_id,
                'library_path': library_path,
                # "Cannot read properties of undefined (reading 'replace')" - bug réel:
                # ce dict n'avait pas 'library_name' (contrairement à celui de
                # find_active_download_destination, qui l'a toujours eu) - _importFileRowHtml
                # (import.js) appelle inconditionnellement escapeHtml(file.destination.
                # library_name) pour l'affichage "📍 Bibliothèque → Série", plantant tout
                # le rendu du tableau dès qu'UN SEUL fichier était résolu via CE repli
                # (find_auto_assign_destination) plutôt que via un téléchargement suivi.
                'library_name': library_name,
                'series_title': series_title,
                'is_new_series': False,
                'is_oneshot': bool(is_oneshot),
                'is_single_album': is_single_album
            }

        # Pas de série existante encontrée, et création auto désactivée
        return None

    except Exception as e:
        print(f"Erreur lors de la recherche de destination: {e}")
        return None


def _no_volume_skip_reason(parsed, file_destination):
    """Explique pourquoi apply_tracked_volume_and_gate a refusé ce fichier ("il faut que
    l'interface indique pourquoi l'import n'est pas possible") - avant ce correctif, un
    fichier bloqué pour cette raison précise (celle la plus fréquente en pratique: aucun
    numéro de tome trouvé nulle part, série pas one-shot) n'affichait AUCUNE explication
    sur /import, contrairement aux autres cas de blocage déjà couverts (override manuel,
    échecs répétés, import désactivé) - il restait juste "en attente" sans qu'on sache
    pourquoi, ni ce qu'il fallait faire pour le débloquer.

    file_destination peut être None (aucune destination trouvée du tout - un fichier non
    rattaché à un téléchargement suivi n'entre jamais dans cette gate, voir
    _append_scanned_file, mais le paramètre reste défensif)."""
    if parsed.get('tracked_volume_conflict') is not None:
        kind = 'une intégrale' if parsed.get('is_integral') else ('un hors-série' if parsed.get('is_hs') else 'un épisode')
        return (
            f"Conflit de tome : ce fichier est {kind} d'après son nom, mais le tome "
            f"{parsed['tracked_volume_conflict']} était attendu - assignation manuelle nécessaire"
        )
    is_oneshot = bool(file_destination and file_destination.get('is_oneshot'))
    is_single_album = bool(file_destination and file_destination.get('is_single_album'))
    if not is_oneshot and not is_single_album:
        return (
            "Aucun numéro de tome trouvé dans le nom du fichier et la série n'est pas "
            "marquée « one-shot » - marquez la série comme one-shot (Éditer manuellement) "
            "ou assignez ce fichier manuellement"
        )
    return None


def _repeated_failure_skip_reason(filepath):
    """"l'import en cours ne s'arrête pas" - un fichier déjà échoué MAX_AUTO_IMPORT_FAILURES
    fois de suite (voir library/scheduler.py, self._failure_counts) n'est plus retenté par
    le scheduler - sans cette vérification, /import continuait pourtant d'afficher "⏳
    Import en cours" indéfiniment pour ce même fichier (aucune autre raison de blocage ne
    s'appliquait), laissant croire à tort qu'une importation était encore en train de se
    faire alors que le scheduler avait déjà abandonné. Vérifié EN AMONT de l'éventuel
    court-circuit "destination déjà connue via active_downloads" côté scan_import_directory:
    l'abandon s'applique quelle que soit la façon dont la destination a été résolue."""
    from .scheduler import (
        library_import_scheduler,
        MAX_AUTO_IMPORT_FAILURES,
        MAX_AUTO_IMPORT_CORRUPTION_RETRIES,
        AUTO_IMPORT_CORRUPTION_RETRY_DELAYS,
    )
    failure_count = library_import_scheduler._failure_counts.get(filepath, 0)
    last_error = library_import_scheduler._failure_last_error.get(filepath)
    is_corruption = (last_error or '').startswith('Fichier corrompu')
    exhausted = (
        failure_count > MAX_AUTO_IMPORT_CORRUPTION_RETRIES
        if is_corruption
        else failure_count >= MAX_AUTO_IMPORT_FAILURES
    )
    if exhausted:
        # "beaucoup trop complexe: juste met le nom du fichier et qu'il est corrompu.
        # correction manuelle nécessaire?? ben si le fichier est corrompu alors il faut
        # le retélécharger" - last_error portait le détail technique complet (CRC
        # attendu/obtenu, chemin interne à l'archive), utile pour un diagnostic profond
        # mais incompréhensible ici où l'action à faire est toujours la même et simple:
        # retélécharger le fichier. Le nom du fichier lui-même est déjà affiché juste
        # au-dessus par le frontend (voir import.js, filename dans la ligne du tableau)
        # - pas la peine de le redire non plus dans ce message.
        if is_corruption:
            return "Fichier corrompu - à retélécharger"
        # "échec s'arrête a 1 essai" (MAX_AUTO_IMPORT_FAILURES=1) - "Échec répété" sous-
        # entend plusieurs tentatives, trompeur quand une seule a suffi à abandonner.
        detail = f" ({last_error})" if last_error else ""
        label = "Échec" if failure_count <= 1 else f"Échec répété ({failure_count}x)"
        return f"{label}{detail} - correction manuelle nécessaire"
    if is_corruption and failure_count:
        last_failed_at = library_import_scheduler._failure_last_at.get(filepath, 0)
        retry_delay = AUTO_IMPORT_CORRUPTION_RETRY_DELAYS[
            min(failure_count - 1, len(AUTO_IMPORT_CORRUPTION_RETRY_DELAYS) - 1)
        ]
        remaining = max(0, int(retry_delay - (time.time() - last_failed_at)))
        return f"Fichier encore en copie réseau - nouvel essai automatique dans {remaining}s"
    return None


def attempt_immediate_auto_import(filepath, import_root):
    """"tu peux directement demander une importation automatique quand le fichier est
    téléchargé et bien matché. Pas besoin de cette fréquence" - déclenché juste après un
    téléchargement Telegram terminé (download_channel_file_background, telegram_channels/
    scraper.py), qui garantit déjà (voir _download_one) que le fichier est complet à cet
    instant précis - contrairement au scheduler périodique (encore utilisé pour aMule/
    qBittorrent/rTorrent/Deluge, qui eux ne préviennent jamais l'appli quand un
    téléchargement se termine, seul un scan du répertoire peut les découvrir), pas besoin
    ici de la vérification de stabilité de taille entre deux passages (self._file_size_history
    côté scheduler.py) : ce fichier précis vient d'être vérifié complet par Telethon avant
    même d'être renommé vers son nom final.

    Reprend le même matching/gating qu'un passage du scheduler périodique (_auto_import,
    library/scheduler.py) mais pour CE seul fichier, sans attendre le prochain tick.

    Nécessite un contexte applicatif Flask actif (appelant: with app.app_context()).
    Best-effort silencieux (jamais d'exception remontée) - un échec ici ne doit jamais
    faire paraître le téléchargement Telegram lui-même en échec, et le fichier reste de
    toute façon repris par le scheduler périodique au passage suivant si cette tentative
    immédiate échoue ou si l'import automatique était désactivé à ce moment précis."""
    try:
        config = load_library_import_config()
        if not config.get('auto_import_enabled', False):
            return

        filename = os.path.basename(filepath)
        ext = os.path.splitext(filename)[1].lower()
        supported_extensions = set(config.get(
            'monitored_extensions', ['.cbz', '.cbr', '.zip', '.rar', '.pdf']
        ))
        if ext not in supported_extensions:
            return

        from .import_history import get_manual_override_filepaths, get_in_progress_filepaths
        if filepath in get_manual_override_filepaths():
            return

        # Déjà réclamé par un batch d'import en cours (scheduler périodique ou un autre
        # déclenchement immédiat) - voir init_import_in_progress_table, scan_import_lock.
        if filepath in get_in_progress_filepaths():
            return

        from .scanner import LibraryScanner
        from blueprints.missing_monitor.downloader import get_trackable_active_downloads
        scanner = LibraryScanner()
        parsed = scanner.parse_filename(filename)

        relative_path = os.path.relpath(filepath, import_root)
        parent_dir = os.path.dirname(relative_path)
        folder_name = os.path.basename(parent_dir) if parent_dir else None

        trackable_downloads = get_trackable_active_downloads()
        destination = find_active_download_destination(filename, trackable_downloads)
        if destination and not apply_tracked_volume_and_gate(parsed, destination):
            destination = None
        # Même règle que le scheduler : un téléchargement terminé doit être rattaché
        # à une ligne active_downloads connue. Un fichier Telegram/aMule/qBittorrent
        # inconnu reste manuel plutôt que d’être associé par similarité de titre.

        if not destination:
            return

        success, stats = execute_auto_import([{
            'filename': filename, 'filepath': filepath, 'import_root': import_root,
            'file_size': os.path.getsize(filepath), 'parsed': parsed, 'destination': destination
        }])
        if success:
            print(f"✓ Import automatique immédiat: {filename}")
    except Exception as e:
        print(f"Erreur import automatique immédiat ({filepath}): {e}")


def execute_auto_import(files_to_import):
    """Exécute l'import automatique des fichiers (scheduler périodique, voir
    library/scheduler.py, et déclenchement immédiat Telegram, voir
    attempt_immediate_auto_import) - voir _execute_import_batch pour le cœur partagé avec
    execute_import (manuel).

    Args:
        files_to_import: Liste des fichiers à importer (chaque entrée porte son
            propre 'import_root', le répertoire d'import surveillé d'où il provient)

    Returns:
        Tuple (success: bool, stats: dict) avec les statistiques d'import
    """
    lock_acquired, operation_id, stats = _execute_import_batch(
        files_to_import, operation_type='auto_import', lock_timeout=60,
        strict_missing_file=False, notify_source='automatique'
    )
    if not lock_acquired:
        print("⚠️ Import automatique reporté: un import est déjà en cours (nouvelle tentative au prochain cycle)")
        return False, {
            'operation_id': None, 'imported_count': 0, 'replaced_count': 0,
            'skipped_count': 0, 'failed_count': 0, 'failures': []
        }

    return not stats.get('error'), {
        'operation_id': operation_id,
        'imported_count': stats['imported_count'],
        'replaced_count': stats['replaced_count'],
        'skipped_count': stats['skipped_count'],
        'failed_count': stats['failed_count'],
        # "l'import en cours ne s'arrête pas" - le détail par fichier (nom + erreur, voir
        # _execute_import_batch) était calculé mais jamais renvoyé jusqu'ici à cet appelant
        # précis (execute_import, la route HTTP manuelle, le renvoyait déjà) - nécessaire
        # à library/scheduler.py pour compter les échecs consécutifs par fichier et
        # arrêter de le retenter indéfiniment (voir MAX_AUTO_IMPORT_FAILURES).
        'failures': stats['failures']
    }

# ========== ROUTES API D'IMPORT AUTOMATIQUE ==========

@library_bp.route('/api/import/config', methods=['GET', 'POST'])
def import_config():
    """Récupère ou met à jour la configuration d'import automatique"""
    
    if request.method == 'GET':
        config = load_library_import_config()
        return jsonify(config)
    
    else:  # POST
        data = request.get_json()
        config = load_library_import_config()
        
        # Mettre à jour les champs
        if 'auto_import_enabled' in data:
            config['auto_import_enabled'] = data['auto_import_enabled']
        if 'auto_assign_enabled' in data:
            config['auto_assign_enabled'] = data['auto_assign_enabled']
        if 'import_mode' in data:
            config['import_mode'] = data['import_mode'] if data['import_mode'] in ('move', 'hardlink') else 'move'
        if 'auto_convert_to_cbz' in data:
            config['auto_convert_to_cbz'] = data['auto_convert_to_cbz']
        if 'monitored_extensions' in data:
            # Toujours au moins une extension - une liste vide couperait silencieusement
            # l'import ET le badge de fichiers en attente sans aucune extension surveillée
            config['monitored_extensions'] = data['monitored_extensions'] or \
                current_app.config['LIBRARY_IMPORT_CONFIG']['monitored_extensions']
        if 'blocked_search_extensions' in data:
            # Liste vide tolérée (contrairement à monitored_extensions ci-dessus): tout
            # décocher revient juste à ne plus rien exclure de la recherche, aucun risque
            # de casser silencieusement autre chose.
            config['blocked_search_extensions'] = data['blocked_search_extensions'] or []
        if 'auto_acquire_pack_search_enabled' in data:
            config['auto_acquire_pack_search_enabled'] = bool(data['auto_acquire_pack_search_enabled'])
        if 'auto_acquire_on_add_enabled' in data:
            # "auto search and download is not active only in the découvrir... auto
            # download button should be there and if active auto search when adding
            # from everything except découvrir" - voir add_series_from_bedetheque
            # (bedetheque/routes.py, from_discover) pour le déclenchement réel, relu à
            # chaque tome via gate_on_global_setting (auto_acquire.py).
            config['auto_acquire_on_add_enabled'] = bool(data['auto_acquire_on_add_enabled'])
        if 'auto_acquire_sources' in data:
            # Liste vide tolérée ici (contrairement à monitored_extensions ci-dessus):
            # désactiver toutes les sources revient juste à ne jamais rien trouver,
            # aucun risque de casser silencieusement autre chose.
            config['auto_acquire_sources'] = data['auto_acquire_sources'] or []

        if save_library_import_config(config):
            # Redémarrer le scheduler si nécessaire (voir sync_auto_import_schedule -
            # tient aussi compte de la notification Telegram "import disponible", pas
            # seulement de ce réglage)
            from .scheduler import sync_auto_import_schedule
            sync_auto_import_schedule()
            
            return jsonify({'success': True, 'config': config})
        else:
            return jsonify({'error': 'Erreur lors de la sauvegarde'}), 500

# ========== ROUTES API D'HISTORIQUE D'IMPORT ==========

@library_bp.route('/api/actions/log-search', methods=['POST'])
def log_search_action():
    """Journalise une recherche manuelle (EBDZ/Prowlarr/Telegram/fourtoutici) dans le
    même historique que l'acquisition automatique - "aussi ajouter une section recherche
    dans l'historique ou tu mets les recherches (manuelles et auto)". Appelé depuis le
    CLIENT (pas de choke point serveur commun: /search, /discover et la fiche série
    appellent chacun directement les 4 endpoints de recherche par source en parallèle,
    voir searchBoth/searchSources/searchMissingVolume) - un seul appel par recherche
    lancée par l'utilisateur, pas un par source interrogée, pour rester au même grain
    qu'un événement "Recherche automatique" (log_action('auto_acquire', ...),
    blueprints/bedetheque/auto_acquire.py) plutôt que 4 lignes redondantes."""
    try:
        from .action_history import log_action

        data = request.get_json() or {}
        title = data.get('title')
        if not title:
            return jsonify({'success': False, 'error': 'title requis'}), 400
        detail = data.get('detail') or ''
        series_id = data.get('series_id')

        log_action('search', series_id, title, detail, success=True)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@library_bp.route('/api/actions/history', methods=['GET'])
def actions_history():
    """Récupère l'historique des renommages/suppressions (voir action_history.py) -
    query param optionnel ?type=rename|delete pour filtrer côté serveur, sinon les deux."""
    try:
        from .action_history import get_action_history

        limit = request.args.get('limit', 50, type=int)
        action_type = request.args.get('type')
        series_id = request.args.get('series_id', type=int)
        history = get_action_history(limit, action_type, series_id)

        return jsonify({'success': True, 'history': history})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@library_bp.route('/api/actions/history/<int:action_id>', methods=['GET'])
def action_history_detail(action_id):
    """Détail fichier par fichier d'une action de renommage (voir action_history.py,
    colonne files_json posée par _log_rename_action) - même esprit que
    GET /api/import/history/<operation_id>, pour la ligne dépliable "renommage" de
    l'Historique ("ouvrir le nom pour avoir le detail du renommage comme dans import")."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM action_history WHERE id = ?', (action_id,))
        row = cursor.fetchone()
        conn.close()

        if not row:
            return jsonify({'success': False, 'error': 'Action introuvable'}), 404

        action = dict(row)
        detail = json.loads(action['files_json']) if action.get('files_json') else {'files': [], 'folder': None}
        action['files'] = detail.get('files', [])
        action['folder'] = detail.get('folder')
        del action['files_json']

        return jsonify({'success': True, 'action': action})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@library_bp.route('/api/import/history', methods=['GET'])
def import_history():
    """Récupère l'historique des imports"""
    try:
        from .import_history import get_import_history

        limit = request.args.get('limit', 50, type=int)
        series_id = request.args.get('series_id', type=int)
        history = get_import_history(limit, series_id)

        return jsonify({'success': True, 'history': history})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@library_bp.route('/api/import/history/<operation_id>', methods=['GET'])
def import_operation_details(operation_id):
    """Récupère les détails d'une opération d'import"""
    try:
        from .import_history import get_operation_details
        
        details = get_operation_details(operation_id)
        
        if not details:
            return jsonify({'error': 'Opération non trouvée'}), 404
        
        return jsonify({'success': True, 'details': details})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@library_bp.route('/api/import/history/<operation_id>/undo', methods=['POST'])
def undo_import_operation(operation_id):
    """Annule une opération d'import"""
    try:
        from .import_history import undo_import_operation as do_undo
        
        success, message, errors = do_undo(operation_id)
        
        if success:
            return jsonify({'success': True, 'message': message, 'errors': errors})
        else:
            return jsonify({'error': message}), 400
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500
