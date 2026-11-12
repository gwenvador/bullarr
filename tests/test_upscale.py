import io
import os
import sqlite3
import tempfile
import unittest
import zipfile

from blueprints.library import upscale, upscale_volume

try:
    import numpy as np
    from PIL import Image
except ImportError:  # pragma: no cover
    np = None


class CatalogTest(unittest.TestCase):
    def test_same_models_as_android_app(self):
        self.assertEqual(list(upscale.MODELS), ['fast', 'general', 'general_wdn', 'cugan_se', 'cugan_se_dn3',
                                                'cugan_pro', 'cugan_pro_dn3', 'quality'])
        for spec in upscale.MODELS.values():
            self.assertIn(spec['scale'], (2, 4))
            self.assertTrue(spec['label'] and spec['detail'] and spec['file'].endswith('.onnx'))

    def test_spans_cover_side_once(self):
        for spec in (upscale.MODELS['fast'], upscale.MODELS['cugan_se']):
            for size in (100, 256, 257, 800, 1080, 1600):
                size += size % 2 if spec['even'] else 0
                spans = upscale.tile_spans(size, spec)
                covered = [i for start, end in spans for i in range(start, end)]
                self.assertEqual(covered, list(range(size)))
                if spec['even']:
                    self.assertTrue(all((end - start) % 2 == 0 for start, end in spans))

    def test_needs_upscale(self):
        self.assertTrue(upscale.needs_upscale(800, 1080))
        self.assertTrue(upscale.needs_upscale(1200, 1600))
        self.assertFalse(upscale.needs_upscale(1700, 2400))
        self.assertFalse(upscale.needs_upscale(0, 0))


@unittest.skipIf(np is None, 'numpy absent')
class UpscaleArrayTest(unittest.TestCase):
    def test_tiles_reassemble_like_whole_image(self):
        rng = np.random.default_rng(1)
        rgb = rng.integers(0, 256, (301, 519, 3), dtype=np.uint8)
        for model in ('fast', 'cugan_se'):
            spec = upscale.MODELS[model]
            scale, border = spec['scale'], spec['border']

            def nearest(tile):
                if border:   # Real-CUGAN : la bordure est consommée par le modèle
                    tile = tile[:, :, border:-border, border:-border]
                return tile.repeat(scale, axis=2).repeat(scale, axis=3)
            out = upscale.upscale_array(rgb, nearest, spec)
            expected = rgb.repeat(scale, axis=0).repeat(scale, axis=1)
            self.assertEqual(out.shape, expected.shape, model)
            self.assertTrue(np.array_equal(out, expected), model)


def _cbz(path, sizes):
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('ComicInfo.xml', '<ComicInfo/>')
        for index, size in enumerate(sizes, 1):
            buffer = io.BytesIO()
            Image.new('RGB', size, (index * 20, 0, 0)).save(buffer, 'PNG')
            archive.writestr(f'{index:03d}.png', buffer.getvalue())


