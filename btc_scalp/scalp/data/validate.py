"""Contrôles qualité des trades (section 6 du cahier des charges).

Problème résolu : détecter AVANT toute recherche les données qui fausseraient un backtest :
horodatages non monotones, trous d'identifiants aggTrade (= trades manquants), doublons,
prix <= 0, prix hors tick, sauts de prix anormaux, jours vides, longues absences de trades.
Le rapport ne corrige rien : il décrit. `ok` est False dès qu'un défaut bloquant existe.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _utc(ms) -> str:
    return str(pd.to_datetime(int(ms), unit="ms", utc=True))


def validate_trades(t: dict[str, np.ndarray], tick: float = 0.1, gap_threshold_s: float = 60.0,
                    jump_threshold_bps: float = 200.0) -> dict:
    ts, px, qty = t["ts"], t["px"], t["qty"]
    rep: dict = {"n_trades": int(ts.size)}
    if ts.size == 0:
        rep["ok"] = False
        return rep
    rep["start_utc"], rep["end_utc"] = _utc(ts[0]), _utc(ts[-1])
    dts = np.diff(ts)
    rep["non_monotonic_timestamps"] = int((dts < 0).sum())
    gaps = np.flatnonzero(dts > gap_threshold_s * 1000)
    rep["gaps_over_threshold"] = int(gaps.size)
    rep["largest_gaps"] = [{"from": _utc(ts[i]), "seconds": float(dts[i] / 1000)}
                           for i in gaps[np.argsort(dts[gaps])[::-1][:10]]]
    if "aid" in t:
        aid = t["aid"]
        da = np.diff(aid)
        rep["duplicate_agg_ids"] = int(aid.size - np.unique(aid).size)
        rep["non_monotonic_agg_ids"] = int((da < 0).sum())
        jumps = np.flatnonzero(da > 1)                       # trous d'identifiants = trades manquants
        rep["agg_id_jumps"] = int(jumps.size)
        rep["missing_agg_ids_total"] = int((da[jumps] - 1).sum())
        rep["agg_id_jump_at"] = [_utc(ts[i]) for i in jumps[:10]]
    if "fid" in t and "lid" in t:
        rep["fid_gt_lid"] = int((t["fid"] > t["lid"]).sum())
    rep["non_positive_prices"] = int((px <= 0).sum())
    rep["non_positive_qty"] = int((qty <= 0).sum())
    pos = px > 0
    ret_bps = np.diff(np.log(np.where(pos, px, np.nan))) * 1e4
    rep["price_jumps_over_threshold"] = int((np.abs(ret_bps) > jump_threshold_bps).sum())
    rep["off_tick_prices"] = int((np.abs(px / tick - np.round(px / tick)) > 1e-6).sum())
    day = ts // 86_400_000
    counts = np.bincount(day - day.min())
    rep["trades_per_day_min"] = int(counts[counts > 0].min())
    rep["empty_days_inside_range"] = int((counts == 0).sum())
    rep["ok"] = (rep["non_monotonic_timestamps"] == 0
                 and rep.get("duplicate_agg_ids", 0) == 0
                 and rep.get("non_monotonic_agg_ids", 0) == 0
                 and rep.get("agg_id_jumps", 0) == 0
                 and rep.get("fid_gt_lid", 0) == 0
                 and rep["non_positive_prices"] == 0
                 and rep["non_positive_qty"] == 0
                 and rep["off_tick_prices"] == 0
                 and rep["empty_days_inside_range"] == 0)
    return rep
