"""Métriques de performance et incertitude statistique.

Problème résolu : juger un backtest sur ses chiffres nets ET sur leur incertitude, sans se
laisser tromper par la dépendance temporelle ni par le nombre d'essais.
- Bootstrap par blocs de jours (dépendance temporelle intra-journalière préservée).
- Probabilistic / Deflated Sharpe Ratio (Bailey & López de Prado, 2014) pour tenir
  compte du nombre d'essais (data snooping).
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy import stats

from .config import venue_profile

DAY_MS = 86_400_000


def summarize(tr: pd.DataFrame, initial_equity: float, n_days: float | None = None) -> dict:
    if tr is None or len(tr) == 0:
        return {"n_trades": 0, "net_pnl": 0.0, "exp_net_bps": float("nan")}
    net, gross = tr["net_pnl"], tr["gross_pnl"]
    wins, losses = net[net > 0], net[net <= 0]
    eq = initial_equity + net.cumsum()
    peak = np.maximum.accumulate(np.concatenate([[initial_equity], eq.values]))[1:]
    dd = (eq - peak) / peak
    hold = (tr["exit_ts"] - tr["entry_ts"]) / 1000
    if n_days is None:
        n_days = max(1.0, (tr["exit_ts"].max() - tr["entry_ts"].min()) / DAY_MS)
    out = {
        "n_trades": int(len(tr)),
        "trades_per_day": len(tr) / n_days,
        "exp_net_bps": float(tr["net_bps"].mean()),
        "exp_gross_bps": float(tr["gross_bps"].mean()),
        "exp_ideal_bps": float(tr["ideal_bps"].mean()) if "ideal_bps" in tr else float("nan"),
        "median_net_bps": float(tr["net_bps"].median()),
        "net_pnl": float(net.sum()),
        "gross_pnl": float(gross.sum()),
        "fees": float(tr["fees"].sum()),
        "funding": float(tr["funding"].sum()),
        "net_return_pct": float(net.sum() / initial_equity * 100),
        "profit_factor": float(wins.sum() / -losses.sum()) if losses.sum() < 0 else float("inf"),
        "win_rate": float((net > 0).mean()),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "max_dd_realized_pct": float(dd.min() * 100),
        "avg_hold_s": float(hold.mean()),
        "median_hold_s": float(hold.median()),
        "turnover_x": float(2 * tr["notional"].sum() / initial_equity),
        "long_n": int((tr["side"] > 0).sum()),
        "short_n": int((tr["side"] < 0).sum()),
        "long_exp_net_bps": float(tr.loc[tr["side"] > 0, "net_bps"].mean()) if (tr["side"] > 0).any() else float("nan"),
        "short_exp_net_bps": float(tr.loc[tr["side"] < 0, "net_bps"].mean()) if (tr["side"] < 0).any() else float("nan"),
        "exit_reasons": tr["exit_reason"].value_counts().to_dict(),
    }
    out["pnl_per_hour_traded"] = float(net.sum() / max(hold.sum() / 3600, 1e-9))
    return out


def slippage_sensitivity(tr: pd.DataFrame, extra_bps=(0.0, 0.5, 1.0, 2.0, 5.0)) -> dict:
    """Espérance nette si chaque jambe taker coûte `x` bps de plus (approx. linéaire)."""
    if len(tr) == 0:
        return {}
    legs = tr["taker_legs"] if "taker_legs" in tr else 2
    return {f"+{x}bps": float((tr["net_bps"] - x * legs).mean()) for x in extra_bps}


def fee_sensitivity(tr: pd.DataFrame, cfg: dict, fee_sets=((2.0, 5.0), (1.6, 4.0), (0.0, 2.5), (0.0, 0.0))) -> dict:
    """Espérance nette sous d'autres grilles maker/taker (bps)."""
    if len(tr) == 0:
        return {}
    v = venue_profile(cfg)
    base_m, base_t = float(v["maker_fee_bps"]), float(v["taker_fee_bps"])
    legs_t = tr["taker_legs"] if "taker_legs" in tr else 2
    legs_m = 2 - legs_t
    out = {}
    for m, t in fee_sets:
        adj = (base_t - t) * legs_t + (base_m - m) * legs_m
        out[f"maker{m}/taker{t}"] = float((tr["net_bps"] + adj).mean())
    return out


