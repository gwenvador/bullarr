"""
Scraping des canaux Telegram configurés (voir routes.py pour la connexion au compte
utilisateur) - liste les fichiers BD postés dans chaque canal, dans une base SQLite dédiée
(data/telegram_messages.db, même principe qu'ebdz.db pour les liens ed2k: un cache des
"nouveautés" trouvées, séparé de la base principale) - "ajoute tous les fichiers nouveaux
dans la section nouveautes... il faudra montrer la source ebdz / telegram" (item #16).

Le téléchargement effectif d'un fichier (voir download_channel_file) le dépose directement
dans un répertoire d'import surveillé plutôt que de l'importer lui-même: un fichier
Telegram nouvellement téléchargé suit alors exactement le même chemin qu'un fichier arrivé
via aMule/un torrent (scan, matching automatique ou assignation manuelle sur /import,
sélecteur de tome...) - aucune logique d'import dupliquée ici.
"""
import asyncio
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone

from telethon import TelegramClient
from telethon.sessions import StringSession

TELEGRAM_FILES_DB = './data/telegram_messages.db'

# Mêmes extensions que le reste du pipeline d'import (voir monitored_extensions,
# config.py) - filtre aussi les vignettes/miniatures que Telegram attache parfois au même
# message (constaté: un .jpg de quelques centaines de Ko à côté du .cbz réel)
SUPPORTED_EXTENSIONS = {'.cbz', '.cbr', '.zip', '.rar', '.pdf'}

_BEDETHEQUE_URL_RE = re.compile(r'https?://(?:www\.)?bedetheque\.com/\S+', re.IGNORECASE)


def _extract_bedetheque_url(msg):
    """Cherche un lien bedetheque.com dans la légende du message ("in telegram if there
    is a bedetheque link use it in the nouveautes serie column") - certains canaux
    postent l'album accompagné d'un lien direct vers sa fiche Bédéthèque en légende, une
    identification bien plus fiable que le matching flou par titre (voir
    _annotate_already_in_library côté routes.py). Cherche d'abord dans le texte brut, puis
    dans les entités du message (msg.entities) : un lien "masqué" (texte affiché différent
    de l'URL, MessageEntityTextUrl) n'apparaît pas dans raw_text mais expose son URL réelle
    via entity.url."""
    text = msg.raw_text or ''
    match = _BEDETHEQUE_URL_RE.search(text)
    if match:
        return match.group(0).rstrip(').,;')
    for entity in (msg.entities or []):
        url = getattr(entity, 'url', None)
        if url and 'bedetheque.com' in url.lower():
            return url
    return None


