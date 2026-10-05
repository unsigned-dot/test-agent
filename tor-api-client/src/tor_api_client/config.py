"""Chargement et validation de la configuration.

La configuration provient des variables d'environnement, éventuellement
complétées par un fichier ``.env`` (les variables déjà définies dans
l'environnement sont prioritaires). Aucun secret n'est codé en dur.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .exceptions import ConfigurationError

LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})

# Bornes de sécurité : empêchent une configuration aberrante de produire
# des boucles de retry quasi infinies ou des attentes démesurées.
MAX_RETRIES_LIMIT = 10
MAX_WAIT_LIMIT = 600.0


def is_loopback_host(host: str) -> bool:
    """Retourne True si ``host`` désigne la machine locale."""
    host = host.strip().strip("[]").lower()
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class TorConfig:
    """Paramètres du client. Utiliser :meth:`from_env` pour la construire."""

    socks_host: str = "127.0.0.1"
    socks_port: int = 9050
    control_host: str = "127.0.0.1"
    control_port: int = 9051
    # Optionnel : vide => authentification par cookie (recommandé).
    # repr=False : le mot de passe n'apparaît jamais dans un log ou une trace.
    control_password: str | None = field(default=None, repr=False)

    request_timeout: float = 15.0
    connect_timeout: float = 15.0
    max_retries: int = 3

    newnym_wait: float = 10.0
    newnym_min_interval: float = 10.0
    tor_ready_timeout: float = 30.0

    backoff_base: float = 1.0
    backoff_max: float = 30.0
    retry_after_max: float = 120.0

    rotate_on_5xx: bool = False
    retry_non_idempotent: bool = False

    # Confidentialité : si True, le client vérifie que le trafic sort bien par
    # Tor (via tor_check_url) avant d'émettre la première requête, et refuse
    # d'émettre sinon (fail-closed) — évite toute fuite en clair si Tor est mal
    # configuré ou arrêté.
    require_tor: bool = False

    tor_check_url: str = "https://check.torproject.org/api/ip"
    ip_check_url: str = "https://check.torproject.org/api/ip"

    allowed_hosts: tuple[str, ...] = ()
    user_agent: str | None = None
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        self._validate()

    # ------------------------------------------------------------------ #
    @property
    def proxy_url(self) -> str:
        """URL du proxy. ``socks5h`` => la résolution DNS se fait via Tor."""
        host = self.socks_host
        if ":" in host and not host.startswith("["):  # IPv6 littérale
            host = f"[{host}]"
        return f"socks5h://{host}:{self.socks_port}"

    @property
    def proxies(self) -> dict[str, str]:
        """Dictionnaire ``proxies`` attendu par requests."""
        return {"http": self.proxy_url, "https": self.proxy_url}

    @property
    def timeout(self) -> tuple[float, float]:
        """Tuple (connexion, lecture) attendu par requests."""
        return (self.connect_timeout, self.request_timeout)

    # ------------------------------------------------------------------ #
    def _validate(self) -> None:
        for name in ("socks_host", "control_host"):
            value = getattr(self, name)
            if not is_loopback_host(value):
                raise ConfigurationError(
                    f"{name}={value!r} refusé : le proxy SOCKS et le ControlPort "
                    "de Tor doivent être joignables uniquement en local "
                    "(127.0.0.1, ::1 ou localhost)."
                )
        for name in ("socks_port", "control_port"):
            port = getattr(self, name)
            if not 1 <= port <= 65535:
                raise ConfigurationError(f"{name}={port} hors de la plage 1-65535.")
        if self.socks_port == self.control_port:
            raise ConfigurationError("Le port SOCKS et le ControlPort doivent différer.")
        if not 0 <= self.max_retries <= MAX_RETRIES_LIMIT:
            raise ConfigurationError(
                f"max_retries doit être compris entre 0 et {MAX_RETRIES_LIMIT}."
            )
        for name in ("request_timeout", "connect_timeout", "tor_ready_timeout"):
            value = getattr(self, name)
            if not 0 < value <= MAX_WAIT_LIMIT:
                raise ConfigurationError(f"{name} doit être dans ]0, {MAX_WAIT_LIMIT}].")
        for name in (
            "newnym_wait",
            "newnym_min_interval",
            "backoff_base",
            "backoff_max",
            "retry_after_max",
        ):
            value = getattr(self, name)
            if not 0 <= value <= MAX_WAIT_LIMIT:
                raise ConfigurationError(f"{name} doit être dans [0, {MAX_WAIT_LIMIT}].")
        for name in ("tor_check_url", "ip_check_url"):
            value = getattr(self, name)
            if not value.startswith(("https://", "http://")):
                raise ConfigurationError(f"{name} doit être une URL http(s).")

    # ------------------------------------------------------------------ #
    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        dotenv_path: str | Path | None = None,
    ) -> TorConfig:
        """Construit la configuration.

        :param env: mapping explicite (utile pour les tests). Si ``None``,
            ``os.environ`` est utilisé après chargement éventuel du ``.env``.
        :param dotenv_path: chemin d'un fichier ``.env`` ; par défaut, le
            ``.env`` du répertoire courant (ou d'un parent) s'il existe.
        """
        if env is None:
            _load_dotenv(dotenv_path)
            env = os.environ

        reader = _EnvReader(env)
        request_timeout = reader.get_float("REQUEST_TIMEOUT", 15.0)
        return cls(
            socks_host=reader.get_str("TOR_SOCKS_HOST", "127.0.0.1"),
            socks_port=reader.get_int("TOR_SOCKS_PORT", 9050),
            control_host=reader.get_str("TOR_CONTROL_HOST", "127.0.0.1"),
            control_port=reader.get_int("TOR_CONTROL_PORT", 9051),
            control_password=reader.get_str("TOR_CONTROL_PASSWORD", "") or None,
            request_timeout=request_timeout,
            connect_timeout=reader.get_float("CONNECT_TIMEOUT", request_timeout),
            max_retries=reader.get_int("MAX_RETRIES", 3),
            newnym_wait=reader.get_float("TOR_NEWNYM_WAIT", 10.0),
            newnym_min_interval=reader.get_float("TOR_NEWNYM_MIN_INTERVAL", 10.0),
            tor_ready_timeout=reader.get_float("TOR_READY_TIMEOUT", 30.0),
            backoff_base=reader.get_float("BACKOFF_BASE", 1.0),
            backoff_max=reader.get_float("BACKOFF_MAX", 30.0),
            retry_after_max=reader.get_float("RETRY_AFTER_MAX", 120.0),
            rotate_on_5xx=reader.get_bool("ROTATE_ON_5XX", False),
            retry_non_idempotent=reader.get_bool("RETRY_NON_IDEMPOTENT", False),
            require_tor=reader.get_bool("REQUIRE_TOR", False),
            tor_check_url=reader.get_str("TOR_CHECK_URL", "https://check.torproject.org/api/ip"),
            ip_check_url=reader.get_str("IP_CHECK_URL", "https://check.torproject.org/api/ip"),
            allowed_hosts=reader.get_list("ALLOWED_HOSTS"),
            user_agent=reader.get_str("USER_AGENT", "") or None,
            log_level=reader.get_str("LOG_LEVEL", "INFO").upper(),
        )


def _load_dotenv(dotenv_path: str | Path | None) -> None:
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:  # pragma: no cover - dépendance déclarée
        return
    path = dotenv_path or find_dotenv(usecwd=True)
    if path:
        load_dotenv(path, override=False)


class _EnvReader:
    """Lecture typée des variables, avec messages d'erreur explicites."""

    _TRUE = {"1", "true", "yes", "on", "oui"}
    _FALSE = {"0", "false", "no", "off", "non"}

    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = env

    def _raw(self, name: str) -> str | None:
        value = self._env.get(name)
        if value is None:
            return None
        value = value.strip()
        return value or None

    def get_str(self, name: str, default: str) -> str:
        value = self._raw(name)
        return default if value is None else value

    def get_int(self, name: str, default: int) -> int:
        value = self._raw(name)
        if value is None:
            return default
        try:
            return int(value)
        except ValueError:
            raise ConfigurationError(f"{name} doit être un entier (reçu {value!r}).") from None

    def get_float(self, name: str, default: float) -> float:
        value = self._raw(name)
        if value is None:
            return default
        try:
            return float(value)
        except ValueError:
            raise ConfigurationError(f"{name} doit être un nombre (reçu {value!r}).") from None

    def get_bool(self, name: str, default: bool) -> bool:
        value = self._raw(name)
        if value is None:
            return default
        lowered = value.lower()
        if lowered in self._TRUE:
            return True
        if lowered in self._FALSE:
            return False
        raise ConfigurationError(f"{name} doit être un booléen (reçu {value!r}).")

    def get_list(self, name: str) -> tuple[str, ...]:
        value = self._raw(name)
        if value is None:
            return ()
        return tuple(item.strip().lower() for item in value.split(",") if item.strip())
