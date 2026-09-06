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


def is_safe_external_url(url):
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        return False
    try:
        addrinfos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        return False
    for _, _, _, _, sockaddr in addrinfos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return False
    return True


def safe_external_get(url, *, session=None, timeout=30, max_bytes=None, max_redirects=5,
                      verify=True, headers=None, params=None):
    """GET an untrusted URL while validating every redirect and limiting its size."""
    client = session or requests
    current_url = url

    for redirect_count in range(max_redirects + 1):
        if not is_safe_external_url(current_url):
            raise ValueError('URL target is not a public HTTP(S) address')

        response = client.get(
            current_url, timeout=timeout, verify=verify, headers=headers,
            params=params if redirect_count == 0 else None,
            allow_redirects=False, stream=True,
        )
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

            content = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                content.extend(chunk)
                if len(content) > max_bytes:
                    response.close()
                    raise ValueError('Response is too large')
            response._content = bytes(content)
            response._content_consumed = True

        return response

    raise ValueError('Too many redirects')
