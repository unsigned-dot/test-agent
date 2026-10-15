"""Backtest événementiel sur le flux de trades réel (aggTrades), par profil de plateforme.

Problème résolu : transformer un vecteur d'entrées (+1/-1/0 par barre) en trades au PnL
réaliste et sans information future, avec les coûts de la plateforme d'exécution choisie
([venue].execution -> [venues.binance] ou [venues.kraken], section 5 du cahier des charges).

Hypothèses (conservatrices, toutes paramétrables) :
- Le signal de la barre k est connu à sa FIN, ts_k. Taille et prix limite sont décidés avec
  les seuls trades de ts <= ts_k. L'ordre d'entrée arrive à ts_k + entry_latency_ms, avec
  entry_latency_ms = [bars].close_grace_ms + [costs].latency_ms (comme le robot live, qui ne
  clôt la barre qu'après la grâce puis envoie l'ordre : scalp.config.entry_latency_ms).
- Toute entrée passe par RiskManager.request_entry (veto : jour/semaine/coupe-circuit,
  week-end, plafond, cooldown, liquidation, sizing au risque fixe).
- Ordre au marché : rempli au 1er trade STRICTEMENT après l'arrivée (aucun trade dans
  max_fill_wait_ms -> trou de données -> pas de trade). Sans carnet, le meilleur prix opposé
  est inféré au tick de la plateforme : un trade acheteur-agresseur imprime à l'ask
  (ask = arrondi supérieur au tick), un trade vendeur-agresseur au bid (arrondi inférieur),
  l'autre côté est à un tick. S'ajoutent slippage + impact linéaire (bps par BTC), puis le
  prix est arrondi au tick dans le sens défavorable.
- Ordre limite (entrée maker, take-profit) : rempli UNIQUEMENT si un trade imprime
  strictement au-delà du prix limite (dernier de la file). Le TP n'est actif qu'après
  t_fill + latency_ms (le temps de le poser) ; le stop est surveillé dès le fill.
- Stop : déclenché par un trade au niveau ou au-delà, exécuté au marché après stop_latency_ms.
- Time stop : ordre au marché envoyé à t_fill + durée max, exécuté après latency_ms.
- Funding : taux du fichier officiel (rate, interval_h) ramené à un taux par ms, ou taux par
  défaut proratisé si pas de fichier. CONVENTION INTERNE : `ts` = FIN de la période couverte
  par le taux (instant de règlement) ; les chargeurs (scalp/data/loaders.py) y ramènent chaque
  source (Binance calc_time = fin ; Kraken historicalfundingrates timestamp = début -> + 1 h). "discrete" : payé aux instants k * intervalle de la
  plateforme dans (t_fill, t_exit] ; "continuous" : accru au prorata du temps de détention.
  Coût = side * notionnel d'entrée * taux (un long paie si le taux est positif).
- Les quantités sont arrondies au lot de la plateforme (par défaut vers le bas).
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import entry_latency_ms, venue_profile
from .risk import RiskManager

HOUR_MS = 3_600_000

TRADE_COLUMNS = ["signal_ts", "entry_ts", "exit_ts", "side", "entry_px", "exit_px", "qty", "notional",
                 "gross_pnl", "fees", "funding", "net_pnl", "gross_bps", "net_bps", "ideal_bps",
                 "mfe_bps", "mae_bps", "exit_reason", "equity_before", "size_mult", "entry_kind",
                 "taker_legs", "venue"]


@dataclass
class ExecParams:
    maker_fee_bps: float
    taker_fee_bps: float
    tick: float
    slip_bps: float
    latency_ms: int
    stop_latency_ms: int
    impact_bps_per_btc: float
    stop_bps: float
    tp_bps: float
    time_stop_s: float
    tp_as_maker: bool
    maker_entry: bool
    maker_timeout_s: float
    lot: float = 0.0001
    entry_latency_ms: int | None = None        # fin de barre -> entrée arrivée (défaut : latency_ms)
    max_fill_wait_ms: int = 5000
    funding_interval_h: float = 8.0
    funding_accrual: str = "discrete"          # "discrete" | "continuous"
    default_funding_rate_8h: float = 0.0001
    venue: str = ""

    @classmethod
    def from_cfg(cls, cfg: dict, exits_override: dict | None = None) -> "ExecParams":
        v = venue_profile(cfg)
        c, m = cfg["costs"], cfg["maker_entry"]
        e = dict(cfg["exits"])
        if exits_override:
            e.update(exits_override)
        return cls(
            maker_fee_bps=float(v["maker_fee_bps"]), taker_fee_bps=float(v["taker_fee_bps"]),
            tick=float(v["tick_size"]), slip_bps=float(v["extra_slippage_bps"]),
            latency_ms=int(c["latency_ms"]), stop_latency_ms=int(c["stop_latency_ms"]),
            entry_latency_ms=entry_latency_ms(cfg),
            impact_bps_per_btc=float(v["impact_bps_per_btc"]),
            stop_bps=float(e["stop_bps"]), tp_bps=float(e["take_profit_bps"]),
            time_stop_s=float(e["time_stop_seconds"]), tp_as_maker=bool(e["take_profit_as_maker"]),
            maker_entry=bool(m["enabled"]), maker_timeout_s=float(m["timeout_seconds"]),
            lot=float(v["lot_size"]), max_fill_wait_ms=int(c["max_fill_wait_ms"]),
            funding_interval_h=float(v["funding_interval_hours"]),
            funding_accrual=str(v["funding_accrual"]),
            default_funding_rate_8h=float(c["default_funding_rate_8h"]), venue=v["name"])

    def __post_init__(self):
        if self.entry_latency_ms is None:
            self.entry_latency_ms = self.latency_ms

    def est_cost_frac(self, ref_px: float) -> float:
        """Coût aller-retour estimé (fraction) servant au sizing : 2 jambes taker, slippage, 1 tick."""
        return (2 * (self.taker_fee_bps + self.slip_bps)) * 1e-4 + self.tick / ref_px


# ------------------------------------------------------------------ prix
def _round_tick(x: float, tick: float, up: bool) -> float:
    n = x / tick
    n = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return round(n * tick, 10)


def taker_price(px: float, buyer_aggr: bool, side: int, tick: float, slip_bps: float,
                impact_bps: float) -> float:
    """Prix effectif d'un ordre au marché (side=+1 achat, -1 vente) face au trade (px, agresseur)."""
    if buyer_aggr:                       # le trade a imprimé à l'ask
        ask = _round_tick(px, tick, up=True)
        bid = ask - tick
    else:                                # le trade a imprimé au bid
        bid = _round_tick(px, tick, up=False)
        ask = bid + tick
    adj = (slip_bps + impact_bps) * 1e-4
    if side > 0:
        return _round_tick(ask * (1 + adj), tick, up=True)
    return _round_tick(bid * (1 - adj), tick, up=False)


