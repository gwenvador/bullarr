"""
Connexion "compte utilisateur" Telegram (MTProto, via Telethon) - pour pouvoir lire le
contenu de canaux publics comme @bd_frBE (item #16 de improvement.txt), ce que l'API Bot
utilisée par blueprints/telegram/ ne permet pas: un bot ne voit que les messages postés
APRÈS avoir été ajouté comme administrateur d'un canal, jamais son historique, et la
plupart des canaux publics de contenu n'ajoutent pas n'importe quel bot comme admin. Un
compte utilisateur normal, lui, peut rejoindre et lire l'historique complet de n'importe
quel canal public sans autorisation particulière - mais la connexion est un flux
interactif complet (numéro de téléphone -> code reçu par Telegram/SMS -> mot de passe 2FA
si activé), impossible à automatiser sans l'utilisateur au clavier.

Ce module fait UNIQUEMENT la connexion/déconnexion pour l'instant (identifiants +
session), pas encore le scraping des canaux eux-mêmes - la session Telegram obtenue ici
est le préalable nécessaire à cette suite.

Chaque appel HTTP est indépendant (Flask est synchrone, pas de boucle événementielle
asyncio persistante entre deux requêtes) - chaque étape du login reconnecte donc un
TelegramClient temporaire à partir de la StringSession de l'étape précédente plutôt que de
garder un client ouvert en mémoire entre les requêtes.
"""
import asyncio
import json
import os
import sqlite3
import threading
import time
from flask import request, jsonify, current_app
from . import telegram_channels_bp
from encryption import encrypt, decrypt

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    ApiIdInvalidError, PhoneNumberInvalidError, PhoneCodeInvalidError,
    PhoneCodeExpiredError, SessionPasswordNeededError, PasswordHashInvalidError,
    FloodWaitError, RPCError
)


def load_telegram_channels_config():
    """Charge la config (identifiants déchiffrés inclus sous *_decrypted, jamais renvoyés
    tels quels au frontend - voir telegram_channels_config() GET qui les masque)."""
    config_file = current_app.config['TELEGRAM_CHANNELS_CONFIG_FILE']
    if os.path.exists(config_file):
        with open(config_file, 'r') as f:
            cfg = json.load(f)
    else:
        cfg = current_app.config['TELEGRAM_CHANNELS_CONFIG'].copy()

    for field in ('api_hash', 'phone', 'session'):
        value = cfg.get(field)
        if value:
            decrypted = decrypt(value)
            if decrypted is not None:
                cfg[f'{field}_decrypted'] = decrypted
    return cfg


def save_telegram_channels_config(config):
    """Sauvegarde la config, chiffrant api_hash/phone/session avant écriture disque.
    Un champ n'est modifié que si sa clé '<champ>_decrypted' est PRÉSENTE dans `config`
    (même vide, ce qui le vide explicitement - voir logout()) - absente, la valeur déjà
    enregistrée (recopiée via to_save = config.copy()) reste inchangée."""
    config_file = current_app.config['TELEGRAM_CHANNELS_CONFIG_FILE']
    try:
        to_save = config.copy()
        for field in ('api_hash', 'phone', 'session'):
            decrypted_key = f'{field}_decrypted'
            if decrypted_key in to_save:
                value = to_save.pop(decrypted_key)
                to_save[field] = encrypt(value) if value else ''
        with open(config_file, 'w') as f:
            json.dump(to_save, f, indent=4)
        os.chmod(config_file, 0o600)
        return True
    except Exception as e:
        print(f"Erreur sauvegarde config Telegram (canaux): {e}")
        return False


@telegram_channels_bp.route('/config', methods=['GET', 'POST'])
def telegram_channels_config():
    if request.method == 'GET':
        config = load_telegram_channels_config()
        return jsonify({
            'api_id': config.get('api_id'),
            'api_hash': '****' if config.get('api_hash') else '',
            'phone': '****' if config.get('phone') else '',
            'connected': bool(config.get('connected')),
            'username': config.get('username', ''),
            'first_name': config.get('first_name', ''),
            'channels': config.get('channels', [])
        })

    data = request.get_json() or {}
    config = load_telegram_channels_config()

    if 'api_id' in data:
        try:
            config['api_id'] = int(data['api_id']) if data['api_id'] not in (None, '') else None
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'api_id invalide (doit être un nombre)'}), 400

    new_api_hash = data.get('api_hash', '')
    if new_api_hash and new_api_hash != '****':
        config['api_hash_decrypted'] = new_api_hash

    new_phone = data.get('phone', '')
    if new_phone and new_phone != '****':
        config['phone_decrypted'] = new_phone

    if save_telegram_channels_config(config):
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Erreur de sauvegarde'}), 500


