"""Amélioration (upscaling) des pages en basse définition.

Les modèles sont ceux de l'application Android (mêmes poids, convertis de ncnn en ONNX,
dans static/models) : Real-ESRGAN (Rapide, Général, Général débruité, Qualité) et
Real-CUGAN (quatre variantes). Ils servent à deux endroits :
  - dans le lecteur, calcul dans le navigateur (static/js/reader-upscale.js, WebGPU) ;
  - sur le serveur, hors ligne, pour tout un tome (upscale_volume.py) : le CBZ est
    remplacé par sa version HD et l'original est gardé de côté.

Une page est agrandie x2 (OUTPUT_SCALE) : les modèles x4 sont ramenés à x2 (assez pour un
écran, quatre fois moins lourd). Le calcul se fait par tuiles de TILE px ; chaque tuile
déborde de `margin` px sur ses voisines (bord en miroir au bord de l'image) pour éviter
les coutures. Real-CUGAN consomme lui-même cette bordure (18 px) et veut des côtés pairs.
"""
import io
from collections import OrderedDict
import json
import os
import threading

LOW_DEF_MAX_SIDE = 1600   # même seuil que l'étiquette « Basse déf. » (library.js)
OUTPUT_SCALE = 2          # facteur de la page rendue
TILE = 256
PAD = 12                  # débord des tuiles Real-ESRGAN
MODELS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                          'static', 'models')

# Même catalogue (identifiants, libellés, textes) que l'application Android et que
# reader-upscale.js. scale : facteur du modèle ; border : bordure consommée par le
# modèle (0 = sortie de la taille de l'entrée x scale) ; even : côtés pairs exigés.
MODELS = {
    'fast': {'file': 'realesrgan-fast.onnx', 'label': 'Rapide', 'scale': 4, 'border': 0, 'even': False,
             'detail': 'Nettoie le grain et le flou JPEG, lettrage net. Conseillé.'},
    'general': {'file': 'realesrgan-general.onnx', 'label': 'Général', 'scale': 4, 'border': 0, 'even': False,
                'detail': 'Real-ESRGAN polyvalent ×4 : traits très nets, un peu plus lent que Rapide.'},
    'general_wdn': {'file': 'realesrgan-general-wdn.onnx', 'label': 'Général débruité', 'scale': 4, 'border': 0,
                    'even': False, 'detail': 'Comme Général, en lissant davantage le grain des scans abîmés.'},
    'cugan_se': {'file': 'realcugan-se.onnx', 'label': 'Real-CUGAN', 'scale': 2, 'border': 18, 'even': True,
                 'detail': "Fidèle au dessin d'origine, garde la trame et les textures."},
    'cugan_se_dn3': {'file': 'realcugan-se-denoise3x.onnx', 'label': 'Real-CUGAN débruité', 'scale': 2, 'border': 18,
                     'even': True, 'detail': 'Aplats propres et traits nets, efface le grain du papier.'},
    'cugan_pro': {'file': 'realcugan-pro.onnx', 'label': 'Real-CUGAN Pro', 'scale': 2, 'border': 18, 'even': True,
                  'detail': 'Version plus récente de Real-CUGAN, un peu plus nette.'},
    'cugan_pro_dn3': {'file': 'realcugan-pro-denoise3x.onnx', 'label': 'Real-CUGAN Pro débruité', 'scale': 2,
                      'border': 18, 'even': True, 'detail': 'Real-CUGAN Pro avec débruitage fort.'},
    'quality': {'file': 'realesrgan-quality.onnx', 'label': 'Qualité', 'scale': 4, 'border': 0, 'even': False,
                'detail': 'Real-ESRGAN anime ×4 : traits et lettrage les plus fins, aplats lissés. Beaucoup plus lent.'},
}
MODEL_LABELS = {key: spec['label'] for key, spec in MODELS.items()}

_lock = threading.Lock()
_sessions = {}

CONFIG_NAME = 'upscale_config.json'
DEFAULT_CONFIG = {'server_enabled': True, 'model': 'fast'}


def _config_path():
    from flask import current_app
    return os.path.join(current_app.config['DATA_DIR'], CONFIG_NAME)


def load_config():
    """Réglages (Configuration > Upscaling) : upscaling hors ligne permis, modèle par défaut."""
    config = dict(DEFAULT_CONFIG)
    try:
        with open(_config_path(), 'r', encoding='utf-8') as handle:
            config.update({k: v for k, v in json.load(handle).items() if k in DEFAULT_CONFIG})
    except (OSError, ValueError):
        pass
    if config.get('model') not in MODELS:
        config['model'] = DEFAULT_CONFIG['model']
    return config


def save_config(config):
    path = _config_path()
    temp = f'{path}.tmp'
    with open(temp, 'w', encoding='utf-8') as handle:
        json.dump({k: config[k] for k in DEFAULT_CONFIG if k in config}, handle)
    os.replace(temp, path)


def model_path(model):
    return os.path.join(MODELS_DIR, MODELS[model]['file'])


def model_status():
    """Modèles présents sur le serveur, avec leur taille."""
    status = {}
    for model in MODELS:
        path = model_path(model)
        present = os.path.isfile(path)
        status[model] = {'present': present, 'bytes': os.path.getsize(path) if present else 0}
    return status


def runtime_available():
    """onnxruntime et numpy installés, et au moins un modèle présent."""
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return any(os.path.isfile(model_path(model)) for model in MODELS)


