"""
Envoi automatique des téléchargements aux clients (qBittorrent, aMule)
"""
import os
import re
import json
import sys
import unicodedata
from typing import Dict, List, Optional, Tuple
from flask import current_app
from urllib.parse import urlparse
import sqlite3


def _normalize_filename(text):
    """Normalise un nom de fichier pour comparaison: accents/casse neutralisés, extension
    retirée, TOUT séparateur (espace, point, tiret, parenthèses...) supprimé plutôt que
    remplacé par un espace - deux noms du même fichier ne s'écrivent presque jamais avec
    exactement la même ponctuation ("Les.Naufrages.du.Temps.T03..." vs "Naufragés du temps
    (Les) - T03...") mais leurs caractères alphanumériques, une fois mis bout à bout,
    coïncident.

    Remplace l'ancien matching par mots + indice de Jaccard (voir historique git) - celui-ci
    exigeait un réglage de seuil fragile: un titre de suivi COURT ("Série - Volume N")
    comparé à un vrai nom de release (qui porte plein de tags génériques en plus - version,
    groupe, format...) tombait souvent sous le seuil même pour le BON fichier, ce qui
    cassait l'affichage de la progression réelle (voir activity/routes.py) et laissait la
    ligne "en attente" fantôme indéfiniment. Le nom suivi est maintenant le nom de fichier
    réel du résultat (voir mark_download_pending/downloadTelegramFile), donc une comparaison
    de chaîne stricte est à la fois plus simple et plus fiable qu'un score approximatif."""
    text = unicodedata.normalize('NFD', text.lower())
    text = ''.join(c for c in text if unicodedata.category(c) != 'Mn')
    text = re.sub(r'\.(cbz|cbr|zip|rar|pdf)$', '', text)
    return re.sub(r'[^a-z0-9]+', '', text)


def _filenames_match(a, b):
    """Est-ce que `a` et `b` désignent le même fichier ? Égalité stricte après
    normalisation, ou l'un contenu dans l'autre (une release ajoute parfois un préfixe/
    suffixe - groupe, tag de version - autour du même nom de base). Volontairement PAS un
    recouvrement partiel de mots: une chaîne alphanumérique collée (voir
    _normalize_filename) qui contient réellement l'autre est un signal beaucoup plus sûr
    qu'un score de similarité, et distingue naturellement deux tomes différents de la même
    série (le numéro fait partie de la chaîne comparée, pas un mot à part qu'un seuil
    pourrait ignorer)."""
    na, nb = _normalize_filename(a), _normalize_filename(b)
    if not na or not nb:
        return False
    return na == nb or na in nb or nb in na


def match_pending_download_by_name(client_pending_rows, item_name):
    """Retrouve, parmi les lignes 'pending' d'un client, celle dont le titre correspond à
    item_name (_filenames_match) - UNIQUEMENT une ligne jamais encore reliée par id exact
    (`not row.get('client_item_id')`). Une ligne déjà liée à un id exact n'est plus jamais
    candidate à une correspondance par nom, sans quoi un vrai doublon de torrent (même nom,
    hash différent) peut voler le lien d'une ligne déjà résolue - bug réel constaté en
    production (deux torrents "Rugbymen (Les) [HD]", le 99.8% doublon écrasait le
    client_item_id du 100% déjà lié) et corrigé en centralisant ce garde-fou ici plutôt que
    de le laisser vivre seulement inline dans activity_status."""
    return next(
        (p for p in client_pending_rows if not p.get('client_item_id') and _filenames_match(item_name, p['title'])),
        None
    )


def _title_already_imported(title, imported_filenames):
    """`title` (nom de fichier réel suivi, voir mark_download_pending) désigne-t-il le même
    fichier qu'un des noms déjà importés avec succès ? Voir _filenames_match."""
    return any(_filenames_match(title, filename) for filename in imported_filenames)


def find_pending_download_duplicate(title: Optional[str], series_id: Optional[int] = None,
                                    volume_number: Optional[int] = None) -> Optional[Dict]:
    """Retourne un suivi ``pending`` qui désigne déjà ce résultat.

    Une recherche automatique peut être relancée pendant que le client traite encore
    le même fichier. Le nom réel est le signal commun aux sources; série/tome évitent
    qu'un titre générique d'une autre série bloque la recherche.
    """
    try:
        db_path = current_app.config.get('DATABASE')
        if not db_path:
            return None
        conn = sqlite3.connect(db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, title, client, series_id, volume_number "
            "FROM active_downloads WHERE status = 'pending' ORDER BY id DESC"
        ).fetchall()
        conn.close()
        for row in rows:
            row_series = row['series_id']
            if series_id is not None and row_series is not None and int(row_series) != int(series_id):
                continue
            row_title = row['title'] or ''
            if title and row_title and _filenames_match(title, row_title):
                return dict(row)
            if (not title or not row_title) and series_id is not None and row_series is not None \
                    and int(row_series) == int(series_id) and row['volume_number'] == volume_number:
                return dict(row)
        return None
    except Exception as exc:
        print(f"Erreur vérification téléchargement déjà en attente: {exc}")
        return None


def log_manual_download(title: str, client: str, success: bool, message: str = '',
                         source: Optional[str] = None, source_link: Optional[str] = None,
                         tracking_id: Optional[int] = None) -> bool:
    ""
    try:
        db_path = current_app.config.get('DATABASE')
        if not db_path:
            return False

        conn = sqlite3.connect(db_path, timeout=30.0)
        cursor = conn.cursor()

        updated = False
        if tracking_id is not None:
            cursor.execute('''
                UPDATE missing_volume_downloads SET
                    success = ?, message = ?, source = ?, source_link = ?
                WHERE tracking_id = ?
            ''', (1 if success else 0, message, source, source_link, tracking_id))
            updated = cursor.rowcount > 0

        if not updated:
            cursor.execute('''
                INSERT INTO missing_volume_downloads
                (title, volume_number, client, success, message, source, source_link, tracking_id, created_at)
                VALUES (?, NULL, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ''', (title, client, 1 if success else 0, message, source, source_link, tracking_id))

        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Erreur log download manuel: {e}")
        return False


TELEGRAM_RETRY_AFTER_STALLED_MINUTES = 1
# Abandon définitif après ce nombre de relances - un message Telegram structurellement
# injoignable (supprimé, canal quitté...) ne doit pas être retenté indéfiniment toutes les
# AUTO_IMPORT_FALLBACK_INTERVAL_MINUTES pour de bon.
TELEGRAM_MAX_RETRY_ATTEMPTS = 3


