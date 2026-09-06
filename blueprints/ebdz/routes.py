"""
Routes pour le scraper ebdz.net
"""
from flask import request, jsonify, current_app
from . import ebdz_bp
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from encryption import encrypt, decrypt, ensure_encryption_key

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
            return jsonify({'success': False, 'error': str(e)}), 500


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
        return jsonify({'success': False, 'error': str(e)}), 500


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
            return jsonify({'success': False, 'error': str(e)}), 500


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

        # Une seule fonction pour calculer la clé de comparaison "already_in_library",
        # appliquée IDENTIQUEMENT au titre local et au thread_title EBDZ (voir son usage
        # plus bas) - ebdz_core_title retire un éventuel suffixe entre parenthèses/
        # crochets (désambiguateur d'auteur, ex: "(Murawiec)"/"[Murawiec]") avant
        # unscramble_trailing_article (article "Le/La/Les/L'" ramené en tête, ex: "Grand
        # vide, Le" -> "Le Grand vide"). Traiter les deux titres avec la MÊME fonction
        # plutôt que d'appliquer ces étapes séparément de chaque côté (ce qui a été
        # tenté puis restait faux pour "Le grand vide (Murawiec)": le suffixe local
        # "(Murawiec)" n'était jamais retiré alors que le suffixe EBDZ "[Murawiec]"
        # l'était) est ce qui garantit que les deux titres finissent bien comparables.
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
        library_cursor.execute("SELECT id, ebdz_thread_id, bedetheque_url, title FROM series WHERE ebdz_thread_id IS NOT NULL")
        thread_to_series_ids = {}
        thread_to_first_series = {}
        for series_id, thread_id, bedetheque_url, series_title in library_cursor.fetchall():
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
            ORDER BY date_scraped DESC
        ''' + limit_sql, (cutoff, limit) if limit else (cutoff,))
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
        return jsonify({'success': False, 'error': str(e)}), 500


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
        return jsonify({'success': False, 'error': str(e)}), 500


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
        return jsonify({'success': False, 'error': str(e)}), 500