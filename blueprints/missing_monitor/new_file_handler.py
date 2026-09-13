"""Traitement direct des nouveautés EBDZ/Telegram pour la surveillance."""
from flask import current_app
from urllib.parse import parse_qs, unquote, urlparse
import html as html_module


def _item_size_bytes(source, item):
    """Taille du fichier candidat en octets, DÉJÀ connue au moment du scrape (avant tout
    téléchargement) - un lien ed2k encode la taille dans le lien lui-même
    (ed2k://|file|nom|TAILLE|hash|/, voir MyBBScraper.parse_ed2k_link -> filesize), et un
    message Telegram porte sa taille de fichier dans ses métadonnées (scraper.py,
    'file_size'). Nécessaire pour évaluer "différence notable de taille" (option upgrade
    qualité) SANS télécharger le candidat juste pour connaître sa taille."""
    raw = item.get('filesize') if source == 'ebdz' else item.get('file_size')
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _quality_upgrade_match(owned, candidate_resolution_label, candidate_size, min_increase_percent):
    """"tu verifies que le fichier est d'abord de meilleur qualité (d'apres la db) et
    ensuite s'il y a une difference notable de taille" - deux portes dans cet ordre:

    1. Qualité: si le tome possédé a une résolution connue en base, le candidat doit en
       avoir une détectée ET strictement supérieure. Si le tome possédé n'a AUCUNE
       résolution connue (tag optionnel, souvent absent), impossible de prouver quoi que
       ce soit sur la qualité - décision produit: tout candidat passe cette porte dans ce
       cas (la porte taille ci-dessous reste le seul filtre réel).
    2. Taille: le candidat doit dépasser la taille possédée d'au moins
       min_size_increase_percent - "notable", pas juste un octet de plus.

    Retourne (True, pourcentage_augmentation) ou (False, None)."""
    from blueprints.library.scanner import LibraryScanner

    if candidate_size is None or not owned.get('file_size'):
        return False, None

    owned_resolution_value = LibraryScanner.resolution_value(owned.get('resolution'))
    if owned_resolution_value is not None:
        candidate_resolution_value = LibraryScanner.resolution_value(candidate_resolution_label)
        if candidate_resolution_value is None or candidate_resolution_value <= owned_resolution_value:
            return False, None

    owned_size = owned['file_size']
    if owned_size <= 0:
        return False, None
    increase_percent = ((candidate_size - owned_size) / owned_size) * 100
    if increase_percent < min_increase_percent:
        return False, None
    return True, increase_percent


def _canonical_ebdz_thread_id(raw_id=None, raw_url=None):
    """Retourne un ``tid`` EBDZ numérique valide, jamais une chaîne arbitraire."""
    text = str(raw_id or '').strip()
    try:
        if text.isascii() and text.isdecimal() and int(text) > 0:
            return str(int(text))
        parsed = urlparse(str(raw_url or '').strip())
        if (parsed.scheme not in ('http', 'https')
                or parsed.hostname not in ('ebdz.net', 'www.ebdz.net')
                or parsed.path.rstrip('/') != '/forum/showthread.php'):
            return None
        values = parse_qs(parsed.query).get('tid') or []
        tid = str(values[0]).strip() if values else ''
        return (str(int(tid))
                if tid.isascii() and tid.isdecimal() and int(tid) > 0 else None)
    except (TypeError, ValueError):
        return None


def _resolve_ebdz_thread_series(item, monitored_series, identities_by_series):
    """Résout une nouveauté EBDZ par son thread: ``(série, état)``.

    États: ``absent`` (aucune identité EBDZ valide, le vieux matching de titre peut
    encore servir), ``matched`` (une seule série liée), ``unmatched`` ou ``ambiguous``.
    Ces deux derniers états sont bloquants: une identité forte inconnue/ambiguë ne doit
    jamais être remplacée par une déduction de titre.
    """
    item_thread_id = _canonical_ebdz_thread_id(item.get('thread_id'), item.get('thread_url'))
    if not item_thread_id:
        return None, 'absent'
    matches = []
    for row in monitored_series:
        identity = identities_by_series.get(row[0]) or {}
        series_thread_id = _canonical_ebdz_thread_id(
            identity.get('thread_id'), identity.get('thread_url')
        )
        if series_thread_id == item_thread_id:
            matches.append(row)
    if len(matches) == 1:
        return matches[0], 'matched'
    if len(matches) > 1:
        return None, 'ambiguous'
    return None, 'unmatched'


