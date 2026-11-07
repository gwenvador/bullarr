"""Fichier encore en cours de téléchargement (préalloué, début vide) : refusé tout de suite
au lieu d'occuper le worker d'import jusqu'à son délai de 300 s."""
import os
import tempfile
import time
import unittest

from blueprints.library import import_worker


class IncompleteFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, name, data):
        path = os.path.join(self.tmp.name, name)
        with open(path, 'wb') as handle:
            handle.write(data)
        return path

    def test_preallocated_pdf_is_refused_at_once(self):
        path = self.write('T05.pdf', b'\0' * 200_000 + b'trailer\n%%EOF\n')
        staging = os.path.join(self.tmp.name, 'staging')
        os.makedirs(staging)
        started = time.time()
        with self.assertRaises(import_worker.IncompleteFileError) as ctx:
            import_worker._prepare_child({'source_path': path, 'format': 'pdf', 'auto_convert': True,
                                          'staging_dir': staging,
                                          'conversion_lock': os.path.join(staging, '.lock')})
        self.assertIn('pas terminé', str(ctx.exception))
        self.assertLess(time.time() - started, 5)
        self.assertEqual(os.listdir(staging), [])  # rien de copié

    def test_valid_headers_pass(self):
        import_worker.ensure_not_preallocated(self.write('a.pdf', b'%PDF-1.7\n' + b'\0' * 2000), 'pdf')
        import_worker.ensure_not_preallocated(self.write('a.cbz', b'PK\x03\x04' + b'\0' * 2000), 'cbz')

    def test_empty_archive_start_is_incomplete(self):
        with self.assertRaises(import_worker.IncompleteFileError):
            import_worker.ensure_not_preallocated(self.write('a.cbz', b'\0' * 4096), 'cbz')
        with self.assertRaises(import_worker.IncompleteFileError):
            import_worker.ensure_not_preallocated(self.write('b.pdf', b''), 'pdf')


if __name__ == '__main__':
    unittest.main()
