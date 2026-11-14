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
        self.assertEqual(names, {'search_series', 'get_series', 'list_tags', 'set_series_tags', 'set_reading_lists'})

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


if __name__ == '__main__':
    unittest.main()