def process_new_monitor_files(source, items):
    """Télécharge les nouveaux fichiers qui correspondent à un tome manquant surveillé -
    ou, si l'option "upgrade qualité" est activée, à un tome DÉJÀ possédé d'une série
    surveillée dont ce nouveau fichier serait une meilleure version (voir
    _quality_upgrade_match).

    Cette voie ne lance aucune recherche: ``items`` contient exclusivement les lignes
    réellement insérées pendant le scrape courant.
    """
    from blueprints.library.action_history import log_action
    from blueprints.library.routes import (
        _match_series_for_auto_import,
        _normalize_title_for_match,
        get_db_connection,
    )
    from blueprints.library.scanner import LibraryScanner
    from blueprints.missing_monitor.downloader import (
        MissingVolumeDownloader,
        get_pending_download_state_for_series,
    )
    from blueprints.missing_monitor.routes import load_monitor_config

    stats = {'new_files': len(items), 'matched_missing': 0, 'matched_quality_upgrade': 0,
             'downloads_sent': 0, 'quality_upgrades_sent': 0, 'notifications_sent': 0,
             'skipped_pending': 0, 'queued_for_review': 0, 'errors': []}
    monitor_config_data = load_monitor_config()
    missing_config = monitor_config_data.get('monitor_missing_volumes', {})
    missing_enabled = missing_config.get('enabled', True)
    quality_config = monitor_config_data.get('quality_upgrade', {})
    quality_enabled = quality_config.get('enabled', False)
    if not missing_enabled and not quality_enabled:
        return stats
    action = missing_config.get('action', 'download')
    min_increase_percent = quality_config.get('min_size_increase_percent', 20)

    conn = get_db_connection()
    cursor = conn.cursor()
    # Même forme de ligne que _resolve_auto_import_series_match (routes.py) - le contrat
    # documenté par _match_series_for_auto_import ((id, library_id, library_path, title,
    # is_oneshot, library_name)) est un point d'entrée UNIQUE partagé par les deux
    # appelants ; bedetheque_url est nécessaire ici pour le repli par hint (voir plus bas)
    # mais n'a rien à voir avec ce contrat de ligne, donc récupéré séparément plutôt que
    # de réutiliser silencieusement l'index 5 pour autre chose que library_name.
    # "s.missing_volumes != '[]'" retiré du WHERE (présent à l'origine, quand cette
    # requête ne servait qu'au matching de tomes manquants): une série surveillée déjà
    # complète doit rester éligible à l'upgrade qualité de ses tomes déjà possédés.
    cursor.execute('''
        SELECT s.id, s.library_id, l.path, s.title, s.is_oneshot, l.name
        FROM series s
        JOIN libraries l ON s.library_id = l.id
        JOIN missing_volume_monitor mm ON mm.series_id = s.id
        WHERE mm.enabled = 1
    ''')
    monitored_series = cursor.fetchall()
    series_ids = [row[0] for row in monitored_series]
    placeholders = ','.join('?' for _ in series_ids)
    import json
    if series_ids:
        cursor.execute(f'SELECT id, missing_volumes FROM series WHERE id IN ({placeholders})', series_ids)
        missing_by_series = {row[0]: set(json.loads(row[1] or '[]')) for row in cursor.fetchall()}
        cursor.execute(f'SELECT id, bedetheque_url FROM series WHERE id IN ({placeholders})', series_ids)
        bedetheque_url_by_series = {row[0]: row[1] for row in cursor.fetchall()}
        cursor.execute(f'SELECT id, ebdz_thread_id, ebdz_thread_url FROM series WHERE id IN ({placeholders})', series_ids)
        ebdz_identity_by_series = {
            row[0]: {'thread_id': row[1], 'thread_url': row[2]}
            for row in cursor.fetchall()
        }
        # Uniquement nécessaire pour l'upgrade qualité - tomes déjà possédés (tome
        # "plain", même périmètre que le matching manquant ci-dessous qui ne gère lui
        # aussi que volume_number, pas intégrale/HS/épisode).
        owned_volume_by_series_number = {}
        if quality_enabled:
            cursor.execute(f'''
                SELECT series_id, volume_number, id, file_size, resolution
                FROM volumes
                WHERE series_id IN ({placeholders}) AND volume_number IS NOT NULL AND filepath IS NOT NULL
            ''', series_ids)
            owned_volume_by_series_number = {
                (row[0], row[1]): {'id': row[2], 'file_size': row[3], 'resolution': row[4]}
                for row in cursor.fetchall()
            }
    else:
        missing_by_series = {}
        bedetheque_url_by_series = {}
        ebdz_identity_by_series = {}
        owned_volume_by_series_number = {}
    conn.close()

    scanner = LibraryScanner()
    downloader = MissingVolumeDownloader()
    handled = set()

    for item in items:
        try:
            filename = html_module.unescape(unquote(item.get('filename') or ''))
            parsed = scanner.parse_filename(filename)
            volume_number = parsed.get('volume')
            if volume_number is None:
                continue

            normalized_parsed_title = _normalize_title_for_match(parsed.get('title', ''))
            thread_match, thread_status = (
                _resolve_ebdz_thread_series(item, monitored_series, ebdz_identity_by_series)
                if source == 'ebdz' else (None, 'absent')
            )
            # Une identité EBDZ valide mais inconnue/ambiguë est un conflit de données,
            # pas une invitation à redéduire la série depuis le titre.
            if thread_status in ('unmatched', 'ambiguous'):
                continue
            match = thread_match or _match_series_for_auto_import(normalized_parsed_title, monitored_series)
            hint = (item.get('bedetheque_url_hint') or '').strip().rstrip('/')
            if hint and not match:
                match = next((row for row in monitored_series
                              if (bedetheque_url_by_series.get(row[0]) or '').strip().rstrip('/') == hint), None)
            if not match:
                continue

            series_id = match[0]
            is_missing = volume_number in missing_by_series.get(series_id, set())
            quality_increase_percent = None
            if not is_missing:
                if not quality_enabled:
                    continue
                owned = owned_volume_by_series_number.get((series_id, volume_number))
                if not owned:
                    continue
                is_upgrade, quality_increase_percent = _quality_upgrade_match(
                    owned, parsed.get('resolution'), _item_size_bytes(source, item), min_increase_percent
                )
                if not is_upgrade:
                    continue

            identity = (series_id, volume_number)
            if identity in handled:
                continue
            handled.add(identity)

            is_exact_title_match = _normalize_title_for_match(match[3]) == normalized_parsed_title
            is_hint_confirmed = bool(hint) and (bedetheque_url_by_series.get(match[0]) or '').strip().rstrip('/') == hint
            is_thread_confirmed = bool(thread_match) and thread_match[0] == match[0]
            if not is_exact_title_match and not is_hint_confirmed and not is_thread_confirmed:
                from blueprints.bedetheque.auto_acquire import queue_manual_review
                candidate = {
                    'source': source, 'filename': item.get('filename'), 'link': item.get('link'),
                    'thread_url': item.get('thread_url'), 'channel': item.get('channel'),
                    'message_id': item.get('message_id'), 'channel_title': item.get('channel_title'),
                }
                review_reason = f"Titre détecté « {parsed.get('title')} » proche mais pas identique à « {match[3]} » - correspondance de série non confirmée, à vérifier manuellement."
                if not is_missing:
                    review_reason += " (candidat upgrade qualité pour un tome déjà possédé)"
                queue_manual_review(
                    series_id, match[3], volume_number, f'Tome {volume_number}', [candidate],
                    review_reason,
                    force_candidates=True
                )
                stats['queued_for_review'] += 1
                continue

            if is_missing:
                stats['matched_missing'] += 1
            else:
                stats['matched_quality_upgrade'] += 1

            has_pack, pending_numbers = get_pending_download_state_for_series(series_id)
            if has_pack or volume_number in pending_numbers:
                stats['skipped_pending'] += 1
                continue

            # L'action "notifier au lieu de télécharger" ne s'applique qu'aux tomes
            # manquants (monitor_missing_volumes.action) - l'upgrade qualité n'a pas
            # d'équivalent "notify", elle télécharge directement dès que les deux portes
            # de _quality_upgrade_match passent (voir sa docstring).
            if is_missing and action == "notify":
                from blueprints.telegram.routes import send_telegram_notification
                source_label = "EBDZ" if source == "ebdz" else "Telegram"
                source_url = (item.get("thread_url") if source == "ebdz" else
                              f"https://t.me/{item.get("channel", "").lstrip("@")}/{item.get("message_id")}")
                message = (f"📚 Nouveau tome surveillé\n"
                           f"Série : {match[3]}\n"
                           f"Tome : {volume_number}\n"
                           f"Source : {source_label}\n"
                           f"Fichier : {filename}"
                           + (f"\n{source_url}" if source_url else ""))
                success, error = send_telegram_notification(message)
                if success:
                    stats["notifications_sent"] += 1
                    print(f"  ✓ Notification envoyée pour {match[3]} tome {volume_number}")
                else:
                    stats["errors"].append(error or "Échec notification Telegram")
                    print(f"  ✗ Notification Telegram: {error}")
                continue

            if source == 'ebdz':
                success, message = downloader.send_torrent_download(
                    item.get('link'), match[3], volume_number, series_id=series_id,
                    filename=filename, source='ebdz', source_link=item.get('thread_url')
                )
            elif source == 'telegram':
                from blueprints.telegram_channels.routes import _require_connected_config
                from blueprints.telegram_channels.scraper import download_channel_file_background

                config = _require_connected_config()
                target_dir = current_app.config.get('TELEGRAM_IMPORT_DIRECTORY')
                if not config or not target_dir:
                    raise RuntimeError('Telegram non connecté ou répertoire non configuré')
                download_channel_file_background(
                    config['api_id'], config['api_hash_decrypted'], config['session_decrypted'],
                    item['channel'], item['message_id'], target_dir,
                    channel_title=item.get('channel_title'),
                    app=current_app._get_current_object(), pending_title=filename,
                    series_id=series_id, volume_number=volume_number
                )
                success, message = True, f'{filename} envoyé à Telegram'
            else:
                raise ValueError(f'Source non prise en charge: {source}')

            if success:
                stats['downloads_sent'] += 1
            else:
                stats['errors'].append(message)

            # "meme s'il y a pas d'upgrade de qualité rajoute qu'un fichier a ete mis en
            # download parce que manquant et surveillé" - même traitement que la branche
            # upgrade qualité ci-dessous (journalisé au moment du déclenchement du
            # téléchargement, succès ou échec), pour que le tableau de bord Historique
            # explique le "pourquoi" de CHAQUE téléchargement déclenché par Surveillance,
            # pas seulement les upgrades qualité.
            if is_missing:
                detail = (
                    f"Tome {volume_number} : nouveau fichier « {filename} » détecté, correspond à un tome "
                    f"manquant d'une série surveillée - téléchargement automatique déclenché."
                    if success else
                    f"Tome {volume_number} : nouveau fichier « {filename} » détecté pour un tome manquant "
                    f"d'une série surveillée, mais échec du déclenchement du téléchargement : {message}"
                )
                log_action('missing_volume_match', series_id, match[3], detail, success=success,
                            error=None if success else message)
            else:
                if success:
                    stats['quality_upgrades_sent'] += 1
                detail = (
                    f"Tome {volume_number} : nouveau fichier « {filename} » détecté comme meilleure qualité "
                    f"que le fichier possédé (+{quality_increase_percent:.0f}% de taille) - "
                    f"téléchargement automatique déclenché selon les critères de surveillance (upgrade qualité)."
                    if success else
                    f"Tome {volume_number} : candidat upgrade qualité détecté (« {filename} », "
                    f"+{quality_increase_percent:.0f}% de taille) mais échec du déclenchement du téléchargement : {message}"
                )
                log_action('quality_upgrade', series_id, match[3], detail, success=success,
                            error=None if success else message)

            print(f"  {'✓' if success else '✗'} {message}")
        except Exception as exc:
            message = f"{item.get('filename', 'fichier')}: {exc}"
            stats['errors'].append(message)
            print(f"  ✗ {message}")

    return stats
