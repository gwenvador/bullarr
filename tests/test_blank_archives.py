import os
import tempfile
import unittest
from pathlib import Path

from archive_utils import detect_actual_format, has_archive_signature, is_blank_file, looks_like_tar

ROOT = Path(__file__).resolve().parents[1]


class BlankArchiveTest(unittest.TestCase):
    """Sillage 20, 22 et 23: fichiers « .cbz » entièrement vides enregistrés comme tar."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, name, data):
        path = os.path.join(self.tmp.name, name)
        with open(path, 'wb') as handle:
            handle.write(data)
        return path

    def test_zero_filled_file_is_blank_and_not_a_tar(self):
        path = self.write('Sillage - #20.cbz', b'\x00' * (4 * 1024 * 1024))
        self.assertTrue(is_blank_file(path))
        self.assertFalse(looks_like_tar(path))
        self.assertEqual(detect_actual_format(path, 'cbz'), 'cbz')   # plus « tar »

    def test_empty_file_is_blank(self):
        self.assertTrue(is_blank_file(self.write('vide.cbz', b'')))

    def test_real_archives_are_not_blank(self):
        import io, tarfile, zipfile
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('1.jpg', b'x')
        self.assertFalse(is_blank_file(self.write('ok.cbz', buffer.getvalue())))
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode='w') as archive:
            info = tarfile.TarInfo('1.jpg')
            info.size = 1
            archive.addfile(info, io.BytesIO(b'x'))
        tar_path = self.write('vrai.cbz', tar_buffer.getvalue())
        self.assertTrue(looks_like_tar(tar_path))
        self.assertEqual(detect_actual_format(tar_path, 'cbz'), 'tar')

    def test_verification_reports_blank_files_even_when_recorded_as_tar(self):
        from blueprints.settings.routes import _verify_archive_formats
        path = self.write('Sillage - #22.cbz', b'\x00' * 8192)
        volume = {'id': 99601, 'filepath': path, 'format': 'tar', 'filename': 'Sillage - #22.cbz'}
        items = _verify_archive_formats([{'id': 1, 'title': 'Sillage'}], {1: [volume]})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['actual_format'], 'invalide')
        self.assertIn('contenu est vide', items[0]['reason'])

    def test_import_refuses_blank_archives(self):
        source = (ROOT / 'blueprints/library/routes.py').read_text()
        self.assertIn("is_blank_file(safe_source_path)", source)
        self.assertIn("not has_archive_signature(safe_source_path)", source)
        self.assertIn("Fichier corrompu : archive illisible", source)
        script = (ROOT / 'static/js/verification.js').read_text()
        self.assertIn("item.actual_format === 'invalide'", script)


    def test_unfinished_copy_with_a_filled_start_is_not_an_archive(self):
        # Syncthing écrit les blocs dans le désordre: le début peut déjà être rempli alors que l'annuaire central
        # du ZIP (en fin de fichier) manque encore.
        import io, zipfile
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('1.jpg', b'x' * 5000)
        data = buffer.getvalue()
        complete = self.write('complet.cbz', data)
        unfinished = self.write('en-cours.cbz', data[:200] + b'\x00' * (len(data) - 200))
        truncated = self.write('tronque.cbz', data[:len(data) - 40])
        self.assertTrue(has_archive_signature(complete))
        self.assertFalse(has_archive_signature(unfinished))
        self.assertFalse(has_archive_signature(truncated))
        self.assertFalse(is_blank_file(unfinished))   # le contrôle du début seul ne suffisait pas


if __name__ == '__main__':
    unittest.main()
