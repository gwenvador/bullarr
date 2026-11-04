"""Notification « Mise à jour disponible » : nouvelle version de Bullarr ou de l'application
Android, vérifiée toutes les 6 heures (mêmes releases GitHub que la carte À propos, voir
updates.py, déjà mises en cache 6 h).

- Bullarr : notifié une fois par version plus récente que celle installée (APP_VERSION).
- Android : le serveur ne connaît pas la version installée sur les téléphones ; chaque
  nouvelle release est notifiée une fois, et la release déjà publiée au premier passage sert
  de point de départ (pas de notification pour elle).

La dernière version notifiée est gardée dans data/update_notify_state.json."""
import json
import os
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

CHECK_INTERVAL_HOURS = 6


def _state_path(app):
    return os.path.join(app.config['DATA_DIR'], 'update_notify_state.json')


def _load_state(app):
    try:
        with open(_state_path(app), encoding='utf-8') as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(app, state):
    path = _state_path(app)
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(state, handle)
    os.replace(tmp, path)


def check_updates_and_notify(app):
    """Un passage de vérification (contexte applicatif requis). Retourne les notifications
    envoyées, sous forme de [(nom, version)]."""
    from notifications import notification_wanted, send_notification
    from .updates import is_newer, latest_release

    if not notification_wanted('update_available'):
        return []
    state = _load_state(app)
    current = app.config.get('APP_VERSION', '')
    sent = []

    bullarr = latest_release('bullarr')
    if bullarr and bullarr.get('tag'):
        tag = bullarr['tag']
        if is_newer(tag, current) and state.get('bullarr') != tag:
            ok, _errors = send_notification(
                f"Bullarr {tag.lstrip('v')} est disponible (installé : {current or '?'}).\n"
                "Mettez à jour l'image Docker pour en profiter.",
                kind='update_available', icon='🆕', title='Mise à jour de Bullarr',
                tags=['arrow_up'], click=bullarr.get('url') or None)
            if ok:
                state['bullarr'] = tag
                sent.append(('bullarr', tag))

    android = latest_release('android')
    if android and android.get('tag'):
        tag = android['tag']
        if 'android' not in state:
            state['android'] = tag  # point de départ : la version déjà publiée
        elif state['android'] != tag and is_newer(tag, state['android']):
            ok, _errors = send_notification(
                f"L'application Android {tag.lstrip('v')} est disponible.",
                kind='update_available', icon='📱', title="Mise à jour de l'application Android",
                tags=['iphone'], click=android.get('apk_url') or android.get('url') or None)
            if ok:
                state['android'] = tag
                sent.append(('android', tag))

    _save_state(app, state)
    return sent


class UpdateNotifierScheduler:
    def __init__(self):
        self.scheduler = None
        self.app = None

    def start(self, app):
        if self.scheduler is not None:
            return
        self.app = app
        self.scheduler = BackgroundScheduler(daemon=True)
        self.scheduler.add_job(
            func=self._run, trigger=IntervalTrigger(hours=CHECK_INTERVAL_HOURS),
            next_run_time=datetime.now() + timedelta(minutes=2),
            id='update_notifier', name='Update notifier', replace_existing=True,
            coalesce=True, max_instances=1,
        )
        self.scheduler.start()
        print(f"✓ Vérification des mises à jour programmée: toutes les {CHECK_INTERVAL_HOURS} heures")

    def _run(self):
        with self.app.app_context():
            try:
                for name, tag in check_updates_and_notify(self.app):
                    print(f"🆕 Notification de mise à jour envoyée: {name} {tag}")
            except Exception as exc:
                print(f"⚠️ Vérification des mises à jour en échec: {exc}")


update_notifier_scheduler = UpdateNotifierScheduler()
