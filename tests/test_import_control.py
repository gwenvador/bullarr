import os
import tempfile
import unittest

from flask import Flask

from blueprints.library import import_control


class ImportControlTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.app = Flask(__name__)
        self.app.config["LIBRARY_IMPORT_CONFIG_FILE"] = os.path.join(self.dir.name, "library_import.json")
        self.ctx = self.app.app_context()
        self.ctx.push()
        import_control.resume()

    def tearDown(self):
        import_control.resume()
        self.ctx.pop()
        self.dir.cleanup()

    def test_stop_pauses_automatic_import_and_stops_the_batch(self):
        self.assertFalse(import_control.should_stop(manual=False))
        import_control.request_stop()
        self.assertTrue(import_control.is_paused())
        self.assertTrue(import_control.should_stop(manual=False))
        self.assertTrue(import_control.should_stop(manual=True))

    def test_new_manual_import_restarts_after_a_stop_while_automatic_stays_paused(self):
        import_control.request_stop()
        import_control.clear_stop()
        self.assertFalse(import_control.should_stop(manual=True))
        self.assertTrue(import_control.should_stop(manual=False))

    def test_pause_survives_a_restart_and_resume_clears_it(self):
        import_control.request_stop()
        import_control._stop_event.clear()  # redémarrage: l\x27événement mémoire est perdu
        self.assertTrue(import_control.is_paused())
        self.assertTrue(import_control.should_stop(manual=False))
        self.assertFalse(import_control.should_stop(manual=True))
        import_control.resume()
        self.assertFalse(import_control.is_paused())
        self.assertFalse(import_control.should_stop(manual=False))


class ImportStopRoutesTest(unittest.TestCase):
    def test_batch_checks_stop_before_each_file(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "blueprints/library/routes.py").read_text()
        loop = source.index("for file_data in files_to_import:")
        self.assertIn("import_control.should_stop(operation_type == \x27manual_import\x27)", source[loop:loop + 400])
        self.assertIn("/api/import/stop", source)
        self.assertIn("/api/import/resume", source)
