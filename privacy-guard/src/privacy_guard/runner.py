"""Orchestration : lancer les vérifications et collecter les constats."""

from __future__ import annotations

import logging

from .checks import ALL_CHECKS, Check
from .models import Finding, Severity, Status
from .system import CommandRunner

logger = logging.getLogger(__name__)


def run_checks(
    runner: CommandRunner | None = None,
    *,
    checks: list[Check] | None = None,
    only: set[str] | None = None,
) -> list[Finding]:
    """Exécute les vérifications et renvoie la liste des constats.

    :param only: si fourni, ne garde que les vérifications dont l'id du constat
        est dans cet ensemble.
    """
    runner = runner or CommandRunner()
    checks = checks if checks is not None else ALL_CHECKS
    findings: list[Finding] = []

    for check in checks:
        name = getattr(check, "__name__", repr(check))
        try:
            finding = check(runner)
        except Exception as exc:  # une vérification ne doit jamais planter l'outil
            logger.exception("La vérification %s a levé une exception", name)
            findings.append(
                Finding(name, name, Status.ERROR, Severity.LOW, detail=f"Exception interne : {exc}")
            )
            continue
        if only is None or finding.check_id in only:
            findings.append(finding)
        logger.debug("%s -> %s", finding.check_id, finding.status.value)

    return findings
