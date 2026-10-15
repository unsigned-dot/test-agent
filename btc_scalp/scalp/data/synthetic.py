"""Données SYNTHÉTIQUES — uniquement pour tester le code.

Problème résolu : vérifier le pipeline sans données réelles (test anti-faux-positif et
test de sensibilité), avec le même format que loaders.parse_aggtrades.

Jamais utilisées pour conclure quoi que ce soit sur BTCUSDT.
- null=True  : prix efficient = marche aléatoire, flux d'ordres sans pouvoir prédictif.
               Toute stratégie doit y perdre ~ ses coûts : c'est un test anti-faux-positif.
- null=False : on implante un signal (le flux agresseur persistant entraîne une dérive
               du prix). Le pipeline doit détecter un avantage brut : test de sensibilité.
"""
from __future__ import annotations

import numpy as np
from scipy.signal import lfilter


def make_trades(days: int = 3, rate_per_s: float = 4.0, daily_vol: float = 0.025,
                start_ms: int = 1_780_272_000_000, p0: float = 100_000.0, tick: float = 0.1,
                null: bool = True, flow_persistence: float = 0.97, alpha_bps: float = 0.6,
                seed: int = 0) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    horizon_s = days * 86_400
    n = rng.poisson(rate_per_s * horizon_s)
    t_s = np.sort(rng.uniform(0, horizon_s, n))
    dt = np.diff(t_s, prepend=0.0)
    sig = daily_vol / np.sqrt(86_400)
    noise = rng.standard_normal(n) * sig * np.sqrt(dt)
    if null:
        side = rng.random(n) < 0.5
        drift = np.zeros(n)
    else:
        # flux persistant : état latent AR(1) qui pilote la probabilité d'achat
        e = rng.standard_normal(n)
        state = lfilter([np.sqrt(1 - flow_persistence**2)], [1.0, -flow_persistence], e)
        p_buy = 1 / (1 + np.exp(-1.5 * state))
        side = rng.random(n) < p_buy
        # le flux signé lisse entraîne la dérive du prix efficient (avantage implanté)
        signed = np.where(side, 1.0, -1.0)
        a = 0.02
        ew = lfilter([a], [1.0, -(1 - a)], signed)
        drift = alpha_bps * 1e-4 * ew
    logp = np.log(p0) + np.cumsum(noise + drift)
    eff = np.exp(logp)
    bid_ticks = np.floor(eff / tick)
    px_ticks = np.where(side, bid_ticks + 1, bid_ticks)
    decimals = max(0, int(np.ceil(-np.log10(tick) - 1e-9)))
    px = np.round(px_ticks * tick, decimals)
    qty = np.round(rng.lognormal(-4.0, 1.3, n), 3).astype(np.float32) + np.float32(0.001)
    aid = np.arange(n, dtype=np.int64) + 1_000_000
    return {
        "ts": (start_ms + t_s * 1000).astype(np.int64),
        "px": px.astype(np.float64),
        "qty": qty,
        "bm": ~side,
        "aid": aid,
        "fid": aid * 3,
        "lid": aid * 3 + 2,
    }
