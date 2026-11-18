"""
« toutes les actions doivent être dans l'historique » : filet de sécurité global.

Une partie des actions écrivait déjà sa propre ligne d'historique (log_action : renommage,
suppression, fusion...; missing_volume_downloads : téléchargements; import_history :
imports; ai_history : traitements IA). Les autres (scan, statut de lecture, surveillance,
métadonnées, réglages, bibliothèques...) ne laissaient aucune trace.

Après chaque requête POST/PUT/PATCH/DELETE réussie dont l'endpoint figure dans ACTIONS,
si rien n'a été journalisé pendant la requête (drapeau g._history_logged posé par
log_action et par l'enregistrement d'un téléchargement), une ligne générique est ajoutée
dans action_history. Liste explicite plutôt que « tout ce qui écrit » : les tests de
connexion, aperçus, recherches, progression de lecture et l'enregistrement des appareils
push ne sont pas des actions à garder.

Les réglages ne journalisent que la section modifiée, jamais les valeurs (clés, mots de
passe...).
"""
import sqlite3

from flask import current_app, g, has_request_context, request

MUTATING = {'POST', 'PUT', 'PATCH', 'DELETE'}

S = ('settings', 'Réglages modifiés')

# endpoint -> (type, libellé) ou {méthode: (type, libellé)}
ACTIONS = {
    # Activité / téléchargements
    'activity.download_control': ('download_control', 'Contrôle de téléchargement'),
    'activity.remove_pending_download_route': ('download_remove', 'Téléchargement retiré'),
    'activity.update_tracking_route': ('download_control', 'Suivi de téléchargement modifié'),
    'missing_monitor.trigger_download': ('download', 'Téléchargement lancé'),
    'missing_monitor.attach_pending_series': ('match', 'Téléchargement rattaché à une série'),
    'fourtoutici.download_fourtoutici_file': ('download', 'Téléchargement fourtoutici'),
    'vk.download_vk_file': ('download', 'Téléchargement VK'),
    'telegram_channels.download_telegram_file': ('download', 'Téléchargement Telegram'),
    'telegram_channels.retry_telegram_download': ('download', 'Téléchargement relancé'),
    'qbittorrent.add_torrent': ('download', 'Ajout à qBittorrent'),
    'qbittorrent.remove_torrent': ('download_remove', 'Retiré de qBittorrent'),
    'rtorrent.add_torrent': ('download', 'Ajout à rTorrent'),
    'rtorrent.remove_torrent': ('download_remove', 'Retiré de rTorrent'),
    'deluge.add_torrent': ('download', 'Ajout à Deluge'),
    'deluge.remove_torrent': ('download_remove', 'Retiré de Deluge'),
    'emule.add_to_emule': ('download', 'Ajout à aMule'),
    'emule.remove_download': ('download_remove', 'Retiré de aMule'),
    'library.download_auto_acquire_review_candidate': ('download', 'Téléchargement depuis la validation'),
    # Validation
    'library.match_auto_acquire_review_series': ('review', 'Validation associée à une série'),
    'library.resolve_auto_acquire_review': ('review', 'Validation ignorée'),
    'library.queue_series_match_review_route': ('review', 'Mise en validation'),
    # Bibliothèques, scans
    'library.libraries': ('library', 'Bibliothèque ajoutée'),
    'library.library_operations': {'PUT': ('library', 'Bibliothèque modifiée'),
                                   'DELETE': ('library', 'Bibliothèque supprimée')},
    'library.start_library_onboard': ('library', 'Intégration de bibliothèque lancée'),
    'library.scan_library': ('scan', 'Scan de bibliothèque'),
    'library.scan_series': ('scan', 'Scan de série'),
    'library.refresh_volume': ('scan', 'Tome rafraîchi'),
    'komga.scan_libraries': ('scan', 'Scan Komga'),
    # Séries et tomes
    'library.create_series': ('add_series', 'Série créée'),
    'library.upload_series_file': ('import', 'Fichier ajouté'),
    'library.toggle_series_complete_override': ('series_edit', 'Forçage « complète » modifié'),
    'library.toggle_series_oneshot': ('series_edit', 'One-shot modifié'),
    'library.execute_series_path_correction': ('rename', 'Chemin corrigé'),
    'library.execute_rename': ('rename', 'Renommage'),
    'library.update_series_manual_metadata': ('metadata', 'Métadonnées modifiées à la main'),
    'library.update_volume_manual_metadata': ('metadata', 'Métadonnées du tome modifiées à la main'),
    'library.manage_series_tags': ('tags', 'Tags modifiés'),
    'library.set_series_reading_lists': ('reading_list', 'Listes de lecture modifiées'),
    'library.post_series_read_status': ('read', 'Statut de lecture de la série'),
    'library.post_volume_read_status': ('read', 'Statut de lecture du tome'),
    'library.download_volumes_zip': ('export', 'Téléchargement ZIP'),
    'library.delete_incompatible_folder': ('delete', "Dossier d'import incompatible supprimé"),
    'library.cleanup_import_directory': ('delete', "Dossier d'import nettoyé"),
    'library.convert_import_file': ('convert', "Conversion d'un fichier à importer"),
    'library.convert_import_files_batch': ('convert', 'Conversion de fichiers à importer'),
    'library.stop_import_route': ('import', 'Import arrêté'),
    'library.cancel_import_route': ('import', 'Import annulé'),
    'library.enrich_series_ebdz': ('metadata', 'Série enrichie (EBDZ)'),
    'library.komga_enrich_series': ('metadata', 'Série enrichie (Komga)'),
    'library.ebdz_match_series': ('match', 'Associée à EBDZ'),
    'library.ebdz_unmatch_series': ('match', 'Dissociée d\'EBDZ'),
    'library.komga_match_series': ('match', 'Associée à Komga'),
    'library.komga_unmatch_series': ('match', 'Dissociée de Komga'),
    'library.upscale_volumes_batch': ('upscale_queue', 'Upscaling demandé'),
    'library.upscale_volume_route': {'POST': ('upscale_queue', 'Upscaling demandé'),
                                     'DELETE': ('upscale_cancel', 'Upscaling annulé')},
    'library.upscale_stop_all': ('upscale_cancel', 'Upscaling arrêté'),
    'library.downscale_volume_route': ('downscale_queue', 'Réduction demandée'),
    'library.restore_volume_sd': ('upscale_restore', 'Version SD remise'),
    'library.delete_volume_sd_backup': ('upscale_backup_delete', 'Sauvegarde SD supprimée'),
    'library.approve_request': ('request', 'Demande approuvée'),
    'bedetheque.enrich_series': ('metadata', 'Série enrichie (Bédéthèque)'),
    'bedetheque.update_metadata_series': ('metadata', 'Métadonnées mises à jour'),
    'bedetheque.update_metadata_volume': ('metadata', 'Métadonnées du tome mises à jour'),
    'bedetheque.link_volume_to_bedetheque': ('match', 'Tome lié à Bédéthèque'),
    'bedetheque.link_volumes_batch': ('match', 'Tomes liés à Bédéthèque'),
    'bedetheque.convert_cbr': ('convert', 'Conversion CBR'),
    'bedetheque.convert_pdf': ('convert', 'Conversion PDF'),
    'bedetheque.convert_zip': ('convert', 'Conversion ZIP'),
    'bedetheque.convert_volumes_batch': ('convert', 'Conversion de tomes'),
    'bedetheque.convert_archive_formats_batch': ('convert', "Conversion de formats d'archive"),
    'bedetheque.backfill_author_photos': ('metadata', "Photos d'auteurs récupérées"),
    'bedetheque.build_catalog_index': ('metadata', 'Index du catalogue reconstruit'),
    # Surveillance / recherche automatique
    'missing_monitor.configure_series_monitor': ('monitor', 'Surveillance de la série'),
    'missing_monitor.configure_library_monitor': ('monitor', 'Surveillance de la bibliothèque'),
    'settings.auto_acquire_missing_files': ('auto_acquire', 'Recherche automatique lancée'),
    'settings.remove_missing_files_series_record': ('delete', 'Fiche de fichiers manquants retirée'),
    # Sources
    'ebdz.scrape': ('scrape', 'Scrap EBDZ'),
    'ebdz.rss_match_override': ('match', 'Correspondance RSS forcée'),
    'telegram_channels.scrape': ('scrape', 'Scrap Telegram'),
    'telegram_channels.backfill_start': ('scrape', 'Rattrapage Telegram lancé'),
    'telegram_channels.backfill_stop': ('scrape', 'Rattrapage Telegram arrêté'),
    'telegram_channels.add_channel': ('settings', 'Canal Telegram ajouté'),
    'telegram_channels.remove_channel': ('settings', 'Canal Telegram retiré'),
    'telegram_channels.set_match_override': ('match', 'Correspondance Telegram forcée'),
    'telegram_channels.logout': ('settings', 'Compte Telegram déconnecté'),
    'komga.sync_komga_read_progress': ('read', 'Lecture synchronisée avec Komga'),
    'komga.my_komga_account': {'PUT': ('user', 'Compte Komga lié'), 'DELETE': ('user', 'Compte Komga délié')},
    # IA
    'ai.ai_apply_normalization': ('tags', 'Normalisation des tags appliquée'),
    'ai.ai_job_stop': ('ai', 'Traitement IA arrêté'),
    # Comptes
    'auth.set_user_role': ('user', "Rôle d'un utilisateur modifié"),
    'auth.delete_my_api_key': ('user', 'Clé API personnelle supprimée'),
    # Réglages et sauvegarde
    'settings.restore_backup': ('settings', 'Sauvegarde restaurée'),
}