# --------------------------------------------------------------- funding
def _rate_per_ms(funding: pd.DataFrame | None, ep: ExecParams):
    """Renvoie (bornes_fin_ms, taux_par_ms) d'un taux constant par morceaux.

    La ligne j (ts_j, rate_j, interval_h_j) couvre la période (ts_j - interval, ts_j] : `ts` est la
    FIN de période (convention interne, voir loaders.load_funding / loaders.kraken_funding).
    """
    if funding is None or len(funding) == 0:
        return None, ep.default_funding_rate_8h / (8 * HOUR_MS)
    f_ts = funding["ts"].to_numpy(np.int64)
    ih = funding["interval_h"].to_numpy(float) if "interval_h" in funding else np.full(f_ts.size, 8.0)
    return f_ts, funding["rate"].to_numpy(float) / (ih * HOUR_MS)


def _rate_at(t: int, f_ts, rpm) -> float:
    if f_ts is None:
        return float(rpm)
    j = min(int(np.searchsorted(f_ts, t, side="left")), f_ts.size - 1)
    return float(rpm[j])


def funding_rate_total(t_from: int, t_to: int, ep: ExecParams, funding: pd.DataFrame | None = None) -> float:
    """Taux de funding cumulé (fraction du notionnel) pour une position longue tenue sur (t_from, t_to]."""
    f_ts, rpm = _rate_per_ms(funding, ep)
    if t_to <= t_from:
        return 0.0
    if ep.funding_accrual == "discrete":
        iv = int(round(ep.funding_interval_h * HOUR_MS))
        k0, k1 = t_from // iv + 1, t_to // iv
        return float(sum(_rate_at(k * iv, f_ts, rpm) * iv for k in range(k0, k1 + 1)))
    # continu : intégrale du taux constant par morceaux
    if f_ts is None:
        return float(rpm) * (t_to - t_from)
    total, t = 0.0, t_from
    while t < t_to:
        j = int(np.searchsorted(f_ts, t, side="right"))      # période se terminant après t
        end = t_to if j >= f_ts.size else min(int(f_ts[j]), t_to)
        total += float(rpm[min(j, f_ts.size - 1)]) * (end - t)
        t = end
    return total


