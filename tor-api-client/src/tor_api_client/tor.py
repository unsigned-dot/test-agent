"""Interaction avec Tor : ControlPort (NEWNYM, état) et vérifications via SOCKS.

Rappels importants :

* NEWNYM demande à Tor d'utiliser de *nouveaux circuits pour les nouvelles
  connexions*. Il ne garantit PAS une nouvelle IP de sortie (Tor peut choisir
  le même nœud de sortie) et ne coupe pas les connexions déjà ouvertes.
* Tor limite le rythme des NEWNYM (environ un toutes les 10 secondes) ; ce
  module impose donc un intervalle minimal entre deux demandes, partagé par
  tous les threads du processus.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
import time
from collections.abc import Callable

import requests
import stem
import stem.connection
from stem import Signal
from stem.connection import AuthMethod
from stem.control import Controller

from .config import TorConfig
from .exceptions import (
    TorAuthenticationError,
    TorCheckError,
    TorControlConnectionError,
    TorControlError,
    TorNotReadyError,
)

logger = logging.getLogger(__name__)

SleepFunc = Callable[[float], None]
ClockFunc = Callable[[], float]

_COOKIE_HINT = (
    "Vérifiez que l'utilisateur courant peut lire le cookie de Tor "
    "(sous Debian/Ubuntu : membre du groupe 'debian-tor', puis reconnexion)."
)

# État partagé du processus : dernier NEWNYM envoyé (horloge monotone).
_rotation_lock = threading.Lock()
_last_rotation: float | None = None


def _reset_rotation_state() -> None:
    """Réinitialise l'état de rotation (utilisé par les tests)."""
    global _last_rotation
    with _rotation_lock:
        _last_rotation = None


# --------------------------------------------------------------------------- #
# ControlPort
# --------------------------------------------------------------------------- #
def _open_controller(config: TorConfig) -> Controller:
    """Ouvre ET authentifie une connexion au ControlPort.

    Refuse un ControlPort configuré sans authentification.
    """
    try:
        controller = Controller.from_port(address=config.control_host, port=config.control_port)
    except stem.SocketError as exc:
        raise TorControlConnectionError(
            f"ControlPort injoignable sur {config.control_host}:{config.control_port} "
            f"({exc}). Tor est-il démarré avec 'ControlPort {config.control_port}' ?"
        ) from exc

    try:
        protocolinfo = stem.connection.get_protocolinfo(controller)
        if AuthMethod.NONE in protocolinfo.auth_methods:
            raise TorAuthenticationError(
                "Le ControlPort n'exige aucune authentification : configuration "
                "refusée. Activez 'CookieAuthentication 1' (ou HashedControlPassword)."
            )
        controller.authenticate(
            password=config.control_password,
            protocolinfo_response=protocolinfo,
        )
    except TorAuthenticationError:
        controller.close()
        raise
    except stem.connection.UnreadableCookieFile as exc:
        controller.close()
        raise TorAuthenticationError(f"Cookie Tor illisible. {_COOKIE_HINT}") from exc
    except stem.connection.MissingPassword as exc:
        controller.close()
        raise TorAuthenticationError(
            "Le ControlPort exige un mot de passe : définissez TOR_CONTROL_PASSWORD "
            "dans le .env ou utilisez l'authentification par cookie."
        ) from exc
    except stem.connection.PasswordAuthFailed as exc:
        controller.close()
        # Ne jamais inclure le mot de passe dans le message.
        raise TorAuthenticationError("Mot de passe du ControlPort refusé.") from exc
    except stem.connection.AuthenticationFailure as exc:
        controller.close()
        raise TorAuthenticationError(
            f"Échec d'authentification au ControlPort ({type(exc).__name__}). {_COOKIE_HINT}"
        ) from exc
    except stem.SocketError as exc:
        controller.close()
        raise TorControlConnectionError(f"Connexion au ControlPort perdue ({exc}).") from exc
    except stem.ControllerError as exc:
        controller.close()
        raise TorControlError(f"Erreur du ControlPort ({exc}).") from exc
    return controller


def rotate_tor_circuit(
    config: TorConfig | None = None,
    *,
    sleep: SleepFunc | None = None,
    clock: ClockFunc | None = None,
) -> None:
    """Demande à Tor de nouveaux circuits (signal NEWNYM).

    Étapes : connexion au ControlPort, authentification (cookie ou mot de
    passe), respect de l'intervalle minimal entre deux NEWNYM, envoi du
    signal, puis attente de ``TOR_NEWNYM_WAIT`` secondes.

    :raises TorControlConnectionError: ControlPort injoignable.
    :raises TorAuthenticationError: authentification refusée.
    :raises TorControlError: Tor a refusé le signal.
    """
    global _last_rotation
    config = config or TorConfig.from_env()
    sleep = sleep or time.sleep
    clock = clock or time.monotonic

    with _rotation_lock:
        if _last_rotation is not None:
            remaining = config.newnym_min_interval - (clock() - _last_rotation)
            if remaining > 0:
                logger.info(
                    "Limitation NEWNYM : attente de %.1f s avant la nouvelle demande", remaining
                )
                sleep(remaining)

        logger.info("Demande d'un nouveau circuit Tor (NEWNYM)")
        controller = _open_controller(config)
        try:
            controller.signal(Signal.NEWNYM)
        except stem.SocketError as exc:
            raise TorControlConnectionError(f"Connexion au ControlPort perdue ({exc}).") from exc
        except stem.ControllerError as exc:
            raise TorControlError(f"Tor a refusé le signal NEWNYM ({exc}).") from exc
        finally:
            controller.close()
        _last_rotation = clock()

    logger.info(
        "NEWNYM accepté ; attente de %.1f s pour la construction des nouveaux circuits "
        "(une nouvelle IP de sortie n'est pas garantie)",
        config.newnym_wait,
    )
    if config.newnym_wait > 0:
        sleep(config.newnym_wait)


