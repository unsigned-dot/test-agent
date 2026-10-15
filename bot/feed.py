"""Flux de marché : connexion WebSocket Kraken (live) et faux flux (tests / démo).

Problème résolu : fournir au moteur une suite unique de messages bruts horodatés à la
réception, (recv_ms, dict), quelle que soit la source, en gérant pour le live la
reconnexion avec backoff exponentiel + gigue et les pings ; signaler chaque
(dé)connexion par un message interne {"_feed": "connected" | "disconnected"} pour que le
moteur sache qu'il y a eu un trou de données. Contient aussi le carnet L2 local.

La bibliothèque `websockets` est importée paresseusement (le module reste importable et
testable sans elle).
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from typing import AsyncIterator, Callable

import numpy as np

from . import kraken


def now_ms() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------- carnet L2
class LocalBook:
    """Carnet L2 reconstruit depuis book_snapshot + deltas (qty 0 = niveau supprimé, À VÉRIFIER)."""

    def __init__(self):
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.seq: int | None = None
        self.valid = False
        self.gaps = 0
        self.ts = 0

    def snapshot(self, s: kraken.BookSnapshot) -> None:
        self.bids = {p: q for p, q in s.bids if q > 0}
        self.asks = {p: q for p, q in s.asks if q > 0}
        self.seq, self.ts, self.valid = s.seq, s.ts, True

    def delta(self, d: kraken.BookDelta) -> bool:
        """Applique un delta. Renvoie False (et invalide le carnet) en cas de trou de séquence."""
        if not self.valid:
            return False
        if self.seq is not None and d.seq is not None:
            if d.seq <= self.seq:
                return True                       # doublon / ancien : ignoré
            if d.seq != self.seq + 1:             # continuité stricte de seq : À VÉRIFIER en réel
                self.valid = False
                self.gaps += 1
                return False
        side = self.bids if d.is_bid else self.asks
        if d.qty <= 0:
            side.pop(d.px, None)
        else:
            side[d.px] = d.qty
        self.seq = d.seq if d.seq is not None else self.seq
        self.ts = max(self.ts, d.ts)
        return True

    def best(self) -> tuple[float | None, float | None]:
        if not self.valid:
            return None, None
        return (max(self.bids) if self.bids else None, min(self.asks) if self.asks else None)

    def sweep_price(self, side: int, qty: float) -> float | None:
        """Prix moyen d'un ordre au marché de `qty` (side +1 achat -> asks). None si profondeur insuffisante."""
        if not self.valid or qty <= 0:
            return None
        levels = sorted(self.asks.items()) if side > 0 else sorted(self.bids.items(), reverse=True)
        left, cost = qty, 0.0
        for p, q in levels:
            take = min(left, q)
            cost += take * p
            left -= take
            if left <= 1e-12:
                return cost / qty
        return None


# --------------------------------------------------------------------- live Kraken
def _default_connect(url: str, ping_interval: float):
    import websockets          # import paresseux : dépendance du VPS uniquement
    return websockets.connect(url, ping_interval=ping_interval, ping_timeout=ping_interval,
                              max_size=2 ** 22, open_timeout=15, close_timeout=5)


async def kraken_messages(ws_url: str, product_id: str, reconnect_min_s: float = 1.0,
                          reconnect_max_s: float = 60.0, ping_interval_s: float = 20.0,
                          connect: Callable | None = None, clock: Callable[[], int] = now_ms,
                          stop: asyncio.Event | None = None,
                          resync: asyncio.Event | None = None) -> AsyncIterator[tuple[int, dict]]:
    """Flux brut Kraken avec reconnexion infinie. `connect` injectable pour les tests.

    Pings : protocole WebSocket (ping/pong RFC 6455) toutes les `ping_interval_s`. La doc
    demande « un ping au moins toutes les 60 s » ; l'implémentation de référence qu'elle cite
    (CryptoFacilities cfWebSocketApiV1.py) utilise exactement ce ping protocole
    (run_forever(ping_interval=30)).
    `resync` : si positionné (trou de séquence ou carnet croisé), désabonnement puis
    réabonnement au seul flux `book` sur la MÊME connexion pour recevoir un nouveau
    book_snapshot (« Most feeds send a snapshot of the current state first ») : pas de trou
    dans les trades, pas de pause des entrées. Réception effective d'un nouveau snapshot après
    réabonnement : À VÉRIFIER en démo Kraken.
    """
    connect = connect or _default_connect
    delay = reconnect_min_s
    while stop is None or not stop.is_set():
        t_conn = clock()
        try:
            async with connect(ws_url, ping_interval_s) as ws:
                for m in kraken.subscribe_all(product_id):
                    await ws.send(m)
                yield clock(), {"_feed": "connected"}
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except (TypeError, ValueError):
                        yield clock(), {"_feed": "bad_json"}
                        continue
                    yield clock(), msg
                    if stop is not None and stop.is_set():
                        return
                    if resync is not None and resync.is_set():
                        resync.clear()
                        await ws.send(kraken.subscribe_msg("book", [product_id], "unsubscribe"))
                        await ws.send(kraken.subscribe_msg("book", [product_id]))
                        yield clock(), {"_feed": "book_resubscribed"}
            err = "fermeture par le serveur"
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — toute erreur réseau => reconnexion
            err = f"{type(e).__name__}: {e}"
        yield clock(), {"_feed": "disconnected", "error": err}
        if clock() - t_conn > 120_000:        # connexion restée stable : on repart du délai minimal
            delay = reconnect_min_s
        await asyncio.sleep(delay * (0.5 + random.random()))
        delay = min(reconnect_max_s, delay * 2)


