"""
Taille réelle des pages d'un tome (largeur × hauteur en pixels), mesurée dans le fichier.

La « qualité » lue dans le nom de fichier (colonne resolution) manque pour la plupart des
tomes. On mesure donc quelques pages du milieu de l'album, ce qui sert à repérer les scans
basse définition (à améliorer à la lecture ou à remplacer par une meilleure version).

Une tâche de fond mesure peu à peu les tomes jamais mesurés ou dont le fichier a changé de
taille depuis (page_size_file_size) ; un échec de lecture est noté 0×0 pour ne pas être
retenté à chaque passage.
"""
import io
import sqlite3
import time
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .reader import _archive_opener, page_index

PAGE_SIZE_COLUMNS = (('page_width', 'INTEGER'), ('page_height', 'INTEGER'), ('page_size_file_size', 'INTEGER'))
BATCH_SIZE = 1000
INTERVAL_MINUTES = 15
_SAMPLES = (0.4, 0.5, 0.6)


def measure(filepath, fmt):
    """(largeur, hauteur) de la page médiane parmi quelques pages du milieu de l'album, ou
    None si le fichier n'a pas d'image lisible."""
    from PIL import Image
    real, pages = page_index(filepath, fmt)
    total = pages if real == 'pdf' else len(pages)
    if not total:
        return None
    numbers = sorted({min(total - 1, int(total * f)) for f in _SAMPLES})
    sizes = []
    if real == 'pdf':
        import fitz
        with fitz.open(filepath) as doc:
            for n in numbers:
                # Taille de la plus grande image de la page : un PDF de scan n'est qu'une image par page.
                images = doc.load_page(n).get_images(full=True)
                if images:
                    sizes.append(max(((img[2], img[3]) for img in images), key=lambda s: s[0] * s[1]))
    else:
        with _archive_opener(real)(filepath) as archive:
            for n in numbers:
                try:
                    with Image.open(io.BytesIO(archive.read(pages[n]))) as image:
                        sizes.append(image.size)
                except Exception:
                    continue
    if not sizes:
        return None
    sizes.sort(key=lambda s: max(s))
    return sizes[len(sizes) // 2]


def ensure_columns(conn):
    have = {row[1] for row in conn.execute('PRAGMA table_info(volumes)')}
    for name, kind in PAGE_SIZE_COLUMNS:
        if name not in have:
            conn.execute(f'ALTER TABLE volumes ADD COLUMN {name} {kind}')
    conn.commit()


def measure_pending(db_path, limit=BATCH_SIZE, pause=0.05):
    """Mesure jusqu'à `limit` tomes possédés pas encore mesurés pour leur fichier actuel.
    Renvoie le nombre de tomes traités."""
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        ensure_columns(conn)
        rows = conn.execute(
            "SELECT id, filepath, format, file_size FROM volumes "
            "WHERE filepath IS NOT NULL AND filepath <> '' "
            "AND (page_width IS NULL OR page_size_file_size IS NOT file_size) "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        for volume_id, filepath, fmt, file_size in rows:
            try:
                size = measure(filepath, fmt)
            except Exception:
                size = None
            width, height = size or (0, 0)
            conn.execute('UPDATE volumes SET page_width = ?, page_height = ?, page_size_file_size = ? WHERE id = ?',
                         (width, height, file_size, volume_id))
            conn.commit()
            if pause:
                time.sleep(pause)
        return len(rows)
    finally:
        conn.close()


class PageSizeScheduler:
    def __init__(self):
        self.scheduler = None
        self.app = None

    def start(self, app):
        if self.scheduler is not None:
            return
        self.app = app
        self.scheduler = BackgroundScheduler(daemon=True)
        self.scheduler.add_job(
            func=self._run, trigger=IntervalTrigger(minutes=INTERVAL_MINUTES),
            next_run_time=datetime.now() + timedelta(minutes=2),
            id='volume_page_size', name='Volume page size', replace_existing=True,
            coalesce=True, max_instances=1,
        )
        self.scheduler.start()
        print(f"✓ Mesure de la taille des pages des tomes programmée: toutes les {INTERVAL_MINUTES} minutes")

    def _run(self):
        with self.app.app_context():
            try:
                measure_pending(self.app.config['DATABASE'])
            except Exception as exc:
                print(f"⚠️ Mesure de la taille des pages en échec: {exc}")


page_size_scheduler = PageSizeScheduler()
