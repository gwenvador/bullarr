import json
import os
import stat
import tempfile
import unittest
from unittest import mock

from cryptography.fernet import Fernet

import encryption


class EncryptionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.key_file = os.path.join(self.tmp.name, '.encryption_key')
        with open(self.key_file, 'wb') as handle:
            handle.write(Fernet.generate_key())
        patcher = mock.patch.object(encryption, 'ENCRYPTION_KEY_FILE', self.key_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_round_trip_and_ciphertext_hides_the_secret(self):
        token = encryption.encrypt('mot-de-passe-secret')
        self.assertNotIn('mot-de-passe-secret', token)
        self.assertEqual(encryption.decrypt(token), 'mot-de-passe-secret')
        self.assertNotEqual(token, encryption.encrypt('mot-de-passe-secret'))   # nonce différent à chaque appel

    def test_empty_values_are_not_encrypted(self):
        self.assertIsNone(encryption.encrypt(''))
        self.assertIsNone(encryption.encrypt(None))
        self.assertIsNone(encryption.decrypt(''))
        self.assertIsNone(encryption.decrypt(None))

    def test_a_token_from_another_key_or_garbage_decrypts_to_none(self):
        foreign = Fernet(Fernet.generate_key()).encrypt(b'x').decode()
        self.assertIsNone(encryption.decrypt(foreign))
        self.assertIsNone(encryption.decrypt('pas-un-jeton'))

    def test_missing_key_file_is_reported(self):
        os.remove(self.key_file)
        with self.assertRaises(FileNotFoundError):
            encryption.load_encryption_key()
        self.assertIsNone(encryption.decrypt('quelque-chose'))   # dégradation silencieuse, sans exception

    def test_config_is_saved_encrypted_with_private_permissions_and_loaded_decrypted(self):
        config_file = os.path.join(self.tmp.name, 'client.json')
        config = {'host': 'localhost', 'password': 'secret', 'enabled': True}
        self.assertTrue(encryption.save_encrypted_json_config(config_file, config))
        raw = open(config_file, encoding='utf-8').read()
        self.assertNotIn('secret', raw)
        self.assertEqual(stat.S_IMODE(os.stat(config_file).st_mode), 0o600)
        loaded = encryption.load_encrypted_json_config(config_file, {})
        self.assertEqual((loaded['host'], loaded['password_decrypted']), ('localhost', 'secret'))
        self.assertNotEqual(loaded['password'], 'secret')

    def test_decrypted_field_replaces_the_stored_secret_and_is_never_written(self):
        config_file = os.path.join(self.tmp.name, 'komga.json')
        encryption.save_encrypted_json_config(config_file, {'api_key': 'old', 'api_key_decrypted': 'nouvelle-cle'},
                                              secret_field='api_key')
        stored = json.load(open(config_file, encoding='utf-8'))
        self.assertNotIn('api_key_decrypted', stored)
        loaded = encryption.load_encrypted_json_config(config_file, {}, secret_field='api_key')
        self.assertEqual(loaded['api_key_decrypted'], 'nouvelle-cle')

    def test_missing_config_file_gives_a_copy_of_the_defaults(self):
        defaults = {'enabled': False, 'password': ''}
        loaded = encryption.load_encrypted_json_config(os.path.join(self.tmp.name, 'absent.json'), defaults)
        self.assertEqual(loaded, defaults)
        self.assertIsNot(loaded, defaults)
        self.assertNotIn('password_decrypted', loaded)

    def test_saving_to_an_unwritable_path_reports_failure(self):
        self.assertFalse(encryption.save_encrypted_json_config(
            os.path.join(self.tmp.name, 'missing-dir', 'x.json'), {'password': 'x'}, client_label='Test'))


if __name__ == '__main__':
    unittest.main()