def daily_pnl(tr: pd.DataFrame, start_ms: int, end_ms: int) -> pd.Series:
    days = np.arange(start_ms // DAY_MS, end_ms // DAY_MS + 1)
    s = pd.Series(0.0, index=days)
    if len(tr):
        g = tr.groupby(tr["exit_ts"] // DAY_MS)["net_pnl"].sum()
        common = g.index.intersection(s.index)
        s.loc[common] += g.loc[common]
    return s


def block_bootstrap_mean(x: np.ndarray, block: int, n: int, seed: int) -> tuple[float, float, float]:
    """IC 95 % de la moyenne par bootstrap à blocs circulaires + p-value unilatérale (H0 : moyenne <= 0)."""
    x = np.asarray(x, float)
    if x.size < 2:
        return float("nan"), float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    T = x.size
    block = max(1, min(block, T))
    nb = math.ceil(T / block)
    means = np.empty(n)
    xc = x - x.mean()                   # pour la p-value : distribution sous H0 centrée
    null_means = np.empty(n)
    for b in range(n):
        st = rng.integers(0, T, nb)
        idx = (st[:, None] + np.arange(block)[None, :]).ravel()[:T] % T
        means[b] = x[idx].mean()
        null_means[b] = xc[idx].mean()
    lo, hi = np.percentile(means, [2.5, 97.5])
    p = float((null_means >= x.mean()).mean())
    return float(lo), float(hi), p


MIN_DAYS_CI = 20          # défaut de [bootstrap].min_days


def trade_bootstrap_by_day(tr: pd.DataFrame, n: int, seed: int, block_days: int = 1,
                           min_days: int = MIN_DAYS_CI) -> tuple[float, float, float]:
    """IC 95 % de l'espérance nette par trade (bps) par bootstrap de BLOCS de jours consécutifs
    (blocs circulaires de `block_days` jours ayant des trades), + p-value unilatérale (H0 : ≤ 0).

    Non calculable (NaN) si moins de `min_days` jours distincts portent des trades : avec
    quelques jours, le rééchantillonnage ne voit que la variabilité INTRA-journalière et
    l'intervalle est faussement étroit (ex. 2 jours -> IC de 0,2 bp de large).
    """
    if tr is None or len(tr) < 5:
        return float("nan"), float("nan"), float("nan")
    g = tr.groupby(tr["entry_ts"] // DAY_MS)["net_bps"].agg(["sum", "size"]).sort_index()
    k = len(g)
    if k < max(2, int(min_days)):
        return float("nan"), float("nan"), float("nan")
    sums, cnts = g["sum"].to_numpy(float), g["size"].to_numpy(float)
    obs = sums.sum() / cnts.sum()
    rng = np.random.default_rng(seed)
    block = max(1, min(int(block_days), k))
    nb = math.ceil(k / block)
    bs = np.empty(n)
    for b in range(n):
        st = rng.integers(0, k, nb)
        i = (st[:, None] + np.arange(block)[None, :]).ravel()[:k] % k
        bs[b] = sums[i].sum() / max(cnts[i].sum(), 1.0)
    lo, hi = np.percentile(bs, [2.5, 97.5])
    p = float(((bs - bs.mean()) >= obs).mean())       # H0 : espérance nulle (bootstrap centré)
    return float(lo), float(hi), p


def sharpe(daily: pd.Series, equity: float, periods: int = 365) -> float:
    r = daily / equity
    sd = r.std(ddof=1)
    return float(r.mean() / sd * math.sqrt(periods)) if sd > 0 else float("nan")


def probabilistic_sharpe(sr_period: float, T: int, skew: float, kurt: float, sr_star: float = 0.0) -> float:
    """PSR : P(SR vrai > sr_star) — Sharpe NON annualisé (par période)."""
    if T < 3 or not np.isfinite(sr_period):
        return float("nan")
    den = math.sqrt(max(1e-12, 1 - skew * sr_period + (kurt - 1) / 4 * sr_period ** 2))
    return float(stats.norm.cdf((sr_period - sr_star) * math.sqrt(T - 1) / den))


def deflated_sharpe(daily: pd.Series, equity: float, n_trials: int, sr_trials_var: float,
                    min_days: int = MIN_DAYS_CI) -> float:
    """DSR : PSR avec seuil = Sharpe maximal attendu parmi n_trials essais sans avantage.

    NaN (non calculable, donc critère non rempli) si moins de `min_days` jours, ou si la
    variance des Sharpe des essais est inconnue alors que plusieurs essais ont eu lieu
    (sinon le seuil tomberait à 0 et le DSR se réduirait au PSR, sans aucune déflation).
    """
    r = (daily / equity).values
    if r.size < max(3, int(min_days)) or r.std(ddof=1) == 0:
        return float("nan")
    n = int(n_trials)
    if n > 1 and not (np.isfinite(sr_trials_var) and sr_trials_var > 0):
        return float("nan")
    sr = r.mean() / r.std(ddof=1)
    if n <= 1:
        emax = 0.0
    else:
        g = 0.5772156649
        emax = math.sqrt(sr_trials_var) * ((1 - g) * stats.norm.ppf(1 - 1 / n)
                                           + g * stats.norm.ppf(1 - 1 / (n * math.e)))
    return probabilistic_sharpe(sr, r.size, float(stats.skew(r)), float(stats.kurtosis(r, fisher=False)), emax)


def mtm_equity(tr: pd.DataFrame, bars_ts: np.ndarray, close: np.ndarray, initial_equity: float,
               step: int = 12) -> pd.Series:
    """Courbe mark-to-market (réalisé + latent au cours de clôture), échantillonnée tous les `step` barres."""
    idx = np.arange(0, len(bars_ts), step)
    t, c = bars_ts[idx], close[idx]
    eq = np.full(idx.size, initial_equity, float)
    if len(tr):
        realized = np.zeros(idx.size)
        exit_pos = np.searchsorted(t, tr["exit_ts"].values, side="left")
        np.add.at(realized, np.clip(exit_pos, 0, idx.size - 1), tr["net_pnl"].values)
        eq += np.cumsum(realized)
        for _, row in tr.iterrows():
            a = np.searchsorted(t, row.entry_ts, side="right")
            b = np.searchsorted(t, row.exit_ts, side="left")
            if b > a:
                eq[a:b] += row.side * (c[a:b] - row.entry_px) * row.qty
    return pd.Series(eq, index=pd.to_datetime(t, unit="ms", utc=True))


def max_drawdown_pct(eq: pd.Series) -> float:
    if len(eq) == 0:
        return float("nan")
    peak = eq.cummax()
    return float(((eq - peak) / peak).min() * 100)


def breakdown(tr: pd.DataFrame, by: str) -> pd.DataFrame:
    if len(tr) == 0 or by not in tr:
        return pd.DataFrame()
    g = tr.groupby(by)
    return pd.DataFrame({"n": g.size(), "exp_net_bps": g["net_bps"].mean(),
                         "net_pnl": g["net_pnl"].sum(), "win_rate": g["net_pnl"].apply(lambda s: (s > 0).mean())})
