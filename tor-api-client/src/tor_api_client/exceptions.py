"""Hiérarchie d'exceptions du client.

Toutes les exceptions publiques héritent de :class:`TorApiClientError`, ce qui
permet à l'appelant de tout intercepter avec un seul ``except`` s'il le souhaite.

Les erreurs réseau (:class:`NetworkError` et sous-classes) portent trois
attributs utilisés par la logique de retry :

* ``retryable`` : l'erreur peut-elle être résolue en retentant ?
* ``rotate_circuit`` : un nouveau circuit Tor (NEWNYM) a-t-il une chance d'aider ?
* ``request_may_have_been_sent`` : la requête a-t-elle pu atteindre le serveur ?
  (si oui, on ne la rejoue pas pour une méthode non idempotente comme POST,
  sauf autorisation explicite).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    import requests


class TorApiClientError(Exception):
    """Classe de base de toutes les erreurs du projet."""


class ConfigurationError(TorApiClientError):
    """Configuration invalide (variable d'environnement, dépendance manquante...)."""


class InvalidURLError(TorApiClientError):
    """URL refusée par la validation (schéma, hôte, caractères interdits...)."""


# --------------------------------------------------------------------------- #
# ControlPort / NEWNYM
# --------------------------------------------------------------------------- #
class TorControlError(TorApiClientError):
    """Erreur lors d'un dialogue avec le ControlPort de Tor."""


class TorControlConnectionError(TorControlError):
    """Impossible de joindre le ControlPort (Tor arrêté, mauvais port...)."""


class TorAuthenticationError(TorControlError):
    """Authentification au ControlPort refusée (cookie illisible, mot de passe...)."""


class TorNotReadyError(TorControlError):
    """Tor n'a pas de circuit utilisable dans le délai imparti."""


class TorCheckError(TorApiClientError):
    """Le service de vérification (IP / IsTor) a renvoyé une réponse inexploitable."""


# --------------------------------------------------------------------------- #
# Erreurs réseau (requête HTTP via SOCKS)
# --------------------------------------------------------------------------- #
class NetworkError(TorApiClientError):
    """Erreur réseau rencontrée en envoyant une requête via Tor."""

    retryable: bool = True
    rotate_circuit: bool = True

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        request_may_have_been_sent: bool = False,
    ) -> None:
        super().__init__(message)
        self.url = url  # URL déjà nettoyée (sans query string ni identifiants)
        self.request_may_have_been_sent = request_may_have_been_sent


class TorProxyError(NetworkError):
    """Le proxy SOCKS de Tor a renvoyé une erreur (circuit, sortie, politique...)."""


class TorProxyUnavailableError(TorProxyError):
    """Le port SOCKS de Tor est injoignable : Tor est probablement arrêté.

    Changer de circuit ne sert à rien dans ce cas : on retente avec backoff.
    """

    rotate_circuit = False


class DNSResolutionError(NetworkError):
    """Résolution DNS impossible (effectuée par le nœud de sortie avec socks5h)."""


class RequestTimeoutError(NetworkError):
    """Délai de connexion ou de lecture dépassé."""


class ConnectionFailedError(NetworkError):
    """Connexion interrompue ou refusée (reset, fermeture prématurée...)."""


class TLSError(NetworkError):
    """Erreur TLS/certificat. Jamais retentée silencieusement."""

    retryable = False
    rotate_circuit = False


# --------------------------------------------------------------------------- #
# Erreurs HTTP
# --------------------------------------------------------------------------- #
class HTTPStatusError(TorApiClientError):
    """Réponse HTTP considérée comme un échec."""

    def __init__(self, message: str, *, response: requests.Response) -> None:
        super().__init__(message)
        self.response = response
        self.status_code = response.status_code


class RateLimitedError(HTTPStatusError):
    """HTTP 429 dont le ``Retry-After`` dépasse le maximum accepté."""

    def __init__(
        self,
        message: str,
        *,
        response: requests.Response,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, response=response)
        self.retry_after = retry_after


class RetriesExhaustedError(TorApiClientError):
    """Nombre maximal de tentatives atteint sans succès."""

    def __init__(
        self,
        message: str,
        *,
        attempts: int,
        last_error: BaseException | None = None,
        last_response: requests.Response | None = None,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.last_error = last_error
        self.last_response = last_response
