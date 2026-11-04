"""Tableau « Événements » (une ligne par type, une case par canal) et « Téléchargement ajouté »."""
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from flask import Flask

from config import Config
from encryption import ensure_encryption_key


class EventsTestCase(unittest.TestCase):
    def setUp(self):
        ensure_encryption_key()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Flask(__name__)
        self.app.config.update(
            NTFY_CONFIG=dict(Config.NTFY_CONFIG), NTFY_CONFIG_FILE=os.path.join(self.tmp.name, 'ntfy.json'),
            TELEGRAM_CONFIG=dict(Config.TELEGRAM_CONFIG), TELEGRAM_CONFIG_FILE=os.path.join(self.tmp.name, 'tg.json'),
            PUSH_ENDPOINTS_FILE=os.path.join(self.tmp.name, 'push.json'),
            DATABASE=os.path.join(self.tmp.name, 'bullarr.db'), DATA_DIR=self.tmp.name,
        )
        from blueprints.notification_events import notification_events_bp
        from blueprints.telegram import telegram_bp
        self.app.register_blueprint(notification_events_bp, url_prefix='/api/notifications')
        self.app.register_blueprint(telegram_bp, url_prefix='/api/telegram')
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)
        with open(self.app.config['TELEGRAM_CONFIG_FILE'], 'w', encoding='utf-8') as handle:
            json.dump({'enabled': True, 'chat_id': '42', 'bot_token': ''}, handle)


class EventsRouteTest(EventsTestCase):
    def test_one_row_per_event_with_defaults(self):
        data = self.client.get('/api/notifications/events').get_json()
        ids = [event['id'] for event in data['events']]
        self.assertEqual(ids, ['download_added', 'import_completed', 'import_available', 'monitor', 'review', 'update_available'])
        self.assertTrue(data['channels']['telegram']['events']['download_added'])
        self.assertFalse(data['channels']['ntfy']['events']['review'])
        self.assertEqual(data['push_devices'], 0)

    def test_saving_one_box_keeps_the_rest_of_the_channel(self):
        with mock.patch('blueprints.library.scheduler.sync_auto_import_schedule'):
            response = self.client.post('/api/notifications/events', json={'telegram': {'monitor': False}})
        self.assertEqual(response.get_json(), {'success': True})
        with open(self.app.config['TELEGRAM_CONFIG_FILE'], encoding='utf-8') as handle:
            stored = json.load(handle)
        self.assertEqual((stored['enabled'], stored['chat_id'], stored['notify_monitor']), (True, '42', False))
        from notifications import notification_wanted
        self.assertFalse(notification_wanted('monitor'))
        self.assertTrue(notification_wanted('import_completed'))

    def test_channel_form_does_not_reset_the_boxes(self):
        with mock.patch('blueprints.library.scheduler.sync_auto_import_schedule'):
            self.client.post('/api/notifications/events', json={'telegram': {'import_available': False}})
            self.client.post('/api/telegram/config', json={'enabled': True, 'chat_id': '43', 'bot_token': '****'})
        with open(self.app.config['TELEGRAM_CONFIG_FILE'], encoding='utf-8') as handle:
            stored = json.load(handle)
        self.assertEqual((stored['chat_id'], stored['notify_import_available']), ('43', False))


class DownloadAddedTest(EventsTestCase):
    def test_message_and_cover(self):
        import notifications
        conn = sqlite3.connect(self.app.config['DATABASE'])
        conn.execute('CREATE TABLE series (id INTEGER PRIMARY KEY, title TEXT, local_cover_path TEXT, '
                     'bedetheque_cover_path TEXT, komga_cover_path TEXT)')
        conn.execute("INSERT INTO series VALUES (5, 'Astérix', NULL, 'covers/5.jpg', NULL)")
        conn.commit()
        conn.close()
        os.makedirs(os.path.join(self.tmp.name, 'covers'))
        open(os.path.join(self.tmp.name, 'covers', '5.jpg'), 'wb').close()

        class Inline:
            def __init__(self, target, **_):
                self.target = target

            def start(self):
                self.target()

        with mock.patch('threading.Thread', Inline), \
                mock.patch.object(notifications, 'send_notification', return_value=(True, [])) as send:
            notifications.notify_download_added('Asterix.T03.cbz', 'qbittorrent', series_id=5, volume_number=3)
        message = send.call_args.args[0]
        self.assertEqual(message, 'Astérix - Tome 3\nFichier : Asterix.T03.cbz\nClient : qBittorrent')
        self.assertEqual(send.call_args.kwargs['kind'], 'download_added')
        self.assertTrue(send.call_args.kwargs['photo_path'].endswith(os.path.join('covers', '5.jpg')))

    def test_android_receives_it_as_an_import_event(self):
        from notifications import send_notification
        with open(self.app.config['PUSH_ENDPOINTS_FILE'], 'w', encoding='utf-8') as handle:
            json.dump({'endpoints': [{'endpoint': 'https://ntfy.sh/upX?up=1'}]}, handle)
        with mock.patch('blueprints.telegram.routes.send_telegram_notification', return_value=(True, None)), \
                mock.patch('blueprints.push.routes.requests.post', return_value=mock.Mock(status_code=200)) as post:
            send_notification('Fichier : x', kind='download_added', title='Téléchargement ajouté')
        self.assertEqual(json.loads(post.call_args.kwargs['data'])['kind'], 'import_available')

    def test_retry_does_not_notify(self):
        from blueprints.missing_monitor import downloader
        source = open(downloader.__file__, encoding='utf-8').read()
        self.assertIn('if not retry_count:\n            from notifications import notify_download_added', source)


if __name__ == '__main__':
    unittest.main()
