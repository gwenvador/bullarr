"""
Intégration de fourtoutici.cc (item #24 improvement.txt: "Integration of
https://fourtoutici.cc/ add this to the source. you will need to search and parse. not
much difficulty after search click telecharger") - contrairement à EBDZ (ed2k, poussé vers
eMule/aMule) ou Prowlarr (torrent, poussé vers qBittorrent/rTorrent/Deluge), fourtoutici
sert ses fichiers en HTTP direct derrière une petite API JSON (pas de forum MyBB ni de
tracker) : le téléchargement se fait donc en interne, exactement comme pour Telegram (voir
download_channel_file_background, blueprints/telegram_channels/scraper.py) - un thread
dédié qui écrit directement dans un répertoire d'import surveillé, aucun client externe à
piloter.
"""
import os
import re
import threading

import requests
from network_safety import safe_external_get

FOURTOUTICI_BASE_URL = 'https://fourtoutici.cc'


def get_fourtoutici_base_url():
    """Adresse de base configurable (Configuration > Indexeurs > Web, champ `base_url`
    dans fourtoutici_config.json) - "je voudrais modifier manuellement l'adresse de
    fourtoutici": le site change parfois de domaine/miroir, sans ça il aurait fallu
    modifier FOURTOUTICI_BASE_URL et redéployer à chaque fois. Repli sur
    FOURTOUTICI_BASE_URL par défaut si aucun contexte Flask n'est disponible (ex: un
    futur script standalone, même raisonnement que backfill_ratings.py qui n'utilise pas
    create_app())."""
    try:
        from .routes import load_fourtoutici_config
        return (load_fourtoutici_config().get('base_url') or FOURTOUTICI_BASE_URL).rstrip('/')
    except RuntimeError:
        return FOURTOUTICI_BASE_URL

# Même UA que bedetheque/scraper.py et ebdz/scraper.py - un User-Agent par défaut de
# `requests` s'est vu opposer un 403 lors de la reconnaissance de cette API, un UA de
# navigateur de bureau classique suffit à passer.
_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

# Mêmes extensions que le reste du pipeline d'import (voir monitored_extensions,
# config.py) - fourtoutici héberge aussi des ebooks/audio/magazines (catégories EBOOK/
# JOURNAL/MAGAZINE/AUDIO/AUTRES, voir son formulaire de recherche), sans intérêt ici.
SUPPORTED_EXTENSIONS = {'.cbz', '.cbr', '.zip', '.rar', '.pdf'}

# Une requête par page suffit dans l'immense majorité des cas (une série de BD
# n'a normalement pas des dizaines de résultats sur ce site) - ce plafond n'est qu'un
# garde-fou contre une requête trop générique qui déclencherait un scan de tout le
# catalogue page par page.
MAX_PAGES = 3

# Avoid an unbounded remote response filling the Docker host filesystem.
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024


