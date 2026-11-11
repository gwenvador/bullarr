"""Amélioration (upscaling) des pages en basse définition pour le lecteur intégré.

Deux modèles Real-ESRGAN exportés en ONNX (static/models, partagés avec le calcul dans le
navigateur, voir static/js/reader-upscale.js) : « fast » (realesr-animevideov3, léger) et
« quality » (RealESRGAN_x4plus_anime_6B, plus fin mais ~15 fois plus lent). Les deux
agrandissent x4 ; la page est rendue à x2 (assez pour un écran, quatre fois moins lourde).

Le calcul se fait par tuiles de TILE px (bord de PAD px recouvert pour éviter les
coutures), sur le processeur via onnxruntime, une page à la fois (verrou) : sur un petit
serveur, une page prend de quelques secondes (fast) à quelques minutes (quality). Le
résultat est gardé sur disque (CACHE_DIR), pour ne jamais recalculer la même page.
"""
import hashlib
import io
import os
import threading

LOW_DEF_MAX_SIDE = 1600   # même seuil que l'étiquette « Basse déf. » (library.js)
SCALE = 4                 # facteur des modèles
OUTPUT_SCALE = 2          # facteur de la page rendue
TILE = 256
PAD = 12
MODELS = {'fast': 'realesrgan-fast.onnx', 'quality': 'realesrgan-quality.onnx'}
MODELS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                          'static', 'models')

_lock = threading.Lock()
_sessions = {}


def available():
    """onnxruntime et numpy installés, et au moins un modèle présent."""
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return any(os.path.isfile(os.path.join(MODELS_DIR, name)) for name in MODELS.values())


def needs_upscale(width, height):
    return 0 < max(width, height) <= LOW_DEF_MAX_SIDE


def _session(model):
    import onnxruntime as ort
    session = _sessions.get(model)
    if session is None:
        options = ort.SessionOptions()
        # Laisse un cœur au reste de l'application.
        options.intra_op_num_threads = max(1, (os.cpu_count() or 2) - 1)
        session = ort.InferenceSession(os.path.join(MODELS_DIR, MODELS[model]), options,
                                       providers=['CPUExecutionProvider'])
        _sessions[model] = session
    return session


def tile_origins(size, tile=TILE, pad=PAD):
    """Découpe d'un côté de `size` px : liste de (début de la zone gardée, sa fin, début de
    la tuile lue). Chaque tuile fait `tile` px (ou `size` si plus petit) et déborde de `pad`
    px autour de la zone gardée, sauf au bord de l'image."""
    if size <= tile:
        return [(0, size, 0)]
    core = tile - 2 * pad
    spans = []
    start = 0
    while start < size:
        end = min(start + core, size)
        read = min(max(0, start - pad), size - tile)
        spans.append((start, end, read))
        start = end
    return spans


def upscale_array(rgb, run):
    """rgb : tableau uint8 (H, W, 3). run(tuile float32 NCHW [0,1]) -> sortie x SCALE.
    Renvoie le tableau uint8 (H*SCALE, W*SCALE, 3)."""
    import numpy as np
    height, width = rgb.shape[:2]
    out = np.zeros((height * SCALE, width * SCALE, 3), dtype=np.uint8)
    source = rgb.astype(np.float32) / 255.0
    for y0, y1, ry in tile_origins(height):
        th = min(TILE, height)
        for x0, x1, rx in tile_origins(width):
            tw = min(TILE, width)
            tile = source[ry:ry + th, rx:rx + tw].transpose(2, 0, 1)[None]
            result = run(np.ascontiguousarray(tile))[0]
            core = result[:, (y0 - ry) * SCALE:(y1 - ry) * SCALE, (x0 - rx) * SCALE:(x1 - rx) * SCALE]
            out[y0 * SCALE:y1 * SCALE, x0 * SCALE:x1 * SCALE] = (
                np.clip(core, 0, 1).transpose(1, 2, 0) * 255.0 + 0.5).astype(np.uint8)
    return out


def upscale_image(data, model):
    """Octets d'une page -> octets JPEG agrandis x OUTPUT_SCALE, ou None si la page n'est
    pas en basse définition."""
    import numpy as np
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        if not needs_upscale(*image.size):
            return None
        rgb = np.asarray(image.convert('RGB'))
    session = _session(model)
    input_name = session.get_inputs()[0].name
    big = upscale_array(rgb, lambda tile: session.run(None, {input_name: tile})[0])
    height, width = rgb.shape[:2]
    result = Image.fromarray(big).resize((width * OUTPUT_SCALE, height * OUTPUT_SCALE), Image.LANCZOS)
    buffer = io.BytesIO()
    result.save(buffer, 'JPEG', quality=90, optimize=True)
    return buffer.getvalue()


def cache_path(cache_dir, filepath, number, model):
    """Fichier du cache pour une page : dépend du fichier (chemin, taille, date), de la page
    et du modèle, donc un tome remplacé est recalculé."""
    try:
        stat = os.stat(filepath)
        stamp = f'{stat.st_size}:{int(stat.st_mtime)}'
    except OSError:
        stamp = ''
    key = hashlib.sha1(f'{filepath}|{stamp}|{number}|{model}'.encode('utf-8')).hexdigest()
    return os.path.join(cache_dir, key[:2], f'{key}.jpg')


NOT_LOW_DEF = b''


def enhanced_page(filepath, fmt, number, model, cache_dir, read_page):
    """Octets JPEG de la page améliorée (depuis le cache ou calculée), ou None si la page
    n'en a pas besoin. read_page(filepath, fmt, number) -> (octets, mimetype)."""
    if model not in MODELS:
        raise ValueError('Modèle inconnu')
    path = cache_path(cache_dir, filepath, number, model)
    if os.path.isfile(path):
        with open(path, 'rb') as handle:
            data = handle.read()
        return data or None
    data, _mimetype = read_page(filepath, fmt, number)
    with _lock:
        if os.path.isfile(path):   # calculée pendant l'attente du verrou
            with open(path, 'rb') as handle:
                cached = handle.read()
            return cached or None
        result = upscale_image(data, model)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temp = f'{path}.tmp'
        with open(temp, 'wb') as handle:
            handle.write(result or NOT_LOW_DEF)
        os.replace(temp, path)
    prune(cache_dir)
    return result


CACHE_MAX_BYTES = 1 << 30


def prune(cache_dir, max_bytes=CACHE_MAX_BYTES):
    """Garde le cache sous max_bytes en supprimant les pages les plus anciennes."""
    entries = []
    for root, _dirs, files in os.walk(cache_dir):
        for name in files:
            path = os.path.join(root, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            entries.append((stat.st_mtime, stat.st_size, path))
    total = sum(size for _mtime, size, _path in entries)
    for _mtime, size, path in sorted(entries):
        if total <= max_bytes:
            break
        try:
            os.remove(path)
            total -= size
        except OSError:
            pass
