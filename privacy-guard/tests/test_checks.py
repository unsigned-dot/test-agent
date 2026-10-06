from __future__ import annotations

import pytest

from privacy_guard import checks
from privacy_guard.models import Status
from privacy_guard.system import CommandResult


class FakeRunner:
    """Runner injectable : réponses prédéfinies par préfixe de commande."""

    def __init__(self, responses: dict[str, CommandResult], commands: set[str] | None = None):
        self._responses = responses
        self._commands = commands if commands is not None else {k.split()[0] for k in responses}

    def has_command(self, name: str) -> bool:
        return name in self._commands

    def run(self, command, *, timeout=None):
        key = " ".join(command)
        for prefix, result in self._responses.items():
            if key.startswith(prefix):
                return result
        return CommandResult(127, "", "non simulé")


def ok(stdout: str) -> CommandResult:
    return CommandResult(0, stdout, "")


def ko(stderr: str = "erreur", code: int = 1) -> CommandResult:
    return CommandResult(code, "", stderr)


# --------------------------------------------------------------------------- #
# Chiffrement du disque
# --------------------------------------------------------------------------- #
def test_disk_encryption_pass():
    runner = FakeRunner({"lsblk": ok("disk\npart\ncrypt\nlvm\n")})
    assert checks.check_disk_encryption(runner).status is Status.PASS


def test_disk_encryption_fail_has_no_autofix():
    runner = FakeRunner({"lsblk": ok("disk\npart\nlvm\n")})
    finding = checks.check_disk_encryption(runner)
    assert finding.status is Status.FAIL
    # Le chiffrement ne s'automatise pas sans risque : pas de fix_commands.
    assert finding.fix_commands == []
    assert "réinstall" in finding.remediation.lower()


def test_disk_encryption_skip_without_lsblk():
    runner = FakeRunner({}, commands=set())
    assert checks.check_disk_encryption(runner).status is Status.SKIP


# --------------------------------------------------------------------------- #
# Pare-feu
# --------------------------------------------------------------------------- #
def test_firewall_not_installed_offers_install():
    runner = FakeRunner({}, commands=set())
    finding = checks.check_firewall(runner)
    assert finding.status is Status.FAIL
    assert any("install" in c for c in finding.fix_commands)
    assert finding.requires_root is True


def test_firewall_inactive_offers_enable():
    runner = FakeRunner({"ufw status verbose": ok("Status: inactive")})
    finding = checks.check_firewall(runner)
    assert finding.status is Status.FAIL
    assert "sudo ufw --force enable" in finding.fix_commands


def test_firewall_active_default_deny_passes():
    runner = FakeRunner({"ufw status verbose": ok("Status: active\nDefault: deny (incoming), allow (outgoing)")})
    assert checks.check_firewall(runner).status is Status.PASS


def test_firewall_active_but_not_deny_warns():
    runner = FakeRunner({"ufw status verbose": ok("Status: active\nDefault: allow (incoming), allow (outgoing)")})
    finding = checks.check_firewall(runner)
    assert finding.status is Status.WARN
    assert finding.fix_commands == ["sudo ufw default deny incoming"]


# --------------------------------------------------------------------------- #
# Mises à jour automatiques
# --------------------------------------------------------------------------- #
def test_auto_updates_missing():
    runner = FakeRunner({"dpkg-query": ok("unknown ok not-installed")})
    finding = checks.check_auto_updates(runner)
    assert finding.status is Status.FAIL
    assert any("unattended-upgrades" in c for c in finding.fix_commands)


def test_auto_updates_installed_and_enabled():
    runner = FakeRunner({
        "dpkg-query": ok("install ok installed"),
        "cat /etc/apt/apt.conf.d/20auto-upgrades": ok('APT::Periodic::Update-Package-Lists "1";'),
    })
    assert checks.check_auto_updates(runner).status is Status.PASS


def test_auto_updates_installed_but_not_scheduled_warns():
    runner = FakeRunner({
        "dpkg-query": ok("install ok installed"),
        "cat /etc/apt/apt.conf.d/20auto-upgrades": ko("fichier absent"),
    })
    assert checks.check_auto_updates(runner).status is Status.WARN


# --------------------------------------------------------------------------- #
# SSH
# --------------------------------------------------------------------------- #
def test_ssh_skip_when_no_server():
    runner = FakeRunner({"cat /etc/ssh/sshd_config": ko("No such file")})
    assert checks.check_ssh_hardening(runner).status is Status.SKIP


def test_ssh_hardened_passes():
    runner = FakeRunner({
        "cat /etc/ssh/sshd_config": ok("PasswordAuthentication no\nPermitRootLogin no\n"),
    })
    assert checks.check_ssh_hardening(runner).status is Status.PASS


def test_ssh_password_auth_enabled_fails_without_autofix():
    runner = FakeRunner({
        "cat /etc/ssh/sshd_config": ok("PasswordAuthentication yes\nPermitRootLogin yes\n"),
    })
    finding = checks.check_ssh_hardening(runner)
    assert finding.status is Status.FAIL
    # On n'automatise pas : risque de verrouillage sans clé SSH en place.
    assert finding.fix_commands == []
    assert "clé ssh" in finding.remediation.lower()


def test_ssh_ignores_comments_and_case():
    runner = FakeRunner({
        "cat /etc/ssh/sshd_config": ok("# PasswordAuthentication yes\npasswordauthentication NO\nPermitRootLogin prohibit-password\n"),
    })
    assert checks.check_ssh_hardening(runner).status is Status.PASS


# --------------------------------------------------------------------------- #
# DNS
# --------------------------------------------------------------------------- #
def test_dns_encrypted_passes():
    runner = FakeRunner({"resolvectl status": ok("Current DNS Server: 9.9.9.9\n+DNSOverTLS=yes")})
    assert checks.check_dns_encryption(runner).status is Status.PASS


def test_dns_plaintext_warns_with_fix():
    runner = FakeRunner({"resolvectl status": ok("DNSOverTLS: no")})
    finding = checks.check_dns_encryption(runner)
    assert finding.status is Status.WARN
    assert any("resolved.conf" in c for c in finding.fix_commands)


def test_dns_skip_without_resolvectl():
    runner = FakeRunner({}, commands=set())
    assert checks.check_dns_encryption(runner).status is Status.SKIP


# --------------------------------------------------------------------------- #
# Secure Boot
# --------------------------------------------------------------------------- #
def test_secure_boot_enabled():
    runner = FakeRunner({"mokutil --sb-state": ok("SecureBoot enabled")})
    assert checks.check_secure_boot(runner).status is Status.PASS


def test_secure_boot_disabled_warns():
    runner = FakeRunner({"mokutil --sb-state": ok("SecureBoot disabled")})
    assert checks.check_secure_boot(runner).status is Status.WARN


# --------------------------------------------------------------------------- #
# Mots de passe vides
# --------------------------------------------------------------------------- #
def test_empty_passwords_detected():
    runner = FakeRunner({"getent shadow": ok("root:$6$x:1::::::\nbob::1::::::\n")})
    finding = checks.check_empty_passwords(runner)
    assert finding.status is Status.FAIL
    assert "bob" in finding.detail


def test_empty_passwords_none():
    runner = FakeRunner({"getent shadow": ok("root:$6$x:1::::::\nbob:$6$y:1::::::\n")})
    assert checks.check_empty_passwords(runner).status is Status.PASS


def test_empty_passwords_skip_without_root():
    runner = FakeRunner({"getent shadow": ok("")})
    assert checks.check_empty_passwords(runner).status is Status.SKIP
