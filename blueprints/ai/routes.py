"""Routes /api/ai : configuration de l'IA et traitements des tags (voir service.py).

Les traitements longs (création et normalisation des tags) tournent dans un fil à part ;
l'interface suit leur avancement avec GET /api/ai/job. Un seul traitement à la fois.
Chaque traitement, et chaque application de regroupements, est noté dans l'historique
(history.py, GET /api/ai/history).
"""
import json
import threading
import time

from flask import current_app, jsonify, request

from . import ai_bp
from .auto_tag import (
    AUTO_TAG_DELAY, AUTO_TAG_MAX_ATTEMPTS, AUTO_TAG_MAX_SERIES, auto_tag_active, ensure_table,
    pending_count,
)
from .history import add_entry, clear_history, load_history
from .service import (
    GENERATE_SCHEMA, AiError, _db, aiautotag_series, generate_batch_size, provider_ready, supports_llm, apply_tag_mapping, call_json, clean_tag,
    active_key, active_model, count_series, generate_system, list_models, load_ai_config, merge_form, merge_generated_tags,
    normalize_proposals, parse_tags, public_config, save_ai_config, series_brief, series_to_tag,
    test_connection, vocabulary,
)

_job_lock = threading.Lock()
_job = {'kind': None, 'status': 'idle'}


def _job_snapshot():
    with _job_lock:
        return dict(_job)


def _update_job(**values):
    with _job_lock:
        _job.update(values)


def _start_job(kind, target, *args, extra=None):
    """Lance target(*args, details=[...]) dans un fil et note le résultat dans l'historique.
    Renvoie un message d'erreur si un traitement tourne déjà."""
    with _job_lock:
        if _job.get('status') == 'running':
            return 'Un traitement IA est déjà en cours'
        _job.clear()
        _job.update({'kind': kind, 'status': 'running', 'progress': 0, 'total': 0,
                     'message': 'Démarrage…', 'started_at': time.time()})
    app = current_app._get_current_object()
    cfg = load_ai_config()
    provider, model = cfg.get('provider'), active_model(cfg)

    def run():
        # Détail des changements, gardé même si le traitement s'arrête en route
        # (les lots déjà traités sont enregistrés).
        details = []
        with app.app_context():
            try:
                target(*args, details=details)
            except AiError as exc:
                _update_job(status='error', message=str(exc))
            except Exception as exc:  # noqa: BLE001 - l'erreur est affichée dans l'interface
                current_app.logger.exception('Traitement IA en échec')
                _update_job(status='error', message=f'Erreur inattendue : {exc}')
            job = _job_snapshot()
            try:
                entry = add_entry(kind, job.get('status'), job.get('message'), provider=provider,
                                  model=model, started_at=job.get('started_at'), details=details,
                                  extra=extra)
                # L'interface recharge l'historique quand cet identifiant apparaît.
                _update_job(history_id=entry['id'])
            except Exception:  # noqa: BLE001 - l'historique ne doit pas casser le traitement
                current_app.logger.exception('Historique IA : entrée non enregistrée')

    threading.Thread(target=run, name=f'ai-{kind}', daemon=True).start()
    return None


def _enabled_config():
    cfg = load_ai_config()
    if not cfg.get('enabled') or not provider_ready(cfg):
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


@ai_bp.route('/models', methods=['POST'])
def ai_models():
    """Modèles du fournisseur saisi dans le formulaire (clé pas forcément enregistrée)."""
    data = request.get_json(silent=True) or {}
    cfg = load_ai_config()
    error = merge_form(cfg, {key: data[key] for key in ('provider', 'api_key', 'base_url') if key in data})
    if error:
        return jsonify({'success': False, 'error': error}), 400
    try:
        models = list_models(cfg)
    except AiError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    return jsonify({'success': True, 'models': models})


@ai_bp.route('/job', methods=['GET'])
def ai_job():
    return jsonify(_job_snapshot())


@ai_bp.route('/stats', methods=['GET'])
def ai_stats():
    """Nombre de séries et de séries sans tags, affichés dans le choix « Créer des tags »."""
    conn = _db()
    try:
        stats = count_series(conn, request.args.get('library_id') or None)
        stats['auto_pending'] = pending_count(conn)
        return jsonify(stats)
    finally:
        conn.close()


