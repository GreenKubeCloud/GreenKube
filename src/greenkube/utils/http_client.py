import logging

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..core.config import config

logger = logging.getLogger(__name__)


class TimeoutHTTPAdapter(HTTPAdapter):
    """
    Transport adapter that sets a default timeout for all requests.
    """

    def __init__(self, *args, **kwargs):
        self.timeout = kwargs.pop("timeout", (config.DEFAULT_TIMEOUT_CONNECT, config.DEFAULT_TIMEOUT_READ))
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        return super().send(request, **kwargs)


def get_http_session(
    retries: int = 3,
    backoff_factor: float = 0.5,
    status_forcelist: tuple = (500, 502, 503, 504),
    connect_timeout: float = None,
    read_timeout: float = None,
) -> requests.Session:
    """
    Returns a configured requests.Session with:
    - Automatic retries for server errors and connection issues.
    - Default timeouts (connect and read).
    - Standard User-Agent header.
    """
    session = requests.Session()

    # Set User-Agent
    session.headers.update({"User-Agent": config.USER_AGENT})

    # Configure Retries
    retry_strategy = Retry(
        total=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        allowed_methods=["HEAD", "GET", "OPTIONS", "POST", "PUT", "DELETE"],
        raise_on_status=False,  # Raise manually if needed, or let calls handle it
    )

    # Configure Timeouts via Adapter
    # We use our custom TimeoutHTTPAdapter to enforce timeouts on every call
    # unless overridden per-request.

    # Determine timeouts
    c_timeout = connect_timeout if connect_timeout is not None else config.DEFAULT_TIMEOUT_CONNECT
    r_timeout = read_timeout if read_timeout is not None else config.DEFAULT_TIMEOUT_READ
    timeout = (c_timeout, r_timeout)

    adapter = TimeoutHTTPAdapter(timeout=timeout, max_retries=retry_strategy)

    session.mount("https://", adapter)
    session.mount("http://", adapter)

    return session
