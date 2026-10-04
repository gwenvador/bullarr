"""
Routes pour le scraper ebdz.net
"""
from flask import request, jsonify, current_app
from . import ebdz_bp
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from encryption import encrypt, decrypt, ensure_encryption_key
from blueprints.settings.rss import fetch_feed, load_feeds, feed_cache_key
from blueprints.prowlarr.config_store import load_prowlarr_config

# Durée max entre deux liens pour qu'ils soient considérés comme faisant
# partie du même scrape (un scrape isolé ne dure jamais aussi longtemps,
# et le prochain scrape programmé arrive bien plus tard)
SCRAPE_SESSION_GAP = timedelta(minutes=30)

# État du scraping manuel déclenché depuis /ebdz-nouveautes ou /settings (POST /scrape,
# voir plus bas) - un simple dict module-level suffit (process unique, threaded=True mais
# le GIL protège ces assignations simples, voir app.py) plutôt qu'une table dédiée : ce
# n'est qu'un statut éphémère consulté par polling, pas une donnée à conserver entre deux
# redémarrages. Existe pour que le bouton "🔄" de /ebdz-nouveautes puisse retrouver l'état
# "scraping en cours" même après un changement de page + retour, ou une simple réouverture
# du navigateur - un scrape POST est une requête HTTP synchrone qui continue de tourner
# côté serveur même si le client abandonne la connexion (navigation, fermeture d'onglet),
# donc l'état visible côté client ne doit pas dépendre de la session JS qui l'a démarré.
_manual_scrape_state = {'running': False, 'forums_total': 0, 'forums_done': 0}


def load_ebdz_config():
    """Charge la configuration ebdz"""
    config_file = current_app.config['EBDZ_CONFIG_FILE']
    
    if os.path.exists(config_file):
        with open(config_file, 'r') as f:
            config = json.load(f)
            # Déchiffrer le mot de passe s'il est chiffré
            if config.get('password'):
                config['password_decrypted'] = decrypt(config.get('password'))
            return config
    
    default_config = current_app.config['EBDZ_CONFIG'].copy()
    if default_config.get('password'):
        default_config['password_decrypted'] = decrypt(default_config.get('password'))
    return default_config


def is_ebdz_configured():
    """"make sure that if komga or ebdz is not configured they dont show up in the
    table or the settings with matching" - point d'entrée unique pour cette question.
    Contrairement à Komga (bascule "enabled" dédiée), EBDZ n'a pas d'interrupteur:
    "configuré" veut dire identifiants renseignés, exactement le même test que /scrape
    plus bas ("Identifiants non configurés") - sans ça un scrape/matching n'a de toute
    façon aucune chance d'aboutir."""
    config = load_ebdz_config()
    return bool(config.get('username')) and bool(config.get('password'))


def save_ebdz_config(config):
    """Sauvegarde la configuration ebdz"""
    config_file = current_app.config['EBDZ_CONFIG_FILE']
    
    try:
        # Préparer une copie pour la sauvegarde
        config_to_save = config.copy()
        
        # Chiffrer le mot de passe avant la sauvegarde
        if config_to_save.get('password'):
            # Enlever le flag _decrypted temporaire aux fins de sauvegarde
            if 'password_decrypted' in config_to_save:
                # Utiliser le mot de passe déchiffré pour le chiffrer à nouveau
                config_to_save['password'] = encrypt(config_to_save['password_decrypted'])
                del config_to_save['password_decrypted']
            else:
                # Le mot de passe est déjà en clair, le chiffrer
                config_to_save['password'] = encrypt(config_to_save['password'])
        
        with open(config_file, 'w') as f:
            json.dump(config_to_save, f, indent=4)
        os.chmod(config_file, 0o600)
        return True
    except Exception as e:
        print(f"Erreur sauvegarde config : {e}")
        return False


def mark_ebdz_scrape_completed():
    """Persiste l'heure de fin d'un scrape (manuel OU automatique) dans ebdz_config.json -
    "a chaque fois que l'on redemarre le docker le rescanne est deplacé d'un jour": le
    scheduler (voir EBDZScheduler.add_job) n'avait jusqu'ici AUCUNE mémoire du dernier
    scrape réellement effectué - à chaque redémarrage du container, IntervalTrigger
    recalculait "prochain scrape = maintenant + intervalle" à partir de l'instant du
    redémarrage, pas du dernier scrape réel. Un redémarrage fréquent (ou simplement pas
    exactement 24h après le précédent) décale donc le planning un peu plus à chaque fois,
    au lieu de suivre un rythme fixe. Appelée après CHAQUE scrape réussi, manuel ou
    planifié, pour que les deux se recalent sur la même horloge."""
    try:
        config = load_ebdz_config()
        config['last_scrape_at'] = datetime.now().isoformat()
        save_ebdz_config(config)
    except Exception as e:
        print(f"Erreur enregistrement heure de dernier scrape EBDZ: {e}")


@ebdz_bp.route('/config', methods=['GET', 'POST'])
def ebdz_config():
    """Configuration ebdz.net"""
    
    if request.method == 'GET':
        config = load_ebdz_config()
        
        return jsonify({
            'username': config.get('username', ''),
            'password': '****' if config.get('password') else '',
            'forums': config.get('forums', [])
        })
    
    else:  # POST
        try:
            data = request.get_json()
            config = load_ebdz_config()
            
            config['username'] = data.get('username', '').strip()
            
            # Ne met à jour le mot de passe que s'il n'est pas masqué
            new_password = data.get('password', '')
            if new_password and new_password != '****':
                config['password_decrypted'] = new_password
            
            # Validation des forums - plus de "max_pages" (voir scraper.py get_thread_links:
            # la pagination s'arrête d'elle-même dès qu'elle retrouve un thread déjà connu
            # et inchangé, un réglage manuel de nombre de pages n'a plus lieu d'être)
            forums = []
            for f in data.get('forums', []):
                fid = f.get('fid')
                if fid is not None:
                    forums.append({
                        'fid': int(fid),
                        'category': f.get('category', '').strip() or f'Forum {fid}',
                    })
            
            config['forums'] = forums
            
            if save_ebdz_config(config):
                return jsonify({'success': True})
            else:
                return jsonify({'success': False, 'error': 'Erreur de sauvegarde'}), 500
        
        except Exception as e:
            return jsonify({'success': False, 'error': 'Erreur interne'}), 500


