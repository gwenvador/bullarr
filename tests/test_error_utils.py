import logging
import unittest

from error_utils import error_message


class ErrorMessageTest(unittest.TestCase):
    def test_returns_the_real_message_for_the_interface(self):
        with self.assertLogs('bullarr.errors', level=logging.WARNING):
            self.assertEqual(error_message(ValueError('Chemin invalide')), 'Chemin invalide')

    def test_prefix_is_prepended(self):
        with self.assertLogs('bullarr.errors', level=logging.WARNING):
            self.assertEqual(error_message(ValueError('hors racine'), 'Chemin de série invalide'),
                             'Chemin de série invalide: hors racine')

    def test_empty_message_falls_back_to_the_exception_type(self):
        with self.assertLogs('bullarr.errors', level=logging.ERROR):
            self.assertEqual(error_message(RuntimeError()), 'RuntimeError')

    def test_unexpected_errors_are_logged_with_their_traceback(self):
        try:
            raise RuntimeError('database is locked')
        except RuntimeError as exc:
            with self.assertLogs('bullarr.errors', level=logging.ERROR) as logs:
                message = error_message(exc)
        self.assertEqual(message, 'database is locked')
        self.assertIsNotNone(logs.records[0].exc_info)

    def test_expected_validation_errors_are_logged_without_a_traceback(self):
        with self.assertLogs('bullarr.errors', level=logging.WARNING) as logs:
            error_message(ValueError('bad input'))
        self.assertIsNone(logs.records[0].exc_info)


if __name__ == '__main__':
    unittest.main()
