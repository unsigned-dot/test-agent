"""Client HTTP dont tout le trafic passe par Tor, avec retry et NEWNYM.

Politique de retry (résumé) :

=========================  =========  ==================  =========================
Situation                  Retry ?    Nouveau circuit ?   Attente
=========================  =========  ==================  =========================
Timeout                    oui        oui                 NEWNYM + Tor prêt
Erreur SOCKS / circuit     oui        oui                 NEWNYM + Tor prêt
Hôte injoignable / DNS     oui        oui                 NEWNYM + Tor prêt
Connexion interrompue      oui        oui                 NEWNYM + Tor prêt
Port SOCKS injoignable     oui        non (Tor arrêté)    backoff exponentiel
Erreur TLS / certificat    non        non                 -
HTTP 429                   oui        NON                 Retry-After ou backoff
HTTP 500/502/503/504       oui        non (option)        Retry-After ou backoff
Autres 4xx (400, 401...)   non        non                 réponse renvoyée telle quelle
=========================  =========  ==================  =========================

Les méthodes non idempotentes (POST, PATCH) ne sont rejouées que si la requête
n'a certainement pas atteint le serveur (échec de connexion), sauf
autorisation explicite (``allow_unsafe_retry=True`` ou RETRY_NON_IDEMPOTENT).
"""

from __future__ import annotations

import ipaddress
import logging
import random
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import requests

from .config import TorConfig, is_loopback_host
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
    TorControlError,
    TorProxyError,
    TorProxyUnavailableError,
)
from .logging_config import sanitize_url
from .tor import create_tor_session, rotate_tor_circuit, wait_for_tor_ready

logger = logging.getLogger(__name__)

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE", "TRACE"})
RETRYABLE_STATUS_CODES = frozenset({500, 502, 503, 504})

SOCKS5_ERRORS = {
    0x01: "défaillance générale du serveur SOCKS",
    0x02: "connexion refusée par la politique de sortie",
    0x03: "réseau injoignable",
    0x04: "hôte injoignable ou nom non résolu par le nœud de sortie",
    0x05: "connexion refusée par l'hôte distant",
    0x06: "TTL expiré (circuit trop lent)",
    0x07: "commande SOCKS non supportée",
    0x08: "type d'adresse non supporté",
}
# 0x04 : avec socks5h, c'est ainsi que Tor signale un échec de résolution DNS.
# 0xF0-0xF7 : erreurs étendues de Tor pour les services .onion.
_DNS_LIKE_SOCKS_CODES = frozenset({0x04, *range(0xF0, 0xF8)})
_SOCKS_CODE_RE = re.compile(r"\b0x([0-9a-f]{2}):")
_DNS_MESSAGES = (
    "name or service not known",
    "temporary failure in name resolution",
    "nodename nor servname",
    "nameresolutionerror",
    "failed to resolve",
)


# --------------------------------------------------------------------------- #
# Validation d'URL
# --------------------------------------------------------------------------- #
def validate_url(url: str, allowed_hosts: tuple[str, ...] = ()) -> None:
    """Validation raisonnable d'une URL avant envoi.

    :raises InvalidURLError: URL vide, schéma non http(s), hôte absent ou
        local/privé (injoignable via Tor), hôte hors liste blanche...
    """
    if not isinstance(url, str) or not url:
        raise InvalidURLError("URL vide ou invalide.")
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        raise InvalidURLError("L'URL contient des espaces ou caractères de contrôle.")
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - lève ValueError si le port est invalide
    except ValueError as exc:
        raise InvalidURLError(f"URL mal formée : {exc}") from None

    if parts.scheme.lower() not in ("http", "https"):
        raise InvalidURLError("Seuls les schémas http et https sont acceptés.")
    host = (parts.hostname or "").lower()
    if not host:
        raise InvalidURLError("L'URL ne contient pas d'hôte.")

    if is_loopback_host(host):
        raise InvalidURLError("Les adresses locales ne sont pas joignables via Tor.")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and not ip.is_global:
        raise InvalidURLError("Les adresses IP privées ou réservées ne sont pas joignables via Tor.")

    if allowed_hosts and not any(
        host == allowed or host.endswith("." + allowed) for allowed in allowed_hosts
    ):
        raise InvalidURLError(f"Hôte {host!r} absent de ALLOWED_HOSTS.")

    if parts.scheme.lower() == "http" and not host.endswith(".onion"):
        logger.warning(
            "HTTP en clair vers %s : le nœud de sortie Tor peut lire et modifier le trafic",
            host,
        )