def _connect_db():
    os.makedirs(os.path.dirname(TELEGRAM_FILES_DB), exist_ok=True)
    conn = sqlite3.connect(TELEGRAM_FILES_DB)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS telegram_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT NOT NULL,
            channel_title TEXT,
            message_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            file_size INTEGER,
            message_date TEXT,
            scraped_at TEXT NOT NULL,
            downloaded INTEGER DEFAULT 0,
            UNIQUE(channel, message_id)
        )
    ''')
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(telegram_files)")
    existing_columns = {row[1] for row in cursor.fetchall()}
    if 'filename_normalized' not in existing_columns:
        cursor.execute('ALTER TABLE telegram_files ADD COLUMN filename_normalized TEXT')
    # Lien bedetheque.com extrait de la légende du message si présent (voir
    # _extract_bedetheque_url) - même colonne défensive ajoutée après coup que
    # filename_normalized ci-dessus, PRAGMA table_info + ALTER TABLE plutôt qu'une
    # migration dédiée (convention de ce fichier de données, voir bullarr.db).
    if 'bedetheque_url_hint' not in existing_columns:
        cursor.execute('ALTER TABLE telegram_files ADD COLUMN bedetheque_url_hint TEXT')
    conn.execute('''
        CREATE VIRTUAL TABLE IF NOT EXISTS telegram_files_fts USING fts5(
            filename_normalized, content='telegram_files', content_rowid='id', tokenize='trigram'
        )
    ''')
    # Triggers de synchronisation créés dans ensure_telegram_search_index(), APRÈS le
    # peuplement initial des lignes déjà présentes, pas ici - voir la docstring de cette
    # fonction (même raisonnement complet que ed2k_links, blueprints/ebdz/scraper.py):
    # sinon le premier peuplement (UPDATE en masse) déclencherait le futur trigger AU sur
    # chaque ligne, doublant son propre travail en écritures FTS dans la même transaction.
    conn.commit()
    return conn


def ensure_telegram_search_index():
    """Peuple filename_normalized + l'index FTS5 pour les lignes déjà présentes avant
    l'ajout de cette colonne, puis crée les triggers de synchronisation - migration
    défensive appelée une seule fois au démarrage (voir app.py). Colonne+table FTS (sans
    triggers) déjà créées par _connect_db().

    Ordre volontaire (normaliser -> peupler la FTS -> créer les triggers, pas l'inverse):
    voir ensure_ed2k_search_index (blueprints/ebdz/scraper.py) pour le "database disk
    image is malformed" observé une fois avec les triggers déjà en place pendant ce même
    genre de peuplement en masse - jamais une corruption permanente (le fichier repassait
    "ok" à l'intégrity_check une fois réouvert), mais plus la peine de risquer cette charge
    d'écriture amplifiée pour un peuplement qui n'en a pas besoin."""
    from blueprints.search.routes import normalize_search_text

    conn = _connect_db()
    conn.create_function('search_normalize', 1, normalize_search_text)
    cursor = conn.cursor()

    cursor.execute('SELECT COUNT(*) FROM telegram_files WHERE filename_normalized IS NULL')
    pending = cursor.fetchone()[0]
    if pending:
        print(f"⏳ Indexation recherche Telegram: normalisation de {pending} fichier(s)...")
        # Une seule UPDATE (SQLite appelle la fonction par ligne en interne) plutôt qu'un
        # aller-retour Python - voir ensure_ed2k_search_index, même raisonnement.
        cursor.execute('''
            UPDATE telegram_files
            SET filename_normalized = search_normalize(filename)
            WHERE filename_normalized IS NULL
        ''')
        conn.commit()

    cursor.execute('SELECT COUNT(*) FROM telegram_files_fts_idx')
    if cursor.fetchone()[0] == 0:
        cursor.execute('SELECT COUNT(*) FROM telegram_files')
        total = cursor.fetchone()[0]
        if total:
            print(f"⏳ Indexation recherche Telegram: peuplement FTS5 ({total} fichier(s))...")
            # Commande officielle de (re)construction d'un index "external content" à
            # partir de sa table de contenu - plus fiable qu'un INSERT...SELECT manuel.
            cursor.execute("INSERT INTO telegram_files_fts(telegram_files_fts) VALUES('rebuild')")
            conn.commit()

    cursor.execute('''
        CREATE TRIGGER IF NOT EXISTS telegram_files_ai AFTER INSERT ON telegram_files BEGIN
            INSERT INTO telegram_files_fts(rowid, filename_normalized) VALUES (new.id, new.filename_normalized);
        END
    ''')
    cursor.execute('''
        CREATE TRIGGER IF NOT EXISTS telegram_files_ad AFTER DELETE ON telegram_files BEGIN
            INSERT INTO telegram_files_fts(telegram_files_fts, rowid, filename_normalized)
            VALUES('delete', old.id, old.filename_normalized);
        END
    ''')
    cursor.execute('''
        CREATE TRIGGER IF NOT EXISTS telegram_files_au AFTER UPDATE ON telegram_files BEGIN
            INSERT INTO telegram_files_fts(telegram_files_fts, rowid, filename_normalized)
            VALUES('delete', old.id, old.filename_normalized);
            INSERT INTO telegram_files_fts(rowid, filename_normalized) VALUES (new.id, new.filename_normalized);
        END
    ''')
    conn.commit()
    conn.close()
    print("✓ Index de recherche Telegram prêt")


# Un seul client Telegram connecté à la fois (scrape/recherche/téléchargement partagent ce
# verrou) - 3 téléchargements lancés à quelques secondes d'intervalle ont déjà déclenché
# "The provided authorization is invalid" côté serveur, chacun ouvrant sa propre connexion
# avec la MÊME session en parallèle, accompagné d'un lot d'erreurs "Event loop is closed"
# de Telethon (le loop.close() ci-dessous survenant avant la fin propre du disconnect()
# d'un autre client concurrent). Sérialiser élimine ce recouvrement plutôt que de fiabiliser
# plus finement l'arrêt concurrent de plusieurs event loops.
_telegram_client_lock = threading.Lock()


def _run_async(coro_fn, *args, **kwargs):
    with _telegram_client_lock:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro_fn(*args, loop, **kwargs))
        finally:
            loop.close()


async def _scrape_channel(client, channel, limit, search=None):
    """search: requête plein texte de Telethon (recherche côté serveur Telegram, pas
    seulement sur les `limit` derniers messages - voir search_channels ci-dessous, "la
    possibilité de faire une recherche" dans tout l'historique du canal, pas juste les
    nouveautés)."""
    entity = await client.get_entity(channel)
    channel_title = getattr(entity, 'title', channel)
    found = []
    async for msg in client.iter_messages(entity, limit=limit, search=search):
        if not msg.file or not msg.file.name:
            continue
        ext = os.path.splitext(msg.file.name)[1].lower()
        if ext not in SUPPORTED_EXTENSIONS:
            continue
        found.append({
            'message_id': msg.id,
            'filename': msg.file.name,
            'file_size': msg.file.size,
            'message_date': msg.date.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S') if msg.date else None,
            'bedetheque_url_hint': _extract_bedetheque_url(msg),
        })
    return channel_title, found


async def _scrape_all(api_id, api_hash, session_string, channels, limit, loop, search=None):
    client = TelegramClient(StringSession(session_string), api_id, api_hash, loop=loop)
    await client.connect()
    try:
        results = {}
        for channel in channels:
            try:
                title, files = await _scrape_channel(client, channel, limit, search=search)
                results[channel] = {'title': title, 'files': files, 'error': None}
            except Exception as e:
                results[channel] = {'title': channel, 'files': [], 'error': str(e)}
        return results
    finally:
        await client.disconnect()


BACKFILL_CHUNK_SIZE = 300
BACKFILL_STATE_FILE = './data/telegram_backfill_state.json'

# Telegram may temporarily rate-limit file chunk requests with an undocumented
# ``FLOOD_PREMIUM_WAIT_N`` RPC error. This is a transient server response (not a
# bad session or a corrupt archive), so let the same download recover before it
# is reported as failed. Keep the retry bounded: a permanently blocked account
# must still become visible in Historique instead of leaving a worker asleep
# indefinitely.
TELEGRAM_DOWNLOAD_FLOOD_RETRIES = 3
TELEGRAM_DOWNLOAD_MAX_WAIT_SECONDS = 120


def _telegram_flood_wait_seconds(exc):
    """Return Telegram's requested wait for a temporary flood error.

    Telethon exposes ``seconds`` for known FloodWaitError variants, while the
    premium variant currently arrives as a generic RPCError whose text is e.g.
    ``RPCError 420: FLOOD_PREMIUM_WAIT_5 (caused by GetFileRequest)``. Support
    both forms and deliberately return ``None`` for unrelated RPC failures.
    """
    seconds = getattr(exc, 'seconds', None)
    if seconds is not None:
        try:
            return max(1, int(seconds))
        except (TypeError, ValueError):
            pass
    match = re.search(r'FLOOD(?:_PREMIUM)?_WAIT[_ ](\d+)', str(exc), re.IGNORECASE)
    if not match:
        return None
    return max(1, int(match.group(1)))


def _load_backfill_state():
    if not os.path.exists(BACKFILL_STATE_FILE):
        return {}
    try:
        with open(BACKFILL_STATE_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_backfill_state(state):
    os.makedirs(os.path.dirname(BACKFILL_STATE_FILE), exist_ok=True)
    with open(BACKFILL_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


async def _backfill_channel_chunk(api_id, api_hash, session_string, channel, offset_id, loop):
    """Un seul morceau de BACKFILL_CHUNK_SIZE messages, en repartant juste avant offset_id
    (0/None = depuis le tout début, les plus récents) - Telethon parcourt du plus récent au
    plus ancien par défaut, offset_id fait donc avancer vers le PASSÉ à chaque appel.
    Retourne (channel_title, files_trouvés, dernier_message_id_vu, épuisé). épuisé=True
    quand ce morceau contient moins de messages que demandé: on a atteint le tout premier
    message du canal, plus rien à parcourir derrière. Connexion propre à cet appel (comme
    _scrape_all/_download_one) - _run_async sérialise déjà via _telegram_client_lock,
    jamais deux clients Telegram simultanés avec la même session."""
    client = TelegramClient(StringSession(session_string), api_id, api_hash, loop=loop)
    await client.connect()
    try:
        entity = await client.get_entity(channel)
        channel_title = getattr(entity, 'title', channel)
        files = []
        last_id = offset_id
        processed = 0
        async for msg in client.iter_messages(entity, limit=BACKFILL_CHUNK_SIZE, offset_id=offset_id or 0):
            last_id = msg.id
            processed += 1
            if msg.file and msg.file.name:
                ext = os.path.splitext(msg.file.name)[1].lower()
                if ext in SUPPORTED_EXTENSIONS:
                    files.append({
                        'message_id': msg.id,
                        'filename': msg.file.name,
                        'file_size': msg.file.size,
                        'message_date': msg.date.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S') if msg.date else None,
                        'bedetheque_url_hint': _extract_bedetheque_url(msg),
                    })
        exhausted = processed < BACKFILL_CHUNK_SIZE
        return channel_title, files, last_id, exhausted
    finally:
        await client.disconnect()


def run_backfill_background(api_id, api_hash, session_string, channels, stop_flag):
    """Lance le backfill complet des `channels` fournis, un morceau à la fois, jusqu'à ce
    que chacun soit épuisé (tout son historique indexé) ou que stop_flag (dict mutable,
    voir _backfill_status) demande l'arrêt. Tourne dans un thread dédié (voir
    /api/telegram-channels/backfill/start) - jamais dans la requête HTTP elle-même, un
    historique de plusieurs milliers de messages prend largement plus longtemps qu'un
    timeout HTTP raisonnable.

    Best-effort par canal: l'échec d'un canal (FloodWaitError, canal supprimé...) passe au
    suivant plutôt que d'arrêter tout le backfill - state persisté après CHAQUE morceau,
    une interruption (redémarrage du conteneur, arrêt demandé) ne perd donc jamais plus
    d'un morceau de progression."""
    state = _load_backfill_state()
    pending = [c for c in channels if not state.get(c, {}).get('done')]

    while pending and not stop_flag.get('stop'):
        channel = pending[0]
        channel_state = state.get(channel, {'oldest_message_id': None, 'total_indexed': 0, 'done': False})
        try:
            channel_title, files, last_id, exhausted = _run_async(
                _backfill_channel_chunk, api_id, api_hash, session_string, channel, channel_state['oldest_message_id']
            )
            summary = _store_scrape_results({channel: {'title': channel_title, 'files': files, 'error': None}})
            channel_state['oldest_message_id'] = last_id
            channel_state['total_indexed'] = channel_state.get('total_indexed', 0) + summary[channel]['new_count']
            channel_state['channel_title'] = channel_title
            channel_state['done'] = exhausted
            state[channel] = channel_state
            _save_backfill_state(state)
            print(f"📚 Backfill {channel}: +{summary[channel]['new_count']} fichier(s), "
                  f"{'terminé' if exhausted else 'en cours'} (dernier message_id vu: {last_id})")
            if exhausted:
                pending.pop(0)
        except Exception as e:
            # Canal en échec (supprimé, inaccessible, FloodWaitError persistant...): marqué
            # "done" pour ne pas boucler indéfiniment dessus, mais l'erreur reste visible
            # dans les logs container pour diagnostic.
            print(f"✗ Erreur backfill {channel}: {e}")
            channel_state['done'] = True
            channel_state['error'] = str(e)
            state[channel] = channel_state
            _save_backfill_state(state)
            pending.pop(0)
            continue
        time.sleep(3)

    stop_flag['running'] = False


def _store_scrape_results(results):
    """Insère les fichiers trouvés (scrape ou recherche, même forme de résultat) dans
    telegram_files.db - une recherche alimente donc le même cache que le scrape
    périodique, ses résultats deviennent téléchargeables via la même route /download et
    réapparaissent dans /latest comme n'importe quel fichier trouvé au scrape normal.
    Retourne {channel: {'title', 'new_count', 'error', 'files'}}."""
    from blueprints.search.routes import normalize_search_text

    conn = _connect_db()
    cursor = conn.cursor()
    # utcnow(), pas now(): comparé/affiché comme UTC partout ailleurs (message_date
    # juste au-dessus est explicitement .astimezone(timezone.utc), et le pendant EBDZ de
    # cette colonne, ed2k_links.date_scraped, est un CURRENT_TIMESTAMP SQLite - toujours
    # UTC quel que soit le fuseau du conteneur) - voir TZ=Europe/Paris, docker-compose.yml:
    # avec un conteneur en heure de Paris, now() aurait dérivé de 1-2h par rapport à ces
    # deux autres sources, cassant le tri chronologique unifié de Nouveautés (EBDZ +
    # Telegram) et le badge "non lu" (voir parseDbUtcDate, nav.js, qui suppose justement
    # que TOUTE date venant de la base est en UTC).
    scraped_at = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    summary = {}

    for channel, data in results.items():
        new_count = 0
        new_files = []
        for f in data['files']:
            cursor.execute('''
                INSERT OR IGNORE INTO telegram_files
                    (channel, channel_title, message_id, filename, file_size, message_date, scraped_at, filename_normalized, bedetheque_url_hint)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (channel, data['title'], f['message_id'], f['filename'], f['file_size'], f['message_date'], scraped_at,
                  normalize_search_text(f['filename']), f.get('bedetheque_url_hint')))
            if cursor.rowcount > 0:
                new_count += 1
                new_files.append(f)
        summary[channel] = {
            'title': data['title'], 'new_count': new_count, 'error': data['error'],
            'files': data['files'], 'new_files': new_files
        }

    conn.commit()
    conn.close()
    return summary


