import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from config import Config


class UserRolesTest(unittest.TestCase):
    """Comptes administrateur / lecteur, accès par bibliothèque, contrôle parental et demandes."""

    def setUp(self):
        os.makedirs('data', exist_ok=True)
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        fd, self.oidc_path = tempfile.mkstemp(suffix='.json')
        os.close(fd)
        Config._init_database(self.db_path)
        from blueprints.library.scanner import LibraryScanner
        LibraryScanner(self.db_path)  # colonnes Komga/EBDZ ajoutées au démarrage
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO libraries (id, name, path, is_default) VALUES (1, 'BD', '/BD', 1)")
        conn.execute("INSERT INTO libraries (id, name, path) VALUES (2, 'Comics', '/Comics')")
        conn.execute("INSERT INTO series (id, library_id, title, path, bedetheque_genre, bedetheque_url) "
                     "VALUES (10, 1, 'Astérix', '/BD/Asterix', 'Humour, Jeunesse', 'https://www.bedetheque.com/serie-59-BD-Asterix.html')")
        conn.execute("INSERT INTO series (id, library_id, title, path, bedetheque_genre) VALUES (11, 1, 'Druuna', '/BD/Druuna', 'Érotique, Science-fiction')")
        conn.execute("INSERT INTO series (id, library_id, title, path, bedetheque_genre) VALUES (12, 2, 'Batman', '/Comics/Batman', 'Super-héros')")
        conn.execute("INSERT INTO volumes (id, series_id, volume_number, filename, filepath) VALUES (100, 11, 1, 'a.cbz', '/BD/Druuna/a.cbz')")
        conn.commit()
        conn.close()
        from app import create_app
        self.app = create_app()
        self.app.config.update(DATABASE=self.db_path, TESTING=True, OIDC_CONFIG_FILE=self.oidc_path)
        self.write_oidc({'mode': 'oidc', 'issuer': 'https://id.example', 'client_id': 'x', 'admin_group': 'bullarr-admins'})
        self.client = self.app.test_client()

    def tearDown(self):
        os.remove(self.db_path)
        os.remove(self.oidc_path)

    def write_oidc(self, cfg):
        with open(self.oidc_path, 'w') as handle:
            json.dump(cfg, handle)

    def login(self, sub, groups=()):
        user = {'sub': sub, 'name': sub.title(), 'groups': list(groups), 'auth_method': 'oidc'}
        with self.app.test_request_context():
            from blueprints.auth.roles import record_login
            record_login(user)
        with self.client.session_transaction() as sess:
            sess['user'] = user
            sess['csrf_token'] = 'tok'
        return user

    def send(self, method, url, **kwargs):
        return self.client.open(url, method=method, headers={'X-CSRF-Token': 'tok'}, **kwargs)

    def db(self, sql, args=()):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    # ----- Rôles -----
    def test_group_member_is_admin_and_others_are_readers(self):
        self.login('alice', ['bullarr-admins'])
        self.assertEqual(self.client.get('/api/auth/me').get_json()['role'], 'admin')
        self.assertEqual(self.client.get('/api/import/state').status_code, 200)
        self.login('bob')
        self.assertEqual(self.client.get('/api/auth/me').get_json()['role'], 'reader')

    def test_without_admin_group_everyone_is_admin(self):
        self.write_oidc({'mode': 'oidc', 'issuer': 'https://id.example', 'client_id': 'x'})
        self.login('bob')
        self.assertEqual(self.client.get('/api/auth/me').get_json()['role'], 'admin')

    def test_reader_is_limited_to_the_allowlist(self):
        self.login('bob')
        self.assertEqual(self.client.get('/api/libraries').status_code, 200)
        self.assertEqual(self.client.get('/api/series/10').status_code, 200)
        self.assertEqual(self.send('DELETE', '/api/series/10').status_code, 403)
        self.assertEqual(self.client.get('/api/import/state').status_code, 403)
        self.assertEqual(self.client.get('/settings').status_code, 302)
        self.assertEqual(self.client.get('/api/volumes/100/download').status_code != 403, True)

    def test_override_wins_over_group_and_admin_cannot_demote_himself(self):
        self.login('alice', ['bullarr-admins'])
        self.assertEqual(self.send('PUT', '/api/auth/users/role', json={'key': 'oidc:alice', 'role': 'reader'}).status_code, 400)
        self.login('bob')
        self.login('alice', ['bullarr-admins'])
        self.assertEqual(self.send('PUT', '/api/auth/users/role', json={'key': 'oidc:bob', 'role': 'admin'}).status_code, 200)
        self.login('bob')
        self.assertEqual(self.client.get('/api/auth/me').get_json()['role'], 'admin')

    def test_saving_admin_group_refuses_to_lock_out_the_saver(self):
        self.write_oidc({'mode': 'oidc', 'issuer': 'https://id.example', 'client_id': 'x'})
        self.login('alice', ['autres'])
        response = self.send('POST', '/api/auth/roles', json={'admin_group': 'bullarr-admins'})
        self.assertEqual(response.status_code, 400)
        self.login('alice', ['bullarr-admins'])
        self.assertEqual(self.send('POST', '/api/auth/roles', json={'admin_group': 'bullarr-admins'}).status_code, 200)

    # ----- Accès et contrôle parental -----
    def test_library_access_and_parental_control(self):
        self.login('bob')
        self.login('alice', ['bullarr-admins'])
        response = self.send('PUT', '/api/auth/users/access',
                             json={'key': 'oidc:bob', 'allowed_libraries': [1], 'blocked_genres': ['Érotique']})
        self.assertEqual(response.status_code, 200, response.get_json())
        # L'administrateur voit toujours tout.
        self.assertEqual(len(self.client.get('/api/libraries').get_json()), 2)
        self.assertEqual(self.client.get('/api/series/11').status_code, 200)

        self.login('bob')
        self.assertEqual([lib['id'] for lib in self.client.get('/api/libraries').get_json()], [1])
        self.assertEqual(self.client.get('/api/library/2/series').status_code, 404)
        self.assertEqual(self.client.get('/api/series/12').status_code, 404)
        self.assertEqual(self.client.get('/api/series/11').status_code, 404)
        self.assertEqual(self.client.get('/api/volumes/100/download').status_code, 404)
        self.assertEqual(self.send('POST', '/api/volumes/download-zip', json={'ids': [100]}).status_code, 404)
        titles = [s['title'] for s in self.client.get('/api/library/1/series').get_json()]
        self.assertIn('Astérix', titles)
        self.assertNotIn('Druuna', titles)

    def test_reader_without_default_library_goes_to_his_first_library(self):
        self.login('bob')
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE users SET allowed_libraries_json = '[2]' WHERE user_key = 'oidc:bob'")
        conn.commit()
        conn.close()
        response = self.client.get('/')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/library/2'))

    # ----- Listes personnelles -----
    def test_reader_lists_are_personal(self):
        self.login('bob')
        response = self.send('PUT', '/api/series/10/reading-lists', json={'to_read_later': True})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(self.client.get('/api/series/10').get_json()['to_read_later'])
        self.assertEqual(self.db('SELECT to_read_later FROM series WHERE id = 10'), [(0,)])
        self.login('carol')
        self.assertFalse(self.client.get('/api/series/10').get_json()['to_read_later'])

    # ----- Demandes -----
    def test_request_existing_series_and_listing(self):
        self.login('bob')
        response = self.send('POST', '/api/requests', json={'url': 'https://www.bedetheque.com/serie-59-BD-Asterix.html', 'title': 'Astérix'})
        self.assertEqual(response.get_json()['status'], 'already')
        self.login('carol')
        self.assertEqual(self.client.get('/api/requests').get_json()['requests'], [])
        self.login('alice', ['bullarr-admins'])
        rows = self.client.get('/api/requests').get_json()['requests']
        self.assertEqual([r['user_name'] for r in rows], ['Bob'])

    def test_request_new_series_goes_to_first_allowed_library_with_forced_search(self):
        self.login('bob')
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE users SET allowed_libraries_json = '[2]' WHERE user_key = 'oidc:bob'")
        conn.commit()
        conn.close()
        seen = {}

        def fake_add():
            from flask import jsonify, request
            seen.update(request.get_json())
            return jsonify({'success': True, 'series_id': 99})

        with mock.patch('blueprints.bedetheque.routes.add_series_from_bedetheque', side_effect=fake_add):
            response = self.send('POST', '/api/requests', json={'url': 'https://www.bedetheque.com/serie-1-BD-X.html', 'title': 'X'})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(seen['library_id'], 2)
        self.assertTrue(seen['force_auto_acquire'])

    def test_rejects_non_bedetheque_url(self):
        self.login('bob')
        self.assertEqual(self.send('POST', '/api/requests', json={'url': 'https://evil.example/x'}).status_code, 400)


if __name__ == '__main__':
    unittest.main()
