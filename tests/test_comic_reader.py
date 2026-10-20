import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image

from blueprints.library import reader

ROOT = Path(__file__).resolve().parents[1]


def _png(color):
    buffer = io.BytesIO()
    Image.new('RGB', (40, 60), color).save(buffer, 'PNG')
    return buffer.getvalue()


class ComicReaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reader._index_cache.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def _cbz(self):
        path = os.path.join(self.tmp.name, 'album.cbz')
        with zipfile.ZipFile(path, 'w') as archive:
            for number in (10, 2, 1):
                archive.writestr(f'pages/p{number}.png', _png((number * 20, 0, 0)))
            archive.writestr('pages/notes.txt', b'pas une page')
            archive.writestr('__MACOSX/pages/._p1.png', b'junk')
        return path

    def test_pages_in_natural_order_and_served_as_is(self):
        path = self._cbz()
        self.assertEqual(reader.page_count(path, 'cbz'), 3)
        data, mimetype = reader.read_page(path, 'cbz', 3)
        self.assertEqual(mimetype, 'image/png')
        # 3e page = p10.png (ordre naturel : p1, p2, p10)
        self.assertEqual(Image.open(io.BytesIO(data)).convert('RGB').getpixel((1, 1))[0], 200)

    def test_page_out_of_range(self):
        path = self._cbz()
        for number in (0, 4):
            with self.assertRaises(IndexError):
                reader.read_page(path, 'cbz', number)

    def test_pdf_pages_are_rendered_as_jpeg(self):
        import fitz
        path = os.path.join(self.tmp.name, 'album.pdf')
        doc = fitz.open()
        for _ in range(3):
            doc.new_page(width=595, height=842)
        doc.save(path)
        doc.close()
        self.assertEqual(reader.page_count(path, 'pdf'), 3)
        data, mimetype = reader.read_page(path, 'pdf', 2)
        self.assertEqual(mimetype, 'image/jpeg')
        self.assertLessEqual(Image.open(io.BytesIO(data)).size[1], reader.READER_PDF_MAX_HEIGHT + 1)

    def test_index_is_cached_until_the_file_changes(self):
        path = self._cbz()
        reader.page_count(path, 'cbz')
        self.assertEqual(len(reader._index_cache), 1)
        reader.page_count(path, 'cbz')
        self.assertEqual(len(reader._index_cache), 1)
        os.utime(path, (1, 1))
        reader.page_count(path, 'cbz')
        self.assertEqual(len(reader._index_cache), 2)

    def test_unsupported_format(self):
        path = os.path.join(self.tmp.name, 'livre.epub')
        open(path, 'wb').write(b'x')
        with self.assertRaises(ValueError):
            reader.page_count(path, 'epub')


class ComicReaderWiringTest(unittest.TestCase):
    def test_read_icon_next_to_buy_and_oneshot_button(self):
        js = (ROOT / 'static/js/library.js').read_text()
        self.assertIn('${buildVolumeReadIconHtml(v)}\n        ${buildVolumeBuyItemsHtml(v)}', js)
        self.assertIn('class="series-read-btn" onclick="openComicReader(', js)
        for template in ('templates/series-detail.html', 'templates/library.html'):
            self.assertIn("js/reader.js", (ROOT / template).read_text())

    def test_reader_navigation(self):
        js = (ROOT / 'static/js/reader.js').read_text()
        self.assertIn('rect.width / 2 ? -1 : 1', js)          # clic gauche / droite
        self.assertIn("addEventListener('wheel'", js)        # souris
        self.assertIn('requestFullscreen', js)               # plein écran


if __name__ == '__main__':
    unittest.main()