def scrape_channels(api_id, api_hash, session_string, channels, limit=50):
    """Scrape tous les canaux configurés (best-effort par canal: l'échec de l'un n'empêche
    pas les autres) et insère les nouveaux fichiers trouvés dans telegram_files.db.
    Retourne {channel: {'title', 'new_count', 'error'}} pour le compte-rendu à l'utilisateur."""
    results = _run_async(_scrape_all, api_id, api_hash, session_string, channels, limit)
    return _store_scrape_results(results)


def search_channels(api_id, api_hash, session_string, channels, query, limit=30):
    """Recherche plein texte dans TOUT l'historique des canaux configurés (pas seulement
    les derniers messages scrapés) - "la possibilité de faire une recherche". Les
    résultats sont aussi mis en cache dans telegram_files.db (voir _store_scrape_results)
    pour être immédiatement téléchargeables."""
    results = _run_async(_scrape_all, api_id, api_hash, session_string, channels, limit, search=query)
    return _store_scrape_results(results)


def get_downloaded_filenames():
    """Noms de fichiers connus de cette table (scrapés, téléchargés ou non) - utilisé par
    le scan d'import (blueprints/library/routes.py) pour attribuer le client 'telegram' aux
    fichiers déjà sur disque, en confirmation du répertoire dédié TELEGRAM_IMPORT_DIRECTORY
    ('/downloads/telegram', voir config.py).

    PAS filtré sur downloaded=1 ("pourquoi c'est amule" pour un fichier réellement
    téléchargé via Telegram, avant la séparation en répertoires dédiés par source - "create
    a download folder with the 3 options torrents amule telegram"): un téléchargement
    Telegram interrompu par un redémarrage du process juste avant la mise à jour de
    `downloaded` (déjà arrivé une fois, voir CLAUDE.md) laisse le fichier marqué
    downloaded=0 en base tout en étant réellement sur disque - la simple PRÉSENCE d'une
    ligne pour ce nom de fichier suffit à l'identifier comme venant de Telegram,
    `downloaded` ne concerne que le suivi de progression, pas
    l'origine du fichier."""
    conn = _connect_db()
    cursor = conn.cursor()
    cursor.execute('SELECT filename FROM telegram_files')
    names = {row[0] for row in cursor.fetchall()}
    conn.close()
    return names


