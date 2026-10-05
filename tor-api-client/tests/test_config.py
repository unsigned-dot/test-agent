from __future__ import annotations

import pytest

from tor_api_client.config import TorConfig, is_loopback_host
from tor_api_client.exceptions import ConfigurationError


def test_defaults():
    config = TorConfig.from_env({})
    assert config.socks_host == "127.0.0.1"
    assert config.socks_port == 9050
    assert config.control_host == "127.0.0.1"
    assert config.control_port == 9051
    assert config.control_password is None
    assert config.request_timeout == 15
    assert config.connect_timeout == 15
    assert config.max_retries == 3
    assert config.newnym_wait == 10
    assert config.rotate_on_5xx is False
    assert config.retry_non_idempotent is False


def test_env_overrides():
    config = TorConfig.from_env(
        {
            "TOR_SOCKS_PORT": "9150",
            "TOR_CONTROL_PORT": "9151",
            "REQUEST_TIMEOUT": "20",
            "CONNECT_TIMEOUT": "5",
            "MAX_RETRIES": "5",
            "TOR_NEWNYM_WAIT": "3.5",
            "ROTATE_ON_5XX": "yes",
            "ALLOWED_HOSTS": "API.example.com, other.org ,",
            "LOG_LEVEL": "debug",
        }
    )
    assert config.socks_port == 9150
    assert config.control_port == 9151
    assert config.timeout == (5.0, 20.0)
    assert config.max_retries == 5
    assert config.newnym_wait == 3.5
    assert config.rotate_on_5xx is True
    assert config.allowed_hosts == ("api.example.com", "other.org")
    assert config.log_level == "DEBUG"


def test_empty_values_fall_back_to_defaults():
    config = TorConfig.from_env({"CONNECT_TIMEOUT": "", "REQUEST_TIMEOUT": "12", "TOR_CONTROL_PASSWORD": " "})
    assert config.connect_timeout == 12
    assert config.control_password is None


def test_proxy_uses_socks5h():
    config = TorConfig.from_env({})
    assert config.proxy_url == "socks5h://127.0.0.1:9050"
    assert config.proxies == {
        "http": "socks5h://127.0.0.1:9050",
        "https": "socks5h://127.0.0.1:9050",
    }


def test_ipv6_proxy_url():
    config = TorConfig.from_env({"TOR_SOCKS_HOST": "::1"})
    assert config.proxy_url == "socks5h://[::1]:9050"


def test_password_never_in_repr():
    config = TorConfig.from_env({"TOR_CONTROL_PASSWORD": "s3cr3t-value"})
    assert config.control_password == "s3cr3t-value"
    assert "s3cr3t-value" not in repr(config)


@pytest.mark.parametrize(
    "env",
    [
        {"TOR_CONTROL_HOST": "0.0.0.0"},
        {"TOR_CONTROL_HOST": "192.168.1.10"},
        {"TOR_SOCKS_HOST": "tor.example.com"},
        {"TOR_SOCKS_PORT": "70000"},
        {"TOR_SOCKS_PORT": "9051"},
        {"MAX_RETRIES": "-1"},
        {"MAX_RETRIES": "1000"},
        {"MAX_RETRIES": "trois"},
        {"REQUEST_TIMEOUT": "0"},
        {"REQUEST_TIMEOUT": "abc"},
        {"TOR_NEWNYM_WAIT": "-5"},
        {"ROTATE_ON_5XX": "peut-être"},
        {"TOR_CHECK_URL": "ftp://example.com"},
    ],
)
def test_invalid_configuration(env):
    with pytest.raises(ConfigurationError):
        TorConfig.from_env(env)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("127.0.0.2", True),
        ("::1", True),
        ("[::1]", True),
        ("localhost", True),
        ("0.0.0.0", False),
        ("10.0.0.1", False),
        ("example.com", False),
    ],
)
def test_is_loopback_host(host, expected):
    assert is_loopback_host(host) is expected


def test_dotenv_file_is_loaded(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("MAX_RETRIES=7\nTOR_NEWNYM_WAIT=4\n")
    # setenv puis delenv : monkeypatch supprimera la variable posée par le .env.
    monkeypatch.setenv("MAX_RETRIES", "0")
    monkeypatch.delenv("MAX_RETRIES")
    monkeypatch.setenv("TOR_NEWNYM_WAIT", "6")  # l'environnement reste prioritaire
    config = TorConfig.from_env(dotenv_path=env_file)
    assert config.max_retries == 7
    assert config.newnym_wait == 6
