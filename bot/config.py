"""Chargement et vérification de la configuration du robot.

Problème résolu : garantir AVANT toute connexion que (1) le mode est "paper" — sinon le
programme refuse de démarrer —, (2) les limites de risque/coûts/sorties viennent
uniquement de config/default.toml (source versionnée), (3) aucun secret n'est dans les
fichiers du dépôt (token Telegram lu dans l'environnement ou un .env externe).
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from scalp.config import config_hash, load_config, venue_profile

BOT_DIR = Path(__file__).resolve().parent
DEFAULT_BOT_CONFIG = BOT_DIR / "config.toml"
ALLOWED_MODES = ("paper",)
FORBIDDEN_SECTIONS = ("risk", "costs", "exits", "venues", "venue")


class ModeError(SystemExit):
    """Mode non autorisé : arrêt immédiat (code 2)."""

    def __init__(self, msg: str):
        super().__init__(2)
        self.msg = msg

    def __str__(self) -> str:
        return self.msg


class ConfigError(SystemExit):
    def __init__(self, msg: str):
        super().__init__(2)
        self.msg = msg

    def __str__(self) -> str:
        return self.msg


@dataclass
class BotConfig:
    bot: dict           # contenu de bot/config.toml
    research: dict      # contenu de config/default.toml (risque, coûts, sorties, barres)
    venue: dict         # profil de coûts Kraken (frais, tick, lot, slippage, funding)
    path: Path
    research_hash: str

    @property
    def mode(self) -> str:
        return self.bot["mode"]

    def storage_path(self, key: str) -> Path:
        p = Path(os.path.expanduser(self.bot["storage"][key]))
        return p if p.is_absolute() else (self.path.parent / p).resolve()


def check_mode(raw: dict) -> None:
    mode = raw.get("mode")
    if mode not in ALLOWED_MODES:
        raise ModeError(f"REFUS DE DÉMARRER : mode={mode!r}. Seul le mode 'paper' est implémenté ; "
                        "le passage en réel exige une autorisation explicite d'Hugo et un "
                        "connecteur réel qui n'existe pas dans ce code.")


def load_bot_config(path: str | Path = DEFAULT_BOT_CONFIG, overrides: dict | None = None) -> BotConfig:
    path = Path(path).resolve()
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    if overrides:
        from scalp.config import deep_update
        raw = deep_update(raw, overrides)
    check_mode(raw)
    bad = [s for s in FORBIDDEN_SECTIONS if s in raw]
    if bad:
        raise ConfigError(f"sections interdites dans {path.name} : {bad}. Les limites de risque, "
                          "coûts et sorties se modifient UNIQUEMENT dans config/default.toml "
                          "(changement versionné, autorisation d'Hugo).")
    rp = Path(os.path.expanduser(raw.get("research_config", "../config/default.toml")))
    if not rp.is_absolute():
        rp = (path.parent / rp).resolve()
    research = load_config(rp)
    venue = venue_profile(research, "kraken")
    for k in ("market", "feed", "bars", "strategy", "storage", "telegram", "monitor"):
        if k not in raw:
            raise ConfigError(f"section [{k}] manquante dans {path}")
    if "bar_close_grace_ms" in raw["feed"]:
        raise ConfigError("[feed].bar_close_grace_ms n'est plus lu ici : la grâce de clôture de barre est "
                          "[bars].close_grace_ms de config/default.toml (elle entre dans la latence "
                          "d'entrée du backtest ; une seule source).")
    if raw["market"]["product_id"] != "PF_XBTUSD":
        raise ConfigError("seul PF_XBTUSD est prévu par le cahier des charges")
    from .bars import required_window_bars
    freq = int(research["bars"]["freq_seconds"])
    need = required_window_bars(freq)
    if int(raw["bars"]["window_hours"] * 3600 / freq) < need:
        raise ConfigError(f"[bars].window_hours trop court : il faut >= {need * freq / 3600:g} h pour que "
                          "les facteurs live soient identiques à ceux de la recherche (vol_regime).")
    return BotConfig(raw, research, venue, path, config_hash(research))


def read_env_file(path: str | Path) -> dict[str, str]:
    """Lit un fichier CLE=valeur (lignes vides et # ignorées). Absent -> {}."""
    p = Path(os.path.expanduser(str(path)))
    out: dict[str, str] = {}
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def telegram_secrets(cfg: BotConfig) -> tuple[str | None, str | None]:
    """(token, chat_id) depuis l'environnement, sinon depuis le .env hors dépôt."""
    t = cfg.bot["telegram"]
    env = read_env_file(t.get("env_file", ""))
    token = os.environ.get(t["token_env"]) or env.get(t["token_env"])
    chat = os.environ.get(t["chat_id_env"]) or env.get(t["chat_id_env"])
    return token or None, chat or None
