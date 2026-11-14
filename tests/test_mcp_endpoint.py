import json
import os
import unittest
from unittest import mock


class McpEndpointTest(unittest.TestCase):
    """Endpoint /mcp : jeton dédié, JSON-RPC, outils en lecture et écriture de tags."""

    @classmethod
    def setUpClass(cls):
        os.makedirs('data', exist_ok=True)
        from app import create_app
        cls.app = create_app()
        cls.app.config['TESTING'] = True

    def setUp(self):
        self.client = self.app.test_client()
        path = os.path.join(os.path.dirname(self.app.config['AI_CONFIG_FILE']), 'mcp_config.json')
        if os.path.exists(path):
            os.remove(path)
        self.token = 'jeton-de-test-' + 'x' * 20

    def _post(self, body, token=None, env=True):
        headers = {'Content-Type': 'application/json'}
        if token is not None:
            headers['Authorization'] = 'Bearer ' + token
        environ = {'BULLARR_MCP_TOKEN': self.token} if env else {'BULLARR_MCP_TOKEN': ''}
        with mock.patch.dict(os.environ, environ):
            return self.client.post('/mcp', data=json.dumps(body), headers=headers)

    def test_503_without_configured_token(self):
        response = self._post({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}, token=self.token, env=False)
        self.assertEqual(response.status_code, 503)

    def test_401_without_or_with_wrong_token(self):
        body = {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}
        self.assertEqual(self._post(body).status_code, 401)
        self.assertEqual(self._post(body, token='mauvais').status_code, 401)

    def test_get_is_refused(self):
        with mock.patch.dict(os.environ, {'BULLARR_MCP_TOKEN': self.token}):
            response = self.client.get('/mcp', headers={'Authorization': 'Bearer ' + self.token})
        self.assertEqual(response.status_code, 405)

    def test_initialize_and_tools_list(self):
        response = self._post({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}}, token=self.token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['result']['serverInfo']['name'], 'bullarr')

        response = self._post({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}, token=self.token)
        names = {tool['name'] for tool in response.get_json()['result']['tools']}
        self.assertTrue({'search_series', 'get_series', 'list_tags', 'set_series_tags', 'set_reading_lists'} <= names)

    def test_notification_is_accepted_without_body(self):
        response = self._post({'jsonrpc': '2.0', 'method': 'notifications/initialized'}, token=self.token)
        self.assertEqual(response.status_code, 202)

    def test_unknown_tool_is_a_jsonrpc_error(self):
        response = self._post({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                               'params': {'name': 'supprimer_tout', 'arguments': {}}}, token=self.token)
        self.assertEqual(response.get_json()['error']['code'], -32602)

    def test_invalid_json_is_400(self):
        with mock.patch.dict(os.environ, {'BULLARR_MCP_TOKEN': self.token}):
            response = self.client.post('/mcp', data='pas du json',
                                        headers={'Content-Type': 'application/json',
                                                 'Authorization': 'Bearer ' + self.token})
        self.assertEqual(response.status_code, 400)

    def test_token_in_url_for_connectors(self):
        body = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}}
        with mock.patch.dict(os.environ, {'BULLARR_MCP_TOKEN': self.token}):
            ok = self.client.post('/mcp/' + self.token, data=json.dumps(body),
                                  headers={'Content-Type': 'application/json'})
            bad = self.client.post('/mcp/mauvais', data=json.dumps(body),
                                   headers={'Content-Type': 'application/json'})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.get_json()['result']['protocolVersion'], '2025-06-18')
        self.assertEqual(bad.status_code, 401)
        self.assertIn('Bearer', bad.headers.get('WWW-Authenticate', ''))

    def test_unknown_protocol_version_falls_back(self):
        response = self._post({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                               'params': {'protocolVersion': '1999-01-01'}}, token=self.token)
        self.assertEqual(response.get_json()['result']['protocolVersion'], '2024-11-05')

    def test_info_for_the_settings_tab(self):
        # Sans authentification configurée (tests), l'appli entière est ouverte, comme le reste
        # de la Configuration ; avec une connexion, la route n'est pas publique.
        with mock.patch.dict(os.environ, {'BULLARR_MCP_TOKEN': self.token}):
            data = self.client.get('/api/mcp/info').get_json()
        self.assertTrue(data['enabled'])
        self.assertEqual(data['token'], self.token)
        with mock.patch.dict(os.environ, {'BULLARR_MCP_TOKEN': ''}):
            data = self.client.get('/api/mcp/info').get_json()
        self.assertFalse(data['enabled'])
        self.assertIsNone(data['token'])
        from blueprints.auth.routes import _PUBLIC_ENDPOINTS
        self.assertNotIn('mcp.mcp_info', _PUBLIC_ENDPOINTS)

    def test_switch_off_refuses_even_a_good_token(self):
        with mock.patch.dict(os.environ, {'BULLARR_MCP_TOKEN': self.token}):
            data = self.client.post('/api/mcp/config', json={'enabled': False}).get_json()
        self.assertFalse(data['enabled'])
        self.assertFalse(data['switch'])
        body = {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}
        self.assertEqual(self._post(body, token=self.token).status_code, 503)
        with mock.patch.dict(os.environ, {'BULLARR_MCP_TOKEN': self.token}):
            self.client.post('/api/mcp/config', json={'enabled': True})
        self.assertEqual(self._post(body, token=self.token).status_code, 200)

    def test_enabling_without_env_token_creates_one(self):
        with mock.patch.dict(os.environ, {'BULLARR_MCP_TOKEN': ''}):
            data = self.client.post('/api/mcp/config', json={'enabled': True}).get_json()
            self.assertTrue(data['enabled'])
            self.assertEqual(data['token_source'], 'settings')
            token = data['token']
            ok = self.client.post('/mcp/' + token, json={'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
            self.assertEqual(ok.status_code, 200)
            data = self.client.post('/api/mcp/config', json={'regenerate': True}).get_json()
            self.assertNotEqual(data['token'], token)
            old = self.client.post('/mcp/' + token, json={'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
            self.assertEqual(old.status_code, 401)

    def _call_tool(self, name, arguments=None):
        response = self._post({'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call',
                               'params': {'name': name, 'arguments': arguments or {}}}, token=self.token)
        result = response.get_json()['result']
        return result['isError'], (json.loads(result['content'][0]['text']) if not result['isError']
                                   else result['content'][0]['text'])

    def _fixture(self):
        import sqlite3
        conn = sqlite3.connect(self.app.config['DATABASE'])
        lib = conn.execute('SELECT id FROM libraries LIMIT 1').fetchone()
        lib_id = lib[0] if lib else conn.execute(
            "INSERT INTO libraries (name, path) VALUES ('Test MCP', '/tmp/mcp-lib')").lastrowid
        sid = conn.execute("INSERT INTO series (library_id, title, path, local_author, local_genre) "
                           "VALUES (?, 'Série MCP', '/tmp/mcp-lib/x', 'Jean Testeur', 'Aventure')", (lib_id,)).lastrowid
        owned = conn.execute("INSERT INTO volumes (series_id, volume_number, filename, filepath, format) "
                             "VALUES (?, 1, 'x1.cbz', '/tmp/mcp-lib/x/x1.cbz', 'cbz')", (sid,)).lastrowid
        missing = conn.execute("INSERT INTO volumes (series_id, volume_number) VALUES (?, 2)", (sid,)).lastrowid
        conn.commit()
        conn.close()
        return lib_id, sid, owned, missing

    def test_new_tools_are_listed(self):
        response = self._post({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}, token=self.token)
        names = {tool['name'] for tool in response.get_json()['result']['tools']}
        for name in ('get_series_volumes', 'list_recent_additions', 'get_reading_history', 'list_libraries',
                     'list_universes', 'list_requests', 'list_downloads', 'set_read_status',
                     'set_series_monitor', 'search_missing_volumes', 'update_series_metadata'):
            self.assertIn(name, names)
        self.assertFalse({n for n in names if 'delete' in n or 'move' in n or 'rename' in n})

    def test_read_tools(self):
        lib_id, sid, owned, missing = self._fixture()
        err, data = self._call_tool('get_series_volumes', {'series_id': sid})
        self.assertFalse(err, data)
        self.assertEqual((data['owned'], data['missing']), (1, 1))
        self.assertEqual(data['volumes'][0]['label'], 'Tome 1')
        err, data = self._call_tool('search_series', {'author': 'testeur', 'incomplete': True})
        self.assertFalse(err, data)
        self.assertIn(sid, [r['id'] for r in data['results']])
        err, data = self._call_tool('search_series', {'author': 'testeur', 'incomplete': False})
        self.assertNotIn(sid, [r['id'] for r in data['results']])
        for name in ('list_recent_additions', 'get_reading_history', 'list_libraries', 'list_universes', 'list_requests', 'list_downloads'):
            err, data = self._call_tool(name)
            self.assertFalse(err, (name, data))
        err, data = self._call_tool('list_libraries')
        self.assertIn(lib_id, [l['library_id'] for l in data['libraries']])

    def test_write_tools(self):
        _lib, sid, owned, missing = self._fixture()
        err, data = self._call_tool('update_series_metadata', {'series_id': sid, 'genre': 'Humour'})
        self.assertFalse(err, data)
        err, data = self._call_tool('get_series', {'series_id': sid})
        self.assertEqual(data['manual_genre'], 'Humour')
        err, data = self._call_tool('set_series_monitor', {'series_id': sid, 'enabled': True})
        self.assertFalse(err, data)
        err, data = self._call_tool('search_missing_volumes', {'series_id': sid, 'volume_id': owned})
        self.assertTrue(err)
        self.assertIn('déjà', data)
        err, data = self._call_tool('update_series_metadata', {'series_id': sid})
        self.assertTrue(err)


if __name__ == '__main__':
    unittest.main()
