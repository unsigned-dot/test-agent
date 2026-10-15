"""Connecteur d'ordres : interface minimale + PaperBroker (plateforme simulée).

Problème résolu : séparer strictement la décision (moteur) de l'exécution, pour que
le futur connecteur réel (KrakenBroker, NON implémenté : exige l'autorisation explicite
d'Hugo) se branche sans toucher au reste, et simuler en attendant les exécutions avec
les MÊMES hypothèses conservatrices que le backtest (scalp/execution.py, section 5) :

- tout ordre devient actif à son envoi + latency_ms (latence réseau) ;
- marché : rempli au 1er trade STRICTEMENT postérieur à l'activation, au prix
  `scalp.execution.taker_price` (bid/ask inférés au tick 1 USD + slippage + impact) ;
  une ENTRÉE sans trade dans max_fill_wait_ms est rejetée (trou de données => pas de trade) ;
  une SORTIE n'expire jamais (elle sera exécutée au 1er trade disponible) ;
- limite (take-profit) : rempli seulement si un trade imprime STRICTEMENT au-delà du prix,
  au prix limite, frais maker (dernier de la file d'attente) ;
- stop : déclenché par un trade au niveau ou au-delà, puis exécuté comme un ordre au
  marché après stop_latency_ms ; il est posé dès le fill de l'entrée (le moteur l'envoie
  immédiatement) ;
- reduce-only : ne peut jamais ouvrir ni augmenter une position (sinon annulé) ;
- les changements d'état se lisent par `get_order` (rapprochement par interrogation,
  robuste à un crash : aucune notification d'exécution ne peut être « perdue ») ;
- funding Kraken : accru en continu sur le notionnel d'entrée au taux relatif horaire du
  ticker (à défaut, taux par défaut de la config) ; un long paie si le taux est positif.

L'état du PaperBroker est dans SA PROPRE base SQLite (comme une vraie plateforme
indépendante du robot) : le rapprochement au démarrage a donc un sens.
"""
from __future__ import annotations

import abc
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from scalp.execution import _round_tick, taker_price

HOUR_MS = 3_600_000


@dataclass(frozen=True)
class OrderRequest:
    client_id: str          # identifiant client DÉTERMINISTE (idempotence)
    side: int               # +1 achat, -1 vente
    type: str               # "market" | "limit" | "stop"
    qty: float
    price: float | None = None      # limite
    stop_px: float | None = None    # déclenchement
    reduce_only: bool = False


class Broker(abc.ABC):
    """Interface minimale commune au PaperBroker et au futur KrakenBroker réel."""

    # False par défaut : un connecteur qui oublie de se déclarer est traité comme RÉEL et refusé
    # par le moteur (seul PaperBroker est accepté, cf. Engine.__init__).
    is_paper: bool = False

    @abc.abstractmethod
    def submit(self, req: OrderRequest, now_ms: int) -> dict:
        """Envoie l'ordre. IDEMPOTENT : un client_id déjà connu renvoie l'ordre existant."""

    @abc.abstractmethod
    def cancel(self, client_id: str, now_ms: int) -> bool:
        """Annule si encore actif. Renvoie True si l'ordre n'est plus actif après l'appel."""

    @abc.abstractmethod
    def get_order(self, client_id: str) -> dict | None:
        """Ordre tel que connu par la plateforme (None si jamais reçu)."""

    @abc.abstractmethod
    def open_orders(self) -> list[dict]:
        """Ordres actifs (non remplis, non annulés)."""

    @abc.abstractmethod
    def position(self) -> dict:
        """{'qty': quantité signée, 'avg_px': prix moyen, 'funding': funding accru}."""

    def cancel_all(self, now_ms: int) -> None:
        """Annule tous les ordres actifs (arrêt d'urgence)."""
        for o in self.open_orders():
            self.cancel(o["client_id"], now_ms)



ORDERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    client_id TEXT PRIMARY KEY, seq INTEGER NOT NULL, side INTEGER NOT NULL, type TEXT NOT NULL,
    qty REAL NOT NULL, price REAL, stop_px REAL, reduce_only INTEGER NOT NULL, status TEXT NOT NULL,
    submit_ms INTEGER NOT NULL, active_ms INTEGER NOT NULL, trigger_ms INTEGER, fill_ms INTEGER,
    fill_px REAL, fill_qty REAL, fee REAL, liquidity TEXT, reason TEXT, funding REAL);
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1), qty REAL NOT NULL, avg_px REAL NOT NULL,
    funding REAL NOT NULL, fees REAL NOT NULL, realized REAL NOT NULL, last_accrual_ms INTEGER,
    rel_funding_per_h REAL, last_trade_ms INTEGER);
