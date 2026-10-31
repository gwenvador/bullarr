import io
import os
import tarfile
import tempfile
import time
import unittest
import zipfile

from blueprints.library.archive_converter import classify_archive


class ClassifyArchiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name, data):
        path = os.path.join(self.tmp.name, name)
        with open(path, 'wb') as handle:
            handle.write(data)
        return path

    def test_zip_rar_and_tar_are_recognised(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('1.jpg', b'x')
        self.assertEqual(classify_archive(self.path('a.cbz', buffer.getvalue())), 'zip')
        self.assertEqual(classify_archive(self.path('b.cbz', b'Rar!\x1a\x07\x01\x00' + b'x' * 100)), 'rar')
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode='w') as archive:
            info = tarfile.TarInfo('1.jpg')
            info.size = 1
            archive.addfile(info, io.BytesIO(b'x'))
        self.assertEqual(classify_archive(self.path('c.cbz', tar_buffer.getvalue())), 'tar')
        gz_buffer = io.BytesIO()
        with tarfile.open(fileobj=gz_buffer, mode='w:gz') as archive:
            info = tarfile.TarInfo('1.jpg')
            info.size = 1
            archive.addfile(info, io.BytesIO(b'x'))
        self.assertEqual(classify_archive(self.path('d.cbz', gz_buffer.getvalue())), 'tar')

    def test_file_still_being_copied_is_unknown_and_instant(self):
        # Octets nuls préalloués par la copie en cours: ni tar ni archive, et surtout pas de lecture complète.
        path = self.path('e.cbz', b'\x00' * (8 * 1024 * 1024))
        started = time.monotonic()
        self.assertEqual(classify_archive(path), 'unknown')
        self.assertLess(time.monotonic() - started, 1)


if __name__ == '__main__':
    unittest.main()
