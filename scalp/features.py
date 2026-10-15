"""Barres temporelles et facteurs — strictement causaux.

Problème résolu : construire, à partir des aggTrades, des barres de F secondes et des
facteurs (section 4 du cahier des charges) qui n'utilisent JAMAIS d'information postérieure
à l'instant de décision.

Convention : la barre k couvre [t0 + k*F, t0 + (k+1)*F) et est horodatée à sa FIN
(`ts` = instant où toute l'information de la barre est connue ; un trade à ts exactement
appartient à la barre suivante). Une décision prise sur la barre k n'est exécutée qu'après
ts_k + latence (execution.py). Toutes les fenêtres sont glissantes vers le passé. Les
données externes (metrics, funding, spot) sont jointes « en arrière » à leur instant de
disponibilité (metrics : + délai de publication).
Les fenêtres sont exprimées en BARRES (WINDOWS) ; les noms supposent F = 5 s.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

WINDOWS = (1, 6, 12, 60, 180)   # en barres (avec F=5 s : 5 s, 30 s, 1 min, 5 min, 15 min)


def make_bars(t: dict[str, np.ndarray], freq_s: int, large_qty: float = 2.0) -> pd.DataFrame:
    """Barres temporelles (ts = fin de barre, ms) : OHLC, volumes agresseurs, gros ordres, proxys bid/ask."""
    F = int(freq_s * 1000)
    ts, px, qty, bm = t["ts"], t["px"], t["qty"].astype(np.float64), t["bm"]
    if ts.size == 0:
        raise ValueError("aucun trade")
    if np.any(np.diff(ts) < 0):
        raise ValueError("trades non triés par horodatage")
    t0 = (int(ts[0]) // F) * F
    b = (ts - t0) // F
    nb = int(b[-1]) + 1
    buy = ~bm
    vol = np.bincount(b, qty, nb)
    bvol = np.bincount(b, qty * buy, nb)
    nt = np.bincount(b, minlength=nb).astype(np.float64)
    nbuy = np.bincount(b, buy.astype(np.float64), nb)
    notional = np.bincount(b, qty * px, nb)
    big = qty >= large_qty
    big_signed = np.bincount(b, np.where(big, np.where(buy, qty, -qty), 0.0), nb)

    nonempty = np.flatnonzero(nt > 0)
    starts = np.searchsorted(b, nonempty, side="left")
    ends = np.searchsorted(b, nonempty, side="right") - 1
    close = np.full(nb, np.nan)
    high = np.full(nb, np.nan)
    low = np.full(nb, np.nan)
    opn = np.full(nb, np.nan)
    close[nonempty] = px[ends]
    opn[nonempty] = px[starts]
    high[nonempty] = np.maximum.reduceat(px, starts)
    low[nonempty] = np.minimum.reduceat(px, starts)

    # proxys de meilleur bid/ask : dernier prix d'un trade vendeur-agresseur (bid) /
    # acheteur-agresseur (ask) observé jusqu'à la fin de la barre (incluse)
    def last_price(mask):
        out = np.full(nb, np.nan)
        idx = np.flatnonzero(mask)
        if idx.size:
            last = np.searchsorted(b[idx], np.arange(nb), side="right") - 1
            ok = last >= 0
            out[ok] = px[idx[last[ok]]]
        return out

    df = pd.DataFrame({
        "ts": t0 + (np.arange(nb, dtype=np.int64) + 1) * F,     # fin de barre
        "open": opn, "high": high, "low": low, "close": close,
        "vol": vol, "buy_vol": bvol, "sell_vol": vol - bvol,
        "n": nt, "n_buy": nbuy, "notional": notional, "big_signed": big_signed,
        "bid_proxy": last_price(bm), "ask_proxy": last_price(buy),
    })
    df["close"] = df["close"].ffill()
    for c in ("open", "high", "low"):
        df[c] = df[c].fillna(df["close"])
    df["vwap"] = (df["notional"] / df["vol"].where(df["vol"] > 0)).fillna(df["close"])
    return df


def _ratio(num: pd.Series, den: pd.Series, fill: float) -> pd.Series:
    return (num / den.where(den > 0)).fillna(fill)


def add_features(bars: pd.DataFrame, freq_s: int, metrics: pd.DataFrame | None = None,
                 funding: pd.DataFrame | None = None, metrics_lag_s: int = 360,
                 *, funding_interval_h: float, spot_bars: pd.DataFrame | None = None) -> pd.DataFrame:
    """Ajoute les facteurs causaux. `spot_bars` : barres spot de même fréquence (make_bars).

    `funding_interval_h` (obligatoire) : intervalle de funding de la plateforme d'EXÉCUTION
    ([venues.<execution>].funding_interval_hours, 1 h sur Kraken), identique en recherche et en
    live (`funding_interval_hours(cfg)`) ; il fixe `sec_to_funding`.
    """
    df = bars.copy()
    lc = np.log(df["close"])
    r1 = lc.diff().fillna(0.0) * 1e4
    df["ret_1"] = r1
    for w in WINDOWS:
        df[f"ret_{w}"] = (lc - lc.shift(w)) * 1e4
        bv = df["buy_vol"].rolling(w, min_periods=1).sum()
        tv = df["vol"].rolling(w, min_periods=1).sum()
        df[f"delta_{w}"] = _ratio(2 * bv - tv, tv, 0.0)
        nbu = df["n_buy"].rolling(w, min_periods=1).sum()
        nn = df["n"].rolling(w, min_periods=1).sum()
        df[f"timb_{w}"] = _ratio(2 * nbu - nn, nn, 0.0)
        df[f"rv_{w}"] = np.sqrt((r1 ** 2).rolling(w, min_periods=1).sum())
        df[f"big_{w}"] = df["big_signed"].rolling(w, min_periods=1).sum()
        hi = df["high"].rolling(w, min_periods=1).max()
        lo = df["low"].rolling(w, min_periods=1).min()
        df[f"rng_{w}"] = (hi - lo) / df["close"] * 1e4
        df[f"pos_{w}"] = _ratio(df["close"] - lo, hi - lo, 0.5)
    one_h = max(1, int(3600 / freq_s))
    one_d = max(1, int(86400 / freq_s))
    df["vol_1h_mean"] = df["vol"].rolling(one_h, min_periods=10).mean()
    for w in WINDOWS:
        df[f"volz_{w}"] = df["vol"].rolling(w, min_periods=1).mean() / df["vol_1h_mean"].where(df["vol_1h_mean"] > 0)
    df["rv_1h"] = np.sqrt((r1 ** 2).rolling(one_h, min_periods=10).sum())
    df["rv_1h_med_1d"] = df["rv_1h"].rolling(one_d, min_periods=one_h).median()
    df["vol_regime"] = df["rv_1h"] / df["rv_1h_med_1d"].where(df["rv_1h_med_1d"] > 0)  # >1 : vol. élevée
    sd = r1.rolling(one_h, min_periods=10).std()
    for w in (6, 12, 60):
        df[f"z_{w}"] = df[f"ret_{w}"] / (sd * np.sqrt(w)).where(sd > 0)
    tsdt = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df["hour"] = tsdt.dt.hour
    df["dow"] = tsdt.dt.dayofweek
    period_s = int(round(funding_interval_h * 3600))
    df["sec_to_funding"] = period_s - (df["ts"] // 1000) % period_s
    df["spread_proxy_bps"] = ((df["ask_proxy"] - df["bid_proxy"]) / df["close"] * 1e4).clip(lower=0)

    if metrics is not None and len(metrics) and "sum_open_interest" in metrics:
        m = metrics.sort_values("ts").copy()
        # disponibilité conservatrice : create_time peut être le DÉBUT de la période de 5 min
        # (ratios taker calculés sur [T, T+5 min)) ; instant de publication non documenté.
        # Défaut 360 s = 5 min de période + 60 s de marge ([data].metrics_lag_seconds).
        m["avail_ts"] = m["ts"] + metrics_lag_s * 1000
        # variations définies dans le temps (pas en lignes) : robustes aux trous du fichier ;
        # valeur de référence = dernière ligne à ts - X min (tolérance 60 s), sinon NaN
        mts = m["ts"].to_numpy(np.int64)
        loi = np.log(m["sum_open_interest"].where(m["sum_open_interest"] > 0)).to_numpy(float)
        for mins in (5, 15):
            target = mts - mins * 60_000
            j = np.searchsorted(mts, target, side="right") - 1
            ok = (j >= 0) & (target - mts[np.clip(j, 0, None)] <= 60_000)
            m[f"oi_chg_{mins}m"] = np.where(ok, (loi - loi[np.clip(j, 0, None)]) * 1e4, np.nan)
        keep = ["avail_ts", "oi_chg_5m", "oi_chg_15m"]
        for c in ("sum_taker_long_short_vol_ratio", "count_long_short_ratio",
                  "sum_toptrader_long_short_ratio"):
            if c in m.columns:
                keep.append(c)
        df = pd.merge_asof(df, m[keep].astype({"avail_ts": "int64"}), left_on="ts",
                           right_on="avail_ts", direction="backward").drop(columns="avail_ts")
    if funding is not None and len(funding):
        f = funding[["ts", "rate"]].rename(columns={"ts": "f_ts", "rate": "last_funding"})
        f = f.astype({"f_ts": "int64"}).sort_values("f_ts")
        df = pd.merge_asof(df, f, left_on="ts", right_on="f_ts", direction="backward").drop(columns="f_ts")
    if spot_bars is not None and len(spot_bars):
        s = spot_bars[["ts", "close"]].rename(columns={"ts": "s_ts", "close": "spot_close"})
        s = s.astype({"s_ts": "int64"}).sort_values("s_ts")
        df = pd.merge_asof(df, s, left_on="ts", right_on="s_ts", direction="backward").drop(columns="s_ts")
        df["basis_bps"] = (df["close"] / df["spot_close"] - 1) * 1e4
        ls = np.log(df["spot_close"])
        for w in (1, 6):
            df[f"spot_ret_{w}"] = (ls - ls.shift(w)) * 1e4
    return df.reset_index(drop=True)


def funding_interval_hours(cfg: dict) -> float:
    """Intervalle de funding de la plateforme d'exécution (profil [venues.<execution>])."""
    from .config import venue_profile
    return float(venue_profile(cfg)["funding_interval_hours"])


