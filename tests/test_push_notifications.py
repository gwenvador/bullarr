"""Notifications instantanées de l'application Android (UnifiedPush, /api/push/*)."""
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from flask import Flask

from config import Config

PHONE = 'https://ntfy.sh/upAbc123?up=1'


def _response(status):
    return mock.Mock(status_code=status)


class PushTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Flask(__name__)
        self.app.config.update(
            PUSH_ENDPOINTS_FILE=os.path.join(self.tmp.name, 'push_endpoints.json'),
            NTFY_CONFIG=dict(Config.NTFY_CONFIG), NTFY_CONFIG_FILE=os.path.join(self.tmp.name, 'ntfy.json'),
            TELEGRAM_CONFIG=dict(Config.TELEGRAM_CONFIG), TELEGRAM_CONFIG_FILE=os.path.join(self.tmp.name, 'tg.json'),
            DATABASE=os.path.join(self.tmp.name, 'bullarr.db'),
        )
        from blueprints.push import push_bp
        self.app.register_blueprint(push_bp, url_prefix='/api/push')
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)

    def register(self, endpoint=PHONE, **extra):
        return self.client.post('/api/push/register', json={'endpoint': endpoint, 'device': 'Fairphone 5', **extra})


class RegisterTest(PushTestCase):
    def test_register_replace_and_unregister(self):
        from blueprints.push.routes import load_endpoints
        self.assertEqual(self.register().get_json(), {'success': True, 'devices': 1})
        self.register()  # même adresse : pas de doublon
        new = 'https://ntfy.sh/upNew456?up=1'
        self.register(new, previous_endpoint=PHONE)
        self.assertEqual([e['endpoint'] for e in load_endpoints()], [new])
        self.assertEqual(self.client.post('/api/push/unregister', json={'endpoint': new}).get_json(), {'success': True})
        self.assertEqual(load_endpoints(), [])

    def test_invalid_endpoint_is_refused(self):
        self.assertEqual(self.register('file:///etc/passwd').status_code, 400)

    def test_devices_never_expose_the_endpoint(self):
        self.register()
        body = self.client.get('/api/push/devices').get_data(as_text=True)
        self.assertIn('Fairphone 5', body)
        self.assertNotIn('upAbc123', body)


class SendTest(PushTestCase):
    def test_payload_matches_the_android_app(self):
        from blueprints.push.routes import send_push
        self.register()
        with mock.patch('blueprints.push.routes.requests.post', return_value=_response(200)) as post:
            self.assertEqual(send_push('monitor_found', 'Nouveau tome surveillé', 'Série : Astérix'), (1, []))
        self.assertEqual(post.call_args.args[0], PHONE)
        self.assertEqual(json.loads(post.call_args.kwargs['data'].decode('utf-8')),
                         {'kind': 'monitor_found', 'title': 'Nouveau tome surveillé', 'body': 'Série : Astérix'})
        self.assertFalse(post.call_args.kwargs['allow_redirects'])

    def test_unsubscribed_endpoint_is_removed(self):
        from blueprints.push.routes import load_endpoints, send_push
        self.register()
        with mock.patch('blueprints.push.routes.requests.post', return_value=_response(404)):
            self.assertEqual(send_push('test', 't', 'b'), (0, []))
        self.assertEqual(load_endpoints(), [])

    def test_test_route(self):
        self.assertEqual(self.client.post('/api/push/test').status_code, 400)
        self.register()
        with mock.patch('blueprints.push.routes.requests.post', return_value=_response(200)) as post:
            self.assertEqual(self.client.post('/api/push/test').get_json(), {'success': True, 'sent': 1})
        self.assertEqual(json.loads(post.call_args.kwargs['data'])['kind'], 'test')


class DispatchTest(PushTestCase):
    def test_kinds_and_channels(self):
        from notifications import send_notification
        self.register()
        with mock.patch('blueprints.push.routes.requests.post', return_value=_response(200)) as post, \
                mock.patch('blueprints.ntfy.routes.send_ntfy_notification') as ntfy:
            send_notification('Série : X', kind='monitor', icon='📚', title='Nouveau tome surveillé',
                              click='https://t.me/c/1')
            payload = json.loads(post.call_args.kwargs['data'])
            self.assertEqual(payload['kind'], 'monitor_found')
            self.assertEqual(payload['body'], 'Série : X\nhttps://t.me/c/1')
            send_notification('Astérix - Tome 3', kind='review', title='Validation en attente')
            self.assertEqual(json.loads(post.call_args.kwargs['data'])['kind'], 'review')
            send_notification('\nAstérix', kind='import_completed', title='BD Importé :')
            self.assertEqual(json.loads(post.call_args.kwargs['data'])['title'], 'BD Importé')
        ntfy.assert_not_called()  # ntfy n'est pas activé

    def test_new_review_is_notified_once(self):
        from blueprints.bedetheque import auto_acquire
        candidate = [{'source': 'ebdz', 'title': 'Asterix T03.cbz', 'filename': 'Asterix T03.cbz'}]
        with mock.patch('notifications.send_notification') as notify:
            auto_acquire.queue_manual_review(7, 'Astérix', 3, 'Tome 3', candidate, 'À vérifier', force_candidates=True)
            auto_acquire.queue_manual_review(7, 'Astérix', 3, 'Tome 3', candidate, 'À vérifier', force_candidates=True)
        self.assertEqual(notify.call_count, 1)
        self.assertEqual(notify.call_args.kwargs['kind'], 'review')
        conn = sqlite3.connect(self.app.config['DATABASE'])
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM auto_acquire_reviews').fetchone()[0], 1)
        conn.close()


if __name__ == '__main__':
    unittest.main()
