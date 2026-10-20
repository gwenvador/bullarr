"""
Aperçu des pages d'une série : quelques pages du premier tome possédé, extraites du fichier
lui-même (CBZ, CBR ou PDF), affichées sous la couverture de la fiche série.

Pages réparties dans l'album (environ 20 %, 40 %, 60 % et 75 %) pour montrer le dessin,
sans les toutes dernières pages, pour ne pas montrer la fin.
Les vignettes sont mises en cache dans data/covers/previews/, sous un nom dérivé du chemin
et de la date de modification du fichier (et de PREVIEW_SELECTION_VERSION) : un fichier
remplacé ou un nouveau choix de pages produit de nouvelles vignettes, et les anciennes de
ce même fichier sont supprimées.
"""
import glob
import hashlib
import io
import os
import re
import zipfile

import rarfile
from PIL import Image

from archive_utils import detect_actual_format

PREVIEW_PAGE_COUNT = 4
# Position des pages dans l'album, en fraction de sa longueur.
PREVIEW_PAGE_POSITIONS = (0.20, 0.40, 0.60, 0.75)
# À changer quand le choix des pages change : invalide les vignettes déjà en cache.
PREVIEW_SELECTION_VERSION = 3
PREVIEW_MAX_SIZE = (600, 900)
IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp')


def _natural_key(name):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r'(\d+)', name)]


def preview_page_indices(total, count=PREVIEW_PAGE_COUNT):
    """Indices (à partir de 0) des pages à montrer, répartis dans l'album (voir
    PREVIEW_PAGE_POSITIONS) : jamais la couverture, jamais la dernière page."""
    if total <= 0:
        return []
    if total <= 2:
        return list(range(total))
    positions = PREVIEW_PAGE_POSITIONS[:count]
    indices = []
    for fraction in positions:
        index = min(max(round(fraction * (total - 1)), 1), total - 2)
        if index not in indices:
            indices.append(index)
    # Album court : compléter avec les pages voisines encore libres (hors couverture et fin).
    for index in range(1, total - 1):
        if len(indices) >= min(count, total - 2):
            break
        if index not in indices:
            indices.append(index)
    return sorted(indices)


def pick_preview_volume(cursor, series_id):
    """Premier tome possédé dont le fichier est présent : tomes réguliers d'abord, par
    numéro croissant, puis intégrales, hors-séries et épisodes."""
    rows = cursor.execute('''
        SELECT id, filepath, format, volume_number, is_integral, is_hs, is_episode
        FROM volumes
        WHERE series_id = ? AND filepath IS NOT NULL AND filepath != ''
          AND COALESCE(filesystem_present, 1) = 1
        ORDER BY COALESCE(is_integral, 0) + COALESCE(is_hs, 0) + COALESCE(is_episode, 0),
                 volume_number IS NULL, volume_number, id
    ''', (series_id,)).fetchall()
    for row in rows:
        if os.path.isfile(row[1]):
            return {'id': row[0], 'filepath': row[1], 'format': row[2], 'volume_number': row[3]}
    return None


def _archive_page_images(filepath, fmt, count):
    if fmt in ('cbz', 'zip'):
        opener = zipfile.ZipFile
    elif fmt in ('cbr', 'rar'):
        opener = rarfile.RarFile
    else:
        return []
    with opener(filepath) as archive:
        names = sorted((n for n in archive.namelist()
                        if n.lower().endswith(IMAGE_EXTENSIONS) and not n.startswith('__MACOSX')),
                       key=_natural_key)
        return [(i + 1, archive.read(names[i])) for i in preview_page_indices(len(names), count)]


def _pdf_page_images(filepath, count):
    import fitz  # PyMuPDF
    with fitz.open(filepath) as doc:
        images = []
        for index in preview_page_indices(doc.page_count, count):
            page = doc.load_page(index)
            # Rendu à environ 900 px de haut : assez pour l'agrandissement, sans plus.
            zoom = max(0.5, min(3.0, PREVIEW_MAX_SIZE[1] / max(page.rect.height, 1)))
            pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            images.append((index + 1, pixmap.tobytes('jpeg')))
        return images


def extract_preview_pages(filepath, fmt, covers_dir, count=PREVIEW_PAGE_COUNT):
    """Liste de (chemin relatif servi par /covers/…, numéro de la page dans le fichier,
    couverture = page 1) des vignettes d'aperçu. Réutilise le cache s'il correspond au
    fichier actuel ; le numéro de page est inscrit dans le nom de la vignette (…-p23.jpg)."""
    previews_dir = os.path.join(covers_dir, 'previews')
    os.makedirs(previews_dir, exist_ok=True)
    file_key = hashlib.md5(filepath.encode('utf-8')).hexdigest()
    version = f'{int(os.path.getmtime(filepath))}v{PREVIEW_SELECTION_VERSION}'
    prefix = f'{file_key}-{version}-'
    cached = sorted(glob.glob(os.path.join(previews_dir, prefix + '*.jpg')), key=_natural_key)
    if cached:
        result = []
        for path in cached:
            name = os.path.basename(path)
            match = re.search(r'-p(\d+)\.jpg$', name)
            if match:
                result.append((f'covers/previews/{name}', int(match.group(1))))
        if result:
            return result

    fmt = detect_actual_format(filepath, fmt)
    raw_pages = _pdf_page_images(filepath, count) if fmt == 'pdf' else _archive_page_images(filepath, fmt, count)

    # Vignettes d'une ancienne version du même fichier : remplacées.
    for old in glob.glob(os.path.join(previews_dir, file_key + '-*.jpg')):
        os.remove(old)
    result = []
    for page_number, data in raw_pages:
        image = Image.open(io.BytesIO(data)).convert('RGB')
        image.thumbnail(PREVIEW_MAX_SIZE)
        name = f'{prefix}p{page_number}.jpg'
        image.save(os.path.join(previews_dir, name), 'JPEG', quality=82)
        result.append((f'covers/previews/{name}', page_number))
    return result
