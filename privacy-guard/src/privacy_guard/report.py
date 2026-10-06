"""Rendu des résultats : texte lisible et JSON."""

from __future__ import annotations

import json

from .models import Finding, Status

_SYMBOLS = {
    Status.PASS: "[ OK ]",
    Status.WARN: "[WARN]",
    Status.FAIL: "[FAIL]",
    Status.SKIP: "[SKIP]",
    Status.ERROR: "[ERR ]",
}


def summarize(findings: list[Finding]) -> dict[str, int]:
    counts = {status.value: 0 for status in Status}
    for finding in findings:
        counts[finding.status.value] += 1
    return counts


def render_text(findings: list[Finding], *, show_commands: bool = True) -> str:
    lines: list[str] = []
    for finding in sorted(findings, key=lambda f: f.sort_key):
        lines.append(f"{_SYMBOLS[finding.status]} {finding.title} ({finding.severity.value})")
        if finding.detail:
            lines.append(f"       {finding.detail}")
        if finding.status in (Status.FAIL, Status.WARN) and finding.remediation:
            lines.append(f"       → {finding.remediation}")
        if show_commands and finding.fixable:
            lines.append("       Commandes de remédiation (--apply) :")
            lines.extend(f"         $ {cmd}" for cmd in finding.fix_commands)
        lines.append("")

    counts = summarize(findings)
    lines.append(
        "Résumé : "
        f"{counts['PASS']} OK, {counts['FAIL']} échec(s), {counts['WARN']} avertissement(s), "
        f"{counts['SKIP']} ignoré(s), {counts['ERROR']} erreur(s)."
    )
    fixable = sum(1 for f in findings if f.fixable)
    if fixable:
        lines.append(f"{fixable} constat(s) avec remédiation automatique disponible (--apply).")
    return "\n".join(lines)


def render_json(findings: list[Finding]) -> str:
    payload = {
        "summary": summarize(findings),
        "findings": [f.to_dict() for f in sorted(findings, key=lambda f: f.sort_key)],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)
