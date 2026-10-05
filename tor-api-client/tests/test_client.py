from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import MagicMock

import pytest
import requests
from urllib3.exceptions import MaxRetryError, NewConnectionError, ProtocolError

from tor_api_client.client import (
    TorApiClient,
    classify_request_exception,
    parse_retry_after,
    validate_url,
)
from tor_api_client.config import TorConfig
from tor_api_client.exceptions import (
    ConfigurationError,
    ConnectionFailedError,
    DNSResolutionError,
    InvalidURLError,
    RateLimitedError,
    RequestTimeoutError,
    RetriesExhaustedError,
    TLSError,
    TorControlConnectionError,
    TorNotReadyError,
    TorProxyError,
    TorProxyUnavailableError,
)
from tor_api_client.logging_config import sanitize_url

from .helpers import make_response

URL = "https://api.example.com/v1/items"


# --------------------------------------------------------------------------- #
# Outils
# --------------------------------------------------------------------------- #
def socks_error(message: str) -> requests.ConnectionError:
    """Reproduit la forme exacte d'une erreur SOCKS remontée par requests/urllib3."""
    reason = NewConnectionError(None, f"Failed to establish a new connection: {message}")
    return requests.ConnectionError(MaxRetryError(None, "/v1/items?token=SECRET", reason))


def make_session(*results):
    session = MagicMock(spec=requests.Session)
    session.adapters = {"https://": MagicMock(), "http://": MagicMock()}
    session.proxies = {
        "http": "socks5h://127.0.0.1:9050",
        "https": "socks5h://127.0.0.1:9050",
    }
    session.trust_env = True
    session.request.side_effect = list(results)
    return session


def make_client(config: TorConfig, *results, rotate=None, ready=None):
    session = make_session(*results)
    rotate = rotate or MagicMock()
    ready = ready or MagicMock()
    sleep = MagicMock()
    client = TorApiClient(
        config, session=session, rotate_circuit=rotate, wait_until_ready=ready, sleep=sleep
    )
    return client, session, rotate, ready, sleep


# --------------------------------------------------------------------------- #
# Création du client / proxy
# --------------------------------------------------------------------------- #
def test_default_client_session_is_configured_for_tor(config):
    with TorApiClient(config) as client:
        assert client.session.trust_env is False
        assert client.session.proxies["https"] == "socks5h://127.0.0.1:9050"
        assert client.session.proxies["http"] == "socks5h://127.0.0.1:9050"


def test_request_goes_through_socks5h_with_timeout(config):
    client, session, rotate, _, _ = make_client(config, make_response(200))
    response = client.get(URL, params={"q": "1"})

    assert response.status_code == 200
    args, kwargs = session.request.call_args
    assert args == ("GET", URL)
    assert kwargs["proxies"] == {
        "http": "socks5h://127.0.0.1:9050",
        "https": "socks5h://127.0.0.1:9050",
    }
    assert kwargs["timeout"] == (15.0, 15.0)
    assert kwargs["params"] == {"q": "1"}
    rotate.assert_not_called()


def test_provided_session_is_forced_to_ignore_env_proxies(config):
    session = requests.Session()
    session.proxies.update(config.proxies)  # socks5h, requis par le garde anti-fuite
    assert session.trust_env is True
    TorApiClient(config, session=session)
    assert session.trust_env is False


def test_post_forwards_json(config):
    client, session, _, _, _ = make_client(config, make_response(201))
    response = client.post(URL, json={"hello": "world"})
    assert response.status_code == 201
    args, kwargs = session.request.call_args
    assert args[0] == "POST"
    assert kwargs["json"] == {"hello": "world"}


def test_custom_timeout_is_kept(config):
    client, session, _, _, _ = make_client(config, make_response(200))
    client.get(URL, timeout=3)
    assert session.request.call_args.kwargs["timeout"] == 3


def test_proxies_argument_is_forbidden(config):
    client, session, _, _, _ = make_client(config)
    with pytest.raises(TypeError):
        client.get(URL, proxies={"https": "http://evil:8080"})
    session.request.assert_not_called()


def test_context_manager_closes_only_owned_session(config):
    client, session, _, _, _ = make_client(config)
    with client:
        pass
    session.close.assert_not_called()

    owned = TorApiClient(config)
    owned.session = MagicMock()
    with owned:
        pass
    owned.session.close.assert_called_once()


# --------------------------------------------------------------------------- #
# Erreurs HTTP
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_client_errors_are_returned_without_retry_or_rotation(config, status):
    client, session, rotate, _, sleep = make_client(config, make_response(status))
    response = client.get(URL)
    assert response.status_code == status
    assert session.request.call_count == 1
    rotate.assert_not_called()
    sleep.assert_not_called()