# --------------------------------------------------------------------------- #
# Classification des erreurs
# --------------------------------------------------------------------------- #
def _exception_chain(exc: BaseException) -> list[BaseException]:
    """Toutes les exceptions liées (cause, contexte, ``reason`` urllib3, args)."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    stack: list[Any] = [exc]
    while stack:
        current = stack.pop()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        stack.extend(current.args)
        stack.extend(
            [current.__cause__, current.__context__, getattr(current, "reason", None)]
        )
    return chain


def classify_request_exception(exc: requests.RequestException, safe_url: str) -> NetworkError | None:
    """Traduit une exception requests en :class:`NetworkError` typée.

    Retourne None si l'exception n'est pas une erreur réseau (URL invalide,
    trop de redirections...). Les messages produits ne contiennent jamais
    l'URL complète (query string, identifiants).
    """
    chain = _exception_chain(exc)
    text = " | ".join(f"{type(e).__name__}: {e}" for e in chain).lower()

    if isinstance(exc, requests.exceptions.SSLError):
        return TLSError(
            "Erreur TLS/certificat : la requête n'est pas rejouée "
            "(certificat invalide ou interception possible).",
            url=safe_url,
        )
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return RequestTimeoutError("Délai de connexion dépassé.", url=safe_url)
    if isinstance(exc, requests.exceptions.Timeout):
        return RequestTimeoutError(
            "Délai de lecture dépassé.", url=safe_url, request_may_have_been_sent=True
        )
    if not isinstance(
        exc, (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError)
    ):
        return None

    match = _SOCKS_CODE_RE.search(text)
    if match:
        code = int(match.group(1), 16)
        meaning = SOCKS5_ERRORS.get(code, "erreur étendue de Tor")
        message = f"Erreur SOCKS {code:#04x} renvoyée par Tor : {meaning}."
        if code in _DNS_LIKE_SOCKS_CODES:
            return DNSResolutionError(message, url=safe_url)
        return TorProxyError(message, url=safe_url)

    if any(marker in text for marker in _DNS_MESSAGES):
        return DNSResolutionError("Résolution DNS impossible.", url=safe_url)

    if "failed to establish a new connection" in text:
        if "[errno" in text:
            # Erreur système sur la seule connexion TCP locale : celle vers Tor.
            return TorProxyUnavailableError(
                "Port SOCKS de Tor injoignable : Tor est-il démarré ?", url=safe_url
            )
        return TorProxyError("Négociation SOCKS avec Tor interrompue.", url=safe_url)

    if "socks" in text:
        return TorProxyError("Erreur du proxy SOCKS de Tor.", url=safe_url)

    root = type(chain[-1]).__name__ if chain else type(exc).__name__
    return ConnectionFailedError(
        f"Connexion interrompue ({root}).", url=safe_url, request_may_have_been_sent=True
    )


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Convertit un en-tête Retry-After (secondes ou date HTTP) en secondes."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max(0.0, (when - now).total_seconds())


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class TorApiClient:
    """Client HTTP(S) passant par Tor, avec retry borné et rotation de circuit.

    Exemple ::

        with TorApiClient() as client:
            response = client.get("https://example.com")
            response = client.post("https://example.com/api", json={"hello": "world"})

    Une instance n'est pas destinée à être partagée entre threads : créez un
    client par thread (la limitation des NEWNYM, elle, est globale).
    """

    def __init__(
        self,
        config: TorConfig | None = None,
        *,
        session: requests.Session | None = None,
        rotate_circuit: Callable[[TorConfig], None] | None = None,
        wait_until_ready: Callable[[TorConfig], None] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config or TorConfig.from_env()
        self._owns_session = session is None
        self.session = session if session is not None else create_tor_session(self.config)
        # Interdit aux variables HTTP(S)_PROXY / NO_PROXY de contourner Tor.
        self.session.trust_env = False
        self._rotate_circuit = rotate_circuit or rotate_tor_circuit
        self._wait_until_ready = wait_until_ready or wait_for_tor_ready
        self._sleep = sleep or time.sleep

    # ------------------------------------------------------------------ #
    def __enter__(self) -> TorApiClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_session:
            self.session.close()

    # Raccourcis ---------------------------------------------------------
    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("HEAD", url, **kwargs)

    def options(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("OPTIONS", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("PUT", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("DELETE", url, **kwargs)

    # ------------------------------------------------------------------ #
    def request(
        self,
        method: str,
        url: str,
        *,
        allow_unsafe_retry: bool | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        """Envoie une requête via Tor en appliquant la politique de retry.

        Les arguments supplémentaires sont transmis à ``requests.Session.request``
        (``params``, ``json``, ``data``, ``headers``, ``timeout``...).
        L'argument ``proxies`` est interdit : tout passe par Tor.

        :param allow_unsafe_retry: autorise à rejouer une méthode non
            idempotente même si la requête a pu atteindre le serveur.
        :returns: la réponse (succès ou erreur HTTP non retentable, ex. 404).
        :raises InvalidURLError: URL refusée.
        :raises NetworkError: erreur réseau non retentable (ex. TLS).
        :raises RateLimitedError: 429 avec Retry-After supérieur au maximum.
        :raises RetriesExhaustedError: toutes les tentatives ont échoué.
        """
        method = method.upper()
        validate_url(url, self.config.allowed_hosts)
        if "proxies" in kwargs:
            raise TypeError("L'argument 'proxies' est interdit : tout le trafic passe par Tor.")
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self.config.timeout

        safe_url = sanitize_url(url)
        if allow_unsafe_retry is None:
            allow_unsafe_retry = self.config.retry_non_idempotent
        replay_allowed = method in IDEMPOTENT_METHODS or allow_unsafe_retry
        max_attempts = self.config.max_retries + 1
        last_error: BaseException | None = None
        last_response: requests.Response | None = None

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                logger.info("Nouvelle tentative %d/%d : %s %s", attempt, max_attempts, method, safe_url)
            else:
                logger.info("Envoi de la requête %s %s", method, safe_url)

            try:
                response = self.session.request(
                    method, url, proxies=self.config.proxies, **kwargs
                )
            except requests.exceptions.InvalidSchema as exc:
                if "socks" in str(exc).lower():
                    raise ConfigurationError(
                        "Support SOCKS absent : installez 'requests[socks]' (PySocks)."
                    ) from exc
                raise InvalidURLError("Schéma d'URL non supporté.") from exc
            except requests.RequestException as exc:
                error = classify_request_exception(exc, safe_url)
                if error is None:
                    raise TorApiClientError(
                        f"Requête {method} {safe_url} invalide ({type(exc).__name__})."
                    ) from exc
                last_error, last_response = error, None
                logger.warning(
                    "Erreur %s sur %s %s : %s", type(error).__name__, method, safe_url, error
                )
                if not error.retryable:
                    raise error from exc
                if error.request_may_have_been_sent and not replay_allowed:
                    logger.warning(
                        "%s non idempotent et requête potentiellement reçue : pas de nouvelle "
                        "tentative (utiliser allow_unsafe_retry=True pour forcer)",
                        method,
                    )
                    raise error from exc
                if attempt < max_attempts:
                    self._prepare_retry(attempt, rotate=error.rotate_circuit)
                continue

            status = response.status_code
            if status != 429 and status not in RETRYABLE_STATUS_CODES:
                if status >= 400:
                    logger.info(
                        "HTTP %d sur %s %s : erreur applicative, ni retry ni changement de circuit",
                        status,
                        method,
                        safe_url,
                    )
                else:
                    logger.info("Succès : HTTP %d sur %s %s", status, method, safe_url)
                return response

            last_error, last_response = None, response
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            if retry_after is not None and retry_after > self.config.retry_after_max:
                logger.error(
                    "HTTP %d avec Retry-After=%.0f s (> %.0f s autorisés) : abandon",
                    status,
                    retry_after,
                    self.config.retry_after_max,
                )
                if status == 429:
                    raise RateLimitedError(
                        f"Limite de débit atteinte sur {safe_url} (Retry-After {retry_after:.0f} s).",
                        response=response,
                        retry_after=retry_after,
                    )
                raise HTTPStatusError(
                    f"HTTP {status} sur {safe_url} (Retry-After {retry_after:.0f} s).",
                    response=response,
                )

            if status == 429:
                # Limite de débit : on ralentit, on ne change PAS de circuit.
                logger.warning("HTTP 429 sur %s %s : limite de débit du service", method, safe_url)
                rotate = False
            else:
                logger.warning("HTTP %d sur %s %s : erreur serveur", status, method, safe_url)
                if not replay_allowed:
                    logger.warning("%s non idempotent : réponse renvoyée sans nouvelle tentative", method)
                    return response
                # Un Retry-After explicite est respecté plutôt que de changer de circuit.
                rotate = self.config.rotate_on_5xx and retry_after is None

            if attempt < max_attempts:
                response.close()
                self._prepare_retry(attempt, rotate=rotate, delay=retry_after)

        logger.error(
            "Échec définitif de %s %s après %d tentative(s)", method, safe_url, max_attempts
        )
        reason = (
            f"HTTP {last_response.status_code}"
            if last_response is not None
            else type(last_error).__name__
        )
        raise RetriesExhaustedError(
            f"{method} {safe_url} : échec après {max_attempts} tentative(s) ({reason}).",
            attempts=max_attempts,
            last_error=last_error,
            last_response=last_response,
        ) from last_error

    # ------------------------------------------------------------------ #
    def _backoff_delay(self, retry_number: int) -> float:
        """Backoff exponentiel plafonné, avec un peu de gigue."""
        base = self.config.backoff_base
        delay = min(self.config.backoff_max, base * (2 ** (retry_number - 1)))
        jitter = random.uniform(0, base / 2) if base > 0 else 0.0
        return min(self.config.backoff_max, delay + jitter)

    def _reset_connections(self) -> None:
        """Ferme les connexions persistantes : elles restent sur l'ancien circuit."""
        for adapter in self.session.adapters.values():
            adapter.close()

    def _prepare_retry(self, retry_number: int, *, rotate: bool, delay: float | None = None) -> None:
        max_retries = self.config.max_retries
        if rotate:
            logger.warning(
                "Retry n°%d/%d : demande d'un nouveau circuit Tor", retry_number, max_retries
            )
            try:
                self._rotate_circuit(self.config)
                self._reset_connections()
                logger.info("Attente de la disponibilité de Tor")
                self._wait_until_ready(self.config)
                return
            except TorControlError as exc:
                self._reset_connections()
                logger.error(
                    "Changement de circuit impossible (%s) : repli sur une simple attente", exc
                )

        wait = delay if delay is not None else self._backoff_delay(retry_number)
        logger.warning("Retry n°%d/%d dans %.1f s", retry_number, max_retries, wait)
        self._sleep(wait)