# État temporaire d'une connexion en cours (entre l'envoi du code et sa validation, puis
# éventuellement le mot de passe 2FA) - un seul flux à la fois puisque cette app est
# mono-utilisateur, pas besoin d'un état par session HTTP/utilisateur. Écrasé à chaque
# nouvel appel à /login/start, jamais persisté sur disque tant que la connexion n'a pas
# pleinement abouti (voir _finalize_login).
_pending_login = {}
_PENDING_LOGIN_TTL = 600  # 10 min - au-delà, on considère l'utilisateur reparti sans finir


def _run_login_coro(coro_fn, *args):
    """coro_fn reçoit `loop` en dernier argument positionnel (Telethon veut la boucle
    explicitement au moment de construire le TelegramClient, pas seulement au run)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro_fn(*args, loop))
    finally:
        loop.close()


async def _send_code(api_id, api_hash, phone, loop):
    client = TelegramClient(StringSession(), api_id, api_hash, loop=loop)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        return client.session.save(), sent.phone_code_hash
    finally:
        await client.disconnect()


async def _submit_code(session_string, api_id, api_hash, phone, phone_code_hash, code, loop):
    client = TelegramClient(StringSession(session_string), api_id, api_hash, loop=loop)
    await client.connect()
    try:
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError:
            return {'needs_password': True, 'session_string': client.session.save()}
        me = await client.get_me()
        return {
            'needs_password': False,
            'session_string': client.session.save(),
            'username': me.username or '',
            'first_name': me.first_name or ''
        }
    finally:
        await client.disconnect()


async def _submit_password(session_string, api_id, api_hash, password, loop):
    client = TelegramClient(StringSession(session_string), api_id, api_hash, loop=loop)
    await client.connect()
    try:
        await client.sign_in(password=password)
        me = await client.get_me()
        return {
            'session_string': client.session.save(),
            'username': me.username or '',
            'first_name': me.first_name or ''
        }
    finally:
        await client.disconnect()


def _finalize_login(pending, result):
    """Connexion pleinement aboutie: la StringSession finale (équivalent d'un mot de
    passe complet - accès total au compte) est chiffrée et persistée. Efface l'état
    temporaire en mémoire."""
    global _pending_login
    config = load_telegram_channels_config()
    config['api_id'] = pending['api_id']
    config['api_hash_decrypted'] = pending['api_hash']
    config['phone_decrypted'] = pending['phone']
    config['session_decrypted'] = result['session_string']
    config['connected'] = True
    config['username'] = result['username']
    config['first_name'] = result['first_name']
    save_telegram_channels_config(config)
    _pending_login = {}


def _get_pending_login():
    if not _pending_login or time.time() - _pending_login.get('created_at', 0) > _PENDING_LOGIN_TTL:
        return None
    return _pending_login


@telegram_channels_bp.route('/login/start', methods=['POST'])
def login_start():
    """Étape 1: envoie le code de connexion au numéro fourni (via l'app Telegram ou SMS,
    au choix de Telegram selon le compte). api_id/api_hash/phone omis ou masqués ('****')
    retombent sur les valeurs déjà enregistrées, pour pouvoir relancer une connexion sans
    tout ressaisir."""
    global _pending_login
    data = request.get_json() or {}
    config = load_telegram_channels_config()

    raw_api_id = data.get('api_id')
    try:
        api_id = int(raw_api_id) if raw_api_id not in (None, '') else config.get('api_id')
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'api_id invalide (doit être un nombre)'}), 400

    api_hash = data.get('api_hash') or ''
    if not api_hash or api_hash == '****':
        api_hash = config.get('api_hash_decrypted')

    phone = data.get('phone') or ''
    if not phone or phone == '****':
        phone = config.get('phone_decrypted')

    if not api_id or not api_hash or not phone:
        return jsonify({'success': False, 'error': 'api_id, api_hash et numéro de téléphone requis'}), 400

    # Sauvegarde immédiate des identifiants saisis (avant même que la connexion aboutisse)
    # pour ne pas les perdre si l'utilisateur recharge la page en plein milieu du flux
    config['api_id'] = api_id
    config['api_hash_decrypted'] = api_hash
    config['phone_decrypted'] = phone
    save_telegram_channels_config(config)

    try:
        session_string, phone_code_hash = _run_login_coro(_send_code, api_id, api_hash, phone)
    except ApiIdInvalidError:
        return jsonify({'success': False, 'error': 'api_id / api_hash invalide(s)'}), 400
    except PhoneNumberInvalidError:
        return jsonify({'success': False, 'error': 'Numéro de téléphone invalide (format international requis, ex: +33612345678)'}), 400
    except FloodWaitError as e:
        return jsonify({'success': False, 'error': f'Trop de tentatives - réessayez dans {e.seconds}s'}), 429
    except RPCError as e:
        return jsonify({'success': False, 'error': str(e)}), 500

    _pending_login = {
        'session_string': session_string,
        'api_id': api_id,
        'api_hash': api_hash,
        'phone': phone,
        'phone_code_hash': phone_code_hash,
        'created_at': time.time()
    }
    return jsonify({'success': True})


@telegram_channels_bp.route('/login/submit-code', methods=['POST'])
def login_submit_code():
    """Étape 2: valide le code reçu. Peut se conclure directement (needs_password=False,
    connexion terminée) ou exiger le mot de passe 2FA du compte (needs_password=True,
    voir /login/submit-password)."""
    data = request.get_json() or {}
    code = (data.get('code') or '').strip()
    if not code:
        return jsonify({'success': False, 'error': 'Code requis'}), 400

    pending = _get_pending_login()
    if not pending:
        return jsonify({'success': False, 'error': 'Connexion expirée, relancez depuis le début'}), 400

    try:
        result = _run_login_coro(
            _submit_code, pending['session_string'], pending['api_id'], pending['api_hash'],
            pending['phone'], pending['phone_code_hash'], code
        )
    except PhoneCodeInvalidError:
        return jsonify({'success': False, 'error': 'Code incorrect'}), 400
    except PhoneCodeExpiredError:
        return jsonify({'success': False, 'error': 'Code expiré, relancez depuis le début'}), 400
    except FloodWaitError as e:
        return jsonify({'success': False, 'error': f'Trop de tentatives - réessayez dans {e.seconds}s'}), 429
    except RPCError as e:
        return jsonify({'success': False, 'error': str(e)}), 500

    pending['session_string'] = result['session_string']

    if result['needs_password']:
        return jsonify({'success': True, 'needs_password': True})

    _finalize_login(pending, result)
    return jsonify({'success': True, 'needs_password': False, 'username': result['username'], 'first_name': result['first_name']})


@telegram_channels_bp.route('/login/submit-password', methods=['POST'])
def login_submit_password():
    """Étape 3 (uniquement si l'étape 2 a répondu needs_password=True): mot de passe de
    vérification en deux étapes du compte Telegram lui-même (pas un code)."""
    data = request.get_json() or {}
    password = data.get('password') or ''
    if not password:
        return jsonify({'success': False, 'error': 'Mot de passe requis'}), 400

    pending = _get_pending_login()
    if not pending:
        return jsonify({'success': False, 'error': 'Connexion expirée, relancez depuis le début'}), 400

    try:
        result = _run_login_coro(
            _submit_password, pending['session_string'], pending['api_id'], pending['api_hash'], password
        )
    except PasswordHashInvalidError:
        return jsonify({'success': False, 'error': 'Mot de passe incorrect'}), 400
    except FloodWaitError as e:
        return jsonify({'success': False, 'error': f'Trop de tentatives - réessayez dans {e.seconds}s'}), 429
    except RPCError as e:
        return jsonify({'success': False, 'error': str(e)}), 500

    _finalize_login(pending, result)
    return jsonify({'success': True, 'username': result['username'], 'first_name': result['first_name']})


@telegram_channels_bp.route('/logout', methods=['POST'])
def logout():
    """Efface uniquement la session (accès au compte) - api_id/api_hash/phone restent
    enregistrés pour pouvoir se reconnecter sans tout ressaisir."""
    global _pending_login
    config = load_telegram_channels_config()
    config['session_decrypted'] = ''
    config['connected'] = False
    config['username'] = ''
    config['first_name'] = ''
    if not save_telegram_channels_config(config):
        return jsonify({'success': False, 'error': 'Erreur de sauvegarde'}), 500
    _pending_login = {}
    return jsonify({'success': True})


def _require_connected_config():
    """Config + identifiants déchiffrés si une session est active, sinon None - évite de
    répéter la même vérification dans chaque route qui a besoin d'un client Telethon
    authentifié (scrape/download ci-dessous)."""
    config = load_telegram_channels_config()
    if not config.get('connected') or not config.get('session_decrypted'):
        return None
    return config


@telegram_channels_bp.route('/channels', methods=['POST'])
def add_channel():
    """Ajoute un canal à surveiller ("integre le channel @bd_frBE et N_art_BD_FRBE... a
    ajouter dans les indexeurs") - vérifie que le canal existe et est accessible avant de
    l'enregistrer (via get_entity), pour ne pas stocker un nom de canal invalide/mal
    orthographié qui échouerait silencieusement à chaque scrape ensuite."""
    config = _require_connected_config()
    if not config:
        return jsonify({'success': False, 'error': 'Non connecté à Telegram'}), 400

    data = request.get_json() or {}
    channel = (data.get('channel') or '').strip().lstrip('@')
    if not channel:
        return jsonify({'success': False, 'error': 'Nom de canal requis'}), 400

    channels = config.get('channels') or []
    if any(c['username'] == channel for c in channels):
        return jsonify({'success': False, 'error': 'Ce canal est déjà dans la liste'}), 400

    try:
        title = _run_login_coro(_check_channel_exists, config['api_id'], config['api_hash_decrypted'],
                                 config['session_decrypted'], channel)
    except RPCError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': f'Canal introuvable ou inaccessible: {e}'}), 400

    channels.append({'username': channel, 'title': title})
    config['channels'] = channels
    if not save_telegram_channels_config(config):
        return jsonify({'success': False, 'error': 'Erreur de sauvegarde'}), 500
    return jsonify({'success': True, 'channel': {'username': channel, 'title': title}})


async def _check_channel_exists(api_id, api_hash, session_string, channel, loop):
    client = TelegramClient(StringSession(session_string), api_id, api_hash, loop=loop)
    await client.connect()
    try:
        entity = await client.get_entity(channel)
        return getattr(entity, 'title', channel)
    finally:
        await client.disconnect()


@telegram_channels_bp.route('/channels/<channel>', methods=['DELETE'])
def remove_channel(channel):
    config = load_telegram_channels_config()
    channels = [c for c in (config.get('channels') or []) if c['username'] != channel]
    config['channels'] = channels
    if not save_telegram_channels_config(config):
        return jsonify({'success': False, 'error': 'Erreur de sauvegarde'}), 500
    return jsonify({'success': True})


@telegram_channels_bp.route('/scrape', methods=['POST'])
def scrape():
    """Scrape tous les canaux configurés pour de nouveaux fichiers - déclenchement manuel
    (voir bouton "Scraper" sur la page Nouveautés), même principe que le scrape EBDZ."""
    from .scraper import scrape_channels

    config = _require_connected_config()
    if not config:
        return jsonify({'success': False, 'error': 'Non connecté à Telegram'}), 400

    channels = [c['username'] for c in (config.get('channels') or [])]
    if not channels:
        return jsonify({'success': False, 'error': 'Aucun canal configuré'}), 400

    try:
        summary = scrape_channels(config['api_id'], config['api_hash_decrypted'], config['session_decrypted'], channels)
    except FloodWaitError as e:
        return jsonify({'success': False, 'error': f'Trop de requêtes - réessayez dans {e.seconds}s'}), 429
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

    return jsonify({'success': True, 'summary': summary})


_backfill_thread_state = {'running': False, 'stop': False}


@telegram_channels_bp.route('/backfill/start', methods=['POST'])
def backfill_start():
    """Démarre le backfill de l'historique complet des canaux configurés (voir
    run_backfill_background/scraper.py) dans un thread dédié - un historique de plusieurs
    milliers de messages dépasse largement un timeout HTTP raisonnable, cette requête
    répond donc immédiatement et la progression se consulte via /backfill/status."""
    from .scraper import run_backfill_background

    if _backfill_thread_state['running']:
        return jsonify({'success': False, 'error': 'Un backfill est déjà en cours'}), 409

    config = _require_connected_config()
    if not config:
        return jsonify({'success': False, 'error': 'Non connecté à Telegram'}), 400

    channels = [c['username'] for c in (config.get('channels') or [])]
    if not channels:
        return jsonify({'success': False, 'error': 'Aucun canal configuré'}), 400

    _backfill_thread_state['running'] = True
    _backfill_thread_state['stop'] = False

    def _run():
        try:
            run_backfill_background(
                config['api_id'], config['api_hash_decrypted'], config['session_decrypted'],
                channels, _backfill_thread_state
            )
        finally:
            _backfill_thread_state['running'] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'success': True})


@telegram_channels_bp.route('/backfill/stop', methods=['POST'])
def backfill_stop():
    """Demande l'arrêt du backfill en cours (best-effort: le morceau déjà en train de se
    télécharger se termine, voir run_backfill_background - state déjà persisté après
    chaque morceau, rien n'est perdu en s'arrêtant entre deux)."""
    _backfill_thread_state['stop'] = True
    return jsonify({'success': True})


@telegram_channels_bp.route('/backfill/status', methods=['GET'])
def backfill_status():
    """État du backfill par canal (voir BACKFILL_STATE_FILE/scraper.py) + si un backfill
    tourne actuellement dans ce process - pollé par l'UI Configuration pendant qu'un
    backfill est en cours."""
    from .scraper import _load_backfill_state

    return jsonify({'success': True, 'running': _backfill_thread_state['running'], 'channels': _load_backfill_state()})


@telegram_channels_bp.route('/auto-scrape/config', methods=['GET', 'POST'])
def auto_scrape_config():
    """Configuration du scraping automatique des canaux Telegram - même forme que
    GET/POST /api/ebdz/auto-scrape/config, restreint à hours/days (voir scheduler.py)."""
    if request.method == 'GET':
        config = load_telegram_channels_config()
        return jsonify({
            'auto_scrape_enabled': config.get('auto_scrape_enabled', False),
            'auto_scrape_interval': config.get('auto_scrape_interval', 6),
            'auto_scrape_interval_unit': config.get('auto_scrape_interval_unit', 'hours')
        })

    data = request.get_json() or {}
    config = load_telegram_channels_config()

    enabled = bool(data.get('auto_scrape_enabled', False))
    try:
        interval = int(data.get('auto_scrape_interval', 6))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Intervalle invalide'}), 400
    interval_unit = data.get('auto_scrape_interval_unit', 'hours')

    if interval < 1:
        return jsonify({'success': False, 'error': "L'intervalle doit être >= 1"}), 400
    if interval_unit not in ('hours', 'days'):
        return jsonify({'success': False, 'error': 'Unité de temps invalide'}), 400

    config['auto_scrape_enabled'] = enabled
    config['auto_scrape_interval'] = interval
    config['auto_scrape_interval_unit'] = interval_unit

    if not save_telegram_channels_config(config):
        return jsonify({'success': False, 'error': 'Erreur de sauvegarde'}), 500

    from .scheduler import telegram_channels_scheduler
    if enabled:
        telegram_channels_scheduler.add_job(interval, interval_unit)
    else:
        telegram_channels_scheduler.remove_job()

    return jsonify({'success': True})


@telegram_channels_bp.route('/auto-scrape/status', methods=['GET'])
def auto_scrape_status():
    """Statut du scheduler Telegram (canaux) - même forme que GET
    /api/ebdz/auto-scrape/status."""
    from .scheduler import telegram_channels_scheduler

    is_running = False
    next_run = None
    if telegram_channels_scheduler.scheduler and telegram_channels_scheduler.scheduler.running:
        is_running = True
        job = telegram_channels_scheduler.scheduler.get_job(telegram_channels_scheduler.job_id)
        if job:
            next_run = job.next_run_time.isoformat() if job.next_run_time else None

    return jsonify({'success': True, 'is_running': is_running, 'next_run': next_run})


def _annotate_already_in_library(files):
    """Ajoute parsed_title/already_in_library à chaque fichier (scrape ou recherche) - même
    matching tolérant que l'auto-import (voir _match_series_for_auto_import dans
    blueprints.library.routes) plutôt qu'une simple égalité de titre, pour rester cohérent
    avec le badge "Possédé" affiché ailleurs dans l'app (EBDZ, Découvrir...). Ajoute aussi
    already_owned (True/False/None, voir volume_possession_status) - le tome/intégrale/HS/
    épisode précis identifié dans CE fichier est-il déjà possédé dans la série matchée
    ("compares les volumes existants avec ceux nouveau et si ceux de nouveautés sont
    manquants ou non de la série"), pas seulement "la série existe-t-elle"."""
    from blueprints.library.routes import (
        get_db_connection, _normalize_title_for_match, _match_series_for_auto_import,
        get_owned_volume_signatures, volume_possession_status
    )
    from blueprints.library.scanner import LibraryScanner

    scanner = LibraryScanner()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT s.id, s.library_id, l.path, s.title, s.is_oneshot, s.bedetheque_url
        FROM series s JOIN libraries l ON s.library_id = l.id
    ''')
    all_series = cursor.fetchall()
    series_by_id = {row[0]: row for row in all_series}
    owned_by_series = get_owned_volume_signatures([row[0] for row in all_series], conn=conn)
    cursor.execute('SELECT series_id FROM missing_volume_monitor WHERE enabled = 1')
    monitored_series_ids = {row[0] for row in cursor.fetchall()}
    cursor.execute('SELECT normalized_title, series_id FROM telegram_title_overrides')
    title_overrides = {row[0]: row[1] for row in cursor.fetchall() if row[1] in series_by_id}
    conn.close()

    # "dans nouveautés telegram et ebdz non pas le meme format. pas les memes infos sur
    # les volumes dans les 2" - même helper que EBDZ/Prowlarr (voir
    # MissingVolumeSearcher._parsed_volume_label, "dans le resultats des sources il ne
    # parse pas les volumes") pour que la page Nouveautés (static/js/ebdz-latest.js)
    # affiche un tome/intégrale/HS/épisode cohérent avec le reste de l'app, plutôt qu'une
    # ligne Telegram sans la moindre info de tome alors qu'un sujet EBDZ l'affiche déjà
    # (une fois déplié) pour chacun de ses fichiers.
    from blueprints.missing_monitor.searcher import MissingVolumeSearcher

    for f in files:
        parsed = scanner.parse_filename(f['filename'])
        normalized = _normalize_title_for_match(parsed.get('title', ''))
        override_series_id = title_overrides.get(normalized)
        match = series_by_id[override_series_id] if override_series_id else _match_series_for_auto_import(normalized, all_series)
        f['match_overridden'] = bool(override_series_id)
        # "in telegram if there is a bedetheque link use it in the nouveautes serie
        # column" - un lien bedetheque.com extrait de la légende du message (voir
        # bedetheque_url_hint, blueprints/telegram_channels/scraper.py) identifie la
        # série de façon bien plus fiable que le matching flou par titre ci-dessus -
        # retrouve ici une série que ce matching aurait manquée (article déplacé,
        # orthographe différente...) dès que son bedetheque_url correspond exactement.
        bedetheque_hint = (f.get('bedetheque_url_hint') or '').strip().rstrip('/') or None
        if bedetheque_hint and not match:
            match = next(
                (row for row in all_series if (row[5] or '').strip().rstrip('/') == bedetheque_hint),
                None
            )
        f['parsed_title'] = parsed.get('title')
        f['parsed_volume'] = MissingVolumeSearcher._parsed_volume_label(f['filename'])
        f['volume'] = parsed.get('volume')
        f['is_integral'] = parsed.get('is_integral')
        f['integral_number'] = parsed.get('integral_number')
        f['is_hs'] = parsed.get('is_hs')
        f['hs_number'] = parsed.get('hs_number')
        f['is_episode'] = parsed.get('is_episode')
        f['episode_number'] = parsed.get('episode_number')
        f['already_in_library'] = bool(match)
        # "clique sur la série pour accéder à la page... lien vers ebdz / bedetheque et dans
        # l'application page série" - même besoin que côté EBDZ (voir matched_series_id/
        # matched_bedetheque_url dans blueprints/ebdz/routes.py), pour que la page Nouveautés
        # puisse construire un lien direct vers la fiche série et sa page Bédéthèque.
        f['series_id'] = match[0] if match else None
        f['series_title'] = match[3] if match else None
        # Le lien de la légende prime sur celui de la série matchée (identique en
        # pratique quand la série a été trouvée via ce lien) et reste disponible même
        # sans série connue en bibliothèque (voir _nouveautesMatchedHtml, ebdz-latest.js).
        f['bedetheque_url'] = f.get('bedetheque_url_hint') or (match[5] if match else None)
        f['already_monitored'] = bool(match and match[0] in monitored_series_ids)
        f['already_owned'] = (
            volume_possession_status(
                owned_by_series[match[0]], volume=parsed.get('volume'),
                is_integral=parsed.get('is_integral', False), integral_number=parsed.get('integral_number'),
                is_hs=parsed.get('is_hs', False), hs_number=parsed.get('hs_number'),
                is_episode=parsed.get('is_episode', False), episode_number=parsed.get('episode_number')
            ) if match else None
        )
    return files


@telegram_channels_bp.route('/match-override', methods=['POST'])
def set_match_override():
    ""
    from blueprints.library.routes import get_db_connection, _normalize_title_for_match
    from blueprints.library.scanner import LibraryScanner

    data = request.get_json(silent=True) or {}
    filename = (data.get('filename') or '').strip()
    series_id = data.get('series_id')

    if not filename:
        return jsonify({'success': False, 'error': 'filename requis'}), 400

    parsed = LibraryScanner.parse_filename(filename)
    normalized = _normalize_title_for_match(parsed.get('title', ''))
    if not normalized:
        return jsonify({'success': False, 'error': "Impossible d'extraire un titre de ce nom de fichier"}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if series_id:
            cursor.execute('SELECT id, title FROM series WHERE id = ?', (series_id,))
            series_row = cursor.fetchone()
            if not series_row:
                return jsonify({'success': False, 'error': 'Série introuvable'}), 404
            cursor.execute('''
                INSERT INTO telegram_title_overrides (normalized_title, series_id)
                VALUES (?, ?)
                ON CONFLICT(normalized_title) DO UPDATE SET series_id = excluded.series_id
            ''', (normalized, series_id))
            conn.commit()
            return jsonify({'success': True, 'series_id': series_id, 'series_title': series_row[1]})
        else:
            cursor.execute('DELETE FROM telegram_title_overrides WHERE normalized_title = ?', (normalized,))
            conn.commit()
            return jsonify({'success': True, 'series_id': None})
    finally:
        conn.close()


@telegram_channels_bp.route('/latest', methods=['GET'])
def latest():
    """Fichiers scrapés récents, pour la page Nouveautés - même fenêtre `days` que
    GET /api/ebdz/latest. `?limit=N` (voir get_latest_files) pour un premier chargement
    rapide côté frontend, avant un second appel sans limite en arrière-plan."""
    from .scraper import get_latest_files

    days = request.args.get('days', 15)
    limit = request.args.get('limit', type=int)
    files = get_latest_files(days=days, limit=limit)
    return jsonify({'success': True, 'files': _annotate_already_in_library(files)})


def search_telegram_files_local(query, limit=100):
    ""
    from .scraper import TELEGRAM_FILES_DB
    from blueprints.search.routes import normalize_search_text
    from blueprints.missing_monitor.searcher import MissingVolumeSearcher
    from blueprints.library.scanner import LibraryScanner

    query = (query or '').strip()
    if not query:
        return []

    words = [w for w in normalize_search_text(query).split() if len(w) >= 2]
    if not words:
        return []

    conn = sqlite3.connect(TELEGRAM_FILES_DB, timeout=30.0)
    cursor = conn.cursor()
    # Trigramme: nécessite au moins 3 caractères pour matcher quoi que ce soit - les mots
    # de 2 caractères (rare, surtout des restes d'article après élision) sont vérifiés
    # séparément ci-dessous (post-filtre Python sur filename_normalized, déjà précalculé
    # donc gratuit) plutôt que d'affaiblir l'index FTS pour eux.
    fts_words = [w for w in words if len(w) >= 3]
    # Sur-échantillonne (5x) avant le post-filtre ci-dessous: seuls les mots de 2
    # caractères ne sont pas déjà garantis par la requête FTS, la marge couvre ce cas rare
    # sans risquer de perdre des résultats légitimes en dessous de `limit`.
    fetch_limit = min(max(limit * 5, limit), 2000)
    if fts_words:
        fts_query = 'filename_normalized: ' + ' AND '.join(
            '"' + w.replace('"', '""') + '"' for w in fts_words
        )
        cursor.execute('''
            SELECT tf.channel, tf.channel_title, tf.message_id, tf.filename, tf.file_size,
                   tf.message_date, tf.filename_normalized
            FROM telegram_files_fts
            JOIN telegram_files tf ON tf.id = telegram_files_fts.rowid
            WHERE telegram_files_fts MATCH ?
            ORDER BY tf.message_date DESC
            LIMIT ?
        ''', (fts_query, fetch_limit))
    else:
        cursor.execute('''
            SELECT channel, channel_title, message_id, filename, file_size, message_date,
                   filename_normalized
            FROM telegram_files
            WHERE ''' + ' AND '.join(['filename_normalized LIKE ?'] * len(words)) + '''
            ORDER BY message_date DESC
            LIMIT ?
        ''', [f'%{w}%' for w in words] + [fetch_limit])
    rows = cursor.fetchall()
    conn.close()

    files = []
    for channel, channel_title, message_id, filename, file_size, message_date, filename_normalized in rows:
        if not all(w in (filename_normalized or '') for w in words):
            continue
        if len(files) >= limit:
            break
        # Numéro de tome brut ("pourquoi nordheim ca na pas bien matcher les volumes") -
        # même parsing que EBDZ (voir 'volume'/'is_integral'/etc. dans /api/search côté
        # search/routes.py) - un numéro exploitable par le frontend pour taguer
        # automatiquement CE résultat précis (voir search-results-table.js,
        # trackingVolumeNumber) plutôt que seulement un libellé d'affichage.
        parsed = LibraryScanner.parse_filename(filename)
        files.append({
            'channel': channel,
            'channel_title': channel_title,
            'message_id': message_id,
            'filename': filename,
            'file_size': file_size,
            'message_date': message_date,
            'parsed_volume': MissingVolumeSearcher._parsed_volume_label(filename),
            'volume': parsed['volume'],
            'is_integral': parsed['is_integral'],
            'integral_number': parsed['integral_number'],
            'is_hs': parsed['is_hs'],
            'hs_number': parsed['hs_number'],
            'resolution': parsed['resolution'],
        })
    return files


@telegram_channels_bp.route('/search', methods=['GET'])
def search():
    """Recherche dans l'historique des canaux configurés ("la possibilité de faire une
    recherche") - contrairement à /latest, ne se limite pas aux derniers messages scrapés.
    Voir Recherche (static/js/search.js) pour l'intégration frontend, aux côtés d'EBDZ/
    Prowlarr. Coeur de recherche: voir search_telegram_files_local ci-dessus."""
    query = (request.args.get('q') or '').strip()
    if not query:
        return jsonify({'success': False, 'error': 'Requête de recherche requise'}), 400

    files = search_telegram_files_local(query)

    # "pas besoin de already in library. c'est jamais demandé dans la recherche des
    # volumes" - _annotate_already_in_library rematche CHAQUE résultat contre TOUTE la
    # bibliothèque (get_owned_volume_signatures + _match_series_for_auto_import par
    # ligne) pour poser already_in_library/already_owned/parsed_title, des champs que
    # search.js (page Recherche) ne lit jamais - contrairement à /latest (Nouveautés,
    # voir plus bas) qui EN A besoin pour son badge "Possédé". Elle re-parsait aussi
    # volume/is_integral/... en double, écrasant ceux déjà posés juste au-dessus.
    return jsonify({'success': True, 'files': files})


@telegram_channels_bp.route('/download', methods=['POST'])
def download_telegram_file():
    """Démarre le téléchargement d'un fichier repéré au scrape dans son propre répertoire
    d'import dédié (TELEGRAM_IMPORT_DIRECTORY, '/downloads/telegram' - voir "create a
    download folder with the 3 options torrents amule telegram") - il y suit
    ensuite exactement le même chemin que n'importe quel fichier aMule/torrent (scan,
    matching auto ou assignation manuelle sur /import), aucune logique d'import dupliquée
    ici. Retourne IMMÉDIATEMENT (le téléchargement tourne dans un thread dédié, voir
    scraper.download_channel_file_background) - sa progression en direct est ensuite
    consultable sur la page Import ("ajoute telegram download dans téléchargement", voir
    blueprints/activity/routes.py)."""
    from .scraper import download_channel_file_background

    config = _require_connected_config()
    if not config:
        return jsonify({'success': False, 'error': 'Non connecté à Telegram'}), 400

    data = request.get_json() or {}
    channel = data.get('channel')
    message_id = data.get('message_id')
    channel_title = data.get('channel_title')
    # filename: connu du frontend au moment du clic (résultat déjà scrapé, voir
    # ebdz-latest.js/search-results-table.js) - sert de titre à la ligne "en attente" sur
    # /import tout de suite, avant que le téléchargement n'ait résolu le vrai nom de
    # fichier auprès de Telegram (voir mark_download_pending côté scraper.py).
    filename = data.get('filename')
    # Connus seulement si l'ajout part d'une fiche série (voir searchMissingVolume côté
    # library.js) - None en recherche libre (Nouveautés/ebdz-latest.js), voir
    # mark_download_pending.
    series_id = data.get('series_id')
    volume_id = data.get('volume_id')
    volume_number = data.get('volume_number')
    force_replace = bool(data.get('force_replace'))
    if not channel or not message_id:
        return jsonify({'success': False, 'error': 'channel et message_id requis'}), 400

    target_dir = current_app.config.get('TELEGRAM_IMPORT_DIRECTORY')
    if not target_dir:
        return jsonify({'success': False, 'error': "Répertoire d'import Telegram non configuré"}), 500

    # _get_current_object(): le thread d'arrière-plan (voir scraper.py) a besoin de
    # l'objet Flask réel pour pousser son propre app.app_context() et journaliser dans
    # Historique (log_manual_download lit current_app.config) - current_app lui-même est
    # un proxy lié à CETTE requête, invalide une fois sortie de son contexte.
    app = current_app._get_current_object()
    download_channel_file_background(
        config['api_id'], config['api_hash_decrypted'], config['session_decrypted'],
        channel, message_id, target_dir, channel_title=channel_title, app=app,
        pending_title=filename or channel_title or channel,
        series_id=series_id, volume_id=volume_id, volume_number=volume_number,
        force_replace=force_replace
    )

    return jsonify({'success': True, 'started': True})


@telegram_channels_bp.route('/retry/<int:download_id>', methods=['POST'])
def retry_telegram_download(download_id):
    """Relance manuelle d'un téléchargement Telegram ayant épuisé son budget de
    tentatives automatique (voir TELEGRAM_MAX_RETRY_ATTEMPTS et
    retry_stalled_telegram_downloads, downloader.py) - "3-retry budget ? do something
    if this exceed the amount. like manual retry in import": au-delà de ce quota le
    système ne retente plus jamais tout seul, la ligne reste affichée "⚠️ Introuvable"
    sur /import jusqu'à ce qu'un humain décide de réessayer explicitement. Repart
    volontairement avec retry_count=0 (voir mark_download_pending) - l'échec précédent
    peut avoir une cause déjà résolue entre-temps (connexion Telegram relancée, etc.),
    donc on lui redonne un budget complet plutôt que de le faire échouer après 0 tentative
    supplémentaire."""
    from .scraper import download_channel_file_background

    db_path = current_app.config.get('DATABASE')
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, title, channel, message_id, series_id, volume_id, volume_number "
        "FROM active_downloads WHERE id = ? AND client = 'telegram' AND status = 'pending'",
        (download_id,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Téléchargement introuvable'}), 404
    row = dict(row)
    if not row['channel'] or not row['message_id']:
        conn.close()
        return jsonify({'success': False, 'error': 'Téléchargement non relançable (informations manquantes)'}), 400

    config = _require_connected_config()
    if not config:
        conn.close()
        return jsonify({'success': False, 'error': 'Non connecté à Telegram'}), 400

    target_dir = current_app.config.get('TELEGRAM_IMPORT_DIRECTORY')
    if not target_dir:
        conn.close()
        return jsonify({'success': False, 'error': "Répertoire d'import Telegram non configuré"}), 500

    conn.execute('DELETE FROM active_downloads WHERE id = ?', (download_id,))
    conn.commit()
    conn.close()

    app = current_app._get_current_object()
    download_channel_file_background(
        config['api_id'], config['api_hash_decrypted'], config['session_decrypted'],
        row['channel'], row['message_id'], target_dir, app=app,
        pending_title=row['title'],
        series_id=row['series_id'], volume_id=row['volume_id'], volume_number=row['volume_number'],
        retry_count=0
    )

    return jsonify({'success': True, 'started': True})
