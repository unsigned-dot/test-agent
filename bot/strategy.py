"""Stratégies branchables du robot.

Problème résolu : brancher une stratégie sans toucher au moteur, tout en garantissant
qu'AUCUNE entrée n'est possible tant qu'aucune stratégie n'a été validée (section 7).

- "none" (défaut) : ne renvoie jamais de signal.
- "dry_signal" : applique une règle de RÉFÉRENCE de scalp/signals.py (benchmark naïf)
  uniquement pour tester la mécanique paper (ordres, stops, reprise...). Ses trades sont
  marqués `dry` dans l'état et dans les alertes ; ils ne prouvent RIEN sur la rentabilité.
- Une future stratégie validée (modèle de l'étape 1) implémentera `Strategy.decide` et
  sera enregistrée dans `make_strategy` après autorisation d'Hugo.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class Signal:
    side: int                         # +1 long, -1 short
    mult: float = 1.0                 # réduction de taille demandée par la stratégie (<= 1)
    expected_gain_frac: float | None = None
    note: str = ""


class Strategy:
    name = "base"
    dry = False

    def decide(self, feats: pd.DataFrame) -> Signal | None:
        """`feats` : facteurs jusqu'à la dernière barre close incluse (rien après)."""
        raise NotImplementedError


class NoStrategy(Strategy):
    name = "none"

    def decide(self, feats: pd.DataFrame) -> Signal | None:
        return None


class DrySignal(Strategy):
    """Règle de référence (scalp.signals) évaluée sur la DERNIÈRE barre seulement."""
    dry = True

    def __init__(self, rule: str, params: dict):
        from scalp.signals import by_name
        self.rule = by_name(rule)
        self.params = dict(params)
        self.name = f"dry_signal:{rule}"

    def decide(self, feats: pd.DataFrame) -> Signal | None:
        if feats.empty:
            return None
        s = int(self.rule.fn(feats.tail(1), self.params)[-1])
        return Signal(s, note=self.rule.name) if s else None


def make_strategy(cfg: dict) -> Strategy:
    s = cfg["strategy"]
    name = s.get("name", "none")
    if name == "none":
        return NoStrategy()
    if name == "dry_signal":
        return DrySignal(s["dry_rule"], dict(s.get("dry_params", {})))
    raise SystemExit(f"stratégie inconnue ou non validée : {name!r} (seules 'none' et 'dry_signal' existent)")