@ai_bp.route('/history', methods=['GET', 'DELETE'])
def ai_history():
    if request.method == 'DELETE':
        clear_history()
        return jsonify({'success': True})
    return jsonify({'entries': load_history()})


# ===== Création des tags =====

def _run_generate(scope, library_id, details):
    cfg = _enabled_config()
    conn = _db()
    try:
        _tag_rows(cfg, conn, series_to_tag(conn, scope, library_id), details)
    finally:
        conn.close()


def _generate(cfg, known_tags, system, briefs, max_tokens):
    """Tags proposés pour briefs ({'series': [{id, tags}]}), quel que soit le fournisseur."""
    if cfg.get('provider') == 'aiautotagging':
        return aiautotag_series(briefs, known_tags)
    user = 'Séries à taguer (JSON) :\n' + json.dumps(briefs, ensure_ascii=False, indent=1)
    # Système identique pour tous les lots : mis en cache pour ne le payer qu'une fois.
    return call_json(cfg, system, user, GENERATE_SCHEMA, cache_system=True, max_tokens=max_tokens)


def _tag_rows(cfg, conn, rows, details, after_batch=None):
    """Fait créer par l'IA les tags des séries rows, par lots, en suivant l'avancement."""
    if not rows:
        _update_job(status='done', message='Aucune série à traiter', result={'changed': 0, 'added': 0})
        return
    known_tags = vocabulary(conn)
    system = generate_system(known_tags)
    total = len(rows)
    size = generate_batch_size(cfg)
    changed = added = 0
    _update_job(total=total, message=f'0 / {total} séries')
    for start in range(0, total, size):
        batch = rows[start:start + size]
        data = _generate(cfg, known_tags, system, [series_brief(row) for row in batch], 32000)
        batch_changed, batch_added = merge_generated_tags(
            conn, data.get('series') or [], {row['id'] for row in batch}, details)
        changed += batch_changed
        added += batch_added
        if after_batch:
            after_batch([row['id'] for row in batch])
        done = min(start + size, total)
        _update_job(progress=done, message=f'{done} / {total} séries')
    _update_job(status='done', result={'changed': changed, 'added': added, 'total': total},
                message=f'{changed} série{"s" if changed > 1 else ""} enrichie{"s" if changed > 1 else ""}, '
                        f'{added} tag{"s" if added > 1 else ""} ajouté{"s" if added > 1 else ""}')


# ===== Tags des nouvelles séries (auto_tag.py) =====

def _run_auto(series_ids, details):
    cfg = _enabled_config()
    conn = _db()
    try:
        marks = ','.join('?' * len(series_ids))
        conn.execute(f'UPDATE ai_autotag_queue SET attempts = attempts + 1 WHERE series_id IN ({marks})',
                     series_ids)
        conn.commit()
        rows = conn.execute(f'SELECT * FROM series WHERE id IN ({marks}) ORDER BY title COLLATE NOCASE',
                            series_ids).fetchall()

        def done(ids):
            conn.execute(f'DELETE FROM ai_autotag_queue WHERE series_id IN ({",".join("?" * len(ids))})', ids)
            conn.commit()

        found = {row['id'] for row in rows}
        gone = [series_id for series_id in series_ids if series_id not in found]
        if gone:
            done(gone)  # séries supprimées entre-temps
        _tag_rows(cfg, conn, rows, details, after_batch=done)
    finally:
        conn.close()


def run_auto_tag_tick():
    """Appelé par le planificateur : lance l'IA sur les nouvelles séries en attente."""
    cfg = load_ai_config()
    conn = _db()
    try:
        ensure_table(conn)
        if not auto_tag_active(cfg):
            conn.execute('DELETE FROM ai_autotag_queue')
            conn.commit()
            return None
        # Après AUTO_TAG_MAX_ATTEMPTS échecs (clé refusée, quota…), la série est abandonnée.
        conn.execute('DELETE FROM ai_autotag_queue WHERE attempts >= ?', (AUTO_TAG_MAX_ATTEMPTS,))
        conn.commit()
        ids = [row[0] for row in conn.execute(
            'SELECT series_id FROM ai_autotag_queue WHERE queued_at <= ? ORDER BY queued_at LIMIT ?',
            (time.time() - AUTO_TAG_DELAY, AUTO_TAG_MAX_SERIES))]
    finally:
        conn.close()
    if not ids:
        return None
    # Si un autre traitement tourne, on réessaie au prochain passage.
    return _start_job('auto', _run_auto, ids, extra={'scope': 'new'})


