import os
import tempfile
import unittest

from blueprints.library.import_worker import download_incomplete_reason


class DownloadIncompleteReasonTest(unittest.TestCase):
    def _file(self, data):
        handle = tempfile.NamedTemporaryFile(delete=False)
        handle.write(data)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_preallocated_file_is_incomplete(self):
        self.assertIsNotNone(download_incomplete_reason(self._file(b'\x00' * 200000)))

    def test_empty_tail_is_incomplete(self):
        self.assertIsNotNone(download_incomplete_reason(self._file(b'Rar!\x1a\x07\x00' + b'x' * 5000 + b'\x00' * 8192)))

    def test_zip_without_end_record_is_incomplete(self):
        self.assertIsNotNone(download_incomplete_reason(self._file(b'PK\x03\x04' + b'x' * 5000)))

    def test_pdf_without_eof_is_incomplete(self):
        self.assertIsNotNone(download_incomplete_reason(self._file(b'%PDF-1.7\n' + b'x' * 5000)))

    def test_complete_files_pass(self):
        self.assertIsNone(download_incomplete_reason(self._file(b'PK\x03\x04' + b'x' * 5000 + b'PK\x05\x06' + b'\x00' * 18)))
        self.assertIsNone(download_incomplete_reason(self._file(b'%PDF-1.7\n' + b'x' * 5000 + b'\n%%EOF\n')))
        self.assertIsNone(download_incomplete_reason(self._file(b'Rar!\x1a\x07\x00' + b'x' * 9000)))


if __name__ == '__main__':
    unittest.main()
