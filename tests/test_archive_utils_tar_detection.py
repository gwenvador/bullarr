import importlib.util, os, tempfile, tarfile, unittest
spec=importlib.util.spec_from_file_location('archive_utils','archive_utils.py')
mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
class ArchiveUtilsTarTests(unittest.TestCase):
    def test_detects_tar_under_cbz_extension(self):
        with tempfile.TemporaryDirectory() as d:
            p=os.path.join(d,'x.cbz')
            with tarfile.open(p,'w') as a: a.addfile(tarfile.TarInfo('page.jpg'))
            self.assertEqual(mod.detect_actual_format(p,'cbz'),'tar')
if __name__=='__main__': unittest.main()
