"""Exécution de commandes système et détection de l'environnement.

Toute interaction avec le système passe par :class:`CommandRunner`, ce qui rend
les vérifications entièrement testables : il suffit d'injecter un faux runner.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def out(self) -> str:
        """stdout nettoyé des espaces de bord."""
        return self.stdout.strip()


class CommandRunner:
    """Exécute des commandes en lecture seule et met en cache ``which``.

    Les vérifications ne lancent que des commandes d'inspection. Les commandes
    de remédiation sont exécutées séparément par le module ``apply``.
    """

    def __init__(self, *, default_timeout: float = 10.0) -> None:
        self._default_timeout = default_timeout
        self._which_cache: dict[str, bool] = {}

    def has_command(self, name: str) -> bool:
        if name not in self._which_cache:
            self._which_cache[name] = shutil.which(name) is not None
        return self._which_cache[name]

    def run(self, command: list[str], *, timeout: float | None = None) -> CommandResult:
        try:
            completed = subprocess.run(  # noqa: S603 - commandes fixes, pas d'entrée shell
                command,
                capture_output=True,
                text=True,
                timeout=timeout or self._default_timeout,
                check=False,
            )
        except FileNotFoundError:
            return CommandResult(127, "", f"commande introuvable : {command[0]}")
        except subprocess.TimeoutExpired:
            return CommandResult(124, "", f"délai dépassé : {' '.join(command)}")
        except OSError as exc:  # pragma: no cover - dépend du système
            return CommandResult(1, "", str(exc))
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def detect_os() -> dict[str, str]:
    """Retourne des infos sur l'OS (système, distribution, famille)."""
    info = {"system": platform.system(), "distro": "", "id_like": ""}
    if info["system"] != "Linux":
        return info
    try:
        with open("/etc/os-release", encoding="utf-8") as handle:
            data = dict(
                line.rstrip().split("=", 1)
                for line in handle
                if "=" in line and not line.startswith("#")
            )
    except OSError:
        return info
    info["distro"] = data.get("ID", "").strip('"')
    info["id_like"] = data.get("ID_LIKE", "").strip('"')
    return info


def is_debian_like(os_info: dict[str, str]) -> bool:
    return os_info.get("distro") in ("debian", "ubuntu") or "debian" in os_info.get("id_like", "")
