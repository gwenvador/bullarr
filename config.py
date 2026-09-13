"""
Configuration centralisée de l'application
"""
import os
import sqlite3

class Config:
    """Configuration de base"""
    
    # Flask
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-in-production'
    # Emergency recovery switch for a misconfigured OIDC/password setup. Keep false in normal use.
    AUTH_BYPASS_LOGIN = os.environ.get('BULLARR_AUTH_BYPASS_LOGIN', 'false').strip().lower() in {'1', 'true', 'yes', 'on'}
    DEBUG = False
    
    # Chemins
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))
    DATA_DIR = os.path.join(BASE_DIR, 'data')
    COVERS_DIR = os.path.join(DATA_DIR, 'covers')
    
    # Bases de données
    DATABASE = os.path.join(DATA_DIR, 'bullarr.db')
    # Local persistent handoff: remote/download sources are copied into a `.partial`
    # directory and atomically renamed `.ready` before validation/conversion.
    IMPORT_STAGING_DIRECTORY = os.path.join(DATA_DIR, 'import-staging')
    DB_FILE = os.path.join(DATA_DIR, 'ebdz.db')
    
    # Fichiers de configuration
    CONFIG_FILE = os.path.join(DATA_DIR, 'emule_config.json')
    EBDZ_CONFIG_FILE = os.path.join(DATA_DIR, 'ebdz_config.json')
    PROWLARR_CONFIG_FILE = os.path.join(DATA_DIR, 'prowlarr_config.json')
    KOMGA_CONFIG_FILE = os.path.join(DATA_DIR, 'komga_config.json')
    OIDC_CONFIG_FILE = os.path.join(DATA_DIR, 'oidc_config.json')
    MISSING_MONITOR_CONFIG_FILE = os.path.join(DATA_DIR, 'missing_monitor_config.json')
    LIBRARY_IMPORT_CONFIG_FILE = os.path.join(DATA_DIR, 'library_import_config.json')
    RENAME_CONFIG_FILE = os.path.join(DATA_DIR, 'rename_config.json')
    TELEGRAM_CONFIG_FILE = os.path.join(DATA_DIR, 'telegram_config.json')
    TELEGRAM_CHANNELS_CONFIG_FILE = os.path.join(DATA_DIR, 'telegram_channels_config.json')
    # État interne (pas une config utilisateur) qui retient quels fichiers d'import ont
    # déjà déclenché une notification "import disponible" - voir scheduler.py
    IMPORT_NOTIFY_STATE_FILE = os.path.join(DATA_DIR, 'import_notify_state.json')
    
    # eMule/aMule par défaut
    # Adresse modifiable depuis Configuration → aMule.
    AMULE_HOST = '127.0.0.1'
    EMULE_CONFIG = {
        'enabled': False,
        'type': 'amule',
        'host': AMULE_HOST,
        'port': 4711,
        'ec_port': 4712,
        'password': ''
    }
    
    # ebdz.net par défaut
    EBDZ_CONFIG = {
        'username': '',
        'password': '',
        'forums': [],
        'auto_scrape_enabled': False,
        'auto_scrape_interval': 60,  # en minutes
        'auto_scrape_interval_unit': 'minutes'  # 'minutes', 'hours', 'days'
    }
    
    # Prowlarr par défaut
    PROWLARR_CONFIG = {
        'enabled': False,
        'url': 'http://127.0.0.1',
        'port': 9696,
        'api_key': '',
        'selected_indexers': []
    }
    
    # Komga par défaut
    KOMGA_CONFIG = {
        'enabled': False,
        'url': 'http://127.0.0.1:25600',
        'api_key': ''
    }

    # Telegram par défaut - branché à deux événements pour l'instant (voir
    # blueprints/telegram/, blueprints/library/routes.py _notify_import_completed et
    # blueprints/library/scheduler.py _check_new_files_available), chacun activable
    # indépendamment une fois "enabled" à True.
    TELEGRAM_CONFIG = {
        'enabled': False,
        'bot_token': '',
        'chat_id': '',
        'notify_import_completed': True,
        'notify_import_available': True
    }

    # Compte Telegram "utilisateur" (MTProto, via Telethon) pour lire le CONTENU des
    # canaux publics (ex: @bd_frBE) - distinct de TELEGRAM_CONFIG ci-dessus qui ne fait
    # qu'ENVOYER des notifications via un bot (l'API Bot ne peut pas lire l'historique
    # d'un canal dont on n'est pas administrateur). api_id/api_hash s'obtiennent
    # gratuitement sur https://my.telegram.org - phone/session ne sont jamais renseignés
    # ici, seulement écrits une fois la connexion établie (voir blueprints/telegram_channels/).
    TELEGRAM_CHANNELS_CONFIG = {
        'api_id': None,
        'api_hash': '',
        'phone': '',
        'session': '',
        'connected': False,
        'username': '',
        'first_name': '',
        'channels': []
    }

    # SSO / OIDC par défaut (désactivé : aucun changement de comportement tant que
    # l'utilisateur ne configure pas son fournisseur dans les paramètres)
    OIDC_CONFIG = {
        'mode': 'none',
        'enabled': False,
        'username': '',
        'password_hash': '',
        'issuer': '',
        'client_id': '',
        'client_secret': '',
        'scopes': 'openid profile email'
    }

    # qBittorrent par défaut
    QBITTORRENT_CONFIG_FILE = os.path.join(DATA_DIR, 'qbittorrent_config.json')
    QBITTORRENT_CONFIG = {
        'enabled': False,
        'url': 'http://127.0.0.1',
        'port': 8080,
        'username': '',
        'password': ''
    }

    # rTorrent par défaut - pas de Web UI/REST natif, piloté en XML-RPC via une passerelle
    # HTTP (rutorrent, xmlrpc-c scgi_to_http, nginx devant le socket SCGI...), d'où
    # rpc_path plutôt qu'un simple port d'API comme les autres clients
    RTORRENT_CONFIG_FILE = os.path.join(DATA_DIR, 'rtorrent_config.json')
    RTORRENT_CONFIG = {
        'enabled': False,
        'url': 'http://127.0.0.1',
        'port': 80,
        'rpc_path': '/RPC2',
        'username': '',
        'password': ''
    }

    # Deluge par défaut - authentification par mot de passe seul (JSON-RPC de la Web UI),
    # pas de nom d'utilisateur contrairement aux autres clients
    DELUGE_CONFIG_FILE = os.path.join(DATA_DIR, 'deluge_config.json')
    DELUGE_CONFIG = {
        'enabled': False,
        'url': 'http://127.0.0.1',
        'port': 8112,
        'password': ''
    }

    # fourtoutici.cc (item #24 improvement.txt): source publique sans identifiants (pas
    # d'URL/clé API à saisir comme Prowlarr/Komga, pas de compte comme Telegram) - juste
    # une bascule pour pouvoir la désactiver si besoin, activée par défaut ("j'ai désactivé
    # prowlarr mais il s'affiche toujours..." - même bascule que les autres sources, mais
    # celle-ci n'a rien d'autre à configurer).
    FOURTOUTICI_CONFIG_FILE = os.path.join(DATA_DIR, 'fourtoutici_config.json')
    FOURTOUTICI_CONFIG = {
        'enabled': True,
        # Modifiable depuis Configuration > Indexeurs > Web ("je voudrais modifier
        # manuellement l'adresse de fourtoutici") - le site change parfois de domaine/
        # miroir, sans quoi il faudrait modifier scraper.py et redéployer à chaque fois.
        'base_url': 'https://fourtoutici.cc'
    }

    ANNAS_ARCHIVE_CONFIG_FILE = os.path.join(DATA_DIR, 'annas_archive_config.json')
    ANNAS_ARCHIVE_CONFIG = {
        'enabled': True,
        'base_url': 'https://annas-archive.gl'
    }

    # Shelfmark is an external download worker. Credentials are supplied through
    # environment variables, never committed to the repository.
    SHELFMARK_CONFIG_FILE = os.path.join(DATA_DIR, 'shelfmark_config.json')
    SHELFMARK_CONFIG = {
        'enabled': False,
        'base_url': '',
        'username': '',
        'password': '',
    }

    # Répertoires surveillés pour l'import (toujours scannés/monitorés ensemble), regroupés
    # sous /downloads/<source> ("create a download folder with the 3 options torrents
    # amule telegram") - chacun sa propre source hôte (voir docker-compose.yml), séparés
    # pour que l'attribution du client d'origine (scan_import_directory) ne soit jamais
    # ambiguë entre aMule et Telegram, contrairement à quand ils partageaient '/amule'.
    # fourtoutici (item #24 improvement.txt): source à téléchargement HTTP direct, comme
    # Telegram - son propre répertoire dédié pour que scan_import_directory l'attribue au
    # bon client plutôt que de le confondre avec Telegram (voir AMULE_IMPORT_DIRECTORY/
    # TELEGRAM_IMPORT_DIRECTORY ci-dessous, même raisonnement).
    IMPORT_DIRECTORIES = ['/downloads/amule', '/downloads/torrents', '/downloads/telegram', '/downloads/fourtoutici', '/downloads/shelfmark']

    # Répertoires cibles dédiés par source, utilisés là où le code doit écrire dans UN
    # répertoire précis plutôt que parcourir IMPORT_DIRECTORIES (voir
    # download_telegram_file, blueprints/telegram_channels/routes.py, et
    # scan_import_directory, blueprints/library/routes.py, pour l'attribution du client).
    AMULE_IMPORT_DIRECTORY = '/downloads/amule'
    TELEGRAM_IMPORT_DIRECTORY = '/downloads/telegram'
    FOURTOUTICI_IMPORT_DIRECTORY = '/downloads/fourtoutici'
    SHELFMARK_IMPORT_DIRECTORY = '/downloads/shelfmark'

    # Configuration d'import automatique
    LIBRARY_IMPORT_CONFIG = {
        'auto_import_enabled': False,
        'auto_assign_enabled': True,
        # 'move' keeps the historical behavior; 'hardlink' preserves the source.
        'import_mode': 'move',
        'auto_assign_rules': [],  # Liste des règles d'auto-assignation
        # Plus de fréquence configurable ("tu peux directement demander une importation
        # automatique quand le fichier est téléchargé et bien matché. Pas besoin de cette
        # fréquence") - Telegram déclenche l'import immédiatement à la fin du
        # téléchargement (voir attempt_immediate_auto_import, library/routes.py); le
        # scheduler périodique restant (aMule/qBittorrent/rTorrent/Deluge, voir
        # AUTO_IMPORT_FALLBACK_INTERVAL_MINUTES côté library/scheduler.py) tourne à un
        # intervalle fixe non exposé aux réglages.
        # Extensions surveillées dans /amule et /torrents (scan manuel /import ET import
        # automatique) - "ajoute une option dans les settings sur les fichiers à
        # monitorer pour l'import". Toutes activées par défaut = comportement identique
        # à avant que ce ne soit configurable.
        'monitored_extensions': ['.cbz', '.cbr', '.zip', '.rar', '.tar', '.pdf'],
        # "je ne veux pas avoir epub etre download. ajoute une section pour desactiver
        # les extensions qui peuvent etre affiche et download" - contrairement à
        # monitored_extensions ci-dessus (ce que l'import lit sur DISQUE), ce réglage
        # exclut des extensions AVANT même qu'un résultat de recherche EBDZ/Prowlarr/
        # Telegram/fourtoutici/Anna's Archive n'atteigne l'utilisateur ou l'acquisition
        # automatique (voir _deduplicate_and_rank, missing_monitor/searcher.py -
        # POINT D'ENTRÉE UNIQUE partagé par la recherche manuelle ET automatique). EPUB
        # activé par défaut : un ebook texte n'est jamais le bon fichier pour une BD,
        # quel que soit son score de correspondance de titre - déjà téléchargé pour rien
        # au moins une fois avant ce réglage (voir l'historique de
        # _NON_COMIC_EXTENSIONS_RE/_is_non_comic_format, bedetheque/auto_acquire.py).
        'blocked_search_extensions': ['epub'],
        # "met une option pour automatiquement convertir pour cbz dans l'import
        # automatique. si c'est desactivé l'utilisateur doit manuellement convertir" -
        # True par défaut pour rester identique au comportement précédent (la conversion
        # cbr->cbz à l'import était jusqu'ici inconditionnelle, voir execute_import/
        # execute_auto_import). Couvre aussi pdf/zip nu désormais (voir
        # _convert_import_file_to_cbz), auparavant jamais convertis automatiquement à
        # l'import (seulement via le bouton "Convertir en CBZ" de /import).
        'auto_convert_to_cbz': True,
        # "au lieu de faire une recherche pour chaque volume fait une recherche pour la
        # serie entiere si il y a l'option pack active" - une seule recherche large pour
        # toute la série plutôt qu'une par tome manquant, à la recherche d'un "pack" (un
        # seul fichier qui regroupe plusieurs tomes, ex. "T01 à T14") : "plus simple pour
        # des grosses séries". Sélection PAR TAILLE DE PACK (nombre de tomes couverts)
        # d'abord, seeds en repli - "peu importe la preference des sources... choisi
        # celui qui a le plus de volumes et qui a des seeds disponibles" - l'ordre de
        # auto_acquire_sources ne s'applique volontairement PAS à ce choix (voir
        # _best_pack_result, auto_acquire.py), seulement au mode tome par tome ci-dessous
        # (utilisé aussi comme repli si aucun pack n'est trouvé). False par défaut - une
        # recherche automatique ne doit pas se mettre à télécharger en masse tant que
        # l'utilisateur n'a pas explicitement activé ce réglage.
        'auto_acquire_pack_search_enabled': False,
        # Ordre = priorité (voir _deduplicate_and_rank/source_order, blueprints/
        # missing_monitor/searcher.py) - une source absente de cette liste n'est pas
        # cherchée du tout, pas seulement dépriorisée. Pas de réglage dédié pour cette
        # liste ("l'ordre des sources c'est déjà dans recherche, supprime cette partie de
        # import" - pas de 2ème réglage dupliqué) : synchronisée depuis Configuration >
        # Recherche > Ordre des sources à chaque sauvegarde là-bas (voir
        # saveSearchSourcePriority, static/js/settings.js), qui ne retire jamais de
        # source (seulement un réordonnancement) - reste donc en pratique toujours les 4.
        'auto_acquire_sources': ['prowlarr', 'ebdz', 'telegram', 'fourtoutici']
    }
    
    @staticmethod
    def init_app(app):
        """Initialise les répertoires et la base de données"""
        os.makedirs(Config.DATA_DIR, exist_ok=True)
        os.makedirs(Config.COVERS_DIR, exist_ok=True)
        
        # Initialiser la base de données si elle n'existe pas
        Config._init_database(Config.DATABASE)
    
    @staticmethod
    def _init_database(db_path):
        """Initialise les tables de la base de données SQLite"""
        import sqlite3
        
        conn = sqlite3.connect(db_path, timeout=30.0)
        cursor = conn.cursor()
        
        # Activer le mode WAL (Write-Ahead Logging) pour de meilleures performances concurrentes
        cursor.execute('PRAGMA journal_mode=WAL')
        
        # Table des bibliothèques
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS libraries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                path TEXT NOT NULL,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_scanned TIMESTAMP
            )
        ''')
        
        # Table des séries
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS series (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                library_id INTEGER,
                title TEXT NOT NULL,
                path TEXT,
                total_volumes INTEGER,
                missing_volumes TEXT,
                has_parts INTEGER DEFAULT 0,
                last_scanned TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (library_id) REFERENCES libraries(id) ON DELETE CASCADE
            )
        ''')
        
        # Table des volumes
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS volumes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id INTEGER,
                part_number INTEGER,
                part_name TEXT,
                volume_number INTEGER,
                filename TEXT,
                filepath TEXT,
                author TEXT,
                year INTEGER,
                resolution TEXT,
                file_size INTEGER,
                page_count INTEGER,
                format TEXT,
                FOREIGN KEY (series_id) REFERENCES series(id) ON DELETE CASCADE
            )
        ''')
        
        conn.commit()

        # Ajouter les colonnes additionnelles (tags, one-shot) si elles n'existent pas
        Config._add_series_extra_columns(conn, db_path)

        # Ajouter les tables de surveillance des volumes manquants
        Config._add_missing_monitor_tables(conn, db_path)

        conn.close()

        # Ajouter les colonnes Bedetheque si elles n'existent pas: fait au démarrage plutôt
        # que d'attendre le premier appel à une route bedetheque (BedethequeDatabase.__init__
        # les crée aussi, mais paresseusement) pour que GET /api/series/<id> puisse lire
        # series.bedetheque_url dès le premier lancement, même sans avoir jamais matché
        # manuellement de série sur Bedetheque
        from blueprints.bedetheque.scraper import BedethequeDatabase
        BedethequeDatabase(db_path)

    @staticmethod
    def _add_series_extra_columns(conn, db_path):
        """Ajoute les colonnes tags et one-shot à la table series si elles n'existent pas"""
        cursor = conn.cursor()

        extra_columns = [
            ('tags', 'TEXT'),  # JSON array de tags
            ('is_oneshot', 'INTEGER DEFAULT 0')  # 1 = one-shot (pas de volumes)
        ]

        # Vérifier quelles colonnes existent
        cursor.execute("PRAGMA table_info(series)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        # Ajouter les colonnes manquantes
        for col_name, col_type in extra_columns:
            if col_name not in existing_columns:
                try:
                    cursor.execute(f"ALTER TABLE series ADD COLUMN {col_name} {col_type}")
                except sqlite3.OperationalError as e:
                    if 'already exists' not in str(e):
                        print(f"⚠️  Impossible d'ajouter {col_name}: {e}")

        conn.commit()

    @staticmethod
    def _add_missing_monitor_tables(conn, db_path):
        """Crée les tables de surveillance des volumes manquants si elles n'existent pas"""
        cursor = conn.cursor()
        
        # Table de surveillance des bibliothèques
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS missing_volume_library (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                library_id INTEGER UNIQUE,
                enabled INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (library_id) REFERENCES libraries(id) ON DELETE CASCADE
            )
        ''')
        
        # Table de surveillance des séries
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS missing_volume_monitor (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id INTEGER UNIQUE,
                enabled INTEGER DEFAULT 1,
                search_sources TEXT DEFAULT '["ebdz", "prowlarr"]',
                auto_download_enabled INTEGER DEFAULT 0,
                last_checked TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (series_id) REFERENCES series(id) ON DELETE CASCADE
            )
        ''')
        
        # Table d'historique des téléchargements automatiques
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS missing_volume_downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                volume_number INTEGER,
                client TEXT,
                success INTEGER,
                message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # "dans historique il faudrait voir quelle est la source du téléchargement et
        # cliquable aussi" - source (ebdz/prowlarr/telegram/fourtoutici) et lien vers la
        # PAGE de la release (fil de forum EBDZ, page de release Prowlarr, message
        # Telegram) - même distinction lien-page-source/lien-de-téléchargement-direct déjà
        # établie côté résultats de recherche (voir _searchResultSourceLinkUrl,
        # static/js/search-results-table.js), réutilisée ici telle quelle plutôt que
        # recalculée. Colonnes ajoutées défensivement (pas de cadre de migration dans
        # cette appli, voir CLAUDE.md).
        cursor.execute("PRAGMA table_info(missing_volume_downloads)")
        existing_download_columns = {row[1] for row in cursor.fetchall()}
        for col_name in ('source', 'source_link'):
            if col_name not in existing_download_columns:
                try:
                    cursor.execute(f"ALTER TABLE missing_volume_downloads ADD COLUMN {col_name} TEXT")
                except sqlite3.OperationalError as e:
                    if 'already exists' not in str(e):
                        print(f"⚠️  Impossible d'ajouter {col_name} à missing_volume_downloads: {e}")

        # "once the download is initiated it should be in the database" - jusqu'ici une
        # ligne n'apparaissait dans missing_volume_downloads (donc dans l'onglet
        # "Téléchargements" d'Historique) qu'une fois le téléchargement TERMINÉ (succès ou
        # échec, voir log_manual_download) - rien entre le clic "Ajouter" et la fin, parfois
        # plusieurs minutes (Telegram). tracking_id relie désormais une ligne posée
        # immédiatement (success NULL = "en cours", voir mark_download_pending côté
        # downloader.py) à la même ligne active_downloads, pour qu'un appel de complétion
        # la METTE À JOUR (au lieu d'en insérer une seconde) une fois le résultat connu.
        if 'tracking_id' not in existing_download_columns:
            try:
                cursor.execute("ALTER TABLE missing_volume_downloads ADD COLUMN tracking_id INTEGER")
            except sqlite3.OperationalError as e:
                if 'already exists' not in str(e):
                    print(f"⚠️  Impossible d'ajouter tracking_id à missing_volume_downloads: {e}")

        # "when adding a new file whatever source. it should be automatically added to
        # import. then you can poll to get its status" - un téléchargement fraîchement
        # lancé (aMule/Deluge/rTorrent/qBittorrent/Telegram) doit apparaître sur /import
        # IMMÉDIATEMENT, pas seulement une fois découvert par le sondage périodique du
        # client ou par le scan du répertoire une fois le fichier écrit. Table dédiée
        # plutôt que de surcharger missing_volume_downloads (qui reste le journal
        # Historique, une ligne = un événement final) : ici une ligne = un téléchargement
        # en cours, retirée (voir get_pending_downloads côté downloader.py) dès que le
        # fichier correspondant a été importé avec succès, ou après expiration.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS active_downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                client TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            )
        ''')

        # "le volume/album doit être matché si le clic vient d'une fiche série" - jusqu'ici
        # active_downloads n'avait qu'un `title` texte, retrouvé plus tard par matching flou
        # (voir _filenames_match côté downloader.py) aussi bien pour savoir "est-ce déjà
        # importé" que pour deviner la série de destination au moment du scan. Quand le
        # téléchargement est
        # lancé depuis une fiche série (searchMissingVolume côté library.js), la série/le
        # tome sont déjà connus avec certitude à cet instant précis - les conserver ici
        # évite d'avoir à les re-deviner depuis un nom de fichier de release ambigu une fois
        # le fichier arrivé sur disque. Nullable: une recherche libre depuis /search n'a
        # aucun contexte série/tome à proposer. Colonnes ajoutées défensivement (pas de
        # cadre de migration dans cette appli, voir CLAUDE.md) plutôt que recréer la table.
        cursor.execute("PRAGMA table_info(active_downloads)")
        existing_columns = {row[1] for row in cursor.fetchall()}
        # volume_number: le numéro de tome (int simple), connu même quand volume_id est
        # NULL (tome manquant, pas encore de ligne `volumes` - voir commentaire ci-dessus) -
        # sert à afficher/assigner le bon tome sans avoir à le re-parser depuis le nom de
        # fichier une fois le téléchargement arrivé sur disque.
        for col_name, col_type in [('series_id', 'INTEGER'), ('volume_id', 'INTEGER'), ('volume_number', 'INTEGER')]:
            if col_name not in existing_columns:
                try:
                    cursor.execute(f"ALTER TABLE active_downloads ADD COLUMN {col_name} {col_type}")
                except sqlite3.OperationalError as e:
                    if 'already exists' not in str(e):
                        print(f"⚠️  Impossible d'ajouter {col_name} à active_downloads: {e}")

        # "import avec telegram essaie de monitorer le status de telechargement" - un
        # téléchargement Telegram (thread de CETTE app, voir download_channel_file_background
        # côté telegram_channels/scraper.py) n'avait jusqu'ici qu'un état binaire "en
        # attente"/"terminé" (voir active_downloads ci-dessus), contrairement à qBittorrent/
        # rTorrent/Deluge/aMule qui exposent une vraie progression en octets via leur API -
        # bytes_downloaded/bytes_total permettent la même barre de progression pour
        # Telegram (voir update_download_progress côté downloader.py, alimenté par le
        # progress_callback de Telethon), plutôt qu'un simple "⏳ En attente..." pour un
        # fichier qui peut prendre plusieurs minutes.
        for col_name, col_type in [('bytes_downloaded', 'INTEGER'), ('bytes_total', 'INTEGER')]:
            if col_name not in existing_columns:
                try:
                    cursor.execute(f"ALTER TABLE active_downloads ADD COLUMN {col_name} {col_type}")
                except sqlite3.OperationalError as e:
                    if 'already exists' not in str(e):
                        print(f"⚠️  Impossible d'ajouter {col_name} à active_downloads: {e}")

        # "il ne faut pas qu'il y ait plus de pending que ceux qui sont vraiment en
        # pending" - un item lie via client_item_id (voir set_active_download_client_
        # item_id, activity/routes.py) qui n'apparait plus dans le sondage du client
        # (torrent supprime/annule cote amule/qbittorrent/etc.) doit finir par
        # transitionner hors de 'pending', mais JAMAIS sur un age quelconque (purge 6h
        # deja retiree par le passe pour cette raison meme). client_absence_streak
        # compte les sondages CONSECUTIFS ou l'item lie etait attendu mais absent - un
        # signal d'activite reel (le client a ete interroge et n'a pas repondu cet
        # item), remis a zero des qu'il reapparait ne serait-ce qu'une fois. Seul un
        # nombre suffisant de sondages consecutifs (pas un seul, pour tolerer un
        # redemarrage temporaire du client ou un sondage manque) declenche la
        # transition vers 'failed' (voir CLIENT_ABSENCE_FAILURE_THRESHOLD,
        # activity/routes.py).
        for col_name, col_type in [('client_absence_streak', 'INTEGER DEFAULT 0')]:
            if col_name not in existing_columns:
                try:
                    cursor.execute(f"ALTER TABLE active_downloads ADD COLUMN {col_name} {col_type}")
                except sqlite3.OperationalError as e:
                    if 'already exists' not in str(e):
                        print(f"⚠️  Impossible d'ajouter {col_name} à active_downloads: {e}")

        # "what's the value in knowing the age of the download" - created_at seul ne dit
        # rien de fiable sur "ce téléchargement est-il bloqué": un gros fichier Telegram
        # légitimement lent dépasse le seuil "probablement bloqué" tout autant qu'un
        # téléchargement dont le thread est réellement mort (voir PENDING_DOWNLOAD_
        # LIKELY_STUCK_MINUTES côté import.js). last_progress_at, mis à jour à chaque
        # callback de progression RÉELLE (voir update_download_progress) et initialisé à
        # CURRENT_TIMESTAMP comme created_at à la création (mark_download_pending), sert de
        # vrai battement de cœur : tant qu'il avance, le téléchargement est vivant quel que
        # soit son âge total ; s'il cesse d'avancer, "bloqué" devient un diagnostic fiable
        # au lieu d'un simple "vieux".
        if 'last_progress_at' not in existing_columns:
            try:
                cursor.execute("ALTER TABLE active_downloads ADD COLUMN last_progress_at TIMESTAMP")
            except sqlite3.OperationalError as e:
                if 'already exists' not in str(e):
                    print(f"⚠️  Impossible d'ajouter last_progress_at à active_downloads: {e}")

        # "yes relaunch and delete the stalled download from telegram" - jusqu'ici, rien
        # ne permettait de relancer un téléchargement Telegram bloqué (thread mort,
        # rebuild du conteneur pendant un lot en cours...): le channel/message_id
        # nécessaire pour redemander le fichier à Telegram n'existait que dans le
        # navigateur au moment du clic "Télécharger", jamais persisté. channel/message_id
        # stockés ici (Telegram uniquement, NULL pour aMule/qBittorrent/rTorrent/Deluge -
        # voir mark_download_pending) permettent à retry_stalled_telegram_downloads
        # (downloader.py, appelé par le scheduler périodique existant) de relancer le
        # même téléchargement depuis zéro plutôt que de laisser la ligne bloquée pour de
        # bon dès que son thread meurt.
        for col_name, col_type in [('channel', 'TEXT'), ('message_id', 'INTEGER')]:
            if col_name not in existing_columns:
                try:
                    cursor.execute(f"ALTER TABLE active_downloads ADD COLUMN {col_name} {col_type}")
                except sqlite3.OperationalError as e:
                    if 'already exists' not in str(e):
                        print(f"⚠️  Impossible d'ajouter {col_name} à active_downloads: {e}")

        # Plafonne le nombre de relances automatiques (voir retry_stalled_telegram_downloads,
        # downloader.py) - un message Telegram structurellement injoignable (supprimé,
        # canal quitté...) ne doit pas être retenté indéfiniment toutes les 5 minutes pour
        # de bon.
        if 'retry_count' not in existing_columns:
            try:
                cursor.execute("ALTER TABLE active_downloads ADD COLUMN retry_count INTEGER DEFAULT 0")
            except sqlite3.OperationalError as e:
                if 'already exists' not in str(e):
                    print(f"⚠️  Impossible d'ajouter retry_count à active_downloads: {e}")

        # "why telegram download is still stucked" - retry_stalled_telegram_downloads ne
        # doit relancer que ce qui a RÉELLEMENT commencé à transférer puis s'est arrêté
        # (voir son commentaire, bytes_downloaded IS NOT NULL) pour ne pas confondre "en
        # file d'attente derrière d'autres téléchargements" (téléchargements Telegram
        # sérialisés un par un, voir _telegram_client_lock) avec "réellement bloqué" -
        # les deux ont un last_progress_at figé identique. Mais ça laisse un vrai trou:
        # une ligne encore en file au moment d'un rebuild du conteneur (son thread meurt
        # AVANT d'avoir jamais progressé) ne serait alors plus JAMAIS relancée non plus.
        # process_instance_id (un identifiant aléatoire généré une fois par démarrage de
        # l'app, voir app.py) distingue les deux cas sans ambiguïté: une ligne posée par
        # un PRÉCÉDENT démarrage du process a forcément perdu son thread (le rebuild a
        # tout tué), quel que soit bytes_downloaded - donc rejouable immédiatement, sans
        # attendre le seuil de blocage d'1 minute qui n'a de sens que pour un thread
        # encore potentiellement vivant dans CE process-ci.
        if 'process_instance_id' not in existing_columns:
            try:
                cursor.execute("ALTER TABLE active_downloads ADD COLUMN process_instance_id TEXT")
            except sqlite3.OperationalError as e:
                if 'already exists' not in str(e):
                    print(f"⚠️  Impossible d'ajouter process_instance_id à active_downloads: {e}")

        # "for import there should be a database of all the downloads. this is the base
        # reference that should be used for display. do not display what files are on
        # disk" - is_pack/expected_volume_count sont calculés une seule fois à l'insertion
        # (voir mark_download_pending, downloader.py) depuis le titre RÉEL du
        # téléchargement, plutôt que redevinés à chaque rendu côté client depuis des champs
        # indirects (absence de volume_number/type, voir l'ancienne logique de
        # _pendingPackGroups côté import.js). Une release qui regroupe plusieurs tomes en
        # un seul téléchargement (ex. "Bouncer.BD.HD.PACK.2024.FR.PDF-STCTEAM") fait
        # désormais autorité pour le regroupement de /import (voir scan_import_directory/
        # get_pending_downloads) sans dépendre du matching flou des fichiers déjà arrivés
        # sur disque. expected_volume_count reste NULL quand aucune plage "T01 à T14" n'est
        # identifiable dans le titre (juste le mot "pack" seul, par exemple) - jamais
        # deviné.
        for col_name, col_type in [('is_pack', 'INTEGER DEFAULT 0'), ('expected_volume_count', 'INTEGER')]:
            if col_name not in existing_columns:
                try:
                    cursor.execute(f"ALTER TABLE active_downloads ADD COLUMN {col_name} {col_type}")
                except sqlite3.OperationalError as e:
                    if 'already exists' not in str(e):
                        print(f"⚠️  Impossible d'ajouter {col_name} à active_downloads: {e}")

        # "you have all the information already in the database. so no need to do any
        # fuzzy-matching" - un item renvoyé par un client (qBittorrent/rTorrent/Deluge/
        # aMule) n'a AUCUN moyen natif de porter notre id interne: leurs API "add" ne
        # renvoient jamais le hash du torrent tout juste créé, donc la toute PREMIÈRE
        # fois qu'un téléchargement suivi est vu chez son client, le relier à sa ligne
        # active_downloads passe forcément par une correspondance de nom (voir
        # find_active_download_destination/find_active_download_row_id côté
        # activity/routes.py). client_item_id (le hash/id que CE client lui-même utilise
        # pour ce téléchargement) est alors persisté une bonne fois pour toutes ici -
        # chaque sondage suivant retrouve la ligne par cet id exact
        # (find_active_downloads_by_client_item_ids, downloader.py), plus jamais par
        # ressemblance de nom pour ce téléchargement.
        if 'client_item_id' not in existing_columns:
            try:
                cursor.execute("ALTER TABLE active_downloads ADD COLUMN client_item_id TEXT")
            except sqlite3.OperationalError as e:
                if 'already exists' not in str(e):
                    print(f"⚠️  Impossible d'ajouter client_item_id à active_downloads: {e}")

        # "pourquoi je ne peux pas choisir intégrale, seulement le tome normal" -
        # update_active_download_tracking (correction d'un téléchargement encore EN COURS,
        # /api/activity/update-tracking) ne stockait que volume_number, aucun moyen de
        # marquer une Intégrale/HS/Épisode avant l'arrivée du fichier sur disque - même
        # jeu de champs que volumes.is_integral/integral_number/is_hs/hs_number/
        # is_episode/episode_number, pour que apply_tracked_volume_and_gate puisse
        # transmettre le bon type au fichier une fois arrivé (voir downloader.py).
        for col_name, col_type in [
            ('is_integral', 'INTEGER DEFAULT 0'), ('integral_number', 'INTEGER'),
            ('is_hs', 'INTEGER DEFAULT 0'), ('hs_number', 'INTEGER'),
            ('is_episode', 'INTEGER DEFAULT 0'), ('episode_number', 'INTEGER'),
        ]:
            if col_name not in existing_columns:
                try:
                    cursor.execute(f"ALTER TABLE active_downloads ADD COLUMN {col_name} {col_type}")
                except sqlite3.OperationalError as e:
                    if 'already exists' not in str(e):
                        print(f"⚠️  Impossible d'ajouter {col_name} à active_downloads: {e}")

        # "Remplacer quand même" (recherche explicite "Rechercher un remplacement" depuis
        # une fiche série pour un tome DÉJÀ possédé, voir searchMissingVolume/currentVolumeId
        # côté library.js) - is_better_volume (comparaison de taille, blueprints/library/
        # routes.py) est pensée pour un import automatique sans humain dans la boucle, où un
        # candidat plus petit est probablement un doublon sans intérêt ; un clic explicite
        # sur "remplacement" est déjà une décision humaine, la taille ne doit plus faire foi
        # (même logique que "Remplacer le fichier", upload manuel - voir
        # destination['force_replace'] côté _execute_import_batch). Posé à l'ajout du
        # téléchargement (mark_download_pending) plutôt que redeviné plus tard, pour la même
        # raison que series_id/volume_id/volume_number juste au-dessus : l'intention de
        # l'utilisateur n'existe qu'au moment du clic, jamais reconstructible après coup
        # depuis le seul fichier arrivé sur disque.
        if 'force_replace' not in existing_columns:
            try:
                cursor.execute("ALTER TABLE active_downloads ADD COLUMN force_replace INTEGER DEFAULT 0")
            except sqlite3.OperationalError as e:
                if 'already exists' not in str(e):
                    print(f"⚠️  Impossible d'ajouter force_replace à active_downloads: {e}")

        # "dans nouveautés ca a match une mauvaise serie. il faudrait pouvoir changer ca
        # et selectionner manuellement la série" - côté EBDZ un thread se réassigne en
        # changeant series.ebdz_thread_id (déjà persistant, voir ebdz-match côté
        # library/routes.py). Côté Telegram il n'existe rien d'équivalent à réassigner:
        # le matching (_annotate_already_in_library, telegram_channels/routes.py) est
        # recalculé À CHAQUE requête depuis le titre parsé du fichier (aucun id de thread
        # stable). Cette table mémorise une correction manuelle par titre normalisé
        # (parsed_title, identique pour tous les fichiers Telegram de la même série) -
        # consultée en priorité, avant le matching flou par titre, pour que la correction
        # s'applique à CHAQUE fichier de cette série sur Nouveautés, pas seulement à celui
        # cliqué.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS telegram_title_overrides (
                normalized_title TEXT PRIMARY KEY,
                series_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (series_id) REFERENCES series(id) ON DELETE CASCADE
            )
        ''')

        conn.commit()


class DevelopmentConfig(Config):
    """Configuration de développement"""
    DEBUG = True


class ProductionConfig(Config):
    """Configuration de production"""
    DEBUG = False


config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'default': ProductionConfig
}