@unittest.skipIf(np is None, 'numpy absent')
class UpscaleCbzTest(unittest.TestCase):
    def fake(self, data, model):
        with Image.open(io.BytesIO(data)) as image:
            if not upscale.needs_upscale(*image.size):
                return None
            big = image.convert('RGB').resize((image.width * 2, image.height * 2))
        buffer = io.BytesIO()
        big.save(buffer, 'JPEG')
        return buffer.getvalue()

    def test_low_def_pages_replaced_and_others_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = os.path.join(tmp, 'a.cbz')
            _cbz(book, [(800, 1080), (800, 1080), (2000, 3000)])
            temp, upscaled, median = upscale_volume.upscale_cbz(book, 'fast', upscale_page=self.fake)
            self.assertEqual(upscaled, 2)
            self.assertEqual(median, (1600, 2160))
            with zipfile.ZipFile(temp) as archive:
                names = archive.namelist()
            self.assertIn('ComicInfo.xml', names)
            self.assertIn('001.jpg', names)
            self.assertIn('003.png', names)
            os.remove(temp)

    def test_cancel_leaves_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = os.path.join(tmp, 'a.cbz')
            _cbz(book, [(800, 1080)])
            before = open(book, 'rb').read()
            with self.assertRaises(upscale_volume.UpscaleCancelled):
                upscale_volume.upscale_cbz(book, 'fast', cancelled=lambda: True, upscale_page=self.fake)
            self.assertEqual(open(book, 'rb').read(), before)
            self.assertEqual(os.listdir(tmp), ['a.cbz'])

    def test_resume_reuses_pages_already_done(self):
        """Interrompu après la 1re page : la 2e fois, seules les pages restantes sont calculées."""
        with tempfile.TemporaryDirectory() as tmp:
            book = os.path.join(tmp, 'a.cbz')
            work = os.path.join(tmp, 'work')
            _cbz(book, [(800, 1080), (800, 1080), (2000, 3000)])
            calls = []

            def counting(data, model):
                calls.append(1)
                return self.fake(data, model)
            checks = iter([False, True])
            with self.assertRaises(upscale_volume.UpscaleCancelled):
                upscale_volume.upscale_cbz(book, 'fast', cancelled=lambda: next(checks, True),
                                           upscale_page=counting, work_dir=work)
            self.assertEqual(len(calls), 1)
            self.assertEqual(os.listdir(work), ['00001.jpg'])
            calls.clear()
            temp, upscaled, median = upscale_volume.upscale_cbz(book, 'fast', upscale_page=counting, work_dir=work)
            self.assertEqual(len(calls), 2)          # pages 2 et 3 seulement
            self.assertEqual(upscaled, 2)
            self.assertEqual(sorted(os.listdir(work)), ['00001.jpg', '00002.jpg', '00003.same'])
            with zipfile.ZipFile(temp) as archive:
                self.assertIn('001.jpg', archive.namelist())
                self.assertIn('003.png', archive.namelist())
            os.remove(temp)

    def test_backup_path_under_library_root(self):
        self.assertEqual(upscale_volume.backup_path_for('/BD/Blake/T1.cbz', '/BD'),
                         '/BD/.bullarr-sd-backup/Blake/T1.cbz')
        self.assertEqual(upscale_volume.backup_path_for('/autre/T1.cbz', '/BD'),
                         '/autre/.bullarr-sd-backup/T1.cbz')


@unittest.skipIf(np is None, 'numpy absent')
class ProcessAndRestoreTest(unittest.TestCase):
    """Remplacement par la version HD, sauvegarde de l'original, puis restauration."""

    def test_round_trip(self):
        from flask import Flask
        with tempfile.TemporaryDirectory() as tmp:
            library = os.path.join(tmp, 'BD')
            os.makedirs(os.path.join(library, 'Blake'))
            book = os.path.join(library, 'Blake', 'T1.cbz')
            _cbz(book, [(800, 1080), (800, 1080)])
            original = open(book, 'rb').read()
            db = os.path.join(tmp, 'b.db')
            conn = sqlite3.connect(db)
            conn.executescript(f'''
                CREATE TABLE libraries (id INTEGER PRIMARY KEY, path TEXT);
                CREATE TABLE series (id INTEGER PRIMARY KEY, title TEXT, library_id INTEGER);
                CREATE TABLE volumes (id INTEGER PRIMARY KEY, series_id INTEGER, filename TEXT, filepath TEXT,
                    format TEXT, page_width INTEGER, page_height INTEGER, file_size INTEGER, page_size_file_size INTEGER,
                    volume_number REAL, page_count INTEGER);
                INSERT INTO libraries VALUES (1, '{library}');
                INSERT INTO series VALUES (1, 'Blake', 1);
                INSERT INTO volumes VALUES (7, 1, 'T1.cbz', '{book}', 'cbz', 800, 1080, {len(original)}, {len(original)}, 1, 2);
            ''')
            conn.commit()
            conn.close()
            app = Flask(__name__)
            app.config.update(DATABASE=db, DATA_DIR=tmp)
            fake = UpscaleCbzTest().fake
            saved = upscale.upscale_image, upscale_volume._after_change
            upscale.upscale_image = fake
            upscale_volume._after_change = lambda row: None
            try:
                with app.app_context():
                    listed = upscale_volume.low_definition_volumes(app)
                    self.assertEqual([(i['volume_id'], i['upscalable']) for i in listed], [(7, True)])
                    upscale_volume._process(app, 7, 'fast')
                    self.assertEqual(upscale_volume.low_definition_volumes(app), [])
                    backup = os.path.join(library, '.bullarr-sd-backup', 'Blake', 'T1.cbz')
                    self.assertEqual(open(backup, 'rb').read(), original)
                    self.assertNotEqual(open(book, 'rb').read(), original)
                    row = sqlite3.connect(db).execute('SELECT page_width, page_height FROM volumes WHERE id = 7').fetchone()
                    self.assertEqual(row, (1600, 2160))
                    self.assertTrue(upscale_volume.has_backup(app, 7))
                    # Déjà upscalé : refusé tant que la version SD est gardée.
                    self.assertIsNotNone(upscale_volume.check_volume(upscale_volume._volume_row(app, 7)))
                    self.assertIsNone(upscale_volume.restore(app, 7))
                    self.assertEqual(open(book, 'rb').read(), original)
                    self.assertFalse(os.path.exists(os.path.join(library, '.bullarr-sd-backup')))
                    row = sqlite3.connect(db).execute('SELECT page_width, page_height FROM volumes WHERE id = 7').fetchone()
                    self.assertEqual(row, (800, 1080))
                    self.assertFalse(upscale_volume.has_backup(app, 7))
                    self.assertEqual(len(upscale_volume.low_definition_volumes(app)), 1)
            finally:
                upscale.upscale_image, upscale_volume._after_change = saved


