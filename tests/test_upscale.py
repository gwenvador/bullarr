import io
import os
import tempfile
import unittest

from blueprints.library import upscale

try:
    import numpy as np
    from PIL import Image
except ImportError:  # pragma: no cover
    np = None


class TileOriginsTest(unittest.TestCase):
    def test_spans_cover_side_once_and_stay_inside(self):
        for size in (100, 256, 257, 800, 1080, 1600):
            spans = upscale.tile_origins(size)
            covered = []
            for start, end, read in spans:
                covered.extend(range(start, end))
                tile = min(upscale.TILE, size)
                self.assertTrue(0 <= read and read + tile <= size)
                self.assertTrue(read <= start and end <= read + tile)
            self.assertEqual(covered, list(range(size)))

    def test_needs_upscale(self):
        self.assertTrue(upscale.needs_upscale(800, 1080))
        self.assertTrue(upscale.needs_upscale(1200, 1600))
        self.assertFalse(upscale.needs_upscale(1700, 2400))
        self.assertFalse(upscale.needs_upscale(0, 0))


@unittest.skipIf(np is None, 'numpy absent')
class UpscaleArrayTest(unittest.TestCase):
    def test_tiles_reassemble_like_whole_image(self):
        rng = np.random.default_rng(1)
        rgb = rng.integers(0, 256, (300, 520, 3), dtype=np.uint8)
        nearest = lambda tile: tile.repeat(upscale.SCALE, axis=2).repeat(upscale.SCALE, axis=3)
        out = upscale.upscale_array(rgb, nearest)
        expected = rgb.repeat(upscale.SCALE, axis=0).repeat(upscale.SCALE, axis=1)
        self.assertEqual(out.shape, expected.shape)
        self.assertTrue(np.array_equal(out, expected))

    def test_enhanced_page_cache_and_high_def(self):
        calls = []

        def fake_upscale(data, model):
            calls.append(model)
            with Image.open(io.BytesIO(data)) as image:
                return None if not upscale.needs_upscale(*image.size) else b'JPEG'

        def page(size):
            def read_page(filepath, fmt, number):
                buffer = io.BytesIO()
                Image.new('RGB', size).save(buffer, 'PNG')
                return buffer.getvalue(), 'image/png'
            return read_page

        original = upscale.upscale_image
        upscale.upscale_image = fake_upscale
        try:
            with tempfile.TemporaryDirectory() as tmp:
                book = os.path.join(tmp, 'a.cbz')
                open(book, 'wb').close()
                cache = os.path.join(tmp, 'cache')
                self.assertEqual(upscale.enhanced_page(book, 'cbz', 1, 'fast', cache, page((800, 1080))), b'JPEG')
                self.assertEqual(upscale.enhanced_page(book, 'cbz', 1, 'fast', cache, page((800, 1080))), b'JPEG')
                self.assertEqual(len(calls), 1)  # deuxième fois : depuis le cache
                self.assertIsNone(upscale.enhanced_page(book, 'cbz', 2, 'fast', cache, page((2000, 3000))))
                self.assertIsNone(upscale.enhanced_page(book, 'cbz', 2, 'fast', cache, page((2000, 3000))))
                self.assertEqual(len(calls), 2)
                with self.assertRaises(ValueError):
                    upscale.enhanced_page(book, 'cbz', 1, 'other', cache, page((800, 1080)))
                upscale.prune(cache, max_bytes=0)
                self.assertEqual(sum(os.path.getsize(os.path.join(r, n)) for r, _d, f in os.walk(cache) for n in f), 0)
        finally:
            upscale.upscale_image = original


if __name__ == '__main__':
    unittest.main()
