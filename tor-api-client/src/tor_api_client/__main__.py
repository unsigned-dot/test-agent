"""Interface en ligne de commande : ``python -m tor_api_client`` ou ``tor-api-client``.

Exemples ::

    tor-api-client check
    tor-api-client ip
    tor-api-client rotate
    tor-api-client get https://httpbin.org/get
    tor-api-client post https://httpbin.org/post --json '{"hello": "world"}'
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .client import TorApiClient
from .config import TorConfig
from .exceptions import TorApiClientError
from .logging_config import setup_logging
from .tor import check_tor_connection, get_tor_ip, rotate_tor_circuit, wait_for_tor_ready

MAX_BODY_CHARS = 2000


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tor-api-client", description="Appels HTTP(S) via Tor avec rotation de circuit."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--env-file", help="Chemin du fichier .env (défaut : ./.env)")
    parser.add_argument("--log-level", help="Niveau de log (écrase LOG_LEVEL)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="Vérifie que le trafic passe par Tor")
    sub.add_parser("ip", help="Affiche l'IP de sortie Tor")
    sub.add_parser("rotate", help="Demande un nouveau circuit (NEWNYM)")

    for name in ("get", "delete", "post", "put", "patch"):
        cmd = sub.add_parser(name, help=f"Requête {name.upper()} via Tor")
        cmd.add_argument("url")
        cmd.add_argument(
            "-H", "--header", action="append", default=[], help="En-tête 'Nom: valeur'"
        )
        if name in ("post", "put", "patch"):
            cmd.add_argument("--json", dest="json_body", help="Corps JSON (chaîne)")
    return parser


def _parse_headers(raw_headers: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw in raw_headers:
        name, sep, value = raw.partition(":")
        if not sep or not name.strip():
            raise SystemExit(f"En-tête invalide : {raw!r} (attendu 'Nom: valeur')")
        headers[name.strip()] = value.strip()
    return headers


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        config = TorConfig.from_env(dotenv_path=args.env_file)
    except TorApiClientError as exc:
        print(f"Configuration invalide : {exc}", file=sys.stderr)
        return 2
    setup_logging(args.log_level or config.log_level)

    try:
        if args.command == "check":
            ok = check_tor_connection(config)
            print("OK : le trafic passe par Tor" if ok else "ÉCHEC : trafic non confirmé via Tor")
            return 0 if ok else 1
        if args.command == "ip":
            print(get_tor_ip(config))
            return 0
        if args.command == "rotate":
            rotate_tor_circuit(config)
            wait_for_tor_ready(config)
            print("NEWNYM envoyé ; Tor est prêt (nouvelle IP non garantie).")
            return 0

        kwargs: dict[str, object] = {"headers": _parse_headers(args.header)}
        if getattr(args, "json_body", None):
            try:
                kwargs["json"] = json.loads(args.json_body)
            except json.JSONDecodeError as exc:
                print(f"JSON invalide : {exc}", file=sys.stderr)
                return 2
        with TorApiClient(config) as client:
            response = client.request(args.command.upper(), args.url, **kwargs)
        print(f"HTTP {response.status_code}")
        body = response.text
        print(body[:MAX_BODY_CHARS] + ("\n[... tronqué ...]" if len(body) > MAX_BODY_CHARS else ""))
        return 0 if response.ok else 1
    except TorApiClientError as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
