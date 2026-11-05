"""Routes /api/ai : configuration de l'IA et traitements des tags (voir service.py).

Les traitements longs (création et normalisation des tags) tournent dans un fil à part ;
l'interface suit leur avancement avec GET /api/ai/job. Un seul traitement à la fois.
"""
import json
import threading
import time

from flask import current_app, jsonify, request

from . import ai_bp
from .service import (
    GENERATE_BATCH_SIZE, GENERATE_SCHEMA, AiError, _db, apply_tag_mapping, call_json, clean_tag,
    generate_system, load_ai_config, merge_form, merge_generated_tags, normalize_proposals,
    public_config, save_ai_config, series_brief, series_to_tag, test_connection, vocabulary,
)

_job_lock = threading.Lock()
_job = {'kind': None, 'status': 'idle'}


def _job_snapshot():
    with _job_lock:
        return dict(_job)


def _update_job(**values):
    with _job_lock:
        _job.update(values)


def _start_job(kind, target, *args):
    """Lance target(app, *args) dans un fil. Renvoie un message d'erreur si un traitement tourne."""
    with _job_lock:
        if _job.get('status') == 'running':
            return 'Un traitement IA est déjà en cours'
        _job.clear()
        _job.update({'kind': kind, 'status': 'running', 'progress': 0, 'total': 0,
                     'message': 'Démarrage…', 'started_at': time.time()})
    app = current_app._get_current_object()

    def run():
        with app.app_context():
            try:
                target(*args)
            except AiError as exc:
                _update_job(status='error', message=str(exc))
            except Exception as exc:  # noqa: BLE001 - l'erreur est affichée dans l'interface
                current_app.logger.exception('Traitement IA en échec')
                _update_job(status='error', message=f'Erreur inattendue : {exc}')

    threading.Thread(target=run, name=f'ai-{kind}', daemon=True).start()
    return None


def _enabled_config():
    cfg = load_ai_config()
    if not cfg.get('enabled') or not cfg.get('api_key_decrypted'):
        raise AiError('Activez l\'IA et renseignez une clé API dans Configuration → IA')
    return cfg


# ===== Configuration =====

@ai_bp.route('/config', methods=['GET', 'POST'])
def ai_config():
    if request.method == 'GET':
        return jsonify(public_config(load_ai_config()))
    data = request.get_json(silent=True) or {}
    cfg = load_ai_config()
    error = merge_form(cfg, data)
    if error:
        return jsonify({'success': False, 'error': error}), 400
    if not save_ai_config(cfg):
        return jsonify({'success': False, 'error': 'Erreur de sauvegarde'}), 500
    return jsonify({'success': True, 'config': public_config(load_ai_config())})


@ai_bp.route('/test', methods=['POST'])
def ai_test():
    """Vérifie la clé et le modèle saisis, sans devoir les enregistrer d'abord."""
    data = request.get_json(silent=True) or {}
    cfg = load_ai_config()
    error = merge_form(cfg, {**data, 'enabled': True})
    if error:
        return jsonify({'success': False, 'error': error}), 400
    try:
        name = test_connection(cfg)
    except AiError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    return jsonify({'success': True, 'message': f'Connexion réussie : {name}'})


@ai_bp.route('/job', methods=['GET'])
def ai_job():
    return jsonify(_job_snapshot())


# ===== Création des tags =====

def _run_generate(scope, library_id):
    cfg = _enabled_config()
    conn = _db()
    try:
        rows = series_to_tag(conn, scope, library_id)
        if not rows:
            _update_job(status='done', message='Aucune série à traiter', result={'changed': 0, 'added': 0})
            return
        system = generate_system(vocabulary(conn))
        total = len(rows)
        changed = added = 0
        _update_job(total=total, message=f'0 / {total} séries')
        for start in range(0, total, GENERATE_BATCH_SIZE):
            batch = rows[start:start + GENERATE_BATCH_SIZE]
            briefs = [series_brief(row) for row in batch]
            user = 'Séries à taguer (JSON) :\n' + json.dumps(briefs, ensure_ascii=False, indent=1)
            # Système identique pour tous les lots : mis en cache pour ne le payer qu'une fois.
            data = call_json(cfg, system, user, GENERATE_SCHEMA, cache_system=True, max_tokens=32000)
            batch_changed, batch_added = merge_generated_tags(
                conn, data.get('series') or [], {row['id'] for row in batch})
            changed += batch_changed
            added += batch_added
            done = min(start + GENERATE_BATCH_SIZE, total)
            _update_job(progress=done, message=f'{done} / {total} séries')
        _update_job(status='done', result={'changed': changed, 'added': added, 'total': total},
                    message=f'{changed} série{"s" if changed > 1 else ""} enrichie{"s" if changed > 1 else ""}, '
                            f'{added} tag{"s" if added > 1 else ""} ajouté{"s" if added > 1 else ""}')
    finally:
        conn.close()


@ai_bp.route('/tags/generate', methods=['POST'])
def ai_generate_tags():
    data = request.get_json(silent=True) or {}
    scope = data.get('scope') if data.get('scope') in ('untagged', 'all') else 'untagged'
    library_id = data.get('library_id') or None
    try:
        _enabled_config()
    except AiError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    error = _start_job('generate', _run_generate, scope, library_id)
    if error:
        return jsonify({'success': False, 'error': error}), 409
    return jsonify({'success': True})


# ===== Normalisation des tags =====

def _run_normalize():
    cfg = _enabled_config()
    _update_job(message='Analyse des tags par l\'IA…')
    conn = _db()
    try:
        groups = normalize_proposals(cfg, conn)
    finally:
        conn.close()
    count = len(groups)
    _update_job(status='done', result={'groups': groups},
                message=f'{count} regroupement{"s" if count > 1 else ""} proposé{"s" if count > 1 else ""}'
                if count else 'Aucun tag à regrouper')


@ai_bp.route('/tags/normalize', methods=['POST'])
def ai_normalize_tags():
    try:
        _enabled_config()
    except AiError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    error = _start_job('normalize', _run_normalize)
    if error:
        return jsonify({'success': False, 'error': error}), 409
    return jsonify({'success': True})


@ai_bp.route('/tags/normalize/apply', methods=['POST'])
def ai_apply_normalization():
    """Applique les regroupements validés : {"groups": [{"canonical": ..., "variants": [...]}]}."""
    data = request.get_json(silent=True) or {}
    mapping = {}
    for group in data.get('groups') or []:
        if not isinstance(group, dict):
            continue
        canonical = clean_tag(group.get('canonical'))
        if not canonical:
            continue
        for variant in group.get('variants') or []:
            variant = str(variant or '')
            if variant and variant != canonical:
                mapping[variant] = canonical
    if not mapping:
        return jsonify({'success': False, 'error': 'Aucun regroupement sélectionné'}), 400
    conn = _db()
    try:
        changed = apply_tag_mapping(conn, mapping)
    finally:
        conn.close()
    with _job_lock:
        if _job.get('kind') == 'normalize':
            _job['applied'] = True
    return jsonify({'success': True, 'changed': changed, 'renamed': len(mapping)})
