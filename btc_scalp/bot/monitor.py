"""Surveillance : métriques de dérive, santé du flux, alertes dédoublonnées.

Problème résolu : savoir, sans regarder le serveur, si (1) le flux est sain (retard,
trous, reconnexions), (2) le modèle d'exécution du backtest reste PESSIMISTE par rapport
au marché réel observé (spread du carnet L2 Kraken vs 1 tick supposé ; prix simulé de
l'entrée vs prix obtenu en balayant le carnet réel), (3) les résultats paper dérivent des
attentes. Les alertes ne sont envoyées qu'aux changements d'état (pas de rafale).
"""
from __future__ import annotations

import statistics
from collections import deque


class Monitor:
    def __init__(self, cfg: dict, notify):
        m = cfg["monitor"]
        self.margin_bps = float(m["slippage_alert_margin_bps"])
        self.min_trades = int(m["min_trades_for_drift"])
        self.expected_spread_ticks = float(m["expected_spread_ticks"])
        self.notify = notify
        self.lag_ms: deque[int] = deque(maxlen=200)
        self.spread_ticks: deque[float] = deque(maxlen=5000)
        self.model_vs_book_bps: deque[float] = deque(maxlen=500)   # >0 : modèle plus pessimiste que le carnet
        self.counters: dict[str, int] = {}
        self._active: dict[str, str] = {}

    def inc(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n

    # ---------------------------------------------------------- mesures
    def add_lag(self, ms: int) -> None:
        self.lag_ms.append(int(ms))

    def median_lag(self) -> float | None:
        return statistics.median(self.lag_ms) if len(self.lag_ms) >= 5 else None

    def add_spread(self, ticks: float) -> None:
        self.spread_ticks.append(ticks)

    def add_entry_comparison(self, side: int, model_px: float, book_px: float | None) -> None:
        """Compare le prix d'entrée simulé au prix obtenu en balayant le carnet réel."""
        if book_px is None or book_px <= 0:
            return
        diff = side * (model_px - book_px) / book_px * 1e4      # >0 : le modèle paie plus cher (prudent)
        self.model_vs_book_bps.append(diff)
        if len(self.model_vs_book_bps) >= self.min_trades:
            mean = statistics.fmean(self.model_vs_book_bps)
            self.set_condition("cost_model_optimistic", mean < -self.margin_bps,
                               f"Le modèle de coûts paper est PLUS OPTIMISTE que le carnet Kraken réel "
                               f"de {-mean:.2f} bp en moyenne (seuil {self.margin_bps} bp) : revoir "
                               "[venues.kraken] avant toute conclusion.")

    def spread_summary(self) -> dict:
        if not self.spread_ticks:
            return {}
        s = sorted(self.spread_ticks)
        return {"median_ticks": s[len(s) // 2], "p90_ticks": s[int(0.9 * (len(s) - 1))],
                "share_above_expected": sum(x > self.expected_spread_ticks for x in s) / len(s)}

    # ---------------------------------------------------------- alertes
    def set_condition(self, key: str, active: bool, msg: str, level: str = "WARN") -> bool:
        """Alerte au début et à la fin d'une condition. Renvoie True si l'état a changé."""
        was = key in self._active
        if active and not was:
            self._active[key] = msg
            self.notify(level, key, msg)
            return True
        if not active and was:
            del self._active[key]
            self.notify("INFO", key, f"Rétabli : {key}")
            return True
        return False

    def active(self) -> dict[str, str]:
        return dict(self._active)