# --------------------------------------------------------------------- faux flux
def fake_messages(trades: dict[str, np.ndarray], product_id: str = "PF_XBTUSD", lag_ms: int = 60,
                  book_every: int = 3, ticker_every_ms: int = 1000, heartbeat_every_ms: int = 5000,
                  funding_rel: float = 2e-6, silences: list[tuple[int, int]] | None = None,
                  seq_gap_at: int | None = None, tick: float = 1.0) -> list[tuple[int, dict]]:
    """Rejoue des trades (scalp.data.synthetic.make_trades) au FORMAT Kraken documenté.

    Produit : connexion, book_snapshot, trades (seq continus), deltas de carnet autour du
    dernier prix, ticker chaque seconde, heartbeat. `silences` : [(t_début, t_fin)] sans
    aucun message (flux périmé) ; `seq_gap_at` : indice de trade à partir duquel on saute
    un numéro de séquence.
    """
    ts, px, qty, bm = trades["ts"], trades["px"], trades["qty"], trades["bm"]
    out: list[tuple[int, dict]] = []
    t0 = int(ts[0])
    p0 = float(px[0])
    out.append((t0 - 10, {"_feed": "connected"}))
    out.append((t0 - 5, {"event": "subscribed", "feed": "trade", "product_ids": [product_id]}))
    bid0 = np.floor(p0 / tick) * tick
    out.append((t0 - 1, {"feed": "book_snapshot", "product_id": product_id, "timestamp": t0 - 1,
                         "seq": 1000, "tickSize": None,
                         "bids": [{"price": bid0 - i * tick, "qty": 0.5 + i} for i in range(10)],
                         "asks": [{"price": bid0 + (i + 1) * tick, "qty": 0.5 + i} for i in range(10)]}))
    bids = {float(bid0 - i * tick) for i in range(10)}
    asks = {float(bid0 + (i + 1) * tick) for i in range(10)}
    bseq, tseq = 1000, 5000
    next_tk, next_hb = t0, t0
    silences = silences or []

    def silent(t):
        return any(a <= t < b for a, b in silences)

    for i in range(ts.size):
        t = int(ts[i])
        while next_tk <= t:
            if not silent(next_tk):
                last = float(px[max(0, i - 1)])
                b = np.floor(last / tick) * tick
                out.append((next_tk + lag_ms, {
                    "feed": "ticker", "product_id": product_id, "time": next_tk, "bid": b, "ask": b + tick,
                    "markPrice": last, "index": last, "funding_rate": funding_rel * last,
                    "relative_funding_rate": funding_rel, "next_funding_rate_time": (next_tk // 3_600_000 + 1) * 3_600_000,
                    "openInterest": 1234.5, "suspended": False, "post_only": False, "tag": "perpetual"}))
            next_tk += ticker_every_ms
        while next_hb <= t:
            if not silent(next_hb):
                out.append((next_hb + lag_ms, {"feed": "heartbeat", "time": next_hb}))
            next_hb += heartbeat_every_ms
        if silent(t):
            continue
        tseq += 1
        if seq_gap_at is not None and i == seq_gap_at:
            tseq += 3
        out.append((t + lag_ms, {"feed": "trade", "product_id": product_id, "uid": f"u{i}",
                                 "side": "sell" if bm[i] else "buy", "type": "fill", "seq": tseq,
                                 "time": t, "qty": float(qty[i]), "price": float(px[i])}))
        if i % book_every == 0:
            b = float(np.floor(float(px[i]) / tick) * tick)
            upd = [("buy", p, 0.0) for p in sorted(bids) if p > b]          # niveaux croisés supprimés
            upd += [("sell", p, 0.0) for p in sorted(asks) if p < b + tick]
            upd += [("buy", b, 0.3 + (i % 7) * 0.1), ("sell", b + tick, 0.3 + (i % 5) * 0.1)]
            for side, p, q in upd:
                bseq += 1
                levels = bids if side == "buy" else asks
                if q == 0:
                    levels.discard(p)
                else:
                    levels.add(p)
                out.append((t + lag_ms, {"feed": "book", "product_id": product_id, "side": side,
                                         "seq": bseq, "price": p, "qty": q, "timestamp": t}))
    out.sort(key=lambda e: e[0])
    return out