def mark_download_pending(title: str, client: str, series_id: Optional[int] = None,
                           volume_id: Optional[int] = None,
                           volume_number: Optional[int] = None,
                           channel: Optional[str] = None,
                           message_id: Optional[int] = None,
                           retry_count: int = 0,
                           bytes_total: Optional[int] = None,
                           client_item_id: Optional[str] = None,
                           force_replace: bool = False) -> Optional[int]:
    """Enregistre un téléchargement tout juste lancé dans active_downloads ('pending') -
    "when adding a new file whatever source. it should be automatically added to import.
    then you can poll to get its status": contrairement à missing_volume_downloads (un
    journal d'événements finaux, voir log_manual_download), une ligne ici représente un
    téléchargement EN COURS, retirée par get_pending_downloads une fois le fichier importé
    ou expirée. Retourne l'id inséré (utile à Telegram, seule source qui peut ensuite faire
    la transition pending -> completed/failed sur CETTE même ligne via son propre thread -
    voir mark_download_completed/mark_download_failed) ou None en cas d'échec.

    title: le nom de fichier RÉEL du résultat ajouté (pas un libellé "Série - Volume N"
    reconstruit) - "the name should be the file downloaded so finding the right client
    download stats is easy": c'est ce nom qui est comparé au nom que renvoie le client
    (voir _filenames_match/activity_status), une comparaison de chaîne fiable seulement si
    les deux désignent le même fichier.

    series_id/volume_id/volume_number: identité RÉELLE déjà connue au moment du clic
    (recherche/remplacement depuis une fiche série, voir searchMissingVolume côté
    library.js, ou le monitoring automatique de volumes manquants) - None pour une
    recherche libre (/search) sans contexte série/tome. Stockés tels quels plutôt que
    re-devinés plus tard: voir find_active_download_match, qui les relit une fois le
    fichier arrivé sur disque au lieu de re-parser son nom.

    channel/message_id: identité Telegram du message source (None pour tout autre
    client) - "yes relaunch and delete the stalled download from telegram": sans ça,
    rien ne permet de redemander ce même fichier à Telegram si le téléchargement meurt en
    route (voir retry_stalled_telegram_downloads), le navigateur qui a fait le clic
    initial étant la seule autre source de cette info, jamais persistée jusqu'ici.

    retry_count: reporté tel quel par retry_stalled_telegram_downloads sur la ligne
    qu'elle recrée après une relance, pour que le compteur de tentatives survive au
    remplacement de la ligne - 0 pour un tout premier lancement normal.

    bytes_total: "dans le téléchargement amule je n'ai pas l'info de la taille du
    fichier" - amulecmd ("show dl") ne rapporte QUE des comptes de parts eD2K, jamais une
    taille réelle en octets (voir _amule_status, activity/routes.py) ; le lien ed2k lui-
    même la contient en revanche (ed2k://|file|<nom>|<taille>|<hash>|/), déjà connue au
    moment de l'ajout (voir add_to_emule, emule/routes.py) - stockée ici pour que
    activity_status() puisse la restituer plus tard sans avoir à la re-parser. None pour
    tout autre client (déjà exposée nativement par leur API respective) ou si le lien
    n'a pas pu être décodé.

    client_item_id: "once you add the file to the client ask for its id so you can put in
    the db and use it later" - l'id que le CLIENT lui-même utilisera pour ce
    téléchargement (hash BitTorrent pour qBittorrent/rTorrent/Deluge, hash eD2K pour
    aMule), connu à l'ajout plutôt que redemandé au client ensuite: un magnet le contient
    déjà tel quel, un .torrent permet de le calculer nous-mêmes (voir torrent_hash.py,
    racine du projet - SHA1 du dict "info" bencodé, la définition même du hash
    BitTorrent), un lien ed2k aussi (voir add_to_emule, emule/routes.py). Stocké dès
    l'insertion pour que find_active_downloads_by_client_item_ids le retrouve par id exact
    dès le tout premier sondage - plus aucune correspondance de nom nécessaire pour ce
    téléchargement. None quand il n'a pas pu être déterminé à l'ajout (ex: rTorrent avec
    une URL de .torrent, que rTorrent télécharge lui-même sans nous en laisser une copie) -
    le lien se fait alors comme avant, par nom, au premier sondage qui le voit.

    force_replace: "Remplacer quand même" - vient d'un clic explicite sur "Rechercher un
    remplacement" pour un tome DÉJÀ possédé (searchMissingVolume/currentVolumeId côté
    library.js), jamais d'une recherche/un monitoring de tome manquant. Stocké tel quel
    sur la ligne pour survivre jusqu'à l'import (voir _build_active_download_destination,
    library/routes.py) où il fait sauter la comparaison de taille is_better_volume - la
    même dérogation que "Remplacer le fichier" (upload manuel), mais pour un remplacement
    trouvé par recherche plutôt qu'envoyé à la main."""
    try:
        db_path = current_app.config.get('DATABASE')
        if not db_path:
            return None

        # "for import there should be a database of all the downloads. this is the base
        # reference that should be used for display" - is_pack/expected_volume_count
        # calculés ICI, une seule fois, depuis `title` (le nom de fichier/release RÉEL,
        # voir docstring plus haut) plutôt que redevinés plus tard à chaque rendu de
        # /import - même parseur que _has_pack_keyword/_detect_pack_size côté
        # bedetheque/auto_acquire.py (qui calculaient déjà cette même info puis la
        # jetaient, jamais transmise jusqu'à mark_download_pending). volume_number is
        # None est un garde-fou nécessaire: un téléchargement déjà rattaché avec
        # certitude à UN tome précis (fiche série/monitoring) ne doit jamais être
        # reclassé "pack" sur la seule coïncidence qu'un mot de son nom de release y
        # ressemble.
        from blueprints.library.scanner import LibraryScanner
        parsed_pack_info = LibraryScanner.parse_filename(title)
        tome_range = LibraryScanner._parse_integral_tome_range(title)
        detected_expected_count = (tome_range[1] - tome_range[0] + 1) if tome_range else None
        is_pack = volume_number is None and (bool(parsed_pack_info.get('is_pack')) or detected_expected_count is not None)
        expected_volume_count = detected_expected_count if is_pack else None

        conn = sqlite3.connect(db_path, timeout=30.0)
        cursor = conn.cursor()
        # Si l'appelant connaît la série et le numéro mais pas encore l'id du volume,
        # résout ici le volume canonique afin que le suivi survive au renommage du fichier.
        # On garde le volume_id fourni explicitement prioritaire.
        if volume_id is None and series_id is not None and volume_number is not None:
            resolved_volume = cursor.execute(
                "SELECT id FROM volumes WHERE series_id = ? AND volume_number = ? "
                "ORDER BY CASE WHEN filepath IS NULL OR filepath = '' THEN 0 ELSE 1 END, id DESC LIMIT 1",
                (series_id, volume_number)
            ).fetchone()
            if resolved_volume:
                volume_id = resolved_volume[0]
        # process_instance_id: voir son commentaire dans config.py - identifie le
        # démarrage du process qui a posé cette ligne, pour distinguer plus tard "encore
        # en file dans CE process" de "orpheline, son thread est mort avec un précédent
        # démarrage" (retry_stalled_telegram_downloads, downloader.py).
        process_instance_id = current_app.config.get('PROCESS_INSTANCE_ID')
        # Un même résultat ne doit pas créer plusieurs suivis si le bouton/recherche a
        # été déclenché plusieurs fois avant que le client ne réponde. Le hash client est
        # le signal le plus fiable; à défaut, le nom réel + série/tome identifient le
        # même fichier suivi. On réutilise la ligne pending existante au lieu d'en créer
        # une seconde qui polluerait Import.
        existing = cursor.execute(
            "SELECT id FROM active_downloads WHERE status = 'pending' AND client = ? "
            "AND ((client_item_id IS NOT NULL AND client_item_id = ?) "
            "OR (client_item_id IS NULL AND title = ? AND COALESCE(series_id, -1) = COALESCE(?, -1) "
            "AND COALESCE(volume_number, -1) = COALESCE(?, -1))) "
            "ORDER BY id DESC LIMIT 1",
            (client, client_item_id.lower() if client_item_id else None, title, series_id, volume_number)
        ).fetchone()
        if existing:
            conn.close()
            return existing[0]

        process_instance_id = current_app.config.get('PROCESS_INSTANCE_ID')
        cursor.execute(
            "INSERT INTO active_downloads (title, client, status, series_id, volume_id, volume_number, last_progress_at, channel, message_id, retry_count, process_instance_id, bytes_total, is_pack, expected_volume_count, client_item_id, force_replace) "
            "VALUES (?, ?, 'pending', ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (title, client, series_id, volume_id, volume_number, channel, message_id, retry_count, process_instance_id, bytes_total, int(is_pack), expected_volume_count, client_item_id.lower() if client_item_id else None, int(force_replace))
        )
        download_id = cursor.lastrowid

        cursor.execute(
            "INSERT INTO missing_volume_downloads (title, volume_number, client, success, message, tracking_id, created_at) "
            "VALUES (?, ?, ?, NULL, NULL, ?, CURRENT_TIMESTAMP)",
            (title, volume_number, client, download_id)
        )

        conn.commit()
        conn.close()
        return download_id
    except Exception as e:
        print(f"Erreur enregistrement téléchargement en attente ({client}): {e}")
        return None


def attach_series_to_pending_download(client: str, series_id: int, link: Optional[str] = None,
                                       channel: Optional[str] = None, message_id: Optional[int] = None) -> bool:
    ""
    try:
        db_path = current_app.config.get('DATABASE')
        if not db_path or not series_id:
            return False
        conn = sqlite3.connect(db_path, timeout=10.0)
        if client == 'amule' and link:
            from blueprints.emule.ed2k_stats import extract_ed2k_hash
            item_id = extract_ed2k_hash(link)
            if not item_id:
                conn.close()
                return False
            cursor = conn.execute(
                "UPDATE active_downloads SET series_id = ? WHERE client = 'amule' AND client_item_id = ? "
                "AND status = 'pending' AND series_id IS NULL",
                (series_id, item_id.lower())
            )
        elif client == 'telegram' and channel and message_id is not None:
            cursor = conn.execute(
                "UPDATE active_downloads SET series_id = ? WHERE client = 'telegram' AND channel = ? "
                "AND message_id = ? AND status = 'pending' AND series_id IS NULL",
                (series_id, channel, message_id)
            )
        else:
            conn.close()
            return False
        conn.commit()
        updated = cursor.rowcount > 0
        conn.close()
        return updated
    except Exception as e:
        print(f"Erreur rattachement série à un téléchargement en attente ({client}): {e}")
        return False


def get_trackable_active_downloads(include_failed=False) -> List[Dict]:
    """Les active_downloads récents porteurs d'un series_id réel (donc posés depuis une
    fiche série ou le monitoring automatique - voir mark_download_pending), sous forme de
    liste brute plutôt que déjà comparés à un nom de fichier précis - voir
    find_active_download_match, qui faisait ce SELECT à chaque appel. scan_import_directory
    (routes.py) et le scan automatique (library/scheduler.py) l'appellent une fois par
    FICHIER scanné: avec des dizaines de fichiers en attente, ça multipliait les
    allers-retours SQLite pour rien ("pourquoi ca met scanner en loading" - un scan qui
    ouvre 2 connexions SQLite par fichier juste pour ce matching se voit). Un seul appel en
    tête de boucle, le matching lui-même reste en Python ensuite (voir
    match_filename_against_trackable_downloads).

    "why is this still looping... 16 ignorés" - bug réel: une ligne déjà terminale
    ('imported'/'skipped'/'failed' - "once Doublon ignoré -> state will be imported.
    so nothing should go after that") restait quand même candidate au matching ici,
    sans filtre sur status. Un fichier venant d'une source non-inscriptible (aMule) ne
    disparaît jamais du répertoire surveillé - chaque passage du scheduler (5s) le
    retrouvait, le rematchait à sa ligne déjà résolue, et relançait tout le pipeline
    d'import pour reconfirmer un résultat déjà connu, journalisant une nouvelle
    opération Historique à chaque fois. Filtré sur les statuts encore OUVERTS plutôt
    que d'énumérer chaque statut terminal (liste qui n'aurait fait que s'allonger) -
    strictement une requête base de données, aucune vérification de fichier sur
    disque: une ligne terminale n'a simplement plus rien à matcher."""
    try:
        db_path = current_app.config.get('DATABASE')
        if not db_path:
            return []

        conn = sqlite3.connect(db_path, timeout=30.0)
        cursor = conn.cursor()
        statuses = "('pending', 'completed', 'importing', 'failed')" if include_failed else "('pending', 'completed', 'importing')"
        cursor.execute(f'''
            SELECT id, title, series_id, volume_id, volume_number, is_pack, expected_volume_count,
                   client, client_item_id, force_replace,
                   is_integral, integral_number, is_hs, hs_number, is_episode, episode_number
            FROM active_downloads
            WHERE series_id IS NOT NULL
              AND status IN {statuses}
            ORDER BY created_at DESC
        ''')
        # client/client_item_id: "can the clients api tell you the location of the files
        # so it will be easier to match" - nécessaires pour retrouver, via l'API du client
        # de téléchargement (voir get_qbittorrent_torrent_names, qbittorrent/routes.py),
        # le nom RÉEL du torrent pour ce hash - un signal fiable pour relier un dossier
        # trouvé sur disque à SON téléchargement suivi sans dépendre du nom des fichiers
        # extraits (voir find_active_download_destination_by_torrent_name, library/routes.py).
        rows = [
            {'id': row_id, 'title': title, 'series_id': series_id, 'volume_id': volume_id, 'volume_number': volume_number,
             'is_pack': bool(is_pack), 'expected_volume_count': expected_volume_count,
             'client': client, 'client_item_id': client_item_id, 'force_replace': bool(force_replace),
             'is_integral': bool(is_integral), 'integral_number': integral_number,
             'is_hs': bool(is_hs), 'hs_number': hs_number,
             'is_episode': bool(is_episode), 'episode_number': episode_number}
            for row_id, title, series_id, volume_id, volume_number, is_pack, expected_volume_count, client, client_item_id, force_replace,
                is_integral, integral_number, is_hs, hs_number, is_episode, episode_number
            in cursor.fetchall()
        ]
        conn.close()
        return rows
    except Exception as e:
        print(f"Erreur lecture téléchargements suivis actifs: {e}")
        return []


def get_pending_download_state_for_series(series_id: int):
    ""
    try:
        db_path = current_app.config.get('DATABASE')
        if not db_path:
            return False, set()
        conn = sqlite3.connect(db_path, timeout=30.0)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT is_pack, volume_number FROM active_downloads WHERE series_id = ? AND status = 'pending'",
            (series_id,)
        )
        rows = cursor.fetchall()
        conn.close()
        has_pending_pack = any(is_pack for is_pack, _ in rows)
        pending_volume_numbers = {vn for _, vn in rows if vn is not None}
        return has_pending_pack, pending_volume_numbers
    except Exception as e:
        print(f"Erreur vérification téléchargements en attente pour série #{series_id}: {e}")
        return False, set()


def match_filename_against_trackable_downloads(filename: str, rows: List[Dict]) -> Optional[Dict]:
    """Matching pur Python (voir _filenames_match) contre une liste déjà chargée (voir
    get_trackable_active_downloads) - retourne {'series_id':, 'volume_id':,
    'volume_number':} ou None, sans toucher la DB."""
    for row in rows:
        if _filenames_match(row['title'], filename):
            return {
                'tracking_id': row['id'], 'series_id': row['series_id'],
                'volume_id': row['volume_id'], 'volume_number': row['volume_number'],
                'force_replace': row.get('force_replace', False),
            }
    return None


