import base64
import hashlib
import unittest

from torrent_hash import _bdecode, _bencode, compute_torrent_info_hash, extract_magnet_btih

INFO_RAW = b'd6:lengthi5e4:name3:abc12:piece lengthi16384e6:pieces20:' + b'x' * 20 + b'e'
TORRENT = b'd8:announce8:http://x4:info' + INFO_RAW + b'e'


class MagnetTest(unittest.TestCase):
    def test_hex_hashes_are_normalised_to_lowercase(self):
        digest = 'ABCDEF0123456789ABCDEF0123456789ABCDEF01'
        self.assertEqual(extract_magnet_btih(f'magnet:?xt=urn:btih:{digest}&dn=Titre&tr=http://t'), digest.lower())

    def test_base32_hashes_are_converted_to_hex(self):
        raw = bytes(range(20))
        encoded = base64.b32encode(raw).decode()
        self.assertEqual(len(encoded), 32)
        self.assertEqual(extract_magnet_btih(f'magnet:?xt=urn:btih:{encoded}'), raw.hex())

    def test_anything_else_gives_none(self):
        for value in (None, '', 'https://example.test/file.torrent', 'magnet:?dn=sans-hash',
                      'magnet:?xt=urn:btih:abc123', 'magnet:?xt=urn:btih:' + 'g' * 40):
            self.assertIsNone(extract_magnet_btih(value), value)


class InfoHashTest(unittest.TestCase):
    def test_hash_is_the_sha1_of_the_info_dictionary_bytes(self):
        self.assertEqual(compute_torrent_info_hash(TORRENT), hashlib.sha1(INFO_RAW).hexdigest())

    def test_other_fields_of_the_torrent_do_not_change_the_hash(self):
        other = b'd8:announce16:http://other/zzz7:comment3:hey4:info' + INFO_RAW + b'e'
        self.assertEqual(compute_torrent_info_hash(other), compute_torrent_info_hash(TORRENT))

    def test_invalid_content_gives_none(self):
        self.assertIsNone(compute_torrent_info_hash(b'<html>not a torrent</html>'))
        self.assertIsNone(compute_torrent_info_hash(b'd8:announce8:http://xe'))   # pas de dictionnaire info
        self.assertIsNone(compute_torrent_info_hash(TORRENT[:30]))                 # tronqué
        self.assertIsNone(compute_torrent_info_hash(b''))


class BencodeTest(unittest.TestCase):
    def test_encoding_is_canonical_with_sorted_dictionary_keys(self):
        value = {b'b': [1, b'x', {b'z': 2, b'a': 3}], b'a': b'hello'}
        self.assertEqual(_bencode(value), b'd1:a5:hello1:bli1e1:xd1:ai3e1:zi2eeee')

    def test_decode_then_encode_gives_back_the_original_bytes(self):
        decoded, end = _bdecode(INFO_RAW, 0)
        self.assertEqual(end, len(INFO_RAW))
        self.assertEqual(_bencode(decoded), INFO_RAW)
        self.assertEqual(decoded[b'length'], 5)

    def test_unsupported_types_are_refused(self):
        for value in (1.5, 'texte', None):
            with self.assertRaises(TypeError):
                _bencode(value)


if __name__ == '__main__':
    unittest.main()
