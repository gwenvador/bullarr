"""Notification « Mise à jour disponible » (blueprints/settings/update_notifier.py)."""
import json
import os
import tempfile
import unittest
from unittest import mock

from flask import Flask

from blueprints.settings import update_notifier


def _release(tag, url='https://github.com/gwenvador/bullarr/releases/tag/x', apk=None):
    return {'tag': tag, 'url': url, 'apk_url': apk, 'published_at': None}


class UpdateNotifierTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Flask(__name__)
        self.app.config.update(DATA_DIR=self.tmp.name, APP_VERSION='10.0')
        self.releases = {'bullarr': _release('v10.1'), 'android': _release('v2.0', apk='https://x/app.apk')}

    def run_check(self, wanted=True):
        with self.app.app_context(), \
                mock.patch('notifications.notification_wanted', return_value=wanted), \
                mock.patch('notifications.send_notification', return_value=(True, [])) as send, \
                mock.patch('blueprints.settings.updates.latest_release', side_effect=lambda name: self.releases[name]):
            sent = update_notifier.check_updates_and_notify(self.app)
        return sent, send

    def test_new_bullarr_version_is_notified_once(self):
        sent, send = self.run_check()
        self.assertEqual(sent, [('bullarr', 'v10.1')])
        self.assertIn('10.1', send.call_args.args[0])
        self.assertEqual(send.call_args.kwargs['kind'], 'update_available')
        self.assertEqual(self.run_check()[0], [])

    def test_installed_version_is_not_notified(self):
        self.releases['bullarr'] = _release('v10.0')
        self.assertEqual(self.run_check()[0], [])

    def test_android_starts_from_the_published_release(self):
        self.releases['bullarr'] = _release('v10.0')
        self.assertEqual(self.run_check()[0], [])
        self.releases['android'] = _release('v2.1', apk='https://x/app-2.1.apk')
        sent, send = self.run_check()
        self.assertEqual(sent, [('android', 'v2.1')])
        self.assertEqual(send.call_args.kwargs['click'], 'https://x/app-2.1.apk')
        with open(os.path.join(self.tmp.name, 'update_notify_state.json'), encoding='utf-8') as handle:
            self.assertEqual(json.load(handle)['android'], 'v2.1')

    def test_nothing_when_no_channel_wants_it(self):
        sent, send = self.run_check(wanted=False)
        self.assertEqual(sent, [])
        send.assert_not_called()

    def test_failed_send_is_retried_next_time(self):
        with self.app.app_context(), \
                mock.patch('notifications.notification_wanted', return_value=True), \
                mock.patch('notifications.send_notification', return_value=(False, ['ntfy : Timeout'])), \
                mock.patch('blueprints.settings.updates.latest_release', side_effect=lambda name: self.releases[name]):
            self.assertEqual(update_notifier.check_updates_and_notify(self.app), [])
        self.assertEqual(self.run_check()[0], [('bullarr', 'v10.1')])


if __name__ == '__main__':
    unittest.main()
