"""
Garde-fou anti-SSRF partagé: rejette une URL qui résout vers une adresse privée/interne.

Utilisé partout où l'app va chercher une ressource (fichier .torrent, image de
couverture...) à une URL qui n'est pas entièrement contrôlée par l'admin - un lien fourni
par le client, ou scrapé/relayé depuis un forum externe (EBDZ) - pour empêcher le
conteneur de sonder des services internes (host.docker.internal, autres conteneurs,
169.254.169.254...).
"""
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter


def _public_target(url):
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('URL target is not a public HTTP(S) address')
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        addrinfos = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
        addresses = [info[4][0] for info in addrinfos]
        if not addresses:
            raise ValueError('URL target has no address')
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global:
                raise ValueError('URL target is not a public HTTP(S) address')
        return parsed, addresses[0]
    except (ValueError, socket.gaierror) as exc:
        raise ValueError('URL target is not a public HTTP(S) address') from exc


class _PinnedAddressAdapter(HTTPAdapter):
    """Connect to the validated IP while retaining the URL host for TLS and Host."""

    def __init__(self, address, hostname):
        self.address = address
        self.hostname = hostname
        super().__init__()

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        # Requests 2.33+ uses this path when selecting a connection pool.
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(request, verify, cert)
        host_params['host'] = self.address
        if urlparse(request.url).scheme == 'https':
            pool_kwargs['assert_hostname'] = self.hostname
            pool_kwargs['server_hostname'] = self.hostname
        return host_params, pool_kwargs

    def get_connection(self, url, proxies=None):
        # Compatibility with Requests versions before get_connection_with_tls_context.
        parsed = urlparse(url)
        pool_kwargs = {}
        if parsed.scheme == 'https':
            pool_kwargs = {'assert_hostname': self.hostname, 'server_hostname': self.hostname}
        return self.poolmanager.connection_from_host(
            self.address, port=parsed.port or (443 if parsed.scheme == 'https' else 80),
            scheme=parsed.scheme, pool_kwargs=pool_kwargs,
        )


def safe_external_get(url, *, session=None, timeout=30, max_bytes=None, max_redirects=5,
                      verify=True, headers=None, params=None):
    """GET an untrusted URL while validating every redirect and limiting its size."""
    current_url = url

    for redirect_count in range(max_redirects + 1):
        parsed, address = _public_target(current_url)
        client = requests.Session()
        client.trust_env = False  # Environment proxies bypass the pinned destination.
        if session is not None:
            client.headers.update(session.headers)
            client.cookies = session.cookies
            client.auth = session.auth
        client.mount(parsed.scheme + '://', _PinnedAddressAdapter(address, parsed.hostname))
        request_headers = dict(headers or {})
        request_headers['Host'] = parsed.netloc
        try:
            response = client.get(
                current_url, timeout=timeout, verify=verify, headers=request_headers,
                params=params if redirect_count == 0 else None,
                allow_redirects=False, stream=True,
            )
        except Exception:
            client.close()
            raise
        original_close = response.close

        def close_response():
            try:
                original_close()
            finally:
                client.close()

        response.close = close_response
        if response.is_redirect or response.is_permanent_redirect:
            if redirect_count == max_redirects:
                response.close()
                raise ValueError('Too many redirects')
            location = response.headers.get('Location')
            response.close()
            if not location:
                raise ValueError('Redirect response has no Location header')
            current_url = urljoin(current_url, location)
            continue

        if max_bytes is not None:
            length = response.headers.get('Content-Length')
            if length is not None:
                try:
                    too_large = int(length) > max_bytes
                except (TypeError, ValueError):
                    too_large = False
                if too_large:
                    response.close()
                    raise ValueError('Response is too large')

            try:
                content = bytearray()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    content.extend(chunk)
                    if len(content) > max_bytes:
                        raise ValueError('Response is too large')
                response._content = bytes(content)
                response._content_consumed = True
            finally:
                response.close()

        return response

    raise ValueError('Too many redirects')
