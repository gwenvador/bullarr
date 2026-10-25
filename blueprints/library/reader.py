"""
Lecteur de BD intégré : liste des pages d'un tome et lecture d'une page, directement dans
le fichier (CBZ/ZIP, CBR/RAR ou PDF). Les pages d'archive sont renvoyées telles quelles,
celles d'un PDF sont rendues en JPEG à la demande.

La liste des pages d'un fichier est gardée en mémoire (clé : chemin + date de
modification), pour ne pas relire l'index de l'archive à chaque page tournée.
"""
import glob
import hashlib
import io
import os
import threading
import zipfile
from collections import OrderedDict

import rarfile

from archive_utils import detect_actual_format
from .preview_pages import IMAGE_EXTENSIONS, _natural_key

READER_PDF_MAX_HEIGHT = 2000
_MIME_BY_EXT = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp',
                '.gif': 'image/gif', '.bmp': 'image/bmp'}
_INDEX_CACHE_SIZE = 32
_index_cache = OrderedDict()
_index_lock = threading.Lock()


def _archive_opener(fmt):
    if fmt in ('cbz', 'zip'):
        return zipfile.ZipFile
    if fmt in ('cbr', 'rar'):
        return rarfile.RarFile
    return None


def page_index(filepath, fmt):
    """(format réel, liste des noms d'images triés) pour une archive, ou (format, nombre de
    pages) pour un PDF. Lève ValueError pour un format non lisible."""
    key = (filepath, os.path.getmtime(filepath))
    with _index_lock:
        if key in _index_cache:
            _index_cache.move_to_end(key)
            return _index_cache[key]
    real = detect_actual_format(filepath, fmt)
    if real == 'pdf':
        import fitz  # PyMuPDF
        with fitz.open(filepath) as doc:
            value = ('pdf', doc.page_count)
    else:
        opener = _archive_opener(real)
        if opener is None:
            raise ValueError(f'Format non lisible dans le lecteur : {real or fmt}')
        with opener(filepath) as archive:
            names = sorted((n for n in archive.namelist()
                            if n.lower().endswith(IMAGE_EXTENSIONS) and not n.startswith('__MACOSX')),
                           key=_natural_key)
        value = (real, names)
    with _index_lock:
        _index_cache[key] = value
        while len(_index_cache) > _INDEX_CACHE_SIZE:
            _index_cache.popitem(last=False)
    return value


def page_count(filepath, fmt):
    real, pages = page_index(filepath, fmt)
    return pages if real == 'pdf' else len(pages)


def read_page(filepath, fmt, number):
    """(octets, type MIME) de la page `number` (à partir de 1). IndexError hors limites."""
    real, pages = page_index(filepath, fmt)
    total = pages if real == 'pdf' else len(pages)
    if not 1 <= number <= total:
        raise IndexError(f'Page {number} hors de l\'album ({total} pages)')
    if real == 'pdf':
        import fitz
        with fitz.open(filepath) as doc:
            page = doc.load_page(number - 1)
            zoom = max(0.5, min(4.0, READER_PDF_MAX_HEIGHT / max(page.rect.height, 1)))
            pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            return pixmap.tobytes('jpeg'), 'image/jpeg'
    name = pages[number - 1]
    with _archive_opener(real)(filepath) as archive:
        data = archive.read(name)
    return data, _MIME_BY_EXT.get(os.path.splitext(name)[1].lower(), 'application/octet-stream')


THUMB_MAX_SIZE = (180, 270)


def thumbnail_path(filepath, fmt, number, covers_dir):
    """Chemin d'une miniature de la page `number` (bande de miniatures du lecteur), créée au
    premier appel puis gardée dans covers/reader-thumbs/ (clé : chemin + date du fichier ;
    les miniatures d'une ancienne version du fichier sont supprimées)."""
    from PIL import Image
    thumbs_dir = os.path.join(covers_dir, 'reader-thumbs')
    os.makedirs(thumbs_dir, exist_ok=True)
    file_key = hashlib.md5(filepath.encode('utf-8')).hexdigest()
    version = str(int(os.path.getmtime(filepath)))
    path = os.path.join(thumbs_dir, f'{file_key}-{version}-{int(number)}.jpg')
    if os.path.exists(path):
        return path
    data, _mimetype = read_page(filepath, fmt, number)  # IndexError hors limites
    for old in glob.glob(os.path.join(thumbs_dir, f'{file_key}-*.jpg')):
        if not os.path.basename(old).startswith(f'{file_key}-{version}-'):
            try:
                os.remove(old)
            except OSError:
                pass
    image = Image.open(io.BytesIO(data)).convert('RGB')
    image.thumbnail(THUMB_MAX_SIZE)
    temporary = f'{path}.{threading.get_ident()}.tmp'
    image.save(temporary, 'JPEG', quality=78)
    os.replace(temporary, path)
    return path
