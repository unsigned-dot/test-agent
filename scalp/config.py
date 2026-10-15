"""Chargement de la configuration TOML et sélection du profil de coûts par plateforme.

Problème résolu : une seule source de paramètres (config/default.toml), lisible avec la
bibliothèque standard (tomllib, Python >= 3.11), et un accès unique au profil de coûts
de la plateforme d'exécution ([venue].execution -> [venues.<nom>]).
"""
from __future__ import annotations

import copy
import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "default.toml"

VENUE_KEYS = ("maker_fee_bps", "taker_fee_bps", "tick_size", "lot_size", "extra_slippage_bps",
              "impact_bps_per_btc", "funding_interval_hours", "funding_accrual")


def load_config(path: str | Path = DEFAULT_CONFIG, overrides: dict | None = None) -> dict[str, Any]:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    if overrides:
        cfg = deep_update(cfg, overrides)
    return cfg


def deep_update(base: dict, upd: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in upd.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_update(out[k], v)
        else:
            out[k] = v
    return out


def venue_profile(cfg: dict, venue: str | None = None) -> dict[str, Any]:
    """Profil de coûts de la plateforme `venue` (défaut : [venue].execution)."""
    name = venue or cfg["venue"]["execution"]
    try:
        prof = dict(cfg["venues"][name])
    except KeyError:
        raise KeyError(f"profil de plateforme inconnu : {name!r} (sections [venues.*])") from None
    missing = [k for k in VENUE_KEYS if k not in prof]
    if missing:
        raise KeyError(f"[venues.{name}] incomplet, clés manquantes : {missing}")
    if prof["funding_accrual"] not in ("discrete", "continuous"):
        raise ValueError(f"[venues.{name}].funding_accrual doit valoir 'discrete' ou 'continuous'")
    prof["name"] = name
    return prof


def entry_latency_ms(cfg: dict) -> int:
    """Latence d'ENTRÉE effective (fin de barre -> ordre arrivé), identique au robot live : celui-ci
    ne clôt la barre qu'après [bars].close_grace_ms, puis l'ordre met [costs].latency_ms à arriver."""
    return int(cfg["bars"]["close_grace_ms"]) + int(cfg["costs"]["latency_ms"])


def with_entry_latency(cfg: dict, total_ms: int) -> dict:
    """Copie de `cfg` dont la latence d'entrée effective vaut `total_ms` (sensibilités). La grâce de
    clôture est gardée si possible ; sous elle (point optimiste), grâce = total et envoi = 0."""
    grace = min(int(cfg["bars"]["close_grace_ms"]), int(total_ms))
    return deep_update(cfg, {"bars": {"close_grace_ms": grace},
                             "costs": {"latency_ms": int(total_ms) - grace}})


def config_hash(obj: Any) -> str:
    """Empreinte stable d'une configuration (utilisée pour verrouiller le test final)."""
    s = json.dumps(obj, sort_keys=True, default=str)
    return hashlib.sha256(s.encode()).hexdigest()[:16]
