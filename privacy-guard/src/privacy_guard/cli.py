"""Interface en ligne de commande.

    privacy-guard audit            # audit en lecture seule (défaut)
    privacy-guard audit --json     # sortie JSON
    privacy-guard apply            # applique les remédiations, avec confirmation
    privacy-guard apply --yes      # sans confirmation (pour l'automatisation)
"""

from __future__ import annotations

import argparse
import logging
import sys

from .apply import apply_fixes
from .models import Status
from .report import render_json, render_text
from .runner import run_checks
from .system import CommandRunner, detect_os, is_debian_like


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        stream=sys.stderr,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="privacy-guard",
        description="Audit et durcissement de sécurité pour Debian/Ubuntu (audit par défaut).",
    )
    parser.add_argument("--log-level", default="WARNING", help="DEBUG, INFO, WARNING...")
    sub = parser.add_subparsers(dest="command")

    audit = sub.add_parser("audit", help="Vérifie la configuration (lecture seule)")
    audit.add_argument("--json", action="store_true", help="Sortie JSON")

    apply_cmd = sub.add_parser("apply", help="Applique les remédiations sûres (modifie le système)")
    apply_cmd.add_argument("--yes", action="store_true", help="Ne pas demander de confirmation")
    return parser


def _warn_if_not_debian() -> None:
    os_info = detect_os()
    if os_info["system"] != "Linux":
        print(
            f"Attention : conçu pour Linux (Debian/Ubuntu) ; système détecté : {os_info['system']}. "
            "Les vérifications non applicables seront ignorées (SKIP).",
            file=sys.stderr,
        )
    elif not is_debian_like(os_info):
        print(
            f"Attention : distribution « {os_info['distro'] or 'inconnue'} » non Debian/Ubuntu ; "
            "certaines vérifications seront ignorées.",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    _setup_logging(args.log_level)
    command = args.command or "audit"
    _warn_if_not_debian()

    runner = CommandRunner()
    findings = run_checks(runner)

    if command == "audit":
        print(render_json(findings) if getattr(args, "json", False) else render_text(findings))
        # Code de sortie non nul s'il reste au moins un échec : utile en CI/cron.
        return 1 if any(f.status == Status.FAIL for f in findings) else 0

    if command == "apply":
        fixable = [f for f in findings if f.fixable]
        if not fixable:
            print("Aucune remédiation automatique disponible. Tout est déjà en place "
                  "ou les actions restantes sont manuelles (voir « audit »).")
            return 0
        print(f"{len(fixable)} remédiation(s) disponible(s).")
        results = apply_fixes(fixable, assume_yes=args.yes)
        applied = sum(1 for state in results.values() if state == "applied")
        failed = sum(1 for state in results.values() if state == "failed")
        print(f"\nTerminé : {applied} appliquée(s), {failed} en échec, "
              f"{len(results) - applied - failed} ignorée(s).")
        print("Relancez « privacy-guard audit » pour vérifier le résultat.")
        return 1 if failed else 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
