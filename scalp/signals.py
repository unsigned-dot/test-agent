"""Règles de RÉFÉRENCE et benchmarks naïfs (section 4 du cahier des charges).

Problème résolu : fournir les points de comparaison que le modèle de l'étape 1 doit battre
hors échantillon, après coûts. Ce ne sont PAS des stratégies candidates à la mise en
production. Chaque fonction renvoie un vecteur d'ENTRÉES int8 (+1 long, -1 short, 0 rien)
calculé sur la barre k avec les seules colonnes causales de features.py ; le moteur
d'exécution gère latence, prix, sorties et risque. Grilles volontairement petites : chaque
combinaison compte dans la correction pour essais multiples (Deflated Sharpe Ratio).
« Ne rien faire » = aucune entrée (PnL nul) ; buy-and-hold est calculé à part (pas un signal).
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd


@dataclass
class Strategy:
    name: str
    hypothesis: str
    fn: Callable[[pd.DataFrame, dict], np.ndarray]
    grid: dict[str, list] = field(default_factory=dict)
    needs: tuple[str, ...] = ()        # colonnes requises (sinon règle indisponible)
    exits: dict | None = None          # surcharge des sorties (None = [exits] de la config)

    def param_sets(self) -> list[dict]:
        keys = list(self.grid)
        return [dict(zip(keys, v)) for v in itertools.product(*(self.grid[k] for k in keys))] or [{}]

    def available(self, df: pd.DataFrame) -> bool:
        return all(c in df.columns and df[c].notna().any() for c in self.needs)

    def signal(self, df: pd.DataFrame, p: dict, freq_s: float | None = None) -> np.ndarray:
        """Entrées int8 ; les barres de la première heure (warmup) sont forcées à 0."""
        s = np.asarray(self.fn(df, p), dtype=np.int8).copy()
        s[:warmup_bars(df, freq_s)] = 0
        return s


def warmup_bars(df: pd.DataFrame, freq_s: float | None = None) -> int:
    """1 h de barres (statistiques glissantes 1 h encore incomplètes avant)."""
    if freq_s is None:
        d = np.diff(df["ts"].to_numpy(np.int64))
        freq_s = float(np.median(d)) / 1000 if d.size else 5.0
    return int(np.ceil(3600 / max(freq_s, 1e-9)))


def _sgn(cond_long, cond_short) -> np.ndarray:
    cl = np.asarray(cond_long, dtype=bool)
    cs = np.asarray(cond_short, dtype=bool)
    return np.where(cl & ~cs, 1, np.where(cs & ~cl, -1, 0)).astype(np.int8)


# R1 — retournement après épuisement : mouvement extrême + volume anormal + baisse d'OI
# (flux forcé / liquidations qui se tarit) -> on joue le retour
def exhaustion_reversal(df, p):
    w = p["w"]
    z, vz, oi = df[f"z_{w}"], df[f"volz_{w}"], df["oi_chg_5m"]
    cond = (vz > p["vz"]) & (oi < -p["oi_drop_bps"])
    return _sgn(cond & (z < -p["z"]), cond & (z > p["z"]))


# R2 — continuation de flux : déséquilibre agresseur persistant, confirmé par le prix et le volume
def flow_continuation(df, p):
    w = p["w"]
    d, r, vz = df[f"delta_{w}"], df[f"ret_{w}"], df[f"volz_{w}"]
    return _sgn((d > p["th"]) & (r > 0) & (vz > p["vz"]), (d < -p["th"]) & (r < 0) & (vz > p["vz"]))


# ---- benchmarks naïfs -------------------------------------------------------------
def naive_momentum(df, p):
    r = df[f"ret_{p['w']}"]
    return _sgn(r > p["th"], r < -p["th"])


def naive_reversion(df, p):
    r = df[f"ret_{p['w']}"]
    return _sgn(r < -p["th"], r > p["th"])


def random_entries(df, p):
    rng = np.random.default_rng(p["seed"])
    u = rng.random(len(df))
    side = np.where(rng.random(len(df)) < 0.5, 1, -1)
    return np.where(u < p["prob"], side, 0).astype(np.int8)


REFERENCES: list[Strategy] = [
    Strategy("R1_exhaustion_reversal",
             "Mouvement extrême à fort volume avec baisse d'OI (flux forcé épuisé) -> retournement",
             exhaustion_reversal, {"w": [12, 60], "z": [3.0, 4.0], "vz": [2.0], "oi_drop_bps": [0.0, 10.0]},
             needs=("oi_chg_5m",)),
    Strategy("R2_flow_continuation",
             "Déséquilibre agresseur persistant confirmé par prix et volume -> continuation",
             flow_continuation, {"w": [12, 60], "th": [0.3, 0.5], "vz": [1.5]}),
]

BENCHMARKS: list[Strategy] = [
    Strategy("B_naive_momentum", "benchmark naïf", naive_momentum, {"w": [12], "th": [10.0]}),
    Strategy("B_naive_reversion", "benchmark naïf", naive_reversion, {"w": [12], "th": [10.0]}),
    Strategy("B_random_entries", "benchmark (hasard, mêmes sorties)", random_entries,
             {"prob": [0.002], "seed": [1, 2, 3]}),
]

STRATEGIES = REFERENCES          # alias de compatibilité


def by_name(name: str) -> Strategy:
    for s in REFERENCES + BENCHMARKS:
        if s.name == name:
            return s
    raise KeyError(name)
