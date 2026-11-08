"""Notifications ntfy et distribution sur les canaux (notifications.py)."""
import json
import os
import tempfile
import unittest
from unittest import mock

from flask import Flask

from config import Config
from encryption import ensure_encryption_key


def _response(status=200, payload=None):
    response = mock.Mock(status_code=status, ok=200 <= status < 300)
    response.json.return_value = payload or {}
    response.text = json.dumps(payload or {})
    return response


class NtfyTestCase(unittest.TestCase):
    def setUp(self):
        ensure_encryption_key()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Flask(__name__)
        self.app.config['NTFY_CONFIG'] = dict(Config.NTFY_CONFIG)
        self.app.config['NTFY_CONFIG_FILE'] = os.path.join(self.tmp.name, 'ntfy_config.json')
        self.app.config['TELEGRAM_CONFIG'] = dict(Config.TELEGRAM_CONFIG)
        self.app.config['TELEGRAM_CONFIG_FILE'] = os.path.join(self.tmp.name, 'telegram_config.json')
        from blueprints.ntfy import ntfy_bp
        self.app.register_blueprint(ntfy_bp, url_prefix='/api/ntfy')
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)

    def save(self, **values):
        from blueprints.ntfy.routes import load_ntfy_config, save_ntfy_config
        cfg = load_ntfy_config()
        cfg.update({'enabled': True, 'topic': 'bullarr-test'}, **values)
        self.assertTrue(save_ntfy_config(cfg))


class SendNtfyTest(NtfyTestCase):
    def test_publishes_json_to_the_server_root(self):
        from blueprints.ntfy.routes import send_ntfy_notification
        self.save(server_url='https://ntfy.example.org/', auth='token', token_decrypted='tk_secret', priority=4)
        with mock.patch('blueprints.ntfy.routes.requests.post', return_value=_response()) as post:
            ok, error = send_ntfy_notification('\nAstérix - T01\n', title='BD Importé :', tags=['books'],
                                               click='https://www.bedetheque.com/serie-59-BD-Asterix.html')
        self.assertEqual((ok, error), (True, None))
        url = post.call_args.args[0]
        payload = post.call_args.kwargs['json']
        self.assertEqual(url, 'https://ntfy.example.org')
        self.assertEqual(payload['topic'], 'bullarr-test')
        self.assertEqual(payload['message'], 'Astérix - T01')
        self.assertEqual(payload['title'], 'BD Importé')
        self.assertEqual(payload['tags'], ['books'])
        self.assertEqual(payload['priority'], 4)
        self.assertTrue(payload['click'].startswith('https://www.bedetheque.com/'))
        self.assertEqual(post.call_args.kwargs['headers'], {'Authorization': 'Bearer tk_secret'})

    def test_cover_is_attached_and_falls_back_without_it(self):
        from blueprints.ntfy.routes import send_ntfy_notification
        self.save(auth='basic', username='lecteur', password_decrypted='pw')
        cover = os.path.join(self.tmp.name, 'cover.jpg')
        with open(cover, 'wb') as handle:
            handle.write(b'\xff\xd8\xff')
        with mock.patch('blueprints.ntfy.routes.requests.put', return_value=_response(200)) as put, \
                mock.patch('blueprints.ntfy.routes.requests.post') as post:
            self.assertEqual(send_ntfy_notification('Import', title='BD', photo_path=cover), (True, None))
        self.assertEqual(put.call_args.args[0], 'https://ntfy.sh/bullarr-test')
        self.assertEqual(put.call_args.kwargs['params']['filename'], 'cover.jpg')
        self.assertTrue(put.call_args.kwargs['headers']['Authorization'].startswith('Basic '))
        post.assert_not_called()
        # Serveur qui refuse les pièces jointes : la notification part quand même, sans image.
        with mock.patch('blueprints.ntfy.routes.requests.put', return_value=_response(400, {'error': 'attachments not allowed'})), \
                mock.patch('blueprints.ntfy.routes.requests.post', return_value=_response()) as post:
            self.assertEqual(send_ntfy_notification('Import', photo_path=cover), (True, None))
        post.assert_called_once()

    def test_disabled_or_invalid_topic_sends_nothing(self):
        from blueprints.ntfy.routes import send_ntfy_notification
        with mock.patch('blueprints.ntfy.routes.requests.post') as post:
            ok, error = send_ntfy_notification('x')
        self.assertFalse(ok)
        self.assertIn('désactivées', error)
        self.save(topic='pas un topic/valide')
        with mock.patch('blueprints.ntfy.routes.requests.post') as post:
            ok, error = send_ntfy_notification('x')
        self.assertFalse(ok)
        self.assertIn('Topic', error)
        post.assert_not_called()


