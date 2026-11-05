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

    def enable(self):
        response = self.client.post('/api/ai/config', json={
            'enabled': True, 'api_key': 'sk-ant-test-123456789', 'model': 'claude-haiku-5-5'})
        self.assertEqual(response.status_code, 200)

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
        public = self.client.get('/api/ai/config').get_json()
        self.assertEqual(public['api_key'], '****')
        self.assertEqual(public['model'], 'claude-haiku-5-5')
        self.assertTrue(public['has_api_key'])
        # Renvoyer le masque garde la clé enregistrée
        self.client.post('/api/ai/config', json={'api_key': '****', 'enabled': True})
        self.assertTrue(self.client.get('/api/ai/config').get_json()['has_api_key'])

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


if __name__ == '__main__':
    unittest.main()