def test_429_respects_retry_after_without_rotation(config):
    client, session, rotate, _, sleep = make_client(
        config, make_response(429, headers={"Retry-After": "7"}), make_response(200)
    )
    assert client.get(URL).status_code == 200
    sleep.assert_called_once_with(7.0)
    rotate.assert_not_called()
    assert session.request.call_count == 2


def test_429_without_retry_after_uses_backoff(config):
    client, _, rotate, _, sleep = make_client(config, make_response(429), make_response(200))
    client.get(URL)
    (delay,), _ = sleep.call_args
    assert 1.0 <= delay <= 1.5
    rotate.assert_not_called()


def test_429_retry_after_too_long_gives_up(config):
    client, session, rotate, _, sleep = make_client(
        config, make_response(429, headers={"Retry-After": "3600"})
    )
    with pytest.raises(RateLimitedError) as info:
        client.get(URL)
    assert info.value.retry_after == 3600
    assert info.value.status_code == 429
    assert session.request.call_count == 1
    rotate.assert_not_called()
    sleep.assert_not_called()


def test_429_is_retried_for_post(config):
    client, session, rotate, _, _ = make_client(config, make_response(429), make_response(201))
    assert client.post(URL, json={}).status_code == 201
    assert session.request.call_count == 2
    rotate.assert_not_called()


def test_persistent_429_exhausts_retries_without_rotation(config):
    responses = [make_response(429, headers={"Retry-After": "1"}) for _ in range(4)]
    client, session, rotate, _, sleep = make_client(config, *responses)
    with pytest.raises(RetriesExhaustedError) as info:
        client.get(URL)
    assert info.value.attempts == 4
    assert info.value.last_response.status_code == 429
    assert session.request.call_count == 4
    assert sleep.call_count == 3
    rotate.assert_not_called()


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_retried_with_backoff_without_rotation(config, status):
    client, session, rotate, _, sleep = make_client(config, make_response(status), make_response(200))
    assert client.get(URL).status_code == 200
    assert session.request.call_count == 2
    sleep.assert_called_once()
    rotate.assert_not_called()


def test_503_uses_retry_after(config):
    client, _, _, _, sleep = make_client(
        config, make_response(503, headers={"Retry-After": "4"}), make_response(200)
    )
    client.get(URL)
    sleep.assert_called_once_with(4.0)


def test_501_is_not_retried(config):
    client, session, _, _, _ = make_client(config, make_response(501))
    assert client.get(URL).status_code == 501
    assert session.request.call_count == 1


def test_5xx_exhausted(config):
    client, session, _, _, _ = make_client(config, *[make_response(502) for _ in range(4)])
    with pytest.raises(RetriesExhaustedError) as info:
        client.get(URL)
    assert info.value.last_response.status_code == 502
    assert session.request.call_count == 4


def test_5xx_on_post_is_not_replayed(config):
    client, session, _, _, sleep = make_client(config, make_response(500))
    assert client.post(URL, json={}).status_code == 500
    assert session.request.call_count == 1
    sleep.assert_not_called()


def test_rotate_on_5xx_option():
    config = TorConfig.from_env({"ROTATE_ON_5XX": "true"})
    client, _, rotate, ready, _ = make_client(config, make_response(502), make_response(200))
    client.get(URL)
    rotate.assert_called_once_with(config)
    ready.assert_called_once_with(config)


def test_rotate_on_5xx_still_honours_retry_after():
    config = TorConfig.from_env({"ROTATE_ON_5XX": "true"})
    client, _, rotate, _, sleep = make_client(
        config, make_response(503, headers={"Retry-After": "5"}), make_response(200)
    )
    client.get(URL)
    rotate.assert_not_called()
    sleep.assert_called_once_with(5.0)


# --------------------------------------------------------------------------- #
# Erreurs réseau / retry / NEWNYM
# --------------------------------------------------------------------------- #
def test_timeout_triggers_newnym_then_retry(config):
    client, session, rotate, ready, sleep = make_client(
        config, requests.exceptions.ReadTimeout("read timed out"), make_response(200)
    )
    assert client.get(URL).status_code == 200
    rotate.assert_called_once_with(config)
    ready.assert_called_once_with(config)
    sleep.assert_not_called()  # l'attente est faite par rotate/ready
    for adapter in session.adapters.values():
        adapter.close.assert_called()  # connexions de l'ancien circuit fermées
    assert session.request.call_count == 2


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("0x01: General SOCKS server failure", TorProxyError),
        ("0x02: Connection not allowed by ruleset", TorProxyError),
        ("0x06: TTL expired", TorProxyError),
        ("0x04: Host unreachable", DNSResolutionError),
        ("0xf0: Unknown error", DNSResolutionError),
        ("Connection closed unexpectedly", TorProxyError),
    ],
)
def test_socks_errors_are_classified_and_rotate(config, message, expected):
    error = classify_request_exception(socks_error(message), URL)
    assert type(error) is expected
    assert error.rotate_circuit is True
    assert error.request_may_have_been_sent is False

    client, _, rotate, _, _ = make_client(config, socks_error(message), make_response(200))
    client.get(URL)
    rotate.assert_called_once()


