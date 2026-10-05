from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests
import stem
import stem.connection
from stem import Signal
from stem.connection import AuthMethod

from tor_api_client import tor
from tor_api_client.config import TorConfig
from tor_api_client.exceptions import (
    TorAuthenticationError,
    TorCheckError,
    TorControlConnectionError,
    TorControlError,
    TorNotReadyError,
)

from .helpers import make_response


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _controller(auth_methods=(AuthMethod.COOKIE, AuthMethod.SAFECOOKIE)):
    controller = MagicMock()
    controller.get_info.side_effect = lambda key, default=None: {
        "status/bootstrap-phase": "NOTICE BOOTSTRAP PROGRESS=100 TAG=done SUMMARY=\"Done\"",
        "status/circuit-established": "1",
    }.get(key, default)
    protocolinfo = MagicMock(auth_methods=auth_methods)
    return controller, protocolinfo


@pytest.fixture
def mock_stem():
    controller, protocolinfo = _controller()
    with patch.object(tor.Controller, "from_port", return_value=controller) as from_port, patch.object(
        tor.stem.connection, "get_protocolinfo", return_value=protocolinfo
    ):
        yield from_port, controller, protocolinfo


# --------------------------------------------------------------------------- #
# rotate_tor_circuit
# --------------------------------------------------------------------------- #
def test_rotate_sends_newnym_and_waits(config, mock_stem):
    from_port, controller, protocolinfo = mock_stem
    clock = FakeClock()

    tor.rotate_tor_circuit(config, sleep=clock.sleep, clock=clock)

    from_port.assert_called_once_with(address="127.0.0.1", port=9051)
    controller.authenticate.assert_called_once_with(password=None, protocolinfo_response=protocolinfo)
    controller.signal.assert_called_once_with(Signal.NEWNYM)
    controller.close.assert_called_once()
    assert clock.sleeps == [10.0]


def test_rotate_passes_password_when_configured(mock_stem):
    _, controller, protocolinfo = mock_stem
    config = TorConfig.from_env({"TOR_CONTROL_PASSWORD": "secret", "TOR_NEWNYM_WAIT": "0"})
    tor.rotate_tor_circuit(config, sleep=lambda s: None)
    controller.authenticate.assert_called_once_with(password="secret", protocolinfo_response=protocolinfo)


def test_rotate_respects_min_interval(config, mock_stem):
    _, controller, _ = mock_stem
    clock = FakeClock()
    config = TorConfig.from_env({"TOR_NEWNYM_WAIT": "2", "TOR_NEWNYM_MIN_INTERVAL": "10"})

    tor.rotate_tor_circuit(config, sleep=clock.sleep, clock=clock)
    tor.rotate_tor_circuit(config, sleep=clock.sleep, clock=clock)

    # 2 s d'attente après le 1er NEWNYM, puis 8 s pour atteindre 10 s d'intervalle.
    assert clock.sleeps == [2.0, 8.0, 2.0]
    assert controller.signal.call_count == 2


def test_rotate_controlport_unreachable(config):
    with patch.object(tor.Controller, "from_port", side_effect=stem.SocketError("refused")):
        with pytest.raises(TorControlConnectionError):
            tor.rotate_tor_circuit(config, sleep=lambda s: None)


@pytest.mark.parametrize(
    ("stem_error", "fragment"),
    [
        (stem.connection.UnreadableCookieFile("denied", "/run/tor/control.authcookie", False), "debian-tor"),
        (stem.connection.MissingPassword("needs password"), "TOR_CONTROL_PASSWORD"),
        (stem.connection.IncorrectPassword("bad", None), "refusé"),
        (stem.connection.AuthenticationFailure("other"), "authentification"),
    ],
)
def test_rotate_authentication_errors(config, mock_stem, stem_error, fragment):
    _, controller, _ = mock_stem
    controller.authenticate.side_effect = stem_error
    with pytest.raises(TorAuthenticationError, match=fragment):
        tor.rotate_tor_circuit(config, sleep=lambda s: None)
    controller.signal.assert_not_called()
    controller.close.assert_called()


def test_rotate_refuses_unauthenticated_controlport(config, mock_stem):
    _, controller, protocolinfo = mock_stem
    protocolinfo.auth_methods = (AuthMethod.NONE,)
    with pytest.raises(TorAuthenticationError, match="aucune authentification"):
        tor.rotate_tor_circuit(config, sleep=lambda s: None)
    controller.authenticate.assert_not_called()
    controller.signal.assert_not_called()


def test_rotate_signal_rejected(config, mock_stem):
    _, controller, _ = mock_stem
    controller.signal.side_effect = stem.InvalidRequest("552", "Unrecognized signal")
    with pytest.raises(TorControlError):
        tor.rotate_tor_circuit(config, sleep=lambda s: None)
    controller.close.assert_called_once()