def search_fourtoutici_raw(query, limit=30):
    """Interroge l'API de recherche de fourtoutici (pas de forum/HTML à parser: une API
    JSON existe déjà, découverte via assets/js/app.js du site - `GET
    /backend/api/files.php?q=<query>&page=<n>`, réponse `{success, files, has_more}`).
    Pas de filtre `cat` posé côté requête: le filtre par extension ci-dessous
    (SUPPORTED_EXTENSIONS) écarte déjà les ebooks/audio/magazines sans risquer de rater un
    fichier mal catégorisé côté site.

    Retourne les entrées brutes de l'API (mêmes clés que la réponse: file_id, original_name,
    size, extension, created_at...), filtrées aux extensions BD et augmentées d'un
    download_url prêt à l'emploi - le parsing volume/titre et le scoring de pertinence
    restent du ressort de l'appelant (voir MissingVolumeSearcher._search_fourtoutici),
    même découpage que pour Telegram (search_telegram_files_local ne fait, elle non plus,
    aucun scoring)."""
    query = (query or '').strip()
    if not query:
        return []

    # "j'ai désactivé prowlarr mais il s'affiche toujours dans découvrir et rechercher" -
    # même vérification défensive côté serveur que search_prowlarr_raw (blueprints/
    # prowlarr/search.py): un appel direct à /api/missing-monitor/search ne doit jamais
    # interroger fourtoutici si l'utilisateur l'a désactivé dans Configuration, même si le
    # frontend a par ailleurs déjà masqué/décoché sa case.
    from .routes import load_fourtoutici_config
    config = load_fourtoutici_config()
    if not config.get('enabled', True):
        return []

    base_url = (config.get('base_url') or FOURTOUTICI_BASE_URL).rstrip('/')
    search_url = f'{base_url}/backend/api/files.php'
    download_url = f'{base_url}/backend/api/download.php'

    results = []
    page = 0
    while page < MAX_PAGES and len(results) < limit:
        try:
            response = safe_external_get(
                search_url,
                max_bytes=5 * 1024 * 1024,
                params={'q': query, 'page': page} if page else {'q': query},
                headers=_HEADERS,
                timeout=15
            )
            if response.status_code != 200:
                break
            data = response.json()
        except (requests.exceptions.RequestException, ValueError) as e:
            print(f"Erreur recherche fourtoutici: {e}")
            break

        if not data.get('success'):
            break

        for f in data.get('files', []):
            ext = ('.' + (f.get('extension') or '').lstrip('.')).lower()
            if ext not in SUPPORTED_EXTENSIONS:
                continue
            f['download_url'] = f"{download_url}?file_id={f.get('file_id', '')}"
            results.append(f)

        if not data.get('has_more'):
            break
        page += 1

    return results[:limit]


