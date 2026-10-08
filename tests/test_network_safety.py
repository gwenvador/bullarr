import socket
import unittest
from unittest import mock

import network_safety


def addresses(*ips):
    return [(socket.AF_INET6 if ':' in ip else socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 80)) for ip in ips]


class PublicTargetTest(unittest.TestCase):
    """Garde-fou anti-SSRF: seules les adresses publiques sont atteignables."""

    def resolve(self, url, *ips):
        with mock.patch.object(network_safety.socket, 'getaddrinfo', return_value=addresses(*ips)):
            return network_safety._public_target(url)

    def test_a_public_address_is_accepted_and_returned_for_pinning(self):
        parsed, address = self.resolve('https://www.example.test/file.torrent', '93.184.216.34')
        self.assertEqual((parsed.hostname, address), ('www.example.test', '93.184.216.34'))

    def test_private_loopback_link_local_and_metadata_addresses_are_refused(self):
        for ip in ('10.0.0.5', '192.168.1.10', '172.16.0.1', '127.0.0.1', '169.254.169.254', '0.0.0.0',
                   '100.64.1.1', '::1', 'fc00::1', 'fe80::1'):
            with self.assertRaises(ValueError, msg=ip):
                self.resolve('http://internal.example.test/', ip)

    def test_a_single_private_answer_among_public_ones_refuses_the_target(self):
        with self.assertRaises(ValueError):
            self.resolve('http://rebind.example.test/', '93.184.216.34', '10.0.0.1')

    def test_unusable_urls_are_refused_before_any_dns_lookup(self):
        with mock.patch.object(network_safety.socket, 'getaddrinfo') as lookup:
            for url in ('file:///etc/passwd', 'ftp://example.test/x', 'gopher://example.test', 'http:///nohost',
                        'http://user:secret@example.test/', 'not a url', ''):
                with self.assertRaises(ValueError, msg=url):
                    network_safety._public_target(url)
            lookup.assert_not_called()

    def test_dns_failure_and_empty_answer_are_refused(self):
        with mock.patch.object(network_safety.socket, 'getaddrinfo', side_effect=socket.gaierror('nodename')):
            with self.assertRaises(ValueError):
                network_safety._public_target('http://does-not-resolve.test/')
        with mock.patch.object(network_safety.socket, 'getaddrinfo', return_value=[]):
            with self.assertRaises(ValueError):
                network_safety._public_target('http://empty-answer.test/')


class FakeResponse:
    def __init__(self, redirect_to=None, headers=None, chunks=()):
        self.is_redirect = redirect_to is not None
        self.is_permanent_redirect = False
        self.headers = dict(headers or {})
        if redirect_to:
            self.headers['Location'] = redirect_to
        self._chunks = list(chunks)
        self.closed = False
        self.close = self._close
        self._content = b''

    def _close(self):
        self.closed = True

    @property
    def content(self):
        return self._content

    def iter_content(self, chunk_size=None):
        return iter(self._chunks)


class SafeExternalGetTest(unittest.TestCase):
    def setUp(self):
        def public_only(url):
            parsed = network_safety.urlparse(url)
            if parsed.hostname in ('169.254.169.254', 'localhost', '127.0.0.1'):
                raise ValueError('URL target is not a public HTTP(S) address')
            return parsed, '93.184.216.34'

        patcher = mock.patch.object(network_safety, '_public_target', side_effect=public_only)
        self.public_target = patcher.start()
        self.addCleanup(patcher.stop)
        session_patcher = mock.patch.object(network_safety.requests, 'Session')
        self.session_class = session_patcher.start()
        self.addCleanup(session_patcher.stop)
        self.client = self.session_class.return_value

    def test_a_redirect_to_an_internal_address_is_blocked_before_connecting(self):
        self.client.get.return_value = FakeResponse(redirect_to='http://169.254.169.254/latest/meta-data')
        with self.assertRaises(ValueError):
            network_safety.safe_external_get('http://public.example.test/start')
        self.assertEqual(self.client.get.call_count, 1)            # l'adresse interne n'est jamais contactée

    def test_relative_redirects_are_followed_after_validation(self):
        self.client.get.side_effect = [FakeResponse(redirect_to='/final'), FakeResponse(chunks=[b'ok'])]
        response = network_safety.safe_external_get('http://public.example.test/start', max_bytes=100)
        self.assertEqual(self.client.get.call_args_list[1][0][0], 'http://public.example.test/final')
        self.assertEqual(response.content, b'ok')

    def test_redirect_loops_and_missing_location_are_errors(self):
        self.client.get.side_effect = lambda *args, **kwargs: FakeResponse(redirect_to='/again')
        with self.assertRaisesRegex(ValueError, 'Too many redirects'):
            network_safety.safe_external_get('http://public.example.test/loop', max_redirects=2)
        self.assertEqual(self.client.get.call_count, 3)
        redirect_without_target = FakeResponse(redirect_to='x')
        del redirect_without_target.headers['Location']
        self.client.get.reset_mock()
        self.client.get.side_effect = None
        self.client.get.return_value = redirect_without_target
        with self.assertRaisesRegex(ValueError, 'no Location'):
            network_safety.safe_external_get('http://public.example.test/bad')

    def test_size_limits_apply_to_the_declared_length_and_to_the_streamed_body(self):
        self.client.get.side_effect = lambda *args, **kwargs: FakeResponse(headers={'Content-Length': '1000'}, chunks=[b'x'])
        with self.assertRaisesRegex(ValueError, 'too large'):
            network_safety.safe_external_get('http://public.example.test/big', max_bytes=100)
        self.client.get.side_effect = lambda *args, **kwargs: FakeResponse(chunks=[b'x' * 60, b'x' * 60])
        with self.assertRaisesRegex(ValueError, 'too large'):
            network_safety.safe_external_get('http://public.example.test/lying-server', max_bytes=100)

    def test_environment_proxies_are_ignored_so_the_pinned_destination_cannot_be_bypassed(self):
        self.client.get.side_effect = lambda *args, **kwargs: FakeResponse(chunks=[b'x'])
        network_safety.safe_external_get('http://public.example.test/', max_bytes=10)
        self.assertFalse(self.client.trust_env)
        self.assertEqual(self.client.get.call_args[1]['allow_redirects'], False)


if __name__ == '__main__':
    unittest.main()
