""
from flask import jsonify, redirect, current_app, request
from . import activity_bp
import subprocess
import re
import requests
import xmlrpc.client
import threading
import time
from concurrent.futures import ThreadPoolExecutor


@activity_bp.route('/activity')
def activity_page():
    """Ancienne page dédiée "Téléchargements", fusionnée dans /import - conservée en
    redirection pour les liens/favoris existants plutôt qu'un 404."""
    return redirect('/import')


_qbittorrent_session_cache = {'session': None, 'base_url': None}


def _qbittorrent_status():
    from blueprints.qbittorrent.routes import load_qbittorrent_config, create_qbittorrent_session

    config = load_qbittorrent_config()
    if not config.get('enabled'):
        return None  # Non configuré: absent du résultat plutôt qu'une erreur affichée

    session = _qbittorrent_session_cache['session']
    base_url = _qbittorrent_session_cache['base_url']

    if session is None:
        session, base_url, error = create_qbittorrent_session(config)
        if error or not session:
            return {'client': 'qbittorrent', 'label': 'qBittorrent', 'error': error or 'Connexion impossible', 'items': []}
        _qbittorrent_session_cache['session'] = session
        _qbittorrent_session_cache['base_url'] = base_url

    try:
        response = session.get(f"{base_url}/api/v2/torrents/info", timeout=8, verify=False)
        if response.status_code in (401, 403):
            # Session expirée côté qBittorrent - reconnecter une seule fois avant d'abandonner
            _qbittorrent_session_cache['session'] = None
            session, base_url, error = create_qbittorrent_session(config)
            if error or not session:
                return {'client': 'qbittorrent', 'label': 'qBittorrent', 'error': error or 'Connexion impossible', 'items': []}
            _qbittorrent_session_cache['session'] = session
            _qbittorrent_session_cache['base_url'] = base_url
            response = session.get(f"{base_url}/api/v2/torrents/info", timeout=8, verify=False)
        response.raise_for_status()
        torrents = response.json()
    except Exception as e:
        _qbittorrent_session_cache['session'] = None  # forcer une reconnexion propre au prochain appel
        return {'client': 'qbittorrent', 'label': 'qBittorrent', 'error': str(e)[:200], 'items': []}

    items = [{
        'id': t.get('hash'),  # requis par POST /api/qbittorrent/remove
        'name': t.get('name', ''),
        'progress': round((t.get('progress') or 0) * 100, 1),
        'state': t.get('state', ''),
        'size_bytes': t.get('size', 0),
        'downloaded_bytes': t.get('completed', 0),
        'speed_bytes_s': t.get('dlspeed', 0),
        'eta_seconds': t.get('eta') if (t.get('eta') or 0) < 8640000 else None,
    } for t in torrents]
    return {'client': 'qbittorrent', 'label': 'qBittorrent', 'error': None, 'items': items}


def _rtorrent_status():
    from blueprints.rtorrent.routes import load_rtorrent_config, _rtorrent_proxy, RtorrentError

    config = load_rtorrent_config()
    if not config.get('enabled'):
        return None

    try:
        proxy = _rtorrent_proxy(config)
        # d.multicall2 (API rTorrent récente): "" = toutes les vues, "main" = vue par
        # défaut (tous les téléchargements, actifs ou non). Champs demandés dans l'ordre:
        # hash (requis par POST /api/rtorrent/remove, d.erase le prend en identifiant),
        # nom, octets déjà téléchargés, taille totale, vitesse de téléchargement, terminé?
        rows = proxy.d.multicall2(
            '', 'main', 'd.hash=', 'd.name=', 'd.bytes_done=', 'd.size_bytes=', 'd.down.rate=', 'd.complete='
        )
    except (xmlrpc.client.Fault, xmlrpc.client.ProtocolError, OSError, RtorrentError) as e:
        return {'client': 'rtorrent', 'label': 'rTorrent', 'error': str(e)[:200], 'items': []}
    except Exception as e:
        return {'client': 'rtorrent', 'label': 'rTorrent', 'error': str(e)[:200], 'items': []}

    items = []
    for torrent_hash, name, done, size, rate, complete in rows:
        progress = round((done / size) * 100, 1) if size else 0
        items.append({
            'id': torrent_hash,
            'name': name,
            'progress': progress,
            'state': 'Terminé' if complete else ('En téléchargement' if rate else 'En pause/inactif'),
            'size_bytes': size,
            'downloaded_bytes': done,
            'speed_bytes_s': rate,
            'eta_seconds': round((size - done) / rate) if rate else None,
        })
    return {'client': 'rtorrent', 'label': 'rTorrent', 'error': None, 'items': items}