@ebdz_bp.route('/scrape', methods=['POST'])
def scrape():
    """Lance le scraper ebdz.net"""
    global _manual_scrape_state

    if _manual_scrape_state['running']:
        return jsonify({'success': False, 'error': 'Un scraping est déjà en cours', 'already_running': True}), 409

    try:
        config = load_ebdz_config()
        username = config.get('username', '')
        password = config.get('password_decrypted', '')  # Utiliser le mot de passe déchiffré
        all_forums = config.get('forums', [])

        if not username or not password:
            return jsonify({'success': False, 'error': 'Identifiants non configurés'}), 400

        if not all_forums:
            return jsonify({'success': False, 'error': 'Aucun forum configuré'}), 400

        # Filtrer par les fid envoyés depuis le front
        data = request.get_json(silent=True) or {}
        requested_fids = data.get('fids', [])

        if requested_fids:
            forums = [f for f in all_forums if f.get('fid') in requested_fids]
            if not forums:
                return jsonify({'success': False, 'error': 'Aucun forum correspondant'}), 400
        else:
            forums = all_forums

        # Import dynamique du scraper
        from .scraper import MyBBScraper

        total_links = 0
        forums_scraped = 0

        # État consulté par GET /scrape/status (voir _manual_scrape_state plus haut) - le
        # scrape continue de tourner même si le client qui l'a déclenché a changé de page
        # ou fermé son onglet, donc ce statut doit être vrai côté serveur, pas déduit d'un
        # état JS local qui disparaît avec la navigation.
        _manual_scrape_state.update({'running': True, 'forums_total': len(forums), 'forums_done': 0})
        # Une seule connexion pour le comptage post-scrape de tous les forums, plutôt
        # qu'une ouverture/fermeture par forum - le scraping lui-même (I/O réseau) domine
        # largement le temps total, mais autant ne pas réouvrir une connexion SQLite à
        # chaque itération pour une simple requête COUNT.
        count_conn = sqlite3.connect(current_app.config['DB_FILE'])
        try:
            for forum_cfg in forums:
                fid = forum_cfg['fid']
                category = forum_cfg['category']

                forum_url = f"https://ebdz.net/forum/forumdisplay.php?fid={fid}"

                print(f"\n� Scraping forum fid={fid} catégorie='{category}'...")

                scraper = MyBBScraper(
                    base_url=forum_url,
                    db_file=current_app.config['DB_FILE'],
                    username=username,
                    password=password,
                    forum_category=category
                )

                scraper.run()
                forums_scraped += 1
                _manual_scrape_state['forums_done'] = forums_scraped

                # Compter les liens
                cursor = count_conn.cursor()
                cursor.execute('SELECT COUNT(*) FROM ed2k_links WHERE forum_category = ?', (category,))
                total_links += cursor.fetchone()[0]
        finally:
            count_conn.close()
            _manual_scrape_state['running'] = False

        mark_ebdz_scrape_completed()

        return jsonify({
            'success': True,
            'forums_scraped': forums_scraped,
            'total_links': total_links
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Erreur interne'}), 500


@ebdz_bp.route('/scrape/status', methods=['GET'])
def scrape_status():
    """Statut du scraping manuel en cours (voir _manual_scrape_state) - consulté par
    polling depuis n'importe quelle page (pollEbdzScrapeStatus dans nav.js), pour retrouver
    l'état "scraping en cours" même après un changement de page ou une réouverture."""
    return jsonify({'success': True, **_manual_scrape_state})


@ebdz_bp.route('/auto-scrape/config', methods=['GET', 'POST'])
def auto_scrape_config():
    """Configuration du scraping automatique EBDZ"""
    
    if request.method == 'GET':
        config = load_ebdz_config()
        return jsonify({
            'auto_scrape_enabled': config.get('auto_scrape_enabled', False),
            'auto_scrape_interval': config.get('auto_scrape_interval', 60),
            'auto_scrape_interval_unit': config.get('auto_scrape_interval_unit', 'minutes')
        })
    
    else:  # POST
        try:
            data = request.get_json()
            config = load_ebdz_config()
            
            # Mettre à jour la configuration
            enabled = data.get('auto_scrape_enabled', False)
            interval = int(data.get('auto_scrape_interval', 60))
            interval_unit = data.get('auto_scrape_interval_unit', 'minutes')
            
            if interval < 1:
                return jsonify({'success': False, 'error': 'L\'intervalle doit être >= 1'}), 400
            
            if interval_unit not in ['minutes', 'hours', 'days']:
                return jsonify({'success': False, 'error': 'Unité de temps invalide'}), 400
            
            config['auto_scrape_enabled'] = enabled
            config['auto_scrape_interval'] = interval
            config['auto_scrape_interval_unit'] = interval_unit
            
            if save_ebdz_config(config):
                # Gérer le scheduler
                if enabled:
                    from .scheduler import ebdz_scheduler
                    ebdz_scheduler.add_job(interval, interval_unit)
                else:
                    from .scheduler import ebdz_scheduler
                    ebdz_scheduler.remove_job()
                
                return jsonify({'success': True})
            else:
                return jsonify({'success': False, 'error': 'Erreur de sauvegarde'}), 500
        
        except Exception as e:
            return jsonify({'success': False, 'error': 'Erreur interne'}), 500


def _build_nouveautes_title_match_index(series_rows, title_match_key):
    """Indexe les séries locales par clé de titre, sans jamais choisir entre deux
    candidates. Les nouveautés peuvent alors ouvrir une fiche et son lien Bédéthèque
    lorsque le titre correspond de façon certaine, même si le sujet EBDZ précis n'a pas
    encore été attaché à la série. Aucun champ de la base n'est modifié ici.
    """
    candidates = {}
    for series_id, _thread_id, bedetheque_url, series_title in series_rows:
        if not series_title:
            continue
        key = title_match_key(series_title)
        candidates.setdefault(key, []).append((series_id, bedetheque_url, series_title))
    return {
        key: matches[0] if len({match[0] for match in matches}) == 1 else None
        for key, matches in candidates.items()
    }


def _build_nouveautes_title_match_index(series_rows, title_match_key):
    """Indexe les séries locales par clé de titre, sans jamais choisir entre deux
    candidates. Les nouveautés peuvent alors ouvrir une fiche et son lien Bédéthèque
    lorsque le titre correspond de façon certaine, même si le sujet EBDZ précis n'a pas
    encore été attaché à la série. Aucun champ de la base n'est modifié ici.
    """
    candidates = {}
    for series_id, _thread_id, bedetheque_url, series_title in series_rows:
        if not series_title:
            continue
        key = title_match_key(series_title)
        candidates.setdefault(key, []).append((series_id, bedetheque_url, series_title))
    return {
        key: matches[0] if len({match[0] for match in matches}) == 1 else None
        for key, matches in candidates.items()
    }


@ebdz_bp.route('/latest', methods=['GET'])
def latest_scrape():
    """Retourne l'historique des scrapes (fichiers ajoutés à chaque session), la plus
    récente en premier, borné aux `days` derniers jours (défaut 15 - "limite à 15 jours")
    plutôt que l'historique complet à chaque chargement (table `ed2k_links` non purgée,
    62k+ lignes et en croissance continue). `?days=N` élargit la fenêtre ("je peux charger
    plus s'il le faut", voir loadLatestEbdz côté JS qui rappelle avec un N plus grand).
    has_more indique s'il existe des données plus anciennes que la fenêtre actuelle.
    Chaque session est en plus bornée à MAX_LINKS_PER_SESSION liens pour éviter un
    payload énorme si un scrape isolé (import initial du catalogue) en contient des
    dizaines de milliers."""
    MAX_LINKS_PER_SESSION = 150
    try:
        days = request.args.get('days', 15, type=int)
        if not days or days <= 0:
            days = 15
        # utcnow(), pas now(): comparé plus bas à ed2k_links.date_scraped, un
        # CURRENT_TIMESTAMP SQLite - toujours UTC quel que soit le fuseau du conteneur
        # (voir TZ=Europe/Paris, docker-compose.yml). now() aurait décalé cette fenêtre
        # "N derniers jours" de 1-2h par rapport aux dates réellement en base.
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
        older_than_days = request.args.get('older_than_days', type=int)
        upper_cutoff = (
            (datetime.now(timezone.utc) - timedelta(days=older_than_days)).strftime('%Y-%m-%d %H:%M:%S')
            if older_than_days is not None else None
        )
        limit = request.args.get('limit', type=int)

        db_file = current_app.config['DB_FILE']
        conn = sqlite3.connect(db_file, timeout=30.0)
        # sqlite3.Row + accès par nom de colonne plutôt qu'un tuple-unpack positionnel
        # row[0]..row[16] loin de son SELECT (17 colonnes) - une colonne insérée ailleurs
        # qu'en toute fin de la liste décalerait silencieusement tous les champs suivants,
        # même raisonnement que get_pending_downloads (downloader.py).
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ed2k_links'")
        if cursor.fetchone() is None:
            conn.close()
            return jsonify({'success': True, 'sessions': [], 'has_more': False})

        # Titres locaux normalisés, pour signaler dans les résultats les threads qui
        # correspondent à une série déjà présente dans une bibliothèque (voir already_in_library)
        # ou à une série sous surveillance active (voir already_monitored, missing_volume_monitor).
        # La table series vit dans la base bibliothèque (DATABASE), pas dans celle du
        # scraper EBDZ (DB_FILE) sur laquelle `conn` est ouverte ci-dessus: connexion séparée
        from blueprints.search.routes import normalize_search_text, ebdz_core_title
        from blueprints.library.scanner import LibraryScanner
        # "dans nouveautés telegram et ebdz non pas le meme format. pas les memes infos
        # sur les volumes dans les 2" - même helper que Telegram (voir plus bas et
        # blueprints/telegram_channels/routes.py) pour un libellé "Tome N"/"Intégrale N"/
        # "HS N"/"Épisode N" cohérent, au lieu du seul numéro brut de tome (`volume`
        # ci-dessous), qui restait "—" pour une intégrale/HS/épisode alors que le scraper
        # avait bien identifié son propre numéro (is_integral/integral_number/etc., déjà
        # utilisés ici pour already_owned mais jamais renvoyés au frontend).
        from blueprints.missing_monitor.searcher import MissingVolumeSearcher

        def _title_match_key(title):
            return normalize_search_text(LibraryScanner.unscramble_trailing_article(ebdz_core_title(title)))

        library_conn = sqlite3.connect(current_app.config['DATABASE'], timeout=30.0)
        library_cursor = library_conn.cursor()
        library_cursor.execute("SELECT title FROM series")
        local_titles = {_title_match_key(row[0]) for row in library_cursor.fetchall() if row[0]}
        library_cursor.execute('''
            SELECT s.title FROM series s
            JOIN missing_volume_monitor mm ON mm.series_id = s.id
            WHERE mm.enabled = 1
        ''')
        monitored_titles = {_title_match_key(row[0]) for row in library_cursor.fetchall() if row[0]}
        library_cursor.execute("SELECT id, ebdz_thread_id, bedetheque_url, title FROM series")
        all_series_rows = library_cursor.fetchall()
        title_to_unambiguous_series = _build_nouveautes_title_match_index(all_series_rows, _title_match_key)
        thread_to_series_ids = {}
        thread_to_first_series = {}
        for series_id, thread_id, bedetheque_url, series_title in all_series_rows:
            if thread_id is None:
                continue
            thread_to_series_ids.setdefault(str(thread_id), []).append(series_id)
            thread_to_first_series.setdefault(str(thread_id), (series_id, bedetheque_url, series_title))
        matched_thread_ids = set(thread_to_series_ids.keys())

        # "compares les volumes existants avec ceux nouveau et si ceux de nouveautés sont
        # manquants ou non de la série" - une fois un thread identifié (déjà_matched_thread
        # ci-dessus), comparer en plus CHAQUE fichier du thread aux tomes/intégrales/HS/
        # épisodes déjà possédés (voir get_owned_volume_signatures), pas seulement dire que
        # la série existe. Un thread_id partagé par plusieurs séries (ex: "Avant + L'Incal
        # + Final") fusionne les signatures possédées des séries concernées (union) plutôt
        # que de n'en retenir qu'une arbitrairement.
        from blueprints.library.routes import get_owned_volume_signatures, volume_possession_status
        all_matched_series_ids = [sid for ids in thread_to_series_ids.values() for sid in ids]
        owned_by_series = get_owned_volume_signatures(all_matched_series_ids, conn=library_conn)
        owned_by_thread = {}
        for tid, series_ids in thread_to_series_ids.items():
            merged = {'volumes': set(), 'integrals': set(), 'hs': set(), 'episodes': set()}
            for sid in series_ids:
                for key, values in owned_by_series[sid].items():
                    merged[key] |= values
            owned_by_thread[tid] = merged
        library_conn.close()

        # is_new_thread a besoin de la date de première apparition de CHAQUE thread sur
        # tout l'historique, pas seulement dans la fenêtre `days` - sinon un sujet déjà vu
        # avant cette fenêtre mais qui reçoit un nouveau tome dedans serait à tort compté
        # "nouveau". Agrégation SQL (MIN groupé, utilise l'index sur thread_id) plutôt
        # qu'un passage Python sur toutes les lignes.
        cursor.execute('SELECT thread_id, MIN(date_scraped) FROM ed2k_links GROUP BY thread_id')
        first_seen_at = dict(cursor.fetchall())

        cursor.execute('SELECT EXISTS(SELECT 1 FROM ed2k_links WHERE date_scraped < ?)', (cutoff,))
        has_more = bool(cursor.fetchone()[0])

        # limit (voir plus haut) porte sur les LIENS bruts, pas les sujets/articles déjà
        # groupés plus bas - une approximation suffisante pour un premier rendu rapide,
        # remplacé par le second appel sans limite qui suit en arrière-plan côté frontend.
        limit_sql = ' LIMIT ?' if limit else ''
        cursor.execute('''
            SELECT thread_id, thread_title, thread_url, forum_category, cover_image,
                   link, filename, filesize, volume, description, date_scraped,
                   is_integral, integral_number, is_hs, hs_number, is_episode, episode_number
            FROM ed2k_links
            WHERE date_scraped >= ?
        ''' + (' AND date_scraped < ?' if upper_cutoff else '') + '''
            ORDER BY date_scraped DESC
        ''' + limit_sql, tuple(
            [cutoff] + ([upper_cutoff] if upper_cutoff else []) + ([limit] if limit else [])
        ))
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            return jsonify({'success': True, 'sessions': [], 'has_more': has_more})

        # Découpe l'ensemble des liens en sessions de scrape successives :
        # tant que l'écart entre deux dates de scrape consécutives reste
        # inférieur à SCRAPE_SESSION_GAP, elles appartiennent à la même session
        sessions_rows = []
        current_session = []
        previous_ts = None
        for row in rows:
            try:
                # fromisoformat plutôt que strptime: même résultat pour ce format
                # ('YYYY-MM-DD HH:MM:SS', sortie de CURRENT_TIMESTAMP), mêmes exceptions
                # (ValueError/TypeError) donc même comportement sur une valeur corrompue,
                # mais ~35x plus rapide en pratique sur cette table (mesuré : 0.5s vs
                # 0.014s pour 62k lignes) - strptime réinterprète la chaîne de format à
                # chaque appel, fromisoformat est un parseur C dédié. Cette boucle tourne
                # sur l'historique complet à chaque chargement de /ebdz-nouveautes.
                ts = datetime.fromisoformat(row['date_scraped'])
            except (ValueError, TypeError):
                continue
            if previous_ts is not None and (previous_ts - ts) > SCRAPE_SESSION_GAP:
                sessions_rows.append(current_session)
                current_session = []
            current_session.append(row)
            previous_ts = ts
        if current_session:
            sessions_rows.append(current_session)

        sessions = []
        for session_rows in sessions_rows:
            grouped = {}
            order = []
            for row in session_rows[:MAX_LINKS_PER_SESSION]:
                thread_id = row['thread_id']
                ts = row['date_scraped']
                if thread_id not in grouped:
                    # _title_match_key (voir plus haut): même fonction que celle qui a
                    # construit local_titles/monitored_titles, appliquée ici au
                    # thread_title EBDZ - sans elle, un thread comme "Grand vide, Le
                    # [Murawiec]" ne matchait jamais la série locale "Le grand vide
                    # (Murawiec)" (article en fin + suffixe différent des deux côtés).
                    normalized_core_title = _title_match_key(row['thread_title'] or '')
                    matched_series_id, matched_bedetheque_url, matched_series_title = thread_to_first_series.get(str(thread_id), (None, None, None))
                    # Le lien explicite de sujet EBDZ est prioritaire. Sans ce lien, un
                    # titre local unique (Carthago [auteurs] -> Carthago) est suffisamment
                    # sûr pour exposer les actions UI, mais n'écrit jamais ebdz_thread_id:
                    # l'utilisateur peut corriger le match depuis la fiche si nécessaire.
                    if matched_series_id is None:
                        title_match = title_to_unambiguous_series.get(normalized_core_title)
                        if title_match is not None:
                            matched_series_id, matched_bedetheque_url, matched_series_title = title_match
                    grouped[thread_id] = {
                        'thread_id': thread_id,
                        'title': row['thread_title'],
                        'url': row['thread_url'],
                        'category': row['forum_category'],
                        'cover_image': row['cover_image'],
                        'description': row['description'],
                        'already_in_library': normalized_core_title in local_titles,
                        'already_monitored': normalized_core_title in monitored_titles,
                        'already_matched_thread': str(thread_id) in matched_thread_ids,
                        'matched_series_id': matched_series_id,
                        'matched_bedetheque_url': matched_bedetheque_url,
                        # "sur nouveautés met en tooltip le nom de la serie sur la petit
                        # validation" - voir checkTooltip, ebdz-latest.js
                        'matched_series_title': matched_series_title,
                        'links': [],
                        '_min_ts': ts,
                    }
                    order.append(thread_id)
                elif ts < grouped[thread_id]['_min_ts']:
                    grouped[thread_id]['_min_ts'] = ts
                # already_owned: True/False/None (voir volume_possession_status) - None
                # (thread non matché, ou fichier sans numéro identifiable) reste distinct
                # de False (numéro identifié, mais absent de la série - manquant) pour ne
                # pas afficher "manquant" à tort là où on ne peut simplement rien affirmer.
                owned = owned_by_thread.get(str(thread_id))
                already_owned = (
                    volume_possession_status(
                        owned, volume=row['volume'], is_integral=bool(row['is_integral']),
                        integral_number=row['integral_number'], is_hs=bool(row['is_hs']),
                        hs_number=row['hs_number'], is_episode=bool(row['is_episode']),
                        episode_number=row['episode_number']
                    ) if owned is not None else None
                )
                grouped[thread_id]['links'].append({
                    'link': row['link'],
                    'filename': row['filename'],
                    'filesize': row['filesize'],
                    'volume': row['volume'],
                    'parsed_volume': MissingVolumeSearcher._parsed_volume_label(row['filename']),
                    'already_owned': already_owned
                })

            # is_new_thread: le tout premier lien jamais scrapé pour ce sujet tombe dans
            # CETTE session - un sujet dont on a déjà vu un tome lors d'une session
            # précédente n'est "que" une mise à jour (nouveau tome), pas un nouveau sujet.
            # missing_links_count: nombre de fichiers de CETTE session identifiés comme
            # manquants (already_owned is False) - résumé au niveau du thread pour ne pas
            # avoir à déplier chaque sujet pour savoir s'il y a quelque chose à récupérer
            # ("compares les volumes existants avec ceux nouveau et si ceux de nouveautés
            # sont manquants ou non de la série").
            for tid in order:
                grouped[tid]['is_new_thread'] = (grouped[tid].pop('_min_ts') == first_seen_at.get(tid))
                grouped[tid]['missing_links_count'] = sum(1 for l in grouped[tid]['links'] if l['already_owned'] is False)

            sessions.append({
                'scraped_at': session_rows[0][10],
                'total_new_links': len(session_rows),
                'total_new_threads': len({r[0] for r in session_rows}),
                'truncated': len(session_rows) > MAX_LINKS_PER_SESSION,
                'results': [grouped[tid] for tid in order]
            })

        return jsonify({
            'success': True,
            'total_sessions': len(sessions),
            'sessions': sessions,
            'has_more': has_more
        })

    except Exception as e:
        return jsonify({'success': False, 'error': 'Erreur interne'}), 500


@ebdz_bp.route('/nouveautes/new-count', methods=['GET'])
def nouveautes_new_count():
    """Nombre de nouveautés (EBDZ + Telegram) apparues depuis `?since=<ISO datetime>` -
    alimente le badge numérique du lien "Nouveautés" de la sidebar (voir initNouveautesBadge,
    nav.js), affiché sur TOUTE page (contrairement à /latest, payload complet réservé à la
    page Nouveautés elle-même). "affiche un numéro pour les nouveautés... comme pour
    import" - même principe que /api/import/pending-count, `since` vit côté client
    (localStorage, posé à la date du jour à chaque visite de /ebdz-nouveautes: "valider" =
    juste avoir vu la page). Approximatif par rapport au nombre de LIGNES affichées sur la
    page (un thread EBDZ ayant reçu 3 nouveaux tomes ne compte ici que comme 3 liens, pas
    comme 1 ligne groupée) - suffisant pour un badge, la page elle-même reste la source de
    vérité pour le détail."""
    try:
        since = request.args.get('since', '').strip()
        if not since:
            return jsonify({'success': True, 'count': 0})

        db_file = current_app.config['DB_FILE']
        conn = sqlite3.connect(db_file, timeout=30.0)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ed2k_links'")
        ebdz_count = 0
        if cursor.fetchone():
            cursor.execute('SELECT COUNT(DISTINCT thread_id) FROM ed2k_links WHERE date_scraped > ?', (since,))
            ebdz_count = cursor.fetchone()[0] or 0
        conn.close()

        telegram_count = 0
        try:
            from blueprints.telegram_channels.scraper import _connect_db
            tg_conn = _connect_db()
            tg_cursor = tg_conn.cursor()
            tg_cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='telegram_files'")
            if tg_cursor.fetchone():
                tg_cursor.execute('SELECT COUNT(*) FROM telegram_files WHERE message_date > ?', (since,))
                telegram_count = tg_cursor.fetchone()[0] or 0
            tg_conn.close()
        except Exception:
            pass

        return jsonify({'success': True, 'count': ebdz_count + telegram_count})
    except Exception as e:
        return jsonify({'success': False, 'error': 'Erreur interne'}), 500


@ebdz_bp.route('/auto-scrape/status', methods=['GET'])
def auto_scrape_status():
    """Récupérer le statut du scraping automatique"""
    try:
        from .scheduler import ebdz_scheduler
        
        is_running = False
        next_run = None
        
        if ebdz_scheduler.scheduler and ebdz_scheduler.scheduler.running:
            is_running = True
            job = ebdz_scheduler.scheduler.get_job(ebdz_scheduler.job_id)
            if job:
                next_run = job.next_run_time.isoformat() if job.next_run_time else None
        
        return jsonify({
            'success': True,
            'is_running': is_running,
            'next_run': next_run
        })
    except Exception as e:
        return jsonify({'success': False, 'error': 'Erreur interne'}), 500

def _annotate_rss_entries(events):
    """Ajoute aux entrées RSS les mêmes repères locaux que Nouveautés EBDZ/Telegram.

    Le RSS ne fournit pas d'identité de série persistée : on utilise uniquement une
    correspondance de titre locale non ambiguë, sans créer ni modifier de série. Le statut
    downloaded vient de l'historique des téléchargements réussis (source_link de la page
    RSS ou titre normalisé) et des téléchargements encore actifs.
    """
    from blueprints.search.routes import normalize_search_text, ebdz_core_title
    from blueprints.library.scanner import LibraryScanner
    from blueprints.bedetheque.catalog_index import search_catalog_index
    from blueprints.bedetheque.scraper import BedethequeScraper, matching_query_variants

    def query_variants(title):
        parsed = LibraryScanner.parse_filename(str(title or '')).get('title') or str(title or '')
        return matching_query_variants(parsed)

    def keys(title):
        raw = str(title or '').strip()
        parsed = LibraryScanner.parse_filename(raw).get('title') or raw
        variants = [raw, parsed]
        words = parsed.split()
        for suffix_count in range(1, len(words)):
            variants.append(' '.join(words[:-suffix_count]))
        return {
            normalize_search_text(LibraryScanner.unscramble_trailing_article(ebdz_core_title(value)))
            for value in variants if value
        }

    conn = sqlite3.connect(current_app.config['DATABASE'], timeout=30.0)
    rows = conn.execute('SELECT id, title, bedetheque_url FROM series').fetchall()
    monitored = {row[0] for row in conn.execute(
        'SELECT series_id FROM missing_volume_monitor WHERE enabled = 1'
    ).fetchall()}
    successful = conn.execute(
        'SELECT title, source_link FROM missing_volume_downloads WHERE success = 1'
    ).fetchall()
    active = conn.execute(
        "SELECT title, series_id, volume_number, is_integral, integral_number, is_hs, hs_number, is_episode, episode_number FROM active_downloads WHERE status NOT IN ('failed', 'cancelled')"
    ).fetchall()
    series_meta = {
        int(series_id): {'is_oneshot': bool(is_oneshot)}
        for series_id, is_oneshot in conn.execute('SELECT id, is_oneshot FROM series').fetchall()
    }
    owned_volume_rows = conn.execute(
        'SELECT series_id, volume_number, is_integral, integral_number, is_hs, hs_number, is_episode, episode_number '
        "FROM volumes WHERE filepath IS NOT NULL AND filepath != ''"
    ).fetchall()
    conn.close()

    by_title = {}
    for series_id, title, bedetheque_url in rows:
        title_key = normalize_search_text(LibraryScanner.unscramble_trailing_article(ebdz_core_title(title or '')))
        if not title_key:
            continue
        if title_key in by_title:
            by_title[title_key] = None  # titre ambigu: ne pas deviner
        else:
            by_title[title_key] = (series_id, title, bedetheque_url)

    # Le statut de téléchargement ne doit jamais reposer sur une variante de titre
    # raccourcie (par exemple "le"), sinon toute release commençant par cet article
    # devient un faux positif. L'historique est donc comparé uniquement au nom exact de
    # la release (avec sa forme parsée complète), puis au volume réellement possédé de
    # la série locale déjà matchée.
    def exact_keys(title):
        raw = str(title or '').strip()
        parsed = LibraryScanner.parse_filename(raw).get('title') or raw
        return {
            normalize_search_text(LibraryScanner.unscramble_trailing_article(value))
            for value in (raw, parsed) if value
        }

    downloaded_links = {link for _title, link in successful if link}
    downloaded_titles = set().union(*(exact_keys(title) for title, _link in successful)) if successful else set()
    active_records = list(active)

    def volume_signature(parsed):
        if parsed.get('is_integral'):
            return ('integral', parsed.get('integral_number'))
        if parsed.get('is_hs'):
            return ('hs', parsed.get('hs_number'))
        if parsed.get('is_episode'):
            return ('episode', parsed.get('episode_number'))
        if parsed.get('volume') is not None:
            return ('volume', parsed.get('volume'))
        return None

    owned_by_series = {}
    for row in owned_volume_rows:
        series_id, volume_number, is_integral, integral_number, is_hs, hs_number, is_episode, episode_number = row
        signatures = owned_by_series.setdefault(int(series_id), set())
        if is_integral:
            signatures.add(('integral', integral_number))
        elif is_hs:
            signatures.add(('hs', hs_number))
        elif is_episode:
            signatures.add(('episode', episode_number))
        elif volume_number is not None:
            signatures.add(('volume', volume_number))
        elif series_meta.get(int(series_id), {}).get('is_oneshot'):
            signatures.add(('oneshot', None))

    def event_volume_signature(event, series_id):
        if series_id is None:
            return None
        signature = volume_signature(LibraryScanner.parse_filename(str(event.get('title') or '')))
        if signature is None and series_meta.get(int(series_id), {}).get('is_oneshot'):
            return ('oneshot', None)
        return signature

    def is_active_volume_download(event, series_id):
        signature = event_volume_signature(event, series_id)
        if signature is None:
            return False
        for row in active_records:
            _title, active_series_id, volume_number, is_integral, integral_number, is_hs, hs_number, is_episode, episode_number = row
            if active_series_id is None or int(active_series_id) != int(series_id):
                continue
            active_signature = (
                ('integral', integral_number) if is_integral else
                ('hs', hs_number) if is_hs else
                ('episode', episode_number) if is_episode else
                ('volume', volume_number) if volume_number is not None else
                ('oneshot', None) if series_meta.get(int(series_id), {}).get('is_oneshot') else None
            )
            if active_signature == signature:
                return True
        return False

    catalog_cache = getattr(_annotate_rss_entries, '_catalog_cache', {})
    rss_overrides = _load_rss_match_overrides()
    series_by_id = {int(series_id): (series_id, title, url) for series_id, title, url in rows}

    def catalog_match(title):
        for query in query_variants(title):
            if query in catalog_cache:
                candidates = catalog_cache[query]
            else:
                candidates = search_catalog_index(query, limit=30) or []
                catalog_cache[query] = candidates
            confident = [candidate for candidate in candidates if BedethequeScraper._is_confident_series_match(query, candidate['title'], BedethequeScraper._match_score(query, candidate['title']))]
            if confident:
                return sorted(confident, key=lambda candidate: BedethequeScraper._match_score(query, candidate['title']), reverse=True)[0]
        return None

    annotated = []
    for event in events:
        event_keys = keys(event.get('title'))
        match = next((by_title.get(candidate) for candidate in event_keys if by_title.get(candidate) is not None), None)
        override_id = rss_overrides.get(event.get('link')) or rss_overrides.get(event.get('title'))
        if override_id is not None:
            try:
                match = series_by_id.get(int(override_id)) or match
            except (TypeError, ValueError):
                pass
        catalog = catalog_match(event.get('title')) if match is None else None
        exact_event_keys = exact_keys(event.get('title'))
        signature = event_volume_signature(event, match[0]) if match else None
        already_owned = (
            signature in owned_by_series.get(int(match[0]), set())
            if signature is not None else None
        )
        downloaded = (
            event.get('link') in downloaded_links
            or bool(exact_event_keys & downloaded_titles)
            or already_owned is True
            or any(
                bool(exact_event_keys & exact_keys(row[0]))
                or (match and match[0] == row[1] and is_active_volume_download(event, match[0]))
                for row in active_records
            )
        )
        annotated.append({
            **event,
            'already_in_library': match is not None,
            'already_owned': already_owned,
            'already_monitored': bool(match and match[0] in monitored),
            'series_id': match[0] if match else None,
            'series_title': match[1] if match else (catalog.get('title') if catalog else None),
            'bedetheque_title': catalog.get('title') if catalog else (match[1] if match else None),
            'bedetheque_url': match[2] if match else (catalog.get('url') if catalog else None),
            'downloaded': downloaded,
            '_rss_annotation_version': RSS_ANNOTATION_VERSION,
        })
    _annotate_rss_entries._catalog_cache = catalog_cache
    return annotated


_rss_persistent_cache_lock = threading.Lock()
RSS_PERSISTENT_CACHE_TTL_SECONDS = 300
RSS_PERSISTENT_CACHE_MAX_ENTRIES = 500
RSS_ANNOTATION_VERSION = 3


def _rss_persistent_cache_path():
    config_path = current_app.config['RSS_CONFIG_FILE']
    return os.path.join(os.path.dirname(config_path), 'rss_entries_cache.json')


def _load_rss_persistent_cache(path):
    try:
        with open(path, encoding='utf-8') as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return {}


def _update_rss_persistent_cache(path, update):
    """Merge against the latest cache under a thread AND process lock."""
    import fcntl
    import tempfile
    with _rss_persistent_cache_lock, open(path + '.lock', 'a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        cache = _load_rss_persistent_cache(path)
        update(cache)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                             dir=os.path.dirname(path), delete=False) as handle:
                temp_path = handle.name
                json.dump(cache, handle, ensure_ascii=False)
            os.replace(temp_path, path)
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
        return cache


_rss_feed_locks = {}


def _rss_entry_key(entry):
    """Return a stable identity for one release across successive RSS polls."""
    indexer = str(entry.get('prowlarr_indexer_id') or '')
    link = next((str(entry.get(field) or '').strip() for field in
                 ('link', 'page_link', 'comments_link') if entry.get(field)), '')
    if link:
        return f'{indexer}\x1f{link}'
    return '\x1f'.join((indexer, str(entry.get('title') or '').strip(),
                        str(entry.get('date') or '').strip()))


def _merge_rss_snapshot(previous, fresh_entries, fetched_at=None):
    """Merge a provider's recent window into the cache without reprocessing history.

    Prowlarr/Newznab cannot reliably query "since this publication".  We therefore
    read its bounded recent window, identify releases already seen, retain older cached
    rows, and carry forward their annotations.  Only genuinely new rows remain
    unannotated for ``rss_latest`` to process.
    """
    previous = previous if isinstance(previous, dict) else {}
    merged = []
    seen = set()
    for entry in list(fresh_entries or []) + list(previous.get('entries') or []):
        if not isinstance(entry, dict):
            continue
        key = _rss_entry_key(entry)
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(entry)
    merged.sort(key=lambda item: item.get('date', ''), reverse=True)
    merged = merged[:RSS_PERSISTENT_CACHE_MAX_ENTRIES]

    old_annotations = {
        _rss_entry_key(entry): entry
        for entry in previous.get('annotated_entries') or []
        if isinstance(entry, dict)
        and entry.get('_rss_annotation_version') == RSS_ANNOTATION_VERSION
    }
    preserved_annotations = []
    for entry in merged:
        annotated = old_annotations.get(_rss_entry_key(entry))
        if annotated is not None:
            # Provider fields may change while the release identity stays stable.
            preserved_annotations.append({**annotated, **entry})
    return {
        'fetched_at': time.time() if fetched_at is None else fetched_at,
        'entries': merged,
        'annotated_entries': preserved_annotations,
    }


def _refresh_rss_persistent_feed(feed, path):
    # Errors propagate to the response: never relabel stale data as freshly fetched.
    entries = fetch_feed(feed, force_refresh=True)
    key = feed_cache_key(feed)
    snapshot = None

    def merge(cache):
        nonlocal snapshot
        snapshot = _merge_rss_snapshot(cache.get(key), entries)
        cache[key] = snapshot

    _update_rss_persistent_cache(path, merge)
    return snapshot


def _read_rss_feed(feed, path, force_refresh=False):
    key = feed_cache_key(feed)
    requested_at = time.time()
    with _rss_persistent_cache_lock:
        lock = _rss_feed_locks.setdefault((path, key), threading.Lock())
    with lock:
        cached = _load_rss_persistent_cache(path).get(key) or {}
        fetched_at = float(cached.get('fetched_at') or 0)
        usable = isinstance(cached.get('entries'), list)
        # Concurrent requests share the successful refresh that completed while waiting.
        if usable and ((not force_refresh and time.time() - fetched_at < RSS_PERSISTENT_CACHE_TTL_SECONDS)
                       or fetched_at >= requested_at):
            return feed, cached, None
        try:
            return feed, _refresh_rss_persistent_feed(feed, path), None
        except Exception:
            # Do not expose provider URLs/API credentials in client-visible exceptions.
            return feed, cached if usable else {'entries': []}, 'Actualisation RSS impossible; dernières données disponibles conservées.'


def _rss_match_overrides_path():
    return os.path.join(os.path.dirname(_rss_persistent_cache_path()), 'rss_series_matches.json')


def _load_rss_match_overrides():
    try:
        with open(_rss_match_overrides_path(), encoding='utf-8') as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return {}


def _visible_rss_entries(entries, blocked_extensions):
    """Apply the search format exclusions to cached RSS releases at display time."""
    from blueprints.missing_monitor.searcher import _blocked_extension_pattern

    patterns = [_blocked_extension_pattern(ext) for ext in blocked_extensions]
    if not patterns:
        return entries
    return [entry for entry in entries if not any(
        pattern.search(str(entry.get(field) or ''))
        for field in ('filename', 'title') for pattern in patterns
    )]


@ebdz_bp.route('/rss/match-override', methods=['POST'])
def rss_match_override():
    data = request.get_json(silent=True) or {}
    identity = str(data.get('link') or data.get('title') or '').strip()
    try:
        series_id = int(data.get('series_id'))
    except (TypeError, ValueError):
        series_id = None
    if not identity or series_id is None:
        return jsonify({'success': False, 'error': 'Lien/titre et série requis'}), 400
    overrides = _load_rss_match_overrides()
    overrides[identity] = series_id
    path = _rss_match_overrides_path()
    temp = path + '.tmp'
    with open(temp, 'w', encoding='utf-8') as handle:
        json.dump(overrides, handle, ensure_ascii=False)
    os.replace(temp, path)
    # Invalider les annotations persistées : le prochain rendu doit refléter
    # immédiatement le nouveau rattachement manuel.
    cache_path = _rss_persistent_cache_path()
    def invalidate(cache):
        for cached in cache.values():
            if isinstance(cached, dict):
                cached.pop('annotated_entries', None)
    _update_rss_persistent_cache(cache_path, invalidate)
    return jsonify({'success': True})


@ebdz_bp.route('/rss/latest', methods=['GET'])
def rss_latest():
    from blueprints.library.routes import load_library_import_config

    blocked_extensions = load_library_import_config().get('blocked_search_extensions', [])
    feeds = [feed for feed in load_feeds(current_app.config['RSS_CONFIG_FILE']) if feed.get('enabled', True)]
    prepared_feeds = []
    for feed in feeds:
        if feed.get('provider') == 'prowlarr' and feed.get('indexer_ids') is None:
            configured = load_prowlarr_config()
            configured_ids = configured.get('rss_indexers')
            if configured_ids is not None:
                feed = {**feed, 'indexer_ids': configured_ids, 'category_ids': configured.get('selected_categories') or {}}
        prepared_feeds.append(feed)
    feeds = prepared_feeds
    cache_path = _rss_persistent_cache_path()
    force_refresh = request.args.get('refresh') == '1'
    limit = min(max(request.args.get('limit', default=100, type=int), 1), 500)
    events, errors, feed_states = [], [], []
    def read(feed):
        return _read_rss_feed(feed, cache_path, force_refresh)
    with ThreadPoolExecutor(max_workers=min(5, max(1, len(feeds)))) as executor:
        snapshots = list(executor.map(read, feeds))
    for feed, cached, error in snapshots:
        name = feed.get('name') or 'RSS'
        key = feed_cache_key(feed)
        if error:
            errors.append({'name': name, 'error': error})
        feed_states.append({'name': name, 'fetched_at': cached.get('fetched_at'), 'stale': bool(error)})
        # Select the requested recent rows before matching against the local library.
        # A Prowlarr poll may contain hundreds of releases across its indexers, while
        # the client initially requests only 100.  Previously all rows were enriched
        # before this slice, making a small incremental refresh take minutes.
        requested_entries = sorted(_visible_rss_entries(cached.get('entries') or [], blocked_extensions),
                                   key=lambda item: item.get('date', ''),
                                   reverse=True)[:limit]
        annotated_by_key = {
            _rss_entry_key(item): item
            for item in cached.get('annotated_entries') or []
            if isinstance(item, dict)
            and item.get('_rss_annotation_version') == RSS_ANNOTATION_VERSION
        }
        missing = [entry for entry in requested_entries
                   if _rss_entry_key(entry) not in annotated_by_key]
        if missing:
            new_annotations = _annotate_rss_entries([
                {**entry, 'type': 'rss', 'feed_name': name} for entry in missing
            ])
            annotated_by_key.update({_rss_entry_key(item): item for item in new_annotations})

            def save_annotations(latest):
                current = latest.get(key)
                # Never restore old entries after another request has refreshed this feed.
                if isinstance(current, dict) and current.get('fetched_at') == cached.get('fetched_at'):
                    combined = {
                        _rss_entry_key(item): item
                        for item in current.get('annotated_entries') or []
                        if isinstance(item, dict)
                        and item.get('_rss_annotation_version') == RSS_ANNOTATION_VERSION
                    }
                    combined.update({_rss_entry_key(item): item for item in new_annotations})
                    current['annotated_entries'] = [
                        {**combined[_rss_entry_key(entry)], **entry}
                        for entry in current.get('entries') or []
                        if _rss_entry_key(entry) in combined
                    ]
            _update_rss_persistent_cache(cache_path, save_annotations)
        entries = [
            {**annotated_by_key[_rss_entry_key(entry)], **entry}
            for entry in requested_entries
            if _rss_entry_key(entry) in annotated_by_key
        ]
        events.extend({**entry, 'type': 'rss', 'feed_name': name} for entry in entries)
    events.sort(key=lambda item: item.get('date', ''), reverse=True)
    return jsonify({'success': True, 'entries': events, 'errors': errors, 'feeds': feed_states})