def _controller_reports_ready(controller: Controller) -> bool:
    bootstrap = controller.get_info("status/bootstrap-phase", "")
    established = controller.get_info("status/circuit-established", "0")
    return "PROGRESS=100" in bootstrap and established.strip() == "1"


def is_tor_ready(config: TorConfig | None = None) -> bool:
    """True si Tor est entièrement bootstrappé et dispose d'un circuit."""
    config = config or TorConfig.from_env()
    controller = _open_controller(config)
    try:
        return _controller_reports_ready(controller)
    except stem.ControllerError as exc:
        logger.warning("Lecture de l'état de Tor impossible : %s", exc)
        return False
    finally:
        controller.close()


def wait_for_tor_ready(
    config: TorConfig | None = None,
    *,
    timeout: float | None = None,
    poll_interval: float = 1.0,
    sleep: SleepFunc | None = None,
    clock: ClockFunc | None = None,
) -> None:
    """Attend que Tor signale un circuit utilisable, au plus ``timeout`` secondes.

    :raises TorNotReadyError: délai dépassé.
    :raises TorAuthenticationError: authentification refusée (inutile d'insister).
    """
    config = config or TorConfig.from_env()
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    timeout = config.tor_ready_timeout if timeout is None else timeout
    deadline = clock() + timeout

    logger.info("Attente de Tor (circuit établi, %.0f s max)", timeout)
    while True:
        try:
            if is_tor_ready(config):
                logger.info("Tor est prêt")
                return
        except TorAuthenticationError:
            raise
        except TorControlError as exc:
            logger.debug("Tor pas encore joignable : %s", exc)
        if clock() >= deadline:
            raise TorNotReadyError(f"Tor n'est pas prêt après {timeout:.0f} s.")
        sleep(poll_interval)


# --------------------------------------------------------------------------- #
# Vérifications via le proxy SOCKS
# --------------------------------------------------------------------------- #
def create_tor_session(config: TorConfig) -> requests.Session:
    """Session requests configurée pour passer exclusivement par Tor.

    ``trust_env = False`` : les variables HTTP(S)_PROXY / NO_PROXY de
    l'environnement ne peuvent pas détourner le trafic hors de Tor.
    """
    session = requests.Session()
    session.trust_env = False
    session.proxies.update(config.proxies)
    if config.user_agent:
        session.headers["User-Agent"] = config.user_agent
    return session


def check_socks_port(config: TorConfig | None = None, timeout: float = 3.0) -> bool:
    """True si le port SOCKS de Tor accepte les connexions TCP."""
    config = config or TorConfig.from_env()
    try:
        with socket.create_connection((config.socks_host, config.socks_port), timeout=timeout):
            return True
    except OSError as exc:
        logger.warning(
            "Port SOCKS %s:%s injoignable : %s", config.socks_host, config.socks_port, exc
        )
        return False


def _fetch(url: str, config: TorConfig, session: requests.Session | None) -> requests.Response:
    own_session = session is None
    session = session or create_tor_session(config)
    try:
        response = session.get(url, proxies=config.proxies, timeout=config.timeout)
        response.raise_for_status()
        return response
    except requests.RequestException as exc:
        raise TorCheckError(f"Service de vérification injoignable via Tor : {exc}") from exc
    finally:
        if own_session:
            session.close()


def check_tor_connection(
    config: TorConfig | None = None, *, session: requests.Session | None = None
) -> bool:
    """Confirme que le trafic sort bien par le réseau Tor.

    Interroge ``TOR_CHECK_URL`` (par défaut l'API de check.torproject.org,
    qui renvoie ``{"IsTor": true, "IP": "..."}``). Retourne False si le
    service est injoignable ou si la réponse ne confirme pas l'usage de Tor.
    """
    config = config or TorConfig.from_env()
    try:
        response = _fetch(config.tor_check_url, config, session)
        data = response.json()
    except TorCheckError as exc:
        logger.warning("%s", exc)
        return False
    except ValueError:
        logger.warning("Réponse non JSON du service de vérification Tor")
        return False

    if not isinstance(data, dict) or "IsTor" not in data:
        logger.warning(
            "Le service %s ne fournit pas le champ 'IsTor' : impossible de confirmer",
            config.tor_check_url,
        )
        return False
    is_tor = data.get("IsTor") is True
    if is_tor:
        logger.info("Connexion via Tor confirmée")
    else:
        logger.error("Le service de vérification indique que le trafic NE passe PAS par Tor")
    return is_tor


def _extract_ip(response: requests.Response) -> str:
    candidate: object = None
    try:
        data = response.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        for key in ("IP", "ip", "origin", "query", "address"):
            if data.get(key):
                candidate = data[key]
                break
    elif isinstance(data, str):
        candidate = data
    if candidate is None:
        candidate = response.text

    text = str(candidate).strip().split(",")[0].strip()
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        raise TorCheckError("Le service d'IP n'a pas renvoyé d'adresse IP valide.") from None


def get_tor_ip(
    config: TorConfig | None = None, *, session: requests.Session | None = None
) -> str:
    """Retourne l'IP publique vue par ``IP_CHECK_URL`` à travers Tor.

    Formats acceptés : JSON (clés IP, ip, origin, query, address) ou texte brut.

    :raises TorCheckError: service injoignable ou réponse invalide.
    """
    config = config or TorConfig.from_env()
    response = _fetch(config.ip_check_url, config, session)
    ip = _extract_ip(response)
    logger.info("IP de sortie Tor actuelle : %s", ip)
    return ip
