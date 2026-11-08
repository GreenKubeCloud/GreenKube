"""Client address resolution behind trusted proxies (roadmap F14 brute-force budget)."""

from __future__ import annotations

import pytest
from starlette.requests import Request

from greenkube.auth.http import client_ip
from greenkube.auth.settings import AuthSettings


def _request(peer: str, forwarded: str | None = None) -> Request:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded is not None else []
    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers, "client": (peer, 1234)})


SETTINGS = AuthSettings(trusted_proxy_ips="10.0.0.0/8")


@pytest.mark.parametrize(
    ("peer", "forwarded", "expected"),
    [
        # Untrusted peers never get to choose their address.
        ("203.0.113.9", "198.51.100.1", "203.0.113.9"),
        # A trusted proxy reports the client it received the request from.
        ("10.0.0.5", "198.51.100.7", "198.51.100.7"),
        # A spoofed leftmost entry is ignored: the proxy appended the real client.
        ("10.0.0.5", "1.2.3.4, 198.51.100.7", "198.51.100.7"),
        # Chained trusted proxies are skipped.
        ("10.0.0.5", "1.2.3.4, 198.51.100.7, 10.0.0.9", "198.51.100.7"),
        # Only trusted hops: fall back to the leftmost one.
        ("10.0.0.5", "10.0.0.7, 10.0.0.9", "10.0.0.7"),
        ("10.0.0.5", None, "10.0.0.5"),
    ],
)
def test_client_ip_uses_the_rightmost_untrusted_hop(peer, forwarded, expected):
    assert client_ip(_request(peer, forwarded), SETTINGS) == expected