def _deluge_status():
    from blueprints.deluge.routes import load_deluge_config, _deluge_login, _deluge_rpc, DelugeError

    config = load_deluge_config()
    if not config.get('enabled'):
        return None

    try:
        session, base_url = _deluge_login(config)
        result = _deluge_rpc(session, base_url, 'core.get_torrents_status', [
            {}, ['name', 'progress', 'state', 'download_payload_rate', 'eta', 'total_size']
        ])
    except DelugeError as e:
        return {'client': 'deluge', 'label': 'Deluge', 'error': str(e)[:200], 'items': []}
    except Exception as e:
        return {'client': 'deluge', 'label': 'Deluge', 'error': str(e)[:200], 'items': []}

    items = []
    for torrent_id, torrent in (result or {}).items():
        progress = torrent.get('progress') or 0  # déjà en 0-100 côté Deluge
        size = torrent.get('total_size') or 0
        items.append({
            'id': torrent_id,  # requis par POST /api/deluge/remove
            'name': torrent.get('name', ''),
            'progress': round(progress, 1),
            'state': torrent.get('state', ''),
            'size_bytes': size,
            'downloaded_bytes': round(size * progress / 100) if size else 0,
            'speed_bytes_s': torrent.get('download_payload_rate', 0),
            'eta_seconds': torrent.get('eta') or None,
        })
    return {'client': 'deluge', 'label': 'Deluge', 'error': None, 'items': items}


# "> HASH nom_du_fichier" puis, sur la ligne suivante, "> [xx.x%] downloaded/total - État
# - partfile - Priorité [Hi/Lo]" (voir `amulecmd -c "show dl"`, format texte fixe de
# amulecmd - aMule n'expose pas d'API HTTP simple contrairement aux clients torrent
# ci-dessus, seule cette CLI est disponible, déjà utilisée pour l'ajout de lien dans
# add_to_emule() de blueprints/emule/routes.py)
_AMULE_NAME_RE = re.compile(r'^\s*>\s+([0-9A-Fa-f]{32})\s+(.+)$')
_AMULE_PROGRESS_RE = re.compile(
    r'^\s*>\s*\[([\d.]+)%\]\s+(\d+)\s*/\s*(\d+)\s*'
    r'(?:\(\d+\)\s*)?(?:\+\d+\s*)?-\s*([^-]+?)\s*-'
)


