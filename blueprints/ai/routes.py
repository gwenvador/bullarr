"""Routes /api/ai : configuration de l'IA et traitements des tags (voir service.py).

Les traitements longs (création et normalisation des tags) tournent dans un fil à part ;
l'interface suit leur avancement avec GET /api/ai/job. Un seul traitement à la fois.
Chaque traitement, et chaque application de regroupements, est noté dans l'historique
(history.py, GET /api/ai/history).

Quand une IA échoue, la suivante de l'ordre de préférence prend le relais (ProviderChain).
La création des tags enregistre son avancement après chaque lot (resume.py) : arrêtée,
en erreur ou coupée par un redémarrage, elle se reprend où elle en était.
"""
import json
import threading
import time

from flask import current_app, jsonify, request

from error_utils import error_message
from . import ai_bp
from .auto_tag import (
    AUTO_TAG_DELAY, AUTO_TAG_MAX_ATTEMPTS, AUTO_TAG_MAX_SERIES, auto_tag_active, ensure_table,
    pending_count,
)
from .history import add_entry, clear_history, load_history
from .resume import clear_state, load_state, new_state, save_state
from .resume import summary as resume_summary
from .service import (
    GENERATE_SCHEMA, AiError, ProviderChain, _db, aiautotag_series, generate_batch_size, provider_chain,
    apply_tag_mapping, call_json, clean_tag, custom_prompt, active_model, count_series, generate_system, list_models,
    load_ai_config, merge_form, merge_generated_tags, normalize_proposals, parse_tags, public_config,
    save_ai_config, series_brief, series_to_tag, test_connection, vocabulary,
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
                # IA de secours : le dernier fournisseur utilisé, et les échecs rencontrés.
                more = dict(extra or {})
                if job.get('fallbacks'):
                    more['fallbacks'] = job['fallbacks']
                entry = add_entry(kind, job.get('status'), job.get('message'),
                                  provider=job.get('provider') or provider,
                                  model=job.get('model') or model, started_at=job.get('started_at'),
                                  details=details, extra=more)
                # L'interface recharge l'historique quand cet identifiant apparaît.
                _update_job(history_id=entry['id'])
            except Exception:  # noqa: BLE001 - l'historique ne doit pas casser le traitement
                current_app.logger.exception('Historique IA : entrée non enregistrée')

    threading.Thread(target=run, name=f'ai-{kind}', daemon=True).start()
    return None


def _enabled_config():
    cfg = load_ai_config()
    if not cfg.get('enabled') or not provider_chain(cfg):
        raise AiError('Activez l\'IA et renseignez une clé API dans Configuration → IA')
    return cfg


def _job_chain(cfg, llm_only=False):
    """ProviderChain dont les changements de fournisseur sont notés dans le traitement en cours."""
    def note(chain):
        current = chain.current
        _update_job(provider=current['provider'], model=active_model(current),
                    fallbacks=list(chain.failures))

    chain = ProviderChain(cfg, llm_only=llm_only, on_switch=note)
    note(chain)
    return chain


def _stop_requested():
    with _job_lock:
        return bool(_job.get('stop_requested'))


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
    error = merge_form(cfg, {**{key: value for key, value in data.items() if key != 'order'}, 'enabled': True})
    if error:
        return jsonify({'success': False, 'error': error}), 400
    try:
        name = test_connection(cfg)
    except AiError as exc:
        return jsonify({'success': False, 'error': error_message(exc)}), 400
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
        return jsonify({'success': False, 'error': error_message(exc)}), 400
    return jsonify({'success': True, 'models': models})


@ai_bp.route('/job', methods=['GET'])
def ai_job():
    job = _job_snapshot()
    # Création des tags interrompue : l'interface propose de la reprendre.
    job['resume'] = None if job.get('status') == 'running' and job.get('kind') == 'generate' else resume_summary()
    return jsonify(job)


@ai_bp.route('/job/stop', methods=['POST'])
def ai_job_stop():
    """Arrête la création des tags après le lot en cours (elle peut être reprise)."""
    with _job_lock:
        if _job.get('status') != 'running' or _job.get('kind') != 'generate':
            return jsonify({'success': False, 'error': 'Aucune création de tags en cours'}), 409
        _job['stop_requested'] = True
        _job['message'] = (_job.get('message') or '') + ' · arrêt après ce lot…'
    return jsonify({'success': True})


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
    entries = load_history()
    series_id = request.args.get('series_id', type=int)
    if series_id is not None:
        # Historique d'une série : les traitements qui la concernent, réduits à ses tags.
        kept = []
        for entry in entries:
            mine = [item for item in entry.get('details') or []
                    if isinstance(item, dict) and item.get('series_id') == series_id]
            if mine:
                kept.append({**entry, 'details': mine, 'details_total': len(mine)})
        entries = kept
    return jsonify({'entries': entries})


# ===== Création des tags =====

def _rows_by_ids(conn, ids):
    """Séries ids, dans cet ordre (celles supprimées entre-temps sont ignorées)."""
    found = {}
    for start in range(0, len(ids), 500):
        part = ids[start:start + 500]
        for row in conn.execute(f'SELECT * FROM series WHERE id IN ({",".join("?" * len(part))})', part):
            found[row['id']] = row
    return [found[series_id] for series_id in ids if series_id in found]


def _run_generate(scope, library_id, resume=False, *, details):
    cfg = _enabled_config()
    conn = _db()
    try:
        state = load_state() if resume else None
        if resume and not state:
            raise AiError('Aucune création de tags à reprendre')
        if state:
            rows = _rows_by_ids(conn, [int(series_id) for series_id in state['remaining']])
        else:
            rows = series_to_tag(conn, scope, library_id)
            state = new_state(scope, library_id, [row['id'] for row in rows])
            save_state(state)

        def after_batch(ids, changed, added):
            done = set(ids)
            state['remaining'] = [series_id for series_id in state['remaining'] if series_id not in done]
            state['changed'] += changed
            state['added'] += added
            save_state(state)

        total = int(state.get('total') or len(rows))
        finished = _tag_rows(cfg, conn, rows, details, after_batch=after_batch,
                             done_before=total - len(rows), total=total,
                             counts=(state.get('changed', 0), state.get('added', 0)))
        if finished:
            clear_state()
    finally:
        conn.close()


def _generate(cfg, known_tags, system, briefs, max_tokens):
    """Tags proposés pour briefs ({'series': [{id, tags}]}), quel que soit le fournisseur."""
    if cfg.get('provider') == 'aiautotagging':
        return aiautotag_series(briefs, known_tags)
    user = 'Séries à taguer (JSON) :\n' + json.dumps(briefs, ensure_ascii=False, indent=1)
    # Système identique pour tous les lots : mis en cache pour ne le payer qu'une fois.
    return call_json(cfg, system, user, GENERATE_SCHEMA, cache_system=True, max_tokens=max_tokens)


def _tag_summary(changed, added):
    return (f'{changed} série{"s" if changed > 1 else ""} enrichie{"s" if changed > 1 else ""}, '
            f'{added} tag{"s" if added > 1 else ""} ajouté{"s" if added > 1 else ""}')


def _tag_rows(cfg, conn, rows, details, after_batch=None, done_before=0, total=None, counts=(0, 0)):
    """Fait créer par l'IA les tags des séries rows, par lots, en suivant l'avancement.
    after_batch(ids, séries modifiées, tags ajoutés) est appelé après chaque lot enregistré.
    done_before, total, counts : avancement d'une création reprise. Renvoie True si toutes
    les séries ont été traitées, False si le traitement a été arrêté."""
    total = total if total is not None else len(rows)
    changed, added = counts
    if not rows:
        _update_job(status='done', message='Aucune série à traiter' if not done_before else _tag_summary(changed, added),
                    result={'changed': changed, 'added': added, 'total': total})
        return True
    chain = _job_chain(cfg)
    known_tags = vocabulary(conn)
    system = generate_system(known_tags, custom_prompt(cfg, 'generate'))
    _update_job(total=total, progress=done_before, message=f'{done_before} / {total} séries')
    position = 0
    while position < len(rows):
        if _stop_requested():
            done = done_before + position
            _update_job(status='stopped', result={'changed': changed, 'added': added, 'total': total},
                        message=f'Arrêtée à {done} / {total} séries : {_tag_summary(changed, added)}')
            return False
        current = chain.current
        # La taille des lots dépend du fournisseur : recalculée après un passage en secours.
        batch = rows[position:position + generate_batch_size(current)]
        try:
            data = _generate(current, known_tags, system, [series_brief(row) for row in batch], 32000)
        except Exception as exc:  # noqa: BLE001 - IA suivante, ou AiError s'il n'y en a plus
            chain.fail(exc)
            continue
        chain.succeeded(current)
        batch_changed, batch_added = merge_generated_tags(
            conn, data.get('series') or [], {row['id'] for row in batch}, details)
        changed += batch_changed
        added += batch_added
        if after_batch:
            after_batch([row['id'] for row in batch], batch_changed, batch_added)
        position += len(batch)
        done = done_before + position
        _update_job(progress=done, message=f'{done} / {total} séries')
    _update_job(status='done', result={'changed': changed, 'added': added, 'total': total},
                message=_tag_summary(changed, added))
    return True


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

        def done(ids, *_counts):
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
        return jsonify({'success': False, 'error': error_message(exc)}), 400
    conn = _db()
    try:
        row = conn.execute('SELECT * FROM series WHERE id = ?', (series_id,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': 'Série introuvable'}), 404
        known_tags = vocabulary(conn)
        system = generate_system(known_tags, custom_prompt(cfg, 'generate'))
        chain = ProviderChain(cfg)
        data = chain.call(lambda current: _generate(current, known_tags, system, [series_brief(row)], 4000))
    except AiError as exc:
        add_entry('series', 'error', str(exc), provider=cfg.get('provider'), model=active_model(cfg),
                  started_at=started, extra={'title': row['title'] if row else ''})
        return jsonify({'success': False, 'error': error_message(exc)}), 502
    finally:
        conn.close()
    # IA de secours : celle qui a répondu.
    cfg = chain.current
    fallback_extra = {'fallbacks': chain.failures} if chain.failures else {}
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
              extra={'title': row['title'], **fallback_extra})
    return jsonify({'success': True, 'tags': proposed, 'model': active_model(cfg)})


@ai_bp.route('/tags/generate', methods=['POST', 'DELETE'])
def ai_generate_tags():
    """POST {scope, library_id} : nouvelle création ; POST {resume: true} : reprend celle
    interrompue. DELETE : abandonne la création interrompue."""
    if request.method == 'DELETE':
        job = _job_snapshot()
        if job.get('status') == 'running' and job.get('kind') == 'generate':
            return jsonify({'success': False, 'error': 'Arrêtez d\'abord la création en cours'}), 409
        clear_state()
        return jsonify({'success': True})
    data = request.get_json(silent=True) or {}
    resume = bool(data.get('resume'))
    state = load_state() if resume else None
    if resume and not state:
        return jsonify({'success': False, 'error': 'Aucune création de tags à reprendre'}), 400
    scope = (state or data).get('scope')
    scope = scope if scope in ('untagged', 'all') else 'untagged'
    library_id = (state or data).get('library_id') or None
    try:
        _enabled_config()
    except AiError as exc:
        return jsonify({'success': False, 'error': error_message(exc)}), 400
    extra = {'scope': scope, **({'resumed': True} if resume else {})}
    error = _start_job('generate', _run_generate, scope, library_id, resume, extra=extra)
    if error:
        return jsonify({'success': False, 'error': error}), 409
    return jsonify({'success': True})


# ===== Normalisation des tags =====

def _run_normalize(details):
    cfg = _enabled_config()
    _update_job(message='Analyse des tags par l\'IA…')
    conn = _db()
    try:
        def progress(done, total):
            if total > 1:
                _update_job(progress=done, total=total,
                            message=f'Analyse des tags par l\'IA : partie {done} / {total}…')

        groups = normalize_proposals(_job_chain(cfg, llm_only=True), conn, progress)
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
        ProviderChain(_enabled_config(), llm_only=True)  # au moins un modèle de langage
    except AiError as exc:
        return jsonify({'success': False, 'error': error_message(exc)}), 400
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
