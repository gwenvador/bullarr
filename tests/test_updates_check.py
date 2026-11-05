import json
import unittest
from unittest import mock

from blueprints.settings import updates


class VersionCompareTest(unittest.TestCase):
    def test_newer_versions_are_detected(self):
        self.assertTrue(updates.is_newer('v9.1', '9.0'))
        self.assertTrue(updates.is_newer('9.10', '9.9'))     # 10 > 9, pas une comparaison de texte
        self.assertFalse(updates.is_newer('v9.1', '9.1'))
        self.assertFalse(updates.is_newer('v9.0', '9.1'))
        self.assertFalse(updates.is_newer('', '9.1'))


class UpdateStatusTest(unittest.TestCase):
    def setUp(self):
        updates._cache.clear()

    def release(self, tag, apk=None):
        return {'tag': tag, 'url': f'https://github.com/x/releases/tag/{tag}', 'published_at': None, 'apk_url': apk}

    def test_reports_update_and_android_apk(self):
        releases = {'bullarr': self.release('v9.2'), 'android': self.release('v1.3', 'https://x/app-1.3.apk')}
        with mock.patch.object(updates, '_fetch_release', side_effect=lambda url: releases[
                'bullarr' if 'bullarr/' in url or url.endswith('bullarr/releases/latest') else 'android']):
            status = updates.update_status('9.1')
        self.assertTrue(status['bullarr']['update_available'])
        self.assertEqual(status['bullarr']['latest'], '9.2')

    def test_github_failure_keeps_the_last_known_value_and_never_raises(self):
        with mock.patch.object(updates, '_fetch_release', return_value=self.release('v9.1')):
            updates.latest_release('bullarr', now=1000)
        with mock.patch.object(updates, '_fetch_release', side_effect=OSError('hors ligne')):
            value = updates.latest_release('bullarr', now=1000 + updates.CACHE_SECONDS + 5)
            self.assertEqual(value['tag'], 'v9.1')
            self.assertIsNone(updates.latest_release('android', now=5))

    def test_cache_avoids_calling_github_again(self):
        with mock.patch.object(updates, '_fetch_release', return_value=self.release('v9.1')) as fetch:
            updates.latest_release('bullarr', now=1000)
            updates.latest_release('bullarr', now=1000 + 60)
        self.assertEqual(fetch.call_count, 1)

    def test_apk_asset_is_picked_from_the_release(self):
        payload = {'tag_name': 'v1.3', 'html_url': 'https://x/r', 'assets': [
            {'name': 'notes.txt', 'browser_download_url': 'https://x/notes'},
            {'name': 'bullarr-android-1.3.apk', 'browser_download_url': 'https://x/app.apk'}]}
        response = mock.Mock(content=json.dumps(payload).encode())
        with mock.patch.object(updates, 'safe_external_get', return_value=response):
            self.assertEqual(updates._fetch_release('https://api.github.com/x')['apk_url'], 'https://x/app.apk')


class AppVersionTest(unittest.TestCase):
    def test_app_version_is_the_current_release(self):
        from config import Config
        self.assertEqual(Config.APP_VERSION, '11.0')


class AboutCardTest(unittest.TestCase):
    def test_android_version_sits_next_to_the_bullarr_version_and_is_the_link(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        template = (root / 'templates/settings.html').read_text()
        script = (root / 'static/js/settings.js').read_text()
        badges = template[template.index('<div class="about-badges">'):template.index('<nav class="about-actions"')]
        self.assertIn('about-version-number">v{{ app_version }}', badges)
        self.assertIn('<a id="about-android" class="about-badge about-badge-link"', badges)
        self.assertIn('id="about-android-link"', badges)
        self.assertNotIn('>Application Android (APK)</a>', template)
        self.assertNotIn('Télécharger</a>', script)
        self.assertIn("document.getElementById('about-android-link').textContent = `v${android.latest}`", script)
        self.assertIn('badge.href = android.apk_url || android.url', script)


if __name__ == '__main__':
    unittest.main()