def _amule_bytes_total_by_name():
    """"dans le téléchargement amule je n'ai pas l'info de la taille du fichier" -
    amulecmd ("show dl") ne rapporte que des comptes de parts eD2K, jamais la taille
    réelle en octets (voir _AMULE_PROGRESS_RE ci-dessous). Le lien ed2k lui-même la
    contient en revanche, déjà décodée et stockée à l'ajout (voir add_to_emule,
    emule/routes.py, et bytes_total côté mark_download_pending) - relue ici pour
    compléter ce que amulecmd ne peut pas donner. active_downloads n'expire plus
    automatiquement (la base est la référence, pas une fenêtre de temps), donc aucun
    filtre d'âge ici non plus."""
    try:
        db_path = current_app.config.get('DATABASE')
        if not db_path:
            return {}
        import sqlite3
        conn = sqlite3.connect(db_path, timeout=15.0)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT title, bytes_total FROM active_downloads
            WHERE client = 'amule' AND bytes_total IS NOT NULL
            ORDER BY created_at DESC
        ''')
        rows = cursor.fetchall()
        conn.close()
        return {title: bytes_total for title, bytes_total in rows}
    except Exception as e:
        print(f"Erreur lecture taille aMule depuis active_downloads: {e}")
        return {}


# "aMule 2.3.3's EC socket implementation mishandles rapid or concurrent short-lived
# amulecmd connections. Repeated five-second polling—especially from multiple browser
# tabs—caused assertions and eventual file-descriptor exhaustion." - _amule_status()
# spawns a REAL amulecmd subprocess (a fresh EC connection each time) on every call,
# and every open /import tab polls it independently every 5s (see
# refreshDownloadingProgress, import.js) - N tabs = N concurrent EC connections, never
# shared. Cache + lock below are shared by the WHOLE process (module-level, not per
# request): a call that finds the cache still fresh (within _AMULE_STATUS_CACHE_TTL)
# reuses it without ever launching amulecmd, and a call that arrives WHILE another is
# already refreshing waits for that same result instead of opening its own EC
# connection in parallel - at most one EC connection at a time, at most one every
# _AMULE_STATUS_CACHE_TTL seconds, no matter how many tabs/users poll simultaneously.
_amule_status_cache = {'result': None, 'fetched_at': 0.0}
_amule_status_lock = threading.Lock()
_AMULE_STATUS_CACHE_TTL = 20


def _amule_status():
    from blueprints.emule.routes import load_emule_config

    config = load_emule_config()
    if not config.get('enabled'):
        return None

    with _amule_status_lock:
        now = time.time()
        if _amule_status_cache['result'] is not None and now - _amule_status_cache['fetched_at'] < _AMULE_STATUS_CACHE_TTL:
            return _amule_status_cache['result']
        status = _amule_status_uncached(config)
        _amule_status_cache['result'] = status
        _amule_status_cache['fetched_at'] = time.time()
        return status


def resolve_absent_amule_download(client_item_id, shared_hashes):
    """"pourquoi il y a autant de telechargement sur amule mais juste 2 s'affiche sur
    bullar" (enquete complementaire) - `show dl` ne liste QUE les telechargements
    encore en cours: un fichier qui vient de terminer bascule cote aMule vers "partage"
    (`show shared`) et disparait instantanement de `show dl`, exactement comme un
    telechargement reellement annule/supprime cote client. Sans cette distinction, le
    compteur d'absences (client_absence_streak) traitait les deux cas de facon
    identique et marquait `failed` un telechargement qui avait en realite REUSSI
    (constate en production: "BD.FR.-.Embrasement (L')..." disparu de `show dl` puis
    marque failed apres 3 sondages, alors que son hash etait deja present dans
    `show shared`, fichier reel de 250 Mo sur disque).

    Retourne 'completed' si ce hash est retrouve dans `show shared` (le fichier a fini
    de telecharger, get_pending_downloads/reconcile_stale_active_downloads prendront le
    relais des que le scan de repertoire le rattache a son volume), 'absent' sinon
    (comportement inchange: compteur d'absence normal, mark_download_failed apres
    CLIENT_ABSENCE_FAILURE_THRESHOLD sondages consecutifs)."""
    if not client_item_id:
        return 'absent'
    return 'completed' if client_item_id.lower() in shared_hashes else 'absent'


def _amule_shared_hashes(config):
    """Hashes (minuscules) de `amulecmd -c "show shared"` - fichiers qu'aMule a fini de
    telecharger et partage desormais, plus jamais listes par `show dl` (voir
    resolve_absent_amule_download). Erreurs avalees (retourne un set vide): un alea
    reseau ici ne doit jamais transformer un telechargement reussi en faux `failed`,
    mais ne doit pas non plus faire echouer tout le sondage principal - au pire ce
    sondage-ci retombe sur le comportement d'absence standard, rattrape au suivant."""
    try:
        cmd = [
            'amulecmd',
            '-h', config['host'],
            '-P', config.get('password_decrypted', ''),
            '-p', str(config['ec_port']),
            '-c', 'show shared'
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except Exception:
        return set()

    if result.returncode != 0:
        return set()

    hashes = set()
    for line in result.stdout.splitlines():
        name_match = _AMULE_NAME_RE.match(line)
        if name_match:
            hashes.add(name_match.group(1).lower())
    return hashes


def _amule_status_uncached(config):
    from blueprints.missing_monitor.downloader import _filenames_match

    try:
        cmd = [
            'amulecmd',
            '-h', config['host'],
            '-P', config.get('password_decrypted', ''),
            '-p', str(config['ec_port']),
            '-c', 'show dl'
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except Exception as e:
        return {'client': 'amule', 'label': 'aMule', 'error': str(e)[:200], 'items': [], 'shared_hashes': set()}

    if result.returncode != 0:
        return {'client': 'amule', 'label': 'aMule', 'error': (result.stderr or 'Erreur amulecmd')[:200], 'items': [], 'shared_hashes': set()}

    bytes_total_by_name = _amule_bytes_total_by_name()
    shared_hashes = _amule_shared_hashes(config)

    items = []
    pending_hash = None
    pending_name = None
    for line in result.stdout.splitlines():
        name_match = _AMULE_NAME_RE.match(line)
        if name_match:
            pending_hash = name_match.group(1)
            pending_name = name_match.group(2).strip()
            continue
        progress_match = _AMULE_PROGRESS_RE.match(line)
        if progress_match and pending_name is not None:
            percent, _downloaded_parts, _total_parts, state = progress_match.groups()
            size_bytes = next(
                (total for name, total in bytes_total_by_name.items() if _filenames_match(name, pending_name)),
                None
            )
            items.append({
                'id': pending_hash,  # requis par POST /api/emule/remove (amulecmd "cancel <hash>")
                'name': pending_name,
                'progress': float(percent),
                'state': state.strip(),
                'size_bytes': size_bytes,
                'downloaded_bytes': round(size_bytes * float(percent) / 100) if size_bytes else None,
                'speed_bytes_s': None,  # Pas exposé par "show dl" (que le débit global)
                'eta_seconds': None,
            })
            pending_hash = None
            pending_name = None
    return {'client': 'amule', 'label': 'aMule', 'error': None, 'items': items, 'shared_hashes': shared_hashes}


def _run_in_context(app, func):
    with app.app_context():
        return func()


@activity_bp.route('/api/activity/status', methods=['GET'])
def activity_status():
    """Agrège le statut de téléchargement de tous les clients configurés+activés.
    Chaque entrée de `clients` est None (client désactivé, absent du résultat) ou un dict
    {client, label, error, items} - `error` non-null signale un client activé mais
    injoignable/en échec, affiché comme tel plutôt que masqué silencieusement.

    "tu as la liste des torrents téléchargés et eMule via leurs clients respectifs, donc
    ne sonde que ceux encore en téléchargement. pas la peine de sonder Deluge s'il n'y a
    pas de téléchargement Deluge" - active_downloads (voir get_pending_downloads) dit déjà,
    sans le moindre appel réseau, si un client a quoi que ce soit à vérifier: un client
    sans aucune ligne 'pending' n'est même pas pingé (skip complet de sa fonction
    _<client>_status(), pas seulement de son résultat une fois obtenu) - inutile
    d'interroger l'API JSON-RPC de Deluge ou de lancer amulecmd si cette app n'a rien en
    attente chez lui.

    Les clients qui ONT quelque chose à vérifier sont sondés en parallèle (un Deluge à
    300+ torrents ou un aMule interrogé via subprocess peuvent à eux seuls prendre
    plusieurs secondes, et s'additionnaient sinon à celles des autres clients - "import
    est très lent à s'afficher"). Chaque thread reçoit explicitement le contexte app (les
    fonctions _<client>_status() lisent current_app.config) - un ThreadPoolExecutor ne
    l'hérite pas automatiquement, voir CLAUDE.md.

    `pending`: LA référence pour "qu'est-ce qui est en train d'être téléchargé" ("the
    database has the information about what is being downloaded / serie / volume /
    client" - voir get_pending_downloads/mark_download_pending) - une ligne par
    active_downloads 'pending', TOUJOURS renvoyée, jamais masquée par une correspondance
    de nom approximative avec un item client actuellement affiché (l'ancien comportement:
    un pack pourtant intact en base disparaissait purement et simplement dès qu'un nom de
    torrent chez le client lui ressemblait suffisamment - "Videur is gone"). Le seul cas
    où une ligne 'pending' est tue ici, c'est quand SA PROPRE ligne (même id de suivi
    exact, pas une ressemblance de nom) est déjà affichée juste au-dessus comme barre de
    progression active - jamais une autre logique de correspondance.

    `completed_pending_ids`: "you periodically check the status of the download. once it
    is finished you look at the file on disk" - ids de lignes 'pending' dont le
    téléchargement suivi vient d'atteindre 100% chez son client CE sondage-ci (résolu par
    id de suivi exact, voir plus bas) - le frontend s'en sert pour déclencher UNE FOIS un
    scan ciblé du répertoire d'import et faire apparaître le pack sous sa forme dépliante
    dès que ses fichiers sont là, sans attendre que l'utilisateur clique "Actualiser"
    lui-même."""
    from blueprints.missing_monitor.downloader import (
        get_pending_downloads, find_active_downloads_by_client_item_ids,
        set_active_download_client_item_id, match_pending_download_by_name,
        reset_client_absence_streak, increment_client_absence_streak,
        mark_download_failed, mark_download_completed, CLIENT_ABSENCE_FAILURE_THRESHOLD,
    )

    check_fns = {
        'qbittorrent': _qbittorrent_status,
        'rtorrent': _rtorrent_status,
        'deluge': _deluge_status,
        'amule': _amule_status,
    }

    # "the db is filled when files are downloaded and put as imported once it is
    # imported. the other files for any clients should not appear there" - UNE seule
    # lecture de la base ici, réutilisée à la fois pour savoir quels clients valent la
    # peine d'être sondés (ceux qui ont au moins une ligne 'pending') et comme périmètre
    # de correspondance par nom plus bas (jamais un historique large de 30 jours).
    pending_rows = get_pending_downloads()
    pending_by_client = {}
    for p in pending_rows:
        pending_by_client.setdefault(p['client'], []).append(p)

    clients_to_probe = {client: fn for client, fn in check_fns.items() if pending_by_client.get(client)}

    # "il faut que les pending soient retires par leur activite naturelle, si le
    # fichier est supprime ou en echec alors son etat ne doit plus etre en pending" -
    # une ligne active_downloads deja LIEE a un item client precis (client_item_id, voir
    # set_active_download_client_item_id plus bas) que ce meme client ne rapporte plus
    # DU TOUT lors de ce sondage a un signal reel et direct: le telechargement a ete
    # supprime/annule cote client. Jamais base sur un age (purge 6h deja retiree pour
    # cette raison), uniquement sur ce fait constate a CHAQUE sondage - remis a zero des
    # qu'un item reapparait, tolere un sondage manque isole (client temporairement
    # injoignable) via CLIENT_ABSENCE_FAILURE_THRESHOLD avant de conclure a un vrai
    # abandon.
    linked_rows_by_client = {}
    for p in pending_rows:
        if p.get('client_item_id'):
            linked_rows_by_client.setdefault(p['client'], []).append(p)

    clients = []
    # Un id de suivi (active_downloads.id) dans live_tracking_ids veut dire "déjà montré
    # ci-dessus comme barre de progression active" - le seul motif légitime de ne pas
    # aussi le montrer comme ligne 'pending' plus bas (éviter un doublon exact de LA MÊME
    # ligne, pas une ressemblance de nom avec autre chose). Un id à 100% n'y va jamais: il
    # sort de "en cours" (voir r['items'] plus bas) mais reste une ligne 'pending' à part
    # entière tant qu'il n'est pas réellement importé - voir just_completed_tracking_ids.
    live_tracking_ids = set()
    just_completed_tracking_ids = set()
    if clients_to_probe:
        app = current_app._get_current_object()
        with ThreadPoolExecutor(max_workers=len(clients_to_probe)) as executor:
            futures = {client: executor.submit(_run_in_context, app, fn) for client, fn in clients_to_probe.items()}
            results = {client: f.result() for client, f in futures.items()}

        for client, r in results.items():
            if r is None:
                continue
            client_pending_rows = pending_by_client.get(client, [])
            # "you have all the information already in the database. so no need to do
            # any fuzzy-matching" - un seul aller-retour DB pour TOUS les items de ce
            # client (voir find_active_downloads_by_client_item_ids) plutôt qu'une
            # connexion SQLite par item à chaque appel de cette route.
            item_ids = [item['id'] for item in r['items'] if item.get('id')]
            linked_by_item_id = find_active_downloads_by_client_item_ids(client, item_ids)
            recognized_items = []
            for item in r['items']:
                item_id = item.get('id')
                linked = linked_by_item_id.get(item_id.lower()) if item_id else None
                if linked is None:
                    # match_pending_download_by_name (downloader.py) exclut déjà toute
                    # ligne ayant un client_item_id - "no pack row at all, only flat
                    # rugby file rows" était causé par l'absence de ce garde-fou: un vrai
                    # doublon de torrent (même nom, hash différent) volait le lien exact
                    # d'une ligne déjà résolue (constaté en réel: torrent 100% déjà lié
                    # remplacé par son doublon 99.8% au sondage suivant).
                    pending_match = match_pending_download_by_name(client_pending_rows, item['name'])
                    if pending_match is None:
                        continue
                    linked = {
                        'tracking_id': pending_match['id'],
                        'series_id': pending_match.get('series_id'),
                        'series_title': pending_match.get('series_title'),
                        'volume_number': pending_match.get('volume_number'),
                        'is_oneshot': pending_match.get('is_oneshot', False),
                        'is_integral': pending_match.get('is_integral', False),
                        'integral_number': pending_match.get('integral_number'),
                        'is_hs': pending_match.get('is_hs', False),
                        'hs_number': pending_match.get('hs_number'),
                        'is_episode': pending_match.get('is_episode', False),
                        'episode_number': pending_match.get('episode_number'),
                        'created_at': pending_match.get('created_at'),
                    }
                    # Lien persisté UNE FOIS ici - tous les sondages suivants pour ce
                    # même téléchargement entreront directement par linked_by_item_id
                    # sans plus jamais comparer de noms.
                    if item_id is not None:
                        set_active_download_client_item_id(pending_match['id'], item_id)

                item['tracking_id'] = linked['tracking_id']
                item['series_id'] = linked['series_id']
                item['series_title'] = linked['series_title']
                item['volume_number'] = linked['volume_number']
                item['is_oneshot'] = linked.get('is_oneshot', False)
                item['is_integral'] = linked['is_integral']
                item['integral_number'] = linked['integral_number']
                item['is_hs'] = linked.get('is_hs', False)
                item['hs_number'] = linked.get('hs_number')
                item['is_episode'] = linked.get('is_episode', False)
                item['episode_number'] = linked.get('episode_number')
                item['created_at'] = linked.get('created_at')
                recognized_items.append(item)

            in_progress_items = [i for i in recognized_items if i['progress'] < 100]
            live_tracking_ids.update(i['tracking_id'] for i in in_progress_items if i['tracking_id'] is not None)
            just_completed_tracking_ids.update(
                i['tracking_id'] for i in recognized_items
                if i['progress'] >= 100 and i['tracking_id'] is not None
            )
            r['items'] = in_progress_items
            # shared_hashes n'est qu'un signal interne pour la resolution d'absence
            # ci-dessous (voir resolve_absent_amule_download) - un set() n'est PAS
            # serialisable en JSON et faisait echouer TOUTE la reponse /api/activity/
            # status avec un 500 des le premier appel une fois cette cle ajoutee a
            # _amule_status_uncached (regression detectee au deploiement, corrigee
            # avant que quiconque ne la voie en prod). Retire ici, apres l'avoir
            # capture, avant que r ne rejoigne clients (ce qui finit par jsonify()).
            shared_hashes = r.pop('shared_hashes', None) or set()
            clients.append(r)

            # Seul un sondage RÉUSSI (error is None, on a vraiment reçu une réponse de
            # ce client) fait foi ici - un client injoignable/en erreur ne doit jamais
            # être interprété comme "tous ses téléchargements ont disparu" (voir
            # error=None ci-dessous, sans quoi une simple panne réseau ferait échouer
            # en masse tout ce qui est en cours chez lui).
            if r.get('error') is None:
                # item['id'] est comparé tel quel a client_item_id qui est TOUJOURS
                # stocke en minuscules (voir mark_download_pending/set_active_download_
                # client_item_id) - meme normalisation que linked_by_item_id plus haut.
                confirmed_item_ids = {
                    item['id'].lower() for item in recognized_items if item.get('id')
                }
                # aMule seulement: un item absent de `show dl` peut avoir RÉUSSI (voir
                # resolve_absent_amule_download) plutôt que réellement disparu - vérifié
                # AVANT de compter une absence, jamais après coup.
                for tracked_row in linked_rows_by_client.get(client, []):
                    if tracked_row['client_item_id'] in confirmed_item_ids:
                        reset_client_absence_streak(tracked_row['id'])
                    elif client == 'amule' and resolve_absent_amule_download(
                        tracked_row['client_item_id'], shared_hashes
                    ) == 'completed':
                        reset_client_absence_streak(tracked_row['id'])
                        mark_download_completed(tracked_row['id'])
                    else:
                        streak = increment_client_absence_streak(tracked_row['id'])
                        if streak >= CLIENT_ABSENCE_FAILURE_THRESHOLD:
                            mark_download_failed(tracked_row['id'])

    # is_pack exempté de ce masquage: contrairement à un téléchargement simple (où montrer
    # à la fois une barre de progression ET une ligne "en attente" serait un vrai doublon),
    # un pack encore activement en transfert (progress < 100, donc dans live_tracking_ids)
    # peut déjà avoir PLUSIEURS de ses fichiers arrivés sur disque - masquer sa ligne
    # 'pending' ici la retire aussi de pendingDownloads côté frontend, et _pendingPackGroups
    # (import.js) n'a alors plus AUCUNE ligne 'pending' sous laquelle regrouper ces fichiers
    # déjà repérés (pack_download_id, voir scan_import_directory) - ils retombaient donc en
    # lignes plates non repliées ("the files were not collapsed") tant que le torrent
    # continuait de transférer le reste. Le pack reste donc visible aux DEUX endroits
    # (barre de progression du torrent entier + ligne dépliante de ce qui est déjà arrivé)
    # tant qu'il n'est pas fini - complémentaire, pas un doublon.
    # Un suivi absent du client reste affiché : il peut nécessiter une décision
    # manuelle. L'utilisateur dispose du bouton de suppression ; l'API ne masque jamais
    # silencieusement cette ligne.
    pending = [p for p in pending_rows if p['id'] not in live_tracking_ids or p.get('is_pack')]
    completed_pending_ids = [p['id'] for p in pending if p['id'] in just_completed_tracking_ids]

    return jsonify({'success': True, 'clients': clients, 'pending': pending, 'completed_pending_ids': completed_pending_ids})


@activity_bp.route('/api/activity/update-tracking', methods=['POST'])
def update_tracking_route():
    """"je voudrais pouvoir editer les volumes et album dans l'import meme quand c'est pas
    terminé" - corrige la série/le tome suivis (active_downloads.series_id/volume_number,
    voir update_active_download_tracking) pour une ligne encore EN COURS (active chez un
    client ou juste 'pending', voir activity_status ci-dessus), sans attendre que le
    fichier soit arrivé sur disque comme le fait déjà la correction manuelle des fichiers
    scannés (voir /api/import/mark-manual). tracking_id: active_downloads.id (item.
    tracking_id pour une ligne active/'pending', jamais item.id qui est le hash CLIENT)."""
    from blueprints.missing_monitor.downloader import update_active_download_tracking

    data = request.get_json() or {}
    tracking_id = data.get('tracking_id')
    series_id = data.get('series_id')
    volume_number = data.get('volume_number')
    is_integral = bool(data.get('is_integral'))
    integral_number = data.get('integral_number')
    is_hs = bool(data.get('is_hs'))
    hs_number = data.get('hs_number')
    is_episode = bool(data.get('is_episode'))
    episode_number = data.get('episode_number')

    if not isinstance(tracking_id, int) or not isinstance(series_id, int):
        return jsonify({'success': False, 'error': 'tracking_id/series_id invalide'}), 400

    ok = update_active_download_tracking(
        tracking_id, series_id, volume_number,
        is_integral=is_integral, integral_number=integral_number,
        is_hs=is_hs, hs_number=hs_number,
        is_episode=is_episode, episode_number=episode_number,
    )
    if not ok:
        return jsonify({'success': False, 'error': 'Échec de la mise à jour'}), 500
    return jsonify({'success': True})


# Un client par clé (voir aussi CLIENT_REMOVE_ENDPOINTS côté import.js) - endpoint HTTP
# local plutôt qu'appeler la logique d'annulation directement: même convention déjà en
# place dans cette appli pour réutiliser un endpoint d'un AUTRE blueprint sans dépendance
# d'import circulaire (voir _download_to_qbittorrent/_download_to_amule côté
# missing_monitor/downloader.py, qui font exactement ce genre d'appel en boucle locale).
_CLIENT_STATUS_FNS = {
    'qbittorrent': _qbittorrent_status, 'rtorrent': _rtorrent_status,
    'deluge': _deluge_status, 'amule': _amule_status,
}
_CLIENT_REMOVE_URLS = {
    'qbittorrent': 'http://127.0.0.1:5000/api/qbittorrent/remove',
    'rtorrent': 'http://127.0.0.1:5000/api/rtorrent/remove',
    'deluge': 'http://127.0.0.1:5000/api/deluge/remove',
    'amule': 'http://127.0.0.1:5000/api/emule/remove',
}


@activity_bp.route('/api/activity/pending/remove', methods=['POST'])
def remove_pending_download_route():
    ""
    from blueprints.missing_monitor.downloader import remove_pending_download, get_pending_downloads, _filenames_match

    data = request.get_json() or {}
    download_id = data.get('id')
    if not isinstance(download_id, int):
        return jsonify({'success': False, 'error': 'id invalide'}), 400

    pending_row = next((p for p in get_pending_downloads() if p['id'] == download_id), None)

    cancelled_at_client = False
    if pending_row and pending_row.get('client') in _CLIENT_STATUS_FNS:
        try:
            status = _CLIENT_STATUS_FNS[pending_row['client']]()
            match = next((i for i in (status or {}).get('items', []) if _filenames_match(i['name'], pending_row['title'])), None)
            if match and match.get('id'):
                resp = requests.post(_CLIENT_REMOVE_URLS[pending_row['client']], json={'id': match['id']}, timeout=15)
                cancelled_at_client = resp.status_code == 200 and resp.json().get('success', False)
        except Exception as e:
            print(f"Erreur annulation téléchargement client pour ligne en attente #{download_id}: {e}")

    if remove_pending_download(download_id):
        return jsonify({'success': True, 'cancelled_at_client': cancelled_at_client})
    return jsonify({'success': False, 'error': 'Suppression impossible'}), 500
