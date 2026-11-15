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
        self.assertIn("/api/import/cancel", source)


class ImportCancelTest(unittest.TestCase):
    """« Annuler l'import » : interrompt le fichier en cours, sans pause de l'import automatique."""

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

    def test_cancel_without_running_import_does_nothing(self):
        self.assertFalse(import_control.request_cancel())
        self.assertFalse(import_control.cancel_requested())

    def test_cancel_stops_the_running_batch_without_pausing(self):
        import_control.register_operation('op-1')
        try:
            self.assertTrue(import_control.request_cancel())
            self.assertTrue(import_control.should_stop(manual=True))
            with self.assertRaises(import_control.ImportCancelled):
                import_control.raise_if_cancelled()
            self.assertFalse(import_control.is_paused())
        finally:
            import_control.unregister_operation('op-1')
        # Lot terminé : l'annulation ne touche pas le lot suivant.
        self.assertFalse(import_control.cancel_requested())
        self.assertFalse(import_control.should_stop(manual=False))

    def test_cancel_kills_the_worker_subprocess(self):
        import sys, time
        from blueprints.library.import_worker import run_killable_command, ImportWorkerCancelled
        start = time.monotonic()
        with self.assertRaises(ImportWorkerCancelled):
            run_killable_command([sys.executable, '-c', 'import time; time.sleep(30)'], timeout=60,
                                 cancel_check=lambda: time.monotonic() - start > 1)
        self.assertLess(time.monotonic() - start, 10)


if __name__ == '__main__':
    unittest.main()