class ConfigRouteTest(NtfyTestCase):
    def test_secrets_are_masked_encrypted_and_kept(self):
        client = self.app.test_client()
        with mock.patch('blueprints.library.scheduler.sync_auto_import_schedule'):
            response = client.post('/api/ntfy/config', json={
                'enabled': True, 'server_url': 'https://ntfy.sh', 'topic': 'bullarr-abc',
                'auth': 'token', 'token': 'tk_secret', 'notify_monitor': False})
            self.assertEqual(response.status_code, 200)
            with open(self.app.config['NTFY_CONFIG_FILE'], encoding='utf-8') as handle:
                self.assertNotIn('tk_secret', handle.read())
            public = client.get('/api/ntfy/config').get_json()
            self.assertEqual(public['token'], '****')
            # Renvoyé masqué : le jeton enregistré reste ; les cases par type, absentes du
            # formulaire (tableau « Événements »), ne sont pas remises à leur valeur par défaut.
            client.post('/api/ntfy/config', json={**public, 'topic': 'bullarr-def'})
        from blueprints.ntfy.routes import load_ntfy_config
        cfg = load_ntfy_config()
        self.assertEqual((cfg['topic'], cfg['token_decrypted']), ('bullarr-def', 'tk_secret'))
        self.assertFalse(cfg['notify_monitor'])

    def test_invalid_topic_is_refused(self):
        response = self.app.test_client().post('/api/ntfy/config', json={'enabled': True, 'topic': 'a b'})
        self.assertEqual(response.status_code, 400)


class DispatchTest(NtfyTestCase):
    def enable_telegram(self, **values):
        with open(self.app.config['TELEGRAM_CONFIG_FILE'], 'w', encoding='utf-8') as handle:
            json.dump({'enabled': True, 'chat_id': '1', 'notify_import_completed': True,
                       'notify_import_available': True, **values}, handle)

    def test_each_channel_follows_its_own_boxes(self):
        from notifications import notification_wanted, send_notification
        self.assertFalse(notification_wanted('import_completed'))
        self.save(notify_monitor=False)
        self.enable_telegram(notify_import_completed=False)
        with mock.patch('blueprints.ntfy.routes.send_ntfy_notification', return_value=(True, None)) as ntfy, \
                mock.patch('blueprints.telegram.routes.send_telegram_notification', return_value=(True, None)) as telegram:
            self.assertEqual(send_notification('m', kind='import_completed', title='T'), (True, []))
            ntfy.assert_called_once()
            telegram.assert_not_called()
            ntfy.reset_mock()
            send_notification('Série : X', kind='monitor', icon='📚', title='Nouvel album surveillé',
                              click='https://t.me/c/1')
            ntfy.assert_not_called()
            self.assertEqual(telegram.call_args.args[0], '📚 Nouvel album surveillé\nSérie : X\nhttps://t.me/c/1')

    def test_failures_are_reported_per_channel(self):
        from notifications import send_notification
        self.save()
        with mock.patch('blueprints.ntfy.routes.send_ntfy_notification', return_value=(False, 'Timeout')):
            self.assertEqual(send_notification('m', kind='import_available'), (False, ['ntfy : Timeout']))


class BackupIncludesNotificationConfigsTest(unittest.TestCase):
    def test_backup_names(self):
        from backup_utils import BACKUP_NAMES
        self.assertTrue({'ntfy_config.json', 'telegram_config.json'} <= BACKUP_NAMES)


if __name__ == '__main__':
    unittest.main()