# ------------------------------------------------------------- simulation
def simulate(trades: dict[str, np.ndarray], bars_ts: np.ndarray, entries: np.ndarray,
             cfg: dict, ep: ExecParams, funding: pd.DataFrame | None = None,
             ref_close: np.ndarray | None = None, size_mult: np.ndarray | float = 1.0,
             risk: RiskManager | None = None) -> pd.DataFrame:
    """Simule les entrées `entries` (int8 +1/-1/0 aligné sur `bars_ts`, fins de barres en ms).

    `size_mult` (scalaire ou tableau aligné sur bars_ts, borné à [0, 1] par le risque) permet au
    signal de réduire la taille. `risk` : RiskManager existant, modifié en place (pour enchaîner
    plusieurs morceaux sans perdre compteurs jour/semaine, sommet et coupe-circuit) ; par défaut,
    un nouveau RiskManager(cfg). Renvoie un trade par ligne (colonnes TRADE_COLUMNS) ;
    `df.attrs["skipped"]` compte les entrées refusées par raison.
    """
    ts, px, bm = trades["ts"], trades["px"], trades["bm"]
    n = ts.size
    bars_ts = np.asarray(bars_ts, dtype=np.int64)
    entries = np.asarray(entries)
    if entries.shape[0] != bars_ts.shape[0]:
        raise ValueError("entries doit être aligné sur bars_ts")
    sm = np.broadcast_to(np.asarray(size_mult, dtype=float), bars_ts.shape)
    risk = RiskManager(cfg) if risk is None else risk
    skipped: Counter = Counter()
    rows = []
    busy_until = -1                      # une seule position : signaux ignorés jusqu'à la sortie
    # indice du dernier trade vendeur-agresseur (bid) / acheteur-agresseur (ask) jusqu'à i inclus
    # (calculé seulement pour l'entrée maker : 2 tableaux int64 de la taille du flux de trades)
    if ep.maker_entry and n:
        ar = np.arange(n)
        last_bid_i = np.maximum.accumulate(np.where(bm, ar, -1))
        last_ask_i = np.maximum.accumulate(np.where(~bm, ar, -1))
        del ar

    for k in np.flatnonzero(entries):
        t_sig = int(bars_ts[k])
        side = 1 if entries[k] > 0 else -1
        if t_sig <= busy_until:
            skipped["position_open"] += 1
            continue
        i0 = int(np.searchsorted(ts, t_sig, side="right")) - 1      # dernier trade connu au signal
        if i0 < 0:
            skipped["no_data_before_signal"] += 1
            continue
        ref_px = float(px[i0])
        arrival = t_sig + ep.entry_latency_ms
        i = int(np.searchsorted(ts, arrival, side="right"))        # 1er trade strictement après l'arrivée
        if i >= n:
            skipped["no_data_after_signal"] += 1
            break
        stop_frac = ep.stop_bps * 1e-4
        cost_frac = ep.est_cost_frac(ref_px)
        q, why = risk.request_entry(t_sig, ref_px, stop_frac, cost_frac, lot=ep.lot, mult=float(sm[k]))
        if why:
            skipped[why] += 1
            continue
        equity = risk.equity
        mult_used = risk.size_mult(t_sig) * min(1.0, max(0.0, float(sm[k])))

        # ---------------- entrée ----------------
        if ep.maker_entry:
            # limite à notre meilleur prix inféré AU SIGNAL (bid pour un achat, ask pour une vente)
            j = int(last_bid_i[i0] if side > 0 else last_ask_i[i0])
            if j < 0:
                risk.cancel_entry(t_sig)
                skipped["no_quote_for_maker"] += 1
                continue
            limit = _round_tick(float(px[j]), ep.tick, up=(side < 0))
            i_end = int(np.searchsorted(ts, arrival + int(ep.maker_timeout_s * 1000), side="right"))
            seg = px[i:i_end]
            hit = np.flatnonzero(seg < limit) if side > 0 else np.flatnonzero(seg > limit)
            if hit.size == 0:
                risk.cancel_entry(t_sig)
                skipped["maker_not_filled"] += 1
                continue
            fi = i + int(hit[0])
            entry_px, t_fill, entry_fee_bps = limit, int(ts[fi]), ep.maker_fee_bps
        else:
            if ts[i] - arrival > ep.max_fill_wait_ms:
                risk.cancel_entry(t_sig)
                skipped["data_gap_no_fill"] += 1
                continue
            fi = i
            entry_px = taker_price(float(px[i]), not bool(bm[i]), side, ep.tick, ep.slip_bps,
                                   ep.impact_bps_per_btc * q)
            t_fill, entry_fee_bps = int(ts[i]), ep.taker_fee_bps

        # ---------------- sortie ----------------
        stop_lvl = entry_px * (1 - side * stop_frac)
        tp_lvl = _round_tick(entry_px * (1 + side * ep.tp_bps * 1e-4), ep.tick, up=(side > 0))
        deadline = t_fill + int(ep.time_stop_s * 1000)
        j_end = int(np.searchsorted(ts, deadline, side="right"))
        seg = px[fi + 1:j_end]
        tp_from = int(np.searchsorted(ts[fi + 1:j_end], t_fill + ep.latency_ms, side="right"))
        if side > 0:
            s_hit = np.flatnonzero(seg <= stop_lvl)
            t_hit = np.flatnonzero(seg[tp_from:] > tp_lvl if ep.tp_as_maker else seg[tp_from:] >= tp_lvl)
        else:
            s_hit = np.flatnonzero(seg >= stop_lvl)
            t_hit = np.flatnonzero(seg[tp_from:] < tp_lvl if ep.tp_as_maker else seg[tp_from:] <= tp_lvl)
        s_i = fi + 1 + int(s_hit[0]) if s_hit.size else None
        t_i = fi + 1 + tp_from + int(t_hit[0]) if t_hit.size else None

        def market_exit(t_send: int) -> tuple[float, int, str | None]:
            xi = int(np.searchsorted(ts, t_send, side="right"))
            flag = None
            if xi >= n:
                xi, flag = n - 1, "end_of_data"
            elif int(ts[xi]) - t_send > ep.max_fill_wait_ms:
                flag = "data_gap_exit"           # exécuté au 1er trade après le trou (risque de gap)
            return (taker_price(float(px[xi]), not bool(bm[xi]), -side, ep.tick, ep.slip_bps,
                                ep.impact_bps_per_btc * q), int(ts[xi]), flag)

        exit_fee_bps, exit_taker = ep.taker_fee_bps, True
        if s_i is not None and (t_i is None or s_i < t_i):
            reason = "stop"
            exit_px, t_exit, flag = market_exit(int(ts[s_i]) + ep.stop_latency_ms)
        elif t_i is not None:
            reason, flag = "take_profit", None
            if ep.tp_as_maker:
                exit_px, t_exit, exit_fee_bps, exit_taker = tp_lvl, int(ts[t_i]), ep.maker_fee_bps, False
            else:
                exit_px, t_exit, flag = market_exit(int(ts[t_i]) + ep.latency_ms)
        else:
            reason = "time_stop"
            exit_px, t_exit, flag = market_exit(deadline + ep.latency_ms)
        reason = flag or reason

        held = px[fi:int(np.searchsorted(ts, t_exit, side="right"))]
        if side > 0:
            mfe, mae = (held.max() / entry_px - 1) * 1e4, (held.min() / entry_px - 1) * 1e4
        else:
            mfe, mae = (1 - held.min() / entry_px) * 1e4, (1 - held.max() / entry_px) * 1e4

        notional = entry_px * q
        gross = side * (exit_px - entry_px) * q
        fees = (entry_fee_bps * entry_px * q + exit_fee_bps * exit_px * q) * 1e-4
        fund = side * notional * funding_rate_total(t_fill, t_exit, ep, funding)
        net = gross - fees - fund
        ideal = np.nan
        if ref_close is not None:
            kx = min(int(np.searchsorted(bars_ts, t_exit, side="left")), len(ref_close) - 1)
            ideal = side * (ref_close[kx] / ref_close[k] - 1) * 1e4     # sans spread, latence ni frais
        rows.append({
            "signal_ts": t_sig, "entry_ts": t_fill, "exit_ts": t_exit, "side": side,
            "entry_px": entry_px, "exit_px": exit_px, "qty": q, "notional": notional,
            "gross_pnl": gross, "fees": fees, "funding": fund, "net_pnl": net,
            "gross_bps": gross / notional * 1e4, "net_bps": net / notional * 1e4,
            "ideal_bps": ideal, "mfe_bps": mfe, "mae_bps": mae,
            "exit_reason": reason, "equity_before": equity, "size_mult": mult_used,
            "entry_kind": "maker" if ep.maker_entry else "taker",
            "taker_legs": int(not ep.maker_entry) + int(exit_taker),
            "venue": ep.venue,
        })
        risk.on_trade_closed(t_exit, net)
        busy_until = t_exit
    out = pd.DataFrame(rows, columns=TRADE_COLUMNS)
    out.attrs["skipped"] = dict(skipped)
    out.attrs["risk_events"] = list(risk.events)
    out.attrs["killed"] = risk.killed
    return out