# Pages de réglages : seule la section est notée.
SETTINGS = {
    'ai.ai_config': 'IA', 'annas_archive.annas_archive_config': "Anna's Archive",
    'auth.oidc_config': 'OIDC', 'auth.save_roles_config': 'Rôles',
    'bedetheque.catalog_index_schedule': 'Index du catalogue', 'bedetheque.bedetheque_guard_config': 'Protection Bédéthèque',
    'deluge.deluge_config': 'Deluge', 'ebdz.auto_scrape_config': 'Scrap EBDZ automatique', 'ebdz.ebdz_config': 'EBDZ',
    'emule.emule_config': 'aMule', 'fourtoutici.fourtoutici_config': 'fourtoutici', 'komga.komga_config': 'Komga',
    'library.import_config': 'Import', 'mcp.mcp_config': 'Serveur MCP', 'missing_monitor.monitor_config': 'Surveillance',
    'notification_events.notification_site_url': 'Notifications', 'notification_events.save_notification_events': 'Notifications',
    'ntfy.ntfy_config': 'ntfy', 'prowlarr.prowlarr_config': 'Prowlarr', 'prowlarr.prowlarr_indexers': 'Indexeurs Prowlarr',
    'qbittorrent.qbittorrent_config': 'qBittorrent', 'rtorrent.rtorrent_config': 'rTorrent',
    'settings.rename_config': 'Renommage', 'settings.rss_config': 'RSS', 'settings.upscale_config': 'Upscaling',
    'shelfmark.shelfmark_config': 'Shelfmark', 'telegram.telegram_config': 'Telegram',
    'telegram_channels.telegram_channels_config': 'Canaux Telegram',
    'telegram_channels.auto_scrape_config': 'Scrap Telegram automatique', 'vk.vk_config': 'VK',
}

