"""Vérifications de durcissement pour Debian/Ubuntu.

Chaque vérification est une fonction ``(runner) -> Finding``. Toutes les
commandes lancées ici sont en lecture seule. Quand une mesure manque, le
``Finding`` propose une remédiation (``fix_commands``) idempotente et sûre,
qui ne fait que *renforcer* la sécurité — jamais l'affaiblir.

Une vérification ne peut pas « échouer » bruyamment : si une commande attendue
est absente, elle renvoie SKIP ; si elle plante, ERROR. Jamais d'exception
remontée à l'appelant.
"""

from __future__ import annotations

from collections.abc import Callable

from .models import Finding, Severity, Status
from .system import CommandRunner

Check = Callable[[CommandRunner], Finding]


def _skip(check_id: str, title: str, reason: str, severity: Severity = Severity.MEDIUM) -> Finding:
    return Finding(check_id, title, Status.SKIP, severity, detail=reason)


# --------------------------------------------------------------------------- #
# 1. Chiffrement du disque
# --------------------------------------------------------------------------- #
def check_disk_encryption(runner: CommandRunner) -> Finding:
    title = "Chiffrement du disque (LUKS)"
    sev = Severity.HIGH
    if not runner.has_command("lsblk"):
        return _skip("disk_encryption", title, "lsblk absent", sev)
    result = runner.run(["lsblk", "-o", "TYPE", "-n"])
    if not result.ok:
        return Finding("disk_encryption", title, Status.ERROR, sev, detail=result.stderr or "lsblk a échoué")
    has_crypt = any(line.strip() == "crypt" for line in result.stdout.splitlines())
    if has_crypt:
        return Finding("disk_encryption", title, Status.PASS, sev, detail="Volume chiffré LUKS détecté.")
    return Finding(
        "disk_encryption",
        title,
        Status.FAIL,
        sev,
        detail="Aucun volume chiffré (type 'crypt') détecté.",
        remediation=(
            "Le chiffrement intégral ne peut pas être activé a posteriori sans risque : "
            "il se met en place à l'installation du système (option « chiffrer le disque », "
            "LUKS). Sauvegardez vos données puis réinstallez en activant le chiffrement, "
            "ou chiffrez un disque secondaire avec cryptsetup."
        ),
    )


# --------------------------------------------------------------------------- #
# 2. Pare-feu
# --------------------------------------------------------------------------- #
def check_firewall(runner: CommandRunner) -> Finding:
    title = "Pare-feu (ufw)"
    sev = Severity.HIGH
    if not runner.has_command("ufw"):
        return Finding(
            "firewall",
            title,
            Status.FAIL,
            sev,
            detail="ufw n'est pas installé.",
            remediation="Installer et activer ufw avec une politique par défaut « refuser l'entrant ».",
            fix_commands=[
                "sudo apt-get update",
                "sudo apt-get install -y ufw",
                "sudo ufw default deny incoming",
                "sudo ufw default allow outgoing",
                "sudo ufw --force enable",
            ],
            requires_root=True,
        )
    result = runner.run(["ufw", "status", "verbose"])
    if not result.ok:
        # Souvent faute de droits root pour lire l'état.
        return Finding(
            "firewall", title, Status.WARN, sev,
            detail="Impossible de lire l'état de ufw (droits root requis).",
            remediation="Vérifier avec : sudo ufw status verbose",
        )
    text = result.out.lower()
    if "status: active" not in text:
        return Finding(
            "firewall", title, Status.FAIL, sev,
            detail="ufw est installé mais inactif.",
            remediation="Activer ufw avec une politique par défaut « refuser l'entrant ».",
            fix_commands=[
                "sudo ufw default deny incoming",
                "sudo ufw default allow outgoing",
                "sudo ufw --force enable",
            ],
            requires_root=True,
        )
    if "deny (incoming)" not in text:
        return Finding(
            "firewall", title, Status.WARN, sev,
            detail="ufw est actif mais la politique entrante par défaut n'est pas « deny ».",
            remediation="Passer la politique entrante par défaut à « deny ».",
            fix_commands=["sudo ufw default deny incoming"],
            requires_root=True,
        )
    return Finding("firewall", title, Status.PASS, sev, detail="ufw actif, entrant refusé par défaut.")


# --------------------------------------------------------------------------- #
# 3. Mises à jour de sécurité automatiques
# --------------------------------------------------------------------------- #
def check_auto_updates(runner: CommandRunner) -> Finding:
    title = "Mises à jour de sécurité automatiques"
    sev = Severity.HIGH
    if not runner.has_command("dpkg-query"):
        return _skip("auto_updates", title, "dpkg-query absent (distribution non Debian ?)", sev)
    result = runner.run(["dpkg-query", "-W", "-f=${Status}", "unattended-upgrades"])
    installed = result.ok and "install ok installed" in result.out
    if not installed:
        return Finding(
            "auto_updates", title, Status.FAIL, sev,
            detail="unattended-upgrades n'est pas installé.",
            remediation="Installer et activer les mises à jour de sécurité automatiques.",
            fix_commands=[
                "sudo apt-get update",
                "sudo apt-get install -y unattended-upgrades",
                "sudo dpkg-reconfigure -f noninteractive unattended-upgrades",
            ],
            requires_root=True,
        )
    enabled_file = runner.run(["cat", "/etc/apt/apt.conf.d/20auto-upgrades"])
    if enabled_file.ok and 'Update-Package-Lists "1"' in enabled_file.stdout:
        return Finding("auto_updates", title, Status.PASS, sev, detail="unattended-upgrades installé et activé.")
    return Finding(
        "auto_updates", title, Status.WARN, sev,
        detail="unattended-upgrades installé mais l'activation périodique n'est pas confirmée.",
        remediation="Activer la planification des mises à jour.",
        fix_commands=["sudo dpkg-reconfigure -f noninteractive unattended-upgrades"],
        requires_root=True,
    )


