import ipaddress
import logging
import socket
from typing import Optional
from urllib.parse import urljoin, urlsplit

import httpx

from ..core.config import get_config

logger = logging.getLogger(__name__)


class UnsafeRedirectError(ValueError):
    """Raised when a redirect resolves to a disallowed network target."""


def _validate_redirect_target(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or parts.username or parts.password or not parts.hostname:
        raise UnsafeRedirectError("Redirect target must be an unauthenticated http(s) URL.")
    host = parts.hostname.lower().rstrip(".")
    if host in {"localhost", "metadata", "metadata.google.internal", "metadata.goog"}:
        raise UnsafeRedirectError("Redirect target host is not allowed.")
    try:
        addresses = socket.getaddrinfo(
            host,
            parts.port or (443 if parts.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise UnsafeRedirectError("Redirect target could not be resolved.") from exc
    for _, _, _, _, address in addresses:
        ip = ipaddress.ip_address(address[0])
        if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            raise UnsafeRedirectError("Redirect target resolves to a blocked address.")


class SafeAsyncClient(httpx.AsyncClient):
    """Async client that validates every redirect before following it."""

    async def request(self, method, url, *args, **kwargs):
        follow_redirects = kwargs.pop("follow_redirects", True)
        current_url = url
        current_kwargs = dict(kwargs)
        for _ in range(6):
            response = await super().request(method, current_url, *args, follow_redirects=False, **current_kwargs)
            if not follow_redirects or response.status_code not in {301, 302, 303, 307, 308}:
                return response
            location = response.headers.get("location")
            if not location:
                return response
            next_url = urljoin(str(current_url), location)
            _validate_redirect_target(next_url)
            if urlsplit(str(current_url)).netloc != urlsplit(next_url).netloc:
                headers = current_kwargs.get("headers")
                if headers:
                    headers = httpx.Headers(headers)
                    headers.pop("authorization", None)
                    current_kwargs["headers"] = headers
                current_kwargs.pop("auth", None)
            current_url = next_url
            method = (
                "GET"
                if response.status_code == 303 or (response.status_code in {301, 302} and method.upper() == "POST")
                else method
            )
        raise UnsafeRedirectError("Too many redirects.")


def get_async_http_client(
    connect_timeout: Optional[float] = None,
    read_timeout: Optional[float] = None,
    verify: bool = True,
) -> httpx.AsyncClient:
    """
    Returns a configured httpx.AsyncClient with:
    - Default timeouts (connect and read).
    - Standard User-Agent header.
    """
    cfg = get_config()
    # Determine timeouts
    c_timeout = connect_timeout if connect_timeout is not None else cfg.DEFAULT_TIMEOUT_CONNECT
    r_timeout = read_timeout if read_timeout is not None else cfg.DEFAULT_TIMEOUT_READ

    timeout = httpx.Timeout(r_timeout, connect=c_timeout)

    headers = {"User-Agent": cfg.USER_AGENT}

    # Note: httpx does not have built-in retry logic like requests' HTTPAdapter.
    # If retries are needed, they should be implemented at the call site or using a library like 'tenacity'.

    return SafeAsyncClient(
        timeout=timeout,
        headers=headers,
        verify=verify,
        follow_redirects=False,
    )
