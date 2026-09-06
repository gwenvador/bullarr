"""
Scraping automatique des canaux Telegram configurés - même principe que
blueprints/ebdz/scheduler.py (APScheduler + config persistée), mais restreint à
heures/jours ('hour/par jours' demandé) plutôt que minutes: contrairement à EBDZ,
chaque appel ouvre une connexion MTProto complète sérialisée (voir
_telegram_client_lock dans scraper.py) - un intervalle à la minute n'aurait aucun
intérêt pour des canaux qui publient au mieux quelques fois par jour.
"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from datetime import datetime


class TelegramChannelsScheduler:
    """Gestionnaire du scraping automatique des canaux Telegram"""

    def __init__(self, app=None):
        self.scheduler = None
        self.app = app
        self.job_id = 'telegram_channels_auto_scrape'

    def init_app(self, app):
        self.app = app

    def start(self):
        if self.scheduler is None:
            self.scheduler = BackgroundScheduler(daemon=True)
            self.scheduler.start()
            print("✓ Scheduler Telegram (canaux) démarré")

    def stop(self):
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            self.scheduler = None
            print("✓ Scheduler Telegram (canaux) arrêté")

    def add_job(self, interval_value, interval_unit):
        self.start()

        if self.scheduler.get_job(self.job_id):
            self.scheduler.remove_job(self.job_id)

        self.scheduler.add_job(
            func=self._scrape_telegram_channels,
            trigger=IntervalTrigger(**{interval_unit: interval_value}),
            id=self.job_id,
            name='Telegram Channels Auto Scrape',
            replace_existing=True
        )

        print(f"✓ Tâche de scraping Telegram (canaux) programmée: tous les {interval_value} {interval_unit}")

    def remove_job(self):
        if self.scheduler and self.scheduler.get_job(self.job_id):
            self.scheduler.remove_job(self.job_id)
            print("✓ Tâche de scraping Telegram (canaux) supprimée")

    def _scrape_telegram_channels(self):
        if not self.app:
            print("Erreur: App Flask non initialisée")
            return

        with self.app.app_context():
            try:
                print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🕷️ Scraping automatique Telegram (canaux) en cours...")

                from .routes import load_telegram_channels_config
                from .scraper import scrape_channels

                config = load_telegram_channels_config()
                if not config.get('connected') or not config.get('session_decrypted'):
                    print("⚠️ Telegram (canaux) non connecté, scraping annulé")
                    return

                channels = [c['username'] for c in (config.get('channels') or [])]
                if not channels:
                    print("⚠️ Aucun canal Telegram configuré, scraping annulé")
                    return

                summary = scrape_channels(config['api_id'], config['api_hash_decrypted'], config['session_decrypted'], channels)
                total_new = sum(s['new_count'] for s in summary.values())
                errors = [f"{s['title']}: {s['error']}" for s in summary.values() if s['error']]
                print(f"✓ Scraping Telegram (canaux) terminé: {total_new} nouveau(x) fichier(s)" + (f" - erreurs: {'; '.join(errors)}" if errors else ""))

                # Traiter uniquement les fichiers réellement insérés pendant CE scrape.
                # Aucun appel de recherche locale ou distante n'est effectué.
                new_items = []
                for channel, channel_summary in summary.items():
                    for item in channel_summary.get('new_files', []):
                        new_items.append({**item, 'channel': channel,
                                          'channel_title': channel_summary.get('title')})
                if new_items:
                    from blueprints.missing_monitor.new_file_handler import process_new_monitor_files
                    stats = process_new_monitor_files('telegram', new_items)
                    print(f"  ✓ Surveillance directe Telegram: {stats['matched_missing']} correspondant(s), "
                          f"{stats['downloads_sent']} téléchargement(s), {stats['notifications_sent']} notification(s), "
                          f"{stats['queued_for_review']} en validation, "
                          f"{stats['matched_quality_upgrade']} upgrade(s) qualité détecté(s), "
                          f"{stats['quality_upgrades_sent']} téléchargement(s) d'upgrade envoyé(s)")

            except Exception as e:
                print(f"✗ Erreur lors du scraping automatique Telegram (canaux): {e}")
                import traceback
                traceback.print_exc()


telegram_channels_scheduler = TelegramChannelsScheduler()
