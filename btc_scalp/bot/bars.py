"""Construction incrémentale des barres de 5 s et des facteurs, en temps réel.

Problème résolu : produire en direct EXACTEMENT les mêmes barres et facteurs que la
recherche, sans dupliquer sa logique. Chaque barre close est calculée par
`scalp.features.make_bars` appliqué aux seuls trades de la barre ; seules les valeurs
« reportées » (close et proxys bid/ask d'une barre sans trade du côté concerné) sont
complétées depuis la barre précédente, comme le fait make_bars sur l'historique entier.
Les facteurs sont calculés par `scalp.features.add_features` sur une fenêtre glissante.

Une barre [t, t+F) est close UNIQUEMENT quand l'horloge (serveur) atteint t+F+grace, même si
des trades de la barre suivante sont déjà arrivés : un trade en désordre arrivé pendant la
grâce est donc encore compté. Un trade arrivé après la clôture de sa barre est compté « en
retard » et ignoré (jamais de réécriture du passé). Les trades d'une barre sont triés par
horodatage avant `make_bars` (qui refuse un flux non trié : sans ce tri, un seul trade en
désordre ferait planter le robot).

Fenêtre : `window_bars` doit couvrir la plus longue fenêtre des facteurs (vol_regime :
médiane 24 h de la volatilité 1 h => 25 h) pour que la dernière ligne soit identique au
calcul sur l'historique complet (voir `required_window_bars`).
"""
from __future__ import annotations

import math
from collections import deque

import numpy as np
import pandas as pd

from scalp.features import add_features, make_bars

BAR_COLUMNS = ["ts", "open", "high", "low", "close", "vol", "buy_vol", "sell_vol", "n", "n_buy",
               "notional", "big_signed", "bid_proxy", "ask_proxy", "vwap"]


def required_window_bars(freq_s: int) -> int:
    """Barres nécessaires pour que add_features donne sur la fenêtre la même dernière ligne que
    sur tout l'historique : médiane 24 h (rv_1h_med_1d) d'une volatilité 1 h (rv_1h) + 1 rendement."""
    one_h = max(1, int(3600 / freq_s))
    one_d = max(1, int(86400 / freq_s))
    return one_d + one_h


class BarBuilder:
    def __init__(self, freq_s: int = 5, window_bars: int = 18000, grace_ms: int = 300,
                 large_qty: float = 2.0):
        self.freq_s = int(freq_s)
        self.F = int(freq_s * 1000)
        self.grace_ms = int(grace_ms)
        self.large_qty = float(large_qty)
        self.bars: deque[dict] = deque(maxlen=int(window_bars))
        self.cur_start: int | None = None     # début de la plus ancienne barre non close (ms)
        self._pending: list[tuple[int, float, float, bool]] = []   # trades des barres non closes
        self.late_trades = 0
        self.last_trade_ts: int | None = None

    # ------------------------------------------------------------ entrées
    def add_trade(self, ts: int, px: float, qty: float, taker_buy: bool) -> bool:
        """Ajoute un trade. Renvoie False s'il arrive trop tard (barre déjà close)."""
        start = (int(ts) // self.F) * self.F
        if self.cur_start is None:
            self.cur_start = start
        if start < self.cur_start:
            self.late_trades += 1
            return False
        self._pending.append((int(ts), float(px), float(qty), not taker_buy))
        self.last_trade_ts = int(ts) if self.last_trade_ts is None else max(self.last_trade_ts, int(ts))
        return True

    def close_until(self, now_ms: int) -> list[dict]:
        """Clôt toutes les barres dont la fin + grace <= now_ms. Renvoie les barres closes."""
        if self.cur_start is None:
            return []
        limit = ((int(now_ms) - self.grace_ms) // self.F) * self.F     # 1er début de barre encore ouvert
        out = []
        while self.cur_start < limit:
            end = self.cur_start + self.F
            mine = [t for t in self._pending if t[0] < end]
            if mine:
                self._pending = [t for t in self._pending if t[0] >= end]
            row = self._close_current(sorted(mine, key=lambda t: t[0]))     # tri stable par horodatage
            if "empty_start" not in row:
                out.append(row)
            self.cur_start = end
        return out

    # ------------------------------------------------------------ interne
    def _close_current(self, trades: list[tuple[int, float, float, bool]]) -> dict:
        prev = self.bars[-1] if self.bars else None
        if trades:
            ts, px, qty, bm = zip(*trades)
            t = {"ts": np.asarray(ts, np.int64), "px": np.asarray(px, np.float64),
                 "qty": np.asarray(qty, np.float64), "bm": np.asarray(bm, bool)}
            row = make_bars(t, self.freq_s, self.large_qty).iloc[0].to_dict()
            row["ts"] = int(row["ts"])
            for c in ("bid_proxy", "ask_proxy"):
                if prev is not None and (row[c] is None or (isinstance(row[c], float) and math.isnan(row[c]))):
                    row[c] = prev[c]
        else:
            if prev is None:
                # pas encore de prix connu : rien à émettre de significatif
                return {"ts": self.cur_start + self.F, "empty_start": True}
            c = prev["close"]
            row = {"ts": self.cur_start + self.F, "open": c, "high": c, "low": c, "close": c,
                   "vol": 0.0, "buy_vol": 0.0, "sell_vol": 0.0, "n": 0.0, "n_buy": 0.0,
                   "notional": 0.0, "big_signed": 0.0, "bid_proxy": prev["bid_proxy"],
                   "ask_proxy": prev["ask_proxy"], "vwap": c}
        row = {k: row[k] for k in BAR_COLUMNS}
        self.bars.append(row)
        return row

    # ------------------------------------------------------------ sorties
    def frame(self) -> pd.DataFrame:
        return pd.DataFrame.from_records(list(self.bars), columns=BAR_COLUMNS)

    def features(self, funding_interval_h: float) -> pd.DataFrame:
        """Facteurs de recherche (scalp.features.add_features) sur la fenêtre glissante.
        `funding_interval_h` : intervalle du profil de la plateforme (comme la recherche)."""
        return add_features(self.frame(), self.freq_s, funding_interval_h=funding_interval_h)

    def last_close(self) -> float | None:
        return self.bars[-1]["close"] if self.bars else None