def available():
    """Upscaling hors ligne possible et permis (Configuration > Upscaling)."""
    return runtime_available() and bool(load_config().get('server_enabled', True))


def needs_upscale(width, height):
    return 0 < max(width, height) <= LOW_DEF_MAX_SIDE


def _session(model):
    import onnxruntime as ort
    session = _sessions.get(model)
    if session is None:
        options = ort.SessionOptions()
        # Laisse un cœur au reste de l'application.
        options.intra_op_num_threads = max(1, (os.cpu_count() or 2) - 1)
        session = ort.InferenceSession(model_path(model), options, providers=['CPUExecutionProvider'])
        _sessions[model] = session
    return session


def margin(spec):
    return spec['border'] or PAD


def tile_spans(size, spec, tile=TILE):
    """Découpe d'un côté de `size` px (déjà pair si le modèle l'exige) : liste de (début,
    fin) des zones gardées, de `tile - 2 * margin` px au plus (pair pour Real-CUGAN)."""
    core = tile - 2 * margin(spec)
    if spec['even']:
        core -= core % 2
    return [(start, min(start + core, size)) for start in range(0, size, core)]


def upscale_array(rgb, run, spec):
    """rgb : tableau uint8 (H, W, 3). run(tuile float32 NCHW [0,1]) -> sortie du modèle.
    Renvoie le tableau uint8 (H*scale, W*scale, 3)."""
    import numpy as np
    height, width = rgb.shape[:2]
    scale = spec['scale']
    m = margin(spec)
    # Côtés pairs (Real-CUGAN) puis bordure en miroir tout autour.
    eh = height % 2 if spec['even'] else 0
    ew = width % 2 if spec['even'] else 0
    source = np.pad(rgb, ((m, m + eh), (m, m + ew), (0, 0)), mode='reflect').astype(np.float32) / 255.0
    out = np.zeros(((height + eh) * scale, (width + ew) * scale, 3), dtype=np.uint8)
    for y0, y1 in tile_spans(height + eh, spec):
        for x0, x1 in tile_spans(width + ew, spec):
            tile = source[y0:y1 + 2 * m, x0:x1 + 2 * m].transpose(2, 0, 1)[None]
            result = run(np.ascontiguousarray(tile))[0]
            if not spec['border']:
                result = result[:, m * scale:(m + y1 - y0) * scale, m * scale:(m + x1 - x0) * scale]
            out[y0 * scale:y1 * scale, x0 * scale:x1 * scale] = (
                np.clip(result, 0, 1).transpose(1, 2, 0) * 255.0 + 0.5).astype(np.uint8)
    return out[:height * scale, :width * scale]


def upscale_image(data, model):
    """Octets d'une page -> octets JPEG agrandis x OUTPUT_SCALE, ou None si la page n'est
    pas en basse définition."""
    import numpy as np
    from PIL import Image
    spec = MODELS[model]
    with Image.open(io.BytesIO(data)) as image:
        if not needs_upscale(*image.size):
            return None
        rgb = np.asarray(image.convert('RGB'))
    height, width = rgb.shape[:2]
    with _lock:
        session = _session(model)
        input_name = session.get_inputs()[0].name
        big = upscale_array(rgb, lambda tile: session.run(None, {input_name: tile})[0], spec)
    result = Image.fromarray(big)
    if spec['scale'] != OUTPUT_SCALE:
        result = result.resize((width * OUTPUT_SCALE, height * OUTPUT_SCALE), Image.LANCZOS)
    buffer = io.BytesIO()
    result.save(buffer, 'JPEG', quality=90, optimize=True)
    return buffer.getvalue()


PREVIEW_SIDE = 160          # côté de l'extrait source ; l'aperçu fait le double
_preview_cache = OrderedDict()
_preview_lock = threading.Lock()


def preview_crop(data, cx, cy, side=PREVIEW_SIDE):
    """Extrait carré de la page (centre en fractions cx, cy), en PNG."""
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        image = image.convert('RGB')
        side = min(side, image.width, image.height)
        left = int(round(min(max(cx * image.width - side / 2, 0), image.width - side)))
        top = int(round(min(max(cy * image.height - side / 2, 0), image.height - side)))
        crop = image.crop((left, top, left + side, top + side))
    buffer = io.BytesIO()
    crop.save(buffer, 'PNG')
    return buffer.getvalue()


def preview(key, crop_png, model):
    """Aperçu x2 de l'extrait : 'original' = simple agrandissement (bicubique), sinon le
    modèle. Les derniers résultats sont gardés en mémoire pour comparer sans recalculer."""
    from PIL import Image
    cache_key = (key, model)
    with _preview_lock:
        if cache_key in _preview_cache:
            _preview_cache.move_to_end(cache_key)
            return _preview_cache[cache_key]
    if model == 'original':
        with Image.open(io.BytesIO(crop_png)) as image:
            big = image.resize((image.width * OUTPUT_SCALE, image.height * OUTPUT_SCALE), Image.BICUBIC)
        buffer = io.BytesIO()
        big.save(buffer, 'JPEG', quality=92)
        result = buffer.getvalue()
    else:
        result = upscale_image(crop_png, model)
    with _preview_lock:
        _preview_cache[cache_key] = result
        while len(_preview_cache) > 96:
            _preview_cache.popitem(last=False)
    return result