def test_failed_rotation_does_not_update_rate_limit(config, mock_stem):
    _, controller, _ = mock_stem
    controller.signal.side_effect = stem.SocketError("closed")
    clock = FakeClock()
    with pytest.raises(TorControlConnectionError):
        tor.rotate_tor_circuit(config, sleep=clock.sleep, clock=clock)
    controller.signal.side_effect = None
    tor.rotate_tor_circuit(config, sleep=clock.sleep, clock=clock)
    assert clock.sleeps == [10.0]  # pas d'attente d'intervalle minimal


# --------------------------------------------------------------------------- #
# Disponibilité de Tor
# --------------------------------------------------------------------------- #
def test_is_tor_ready_true(config, mock_stem):
    assert tor.is_tor_ready(config) is True


def test_is_tor_ready_false_during_bootstrap(config, mock_stem):
    _, controller, _ = mock_stem
    controller.get_info.side_effect = lambda key, default=None: {
        "status/bootstrap-phase": "NOTICE BOOTSTRAP PROGRESS=50 TAG=loading_descriptors",
        "status/circuit-established": "0",
    }[key]
    assert tor.is_tor_ready(config) is False


def test_wait_for_tor_ready_polls_until_ready(config):
    clock = FakeClock()
    with patch.object(tor, "is_tor_ready", side_effect=[False, TorControlConnectionError("x"), True]) as ready:
        tor.wait_for_tor_ready(config, timeout=10, sleep=clock.sleep, clock=clock)
    assert ready.call_count == 3
    assert clock.sleeps == [1.0, 1.0]


def test_wait_for_tor_ready_times_out(config):
    clock = FakeClock()
    with patch.object(tor, "is_tor_ready", return_value=False):
        with pytest.raises(TorNotReadyError):
            tor.wait_for_tor_ready(config, timeout=3, sleep=clock.sleep, clock=clock)
    assert sum(clock.sleeps) == 3


def test_wait_for_tor_ready_stops_on_auth_error(config):
    with patch.object(tor, "is_tor_ready", side_effect=TorAuthenticationError("cookie")):
        with pytest.raises(TorAuthenticationError):
            tor.wait_for_tor_ready(config, timeout=30, sleep=lambda s: None)


# --------------------------------------------------------------------------- #
# Vérifications via SOCKS
# --------------------------------------------------------------------------- #
def test_create_tor_session_ignores_environment_proxies(config):
    session = tor.create_tor_session(config)
    assert session.trust_env is False
    assert session.proxies["https"] == "socks5h://127.0.0.1:9050"


def _session_returning(response):
    session = MagicMock()
    session.get.return_value = response
    return session


def test_check_tor_connection_true(config):
    session = _session_returning(make_response(200, b'{"IsTor": true, "IP": "185.220.101.1"}'))
    assert tor.check_tor_connection(config, session=session) is True
    _, kwargs = session.get.call_args
    assert kwargs["proxies"] == config.proxies
    assert kwargs["timeout"] == config.timeout


def test_check_tor_connection_false(config):
    session = _session_returning(make_response(200, b'{"IsTor": false, "IP": "203.0.113.5"}'))
    assert tor.check_tor_connection(config, session=session) is False


@pytest.mark.parametrize("body", [b"not json", b'{"ip": "1.2.3.4"}', b"[1, 2]"])
def test_check_tor_connection_unconfirmed(config, body):
    session = _session_returning(make_response(200, body))
    assert tor.check_tor_connection(config, session=session) is False


def test_check_tor_connection_network_error(config):
    session = MagicMock()
    session.get.side_effect = requests.ConnectionError("down")
    assert tor.check_tor_connection(config, session=session) is False


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b'{"IsTor": true, "IP": "185.220.101.1"}', "185.220.101.1"),
        (b'{"ip": "2001:db8::1"}', "2001:db8::1"),
        (b'{"origin": "185.220.101.2, 185.220.101.3"}', "185.220.101.2"),
        (b"185.220.101.4\n", "185.220.101.4"),
    ],
)
def test_get_tor_ip_formats(config, body, expected):
    session = _session_returning(make_response(200, body))
    assert tor.get_tor_ip(config, session=session) == expected


def test_get_tor_ip_uses_configurable_url():
    config = TorConfig.from_env({"IP_CHECK_URL": "https://ip.example.org/json"})
    session = _session_returning(make_response(200, b'{"ip": "185.220.101.9"}'))
    tor.get_tor_ip(config, session=session)
    assert session.get.call_args.args[0] == "https://ip.example.org/json"


@pytest.mark.parametrize("response", [make_response(200, b"<html>nope</html>"), make_response(503, b"")])
def test_get_tor_ip_errors(config, response):
    with pytest.raises(TorCheckError):
        tor.get_tor_ip(config, session=_session_returning(response))


def test_check_socks_port_closed():
    config = TorConfig.from_env({"TOR_SOCKS_PORT": "1", "TOR_CONTROL_PORT": "2"})
    assert tor.check_socks_port(config, timeout=0.5) is False
