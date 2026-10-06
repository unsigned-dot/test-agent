from __future__ import annotations

import json

from privacy_guard.apply import apply_fixes
from privacy_guard.models import Finding, Severity, Status
from privacy_guard.report import render_json, render_text, summarize
from privacy_guard.runner import run_checks
from privacy_guard.system import CommandResult


# --------------------------------------------------------------------------- #
# runner : robustesse
# --------------------------------------------------------------------------- #
def test_runner_isolates_check_exceptions():
    def boom(_runner):
        raise RuntimeError("kaboom")

    def good(_runner):
        return Finding("good", "OK", Status.PASS)

    findings = run_checks(checks=[boom, good])
    statuses = {f.status for f in findings}
    assert Status.ERROR in statuses and Status.PASS in statuses


def test_runner_only_filter():
    def a(_r):
        return Finding("a", "A", Status.FAIL)

    def b(_r):
        return Finding("b", "B", Status.PASS)

    findings = run_checks(checks=[a, b], only={"a"})
    assert [f.check_id for f in findings] == ["a"]


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def _sample() -> list[Finding]:
    return [
        Finding("pass", "Déjà OK", Status.PASS, Severity.LOW),
        Finding("fail", "Pare-feu", Status.FAIL, Severity.HIGH,
                detail="inactif", remediation="activer", fix_commands=["sudo ufw --force enable"]),
        Finding("warn", "DNS", Status.WARN, Severity.MEDIUM, detail="clair"),
    ]


def test_render_text_orders_fail_first_and_lists_commands():
    text = render_text(_sample())
    assert text.index("[FAIL]") < text.index("[WARN]") < text.index("[ OK ]")
    assert "sudo ufw --force enable" in text
    assert "1 constat(s) avec remédiation automatique disponible" in text


def test_render_json_valid_and_summarized():
    payload = json.loads(render_json(_sample()))
    assert payload["summary"]["FAIL"] == 1
    assert payload["summary"]["PASS"] == 1
    assert len(payload["findings"]) == 3


def test_summarize_counts():
    counts = summarize(_sample())
    assert counts["FAIL"] == 1 and counts["WARN"] == 1 and counts["PASS"] == 1


# --------------------------------------------------------------------------- #
# apply : confirmation, exécution, arrêt sur erreur
# --------------------------------------------------------------------------- #
def _fixable() -> Finding:
    return Finding("fw", "Pare-feu", Status.FAIL, Severity.HIGH,
                   fix_commands=["cmd-a", "cmd-b"], requires_root=True)


def test_apply_requires_confirmation_and_skips_on_no():
    executed: list[str] = []
    results = apply_fixes(
        [_fixable()],
        confirm=lambda _p: False,
        executor=lambda c: executed.append(c) or 0,
    )
    assert results == {"fw": "skipped"}
    assert executed == []


def test_apply_runs_all_commands_on_yes():
    executed: list[str] = []
    results = apply_fixes(
        [_fixable()],
        assume_yes=True,
        executor=lambda c: executed.append(c) or 0,
    )
    assert results == {"fw": "applied"}
    assert executed == ["cmd-a", "cmd-b"]


def test_apply_stops_sequence_on_first_failure():
    executed: list[str] = []

    def executor(cmd: str) -> int:
        executed.append(cmd)
        return 1 if cmd == "cmd-a" else 0

    results = apply_fixes([_fixable()], assume_yes=True, executor=executor)
    assert results == {"fw": "failed"}
    assert executed == ["cmd-a"]  # cmd-b non lancée


def test_apply_ignores_non_fixable_findings():
    passing = Finding("ok", "OK", Status.PASS)
    results = apply_fixes([passing], assume_yes=True, executor=lambda c: 0)
    assert results == {}