# --------------------------------------------------------------------------- #
# 4. Durcissement SSH (seulement si un serveur SSH est présent)
# --------------------------------------------------------------------------- #
def check_ssh_hardening(runner: CommandRunner) -> Finding:
    title = "Durcissement du serveur SSH"
    sev = Severity.HIGH
    config = runner.run(["cat", "/etc/ssh/sshd_config"])
    if not config.ok:
        return _skip("ssh_hardening", title, "Aucun serveur SSH (sshd_config absent).", sev)

    directives: dict[str, str] = {}
    for line in config.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2:
            directives.setdefault(parts[0].lower(), parts[1].strip().lower())

    problems = []
    if directives.get("passwordauthentication") != "no":
        problems.append("PasswordAuthentication devrait être « no » (authentification par clé).")
    if directives.get("permitrootlogin") not in ("no", "prohibit-password"):
        problems.append("PermitRootLogin devrait être « no ».")
    if not problems:
        return Finding("ssh_hardening", title, Status.PASS, sev, detail="Mots de passe et login root désactivés.")
    return Finding(
        "ssh_hardening", title, Status.FAIL, sev,
        detail=" ".join(problems),
        remediation=(
            "Avant de désactiver les mots de passe, assurez-vous d'avoir une clé SSH "
            "fonctionnelle (ssh-copy-id), sinon vous risquez de vous verrouiller dehors. "
            "Éditez /etc/ssh/sshd_config puis rechargez : sudo systemctl reload ssh"
        ),
    )


# --------------------------------------------------------------------------- #
# 5. DNS chiffré (systemd-resolved)
# --------------------------------------------------------------------------- #
def check_dns_encryption(runner: CommandRunner) -> Finding:
    title = "DNS chiffré (DNS-over-TLS)"
    sev = Severity.MEDIUM
    if not runner.has_command("resolvectl"):
        return _skip("dns_encryption", title, "systemd-resolved (resolvectl) absent.", sev)
    result = runner.run(["resolvectl", "status"])
    if not result.ok:
        return Finding("dns_encryption", title, Status.ERROR, sev, detail=result.stderr or "resolvectl a échoué")
    if "dnsovertls=yes" in result.out.lower().replace(" ", ""):
        return Finding("dns_encryption", title, Status.PASS, sev, detail="DNS-over-TLS activé.")
    return Finding(
        "dns_encryption", title, Status.WARN, sev,
        detail="Le DNS n'est pas chiffré (DNS-over-TLS désactivé).",
        remediation=(
            "Activer DNS-over-TLS dans /etc/systemd/resolved.conf "
            "(DNSOverTLS=yes et un DNS respectueux de la vie privée), puis "
            "redémarrer systemd-resolved."
        ),
        fix_commands=[
            "sudo mkdir -p /etc/systemd/resolved.conf.d",
            "sudo tee /etc/systemd/resolved.conf.d/dot.conf >/dev/null <<'EOF'\n[Resolve]\nDNSOverTLS=yes\nEOF",
            "sudo systemctl restart systemd-resolved",
        ],
        requires_root=True,
    )


# --------------------------------------------------------------------------- #
# 6. Secure Boot
# --------------------------------------------------------------------------- #
def check_secure_boot(runner: CommandRunner) -> Finding:
    title = "Secure Boot (UEFI)"
    sev = Severity.LOW
    if not runner.has_command("mokutil"):
        return _skip("secure_boot", title, "mokutil absent.", sev)
    result = runner.run(["mokutil", "--sb-state"])
    if not result.ok:
        return _skip("secure_boot", title, "État Secure Boot indisponible (BIOS hérité ?).", sev)
    if "enabled" in result.out.lower():
        return Finding("secure_boot", title, Status.PASS, sev, detail="Secure Boot activé.")
    return Finding(
        "secure_boot", title, Status.WARN, sev,
        detail="Secure Boot désactivé.",
        remediation="Activer Secure Boot dans le firmware UEFI (réglage BIOS, pas automatisable ici).",
    )


# --------------------------------------------------------------------------- #
# 7. Comptes sans mot de passe (nécessite root pour lire /etc/shadow)
# --------------------------------------------------------------------------- #
def check_empty_passwords(runner: CommandRunner) -> Finding:
    title = "Comptes sans mot de passe"
    sev = Severity.HIGH
    result = runner.run(["getent", "shadow"])
    if not result.ok or not result.out:
        return _skip("empty_passwords", title, "Lecture de la base shadow impossible (root requis).", sev)
    empty = [
        line.split(":", 1)[0]
        for line in result.stdout.splitlines()
        if len(line.split(":")) > 1 and line.split(":")[1] == ""
    ]
    if empty:
        return Finding(
            "empty_passwords", title, Status.FAIL, sev,
            detail=f"Comptes avec mot de passe vide : {', '.join(empty)}.",
            remediation="Définir un mot de passe (passwd <utilisateur>) ou verrouiller le compte (passwd -l).",
        )
    return Finding("empty_passwords", title, Status.PASS, sev, detail="Aucun compte sans mot de passe.")


ALL_CHECKS: list[Check] = [
    check_disk_encryption,
    check_firewall,
    check_auto_updates,
    check_ssh_hardening,
    check_dns_encryption,
    check_secure_boot,
    check_empty_passwords,
]
