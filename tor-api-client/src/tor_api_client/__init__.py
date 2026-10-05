"""Client API Python passant par Tor, avec renouvellement de circuit (NEWNYM)."""

import logging

from .client import TorApiClient, validate_url
from .config import TorConfig
from .exceptions import (
    ConfigurationError,
    ConnectionFailedError,
    DNSResolutionError,
    HTTPStatusError,
    InvalidURLError,
    NetworkError,
    RateLimitedError,
    RequestTimeoutError,
    RetriesExhaustedError,
    TLSError,
    TorApiClientError,
    TorAuthenticationError,
    TorCheckError,
    TorControlConnectionError,
    TorControlError,
    TorNotReadyError,
    TorProxyError,
    TorProxyUnavailableError,
)
from .logging_config import sanitize_url, setup_logging
from .tor import (
    check_socks_port,
    check_tor_connection,
    get_tor_ip,
    is_tor_ready,
    rotate_tor_circuit,
    wait_for_tor_ready,
)

__version__ = "1.0.0"

# Une bibliothèque ne configure pas le logging : l'application s'en charge.
logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "ConfigurationError",
    "ConnectionFailedError",
    "DNSResolutionError",
    "HTTPStatusError",
    "InvalidURLError",
    "NetworkError",
    "RateLimitedError",
    "RequestTimeoutError",
    "RetriesExhaustedError",
    "TLSError",
    "TorApiClient",
    "TorApiClientError",
    "TorAuthenticationError",
    "TorCheckError",
    "TorConfig",
    "TorControlConnectionError",
    "TorControlError",
    "TorNotReadyError",
    "TorProxyError",
    "TorProxyUnavailableError",
    "__version__",
    "check_socks_port",
    "check_tor_connection",
    "get_tor_ip",
    "is_tor_ready",
    "rotate_tor_circuit",
    "sanitize_url",
    "setup_logging",
    "validate_url",
    "wait_for_tor_ready",
]
