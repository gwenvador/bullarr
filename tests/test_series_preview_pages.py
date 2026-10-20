import io
import os
import sqlite3
import tempfile
import time
import unittest
import zipfile

from PIL import Image

from blueprints.library import preview_pages as pp


def _png(color, size=(800, 1200)):
    buffer = io.BytesIO()
    Image.new('RGB', size, color).save(buffer, 'PNG')
    return buffer.getvalue()


class PreviewPageIndicesTest(unittest.TestCase):
    def test_pages_are_spread_through_the_album(self):
        self.assertEqual(pp.preview_page_indices(48), [9, 19, 28, 35])
        self.assertEqual(pp.preview_page_indices(56), [11, 22, 33, 41])
        # Jamais la couverture (0) ni la dernière page
        self.assertEqual(pp.preview_page_indices(8), [1, 3, 4, 5])
        self.assertEqual(pp.preview_page_indices(5), [1, 2, 3])
        self.assertEqual(pp.preview_page_indices(3), [1])
        self.assertEqual(pp.preview_page_indices(2), [0, 1])
        self.assertEqual(pp.preview_page_indices(0), [])


class ExtractPreviewPagesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.covers = os.path.join(self.tmp.name, 'covers')

    def tearDown(self):
        self.tmp.cleanup()

    def _cbz(self, pages=20):
        path = os.path.join(self.tmp.name, 'Serie - T01.cbz')
        with zipfile.ZipFile(path, 'w') as archive:
            # Ordre naturel : page10 vient après page9, pas après page1.
            for number in range(1, pages + 1):
                archive.writestr(f'Serie/page{number}.png', _png((number * 10 % 256, 0, 0)))
            archive.writestr('__MACOSX/._page1.png', b'junk')
        return path

    def test_cbz_pages_are_extracted_resized_and_cached(self):
        path = self._cbz()
        result = pp.extract_preview_pages(path, 'cbz', self.covers)
        # 20 pages : pages 5, 9, 12 et 15 du fichier (couverture = page 1)
        self.assertEqual([number for _path, number in result], [5, 9, 12, 15])
        pages = [p for p, _n in result]
        self.assertEqual(len(pages), 4)
        first = Image.open(os.path.join(self.covers, pages[0].split('covers/', 1)[1]))
        self.assertLessEqual(first.size[1], pp.PREVIEW_MAX_SIZE[1])
        # 20 pages : première vignette = index 4 = page5.png, rouge 50
        self.assertEqual(first.convert('RGB').getpixel((10, 10))[0] // 5, 50 // 5)
        # Deuxième appel : même fichiers, rien n'est recalculé
        mtimes = [os.path.getmtime(os.path.join(self.covers, p.split('covers/', 1)[1])) for p in pages]
        self.assertEqual(pp.extract_preview_pages(path, 'cbz', self.covers), result)
        self.assertEqual(mtimes, [os.path.getmtime(os.path.join(self.covers, p.split('covers/', 1)[1])) for p in pages])

    def test_replaced_file_gets_new_previews_and_old_ones_are_removed(self):
        path = self._cbz()
        old = [p for p, _n in pp.extract_preview_pages(path, 'cbz', self.covers)]
        later = time.time() + 10
        os.utime(path, (later, later))
        new = [p for p, _n in pp.extract_preview_pages(path, 'cbz', self.covers)]
        self.assertNotEqual(old, new)
        self.assertFalse(any(os.path.exists(os.path.join(self.covers, p.split('covers/', 1)[1])) for p in old))

    def test_pdf_pages_are_rendered(self):
        import fitz
        path = os.path.join(self.tmp.name, 'album.pdf')
        doc = fitz.open()
        for _ in range(15):
            doc.new_page(width=595, height=842)
        doc.save(path)
        doc.close()
        result = pp.extract_preview_pages(path, 'pdf', self.covers)
        self.assertEqual([n for _p, n in result], [4, 7, 9, 11])
        self.assertTrue(all(p.startswith('covers/previews/') and p.endswith(f'-p{n}.jpg') for p, n in result))


class PickPreviewVolumeTest(unittest.TestCase):
    def test_first_regular_volume_with_a_file_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            existing = os.path.join(tmp, 't2.cbz')
            open(existing, 'wb').close()
            conn = sqlite3.connect(':memory:')
            conn.execute('CREATE TABLE volumes (id INTEGER, series_id INTEGER, filepath TEXT, format TEXT, '
                         'volume_number INTEGER, is_integral INTEGER, is_hs INTEGER, is_episode INTEGER, '
                         'filesystem_present INTEGER)')
            conn.executemany('INSERT INTO volumes VALUES (?,?,?,?,?,?,?,?,?)', [
                (1, 7, existing, 'cbz', 1, 1, 0, 0, 1),           # intégrale : après les tomes
                (2, 7, os.path.join(tmp, 'absent.cbz'), 'cbz', 1, 0, 0, 0, 1),  # fichier absent
                (3, 7, existing, 'cbz', 2, 0, 0, 0, 1),
                (4, 7, None, None, 3, 0, 0, 0, 1),                 # tome manquant
                (5, 8, existing, 'cbz', 1, 0, 0, 0, 1),            # autre série
            ])
            self.assertEqual(pp.pick_preview_volume(conn.cursor(), 7)['id'], 3)
            self.assertIsNone(pp.pick_preview_volume(conn.cursor(), 99))


if __name__ == '__main__':
    unittest.main()
