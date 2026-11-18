import unittest

from blueprints.auth.api_scopes import normalize_scopes, scope_allows


class ApiScopeTests(unittest.TestCase):
    def test_legacy_key_keeps_full_access(self):
        self.assertTrue(scope_allows(None, 'prowlarr.prowlarr_config', 'POST', '/api/prowlarr/config'))

    def test_read_only_key(self):
        scopes = normalize_scopes(['read'])
        self.assertTrue(scope_allows(scopes, 'library.get_series_details', 'GET', '/api/series/<int:series_id>'))
        self.assertFalse(scope_allows(scopes, 'library.manage_series_tags', 'PUT', '/api/series/<int:series_id>/tags'))
        self.assertFalse(scope_allows(scopes, 'prowlarr.prowlarr_config', 'GET', '/api/prowlarr/config'))
        self.assertFalse(scope_allows(scopes, 'auth.list_users', 'GET', '/api/auth/users'))
        self.assertTrue(scope_allows(scopes, 'auth.current_user', 'GET', '/api/auth/me'))

    def test_tags_key_writes_only_tags(self):
        scopes = normalize_scopes(['read', 'tags'])
        self.assertTrue(scope_allows(scopes, 'library.manage_series_tags', 'PUT', '/api/series/<int:series_id>/tags'))
        self.assertFalse(scope_allows(scopes, 'library.post_series_read_status', 'POST', '/api/series/<int:series_id>/read-status'))
        self.assertFalse(scope_allows(scopes, 'library.delete_series', 'DELETE', '/api/series/<int:series_id>'))

    def test_write_scope_without_read(self):
        scopes = normalize_scopes(['tags'])
        self.assertFalse(scope_allows(scopes, 'library.get_series_details', 'GET', '/api/series/<int:series_id>'))
        self.assertTrue(scope_allows(scopes, 'library.manage_series_tags', 'PUT', '/api/series/<int:series_id>/tags'))

    def test_normalize(self):
        self.assertEqual(normalize_scopes(['tags', 'read', 'bogus']), ['read', 'tags'])
        self.assertEqual(normalize_scopes(['read', 'full']), ['full'])
        self.assertEqual(normalize_scopes([]), [])
        self.assertIsNone(normalize_scopes(None))


if __name__ == '__main__':
    unittest.main()