def _download_one(file_id, filename, target_dir, base_url, progress_callback=None):
    """Téléchargement HTTP direct en streaming - pas de client/session persistante à
    maintenir (contrairement à Telethon côté Telegram), une simple requête GET suffit.

    base_url: résolu par l'appelant (voir download_fourtoutici_file_background) - ce
    thread d'arrière-plan n'a pas de contexte Flask automatique, get_fourtoutici_base_url()
    ne peut donc pas être rappelé ici directement.

    Même convention ".part" que download_channel_file_background (telegram_channels/
    scraper.py): le scheduler d'import automatique scanne le même répertoire en
    parallèle et ignore déjà cette extension, un fichier encore à moitié écrit ne doit
    jamais lui être visible avant la fin du téléchargement."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', str(file_id)):
        raise ValueError('Identifiant de fichier non autorisé')
    target_root = os.path.realpath(target_dir)
    safe_name = os.path.basename(str(filename))
    target_path = os.path.realpath(os.path.join(target_root, safe_name))
    if not safe_name or os.path.commonpath((target_root, target_path)) != target_root:
        raise ValueError('Nom de fichier non autorisé')
    if os.path.exists(target_path):
        base, ext = os.path.splitext(safe_name)
        target_path = os.path.join(target_dir, f'{base}_{file_id}{ext}')
    temp_path = target_path + '.part'

    url = f"{base_url}/backend/api/download.php?file_id={file_id}"
    response = safe_external_get(url, headers=_HEADERS, timeout=30)
    if response.status_code != 200:
        raise ValueError(f"Téléchargement impossible (HTTP {response.status_code})")

    total = int(response.headers.get('Content-Length') or 0) or None
    if total and total > MAX_DOWNLOAD_BYTES:
        response.close()
        raise ValueError('Téléchargement refusé: fichier trop volumineux')

    downloaded = 0
    with open(temp_path, 'wb') as out_file:
        for chunk in response.iter_content(chunk_size=1024 * 256):
            if not chunk:
                continue
            out_file.write(chunk)
            downloaded += len(chunk)
            if downloaded > MAX_DOWNLOAD_BYTES:
                response.close()
                out_file.close()
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
                raise ValueError('Téléchargement interrompu: fichier trop volumineux')
            if progress_callback:
                progress_callback(downloaded, total)
    response.close()

    if total and downloaded != total:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise ValueError(f"Téléchargement tronqué: {downloaded} octets reçus sur {total} attendus")

    os.rename(temp_path, target_path)
    return target_path


def download_fourtoutici_file_background(file_id, filename, target_dir, base_url, app=None,
                                          pending_title=None, series_id=None,
                                          volume_id=None, volume_number=None, force_replace=False):
    """Lance le téléchargement dans un thread dédié et retourne immédiatement - même
    raisonnement que download_channel_file_background (telegram_channels/scraper.py): un
    fichier de plusieurs dizaines/centaines de Mo bloquerait la requête HTTP le temps du
    transfert complet.

    base_url: résolu par l'appelant (routes.py, dans le contexte de la requête HTTP) via
    get_fourtoutici_base_url() - pas rappelé ici, ce thread n'a pas de contexte Flask
    automatique une fois lancé.

    app: current_app._get_current_object() capturé par l'appelant (voir routes.py) -
    nécessaire pour journaliser (log_manual_download) et suivre la progression
    (active_downloads) depuis ce thread sans contexte de requête Flask automatique (voir
    CLAUDE.md, section Komga, même piège)."""
    def _log(name, success, message='', tracking_id=None):
        if not app:
            return
        try:
            from blueprints.missing_monitor.downloader import log_manual_download
            # "dans historique il faudrait voir quelle est la source du téléchargement et
            # cliquable aussi" - pas de page de release distincte du lien de
            # téléchargement lui-même pour fourtoutici (même constat que côté
            # _searchResultSourceLinkUrl, static/js/search-results-table.js), donc pas de
            # source_link ici.
            with app.app_context():
                log_manual_download(name, 'fourtoutici', success, message, source='fourtoutici', tracking_id=tracking_id)
        except Exception as e:
            print(f"Erreur journalisation téléchargement fourtoutici dans Historique: {e}")

    def run():
        download_id = None
        if app:
            try:
                from blueprints.missing_monitor.downloader import mark_download_pending
                with app.app_context():
                    download_id = mark_download_pending(
                        pending_title or filename, 'fourtoutici',
                        series_id=series_id, volume_id=volume_id, volume_number=volume_number,
                        force_replace=force_replace
                    )
            except Exception as e:
                print(f"Erreur enregistrement téléchargement fourtoutici en attente: {e}")

        # Throttlé au pourcentage entier, même raisonnement que Telegram (on_progress,
        # download_channel_file_background) - évite de marteler SQLite à chaque chunk reçu.
        last_reported_pct = -1

        def on_progress(current, total):
            nonlocal last_reported_pct
            if not app or not download_id or not total:
                return
            pct = int(current * 100 / total)
            if pct == last_reported_pct and pct < 100:
                return
            last_reported_pct = pct
            try:
                from blueprints.missing_monitor.downloader import update_download_progress
                with app.app_context():
                    update_download_progress(download_id, current, total)
            except Exception as e:
                print(f"Erreur mise à jour progression téléchargement fourtoutici: {e}")

        try:
            os.makedirs(target_dir, exist_ok=True)
            target_path = _download_one(file_id, filename, target_dir, base_url, progress_callback=on_progress)
            _log(filename, True, 'Téléchargé depuis fourtoutici.cc', download_id)
            if app and download_id:
                from blueprints.missing_monitor.downloader import mark_download_completed
                with app.app_context():
                    mark_download_completed(download_id)
            # Import automatique immédiat une fois le fichier écrit - même raisonnement
            # que Telegram (download_channel_file_background): cette source aussi sait
            # EXACTEMENT quand le téléchargement se termine (thread interne), pas besoin
            # d'attendre le prochain passage du scheduler périodique.
            if app:
                try:
                    from blueprints.library.routes import attempt_immediate_auto_import
                    with app.app_context():
                        attempt_immediate_auto_import(target_path, target_dir)
                except Exception as e:
                    print(f"Erreur import automatique immédiat après téléchargement fourtoutici: {e}")
        except Exception as e:
            _log(filename, False, str(e), download_id)
            if app and download_id:
                from blueprints.missing_monitor.downloader import mark_download_failed
                with app.app_context():
                    mark_download_failed(download_id)

    threading.Thread(target=run, daemon=True).start()
