"""Configuration du logging et utilitaires d'assainissement pour les logs."""

from __future__ import annotations

import logging
import sys
from urllib.parse import urlsplit, urlunsplit

DEFAULT_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def setup_logging(level: str | int = "INFO", fmt: str = DEFAULT_FORMAT) -> None:
    """Configure le logging racine pour une application ou un script.

    La bibliothèque elle-même n'appelle jamais cette fonction : c'est à
    l'application de décider de la configuration des logs.
    """
    if isinstance(level, str):
        level = logging.getLevelName(level.upper())
        if not isinstance(level, int):
            level = logging.INFO
    logging.basicConfig(level=level, format=fmt, stream=sys.stderr, force=True)
    # stem est très bavard en DEBUG (contenu du protocole de contrôle).
    logging.getLogger("stem").setLevel(max(level, logging.WARNING))
    logging.getLogger("urllib3").setLevel(max(level, logging.WARNING))


def sanitize_url(url: str) -> str:
    """Version d'une URL sûre pour les logs.

    Supprime identifiants (user:pass@), query string et fragment, qui
    contiennent souvent des jetons ou des données personnelles.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<url invalide>"
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = f"{host}:{port}" if port else host
    path = parts.path
    suffix = "?<redacted>" if parts.query else ""
    return urlunsplit((parts.scheme, netloc, path, "", "")) + suffix