def test_unreachable_socks_port_is_detected_without_rotation():
    """Erreur réelle : aucun Tor n'écoute sur le port 1."""
    config = TorConfig.from_env({"TOR_SOCKS_PORT": "1", "TOR_CONTROL_PORT": "2", "MAX_RETRIES": "1"})
    rotate, sleep = MagicMock(), MagicMock()
    client = TorApiClient(config, rotate_circuit=rotate, wait_until_ready=MagicMock(), sleep=sleep)
    with pytest.raises(RetriesExhaustedError) as info:
        client.get(URL)
    assert isinstance(info.value.last_error, TorProxyUnavailableError)
    rotate.assert_not_called()
    sleep.assert_called_once()


def test_local_dns_failure_is_classified():
    exc = requests.ConnectionError("NameResolutionError: Failed to resolve 'x'")
    assert isinstance(classify_request_exception(exc, URL), DNSResolutionError)


def test_connection_reset_may_have_been_sent():
    exc = requests.ConnectionError(ProtocolError("Connection aborted.", ConnectionResetError(104, "reset")))
    error = classify_request_exception(exc, URL)
    assert isinstance(error, ConnectionFailedError)
    assert error.request_may_have_been_sent is True


def test_chunked_encoding_error_is_connection_failure():
    error = classify_request_exception(requests.exceptions.ChunkedEncodingError("broken"), URL)
    assert isinstance(error, ConnectionFailedError)


def test_unrelated_request_exception_is_not_network_error():
    assert classify_request_exception(requests.exceptions.TooManyRedirects("loop"), URL) is None


def test_tls_error_is_not_retried(config):
    client, session, rotate, _, _ = make_client(config, requests.exceptions.SSLError("bad cert"))
    with pytest.raises(TLSError):
        client.get(URL)
    assert session.request.call_count == 1
    rotate.assert_not_called()


def test_connect_timeout_on_post_is_retried(config):
    client, session, rotate, _, _ = make_client(
        config, requests.exceptions.ConnectTimeout("connect timed out"), make_response(201)
    )
    assert client.post(URL, json={"a": 1}).status_code == 201
    assert session.request.call_count == 2
    rotate.assert_called_once()


def test_read_timeout_on_post_is_not_replayed_by_default(config):
    client, session, rotate, _, _ = make_client(config, requests.exceptions.ReadTimeout("slow"))
    with pytest.raises(RequestTimeoutError):
        client.post(URL, json={"a": 1})
    assert session.request.call_count == 1
    rotate.assert_not_called()


def test_read_timeout_on_post_replayed_when_allowed(config):
    client, session, _, _, _ = make_client(
        config, requests.exceptions.ReadTimeout("slow"), make_response(201)
    )
    assert client.post(URL, json={"a": 1}, allow_unsafe_retry=True).status_code == 201
    assert session.request.call_count == 2


def test_max_retries_bounds_rotations(config):
    errors = [requests.exceptions.ReadTimeout("slow") for _ in range(10)]
    client, session, rotate, ready, _ = make_client(config, *errors)
    with pytest.raises(RetriesExhaustedError) as info:
        client.get(URL)
    assert session.request.call_count == 4  # 1 essai + MAX_RETRIES=3
    assert rotate.call_count == 3  # jamais de NEWNYM après la dernière tentative
    assert ready.call_count == 3
    assert info.value.attempts == 4
    assert isinstance(info.value.last_error, RequestTimeoutError)


def test_max_retries_zero():
    config = TorConfig.from_env({"MAX_RETRIES": "0"})
    client, session, rotate, _, _ = make_client(config, requests.exceptions.ReadTimeout("slow"))
    with pytest.raises(RetriesExhaustedError):
        client.get(URL)
    assert session.request.call_count == 1
    rotate.assert_not_called()