def find_active_downloads_by_client_item_ids(client: str, client_item_ids) -> Dict[str, Dict]:
    """"you have all the information already in the database. so no need to do any
    fuzzy-matching" - une fois qu'un item client a été relié une première fois à sa ligne
    active_downloads par correspondance de nom (voir set_active_download_client_item_id,
    seul chemin qui écrit client_item_id), tous les sondages suivants le retrouvent par CET
    id exact - plus aucune comparaison de nom nécessaire pour ce téléchargement.

    Version par lot (un seul aller-retour DB pour tous les items d'un client à chaque
    appel de /api/activity/status) plutôt qu'une connexion SQLite par item - la version
    précédente ouvrait une connexion par item à chaque appel, un N+1 inutile pour un
    client qui suit plusieurs téléchargements à la fois. Retourne un dict
    client_item_id (minuscule) -> {'tracking_id':, 'series_id':, 'volume_number':,
    'series_title':}, uniquement pour les ids réellement reliés (absent du dict = jamais
    relié, cas normal du tout premier sondage après l'ajout - voir activity_status, qui
    retombe alors sur la correspondance de nom UNE fois, puis persiste ce lien).

    Comparaison insensible à la casse (normalisée en minuscules des deux côtés, voir
    set_active_download_client_item_id): qBittorrent/Deluge rapportent leurs hash en
    minuscules, rTorrent en MAJUSCULES, amulecmd en MAJUSCULES aussi - une comparaison de
    chaîne stricte aurait fait rater le lien pour rTorrent/aMule alors même que c'est
    exactement le même id."""
    result: Dict[str, Dict] = {}
    ids = [cid.lower() for cid in client_item_ids if cid]
    if not ids:
        return result
    try:
        db_path = current_app.config.get('DATABASE')
        if not db_path:
            return result
        conn = sqlite3.connect(db_path, timeout=30.0)
        cursor = conn.cursor()
        placeholders = ','.join('?' for _ in ids)
        cursor.execute(
            f"SELECT ad.client_item_id, ad.id, ad.series_id, ad.volume_number, ad.created_at, s.title, s.is_oneshot, "
            f"ad.is_integral, ad.integral_number, ad.is_hs, ad.hs_number, ad.is_episode, ad.episode_number "
            f"FROM active_downloads ad LEFT JOIN series s ON s.id = ad.series_id "
            f"WHERE ad.client = ? AND LOWER(ad.client_item_id) IN ({placeholders})",
            (client, *ids)
        )
        for row in cursor.fetchall():
            (client_item_id, tracking_id, series_id, volume_number, created_at, series_title, is_oneshot,
             is_integral, integral_number, is_hs, hs_number, is_episode, episode_number) = row
            result[client_item_id.lower()] = {
                'tracking_id': tracking_id, 'series_id': series_id,
                'volume_number': volume_number, 'series_title': series_title, 'created_at': created_at,
                'is_oneshot': bool(is_oneshot),
                'is_integral': bool(is_integral), 'integral_number': integral_number,
                'is_hs': bool(is_hs), 'hs_number': hs_number,
                'is_episode': bool(is_episode), 'episode_number': episode_number,
            }
        conn.close()
    except Exception as e:
        print(f"Erreur recherche téléchargements par id client ({client}): {e}")
    return result


def set_active_download_client_item_id(tracking_id: int, client_item_id: str) -> None:
    """Persiste l'id que le CLIENT lui-même utilise pour ce téléchargement (hash de
    torrent qBittorrent/rTorrent/Deluge, hash eD2K aMule) - voir
    find_active_downloads_by_client_item_ids, le seul lien qui permet ensuite de le
    retrouver sans ressemblance de nom. Appelée une seule fois, juste après la toute
    première correspondance de nom réussie pour ce téléchargement (activity_status).
    Normalisé en minuscules au stockage - voir find_active_downloads_by_client_item_ids."""
    try:
        db_path = current_app.config.get('DATABASE')
        if not db_path or not client_item_id:
            return
        conn = sqlite3.connect(db_path, timeout=30.0)
        conn.execute("UPDATE active_downloads SET client_item_id = ? WHERE id = ?", (client_item_id.lower(), tracking_id))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Erreur enregistrement id client pour le téléchargement suivi #{tracking_id}: {e}")


def find_active_download_match(filename: str) -> Optional[Dict]:
    """Cherche, parmi les active_downloads porteurs d'un series_id réel, celui dont le nom
    de fichier suivi désigne le même fichier que `filename` (voir _filenames_match) -
    retourne {'series_id':, 'volume_id':, 'volume_number':} ou None. Statut ignoré (pas
    seulement 'pending'): l'import automatique (scheduler.py) peut tourner AVANT que
    scan_import_directory n'ait eu l'occasion de nettoyer la ligne via
    clear_pending_downloads_by_filenames, une ligne déjà 'completed' reste donc un signal
    valable.

    "get the volume number and album name not from a matching but from when the file was
    added" - à utiliser en PRIORITÉ sur la re-dérivation par titre parsé
    (_match_series_for_auto_import côté routes.py): on connaissait déjà la bonne série (et
    le tome) avec certitude au moment du téléchargement, inutile de re-deviner depuis un nom
    de fichier de release potentiellement ambigu une fois le fichier arrivé sur disque.

    Pour un appel isolé (un seul fichier) - voir get_trackable_active_downloads/
    match_filename_against_trackable_downloads pour un appel en boucle sur plusieurs
    fichiers, qui ne doit charger cette liste qu'une seule fois."""
    return match_filename_against_trackable_downloads(filename, get_trackable_active_downloads())


