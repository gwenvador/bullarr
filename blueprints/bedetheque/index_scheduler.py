"""
Mise à jour périodique de l'index local du catalogue Bédéthèque (voir catalog_index.py).

Réglable depuis Paramètres (activée, tous les N jours, à quelle heure). Le job tourne CHAQUE
HEURE et relit le réglage à chaque passage: aucun re-planning n'est nécessaire quand il change
(ni entre les processus). Il ne reconstruit que si l'option est active, si l'heure courante est
l'heure choisie et si l'index a au moins N jours (~27 requêtes, de l'ordre de la minute).
"""
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# Tolérance: une reconstruction faite hier à 04:30 reste « due » aujourd'hui à 04:30.
_DUE_TOLERANCE_DAYS = 0.1


def refresh_tick(now=None):
    """Un passage horaire. Retourne True si l'index a été reconstruit."""
    from blueprints.bedetheque.catalog_index import load_index_refresh_config, refresh_catalog_index_if_stale
    config = load_index_refresh_config()
    if not config['enabled']:
        return False
    hour = (now or time.localtime()).tm_hour
    if hour != config['hour']:
        return False
    return refresh_catalog_index_if_stale(config['interval_days'] - _DUE_TOLERANCE_DAYS)


class CatalogIndexScheduler:
    def __init__(self):
        self.scheduler = None
        self.job_id = 'bedetheque_catalog_index_refresh'

    def start(self):
        if self.scheduler is not None:
            return
        self.scheduler = BackgroundScheduler(daemon=True)
        self.scheduler.add_job(
            func=self._run, trigger=CronTrigger(minute=30), id=self.job_id,
            name='Bédéthèque catalog index refresh', replace_existing=True,
        )
        self.scheduler.start()
        print("✓ Mise à jour périodique de l'index Bédéthèque prête (réglable dans Paramètres)")

    def _run(self):
        try:
            if refresh_tick():
                print("✓ Index du catalogue Bédéthèque mis à jour automatiquement")
        except Exception as exc:
            print(f"⚠️ Mise à jour automatique de l'index Bédéthèque échouée: {exc}")


catalog_index_scheduler = CatalogIndexScheduler()
