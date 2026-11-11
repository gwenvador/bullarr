import io
import os
import sqlite3
import tempfile
import unittest
import zipfile

from PIL import Image

from blueprints.library import page_size


def _cbz(path, sizes):
    with zipfile.ZipFile(path, 'w') as archive:
        for i, size in enumerate(sizes):
            buf = io.BytesIO()
            Image.new('RGB', size, 'white').save(buf, 'JPEG')
            archive.writestr(f'{i:03d}.jpg', buf.getvalue())


class PageSizeTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dir.cleanup()

    def test_measure_uses_middle_pages(self):
        path = os.path.join(self.dir.name, 'a.cbz')
        _cbz(path, [(2000, 3000)] + [(800, 1080)] * 8 + [(1600, 1080)])
        self.assertEqual(page_size.measure(path, 'cbz'), (800, 1080))

    def test_measure_pending_marks_changed_and_unreadable_files(self):
        good = os.path.join(self.dir.name, 'b.cbz')
        _cbz(good, [(1920, 2650)] * 5)
        db = os.path.join(self.dir.name, 'db.sqlite')
        conn = sqlite3.connect(db)
        conn.execute('CREATE TABLE volumes (id INTEGER PRIMARY KEY, filepath TEXT, format TEXT, file_size INTEGER)')
        conn.execute("INSERT INTO volumes VALUES (1, ?, 'cbz', 10), (2, '/absent.cbz', 'cbz', 5), (3, NULL, NULL, NULL)", (good,))
        conn.commit()
        self.assertEqual(page_size.measure_pending(db, pause=0), 2)
        rows = dict((r[0], r[1:]) for r in conn.execute('SELECT id, page_width, page_height FROM volumes'))
        self.assertEqual(rows[1], (1920, 2650))
        self.assertEqual(rows[2], (0, 0))
        self.assertEqual(rows[3], (None, None))
        self.assertEqual(page_size.measure_pending(db, pause=0), 0)
        conn.execute('UPDATE volumes SET file_size = 11 WHERE id = 1')
        conn.commit()
        self.assertEqual(page_size.measure_pending(db, pause=0), 1)
        conn.close()


if __name__ == '__main__':
    unittest.main()