def get_latest_files(days=15, limit=None):
    """Fichiers scrapés dans les `days` derniers jours, plus récents d'abord - même fenêtre
    glissante que GET /api/ebdz/latest (voir routes.py de ebdz), pour un rendu cohérent
    entre les deux sources sur la page Nouveautés.

    limit (optionnel): "limite toi à 100 articles pour l'instant et charge en background
    après" - un chargement initial rapide de la page Nouveautés n'a pas besoin d'attendre
    l'annotation (already_in_library/already_monitored, coûteuse par fichier - voir
    _annotate_already_in_library) de TOUT le contenu de la fenêtre `days` avant de
    afficher quoi que ce soit ; le frontend redemande ensuite la fenêtre complète sans
    limite en arrière-plan (voir loadNouveautesEvents, ebdz-latest.js)."""
    conn = _connect_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    sql = '''
        SELECT * FROM telegram_files
        WHERE message_date >= datetime('now', ?)
        ORDER BY message_date DESC
    '''
    params = [f'-{int(days)} days']
    if limit:
        sql += ' LIMIT ?'
        params.append(int(limit))
    cursor.execute(sql, params)
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


async def _download_one(api_id, api_hash, session_string, channel, message_id, target_dir, loop, progress_callback=None, on_flood_retry=None):
    client = TelegramClient(StringSession(session_string), api_id, api_hash, loop=loop)
    await client.connect()
    try:
        entity = await client.get_entity(channel)
        msg = await client.get_messages(entity, ids=message_id)
        if not msg or not msg.file:
            raise ValueError('Message introuvable ou sans fichier (peut-être supprimé depuis)')
        filename = os.path.basename(msg.file.name or f'telegram_{channel}_{message_id}')
        target_root = os.path.realpath(target_dir)
        target_path = os.path.realpath(os.path.join(target_root, filename))
        if not filename or os.path.commonpath((target_root, target_path)) != target_root:
            raise ValueError('Nom de fichier Telegram non autorisé')
        expected_size = getattr(msg.file, 'size', None)
        if expected_size and expected_size > 2 * 1024 * 1024 * 1024:
            raise ValueError('Fichier Telegram refusé: taille supérieure à 2 Gio')
        # Si le fichier de destination existe déjà (même nom), suffixer plutôt qu'écraser -
        # même convention que upload_series_file (routes.py de library)
        if os.path.exists(target_path):
            base, ext = os.path.splitext(filename)
            target_path = os.path.join(target_root, f'{base}_{message_id}{ext}')

        temp_path = target_path + '.part'
        # progress_callback: "import avec telegram essaie de monitorer le status de
        # telechargement" - Telethon accepte un callable synchrone (current, total),
        # appelé très fréquemment (par chunk reçu) ; le throttling (pour ne pas marteler
        # SQLite à chaque appel) est géré côté appelant (voir download_channel_file_background),
        # pas ici.
        # A large file is fetched through many GetFileRequest calls. Telegram
        # occasionally answers one of those chunks with FLOOD_PREMIUM_WAIT_N
        # (HTTP/RPC 420). Treat that as a short server-side pause, not as a
        # permanent download failure: discard the incomplete temporary archive,
        # wait the requested interval, and retry from a clean file. The retry is
        # deliberately limited so genuine authorization/network errors still
        # reach the caller and are recorded in Historique.
        flood_retries = 0
        while True:
            try:
                await client.download_media(msg, file=temp_path, progress_callback=progress_callback)
                break
            except Exception as exc:
                wait_seconds = _telegram_flood_wait_seconds(exc)
                if wait_seconds is None or flood_retries >= TELEGRAM_DOWNLOAD_FLOOD_RETRIES:
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                    raise
                flood_retries += 1
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
                wait_seconds = min(wait_seconds, TELEGRAM_DOWNLOAD_MAX_WAIT_SECONDS)
                print(
                    f"⏳ Telegram limite temporaire ({wait_seconds}s), "
                    f"nouvel essai {flood_retries}/{TELEGRAM_DOWNLOAD_FLOOD_RETRIES} "
                    f"pour {filename}"
                )
                # Battement de cœur (voir refresh_download_heartbeat, downloader.py) :
                # bytes_downloaded reste figé pendant toute cette séquence (le fichier
                # temporaire est repris de zéro à chaque tentative), sans ce signal
                # retry_stalled_telegram_downloads pouvait croire ce téléchargement mort
                # après 1 minute et en relancer un second en parallèle pour le même
                # message pendant que celui-ci travaille encore.
                if on_flood_retry:
                    try:
                        on_flood_retry()
                    except Exception:
                        pass
                await asyncio.sleep(wait_seconds)

        # Un fichier tronqué silencieusement en cours de route (déjà constaté à deux
        # reprises sur le même fichier lors de la récupération de la série "Le testament
        # du Capitaine Crown", et une troisième fois ici sur "Foudroyants" T02: 43 Mo reçus
        # sur 228 Mo attendus) n'est jamais remonté comme une erreur par download_media
        # lui-même - il rend juste la main une fois le flux terminé, sans lever
        # d'exception ni comparer à la taille annoncée par le message.
        actual_size = os.path.getsize(temp_path) if os.path.exists(temp_path) else 0
        if expected_size and actual_size != expected_size:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            raise ValueError(
                f"Téléchargement tronqué: {actual_size} octets reçus sur {expected_size} attendus"
            )

        # Renommage atomique (même filesystem, voir target_dir/target_path ci-dessus) vers
        # le nom final SEULEMENT une fois la taille vérifiée - c'est ce rename qui rend le
        # fichier visible au scanner d'import, jamais avant.
        os.rename(temp_path, target_path)
        return target_path
    finally:
        await client.disconnect()


