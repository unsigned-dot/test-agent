#!/usr/bin/env python3
"""Test d'intégration MANUEL sur une vraie installation Tor.

Vérifie dans l'ordre :
  1. la configuration (.env) ;
  2. l'ouverture du port SOCKS ;
  3. l'authentification au ControlPort et l'état de Tor ;
  4. que le trafic sort bien par Tor (IsTor) ;
  5. l'IP de sortie, NEWNYM, puis l'IP de sortie à nouveau ;
  6. (option --url) une requête via TorApiClient.

Usage :
    python scripts/check_tor.py
    python scripts/check_tor.py --no-rotate
    python scripts/check_tor.py --url https://httpbin.org/get

Code de retour : 0 si toutes les vérifications obligatoires passent, 1 sinon.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import tor_api_client  # noqa: F401
except ImportError:  # exécution sans 'pip install -e .'
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tor_api_client import (  # noqa: E402
    TorApiClient,
    TorApiClientError,
    TorConfig,
    check_socks_port,
    check_tor_connection,
    get_tor_ip,
    is_tor_ready,
    rotate_tor_circuit,
    setup_logging,
    wait_for_tor_ready,
)


def step(title: str) -> None:
    print(f"\n=== {title}")


def ok(message: str) -> None:
    print(f"[OK]    {message}")


def fail(message: str) -> None:
    print(f"[ÉCHEC] {message}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", help="Fichier .env à utiliser")
    parser.add_argument("--no-rotate", action="store_true", help="Ne pas envoyer de NEWNYM")
    parser.add_argument("--url", help="URL à appeler via TorApiClient à la fin du test")
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args()
    setup_logging(args.log_level)

    step("1. Configuration")
    try:
        config = TorConfig.from_env(dotenv_path=args.env_file)
    except TorApiClientError as exc:
        fail(f"configuration invalide : {exc}")
        return 1
    ok(f"SOCKS {config.proxy_url} | ControlPort {config.control_host}:{config.control_port} | "
       f"auth : {'mot de passe' if config.control_password else 'cookie'}")

    step("2. Port SOCKS")
    if not check_socks_port(config):
        fail("port SOCKS injoignable : 'sudo systemctl status tor@default' ?")
        return 1
    ok(f"{config.socks_host}:{config.socks_port} accepte les connexions")

    step("3. ControlPort (authentification + état)")
    try:
        if is_tor_ready(config):
            ok("authentification réussie, Tor bootstrappé avec un circuit établi")
        else:
            print("[...]   authentification réussie, Tor pas encore prêt : attente")
            wait_for_tor_ready(config)
            ok("Tor est prêt")
    except TorApiClientError as exc:
        fail(str(exc))
        return 1

    step("4. Le trafic passe-t-il par Tor ?")
    if not check_tor_connection(config):
        fail(f"non confirmé par {config.tor_check_url}")
        return 1
    ok(f"confirmé par {config.tor_check_url}")

    step("5. IP de sortie et NEWNYM")
    try:
        ip_before = get_tor_ip(config)
        ok(f"IP de sortie : {ip_before}")
        if args.no_rotate:
            print("[--]    NEWNYM ignoré (--no-rotate)")
        else:
            rotate_tor_circuit(config)
            wait_for_tor_ready(config)
            ok("NEWNYM accepté et Tor prêt")
            ip_after = get_tor_ip(config)
            if ip_after != ip_before:
                ok(f"nouvelle IP de sortie : {ip_after}")
            else:
                # Comportement normal possible : NEWNYM ne garantit pas une nouvelle IP.
                print(f"[INFO]  même IP de sortie ({ip_after}) : NEWNYM ne garantit pas un autre nœud de sortie")
    except TorApiClientError as exc:
        fail(str(exc))
        return 1

    if args.url:
        step("6. Requête via TorApiClient")
        try:
            with TorApiClient(config) as client:
                response = client.get(args.url)
            ok(f"HTTP {response.status_code} ({len(response.content)} octets)")
        except TorApiClientError as exc:
            fail(str(exc))
            return 1

    print("\nToutes les vérifications sont passées.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
