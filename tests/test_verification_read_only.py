import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

WORKTREE = Path('[REDACTED_PATH]')
sys.path.insert(0, str(WORKTREE))


class VerificationNamingSafetyTest(unittest.TestCase):
    def test_misnamed_verification_never_lists_a_series_folder_for_rename(self):
        routes = importlib.import_module('blueprints.settings.routes')
        series = {
            'id': 1202,
            'title': 'Châteaux Bordeaux - À table !',
            'path': '/BD/Châteaux Bordeaux/À table !',
            'is_oneshot': 0,
            'universe_name': 'Châteaux Bordeaux',
        }
        config = {
            'volume_template': '<series> - #<volume>',
            'oneshot_template': '<series>',
            'series_template': '<universe>/<series>',
        }
        with patch.object(routes, 'load_rename_config', return_value=config):
            result = routes._verify_misnamed([series], {1202: []})
        self.assertEqual([], result)


if __name__ == '__main__':
    unittest.main()