@pytest.mark.parametrize(
    "failure", [TorControlConnectionError("controlport down"), TorNotReadyError("not ready")]
)
def test_rotation_failure_falls_back_to_backoff(config, failure):
    rotate = MagicMock(side_effect=failure) if isinstance(failure, TorControlConnectionError) else MagicMock()
    ready = MagicMock(side_effect=failure) if isinstance(failure, TorNotReadyError) else MagicMock()
    client, session, _, _, sleep = make_client(
        config, requests.exceptions.ReadTimeout("slow"), make_response(200), rotate=rotate, ready=ready
    )
    assert client.get(URL).status_code == 200
    sleep.assert_called_once()
    assert session.request.call_count == 2


def test_missing_pysocks_is_a_configuration_error(config):
    client, _, _, _, _ = make_client(
        config, requests.exceptions.InvalidSchema("Missing dependencies for SOCKS support.")
    )
    with pytest.raises(ConfigurationError, match="requests\\[socks\\]"):
        client.get(URL)


def test_logs_do_not_leak_query_string_or_credentials(config, caplog):
    caplog.set_level(logging.DEBUG)
    url = "https://user:hunter2@api.example.com/v1?token=SECRET"
    client, _, _, _, _ = make_client(config, socks_error("0x04: Host unreachable"), make_response(200))
    client.get(url)
    assert "SECRET" not in caplog.text
    assert "hunter2" not in caplog.text
    assert "api.example.com/v1" in caplog.text


# --------------------------------------------------------------------------- #
# Validation d'URL et utilitaires
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://example.com/file",
        "file:///etc/passwd",
        "example.com/no-scheme",
        "https:///no-host",
        "https://exa mple.com/",
        "https://example.com/\n",
        "https://example.com:99999/",
        "http://localhost:8080/",
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://10.0.0.5/",
        "https://192.168.1.1/",
        "https://169.254.169.254/latest/meta-data",
    ],
)
def test_invalid_urls_are_rejected(config, url):
    client, session, _, _, _ = make_client(config)
    with pytest.raises(InvalidURLError):
        client.get(url)
    session.request.assert_not_called()


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://example.com/path?x=1",
        "https://93.184.215.14/",
        "http://duckduckgogg42xjoc72x3sjasowoarfbgcmvfimaftt6twagswzczad.onion/",
    ],
)
def test_valid_urls(url):
    validate_url(url)


def test_allowed_hosts():
    allowed = ("api.example.com",)
    validate_url("https://api.example.com/x", allowed)
    validate_url("https://eu.api.example.com/x", allowed)
    for url in ("https://evil.com/", "https://notapi.example.com/", "https://api.example.com.evil.com/"):
        with pytest.raises(InvalidURLError):
            validate_url(url, allowed)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://user:pass@api.example.com:8443/v1/x?token=abc#frag", "https://api.example.com:8443/v1/x?<redacted>"),
        ("https://api.example.com/v1", "https://api.example.com/v1"),
        ("http://[2001:db8::1]/a", "http://[2001:db8::1]/a"),
    ],
)
def test_sanitize_url(url, expected):
    assert sanitize_url(url) == expected


def test_parse_retry_after():
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert parse_retry_after("120") == 120.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    assert parse_retry_after("bientôt") is None
    assert parse_retry_after(format_datetime(now + timedelta(seconds=30), usegmt=True), now=now) == 30.0
    assert parse_retry_after(format_datetime(now - timedelta(seconds=30), usegmt=True), now=now) == 0.0


# --------------------------------------------------------------------------- #
# Confidentialité : anti-fuite DNS et fail-closed
# --------------------------------------------------------------------------- #
def test_client_refuses_session_without_socks5h(config):
    session = requests.Session()
    session.proxies.update({"http": "socks5://127.0.0.1:9050", "https": "socks5://127.0.0.1:9050"})
    with pytest.raises(ConfigurationError, match="socks5h"):
        TorApiClient(config, session=session)


def test_require_tor_blocks_requests_when_unconfirmed():
    config = TorConfig.from_env({"REQUIRE_TOR": "true"})
    client, session, _, _, _ = make_client(config, make_response(200))
    # check_tor_connection utilise session.get ; on simule un service non confirmé.
    session.get = MagicMock(return_value=make_response(200, b'{"IsTor": false}'))
    with pytest.raises(TorProxyUnavailableError):
        client.get(URL)
    session.request.assert_not_called()  # aucune requête applicative émise


def test_require_tor_allows_requests_once_confirmed():
    config = TorConfig.from_env({"REQUIRE_TOR": "true"})
    client, session, _, _, _ = make_client(config, make_response(200), make_response(200))
    session.get = MagicMock(return_value=make_response(200, b'{"IsTor": true, "IP": "185.220.101.1"}'))
    assert client.get(URL).status_code == 200
    session.get.assert_called_once()  # vérification faite une seule fois
    client.get(URL)
    session.get.assert_called_once()