def download_channel_file_background(api_id, api_hash, session_string, channel, message_id, target_dir, channel_title=None, app=None, pending_title=None, series_id=None, volume_id=None, volume_number=None, retry_count=0, force_replace=False):
    """Lance le téléchargement dans un thread dédié et retourne immédiatement (voir
    download_telegram_file dans routes.py) - un fichier de plusieurs centaines de Mo
    (constaté jusqu'à ~230 Mo sur ces canaux) prend de longues secondes à quelques minutes
    selon la bande passante ; le bloquer dans la requête HTTP empêcherait le reste de l'app
    de répondre pendant ce temps (threaded=True côté Flask, voir run.py).

    "when adding a new file whatever source. it should be automatically added to import.
    then you can poll to get its status" - contrairement à avant (aucun état intermédiaire,
    le fichier n'apparaissait qu'une fois le scan relancé une fois l'écriture terminée),
    une ligne "en attente" (active_downloads, voir mark_download_pending) est posée dès le
    lancement du thread et retirée à la fin (mark_download_completed/mark_download_failed)
    - pas de vraie progression en %, juste présence/absence (voir get_pending_downloads,
    consommé par /api/activity/status côté Import).

    app: objet Flask capturé par l'appelant (current_app._get_current_object(), voir
    routes.py) - nécessaire pour journaliser dans Historique via log_manual_download, qui
    lit current_app.config depuis CE thread, sans contexte de requête automatique (voir
    CLAUDE.md: un thread d'arrière-plan qui touche current_app sans app.app_context()
    explicite échoue silencieusement).

    series_id/volume_id: identité réelle connue si le téléchargement part d'une fiche série
    (voir mark_download_pending) - simplement transmis tels quels à la ligne active_downloads.

    retry_count: transmis par retry_stalled_telegram_downloads (downloader.py) lors d'une
    relance automatique d'un téléchargement bloqué - 0 pour un lancement normal."""
    def _log(filename, success, message='', tracking_id=None):
        if not app:
            return
        try:
            from blueprints.missing_monitor.downloader import log_manual_download
            source_link = f"https://t.me/{channel}/{message_id}" if channel and message_id else None
            with app.app_context():
                log_manual_download(filename, 'telegram', success, message, source='telegram', source_link=source_link, tracking_id=tracking_id)
        except Exception as e:
            print(f"Erreur journalisation téléchargement Telegram dans Historique: {e}")

    def run():
        download_id = None
        if app:
            try:
                from blueprints.missing_monitor.downloader import mark_download_pending, mark_download_completed, mark_download_failed
                with app.app_context():
                    download_id = mark_download_pending(
                        pending_title or channel_title or channel, 'telegram',
                        series_id=series_id, volume_id=volume_id, volume_number=volume_number,
                        channel=channel, message_id=message_id, retry_count=retry_count,
                        force_replace=force_replace
                    )
            except Exception as e:
                print(f"Erreur enregistrement téléchargement Telegram en attente: {e}")

        # Callback de progression Telethon ("import avec telegram essaie de monitorer le
        # status de telechargement") - appelé très fréquemment (par chunk reçu, potentiellement
        # des centaines de fois pour un gros fichier) : throttlé au pourcentage entier pour
        # ne pas marteler SQLite à chaque appel (voir update_download_progress), toujours
        # laissé passer le tout dernier appel (100%) explicitement. Fonction SYNCHRONE
        # (pas de coroutine) - Telethon accepte les deux, et _download_one/_run_async
        # tournent déjà sur le même thread que ce run() (pas un nouveau thread par appel),
        # donc pousser app.app_context() ici à chaque appel throttlé est sûr.
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
                print(f"Erreur mise à jour progression téléchargement Telegram: {e}")

        def on_flood_retry():
            if not app or not download_id:
                return
            try:
                from blueprints.missing_monitor.downloader import refresh_download_heartbeat
                with app.app_context():
                    refresh_download_heartbeat(download_id)
            except Exception as e:
                print(f"Erreur rafraîchissement téléchargement Telegram: {e}")

        try:
            os.makedirs(target_dir, exist_ok=True)
            target_path = _run_async(
                _download_one, api_id, api_hash, session_string, channel, message_id, target_dir,
                progress_callback=on_progress, on_flood_retry=on_flood_retry
            )
            filename = os.path.basename(target_path)

            conn = _connect_db()
            conn.execute('UPDATE telegram_files SET downloaded = 1 WHERE channel = ? AND message_id = ?', (channel, message_id))
            conn.commit()
            conn.close()
            _log(filename, True, f'Téléchargé depuis {channel_title or channel}', tracking_id=download_id)
            if app and download_id:
                with app.app_context():
                    mark_download_completed(download_id)
            if app:
                try:
                    from blueprints.library.routes import attempt_immediate_auto_import
                    with app.app_context():
                        attempt_immediate_auto_import(target_path, target_dir)
                except Exception as e:
                    print(f"Erreur import automatique immédiat après téléchargement Telegram: {e}")
        except Exception as e:
            # pending_title/channel_title: même repli que mark_download_pending plus haut
            # ("title") plutôt que le simple identifiant de canal - `filename` n'est pas
            # encore défini à ce stade (l'échec a eu lieu avant que le fichier ne soit
            # renommé/déplacé), donc le nom réel n'est pas encore connu.
            _log(pending_title or channel_title or channel, False, str(e), tracking_id=download_id)
            if app and download_id:
                with app.app_context():
                    mark_download_failed(download_id)

    threading.Thread(target=run, daemon=True).start()
