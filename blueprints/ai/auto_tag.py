"""Tags automatiques des nouvelles séries (Configuration → IA, « Créer les tags des
nouvelles séries »).

Les endroits qui créent une série (import, création manuelle, ajout depuis Bédéthèque,
scan de la bibliothèque) appellent queue_new_series(cursor, ids) dans leur transaction.
Toutes les CHECK_INTERVAL_MINUTES minutes, le planificateur envoie à l'IA les séries en
attente depuis au moins AUTO_TAG_DELAY secondes, le temps que leur fiche Bédéthèque
soit lue en arrière-plan. Le traitement est le même que « Créer des tags » et il est
noté dans l'historique (genre « auto »).
"""
import time
from datetime import datetime, timedelta

AUTO_TAG_DELAY = 180
AUTO_TAG_MAX_ATTEMPTS = 3
AUTO_TAG_MAX_SERIES = 200
CHECK_INTERVAL_MINUTES = 2


def ensure_table(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS ai_autotag_queue (
        series_id INTEGER PRIMARY KEY,
        queued_at REAL NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0)''')


def auto_tag_active(cfg):
    from .service import provider_chain
    return bool(cfg.get('enabled') and cfg.get('auto_tag_new') and provider_chain(cfg))


def queue_new_series(cursor, series_ids):
    """Met des séries tout juste créées en attente de tags, si l'option est active.
    N'échoue jamais : la création de la série passe avant."""
    try:
        ids = [int(series_id) for series_id in series_ids if series_id]
        if not ids:
            return
        from .service import load_ai_config
        if not auto_tag_active(load_ai_config()):
            return
        ensure_table(cursor)
        now = time.time()
        cursor.executemany('INSERT OR IGNORE INTO ai_autotag_queue (series_id, queued_at) VALUES (?, ?)',
                           [(series_id, now) for series_id in ids])
    except Exception as exc:  # noqa: BLE001
        print(f'⚠️ IA : série non mise en attente pour les tags automatiques : {exc}')


def tag_soon(app, series_ids):
    """« pourquoi ajouter une nouvelle serie ca n'ajoute pas les tags » : une série ajoutée
    depuis Bédéthèque a déjà sa fiche en base, inutile d'attendre AUTO_TAG_DELAY puis le
    passage suivant du planificateur (3 à 5 minutes). Rend la série prête tout de suite et
    lance un passage dans un fil. Si un traitement IA tourne déjà, le planificateur
    reprendra la série à son prochain passage. N'échoue jamais."""
    try:
        import sqlite3
        import threading
        ids = [int(series_id) for series_id in series_ids if series_id]
        if not ids:
            return
        conn = sqlite3.connect(app.config['DATABASE'], timeout=30)
        try:
            ensure_table(conn)
            marks = ','.join('?' * len(ids))
            updated = conn.execute(f'UPDATE ai_autotag_queue SET queued_at = ? WHERE series_id IN ({marks})',
                                   [time.time() - AUTO_TAG_DELAY, *ids]).rowcount
            conn.commit()
        finally:
            conn.close()
        if not updated:
            return  # option désactivée : rien n'a été mis en file

        def kick():
            time.sleep(2)
            with app.app_context():
                try:
                    from .routes import run_auto_tag_tick
                    run_auto_tag_tick()
                except Exception as exc:  # noqa: BLE001
                    print(f'⚠️ IA : tags immédiats non lancés : {exc}')

        threading.Thread(target=kick, daemon=True).start()
    except Exception as exc:  # noqa: BLE001
        print(f'⚠️ IA : tags immédiats non lancés : {exc}')


def pending_count(conn):
    ensure_table(conn)
    return conn.execute('SELECT COUNT(*) FROM ai_autotag_queue').fetchone()[0]


class AutoTagScheduler:
    def __init__(self):
        self.scheduler = None
        self.app = None

    def start(self, app):
        if self.scheduler is not None:
            return
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger
        self.app = app
        self.scheduler = BackgroundScheduler(daemon=True)
        self.scheduler.add_job(
            func=self._run, trigger=IntervalTrigger(minutes=CHECK_INTERVAL_MINUTES),
            next_run_time=datetime.now() + timedelta(minutes=1),
            id='ai_auto_tag', name='IA tags des nouvelles séries', replace_existing=True,
            coalesce=True, max_instances=1,
        )
        self.scheduler.start()

    def _run(self):
        with self.app.app_context():
            try:
                from .routes import run_auto_tag_tick
                run_auto_tag_tick()
            except Exception as exc:  # noqa: BLE001
                print(f'⚠️ IA : tags automatiques en échec : {exc}')


auto_tag_scheduler = AutoTagScheduler()
