"""url_safety.py — SSRF guard run before every network call.

Moved out of fetch_and_clean.py so wayback.py and fetch_playwright.py can
import it without a circular import. fetch_and_clean re-exports validate_url.
"""

import socket
from ipaddress import ip_address
from urllib.parse import urljoin, urlparse

import requests

_BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain"}
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


class UnsafeURLError(ValueError):
    """The URL uses a non-http(s) scheme or targets a private/internal address."""


def _is_internal(addr) -> bool:
    return addr.is_private or addr.is_loopback or addr.is_reserved or addr.is_link_local


def validate_url(url: str) -> None:
    """Raise UnsafeURLError for non-http(s) URLs or private/reserved targets."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError(f"Blocked URL scheme '{parsed.scheme}': {url}")
    hostname = parsed.hostname or ""
    if hostname in _BLOCKED_HOSTNAMES:
        raise UnsafeURLError(f"Blocked hostname '{hostname}': {url}")
    try:
        addr = ip_address(hostname)
    except ValueError:
        addr = None
    if addr is not None:
        if _is_internal(addr):
            raise UnsafeURLError(f"Blocked private/reserved IP '{hostname}': {url}")
        return
    try:
        resolved = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return  # let the actual fetch handle DNS failures
    for *_, sockaddr in resolved:
        addr = ip_address(sockaddr[0])
        if _is_internal(addr):
            raise UnsafeURLError(f"Blocked: '{hostname}' resolves to private IP {addr}: {url}")


def get_validated(url: str, *, timeout, max_redirects: int = 5):
    """requests.get that follows redirects by hand so every hop passes validate_url.

    Raises UnsafeURLError for an unsafe hop and RuntimeError after max_redirects.
    """
    for _ in range(max_redirects + 1):
        validate_url(url)
        response = requests.get(url, timeout=timeout, allow_redirects=False)
        location = response.headers.get("Location")
        if response.status_code not in _REDIRECT_CODES or not location:
            return response
        url = urljoin(url, location)
    raise RuntimeError(f"too many redirects: {url}")