# Facteurs du MODÈLE : ≈ 20 facteurs (section 4) calculés depuis le SEUL flux de trades, donc
# reproduits à l'identique par le robot live (bot/bars.py -> add_features sur les trades Kraken ;
# vérifié par tests/test_features.py::TestLiveFeatureParity).
FEATURE_COLS_ML = [
    "ret_1", "ret_12", "ret_60", "ret_180",
    "delta_12", "delta_60", "delta_180", "timb_12",
    "big_60", "rv_12", "rv_60", "volz_12", "volz_60",
    "pos_180", "rng_180", "vol_regime", "z_12", "z_60",
    "hour", "sec_to_funding",
]
# Facteurs issus des fichiers Binance metrics / funding / spot : le robot live ne les calcule PAS
# (il ne reçoit que les trades Kraken). JAMAIS dans le modèle ; utilisables seulement par des
# règles de référence (non destinées au live).
FEATURE_COLS_RESEARCH_ONLY = ["oi_chg_5m", "oi_chg_15m", "sum_taker_long_short_vol_ratio",
                              "count_long_short_ratio", "sum_toptrader_long_short_ratio",
                              "last_funding", "spot_close", "basis_bps", "spot_ret_1", "spot_ret_6"]


def feature_cols(df: pd.DataFrame) -> list[str]:
    """Facteurs du modèle : uniquement ceux que le robot live calcule à l'identique."""
    return list(FEATURE_COLS_ML)
