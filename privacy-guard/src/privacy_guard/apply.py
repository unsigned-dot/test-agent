"""Application des remédiations, de façon contrôlée et confirmée.

Garde-fous :

* seules les commandes attachées aux constats FAIL/WARN sont candidates ;
* chaque commande est affichée avant exécution ;
* confirmation interactive obligatoire, sauf ``assume_yes`` explicite ;
* à la moindre erreur sur un constat, on arrête les commandes de CE constat
  (on ne lance pas la suite d'une séquence partiellement échouée).

L'outil ne désactive jamais une protection : toutes les commandes proviennent
des ``fix_commands`` curés dans ``checks.py``.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable

from .models import Finding

logger = logging.getLogger(__name__)

Confirmer = Callable[[str], bool]


def _default_confirm(prompt: str) -> bool:
    try:
        return input(f"{prompt} [o/N] ").strip().lower() in ("o", "oui", "y", "yes")
    except EOFError:
        return False


def apply_fixes(
    findings: list[Finding],
    *,
    assume_yes: bool = False,
    confirm: Confirmer | None = None,
    executor: Callable[[str], int] | None = None,
) -> dict[str, str]:
    """Applique les remédiations disponibles. Retourne {check_id: état}.

    États possibles : ``applied``, ``skipped``, ``failed``.
    """
    confirm = confirm or _default_confirm
    executor = executor or _run_shell
    results: dict[str, str] = {}

    for finding in findings:
        if not finding.fixable:
            continue
        print(f"\n# {finding.title} ({finding.severity.value})")
        print(f"  {finding.detail}")
        for cmd in finding.fix_commands:
            print(f"    $ {cmd}")
        if finding.requires_root:
            print("  (nécessite les droits root / sudo)")

        if not assume_yes and not confirm("Appliquer ces commandes ?"):
            logger.info("Remédiation ignorée pour %s", finding.check_id)
            results[finding.check_id] = "skipped"
            continue

        results[finding.check_id] = _apply_one(finding, executor)
    return results


def _apply_one(finding: Finding, executor: Callable[[str], int]) -> str:
    for cmd in finding.fix_commands:
        logger.info("Exécution : %s", cmd)
        code = executor(cmd)
        if code != 0:
            logger.error("Échec (code %d) ; arrêt de la séquence pour %s", code, finding.check_id)
            return "failed"
    logger.info("Remédiation appliquée pour %s", finding.check_id)
    return "applied"


def _run_shell(command: str) -> int:
    # Les commandes proviennent exclusivement des fix_commands curés (heredocs
    # inclus), d'où l'usage volontaire du shell. Aucune entrée utilisateur libre.
    completed = subprocess.run(command, shell=True, check=False)  # noqa: S602
    return completed.returncode