def find_active_download_row_id(filename: str) -> Optional[int]:
    """Comme find_active_download_match, mais SANS filtrer sur series_id IS NOT NULL -
    une ligne active_downloads existe TOUJOURS pour un téléchargement lancé depuis cette
    app (mark_download_pending est appelé à chaque fois que log_manual_download l'est
    aussi, y compris avec series_id=None pour une recherche libre /search sans contexte
    série). Sert uniquement à retrouver l'id à corriger pour POST /api/activity/
    update-tracking quand aucune série n'était encore connue - find_active_download_match/
    _destination restent la référence pour tout le reste (série/tome déjà CONNUS avec
    certitude, qui eux exigent series_id IS NOT NULL)."""
    try:
        db_path = current_app.config.get('DATABASE')
        if not db_path:
            return None
        conn = sqlite3.connect(db_path, timeout=30.0)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, title FROM active_downloads
            ORDER BY created_at DESC
        ''')
        rows = cursor.fetchall()
        conn.close()
        for row_id, title in rows:
            if _filenames_match(title, filename):
                return row_id
        return None
    except Exception as e:
        print(f"Erreur résolution id de suivi téléchargement: {e}")
        return None


def update_active_download_tracking(
    download_id: int, series_id: int, volume_number,
    is_integral: bool = False, integral_number=None,
    is_hs: bool = False, hs_number=None,
    is_episode: bool = False, episode_number=None,
) -> bool:
    """Corrige la série/le tome suivis pour un téléchargement encore en cours (actif chez
    un client ou juste 'pending', voir mark_download_pending) - "je voudrais pouvoir
    editer les volumes et album dans l'import meme quand c'est pas terminé": jusqu'ici
    seul un fichier déjà arrivé sur disque (importFiles) pouvait être réassigné, un
    téléchargement encore en cours gardait la série/le tome (éventuellement erronés)
    connus au moment du clic "Ajouter" jusqu'à son arrivée. volume_id repart à NULL: pas
    de tome précis en base à rattacher avec certitude tant que le fichier n'existe pas
    (même raison que mark_download_pending) - seul volume_number, relu par
    apply_tracked_volume_and_gate une fois le fichier sur disque, est corrigé ici.

    "pourquoi je ne peux pas choisir intégrale, seulement le tome normal" - le correctif
    ne proposait jusqu'ici que volume_number (tome classique), aucun moyen de marquer ce
    téléchargement encore en cours comme une Intégrale/HS/Épisode - exactement le même
    jeu de champs que volumes.is_integral/integral_number/is_hs/hs_number/is_episode/
    episode_number (voir buildVolumeOverride côté import.js pour la correction équivalente
    d'un fichier déjà arrivé). volume_id reste NULL dans ces 3 cas (pas de numéro de tome
    classique à rattacher), même raisonnement que ci-dessus."""
    try:
        db_path = current_app.config.get('DATABASE')
        if not db_path:
            return False
        conn = sqlite3.connect(db_path, timeout=30.0)
        volume_id = None
        if series_id is not None and volume_number is not None and not (is_integral or is_hs or is_episode):
            volume_row = conn.execute(
                "SELECT id FROM volumes WHERE series_id = ? AND volume_number = ? "
                "ORDER BY CASE WHEN filepath IS NULL OR filepath = '' THEN 0 ELSE 1 END, id DESC LIMIT 1",
                (series_id, volume_number)
            ).fetchone()
            if volume_row:
                volume_id = volume_row[0]
        conn.execute(
            "UPDATE active_downloads SET series_id = ?, volume_id = ?, volume_number = ?, "
            "is_integral = ?, integral_number = ?, is_hs = ?, hs_number = ?, "
            "is_episode = ?, episode_number = ? WHERE id = ?",
            (series_id, volume_id, None if (is_integral or is_hs or is_episode) else volume_number,
             int(bool(is_integral)), integral_number, int(bool(is_hs)), hs_number,
             int(bool(is_episode)), episode_number, download_id)
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Erreur correction du suivi téléchargement #{download_id}: {e}")
        return False


def _set_pending_download_status(download_id: int, status: str) -> None:
    if not download_id:
        return
    try:
        db_path = current_app.config.get('DATABASE')
        if not db_path:
            return
        conn = sqlite3.connect(db_path, timeout=30.0)
        conn.execute(
            "UPDATE active_downloads SET status = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, download_id)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Erreur mise à jour statut téléchargement #{download_id}: {e}")


def mark_download_completed(download_id: Optional[int]) -> None:
    """Transition pending -> completed pour UNE ligne précise (voir mark_download_pending)
    - utilisée par Telegram, seule source dont le thread de téléchargement connaît la fin
    réelle de l'opération (les autres clients sont fire-and-forget côté app: le fichier
    "complété" est détecté indirectement par get_pending_downloads via l'import réussi).

    Aussi utilisée pour REVENIR en arrière depuis 'importing' (voir mark_download_importing)
    quand la tentative d'import de ce fichier précis échoue - le fichier reste sur place,
    'completed' ("waiting to import") est le bon état pour qu'un prochain passage le
    reprenne, plutôt que de le laisser bloqué en 'importing' pour de bon."""
    _set_pending_download_status(download_id, 'completed')


def mark_download_importing(download_id: Optional[int]) -> None:
    """"it should have in db the all cycle of importing: sent to client, downloading,
    waiting to import, importing and imported" - jusqu'ici active_downloads.status ne
    couvrait déjà que 'pending' (sent to client / downloading, indistincts faute de
    signal de progression fiable pour tous les clients hors Telegram - voir
    update_download_progress) et 'completed' (waiting to import: fichier confirmé sur
    disque, voir clear_pending_downloads_by_filenames/mark_download_completed), sans
    aucune trace du moment où CE fichier précis est activement en train d'être traité par
    _execute_import_batch (routes.py) - une conversion PDF peut prendre plusieurs minutes
    (CLAUDE.md) pendant lesquelles le suivi semblait juste "en attente" comme n'importe
    quel autre fichier pas encore repris. Posé juste avant le traitement réel de ce
    fichier (une fois le verrou d'import déjà tenu), remis à 'completed' si CET essai
    précis échoue (voir le bloc except de _execute_import_batch) pour ne jamais rester
    bloqué en 'importing' si le fichier doit être retenté plus tard."""
    _set_pending_download_status(download_id, 'importing')


def mark_download_imported(download_id: Optional[int]) -> None:
    """Dernière étape du cycle (voir mark_download_importing) - remplace l'ancienne
    suppression immédiate de la ligne (remove_pending_download) une fois un téléchargement
    suivi réellement importé (_maybe_complete_tracking_after_move, routes.py). Garde une
    trace durable et interrogeable de ce qui est arrivé à CE téléchargement précis (même
    philosophie "pas d'expiration automatique" que 'completed'/'failed', voir
    get_pending_downloads) plutôt que de le faire disparaître silencieusement de la base
    - c'est exactement ce qui manquait pour retracer un incident comme celui du 12/08/2026
    (Nordheim T14 Aaricia, deux imports concurrents pour le même fichier) sans devoir
    recouper plusieurs tables à la main."""
    _set_pending_download_status(download_id, 'imported')


def mark_download_skipped(download_id: Optional[int]) -> None:
    """Pendant de mark_download_imported pour un doublon reconnu et volontairement
    ignoré (voir _maybe_complete_tracking_after_move, routes.py, paramètre outcome=
    'skipped') - "add an explicit import result state... skipped, cancelled". Avant
    cette distinction, ce cas réutilisait 'imported' par commodité (le fichier n'a
    pourtant RIEN importé de nouveau, un exemplaire équivalent ou meilleur était déjà
    possédé) - même philosophie no-expiry que 'imported'/'completed'/'failed' (voir
    get_pending_downloads): garde une trace distincte plutôt que de disparaître ou de
    se faire passer pour un import réel."""
    _set_pending_download_status(download_id, 'skipped')


def mark_download_cancelled(download_id: Optional[int]) -> None:
    '''Mark an explicitly removed import row as cancelled without touching its source.'''
    _set_pending_download_status(download_id, 'cancelled')


def reconcile_stale_active_downloads() -> int:
    """"a reconciliation job should detect: a successful volume row whose tracking
    state is still completed/importing" - filet de sécurité DB-vers-DB (aucun accès
    disque) pour toute ligne active_downloads dont le contenu suivi est en réalité
    déjà possédé dans `volumes` (volumes.filepath populated - même critère que le NOT
    EXISTS de get_pending_downloads, ici inversé) mais dont le status n'a jamais
    avancé jusqu'à 'imported'. Causes réelles distinctes identifiées: (1) tout import
    réussi avant le 12/08/2026 (mark_download_imported n'existait pas encore - voir son
    commentaire), pur passif historique; (2) des relances auto-acquire créant plusieurs
    lignes active_downloads pour le même contenu, dont une seule finit par être
    effectivement rattachée à l'import réel - les autres restent orphelines même après
    le 12/08; (3) active_downloads.volume_number jamais renseigné à la création (constaté
    sur "Sorceline - T08...", "Le Grimoire d'Elfie - T05..." - le titre porte pourtant un
    numéro sans ambiguïté) - une comparaison SQL directe volumes.volume_number =
    active_downloads.volume_number ne peut alors jamais matcher (NULL = 8 est toujours
    faux), même une fois le tome réellement possédé.

    Le repli (3) est fait en Python, pas en SQL: "there is exactly ONE volume-number
    parser in this repo" (CLAUDE.md) - LibraryScanner.parse_filename(ad.title), jamais
    une seconde extraction ad hoc ici. Un titre qui échoue à parser ("Gil Jourdan 08 Les
    trois taches.cbz" - pas de séparateur "T"/tome reconnu par le parseur canonique)
    reste non réconcilié: pas de doublon possible ici (rien ne dit à tort "possédé" sur
    un simple échec de parsing), juste un candidat qui restera visible comme "en
    attente" jusqu'à résolution manuelle - cohérent avec le comportement déjà volontaire
    de get_pending_downloads pour ce genre de cas.

    "je ne veux pas qu'il y ait plus de pending que ceux qui sont vraiment en pending" -
    inclut désormais AUSSI 'pending' (pas seulement 'completed'/'importing'): un
    téléchargement dont le volume suivi est arrivé en bibliothèque par un AUTRE biais
    (import manuel séparé, remplacement, autre client) que celui suivi par CETTE ligne
    n'a jamais eu l'occasion de passer par 'completed' - il restait donc invisible du
    premier SELECT ci-dessus AUSSI silencieusement exclu, pour toujours, du résultat de
    get_pending_downloads (son NOT EXISTS) sans jamais être marqué résolu en base. Même
    critère de vérité (volumes.filepath réellement peuplé), pas une exclusion
    différente - transitionne la ligne vers son état terminal réel dès que ce fait est
    constaté, plutôt que la laisser en 'pending' indéfiniment tout en étant filtrée de
    l'affichage.

    Ne touche jamais les statuts déjà terminaux ('imported'/'failed'/'skipped').
    Retourne le nombre de lignes corrigées."""
    try:
        from blueprints.library.scanner import LibraryScanner

        db_path = current_app.config.get('DATABASE')
        if not db_path:
            return 0
        conn = sqlite3.connect(db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Correspondance exacte d'abord (volume_id ou volume_number déjà renseigné, ou
        # one-shot sans numéro) - identique à l'ancienne requête SQL pure, gère la
        # majorité des cas sans avoir à reparser le moindre titre.
        cursor.execute('''
            SELECT ad.id FROM active_downloads ad
            LEFT JOIN series s ON s.id = ad.series_id
            WHERE ad.status IN ('completed', 'importing', 'pending')
              AND EXISTS (
                SELECT 1 FROM volumes v
                WHERE (v.id = ad.volume_id
                       OR (v.series_id = ad.series_id
                           AND (v.volume_number = ad.volume_number
                                OR (ad.volume_number IS NULL AND s.is_oneshot = 1))))
                  AND v.filepath IS NOT NULL AND v.filepath != ''
              )
        ''')
        matched_ids = {row['id'] for row in cursor.fetchall()}

        # Repli (3): candidats restants sans volume_number stocké, sur une série non
        # one-shot (déjà couverte ci-dessus) - reparse le titre avant de retenter le
        # même croisement contre volumes.
        cursor.execute('''
            SELECT ad.id, ad.title, ad.series_id FROM active_downloads ad
            LEFT JOIN series s ON s.id = ad.series_id
            WHERE ad.status IN ('completed', 'importing', 'pending')
              AND ad.volume_number IS NULL AND ad.volume_id IS NULL
              AND (s.is_oneshot IS NULL OR s.is_oneshot = 0)
        ''')
        for row in cursor.fetchall():
            if row['id'] in matched_ids:
                continue
            parsed = LibraryScanner.parse_filename(row['title'] or '')
            # Tome classique d'abord, puis intégrale/HS/épisode (constaté en réel:
            # "Compagnons du crépuscule - INT" déjà possédée en intégrale, jamais
            # réconciliée tant que seul volume_number était comparé) - une série de
            # colonnes DIFFÉRENTE selon le type, jamais volume_number pour ces trois-là
            # (voir _classify_bedetheque_number/CLAUDE.md, même règle qu'à l'écriture).
            if parsed.get('volume') is not None:
                owned = cursor.execute(
                    "SELECT 1 FROM volumes WHERE series_id = ? AND volume_number = ? "
                    "AND filepath IS NOT NULL AND filepath != ''",
                    (row['series_id'], parsed['volume'])
                ).fetchone()
            elif parsed.get('is_integral'):
                owned = cursor.execute(
                    "SELECT 1 FROM volumes WHERE series_id = ? AND is_integral = 1 "
                    "AND integral_number IS ? AND filepath IS NOT NULL AND filepath != ''",
                    (row['series_id'], parsed.get('integral_number'))
                ).fetchone()
            elif parsed.get('is_hs'):
                owned = cursor.execute(
                    "SELECT 1 FROM volumes WHERE series_id = ? AND is_hs = 1 "
                    "AND hs_number IS ? AND filepath IS NOT NULL AND filepath != ''",
                    (row['series_id'], parsed.get('hs_number'))
                ).fetchone()
            elif parsed.get('is_episode'):
                owned = cursor.execute(
                    "SELECT 1 FROM volumes WHERE series_id = ? AND is_episode = 1 "
                    "AND episode_number IS ? AND filepath IS NOT NULL AND filepath != ''",
                    (row['series_id'], parsed.get('episode_number'))
                ).fetchone()
            else:
                owned = None
            if owned:
                matched_ids.add(row['id'])

        if not matched_ids:
            conn.close()
            return 0

        cursor.executemany(
            "UPDATE active_downloads SET status = 'imported', completed_at = CURRENT_TIMESTAMP WHERE id = ?",
            [(i,) for i in matched_ids]
        )
        conn.commit()
        conn.close()
        return len(matched_ids)
    except Exception as e:
        print(f"Erreur réconciliation des téléchargements déjà importés: {e}")
        return 0


def update_download_progress(download_id: Optional[int], bytes_downloaded: int, bytes_total: Optional[int]) -> None:
    """Met à jour la progression d'un téléchargement 'pending' ("import avec telegram
    essaie de monitorer le status de telechargement") - seule source aujourd'hui:
    download_channel_file_background (telegram_channels/scraper.py) via le
    progress_callback de Telethon, appelé très fréquemment (une fois par chunk reçu) et
    déjà throttlé côté appelant avant d'arriver ici (pas la peine de re-throttler en plus
    dans cette fonction). Best-effort silencieux: une écriture de progression ratée ne
    doit jamais faire échouer le téléchargement lui-même."""
    if download_id is None:
        return
    try:
        db_path = current_app.config.get('DATABASE')
        if not db_path:
            return
        conn = sqlite3.connect(db_path, timeout=30.0)
        # last_progress_at: rafraîchi à chaque VRAIE progression (voir throttling côté
        # appelant, ~1 fois par % reçu) - le battement de cœur qui distingue "encore actif"
        # de "probablement mort", voir get_pending_downloads/config.py.
        conn.execute(
            "UPDATE active_downloads SET bytes_downloaded = ?, bytes_total = ?, last_progress_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND status = 'pending'",
            (bytes_downloaded, bytes_total, download_id)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Erreur mise à jour progression téléchargement #{download_id}: {e}")


def refresh_download_heartbeat(download_id: Optional[int]) -> None:
    """Rafraîchit last_progress_at SANS toucher bytes_downloaded/bytes_total - contrairement
    à update_download_progress ci-dessus, appelée quand un téléchargement Telegram est
    encore réellement actif mais SANS nouvel octet reçu à annoncer (voir
    TELEGRAM_DOWNLOAD_FLOOD_RETRIES/_telegram_flood_wait_seconds, scraper.py): un
    FLOOD_PREMIUM_WAIT force à reprendre le fichier depuis zéro (l'archive temporaire
    partielle est supprimée), donc bytes_downloaded resterait figé sur son ancienne valeur
    pendant toute une séquence de plusieurs tentatives+attentes qui peut légitimement durer
    plus d'une minute (jusqu'à TELEGRAM_DOWNLOAD_MAX_WAIT_SECONDS par tentative). Sans ce
    battement de cœur, retry_stalled_telegram_downloads (dont le seuil "bloqué" est
    justement 1 minute sans progression) pouvait relancer une DEUXIÈME tentative
    concurrente pour LE MÊME message pendant que la première travaillait encore
    - constaté en réel: "Cartagena" tenté deux fois à 24s puis 12s d'écart, chaque
    paire échouant séparément avec son propre FLOOD_PREMIUM_WAIT_N distinct."""
    if download_id is None:
        return
    try:
        db_path = current_app.config.get('DATABASE')
        if not db_path:
            return
        conn = sqlite3.connect(db_path, timeout=30.0)
        conn.execute(
            "UPDATE active_downloads SET last_progress_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'pending'",
            (download_id,)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Erreur rafraîchissement téléchargement #{download_id}: {e}")


def clear_pending_downloads_by_tracking_ids(tracking_ids) -> None:
    """Marque comme terminé les suivis dont un fichier a été retrouvé par son
    ``destination.tracking_id``. Le nom local peut différer après extraction/renommage.

    Appelée en synchrone DANS /api/import/scan (voir scan_import_directory) - donc dans le
    chemin que l'utilisateur attend pour que /import s'affiche. "Import can not load" avec
    un gros lot de téléchargements en cours: un import automatique qui écrit pendant
    plusieurs minutes tient le verrou d'écriture SQLite tout du long, et ce best-effort
    (résultat jeté de toute façon en cas d'échec, voir le except ci-dessous) attendait
    jusqu'à 30s de ce même timeout avant d'abandonner - confirmé en pratique
    ("database is locked" loggé pendant un import automatique long). Le prochain scan
    (Actualiser, ou le sondage suivant) réessaiera de lui-même : un délai court fait
    échouer vite plutôt que de bloquer toute la page pour un résultat qui sera de toute
    façon ignoré."""
    ids = sorted({int(i) for i in (tracking_ids or []) if i is not None})
    if not ids:
        return
    try:
        db_path = current_app.config.get('DATABASE')
        if not db_path:
            return
        conn = sqlite3.connect(db_path, timeout=3.0)
        placeholders = ','.join('?' for _ in ids)
        conn.execute(
            f"UPDATE active_downloads SET status = 'completed', completed_at = CURRENT_TIMESTAMP "
            f"WHERE status = 'pending' AND id IN ({placeholders})", ids
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Erreur nettoyage des téléchargements suivis: {e}")


def clear_pending_downloads_by_filenames(filenames: List[str]) -> None:
    """"dans import naufrage du temps alors que cest fini" - une ligne "en attente"
    (active_downloads) ne se videait jusqu'ici que par correspondance floue avec un
    fichier déjà IMPORTÉ (import_history_files, voir get_pending_downloads) - mais un
    fichier qui vient tout juste d'apparaître sur le disque (scan_import_directory) n'est
    pas forcément déjà importé, il peut attendre une assignation manuelle un bon moment.
    Pendant toute cette fenêtre, il était compté DEUX FOIS sur /import: une fois comme
    fichier normal à importer, une fois comme téléchargement "en attente" fantôme.

    Le téléchargement d'un fichier est terminé dès qu'un fichier RÉEL existe pour le
    représenter, que ce fichier ait déjà été importé ou non - appelée avec la liste
    complète des noms de fichiers vus au scan (un seul aller-retour DB pour tous, plutôt
    qu'un par fichier: cette fonction tourne à chaque scan, déclenché uniquement par un
    clic explicite "Actualiser" côté /import - plus de sondage automatique)."""
    if not filenames:
        return
    try:
        db_path = current_app.config.get('DATABASE')
        if not db_path:
            return
        # timeout court: même raisonnement que clear_pending_downloads_by_tracking_ids
        # ci-dessus - appelée depuis _extract_archive_containers, dans le chemin
        # synchrone de /api/import/scan, pour un résultat best-effort de toute façon jeté
        # en cas d'échec.
        conn = sqlite3.connect(db_path, timeout=3.0)
        cursor = conn.cursor()
        cursor.execute("SELECT id, title FROM active_downloads WHERE status = 'pending'")
        rows = cursor.fetchall()
        if not rows:
            conn.close()
            return

        for download_id, title in rows:
            matched = any(_filenames_match(title, filename) for filename in filenames)
            if matched:
                cursor.execute(
                    "UPDATE active_downloads SET status = 'completed', completed_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (download_id,)
                )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Erreur nettoyage téléchargements en attente pour fichiers scannés: {e}")


def clear_pending_download_by_title(archive_filename: str) -> None:
    """Variante à un seul nom (voir clear_pending_downloads_by_filenames) - utilisée par
    _extract_archive_containers juste après extraction d'une archive, où il n'y a
    qu'un seul nom d'archive à comparer."""
    try:
        clear_pending_downloads_by_filenames([archive_filename])
    except Exception as e:
        print(f"Erreur nettoyage téléchargement en attente pour l'archive extraite '{archive_filename}': {e}")


def mark_download_failed(download_id: Optional[int]) -> None:
    """Voir mark_download_completed - pendant symétrique pour un échec."""
    _set_pending_download_status(download_id, 'failed')


# "il faut que les pending soient retires par leur activite naturelle, si le fichier
# est supprime ou en echec alors son etat ne doit plus etre en pending [...] je ne
# veux pas d'auto-expiration" - nombre de sondages consecutifs (voir activity_status,
# activity/routes.py) sans que le client ne rapporte plus l'item lie avant de conclure
# a un vrai abandon. Signal d'ACTIVITE reelle (le client a repondu et ne connait plus
# cet item), jamais un age: un client hors-ligne/en erreur ne compte jamais comme une
# absence (voir error is None dans activity_status) et un item qui reapparait remet le
# compteur a zero. >1 pour tolerer un sondage isole rate (redemarrage bref du client)
# sans declencher un faux abandon.
CLIENT_ABSENCE_FAILURE_THRESHOLD = 3


def reset_client_absence_streak(download_id: Optional[int]) -> None:
    """Remet a zero le compteur d'absences consecutives d'une ligne (voir
    CLIENT_ABSENCE_FAILURE_THRESHOLD) - appelee des que le client confirme a nouveau
    la presence de l'item lie, meme une seule fois apres une ou plusieurs absences."""
    if not download_id:
        return
    try:
        db_path = current_app.config.get('DATABASE')
        if not db_path:
            return
        conn = sqlite3.connect(db_path, timeout=10.0)
        conn.execute(
            "UPDATE active_downloads SET client_absence_streak = 0 WHERE id = ? AND client_absence_streak != 0",
            (download_id,)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Erreur reinitialisation compteur d'absence #{download_id}: {e}")


def increment_client_absence_streak(download_id: Optional[int]) -> int:
    """Incremente le compteur d'absences consecutives d'une ligne lors d'un sondage
    client REUSSI (le client a repondu) qui ne rapporte plus l'item lie a cette ligne -
    voir CLIENT_ABSENCE_FAILURE_THRESHOLD. Retourne la nouvelle valeur pour que
    l'appelant decide s'il doit transitionner la ligne vers 'failed'."""
    if not download_id:
        return 0
    try:
        db_path = current_app.config.get('DATABASE')
        if not db_path:
            return 0
        conn = sqlite3.connect(db_path, timeout=10.0)
        conn.execute(
            "UPDATE active_downloads SET client_absence_streak = COALESCE(client_absence_streak, 0) + 1 WHERE id = ?",
            (download_id,)
        )
        conn.commit()
        row = conn.execute("SELECT client_absence_streak FROM active_downloads WHERE id = ?", (download_id,)).fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception as e:
        print(f"Erreur incrementation compteur d'absence #{download_id}: {e}")
        return 0


def retry_stalled_telegram_downloads() -> None:
    """"yes relaunch and delete the stalled download from telegram" - un téléchargement
    Telegram qui ne progresse plus (thread mort - crash, rebuild du conteneur en plein
    lot, blocage réseau Telethon) restait sinon "pending" avec des données périmées
    indéfiniment (active_downloads n'expire plus jamais automatiquement, voir
    get_pending_downloads), sans qu'aucun mécanisme ne le relance jamais tout seul -
    confirmé en pratique le 2026-07-24: un lot de plusieurs tomes tué par un rebuild
    abandonnait pour de bon tout ce qui restait dans la file.

    Appelée par le même scheduler périodique que l'import automatique (voir
    library/scheduler.py, AUTO_IMPORT_FALLBACK_INTERVAL_MINUTES) plutôt qu'un planning
    dédié - c'est exactement le même genre de filet de sécurité "aMule/qBittorrent/etc.
    ne préviennent jamais l'appli", sauf que pour Telegram on a justement de quoi agir
    (channel/message_id, voir mark_download_pending) au lieu de seulement constater.

    Ne concerne que les lignes qui ONT channel/message_id enregistrés - une ligne plus
    ancienne (créée avant l'ajout de ces colonnes) n'a aucun moyen d'être relancée
    automatiquement, laissée telle quelle indéfiniment."""
    try:
        db_path = current_app.config.get('DATABASE')
        if not db_path:
            return

        current_process_id = current_app.config.get('PROCESS_INSTANCE_ID')

        conn = sqlite3.connect(db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Deux catégories bien distinctes, jamais confondues (voir commentaires sur
        # chaque requête) - "why telegram download is still stucked" / "3-retry budget ?
        # do something if this exceed the amount".

        # 1) Orpheline: posée par un démarrage PRÉCÉDENT du process (voir
        # process_instance_id, config.py) - son thread est mort avec certitude (rebuild
        # du conteneur en plein lot/en pleine file d'attente), quel que soit
        # bytes_downloaded/retry_count/l'ancienneté. Relancée à chaque tour de scheduler
        # tant qu'elle traîne, avec un compteur de tentatives repartant de zéro: les
        # tentatives "consommées" avant un rebuild ne reflètent aucun échec réel du
        # téléchargement lui-même, les compter contre son quota serait injuste.
        cursor.execute('''
            SELECT id, title, channel, message_id, series_id, volume_id, volume_number
            FROM active_downloads
            WHERE client = 'telegram' AND status = 'pending'
              AND channel IS NOT NULL AND message_id IS NOT NULL
              AND process_instance_id IS NOT NULL AND process_instance_id != ?
        ''', (current_process_id,))
        orphaned = [dict(row) for row in cursor.fetchall()]
        for row in orphaned:
            row['retry_count'] = 0
            row['_reason'] = 'orpheline (redémarrage du conteneur)'

        # 2) Réellement bloquée: a RÉELLEMENT commencé à transférer des données (mis à
        # jour par update_download_progress dès le premier octet reçu) puis s'est arrêtée
        # de progresser depuis plus d'1 minute - contrairement à une ligne encore
        # bytes_downloaded NULL, qui n'a simplement pas encore eu son tour dans la file
        # (les téléchargements Telegram sont sérialisés un par un, voir
        # _telegram_client_lock scraper.py) et n'a donc rien d'anormal. Avant ce filtre
        # bytes_downloaded IS NOT NULL, les deux cas étaient indiscernables (même
        # last_progress_at figé à created_at) - un lot de plusieurs tomes épuisait alors
        # ses 3 tentatives sur des tomes encore simplement en attente, jamais réellement
        # démarrés. Respecte le quota de tentatives: ici, contrairement au cas orphelin
        # ci-dessus, un blocage répété reflète potentiellement un vrai problème
        # (message supprimé, blocage réseau Telethon récurrent...).
        cursor.execute(f'''
            SELECT id, title, channel, message_id, series_id, volume_id, volume_number, retry_count
            FROM active_downloads
            WHERE client = 'telegram' AND status = 'pending'
              AND channel IS NOT NULL AND message_id IS NOT NULL
              AND bytes_downloaded IS NOT NULL
              AND COALESCE(retry_count, 0) < {TELEGRAM_MAX_RETRY_ATTEMPTS}
              AND COALESCE(last_progress_at, created_at) < datetime('now', '-{TELEGRAM_RETRY_AFTER_STALLED_MINUTES} minutes')
        ''')
        stalled = [dict(row) for row in cursor.fetchall()]
        for row in stalled:
            row['_reason'] = 'bloquée'
        conn.close()

        to_retry = orphaned + stalled
        if not to_retry:
            return

        from blueprints.telegram_channels.routes import _require_connected_config
        from blueprints.telegram_channels.scraper import download_channel_file_background

        config = _require_connected_config()
        if not config:
            print(f"⚠️ {len(to_retry)} téléchargement(s) Telegram orphelin(s)/bloqué(s), mais impossible de relancer: non connecté à Telegram")
            return

        target_dir = current_app.config.get('TELEGRAM_IMPORT_DIRECTORY')
        if not target_dir:
            return

        app = current_app._get_current_object()
        conn = sqlite3.connect(db_path, timeout=30.0)
        for row in to_retry:
            print(f"↻ Téléchargement Telegram {row['_reason']}, relance (tentative {row['retry_count'] + 1}/{TELEGRAM_MAX_RETRY_ATTEMPTS}): {row['title']}")
            # Supprimée AVANT la relance (pas après): download_channel_file_background va
            # créer sa propre nouvelle ligne 'pending' via mark_download_pending - la
            # laisser vivante en parallèle donnerait deux lignes pour le même fichier le
            # temps du nouveau téléchargement.
            conn.execute('DELETE FROM active_downloads WHERE id = ?', (row['id'],))
            conn.execute(
                "UPDATE missing_volume_downloads SET success = 0, message = ? "
                "WHERE tracking_id = ? AND success IS NULL",
                (f"Bloqué ({row['_reason']}), relancé automatiquement (tentative {row['retry_count'] + 1}/{TELEGRAM_MAX_RETRY_ATTEMPTS})", row['id'])
            )
            conn.commit()
            try:
                # retry_count transmis directement (pas de relecture/mise à jour après
                # coup): download_channel_file_background tourne dans son propre thread,
                # deviner quand sa ligne mark_download_pending a fini d'être écrite pour
                # la corriger ensuite serait une course inutile.
                download_channel_file_background(
                    config['api_id'], config['api_hash_decrypted'], config['session_decrypted'],
                    row['channel'], row['message_id'], target_dir, app=app,
                    pending_title=row['title'],
                    series_id=row['series_id'], volume_id=row['volume_id'], volume_number=row['volume_number'],
                    retry_count=row['retry_count'] + 1
                )
            except Exception as e:
                print(f"Erreur relance téléchargement Telegram ({row['title']}): {e}")
        conn.close()
    except Exception as e:
        print(f"Erreur lors de la relance des téléchargements Telegram bloqués: {e}")


def remove_pending_download(download_id: int) -> bool:
    """Supprime définitivement une ligne "en attente" (voir get_pending_downloads) -
    "pourquoi je peux pas supprimer" : une ligne pending peut rester bloquée indéfiniment
    (téléchargement mort côté client sans jamais atteindre 100%, ou le nom ne matchera
    jamais exactement le fichier final) sans qu'aucune action ne permette de la faire
    disparaître - active_downloads n'expire plus jamais automatiquement, c'est le seul
    moyen de s'en débarrasser. Ne touche QUE cette ligne de bookkeeping - n'annule pas le
    téléchargement lui-même chez le client
    (pas d'identifiant de torrent/hash fiable à ce stade, voir docstring de
    mark_download_pending): si le téléchargement est toujours actif, il continuera, mais ne
    réapparaîtra plus comme "en attente" fantôme une fois retiré d'ici."""
    try:
        db_path = current_app.config.get('DATABASE')
        if not db_path:
            return False
        conn = sqlite3.connect(db_path, timeout=30.0)
        conn.execute("DELETE FROM active_downloads WHERE id = ?", (download_id,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Erreur suppression téléchargement en attente #{download_id}: {e}")
        return False


def remove_completed_pack_if_owned(download_id: int) -> bool:
    """Supprime le suivi d'un pack terminé lorsque tous ses tomes attendus sont
    déjà présents dans la bibliothèque.

    Le dossier source peut disparaître avant le dernier passage de l'import (client
    torrent qui nettoie son dossier, ou déplacement manuel). Dans ce cas le nettoyage
    basé uniquement sur l'existence du dossier ne peut plus retrouver le pack. La base
    des volumes est alors la preuve durable : seuls les volumes avec un filepath réel
    et un numéro distinct sont comptés, jamais les placeholders Bédéthèque.
    """
    try:
        db_path = current_app.config.get('DATABASE')
        if not db_path:
            return False
        conn = sqlite3.connect(db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT series_id, status, is_pack, expected_volume_count "
            "FROM active_downloads WHERE id = ?", (download_id,)
        ).fetchone()
        if not row or row['status'] != 'completed' or not row['is_pack']:
            conn.close()
            return False
        expected = row['expected_volume_count']
        if not expected or row['series_id'] is None:
            conn.close()
            return False
        owned = conn.execute(
            "SELECT COUNT(DISTINCT volume_number) FROM volumes "
            "WHERE series_id = ? AND volume_number IS NOT NULL "
            "AND filepath IS NOT NULL AND filepath != ''",
            (row['series_id'],)
        ).fetchone()[0]
        if owned < int(expected):
            conn.close()
            return False
        conn.execute("DELETE FROM active_downloads WHERE id = ?", (download_id,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Erreur nettoyage pack terminé #{download_id}: {e}")
        return False


# Même seuil que _IN_PROGRESS_STALE_MINUTES (import_history.py) - la réclamation
# (import_in_progress) et ce statut sont censés évoluer ensemble, mais rien ne garantit
# que _execute_import_batch atteigne son bloc except pour revenir à 'completed' si le
# process meurt en plein milieu (kill, OOM, rebuild pendant une conversion PDF de
# plusieurs minutes) - sans ce filet, la ligne restait figée sur 'importing' pour
# toujours une fois la réclamation elle-même expirée, "la base est la vérité" devenant
# faux pour cette ligne précise (elle prétend qu'un import est en cours alors que plus
# rien ne le traite).
_STALE_IMPORTING_MINUTES = 30


def _revert_stale_importing_downloads() -> None:
    """Filet de sécurité pour un crash en plein 'importing' - voir _STALE_IMPORTING_MINUTES.
    Best-effort silencieux, appelée à chaque lecture de get_pending_downloads (donc à
    chaque chargement de /import et chaque sondage /api/activity/status) pour que ce cas
    se corrige de lui-même sans dépendre d'un job périodique dédié."""
    try:
        db_path = current_app.config.get('DATABASE')
        if not db_path:
            return
        conn = sqlite3.connect(db_path, timeout=30.0)
        conn.execute(f'''
            UPDATE active_downloads SET status = 'completed'
            WHERE status = 'importing'
              AND completed_at < datetime('now', '-{_STALE_IMPORTING_MINUTES} minutes')
        ''')
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Erreur réconciliation des téléchargements bloqués en 'importing': {e}")


def _reconcile_stuck_completed_download(download_status, is_pack, series_id, series_path, title, db_path):
    """"why prêt si le fichier n'existe pas... can you do active_downloads.status=
    'completed' and a live disk check" - un import interrompu par un redémarrage de
    l'app (voir import_history.py, "Interrompu par un redémarrage...") peut avoir déjà
    déplacé/converti le fichier avec succès AVANT d'être coupé, sans jamais mettre à jour
    volumes.filepath ni relancer clear_pending_downloads_*: la ligne reste 'completed'
    pour toujours (voir docstring de get_pending_downloads, aucune expiration
    automatique) et affiche "✓ Prêt" indéfiniment alors qu'aucun fichier n'est réellement
    lié - constaté en réel sur "Orbital" T8. Vérif peu coûteuse (un seul os.listdir du
    dossier de LA série, pas un scan complet): si un fichier du dossier correspond déjà
    au titre suivi (_filenames_match, même comparaison que _title_already_imported), un
    rescan de cette seule série (scan_single_series, rapide - réutilise page_count/
    ComicInfo déjà en cache pour tout fichier inchangé, ne retraite que le nouveau) répare
    l'incohérence immédiatement plutôt que d'attendre qu'un humain vienne demander
    pourquoi.

    "1 mais pourtant import il y a rien" - bug réel: cette vérif suppose UN fichier par
    téléchargement (un match suffit -> tout est résolu). Pour un PACK (plusieurs tomes/
    fichiers sous UN SEUL active_downloads), UN SEUL tome déjà importé et présent dans le
    dossier série (ex: "Joe Bar Team - #HS01 - ...cbz") fait fuzzy-matcher le titre du
    pack et fait disparaître TOUTE la ligne 'pending' - alors qu'il restait un autre
    fichier du même pack ("HS1a - ...pdf") jamais importé. Une fois la ligne 'pending'
    disparue, _pendingPackGroups (import.js) n'a plus rien sous quoi regrouper ce fichier
    restant, qui reste renvoyé par /api/import/scan (son pack_download_id pointe toujours
    vers ce téléchargement) mais n'apparaît alors plus nulle part sur /import - le badge
    de la sidebar (qui compte, lui, directement ce que /api/import/scan renvoie) et la
    page affichée divergent. Exclu des packs: leur résolution fiable passe déjà par
    clear_pending_downloads_by_tracking_ids (scan_import_directory, par fichier réellement
    importé), pas par cette heuristique "un seul fichier suffit".

    Retourne True si la ligne a été résolue (le fichier est désormais lié en base, cette
    ligne n'a plus lieu d'apparaître comme "en attente"/"Prêt") - l'appelant doit alors
    `continue` sans plus rien faire pour cette ligne."""
    if not (download_status == 'completed' and not is_pack and series_id is not None
            and series_path and os.path.isdir(series_path)):
        return False
    try:
        disk_match = any(
            os.path.isfile(os.path.join(series_path, name)) and _filenames_match(title, name)
            for name in os.listdir(series_path)
        )
    except OSError:
        disk_match = False
    if not disk_match:
        return False
    try:
        from blueprints.library.scanner import LibraryScanner
        LibraryScanner(db_path).scan_single_series(series_id)
    except Exception as e:
        print(f"✗ Erreur auto-résolution téléchargement bloqué (série #{series_id}): {e}")
        return False
    return True


def get_pending_downloads() -> List[Dict]:
    """Téléchargements 'en attente' à afficher sur /import en plus des fichiers déjà sur
    disque et des téléchargements actifs sondés en direct chez chaque client (voir
    /api/activity/status) - couvre le délai entre "l'utilisateur a cliqué Ajouter/
    Télécharger" et "le fichier existe quelque part de consultable" (scan de répertoire ou
    sondage client), quelle que soit la source.

    Exclut les lignes dont le titre correspond déjà à un import réussi récent (voir
    _title_already_imported) - le fichier est arrivé, la ligne "en attente" n'a plus lieu
    d'être.

    "no the database is the truth. don't bring file from the folder into the equation.
    this will mess up things" - une ligne active_downloads n'expire plus jamais
    automatiquement (l'ancienne purge PENDING_DOWNLOAD_STALE_HOURS de 6h supprimait aussi
    une ligne 'completed'/'pack' encore utile, ex. un pack groupant plusieurs fichiers dont
    l'import prend plus de 6h - la base est la référence, pas une fenêtre de temps
    arbitraire). Une ligne 'pending' réellement abandonnée (téléchargement mort côté
    client, titre qui ne matchera jamais le fichier final) reste donc affichée
    indéfiniment jusqu'à suppression manuelle (voir remove_pending_download) - c'est
    volontaire, pas un oubli.

    Ne supprime PAS une ligne 'completed'/'failed' simplement parce que son statut n'est
    plus 'pending' (bug réel constaté: "why is it gone" - un tome tout juste passé
    'completed' par clear_pending_downloads_by_filenames disparaissait ENTIÈREMENT de la
    base à l'appel suivant de cette fonction, avant même que scan_import_directory n'ait pu
    la relire via find_active_download_match/find_active_download_destination pour
    rattacher le fichier fraîchement arrivé à sa série/son tome - deux sondages périodiques
    indépendants (/api/activity/status et /api/import/scan) se marchaient dessus selon
    l'ordre d'arrivée).

    "i don't understand as long it is not imported [...] no need to do a scan" - le filtre
    ci-dessous inclut donc AUSSI 'completed' (pas seulement 'pending'): un téléchargement
    fini côté client (fichier réellement sur disque, voir clear_pending_downloads_by_filenames/
    mark_download_completed) mais pas encore importé reste visible ici dès le prochain appel
    de /api/activity/status (chargement de page ou clic explicite "Actualiser", plus de
    sondage automatique périodique côté frontend - voir import.js) - jamais besoin d'un
    scan de répertoire explicite (/api/import/scan) pour le faire réapparaître. La ligne ne
    disparaît que sur un fait réel: import confirmé (voir _title_already_imported plus
    bas) ou suppression manuelle (remove_pending_download) - jamais sur la seule
    présence d'un fichier dans le répertoire d'import, ni sur un âge quelconque.
    """
    from blueprints.library.scanner import LibraryScanner

    _revert_stale_importing_downloads()
    # "a reconciliation job should detect a successful volume row whose tracking state
    # is still completed/importing" - même cadence que le self-heal ci-dessus (chaque
    # appel de cette fonction, pas un job planifié séparé): couvre à la fois le passif
    # historique (imports réussis avant le 12/08/2026, voir reconcile_stale_active_
    # downloads) et les orphelins créés en continu par les relances auto-acquire.
    reconcile_stale_active_downloads()

    try:
        db_path = current_app.config.get('DATABASE')
        if not db_path:
            return []

        conn = sqlite3.connect(db_path, timeout=30.0)
        # sqlite3.Row + accès par nom de colonne plutôt qu'un tuple-unpack positionnel:
        # ce SELECT et la boucle qui le consomme sont à une centaine de lignes d'écart
        # (voir plus bas) - une colonne insérée ailleurs qu'en toute fin de la liste
        # décalerait silencieusement tous les champs suivants, sans la moindre erreur,
        # juste un series_id/volume_number/is_pack faux affiché sur /import (même classe
        # de bug déjà rencontrée ailleurs dans ce repo). ad.title/s.title ont le MÊME nom
        # de colonne non aliasé - alias explicite requis pour ne serait-ce que pouvoir
        # les distinguer par nom (voir get_operation_details, import_history.py, qui a le
        # même piège documenté avec f.*).
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # LEFT JOIN series: titre à jour plutôt que figé (une série peut être renommée/
        # rematchée après le lancement du téléchargement) - même raisonnement que
        # get_operation_details côté import_history.py.
        cursor.execute(
            "SELECT ad.id, ad.title, ad.client, ad.created_at, ad.series_id, ad.volume_id, "
            "ad.volume_number, s.title AS series_title, s.path AS series_path, s.is_oneshot, "
            "ad.bytes_downloaded, ad.bytes_total, "
            "ad.last_progress_at, ad.retry_count, ad.is_pack, ad.expected_volume_count, "
            "ad.client_item_id, ad.status, "
            "ad.is_integral, ad.integral_number, ad.is_hs, ad.hs_number, ad.is_episode, ad.episode_number "
            "FROM active_downloads ad "
            "LEFT JOIN series s ON s.id = ad.series_id "
            "WHERE ad.status IN ('pending', 'completed', 'importing') "
            "AND NOT (EXISTS ("
            "SELECT 1 FROM volumes v WHERE (v.id = ad.volume_id "
            "OR (v.series_id = ad.series_id AND (v.volume_number = ad.volume_number "
            "OR (ad.volume_number IS NULL AND s.is_oneshot = 1)))) "
            "AND v.filepath IS NOT NULL AND v.filepath != ''"
            ")) ORDER BY ad.created_at DESC"
        )
        rows = cursor.fetchall()

        if not rows:
            conn.close()
            return []

        cursor.execute("SELECT series_id, volume_number FROM volumes WHERE filepath IS NOT NULL AND filepath != ''")
        owned_volumes = {(r['series_id'], r['volume_number']) for r in cursor.fetchall()}
        cursor.execute(
            "SELECT DISTINCT v.series_id FROM volumes v JOIN series s ON s.id = v.series_id "
            "WHERE s.is_oneshot = 1 AND v.volume_number IS NULL AND v.filepath IS NOT NULL AND v.filepath != ''"
        )
        owned_oneshot_series_ids = {r['series_id'] for r in cursor.fetchall()}

        conn.close()

        pending = []
        seen_pending_keys = set()
        for row in rows:
            download_id = row['id']
            title = row['title']
            client = row['client']
            created_at = row['created_at']
            series_id = row['series_id']
            volume_id = row['volume_id']
            volume_number = row['volume_number']
            series_title = row['series_title']
            series_path = row['series_path']
            is_oneshot = row['is_oneshot']
            bytes_downloaded = row['bytes_downloaded']
            bytes_total = row['bytes_total']
            retry_count = row['retry_count']
            is_pack = row['is_pack']
            expected_volume_count = row['expected_volume_count']
            client_item_id = row['client_item_id']
            download_status = row['status']
            stored_is_integral = bool(row['is_integral'])
            stored_is_hs = bool(row['is_hs'])
            stored_is_episode = bool(row['is_episode'])

            # Un pack terminé dont tous les tomes sont déjà dans la bibliothèque ne
            # doit plus être présenté comme « Prêt », même si son dossier torrent a été
            # supprimé avant le dernier passage de nettoyage.
            if download_status == 'completed' and is_pack and remove_completed_pack_if_owned(download_id):
                continue
            # Les anciennes versions pouvaient déjà avoir créé plusieurs lignes pour le
            # même lien. Les lignes sont triées du plus récent au plus ancien: garder la
            # première évite les doublons dans Import sans supprimer l'historique en base.
            dedupe_key = (
                client,
                ('id', str(client_item_id).lower()) if client_item_id else
                ('name', _normalize_filename(title), series_id, volume_number)
            )
            if dedupe_key in seen_pending_keys:
                continue
            seen_pending_keys.add(dedupe_key)

            # Auto-résolution d'une ligne 'completed' orpheline (import interrompu par un
            # redémarrage avant la mise à jour de volumes.filepath) - voir
            # _reconcile_stuck_completed_download pour le détail complet (incident réel
            # "Orbital" T8) et le garde-fou pack ("1 mais pourtant import il y a rien").
            if _reconcile_stuck_completed_download(download_status, is_pack, series_id, series_path, title, db_path):
                # Le fichier est maintenant lié (volumes.filepath) - cette ligne n'a plus
                # lieu d'apparaître comme "en attente"/"Prêt".
                continue
            try:
                parsed = LibraryScanner.parse_filename(title)
                parsed_type = {
                    'is_integral': parsed.get('is_integral'), 'integral_number': parsed.get('integral_number'),
                    'is_hs': parsed.get('is_hs'), 'hs_number': parsed.get('hs_number'),
                    'is_episode': parsed.get('is_episode'), 'episode_number': parsed.get('episode_number'),
                }
                resolved_volume_number = volume_number if volume_number is not None else parsed.get('volume')
            except Exception:
                parsed_type = {}
                resolved_volume_number = volume_number
            if stored_is_integral or stored_is_hs or stored_is_episode:
                parsed_type = {
                    'is_integral': stored_is_integral, 'integral_number': row['integral_number'],
                    'is_hs': stored_is_hs, 'hs_number': row['hs_number'],
                    'is_episode': stored_is_episode, 'episode_number': row['episode_number'],
                }
                resolved_volume_number = None
            if series_id is not None and not is_pack and (
                (series_id, resolved_volume_number) in owned_volumes
                or (resolved_volume_number is None and series_id in owned_oneshot_series_ids)
            ):
                continue
            exhausted = (
                client == 'telegram' and bytes_downloaded is not None
                and (retry_count or 0) >= TELEGRAM_MAX_RETRY_ATTEMPTS
            )
            # "1. Separate download complete from ready to import... completed
            # currently appears as Prêt even when not actually importable
            # automatically" - un pack/one-shot/intégrale/HS/épisode n'a par définition
            # jamais besoin d'un numéro de tome simple pour être importable (voir
            # is_pack/is_oneshot/parsed_type ci-dessus, mêmes exclusions que
            # resolved_volume_number déjà appliquées plus haut) - needs_volume_correction
            # n'est vrai que pour le cas résiduel réel: un titre non ambigu QUANT à sa
            # série mais dont le numéro n'a pu être ni stocké ni reparsé (ex: "Gil
            # Jourdan 08 Les trois taches.cbz" - pas de séparateur "T"/tome reconnu par
            # LibraryScanner.parse_filename, le seul parseur de ce dépôt). Le frontend
            # s'en sert pour ne plus afficher "✓ Prêt" à tort sur ce genre de ligne -
            # openTrackingEditModal (import.js, déjà existant) reste le point de
            # correction manuelle.
            needs_volume_correction = (
                not is_pack and not is_oneshot and resolved_volume_number is None
                and not parsed_type.get('is_integral') and not parsed_type.get('is_hs')
                and not parsed_type.get('is_episode')
            )
            needs_series_correction = series_id is None
            pending.append({
                'id': download_id, 'title': title, 'client': client, 'created_at': created_at,
                'series_id': series_id, 'volume_id': volume_id, 'volume_number': resolved_volume_number,
                'series_title': series_title,
                'bytes_downloaded': bytes_downloaded, 'bytes_total': bytes_total,
                'exhausted': exhausted,
                'is_pack': bool(is_pack), 'is_oneshot': bool(is_oneshot), 'expected_volume_count': expected_volume_count,
                'client_item_id': client_item_id,
                'status': download_status,
                'needs_volume_correction': needs_volume_correction,
                'needs_series_correction': needs_series_correction,
                **parsed_type,
            })
        return pending
    except Exception as e:
        print(f"Erreur lecture téléchargements en attente: {e}")
        return []


class MissingVolumeDownloader:
    """Envoie les téléchargements aux clients configurés"""
    
    def __init__(self):
        self.clients = {
            'qbittorrent': self._download_to_qbittorrent,
            'amule': self._download_to_amule,
            'rtorrent': self._download_to_rtorrent,
            'deluge': self._download_to_deluge
        }
    
    def send_torrent_download(self, torrent_link: str, title: str, volume_num: int,
                             client: str = None, category: str = None,
                             series_id: Optional[int] = None,
                             filename: Optional[str] = None,
                             source: Optional[str] = None,
                             source_link: Optional[str] = None) -> Tuple[bool, str]:
        ""
        if not torrent_link:
            return False, "Lien vide"

        # Auto-détection du client si non spécifié
        if not client:
            # Déterminer le client selon le type de lien
            if torrent_link.startswith('ed2k://'):
                client = 'amule'
            else:
                # magnet:, http://, https://, etc. → qBittorrent
                client = 'qbittorrent'

        if client not in self.clients:
            return False, f"Client inconnu: {client}"

        try:
            return self.clients[client](torrent_link, title, volume_num, category, series_id, filename,
                                         source, source_link)
        except Exception as e:
            return False, f"Erreur {client}: {str(e)}"
    
    def _get_default_client(self) -> str:
        """Détermine le client par défaut (le premier actif)"""
        try:
            config_file = current_app.config.get('QBITTORRENT_CONFIG_FILE')
            if config_file:
                with open(config_file, 'r') as f:
                    config = json.load(f)
                    if config.get('enabled'):
                        return 'qbittorrent'
        except:
            pass
        
        try:
            config = current_app.config.get('EMULE_CONFIG', {})
            if config.get('enabled'):
                return 'amule'
        except:
            pass
        
        return 'qbittorrent'  # Par défaut
    
    def _post_to_client(self, endpoint: str, payload: dict, client_key: str, client_label: str,
                         title: str, volume_num: Optional[int], source: Optional[str],
                         source_link: Optional[str]) -> Tuple[bool, str]:
        ""
        display_label = payload.get('title') or (f"{title} - Tome {volume_num}" if volume_num is not None else title)
        print(f"[{client_label} Download] Envoi à {client_label}: {display_label}", file=sys.stderr)
        try:
            path = urlparse(endpoint).path
            response = current_app.test_client().post(path, json=payload)
            print(f"[{client_label} Download] Réponse HTTP: {response.status_code}", file=sys.stderr)

            if response.status_code == 200:
                result = response.get_json()
                if result.get('success'):
                    msg = f"{display_label} envoyé à {client_label}"
                    print(f"[{client_label} Download] ✅ Succès", file=sys.stderr)
                    return True, msg
                else:
                    error_msg = result.get('error', 'Erreur inconnue')
                    msg = f"Erreur {client_label}: {error_msg}"
                    print(f"[{client_label} Download] ❌ {error_msg}", file=sys.stderr)
                    return False, msg
            else:
                msg = f"Erreur {client_label}: HTTP {response.status_code}"
                print(f"[{client_label} Download] ❌ HTTP {response.status_code}: {response.get_data(as_text=True)[:200]}", file=sys.stderr)
                return False, msg

        except Exception as e:
            msg = f"Erreur connexion {client_label}: {str(e)}"
            print(f"[{client_label} Download] ❌ Exception: {str(e)}", file=sys.stderr)
            return False, msg

    def _download_to_qbittorrent(self, torrent_link: str, title: str,
                                volume_num: int, category: str = None,
                                series_id: Optional[int] = None,
                                filename: Optional[str] = None,
                                source: Optional[str] = None,
                                source_link: Optional[str] = None) -> Tuple[bool, str]:
        """Envoie à qBittorrent en utilisant l'endpoint /api/qbittorrent/add"""
        # Charger la catégorie par défaut si non fournie
        if not category:
            from ..qbittorrent.routes import load_qbittorrent_config
            config = load_qbittorrent_config()
            category = config.get('default_category', '')

        # filename (nom RÉEL du résultat, voir send_torrent_download) en priorité -
        # repli sur "Titre - Tome N" seulement si aucun nom réel n'est connu, pour ne
        # jamais laisser retomber sur torrent_url[:80] (voir son propre `title =
        # data.get('title') or torrent_url[:80]`), qui polluait Historique d'une ligne
        # en URL brute.
        payload = {
            'torrent_url': torrent_link,
            'title': filename or (f"{title} - Tome {volume_num}" if volume_num is not None else title),
            'series_id': series_id,
            'volume_number': volume_num,
            'source': source,
            'source_link': source_link,
        }
        if category:
            payload['category'] = category

        return self._post_to_client(
            'http://127.0.0.1:5000/api/qbittorrent/add', payload,
            'qbittorrent', 'qBittorrent', title, volume_num, source, source_link
        )

    def _download_to_amule(self, torrent_link: str, title: str,
                          volume_num: int, category: str = None,
                          series_id: Optional[int] = None,
                          filename: Optional[str] = None,
                          source: Optional[str] = None,
                          source_link: Optional[str] = None) -> Tuple[bool, str]:
        """Envoie à aMule/eMule en utilisant l'endpoint /api/emule/add"""
        # /api/emule/add préfère de toute façon le nom de fichier réel extrait du lien
        # ed2k lui-même (voir _title_from_ed2k_link côté emule/routes.py - garantie
        # d'être identique à ce qu'aMule rapportera ensuite via `amulecmd show dl`) -
        # `title` ici ne sert que de repli si cette extraction échouait.
        payload = {
            'link': torrent_link,
            'title': filename or (f"{title} - Tome {volume_num}" if volume_num is not None else title),
            'series_id': series_id,
            'volume_number': volume_num,
            'source': source,
            'source_link': source_link,
        }
        # Si on a une catégorie, on peut la passer (bien que aMule ne l'utilise pas)
        if category:
            payload['category'] = category

        return self._post_to_client(
            'http://127.0.0.1:5000/api/emule/add', payload,
            'amule', 'aMule', title, volume_num, source, source_link
        )

    def _download_to_rtorrent(self, torrent_link: str, title: str,
                             volume_num: int, category: str = None,
                             series_id: Optional[int] = None,
                             filename: Optional[str] = None,
                             source: Optional[str] = None,
                             source_link: Optional[str] = None) -> Tuple[bool, str]:
        """Envoie à rTorrent en utilisant l'endpoint /api/rtorrent/add (pas de catégorie -
        rTorrent n'a pas cette notion côté XML-RPC load.start)"""
        payload = {
            'torrent_url': torrent_link,
            'title': filename or (f"{title} - Tome {volume_num}" if volume_num is not None else title),
            'series_id': series_id,
            'volume_number': volume_num,
            'source': source,
            'source_link': source_link,
        }
        return self._post_to_client(
            'http://127.0.0.1:5000/api/rtorrent/add', payload,
            'rtorrent', 'rTorrent', title, volume_num, source, source_link
        )

    def _download_to_deluge(self, torrent_link: str, title: str,
                           volume_num: int, category: str = None,
                           series_id: Optional[int] = None,
                           filename: Optional[str] = None,
                           source: Optional[str] = None,
                           source_link: Optional[str] = None) -> Tuple[bool, str]:
        """Envoie à Deluge en utilisant l'endpoint /api/deluge/add (pas de catégorie -
        pas câblé côté core.add_torrent_url pour l'instant)"""
        payload = {
            'torrent_url': torrent_link,
            'title': filename or (f"{title} - Tome {volume_num}" if volume_num is not None else title),
            'series_id': series_id,
            'volume_number': volume_num,
            'source': source,
            'source_link': source_link,
        }
        return self._post_to_client(
            'http://127.0.0.1:5000/api/deluge/add', payload,
            'deluge', 'Deluge', title, volume_num, source, source_link
        )

    def get_download_history(self, limit: int = 50, series_id: Optional[int] = None) -> List[Dict]:
        """Récupère l'historique des téléchargements

        Args:
            limit: Nombre maximum de records à retourner
            series_id: "ajoute une fiche histoire par série" - filtre optionnel, pour la
                fiche historique d'une série précise plutôt que la page /history générale.

        Returns:
            Liste historique
        """
        try:
            db_path = current_app.config.get('DATABASE')
            if not db_path:
                return []

            conn = sqlite3.connect(db_path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute(f'''
                SELECT md.id, md.title, md.volume_number, md.client, md.success, md.message, md.created_at, md.source, md.source_link, ad.series_id, s.title AS series_title
                FROM missing_volume_downloads md LEFT JOIN active_downloads ad ON ad.id = md.tracking_id LEFT JOIN series s ON s.id = ad.series_id
                {"WHERE ad.series_id = ?" if series_id is not None else ""}
                ORDER BY md.created_at DESC
                LIMIT ?
            ''', ((series_id, limit) if series_id is not None else (limit,)))

            history = []
            for row in cursor.fetchall():
                history.append({
                    'id': row[0],
                    'title': row[1],
                    'volume_number': row[2],
                    'client': row[3],
                    # NULL signifie que le téléchargement vient d'être lancé :
                    # préserver cet état pour que l'UI affiche "En cours", pas "Échec".
                    'success': None if row[4] is None else bool(row[4]),
                    'message': row[5],
                    'created_at': row[6],
                    'source': row[7],
                    'source_link': row[8], 'series_id': row[9], 'series_title': row[10]
                })
            
            conn.close()
            return history
        except Exception as e:
            print(f"Erreur récupération historique: {e}")
            return []
