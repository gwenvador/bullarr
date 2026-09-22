import unittest
from unittest.mock import patch

from flask import Flask

from blueprints.komga import client as komga_client


class KomgaRefreshAfterMetadataTest(unittest.TestCase):
    def test_scan_callback_runs_after_all_library_scans(self):
        app = Flask(__name__)
        callback_events = []

        class ImmediateThread:
            def __init__(self, target, daemon):
                self.target = target

            def start(self):
                self.target()

        with app.app_context(), \
             patch.object(komga_client, 'KomgaClient') as client_cls, \
             patch.object(komga_client.threading, 'Thread', ImmediateThread):
            client = client_cls.return_value
            client.list_libraries.return_value = [{'id': 'library-1'}, {'id': 'library-2'}]
            komga_client.trigger_scan_async(
                after_scan=lambda: callback_events.append('resync')
            )

        self.assertEqual(client.scan_library.call_args_list, [
            unittest.mock.call('library-1'),
            unittest.mock.call('library-2'),
        ])
        self.assertEqual(callback_events, ['resync'])


if __name__ == '__main__':
    unittest.main()
