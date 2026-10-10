"""
Surveillance des séries via les flux RSS (Prowlarr...).

Les nouveautés EBDZ et Telegram passaient déjà par la surveillance (tome manquant d'une série
surveillée -> téléchargement automatique) à chaque récupération; les flux RSS n'étaient lus
que lorsque la page Nouveautés était ouverte, et jamais transmis à la surveillance. Ce
planificateur les lit en tâche de fond (voir routes.poll_rss_for_monitoring).
"""
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

RSS_MONITOR_INTERVAL_MINUTES = 15


class RssMonitorScheduler:
    def __init__(self):
        self.scheduler = None
        self.app = None
        self.job_id = 'rss_series_monitor'

    def start(self, app):
        if self.scheduler is not None:
            return
        self.app = app
        self.scheduler = BackgroundScheduler(daemon=True)
        self.scheduler.add_job(
            func=self._run, trigger=IntervalTrigger(minutes=RSS_MONITOR_INTERVAL_MINUTES),
            next_run_time=datetime.now() + timedelta(minutes=1),
            id=self.job_id, name='RSS series monitor', replace_existing=True,
        )
        self.scheduler.start()
        print(f"✓ Surveillance des séries via RSS programmée: toutes les {RSS_MONITOR_INTERVAL_MINUTES} minutes")

    def _run(self):
        with self.app.app_context():
            try:
                from blueprints.ebdz.routes import poll_rss_for_monitoring
                stats = poll_rss_for_monitoring()
                if stats and (stats.get('downloads_sent') or stats.get('queued_for_review') or stats.get('errors')):
                    print(f"📡 Surveillance RSS: {stats.get('downloads_sent', 0)} téléchargement(s), "
                          f"{stats.get('queued_for_review', 0)} en validation, {len(stats.get('errors') or [])} erreur(s)")
            except Exception as exc:
                print(f"⚠️ Surveillance RSS en échec: {exc}")


rss_monitor_scheduler = RssMonitorScheduler()
