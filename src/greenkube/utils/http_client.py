import logging

import httpx

from ..core.config import config

logger = logging.getLogger(__name__)


def get_async_http_client(
    connect_timeout: float = None,
    read_timeout: float = None,
    verify: bool = True,
) -> httpx.AsyncClient:
    """
    Returns a configured httpx.AsyncClient with:
    - Default timeouts (connect and read).
    - Standard User-Agent header.
    """
    # Determine timeouts
    c_timeout = connect_timeout if connect_timeout is not None else config.DEFAULT_TIMEOUT_CONNECT
    r_timeout = read_timeout if read_timeout is not None else config.DEFAULT_TIMEOUT_READ

    timeout = httpx.Timeout(r_timeout, connect=c_timeout)

    headers = {"User-Agent": config.USER_AGENT}

    # Note: httpx does not have built-in retry logic like requests' HTTPAdapter.
    # If retries are needed, they should be implemented at the call site or using a library like 'tenacity'.

    return httpx.AsyncClient(
        timeout=timeout,
        headers=headers,
        verify=verify,
        follow_redirects=True,
    )
