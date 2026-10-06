"""Types de base : statut, sévérité, résultat d'une vérification."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Status(str, Enum):
    """Résultat d'une vérification."""

    PASS = "PASS"      # mesure en place
    WARN = "WARN"      # partiellement en place ou à confirmer manuellement
    FAIL = "FAIL"      # mesure absente
    SKIP = "SKIP"      # non applicable (commande/service absent)
    ERROR = "ERROR"    # la vérification elle-même a échoué


class Severity(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# Ordre d'affichage : les problèmes les plus graves d'abord.
_STATUS_ORDER = {Status.FAIL: 0, Status.WARN: 1, Status.ERROR: 2, Status.SKIP: 3, Status.PASS: 4}
_SEVERITY_ORDER = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}


@dataclass
class Finding:
    """Résultat d'une vérification, avec remédiation éventuelle.

    ``fix_commands`` ne contient que des commandes idempotentes et sûres, qui
    *renforcent* la sécurité. Elles ne sont jamais exécutées automatiquement :
    seul ``--apply`` les lance, après affichage et confirmation.
    """

    check_id: str
    title: str
    status: Status
    severity: Severity = Severity.MEDIUM
    detail: str = ""
    remediation: str = ""
    fix_commands: list[str] = field(default_factory=list)
    requires_root: bool = False

    @property
    def sort_key(self) -> tuple[int, int, str]:
        return (_STATUS_ORDER[self.status], _SEVERITY_ORDER[self.severity], self.check_id)

    @property
    def fixable(self) -> bool:
        """Vrai si une remédiation automatique sûre existe pour ce constat."""
        return self.status in (Status.FAIL, Status.WARN) and bool(self.fix_commands)

    def to_dict(self) -> dict:
        return {
            "check_id": self.check_id,
            "title": self.title,
            "status": self.status.value,
            "severity": self.severity.value,
            "detail": self.detail,
            "remediation": self.remediation,
            "fix_commands": list(self.fix_commands),
            "requires_root": self.requires_root,
        }
