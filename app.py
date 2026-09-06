"""
Point d'entrée principal de l'application Bullarr
"""
from flask import Flask, send_from_directory
from config import config
import os
import sys
import uuid
from encryption import ensure_encryption_key

def create_app(config_name='default'):
    """Factory pour créer l'application Flask"""

    # Initialiser la clé de chiffrement
    ensure_encryption_key()

    app = Flask(__name__)
    app.config.from_object(config[config_name])

    # "why telegram download is still stucked" - identifiant unique de CE démarrage du
    # process (voir active_downloads.process_instance_id, config.py/downloader.py): une
    # ligne posée par un démarrage précédent (rebuild du conteneur) a forcément perdu son
    # thread de téléchargement, quel que soit son état - permet de le détecter avec
    # certitude plutôt que de deviner depuis un simple délai.
    app.config['PROCESS_INSTANCE_ID'] = str(uuid.uuid4())

    # Initialiser l'app (créer les répertoires)
    config[config_name].init_app(app)
    
    # Route pour servir les couvertures. max_age évite au navigateur de revalider (round-trip
    # réseau) à chaque affichage de la bibliothèque; les couvertures ne changent qu'après un
    # rescan explicite, une valeur courte (1 jour) suffit donc sans risquer un cache trop périmé
    @app.route('/covers/<path:filename>')
    def serve_cover(filename):
        return send_from_directory(app.config['COVERS_DIR'], filename, max_age=86400)
    
    # Enregistrer les blueprints
    from blueprints.library import library_bp
    from blueprints.search import search_bp
    from blueprints.emule import emule_bp
    from blueprints.ebdz import ebdz_bp
    from blueprints.prowlarr import prowlarr_bp
    from blueprints.komga import komga_bp
    from blueprints.bedetheque import bedetheque_bp
    from blueprints.settings import settings_bp
    from blueprints.qbittorrent import qbittorrent_bp
    from blueprints.rtorrent import rtorrent_bp
    from blueprints.deluge import deluge_bp
    from blueprints.missing_monitor import missing_monitor_bp
    from blueprints.auth import auth_bp
    from blueprints.telegram import telegram_bp
    from blueprints.activity import activity_bp
    from blueprints.telegram_channels import telegram_channels_bp
    from blueprints.fourtoutici import fourtoutici_bp
    from blueprints.annas_archive import annas_archive_bp
    from blueprints.shelfmark import shelfmark_bp
    from blueprints.bdgest import bdgest_bp

    app.register_blueprint(library_bp)
    app.register_blueprint(search_bp)
    app.register_blueprint(emule_bp, url_prefix='/api/emule')
    app.register_blueprint(ebdz_bp, url_prefix='/api/ebdz')
    app.register_blueprint(prowlarr_bp, url_prefix='/api/prowlarr')
    app.register_blueprint(komga_bp, url_prefix='/api/komga')
    app.register_blueprint(bedetheque_bp, url_prefix='/api/bedetheque')
    app.register_blueprint(qbittorrent_bp)
    app.register_blueprint(rtorrent_bp)
    app.register_blueprint(deluge_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(missing_monitor_bp, url_prefix='/api/missing-monitor')
    app.register_blueprint(auth_bp)
    app.register_blueprint(telegram_bp, url_prefix='/api/telegram')
    app.register_blueprint(activity_bp)
    app.register_blueprint(telegram_channels_bp)
    app.register_blueprint(fourtoutici_bp)
    app.register_blueprint(annas_archive_bp)
    app.register_blueprint(shelfmark_bp)
    app.register_blueprint(bdgest_bp, url_prefix='/api/bdgest')
    
    # Initialiser le scheduler EBDZ
    from blueprints.ebdz.scheduler import ebdz_scheduler
    ebdz_scheduler.init_app(app)

    # Initialiser le scheduler de scraping automatique des canaux Telegram
    from blueprints.telegram_channels.scheduler import telegram_channels_scheduler
    telegram_channels_scheduler.init_app(app)
    
    # Initialiser le scheduler d'import automatique
    from blueprints.library.scheduler import library_import_scheduler
    library_import_scheduler.init_app(app)
    
    # Démarrer les schedulers et charger les configurations automatiques
    with app.app_context():
        # Initialiser/migrer le schéma de la base bibliothèque (tables + colonnes EBDZ/Bédéthèque)
        from blueprints.library.scanner import LibraryScanner
        LibraryScanner()

        # Initialiser la table d'historique des imports
        from blueprints.library.import_history import init_import_history_table, cleanup_stale_operations, init_import_manual_overrides_table, init_import_in_progress_table
        init_import_history_table()
        cleanup_stale_operations()
        init_import_manual_overrides_table()
        init_import_in_progress_table()

        # Initialiser la table d'historique des renommages/suppressions
        from blueprints.library.action_history import init_action_history_table
        init_action_history_table()

        # Index de recherche FTS5 (EBDZ + Telegram) - "do the same as ebdz. this is fast.
        # why?": ni l'un ni l'autre n'était rapide (voir commentaires dans ebdz/scraper.py
        # et telegram_channels/scraper.py). Migration une seule fois au démarrage plutôt
        # qu'à chaque scrape/recherche - peuple les colonnes normalisées + l'index FTS5
        # pour les lignes déjà présentes avant l'ajout de ces colonnes (no-op quasi
        # instantané une fois déjà fait, seul le tout premier démarrage après cette
        # mise à jour fait le travail réel).
        from blueprints.ebdz.scraper import ensure_ed2k_search_index
        ensure_ed2k_search_index()
        from blueprints.telegram_channels.scraper import ensure_telegram_search_index
        ensure_telegram_search_index()

        from blueprints.ebdz.routes import load_ebdz_config
        ebdz_config = load_ebdz_config()
        
        if ebdz_config.get('auto_scrape_enabled', False):
            interval = ebdz_config.get('auto_scrape_interval', 60)
            interval_unit = ebdz_config.get('auto_scrape_interval_unit', 'minutes')
            ebdz_scheduler.add_job(interval, interval_unit)
            print(f"✓ Scraping automatique EBDZ activé: tous les {interval} {interval_unit}")

        from blueprints.telegram_channels.routes import load_telegram_channels_config
        telegram_channels_config = load_telegram_channels_config()

        if telegram_channels_config.get('auto_scrape_enabled', False):
            interval = telegram_channels_config.get('auto_scrape_interval', 6)
            interval_unit = telegram_channels_config.get('auto_scrape_interval_unit', 'hours')
            telegram_channels_scheduler.add_job(interval, interval_unit)
            print(f"✓ Scraping automatique Telegram (canaux) activé: tous les {interval} {interval_unit}")

        # Charger la configuration d'import automatique (+ tâche planifiée si elle-même
        # activée ou si la notification Telegram "import disponible" en a besoin, voir
        # sync_auto_import_schedule)
        from blueprints.library.scheduler import sync_auto_import_schedule, library_import_scheduler
        sync_auto_import_schedule()

        # "should i restart automatically?" - relance des téléchargements Telegram
        # bloqués, TOUJOURS active (voir add_stalled_telegram_job/
        # _retry_stalled_telegram_tick, scheduler.py) - contrairement à la tâche
        # ci-dessus, ne dépend PAS de auto_import_enabled/notify_import_available: un
        # téléchargement Telegram resté à retry_count=0 indéfiniment quand ces deux
        # réglages étaient désactivés était la conséquence directe de l'ancien piggyback
        # sur ce même planning conditionnel.
        library_import_scheduler.add_stalled_telegram_job()

    # "toujours trop petit. on voit même pas que c'est un cover" - la modification était
    # bien déployée côté serveur (vérifié directement), donc un cache navigateur/proxy
    # côté mobile n'ayant pas revalidé malgré Cache-Control:no-cache est l'explication la
    # plus probable. `{{ asset_version('css/style.css') }}` (mtime du fichier, en secondes)
    # ajouté en `?v=` sur un lien/script force une URL différente à chaque modification
    # réelle du fichier - un cache, quel qu'il soit, n'a alors plus le choix de resservir
    # l'ancienne version puisque ce n'est plus littéralement la même URL.
    @app.context_processor
    def inject_asset_version():
        def asset_version(filename):
            path = os.path.join(app.static_folder, filename)
            try:
                return int(os.path.getmtime(path))
            except OSError:
                return 0
        return {'asset_version': asset_version}

    return app


if __name__ == '__main__':
    # Déterminer le mode (développement ou production)
    # FLASK_ENV peut être: development ou production (défaut: development)
    config_name = os.getenv('FLASK_ENV', 'development')
    
    app = create_app(config_name)
    
    debug_mode = config_name == 'development'
    
    print("=" * 60)
    print("Gestionnaire Multi-Bibliothèques BD")
    print("=" * 60)
    print(f"Mode: {config_name.upper()}")
    print("Accédez à http://localhost:5000")
    print("Écoute sur IPv4 et IPv6")
    print("=" * 60)
    
    # threaded=True: sans ça, le serveur ne traite qu'une requête à la fois (défaut
    # Werkzeug), donc un scan de bibliothèque long bloque toute l'app (même charger une
    # autre page) jusqu'à sa fin, ce qui donne l'impression que tout (y compris le scan)
    # s'est arrêté si on quitte la page en cours de scan
    app.run(debug=debug_mode, host='::', port=5000, use_reloader=False, threaded=True)