class QueueResumeTest(unittest.TestCase):
    """La file notée en base est reprise au démarrage ; les pages orphelines sont nettoyées."""

    def test_resume_requeues_in_order_and_cleans(self):
        import threading
        from flask import Flask
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, 'b.db')
            conn = sqlite3.connect(db)
            conn.executescript('''
                CREATE TABLE libraries (id INTEGER PRIMARY KEY, path TEXT);
                CREATE TABLE series (id INTEGER PRIMARY KEY, title TEXT, library_id INTEGER);
                CREATE TABLE volumes (id INTEGER PRIMARY KEY, series_id INTEGER, filename TEXT, filepath TEXT,
                    format TEXT, page_width INTEGER, page_height INTEGER, file_size INTEGER);''')
            conn.commit()
            conn.close()
            app = Flask(__name__)
            app.config.update(DATABASE=db, DATA_DIR=tmp)
            upscale_volume._queue_add(app, 9, 'quality', 2.0)
            upscale_volume._queue_add(app, 4, 'fast', 1.0)
            upscale_volume._queue_add(app, 5, 'fast', 3.0)
            root = os.path.join(tmp, 'upscale-work')
            for name in ('4-fast-1-2', '8-fast-1-2', 'junk'):
                os.makedirs(os.path.join(root, name))
            seen = []

            def fake_enqueue(app_, volume_id, model):
                seen.append((volume_id, model))
                return (None, 'Tome introuvable') if volume_id == 5 else ({}, None)
            saved = upscale_volume.enqueue
            upscale_volume.enqueue = fake_enqueue
            try:
                upscale_volume.resume(app, delay=0)
                for thread in threading.enumerate():
                    if thread.name == 'upscale-resume':
                        thread.join(5)
            finally:
                upscale_volume.enqueue = saved
            self.assertEqual(seen, [(4, 'fast'), (9, 'quality'), (5, 'fast')])
            self.assertEqual(os.listdir(root), ['4-fast-1-2'])
            left = [r[0] for r in sqlite3.connect(db).execute('SELECT volume_id FROM upscale_queue ORDER BY volume_id')]
            self.assertEqual(left, [4, 9])         # le tome en erreur sort de la file


class ThresholdConfigTest(unittest.TestCase):
    def test_max_side_validated(self):
        from flask import Flask
        with tempfile.TemporaryDirectory() as tmp:
            app = Flask(__name__)
            app.config.update(DATA_DIR=tmp, DATABASE=os.path.join(tmp, 'b.db'))
            with app.app_context():
                self.assertEqual(upscale.low_def_max_side(), 1600)
                config = upscale.load_config()
                config['max_side'] = 2000
                upscale.save_config(config)
                self.assertEqual(upscale.low_def_max_side(), 2000)
                self.assertTrue(upscale.needs_upscale(1400, 1900))
                self.assertFalse(upscale.needs_upscale(1400, 2100))
                config['max_side'] = 1234
                upscale.save_config(config)
                self.assertEqual(upscale.low_def_max_side(), 1600)


if __name__ == '__main__':
    unittest.main()
