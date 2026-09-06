"""
Gestionnaire du scraping automatique pour ebdz.net
"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
import json
import os
from datetime import datetime


class EBDZScheduler:
    """Gestionnaire du scraping automatique EBDZ"""
    
    def __init__(self, app=None):
        self.scheduler = None
        self.app = app
        self.job_id = 'ebdz_auto_scrape'
        
    def init_app(self, app):
        """Initialiser le scheduler avec l'app Flask"""
        self.app = app
        
    def start(self):
        """Démarrer le scheduler"""
        if self.scheduler is None:
            self.scheduler = BackgroundScheduler(daemon=True)
            self.scheduler.start()
            print("✓ Scheduler EBDZ démarré")
        
    def stop(self):
        """Arrêter le scheduler"""
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            self.scheduler = None
            print("✓ Scheduler EBDZ arrêté")
    
    def _compute_next_run_time(self, interval_value, interval_unit):
        """"a chaque fois que l'on redemarre le docker le rescanne est deplacé d'un jour" -
        sans mémoire du dernier scrape réel (voir mark_ebdz_scrape_completed côté
        routes.py), IntervalTrigger recalculait "maintenant + intervalle" à CHAQUE
        redémarrage, décalant le planning à chaque fois plutôt que de suivre un rythme
        fixe. None (comportement par défaut d'APScheduler, maintenant + intervalle)
        seulement si aucun scrape précédent n'est connu."""
        from datetime import timedelta
        try:
            from .routes import load_ebdz_config
            last_scrape_at = load_ebdz_config().get('last_scrape_at')
            if not last_scrape_at:
                return None
            last_dt = datetime.fromisoformat(last_scrape_at)
            due_at = last_dt + timedelta(**{interval_unit: interval_value})
            now = datetime.now()
            # En retard (container resté éteint plus longtemps qu'un intervalle, ou
            # plusieurs redémarrages rapprochés) - lancer bientôt plutôt qu'attendre un
            # intervalle complet de plus.
            return due_at if due_at > now else now + timedelta(seconds=30)
        except Exception as e:
            print(f"⚠️ Impossible de calculer le prochain scrape EBDZ depuis le dernier scrape connu: {e}")
            return None

    def add_job(self, interval_value, interval_unit):
        """Ajouter une tâche de scraping automatique"""
        self.start()

        # Supprimer la tâche existante si elle existe
        if self.scheduler.get_job(self.job_id):
            self.scheduler.remove_job(self.job_id)

        next_run_time = self._compute_next_run_time(interval_value, interval_unit)

        # Ajouter la nouvelle tâche
        self.scheduler.add_job(
            func=self._scrape_ebdz,
            trigger=IntervalTrigger(**{interval_unit: interval_value}),
            id=self.job_id,
            name='EBDZ Auto Scrape',
            replace_existing=True,
            next_run_time=next_run_time
        )

        print(f"✓ Tâche de scraping EBDZ programmée: tous les {interval_value} {interval_unit}"
              + (f" (prochain: {next_run_time.strftime('%Y-%m-%d %H:%M:%S')})" if next_run_time else ""))
    
    def remove_job(self):
        """Supprimer la tâche de scraping automatique"""
        if self.scheduler and self.scheduler.get_job(self.job_id):
            self.scheduler.remove_job(self.job_id)
            print("✓ Tâche de scraping EBDZ supprimée")
    
    def _scrape_ebdz(self):
        """Fonction appelée par le scheduler pour scraper EBDZ"""
        if not self.app:
            print("Erreur: App Flask non initialisée")
            return
        
        with self.app.app_context():
            try:
                print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🕷️ Scraping automatique EBDZ en cours...")
                
                # Import local pour éviter les boucles circulaires
                from . import routes
                from .scraper import MyBBScraper
                import sqlite3
                from flask import current_app
                from encryption import decrypt
                
                # Charger la configuration EBDZ
                config = routes.load_ebdz_config()
                username = config.get('username', '')
                password = config.get('password_decrypted', '')
                all_forums = config.get('forums', [])
                
                if not username or not password or not all_forums:
                    print("⚠️ Configuration EBDZ incomplète, scraping annulé")
                    return
                
                total_links = 0
                forums_scraped = 0
                new_items_this_run = []

                # Une seule connexion réutilisée pour le comptage post-scrape de chaque
                # forum, plutôt qu'une ouverture/fermeture par forum (même raisonnement
                # que scrape() dans routes.py, voir son commentaire).
                count_conn = sqlite3.connect(current_app.config['DB_FILE'])
                try:
                    for forum_cfg in all_forums:
                        fid = forum_cfg['fid']
                        category = forum_cfg['category']

                        forum_url = f"https://ebdz.net/forum/forumdisplay.php?fid={fid}"

                        print(f"  → Forum fid={fid} catégorie='{category}'...")

                        scraper = MyBBScraper(
                            base_url=forum_url,
                            db_file=current_app.config['DB_FILE'],
                            username=username,
                            password=password,
                            forum_category=category
                        )

                        scraper.run()
                        new_items_this_run.extend(scraper.new_links)
                        forums_scraped += 1

                        # Compter les liens - non-fatal (le scraping a déjà réussi, un
                        # échec ici ne doit pas faire échouer tout le job planifié), mais
                        # on log l'exception au lieu de l'avaler silencieusement (un
                        # `except: pass` masquait aussi bien une DB verrouillée qu'un bug
                        # réel, sans aucune trace dans les logs)
                        try:
                            cursor = count_conn.cursor()
                            cursor.execute('SELECT COUNT(*) FROM ed2k_links WHERE forum_category = ?', (category,))
                            count = cursor.fetchone()[0]
                            total_links += count
                        except Exception as count_err:
                            print(f"⚠️ Erreur comptage des liens pour catégorie '{category}': {count_err}")
                finally:
                    count_conn.close()

                print(f"✓ Scraping EBDZ terminé: {forums_scraped} forum(s), {total_links} lien(s)")

                from .routes import mark_ebdz_scrape_completed
                mark_ebdz_scrape_completed()

                # Traiter directement les liens insérés pendant CE scrape. Aucune
                # recherche EBDZ/Prowlarr supplémentaire n'est lancée ici.
                if new_items_this_run:
                    from blueprints.missing_monitor.new_file_handler import process_new_monitor_files
                    stats = process_new_monitor_files('ebdz', new_items_this_run)
                    print(f"  ✓ Surveillance directe EBDZ: {stats['matched_missing']} correspondant(s), "
                          f"{stats['downloads_sent']} téléchargement(s), {stats['notifications_sent']} notification(s), "
                          f"{stats['queued_for_review']} en validation, "
                          f"{stats['matched_quality_upgrade']} upgrade(s) qualité détecté(s), "
                          f"{stats['quality_upgrades_sent']} téléchargement(s) d'upgrade envoyé(s)")

            except Exception as e:
                print(f"✗ Erreur lors du scraping automatique EBDZ: {e}")
                import traceback
                traceback.print_exc()


# Instance globale du scheduler
ebdz_scheduler = EBDZScheduler()
