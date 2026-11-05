"""IA (blueprints/ai) : configuration, normalisation et création des tags, modèle simulé."""
import json
import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from flask import Flask

from config import Config
from encryption import ensure_encryption_key


class AiTagsTestCase(unittest.TestCase):
    def setUp(self):
        ensure_encryption_key()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, 'bullarr.db')
        conn = sqlite3.connect(self.db_path)
        conn.execute('''CREATE TABLE series (
            id INTEGER PRIMARY KEY, library_id INTEGER, title TEXT, tags TEXT,
            manual_author TEXT, bedetheque_scenaristes TEXT, bedetheque_dessinateurs TEXT,
            local_author TEXT, komga_authors TEXT, manual_summary TEXT, local_summary TEXT,
            bedetheque_description TEXT, komga_summary TEXT, manual_year_start TEXT,
            bedetheque_year_start TEXT, local_year TEXT, manual_genre TEXT, bedetheque_genre TEXT,
            local_genre TEXT, bedetheque_origin TEXT)''')
        rows = [
            (1, 1, 'Gaulle 1', ['de-gaulle', 'politique']),
            (2, 1, 'Gaulle 2', ['charles-de-gaulle']),
            (3, 1, 'Gaulle 3', ['general de gaulle', 'de-gaulle']),
            (4, 2, 'Sans tags', []),
            (5, 1, 'Autre', ['humour']),
        ]
        for sid, lib, title, tags in rows:
            conn.execute('INSERT INTO series (id, library_id, title, tags, bedetheque_description) VALUES (?, ?, ?, ?, ?)',
                         (sid, lib, title, json.dumps(tags), f'Résumé de {title}'))
        conn.commit()
        conn.close()

        self.app = Flask(__name__)
        self.app.config['DATABASE'] = self.db_path
        self.app.config['AI_CONFIG'] = dict(Config.AI_CONFIG)
        self.app.config['AI_CONFIG_FILE'] = os.path.join(self.tmp.name, 'ai_config.json')
        from blueprints.ai import ai_bp
        self.app.register_blueprint(ai_bp, url_prefix='/api/ai')
        self.client = self.app.test_client()

    def tags(self, series_id):
        conn = sqlite3.connect(self.db_path)
        try:
            raw = conn.execute('SELECT tags FROM series WHERE id = ?', (series_id,)).fetchone()[0]
        finally:
            conn.close()
        return json.loads(raw)

    def provider(self, provider_id):
        public = self.client.get('/api/ai/config').get_json()
        return next(p for p in public['providers'] if p['id'] == provider_id)

    def enable(self):
        response = self.client.post('/api/ai/config', json={
            'enabled': True, 'api_key': 'sk-ant-test-123456789', 'model': 'claude-haiku-5-5'})
        self.assertEqual(response.status_code, 200)

    def wait_history(self, count=1):
        for _ in range(100):
            entries = self.client.get('/api/ai/history').get_json()['entries']
            if len(entries) >= count:
                return entries
            time.sleep(0.02)
        self.fail('historique IA non écrit')

    def wait_job(self):
        for _ in range(100):
            job = self.client.get('/api/ai/job').get_json()
            if job['status'] != 'running':
                return job
            time.sleep(0.05)
        self.fail('Le traitement IA ne se termine pas')

    def test_config_key_is_encrypted_and_masked(self):
        self.enable()
        with open(self.app.config['AI_CONFIG_FILE'], encoding='utf-8') as handle:
            stored = json.load(handle)
        self.assertNotIn('sk-ant-test', json.dumps(stored))
        public = self.provider('anthropic')
        self.assertEqual(public['api_key'], '****')
        self.assertEqual(public['model'], 'claude-haiku-5-5')
        self.assertTrue(public['has_api_key'])
        # Renvoyer le masque garde la clé enregistrée
        self.client.post('/api/ai/config', json={'api_key': '****', 'enabled': True})
        self.assertTrue(self.provider('anthropic')['has_api_key'])

    def test_enabling_without_key_is_refused(self):
        response = self.client.post('/api/ai/config', json={'enabled': True, 'api_key': ''})
        self.assertEqual(response.status_code, 400)

    def test_unknown_model_is_refused(self):
        response = self.client.post('/api/ai/config', json={'model': 'gpt-4'})
        self.assertEqual(response.status_code, 400)

    def test_jobs_require_enabled_ai(self):
        self.assertEqual(self.client.post('/api/ai/tags/normalize').status_code, 400)
        self.assertEqual(self.client.post('/api/ai/tags/generate', json={}).status_code, 400)

    def test_normalize_proposals_keep_only_existing_tags(self):
        self.enable()
        answer = {'groups': [
            {'canonical': 'Charles de Gaulle', 'reason': 'Même personne',
             'variants': ['de-gaulle', 'charles-de-gaulle', 'general de gaulle', 'inexistant']},
            {'canonical': 'humour', 'reason': 'Rien à fusionner', 'variants': ['humour']},
        ]}
        with mock.patch('blueprints.ai.service.call_json', return_value=answer) as call:
            self.assertEqual(self.client.post('/api/ai/tags/normalize').status_code, 200)
            job = self.wait_job()
        self.assertEqual(job['status'], 'done', job)
        sent = call.call_args.args[2]
        self.assertIn('de-gaulle (2)', sent)
        groups = job['result']['groups']
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['canonical'], 'charles-de-gaulle')
        self.assertEqual([v['tag'] for v in groups[0]['variants']],
                         ['de-gaulle', 'charles-de-gaulle', 'general de gaulle'])
        # Rien n'est modifié avant validation
        self.assertEqual(self.tags(3), ['general de gaulle', 'de-gaulle'])

    def test_apply_normalization_merges_and_dedupes(self):
        response = self.client.post('/api/ai/tags/normalize/apply', json={'groups': [{
            'canonical': 'Charles de Gaulle',
            'variants': ['de-gaulle', 'charles-de-gaulle', 'general de gaulle']}]})
        data = response.get_json()
        self.assertTrue(data['success'], data)
        self.assertEqual(data['changed'], 2)
        self.assertEqual(self.tags(1), ['charles-de-gaulle', 'politique'])
        self.assertEqual(self.tags(2), ['charles-de-gaulle'])
        self.assertEqual(self.tags(3), ['charles-de-gaulle'])
        self.assertEqual(self.tags(5), ['humour'])

    def test_apply_without_groups_is_refused(self):
        response = self.client.post('/api/ai/tags/normalize/apply', json={'groups': []})
        self.assertEqual(response.status_code, 400)

    def test_generate_adds_tags_to_untagged_series_only(self):
        self.enable()

        def fake(cfg, system, user, schema, **kwargs):
            payload = json.loads(user.split('\n', 1)[1])
            self.assertEqual([item['id'] for item in payload], [4])
            self.assertIn('de-gaulle', system)  # vocabulaire existant (tag employé 2 fois)
            self.assertTrue(kwargs.get('cache_system'))
            return {'series': [{'id': 4, 'tags': ['Science Fiction', 'espace']},
                               {'id': 1, 'tags': ['intrus']}]}

        with mock.patch('blueprints.ai.routes.call_json', side_effect=fake):
            self.assertEqual(self.client.post('/api/ai/tags/generate', json={'scope': 'untagged'}).status_code, 200)
            job = self.wait_job()
        self.assertEqual(job['status'], 'done', job)
        self.assertEqual(job['result'], {'changed': 1, 'added': 2, 'total': 1})
        self.assertEqual(self.tags(4), ['science-fiction', 'espace'])
        self.assertEqual(self.tags(1), ['de-gaulle', 'politique'])

    def test_generate_all_keeps_existing_tags(self):
        self.enable()

        def fake(cfg, system, user, schema, **kwargs):
            payload = json.loads(user.split('\n', 1)[1])
            return {'series': [{'id': item['id'], 'tags': ['politique', 'histoire']} for item in payload]}

        with mock.patch('blueprints.ai.routes.call_json', side_effect=fake):
            self.client.post('/api/ai/tags/generate', json={'scope': 'all'})
            job = self.wait_job()
        self.assertEqual(job['status'], 'done', job)
        self.assertEqual(self.tags(1), ['de-gaulle', 'politique', 'histoire'])
        self.assertEqual(self.tags(5), ['humour', 'politique', 'histoire'])

    def test_model_error_is_reported_in_job(self):
        self.enable()
        from blueprints.ai.service import AiError
        with mock.patch('blueprints.ai.service.call_json', side_effect=AiError('Clé API refusée par Anthropic')):
            self.client.post('/api/ai/tags/normalize')
            job = self.wait_job()
        self.assertEqual(job['status'], 'error')
        self.assertEqual(job['message'], 'Clé API refusée par Anthropic')


    def test_each_provider_keeps_its_own_key_and_model(self):
        self.enable()
        response = self.client.post('/api/ai/config', json={
            'provider': 'gemini', 'api_key': 'AIza-gemini-key-123456', 'model': 'models/gemini-3.5-flash'})
        self.assertEqual(response.status_code, 200, response.get_json())
        public = self.client.get('/api/ai/config').get_json()
        self.assertEqual(public['provider'], 'gemini')
        gemini = self.provider('gemini')
        self.assertEqual(gemini['model'], 'gemini-3.5-flash')
        self.assertEqual(gemini['api_key_hint'], '…3456')
        self.assertEqual(self.provider('anthropic')['api_key_hint'], '…6789')
        with open(self.app.config['AI_CONFIG_FILE'], encoding='utf-8') as handle:
            self.assertNotIn('AIza', handle.read())

    def test_switching_to_provider_without_key_cannot_enable(self):
        self.enable()
        response = self.client.post('/api/ai/config', json={'provider': 'gemini', 'enabled': True})
        self.assertEqual(response.status_code, 400)

    def test_legacy_single_key_config_is_read_as_claude(self):
        from encryption import encrypt
        with open(self.app.config['AI_CONFIG_FILE'], 'w', encoding='utf-8') as handle:
            json.dump({'enabled': True, 'api_key': encrypt('sk-ant-legacy-987654321'),
                       'model': 'claude-sonnet-5-5'}, handle)
        anthropic_cfg = self.provider('anthropic')
        self.assertTrue(anthropic_cfg['has_api_key'])
        self.assertEqual(anthropic_cfg['model'], 'claude-sonnet-5-5')

    def test_gemini_schema_conversion(self):
        from blueprints.ai.service import NORMALIZE_SCHEMA, gemini_schema
        converted = gemini_schema(NORMALIZE_SCHEMA)
        self.assertEqual(converted['type'], 'OBJECT')
        self.assertEqual(converted['properties']['groups']['items']['properties']['variants']['type'], 'ARRAY')
        self.assertNotIn('additionalProperties', json.dumps(converted))

    def _gemini_response(self, payload, finish='STOP'):
        response = mock.Mock(status_code=200, ok=True)
        response.json.return_value = {'candidates': [{
            'finishReason': finish,
            'content': {'parts': [{'text': 'réflexion', 'thought': True}, {'text': json.dumps(payload)}]},
        }]}
        return response

    def test_gemini_call_sends_key_header_and_schema(self):
        self.client.post('/api/ai/config', json={
            'enabled': True, 'provider': 'gemini', 'api_key': 'AIza-gemini-key-123456',
            'model': 'gemini-3.5-flash-lite'})
        answer = {'groups': [{'canonical': 'charles-de-gaulle', 'reason': 'Même personne',
                              'variants': ['de-gaulle', 'charles-de-gaulle']}]}
        with mock.patch('blueprints.ai.service.requests.request',
                        return_value=self._gemini_response(answer)) as request:
            self.client.post('/api/ai/tags/normalize')
            job = self.wait_job()
        self.assertEqual(job['status'], 'done', job)
        self.assertEqual(job['result']['groups'][0]['canonical'], 'charles-de-gaulle')
        method, url = request.call_args.args
        self.assertEqual(method, 'POST')
        self.assertTrue(url.endswith('/models/gemini-3.5-flash-lite:generateContent'))
        kwargs = request.call_args.kwargs
        self.assertEqual(kwargs['headers']['x-goog-api-key'], 'AIza-gemini-key-123456')
        self.assertNotIn('key=', url)
        config = kwargs['json']['generationConfig']
        self.assertEqual(config['responseMimeType'], 'application/json')
        self.assertEqual(config['responseSchema']['type'], 'OBJECT')

    def test_gemini_truncated_answer_is_an_error(self):
        self.client.post('/api/ai/config', json={
            'enabled': True, 'provider': 'gemini', 'api_key': 'AIza-gemini-key-123456'})
        with mock.patch('blueprints.ai.service.requests.request',
                        return_value=self._gemini_response({}, finish='MAX_TOKENS')):
            self.client.post('/api/ai/tags/normalize')
            job = self.wait_job()
        self.assertEqual(job['status'], 'error')
        self.assertIn('tronquée', job['message'])

    def test_gemini_invalid_key_message(self):
        self.client.post('/api/ai/config', json={
            'provider': 'gemini', 'api_key': 'AIza-bad-key-0000000'})
        bad = mock.Mock(status_code=400, ok=False, text='')
        bad.json.return_value = {'error': {'message': 'API key not valid',
                                           'details': [{'reason': 'API_KEY_INVALID'}]}}
        with mock.patch('blueprints.ai.service.requests.request', return_value=bad):
            response = self.client.post('/api/ai/test', json={'provider': 'gemini'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'Clé API refusée par Google')

    def test_gemini_model_list_keeps_text_models(self):
        self.client.post('/api/ai/config', json={
            'provider': 'gemini', 'api_key': 'AIza-gemini-key-123456'})
        listing = mock.Mock(status_code=200, ok=True)
        listing.json.return_value = {'models': [
            {'name': 'models/gemini-3.5-flash-lite', 'displayName': 'Gemini 3.5 Flash-Lite',
             'supportedGenerationMethods': ['generateContent']},
            {'name': 'models/text-embedding-004', 'supportedGenerationMethods': ['embedContent']},
        ]}
        with mock.patch('blueprints.ai.service.requests.request', return_value=listing):
            data = self.client.post('/api/ai/models', json={'provider': 'gemini'}).get_json()
        self.assertTrue(data['success'], data)
        self.assertEqual([m['id'] for m in data['models']], ['gemini-3.5-flash-lite'])


    def test_stats_count_untagged_series(self):
        self.assertEqual(self.client.get('/api/ai/stats').get_json(),
                         {'total': 5, 'untagged': 1, 'auto_pending': 0})
        self.assertEqual(self.client.get('/api/ai/stats?library_id=2').get_json()['total'], 1)

    def test_history_records_generated_tags(self):
        self.enable()
        with mock.patch('blueprints.ai.routes.call_json',
                        return_value={'series': [{'id': 4, 'tags': ['espace']}]}):
            self.client.post('/api/ai/tags/generate', json={'scope': 'untagged'})
            self.wait_job()
            entry = self.wait_history()[0]
        self.assertEqual(entry['kind'], 'generate')
        self.assertEqual(entry['status'], 'done')
        self.assertEqual(entry['scope'], 'untagged')
        self.assertEqual(entry['provider'], 'anthropic')
        self.assertEqual(entry['model'], 'claude-haiku-5-5')
        self.assertEqual(entry['details'], [{'series_id': 4, 'title': 'Sans tags', 'added': ['espace']}])
        self.assertEqual(self.client.get('/api/ai/job').get_json()['history_id'], entry['id'])
        self.assertEqual(self.client.get('/api/ai/stats').get_json()['untagged'], 0)

    def test_history_records_errors_and_merges(self):
        self.enable()
        from blueprints.ai.service import AiError
        with mock.patch('blueprints.ai.service.call_json', side_effect=AiError('Quota dépassé')):
            self.client.post('/api/ai/tags/normalize')
            self.wait_job()
            self.wait_history()
        self.client.post('/api/ai/tags/normalize/apply', json={'groups': [{
            'canonical': 'charles-de-gaulle', 'variants': ['de-gaulle', 'charles-de-gaulle']}]})
        entries = self.wait_history(2)
        self.assertEqual(entries[0]['kind'], 'apply')
        self.assertEqual(entries[0]['details'], [{'canonical': 'charles-de-gaulle', 'variants': ['de-gaulle']}])
        self.assertEqual(entries[1]['status'], 'error')
        self.assertEqual(entries[1]['message'], 'Quota dépassé')
        self.assertEqual(self.client.delete('/api/ai/history').status_code, 200)
        self.assertEqual(self.client.get('/api/ai/history').get_json()['entries'], [])


    def queue(self, *ids, age=600):
        from blueprints.ai.auto_tag import ensure_table, queue_new_series
        conn = sqlite3.connect(self.db_path)
        ensure_table(conn)
        with self.app.app_context():
            queue_new_series(conn.cursor(), ids)
        conn.execute('UPDATE ai_autotag_queue SET queued_at = queued_at - ?', (age,))
        conn.commit()
        rows = [row[0] for row in conn.execute('SELECT series_id FROM ai_autotag_queue')]
        conn.close()
        return rows

    def test_new_series_not_queued_when_option_off(self):
        self.enable()
        self.assertEqual(self.queue(4), [])

    def test_new_series_tagged_automatically(self):
        self.enable()
        self.client.post('/api/ai/config', json={'auto_tag_new': True})
        self.assertTrue(self.client.get('/api/ai/config').get_json()['auto_tag_new'])
        self.assertEqual(self.queue(4), [4])
        self.assertEqual(self.client.get('/api/ai/stats').get_json()['auto_pending'], 1)
        from blueprints.ai.routes import run_auto_tag_tick
        with mock.patch('blueprints.ai.routes.call_json',
                        return_value={'series': [{'id': 4, 'tags': ['espace']}]}):
            with self.app.test_request_context():
                self.assertIsNone(run_auto_tag_tick())
            self.wait_job()
            entry = self.wait_history()[0]
        self.assertEqual(self.tags(4), ['espace'])
        self.assertEqual(entry['kind'], 'auto')
        self.assertEqual(entry['details'][0]['added'], ['espace'])
        self.assertEqual(self.client.get('/api/ai/stats').get_json()['auto_pending'], 0)

    def test_recent_series_wait_before_tagging(self):
        self.enable()
        self.client.post('/api/ai/config', json={'auto_tag_new': True})
        self.queue(4, age=0)
        from blueprints.ai.routes import run_auto_tag_tick
        with mock.patch('blueprints.ai.routes.call_json') as call:
            with self.app.test_request_context():
                run_auto_tag_tick()
        call.assert_not_called()
        self.assertEqual(self.client.get('/api/ai/stats').get_json()['auto_pending'], 1)

    def test_failed_auto_tagging_is_retried_then_dropped(self):
        self.enable()
        self.client.post('/api/ai/config', json={'auto_tag_new': True})
        self.queue(4)
        from blueprints.ai.routes import run_auto_tag_tick
        from blueprints.ai.service import AiError
        with mock.patch('blueprints.ai.routes.call_json', side_effect=AiError('Quota dépassé')):
            for attempt in range(3):
                with self.app.test_request_context():
                    run_auto_tag_tick()
                self.assertEqual(self.wait_job()['status'], 'error')
                self.wait_history(attempt + 1)
            with self.app.test_request_context():
                run_auto_tag_tick()
        self.assertEqual(self.client.get('/api/ai/stats').get_json()['auto_pending'], 0)

    def test_disabling_option_empties_queue(self):
        self.enable()
        self.client.post('/api/ai/config', json={'auto_tag_new': True})
        self.queue(4)
        self.client.post('/api/ai/config', json={'auto_tag_new': False})
        from blueprints.ai.routes import run_auto_tag_tick
        with self.app.test_request_context():
            run_auto_tag_tick()
        self.assertEqual(self.client.get('/api/ai/stats').get_json()['auto_pending'], 0)


    def enable_openai(self):
        response = self.client.post('/api/ai/config', json={
            'enabled': True, 'provider': 'openai', 'api_key': 'sk-proj-openai-key-123456',
            'model': 'gpt-5-mini'})
        self.assertEqual(response.status_code, 200, response.get_json())

    def _openai_response(self, content, finish='stop', refusal=None):
        response = mock.Mock(status_code=200, ok=True)
        response.json.return_value = {'choices': [{
            'finish_reason': finish,
            'message': {'role': 'assistant', 'content': content, 'refusal': refusal},
        }]}
        return response

    def test_openai_call_sends_bearer_key_and_strict_schema(self):
        self.enable_openai()
        answer = {'groups': [{'canonical': 'charles-de-gaulle', 'reason': 'Même personne',
                              'variants': ['de-gaulle', 'charles-de-gaulle']}]}
        with mock.patch('blueprints.ai.service.requests.request',
                        return_value=self._openai_response(json.dumps(answer))) as request:
            self.client.post('/api/ai/tags/normalize')
            job = self.wait_job()
        self.assertEqual(job['status'], 'done', job)
        self.assertEqual(job['result']['groups'][0]['canonical'], 'charles-de-gaulle')
        method, url = request.call_args.args
        self.assertEqual((method, url), ('POST', 'https://api.openai.com/v1/chat/completions'))
        kwargs = request.call_args.kwargs
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer sk-proj-openai-key-123456')
        body = kwargs['json']
        self.assertEqual(body['model'], 'gpt-5-mini')
        self.assertEqual(body['messages'][0]['role'], 'system')
        self.assertTrue(body['response_format']['json_schema']['strict'])
        self.assertIn('max_completion_tokens', body)
        with open(self.app.config['AI_CONFIG_FILE'], encoding='utf-8') as handle:
            self.assertNotIn('sk-proj', handle.read())

    def test_openai_refusal_and_truncation_are_errors(self):
        self.enable_openai()
        for response, expected in ((self._openai_response(None, refusal='Non'), 'refusé'),
                                   (self._openai_response('{', finish='length'), 'tronquée')):
            with mock.patch('blueprints.ai.service.requests.request', return_value=response):
                self.client.post('/api/ai/tags/normalize')
                job = self.wait_job()
            self.assertEqual(job['status'], 'error')
            self.assertIn(expected, job['message'])

    def test_openai_errors_are_explained(self):
        self.client.post('/api/ai/config', json={'provider': 'openai', 'api_key': 'sk-proj-bad-0000000'})
        for status, code, expected in ((401, 'invalid_api_key', 'Clé API refusée par OpenAI'),
                                       (429, 'insufficient_quota', 'Crédit OpenAI épuisé')):
            bad = mock.Mock(status_code=status, ok=False, text='')
            bad.json.return_value = {'error': {'message': 'x', 'code': code}}
            with mock.patch('blueprints.ai.service.requests.request', return_value=bad) as request:
                response = self.client.post('/api/ai/test', json={'provider': 'openai'})
            self.assertEqual(response.status_code, 400)
            self.assertIn(expected, response.get_json()['error'])
            self.assertEqual(request.call_count, 1)  # pas de nouvel essai

    def test_openai_model_list_keeps_chat_models(self):
        self.client.post('/api/ai/config', json={'provider': 'openai', 'api_key': 'sk-proj-openai-key-123456'})
        listing = mock.Mock(status_code=200, ok=True)
        listing.json.return_value = {'data': [
            {'id': 'gpt-5-mini'}, {'id': 'gpt-5'}, {'id': 'o4-mini'}, {'id': 'text-embedding-3-small'},
            {'id': 'gpt-4o-audio-preview'}, {'id': 'gpt-image-1'}, {'id': 'dall-e-3'},
        ]}
        with mock.patch('blueprints.ai.service.requests.request', return_value=listing):
            data = self.client.post('/api/ai/models', json={'provider': 'openai'}).get_json()
        self.assertTrue(data['success'], data)
        self.assertEqual([m['id'] for m in data['models']], ['o4-mini', 'gpt-5-mini', 'gpt-5'])

    def enable_openrouter(self):
        response = self.client.post('/api/ai/config', json={
            'enabled': True, 'provider': 'openrouter', 'api_key': 'sk-or-v1-test-123456',
            'model': 'anthropic/claude-haiku-5.5'})
        self.assertEqual(response.status_code, 200, response.get_json())

    def test_openrouter_call_requires_structured_output_providers(self):
        self.enable_openrouter()
        answer = {'groups': []}
        with mock.patch('blueprints.ai.service.requests.request',
                        return_value=self._openai_response('```json\n' + json.dumps(answer) + '\n```')) as request:
            self.client.post('/api/ai/tags/normalize')
            job = self.wait_job()
        self.assertEqual(job['status'], 'done', job)
        method, url = request.call_args.args
        self.assertEqual((method, url), ('POST', 'https://openrouter.ai/api/v1/chat/completions'))
        kwargs = request.call_args.kwargs
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer sk-or-v1-test-123456')
        body = kwargs['json']
        self.assertEqual(body['model'], 'anthropic/claude-haiku-5.5')
        self.assertEqual(body['provider'], {'require_parameters': True})
        self.assertTrue(body['response_format']['json_schema']['strict'])
        with open(self.app.config['AI_CONFIG_FILE'], encoding='utf-8') as handle:
            self.assertNotIn('sk-or-v1', handle.read())

    def test_openrouter_rejects_bad_model_ids_and_explains_errors(self):
        response = self.client.post('/api/ai/config', json={'provider': 'openrouter', 'model': 'pas un modèle'})
        self.assertEqual(response.status_code, 400)
        self.client.post('/api/ai/config', json={'provider': 'openrouter', 'api_key': 'sk-or-v1-bad-000000'})
        for status, expected in ((401, 'Clé API refusée par OpenRouter'), (402, 'Crédit OpenRouter insuffisant')):
            bad = mock.Mock(status_code=status, ok=False, text='')
            bad.json.return_value = {'error': {'message': 'x', 'code': status}}
            with mock.patch('blueprints.ai.service.requests.request', return_value=bad) as request:
                response = self.client.post('/api/ai/test', json={'provider': 'openrouter'})
            self.assertEqual(response.status_code, 400)
            self.assertIn(expected, response.get_json()['error'])
            self.assertEqual(request.call_count, 1)

    def test_openrouter_free_model_without_structured_output_gets_schema_in_prompt(self):
        from blueprints.ai import service
        service._openrouter_catalog_cache.update(at=0.0, models=None)
        self.client.post('/api/ai/config', json={
            'enabled': True, 'provider': 'openrouter', 'api_key': 'sk-or-v1-test-123456',
            'model': 'google/gemma-4-31b-it:free'})
        listing = mock.Mock(status_code=200, ok=True)
        listing.json.return_value = {'data': [
            {'id': 'google/gemma-4-31b-it:free', 'name': 'Google: Gemma 4 31B (free)',
             'architecture': {'output_modalities': ['text']}, 'supported_parameters': ['response_format'],
             'pricing': {'prompt': '0', 'completion': '0'}}]}
        answer = self._openai_response('Voici : {"groups": []} Bonne journée')
        with mock.patch('blueprints.ai.service.requests.request', side_effect=[listing, answer]) as request:
            self.client.post('/api/ai/tags/normalize')
            job = self.wait_job()
        self.assertEqual(job['status'], 'done', job)
        body = request.call_args.kwargs['json']
        self.assertEqual(body['response_format'], {'type': 'json_object'})
        self.assertIn('schéma JSON', body['messages'][0]['content'])
        service._openrouter_catalog_cache.update(at=0.0, models=None)

    def test_openrouter_model_list_keeps_structured_text_models(self):
        self.client.post('/api/ai/config', json={'provider': 'openrouter', 'api_key': 'sk-or-v1-test-123456'})
        listing = mock.Mock(status_code=200, ok=True)
        text = {'output_modalities': ['text']}
        listing.json.return_value = {'data': [
            {'id': 'openai/gpt-5-mini', 'name': 'OpenAI: GPT-5 Mini', 'architecture': text,
             'supported_parameters': ['response_format', 'structured_outputs']},
            {'id': 'openai/gpt-5-mini:batch', 'name': 'batch', 'architecture': text,
             'supported_parameters': ['structured_outputs']},
            {'id': 'anthropic/claude-haiku-5.5', 'name': 'Anthropic: Claude Haiku 5.5', 'architecture': text,
             'supported_parameters': ['structured_outputs']},
            {'id': 'meta/old-model', 'name': 'Old', 'architecture': text, 'supported_parameters': ['tools']},
            {'id': 'google/gemma-4-31b-it:free', 'name': 'Google: Gemma 4 31B', 'architecture': text,
             'supported_parameters': ['response_format'], 'pricing': {'prompt': '0', 'completion': '0'}},
            {'id': 'nvidia/nemotron-3.5-content-safety:free', 'name': 'Safety', 'architecture': text,
             'supported_parameters': [], 'pricing': {'prompt': '0', 'completion': '0'}},
            {'id': 'google/image-model', 'name': 'Image', 'architecture': {'output_modalities': ['image', 'text']},
             'supported_parameters': ['structured_outputs']},
        ]}
        with mock.patch('blueprints.ai.service.requests.request', return_value=listing):
            data = self.client.post('/api/ai/models', json={'provider': 'openrouter'}).get_json()
        self.assertTrue(data['success'], data)
        self.assertEqual([m['id'] for m in data['models']],
                         ['google/gemma-4-31b-it:free', 'anthropic/claude-haiku-5.5', 'openai/gpt-5-mini'])
        self.assertTrue(data['models'][0]['name'].endswith('(gratuit)'))


if __name__ == '__main__':
    unittest.main()
