import os
import tempfile
import unittest

from blueprints.library.routes import add_missing_download_extensions

SUPPORTED = {'.cbz', '.cbr', '.pdf'}


class ExtensionlessDownloadTest(unittest.TestCase):
    def test_tracked_pdf_without_extension_gets_pdf(self):
        with tempfile.TemporaryDirectory() as root:
            name = 'Chagrin.Rodolphe.Griffo.2026.FR.[PDF]-NOTAG'
            with open(os.path.join(root, name), 'wb') as handle:
                handle.write(b'%PDF-1.6 ...')
            renamed = add_missing_download_extensions([root], [{'client_item_name': name}], SUPPORTED)
            self.assertEqual(renamed, [os.path.join(root, name + '.pdf')])
            self.assertTrue(os.path.isfile(os.path.join(root, name + '.pdf')))

    def test_untracked_or_unknown_content_is_left_alone(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, 'Autre-NOTAG'), 'wb') as handle:
                handle.write(b'%PDF-1.6')
            with open(os.path.join(root, 'Suivi-NOTAG'), 'wb') as handle:
                handle.write(b'plain text')
            renamed = add_missing_download_extensions(
                [root], [{'client_item_name': 'Suivi-NOTAG'}], SUPPORTED)
            self.assertEqual(renamed, [])
            self.assertEqual(sorted(os.listdir(root)), ['Autre-NOTAG', 'Suivi-NOTAG'])

    def test_zip_content_becomes_cbz(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, 'Album-TEAM'), 'wb') as handle:
                handle.write(b'PK\x03\x04rest')
            renamed = add_missing_download_extensions([root], [{'title': 'Album-TEAM'}], SUPPORTED)
            self.assertEqual(renamed, [os.path.join(root, 'Album-TEAM.cbz')])


if __name__ == '__main__':
    unittest.main()