# Valeurs courtes du corps de requête reprises dans le détail (jamais de secret ici).
_DETAIL_KEYS = ('status', 'read', 'action', 'enabled', 'monitored', 'monitor', 'is_oneshot', 'role', 'name')
_ID_LIST_KEYS = ('volume_ids', 'series_ids', 'ids', 'review_ids', 'files', 'download_ids')


def mark_logged():
    """À appeler quand une action a écrit sa propre ligne d'historique pendant la requête."""
    try:
        if has_request_context():
            g._history_logged = True
    except Exception:
        pass


def _resolve(conn, view_args, body):
    series_id = view_args.get('series_id') or (body.get('series_id') if isinstance(body.get('series_id'), int) else None)
    series_title, extra = None, []
    volume_id = view_args.get('volume_id')
    if volume_id:
        row = conn.execute('SELECT v.series_id, v.volume_number, v.filename, s.title FROM volumes v '
                           'LEFT JOIN series s ON s.id = v.series_id WHERE v.id = ?', (volume_id,)).fetchone()
        if row:
            series_id = series_id or row[0]
            series_title = row[3]
            extra.append(f'T{row[1]}' if row[1] is not None else (row[2] or f'tome #{volume_id}'))
    if series_id and not series_title:
        row = conn.execute('SELECT title FROM series WHERE id = ?', (series_id,)).fetchone()
        series_title = row[0] if row else None
    library_id = view_args.get('library_id') or (body.get('library_id') if isinstance(body.get('library_id'), int) else None)
    if library_id:
        row = conn.execute('SELECT name FROM libraries WHERE id = ?', (library_id,)).fetchone()
        if row:
            extra.append(f'bibliothèque {row[0]}')
    return series_id, series_title, extra


def record_after_request(response):
    try:
        if request.method not in MUTATING or response.status_code >= 400 or getattr(g, '_history_logged', False):
            return response
        endpoint = request.endpoint or ''
        if endpoint in SETTINGS:
            action_type, label = 'settings', f'Réglages modifiés : {SETTINGS[endpoint]}'
            body = {}
        elif endpoint in ACTIONS:
            spec = ACTIONS[endpoint]
            if isinstance(spec, dict):
                spec = spec.get(request.method)
                if not spec:
                    return response
            action_type, label = spec
            body = request.get_json(silent=True) if request.is_json else None
            body = body if isinstance(body, dict) else {}
        else:
            return response
        if response.is_json:
            payload = response.get_json(silent=True)
            if isinstance(payload, dict) and payload.get('success') is False:
                return response
        from .action_history import log_action
        conn = sqlite3.connect(current_app.config['DATABASE'], timeout=30.0)
        try:
            series_id, series_title, extra = _resolve(conn, request.view_args or {}, body)
        finally:
            conn.close()
        for key in _DETAIL_KEYS:
            value = body.get(key)
            if isinstance(value, (bool, int, str)) and len(str(value)) <= 60:
                extra.append(f'{key} : {"oui" if value is True else "non" if value is False else value}')
        for key in _ID_LIST_KEYS:
            if isinstance(body.get(key), list):
                extra.append(f'{len(body[key])} élément(s)')
                break
        detail = label + (' · ' + ' · '.join(extra) if extra else '')
        log_action(action_type, series_id, series_title, detail)
    except Exception as exc:
        print(f"Historique : action {request.endpoint} non journalisée ({exc})")
    return response


def init_app(app):
    app.after_request(record_after_request)