@ai_bp.route('/series/<int:series_id>/suggest', methods=['POST'])
def ai_suggest_series_tags(series_id):
    """Propose des tags pour une seule série (fenêtre Tags de la fiche), sans rien enregistrer :
    l'utilisateur choisit ensuite ceux qu'il ajoute."""
    started = time.time()
    try:
        cfg = _enabled_config()
    except AiError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    conn = _db()
    try:
        row = conn.execute('SELECT * FROM series WHERE id = ?', (series_id,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': 'Série introuvable'}), 404
        known_tags = vocabulary(conn)
        data = _generate(cfg, known_tags, generate_system(known_tags), [series_brief(row)], 4000)
    except AiError as exc:
        add_entry('series', 'error', str(exc), provider=cfg.get('provider'), model=active_model(cfg),
                  started_at=started, extra={'title': row['title'] if row else ''})
        return jsonify({'success': False, 'error': str(exc)}), 502
    finally:
        conn.close()
    current = parse_tags(row['tags'])
    proposed = []
    for item in data.get('series') or []:
        for tag in item.get('tags') or []:
            tag = clean_tag(tag)
            if tag and tag not in current and tag not in proposed:
                proposed.append(tag)
    count = len(proposed)
    add_entry('series', 'done',
              f'{count} tag{"s" if count > 1 else ""} proposé{"s" if count > 1 else ""}',
              provider=cfg.get('provider'), model=active_model(cfg), started_at=started,
              details=[{'series_id': series_id, 'title': row['title'], 'added': proposed}],
              extra={'title': row['title']})
    return jsonify({'success': True, 'tags': proposed, 'model': active_model(cfg)})


@ai_bp.route('/tags/generate', methods=['POST'])
def ai_generate_tags():
    data = request.get_json(silent=True) or {}
    scope = data.get('scope') if data.get('scope') in ('untagged', 'all') else 'untagged'
    library_id = data.get('library_id') or None
    try:
        _enabled_config()
    except AiError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    error = _start_job('generate', _run_generate, scope, library_id, extra={'scope': scope})
    if error:
        return jsonify({'success': False, 'error': error}), 409
    return jsonify({'success': True})


# ===== Normalisation des tags =====

def _run_normalize(details):
    cfg = _enabled_config()
    _update_job(message='Analyse des tags par l\'IA…')
    conn = _db()
    try:
        groups = normalize_proposals(cfg, conn)
    finally:
        conn.close()
    details.extend({'canonical': group['canonical'], 'variants': [v['tag'] for v in group['variants']]}
                   for group in groups)
    count = len(groups)
    _update_job(status='done', result={'groups': groups},
                message=f'{count} regroupement{"s" if count > 1 else ""} proposé{"s" if count > 1 else ""}'
                if count else 'Aucun tag à regrouper')


@ai_bp.route('/tags/normalize', methods=['POST'])
def ai_normalize_tags():
    try:
        cfg = _enabled_config()
        if not supports_llm(cfg):
            raise AiError('La normalisation demande un modèle de langage : aiautotagging.com ne sait que '
                          'proposer des tags. Choisissez un autre fournisseur ci-dessus (Claude, Gemini, '
                          'ChatGPT, OpenRouter, Groq ou un LLM local)')
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
    applied = []
    for group in data.get('groups') or []:
        if not isinstance(group, dict):
            continue
        canonical = clean_tag(group.get('canonical'))
        if not canonical:
            continue
        merged = []
        for variant in group.get('variants') or []:
            variant = str(variant or '')
            if variant and variant != canonical:
                mapping[variant] = canonical
                merged.append(variant)
        if merged:
            applied.append({'canonical': canonical, 'variants': merged})
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
    renamed = len(mapping)
    add_entry('apply', 'done',
              f'{renamed} tag{"s" if renamed > 1 else ""} fusionné{"s" if renamed > 1 else ""}, '
              f'{changed} série{"s" if changed > 1 else ""} modifiée{"s" if changed > 1 else ""}',
              details=applied)
    return jsonify({'success': True, 'changed': changed, 'renamed': len(mapping)})
