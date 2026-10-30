import unittest
from unittest import mock

from blueprints.bedetheque import routes


class SeriesNotFoundMessageTest(unittest.TestCase):
    def test_pause_is_reported_instead_of_series_not_found(self):
        guard = mock.Mock()
        guard.blocked_message.return_value = "Bédéthèque en pause jusqu'à 08:58 (HTTP 403 (défi Cloudflare))"
        with mock.patch('blueprints.bedetheque.guard.get_guard', return_value=guard):
            self.assertIn('08:58', routes._series_not_found_error())

    def test_regular_message_is_kept_when_not_paused(self):
        guard = mock.Mock()
        guard.blocked_message.return_value = None
        with mock.patch('blueprints.bedetheque.guard.get_guard', return_value=guard):
            self.assertEqual(routes._series_not_found_error(), 'Impossible de trouver la série sur Bedetheque')
