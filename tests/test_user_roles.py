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

    def _fake_add(self, seen, series_id=99):
        def fake_add():
            from flask import jsonify, request
            seen.update(request.get_json())
            return jsonify({'success': True, 'series_id': series_id, 'title': 'X'})
        return fake_add

    def test_reader_request_waits_for_validation_in_his_library(self):
        self.login('bob')
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE users SET allowed_libraries_json = '[2]' WHERE user_key = 'oidc:bob'")
        conn.commit()
        conn.close()
        seen = {}
        with mock.patch('blueprints.bedetheque.routes.add_series_from_bedetheque', side_effect=self._fake_add(seen)), \
                mock.patch('notifications.send_notification') as notify:
            response = self.send('POST', '/api/requests', json={'url': 'https://www.bedetheque.com/serie-1-BD-X.html', 'title': 'X'})
            again = self.send('POST', '/api/requests', json={'url': 'https://www.bedetheque.com/serie-1-BD-X.html', 'title': 'X'})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()['status'], 'pending')
        self.assertTrue(again.get_json()['duplicate'])
        self.assertEqual(seen, {})  # rien ajouté avant la validation
        self.assertEqual(notify.call_count, 1)
        self.assertEqual(notify.call_args.kwargs['kind'], 'book_request')
        self.assertEqual(self.db("SELECT status, library_id FROM book_requests"), [('pending', 2)])

        self.login('alice', ['bullarr-admins'])
        self.assertEqual(self.client.get('/api/requests/pending-count').get_json()['books'], 1)
        request_id = self.client.get('/api/requests?status=pending').get_json()['requests'][0]['id']
        with mock.patch('blueprints.bedetheque.routes.add_series_from_bedetheque', side_effect=self._fake_add(seen)):
            approved = self.send('POST', f'/api/requests/{request_id}/approve', json={})
        self.assertEqual(approved.status_code, 200, approved.get_json())
        self.assertEqual(seen['library_id'], 2)
        self.assertTrue(seen['force_auto_acquire'])
        self.assertEqual(self.db("SELECT status, series_id, decided_by FROM book_requests"), [('added', 99, 'Alice')])
        self.assertEqual(self.send('POST', f'/api/requests/{request_id}/approve', json={}).status_code, 409)
        actions = [e['action'] for e in self.client.get('/api/auth/activity').get_json()['entries']]
        self.assertIn('request_created', actions)
        self.assertIn('request_approved', actions)

    def test_failed_approval_keeps_the_request_pending(self):
        self.login('bob')
        with mock.patch('notifications.send_notification'):
            self.send('POST', '/api/requests', json={'url': 'https://www.bedetheque.com/serie-1-BD-X.html', 'title': 'X'})
        self.login('alice', ['bullarr-admins'])
        request_id = self.db('SELECT id FROM book_requests')[0][0]

        def failing_add():
            from flask import jsonify
            return jsonify({'success': False, 'error': 'Bédéthèque injoignable'}), 502

        with mock.patch('blueprints.bedetheque.routes.add_series_from_bedetheque', side_effect=failing_add):
            response = self.send('POST', f'/api/requests/{request_id}/approve', json={})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.db('SELECT status FROM book_requests'), [('pending',)])

    def test_admin_rejects_with_a_reason_and_reader_sees_it(self):
        self.login('bob')
        with mock.patch('notifications.send_notification'):
            self.send('POST', '/api/requests', json={'url': 'https://www.bedetheque.com/serie-1-BD-X.html', 'title': 'X'})
        request_id = self.db('SELECT id FROM book_requests')[0][0]
        self.assertEqual(self.send('POST', f'/api/requests/{request_id}/reject', json={}).status_code, 403)
        self.login('alice', ['bullarr-admins'])
        self.assertEqual(self.send('POST', f'/api/requests/{request_id}/reject', json={'reason': 'Pas pour les enfants'}).status_code, 200)
        self.login('bob')
        row = self.client.get('/api/requests').get_json()['requests'][0]
        self.assertEqual((row['status'], row['error']), ('rejected', 'Pas pour les enfants'))
        lookup = self.send('POST', '/api/requests/lookup', json={'urls': ['https://www.bedetheque.com/serie-1-BD-X.html',
                                                                         'https://www.bedetheque.com/serie-59-BD-Asterix.html']}).get_json()
        self.assertEqual(lookup['items']['https://www.bedetheque.com/serie-1-BD-X.html']['state'], 'rejected')
        self.assertEqual(lookup['items']['https://www.bedetheque.com/serie-59-BD-Asterix.html']['state'], 'library')

    def test_reader_cancels_only_his_own_pending_request(self):
        self.login('bob')
        with mock.patch('notifications.send_notification'):
            self.send('POST', '/api/requests', json={'url': 'https://www.bedetheque.com/serie-1-BD-X.html', 'title': 'X'})
        request_id = self.db('SELECT id FROM book_requests')[0][0]
        self.login('carol')
        self.assertEqual(self.send('DELETE', f'/api/requests/{request_id}').status_code, 404)
        self.login('bob')
        self.assertEqual(self.send('DELETE', f'/api/requests/{request_id}').status_code, 200)
        self.assertEqual(self.db('SELECT status FROM book_requests'), [('cancelled',)])

    def test_auto_approve_setting_adds_reader_requests_at_once(self):
        self.login('alice', ['bullarr-admins'])
        self.assertEqual(self.send('PUT', '/api/requests/settings', json={'auto_approve': True}).status_code, 200)
        self.assertTrue(self.client.get('/api/auth/users').get_json()['auto_approve_requests'])
        self.login('bob')
        self.assertEqual(self.send('PUT', '/api/requests/settings', json={'auto_approve': False}).status_code, 403)
        seen = {}
        with mock.patch('blueprints.bedetheque.routes.add_series_from_bedetheque', side_effect=self._fake_add(seen)), \
                mock.patch('notifications.send_notification') as notify:
            response = self.send('POST', '/api/requests', json={'url': 'https://www.bedetheque.com/serie-1-BD-X.html', 'title': 'X'})
        self.assertEqual(response.get_json()['status'], 'added')
        self.assertEqual(seen['library_id'], 1)
        notify.assert_not_called()

    def test_admins_have_no_requests_page_and_readers_no_activity_log(self):
        self.login('alice', ['bullarr-admins'])
        response = self.client.get('/demandes')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/validation', response.headers['Location'])
        self.login('bob')
        self.assertEqual(self.client.get('/demandes').status_code, 200)
        self.assertEqual(self.client.get('/api/auth/activity').status_code, 403)
        self.assertEqual(self.client.get('/api/requests/pending-count').status_code, 403)

    # ----- Profil -----
    def test_profile_avatar_for_sso_reader_and_no_identity_edit(self):
        import io
        from PIL import Image
        self.login('bob')
        self.assertEqual(self.client.get('/profil').status_code, 200)
        profile = self.client.get('/api/auth/profile').get_json()['profile']
        self.assertEqual((profile['type'], profile['role'], profile['can_edit_identity']), ('oidc', 'reader', False))
        self.assertEqual(self.send('PUT', '/api/auth/profile', json={'name': 'Hack'}).status_code, 400)
        buffer = io.BytesIO()
        Image.new('RGB', (600, 300), (200, 30, 30)).save(buffer, 'PNG')
        buffer.seek(0)
        avatars = tempfile.TemporaryDirectory()
        self.app.config['DATA_DIR'] = avatars.name
        try:
            upload = self.client.post('/api/auth/profile/avatar', headers={'X-CSRF-Token': 'tok'},
                                      data={'avatar': (buffer, 'me.png')}, content_type='multipart/form-data')
            self.assertEqual(upload.status_code, 200, upload.get_json())
            url = upload.get_json()['avatar_url']
            served = self.client.get(url)
            self.assertEqual(served.status_code, 200)
            self.assertEqual(Image.open(io.BytesIO(served.data)).size, (256, 256))
            self.assertEqual(self.client.get('/api/auth/me').get_json()['profile']['avatar_url'], url)
            bad = self.client.post('/api/auth/profile/avatar', headers={'X-CSRF-Token': 'tok'},
                                   data={'avatar': (io.BytesIO(b'not an image'), 'x.png')}, content_type='multipart/form-data')
            self.assertEqual(bad.status_code, 400)
            self.assertEqual(self.send('DELETE', '/api/auth/profile/avatar').status_code, 200)
            self.assertEqual(os.listdir(os.path.join(avatars.name, 'avatars')), [])
            self.assertEqual(self.client.get('/avatars/../oidc_config.json').status_code, 404)
        finally:
            avatars.cleanup()

    def test_local_reader_edits_profile_and_password_with_current_password(self):
        with self.app.app_context():
            from blueprints.auth import accounts
            accounts.create_local_account('lea', 'lecture-123', name='Léa', role='reader')
        user = {'name': 'lea', 'auth_method': 'password'}
        with self.client.session_transaction() as sess:
            sess['user'] = user
            sess['csrf_token'] = 'tok'
        profile = self.client.get('/api/auth/profile').get_json()['profile']
        self.assertEqual((profile['username'], profile['role'], profile['can_edit_identity']), ('lea', 'reader', True))
        self.assertEqual(self.send('PUT', '/api/auth/profile', json={'name': 'Léa M.', 'email': 'lea@example.test'}).status_code, 200)
        wrong = self.send('PUT', '/api/auth/profile', json={'current_password': 'nope', 'new_password': 'nouveau-456',
                                                             'new_password_confirm': 'nouveau-456'})
        self.assertEqual(wrong.status_code, 403)
        mismatch = self.send('PUT', '/api/auth/profile', json={'current_password': 'lecture-123', 'new_password': 'nouveau-456',
                                                                'new_password_confirm': 'autre'})
        self.assertEqual(mismatch.status_code, 400)
        changed = self.send('PUT', '/api/auth/profile', json={'current_password': 'lecture-123', 'new_password': 'nouveau-456',
                                                               'new_password_confirm': 'nouveau-456'})
        self.assertEqual(changed.status_code, 200, changed.get_json())
        self.assertEqual(self.client.get('/api/auth/profile').status_code, 200)  # session gardée
        with self.app.app_context():
            from blueprints.auth import accounts
            self.assertIsNotNone(accounts.authenticate('lea', 'nouveau-456'))
            row = accounts.get_user('password:lea')
        self.assertEqual((row['name'], row['email']), ('Léa M.', 'lea@example.test'))
        actions = [r[0] for r in self.db('SELECT action FROM user_activity ORDER BY id')]
        self.assertEqual(actions.count('profile_edited'), 2)
        self.assertIn('password_change_failed', actions)

    def test_rejects_non_bedetheque_url(self):
        self.login('bob')
        self.assertEqual(self.send('POST', '/api/requests', json={'url': 'https://evil.example/x'}).status_code, 400)


if __name__ == '__main__':
    unittest.main()
