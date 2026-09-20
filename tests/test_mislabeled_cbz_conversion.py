import importlib.util, os, tempfile, tarfile, zipfile, unittest
spec=importlib.util.spec_from_file_location('archive_converter','blueprints/library/archive_converter.py')
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

class MislabeledCbzConversionTests(unittest.TestCase):
    def test_reports_tar_and_converts_without_deleting_original(self):
        with tempfile.TemporaryDirectory() as d:
            source=os.path.join(d,'album.cbz')
            page=os.path.join(d,'page.jpg')
            with open(page,'wb') as fh: fh.write(b'jpg')
            with tarfile.open(source,'w') as archive: archive.add(page, arcname='page.jpg')
            self.assertEqual(mod.classify_archive(source), 'tar')
            output=mod.convert_mislabeled_archive_to_cbz(source)
            self.assertTrue(os.path.exists(source))
            self.assertTrue(os.path.exists(output))
            with zipfile.ZipFile(output) as z: self.assertEqual(z.namelist(), ['page.jpg'])

    def test_converts_in_place_without_leaving_converted_sibling(self):
        with tempfile.TemporaryDirectory() as d:
            source=os.path.join(d,'album.cbz')
            page=os.path.join(d,'page.jpg')
            with open(page,'wb') as fh: fh.write(b'jpg')
            with tarfile.open(source,'w') as archive: archive.add(page, arcname='page.jpg')
            mod.convert_mislabeled_archive_in_place(source)
            with open(source,'rb') as fh: self.assertEqual(fh.read(4), b'PK\x03\x04')
            self.assertFalse(os.path.exists(os.path.join(d,'album.converted.cbz')))

    def test_rejects_unsafe_tar_member(self):
        with tempfile.TemporaryDirectory() as d:
            source=os.path.join(d,'album.cbz')
            page=os.path.join(d,'page.jpg')
            with open(page,'wb') as fh: fh.write(b'jpg')
            with tarfile.open(source,'w') as archive: archive.add(page, arcname='../page.jpg')
            with self.assertRaises(ValueError): mod.convert_mislabeled_archive_to_cbz(source)

    def test_zip_is_already_valid(self):
        with tempfile.TemporaryDirectory() as d:
            source=os.path.join(d,'album.cbz')
            with zipfile.ZipFile(source,'w') as z: z.writestr('page.jpg',b'jpg')
            self.assertEqual(mod.classify_archive(source), 'zip')

if __name__ == '__main__': unittest.main()