"""

ACTIVE = ("pending", "open", "triggered")


class PaperBroker(Broker):
    is_paper = True

    def __init__(self, path: str | Path, venue: dict, costs: dict):
        self.tick = float(venue["tick_size"])
        self.lot = float(venue["lot_size"])
        self.maker_bps = float(venue["maker_fee_bps"])
        self.taker_bps = float(venue["taker_fee_bps"])
        self.slip_bps = float(venue["extra_slippage_bps"])
        self.impact_bps_per_btc = float(venue["impact_bps_per_btc"])
        self.latency_ms = int(costs["latency_ms"])
        self.stop_latency_ms = int(costs["stop_latency_ms"])
        self.max_fill_wait_ms = int(costs["max_fill_wait_ms"])
        self.funding_interval_h = float(venue["funding_interval_hours"])
        # taux par défaut ramené à l'heure (utilisé tant qu'aucun ticker n'a donné le taux réel)
        self.default_rel_per_h = float(costs["default_funding_rate_8h"]) / 8.0
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")     # WAL : sûr en cas de crash du processus
        self.db.executescript(ORDERS_SCHEMA)
        self.db.execute("INSERT OR IGNORE INTO account VALUES (1, 0, 0, 0, 0, 0, NULL, NULL, NULL)")
        self._seq = (self.db.execute("SELECT COALESCE(MAX(seq), 0) FROM orders").fetchone()[0])
        self.last_trade_ms: int | None = None
        self._refresh_idle()

    def _refresh_idle(self) -> None:
        self._flat_and_idle = self._acct()["qty"] == 0 and not self.open_orders()

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------ interface
    def submit(self, req: OrderRequest, now_ms: int) -> dict:
        ex = self.get_order(req.client_id)
        if ex is not None:
            return ex                                    # idempotence : pas de double ordre
        if req.side not in (1, -1) or req.qty <= 0 or req.type not in ("market", "limit", "stop"):
            raise ValueError(f"ordre invalide : {req}")
        qty = round(round(req.qty / self.lot) * self.lot, 10)
        self._seq += 1
        status = "pending" if req.type == "market" else "open"
        self.db.execute(
            "INSERT INTO orders(client_id, seq, side, type, qty, price, stop_px, reduce_only, status, "
            "submit_ms, active_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (req.client_id, self._seq, req.side, req.type, qty,
             None if req.price is None else _round_tick(req.price, self.tick, up=req.side < 0),
             req.stop_px, int(req.reduce_only), status, now_ms, now_ms + self.latency_ms))
        self._flat_and_idle = False
        return self.get_order(req.client_id)

    def cancel(self, client_id: str, now_ms: int) -> bool:
        o = self.get_order(client_id)
        if o is None:
            return True
        if o["status"] in ACTIVE:
            if o["status"] == "triggered":
                return False      # stop déjà déclenché : l'ordre au marché est parti
            self._finish(client_id, "cancelled", now_ms, reason="cancel")
            self._refresh_idle()
        return True

    def get_order(self, client_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM orders WHERE client_id=?", (client_id,)).fetchone()
        return dict(r) if r else None

    def open_orders(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            f"SELECT * FROM orders WHERE status IN {ACTIVE} ORDER BY seq")]

    def position(self) -> dict:
        a = self._acct()
        return {"qty": a["qty"], "avg_px": a["avg_px"], "funding": a["funding"], "fees": a["fees"],
                "realized": a["realized"]}

    # ------------------------------------------------------------ marché
    def on_ticker(self, rel_funding_per_h: float | None) -> None:
        if rel_funding_per_h is not None:
            self.db.execute("UPDATE account SET rel_funding_per_h=? WHERE id=1", (rel_funding_per_h,))

    def on_time(self, now_ms: int) -> None:
        """Expire les ENTRÉES au marché restées sans trade (trou de données => pas de trade)."""
        for o in self.open_orders():
            if (o["type"] == "market" and o["status"] == "pending" and not o["reduce_only"]
                    and now_ms - o["active_ms"] > self.max_fill_wait_ms):
                self._finish(o["client_id"], "rejected", now_ms, reason="no_trade_after_arrival")
                self._refresh_idle()

    def on_trade(self, ts: int, px: float, taker_buy: bool) -> None:
        self.last_trade_ms = ts
        if self._flat_and_idle:
            return                     # rien à faire : aucune écriture disque par trade
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._accrue_funding(ts)
            for o in self.open_orders():
                o = self.get_order(o["client_id"])          # relu : un fill précédent a pu l'annuler
                if o["status"] not in ACTIVE:
                    continue
                self._process(o, ts, px, taker_buy)
            self.db.execute("COMMIT")
            self._refresh_idle()
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------ interne
    def _acct(self) -> dict:
        return dict(self.db.execute("SELECT * FROM account WHERE id=1").fetchone())

    def _accrue_funding(self, ts: int) -> None:
        a = self._acct()
        last = a["last_accrual_ms"]
        if a["qty"] != 0 and last is not None and ts > last:
            rate_h = a["rel_funding_per_h"] if a["rel_funding_per_h"] is not None else self.default_rel_per_h
            side = 1 if a["qty"] > 0 else -1
            cost = side * a["avg_px"] * abs(a["qty"]) * rate_h * (ts - last) / (self.funding_interval_h * HOUR_MS)
            self.db.execute("UPDATE account SET funding=funding+? WHERE id=1", (cost,))
        self.db.execute("UPDATE account SET last_accrual_ms=? WHERE id=1", (ts,))

    def _process(self, o: dict, ts: int, px: float, taker_buy: bool) -> None:
        side = o["side"]
        if o["type"] == "stop" and o["status"] == "open":
            if ts < o["active_ms"]:
                return
            hit = px <= o["stop_px"] if side < 0 else px >= o["stop_px"]
            if hit:
                self.db.execute("UPDATE orders SET status='triggered', trigger_ms=?, active_ms=? "
                                "WHERE client_id=?", (ts, ts + self.stop_latency_ms, o["client_id"]))
            return
        if o["type"] == "limit":
            if ts <= o["active_ms"]:
                return
            through = px > o["price"] if side < 0 else px < o["price"]
            if through:
                self._fill(o, ts, o["price"], self.maker_bps, "maker")
            return
        # ordre au marché (ou stop déclenché) : 1er trade strictement après activation
        if ts <= o["active_ms"]:
            return
        if (o["type"] == "market" and not o["reduce_only"]
                and ts - o["active_ms"] > self.max_fill_wait_ms):
            self._finish(o["client_id"], "rejected", ts, reason="data_gap_no_fill")
            return
        fpx = taker_price(px, taker_buy, side, self.tick, self.slip_bps, self.impact_bps_per_btc * o["qty"])
        self._fill(o, ts, fpx, self.taker_bps, "taker")

    def _fill(self, o: dict, ts: int, px: float, fee_bps: float, liq: str) -> None:
        a = self._acct()
        qty = o["qty"]
        if o["reduce_only"]:
            if a["qty"] == 0 or (a["qty"] > 0) == (o["side"] > 0):
                self._finish(o["client_id"], "cancelled", ts, reason="reduce_only_no_position")
                return
            qty = min(qty, abs(a["qty"]))
        fee = fee_bps * 1e-4 * px * qty
        new_qty = round(a["qty"] + o["side"] * qty, 10)
        realized, avg, funding_out = a["realized"], a["avg_px"], None
        if a["qty"] == 0 or (a["qty"] > 0) == (o["side"] > 0):          # ouverture / augmentation
            avg = (a["avg_px"] * abs(a["qty"]) + px * qty) / abs(new_qty)
        else:                                                          # réduction
            realized += (px - a["avg_px"]) * qty * (1 if a["qty"] > 0 else -1)
            if new_qty == 0:
                avg = 0.0
        funding = a["funding"]
        if a["qty"] == 0:
            self.db.execute("UPDATE account SET last_accrual_ms=? WHERE id=1", (ts,))
        if new_qty == 0:
            funding_out, funding = funding, 0.0
        self.db.execute("UPDATE account SET qty=?, avg_px=?, fees=fees+?, realized=?, funding=? WHERE id=1",
                        (new_qty, avg, fee, realized, funding))
        self.db.execute("UPDATE orders SET status='filled', fill_ms=?, fill_px=?, fill_qty=?, fee=?, "
                        "liquidity=?, funding=? WHERE client_id=?",
                        (ts, px, qty, fee, liq, funding_out, o["client_id"]))
        if new_qty == 0:      # position soldée : les reduce-only restants n'ont plus d'objet
            for r in self.open_orders():
                if r["reduce_only"] and r["status"] != "triggered":
                    self._finish(r["client_id"], "cancelled", ts, reason="position_flat")

    def _finish(self, client_id: str, status: str, ts: int, reason: str) -> None:
        self.db.execute("UPDATE orders SET status=?, reason=?, fill_ms=? WHERE client_id=?",
                        (status, reason, ts, client_id))
