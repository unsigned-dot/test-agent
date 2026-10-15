"""Protocole public Kraken Derivatives (Futures) : messages WebSocket v1 et REST v3.

Problème résolu : un seul endroit où l'on traduit les messages bruts de Kraken en
événements normalisés (Trade, BookSnapshot, BookDelta, Ticker, Heartbeat, Control),
avec un parsing TOLÉRANT (champ manquant ou inattendu -> message ignoré et compté,
jamais d'exception qui ferait tomber le robot).

Sources consultées le 14/10/2026, revérifiées le 15/10/2026 (docs.kraken.com, support.kraken.com) :
- WebSocket : https://docs.kraken.com/api/docs/guides/futures-websockets
  (URL wss://futures.kraken.com/ws/v1 ; « ping au moins toutes les 60 s » ; l'implémentation
  de référence liée par la doc utilise le ping PROTOCOLE : run_forever(ping_interval=30),
  https://github.com/CryptoFacilities/WebSocket-v1-Python/blob/master/cfWebSocketApiV1.py)
- démo : https://docs.kraken.com/api/docs/guides/futures-introduction
  (wss://demo-futures.kraken.com/ws/v1, https://demo-futures.kraken.com/derivatives/api/v3)
- feed trade : https://docs.kraken.com/api/docs/futures-api/websocket/trade
  (trade_snapshot puis trade ; side = côté TAKER ; type fill|liquidation|termination|block ;
  seq = « subscription message sequence number » : continuité stricte NON précisée)
- feed book : https://docs.kraken.com/api/docs/futures-api/websocket/book
  (book_snapshot {bids, asks, seq, timestamp} puis book {side, price, qty, seq, timestamp} ;
  valeurs de side, sens de qty=0 et continuité de seq NON précisés)
- feed ticker : https://docs.kraken.com/api/docs/futures-api/websocket/ticker
  (champs de funding « If zero, field is not populated »)
- feed heartbeat : https://docs.kraken.com/api/docs/futures-api/websocket/heartbeat
- REST instruments : https://docs.kraken.com/api/docs/futures-api/trading/get-instruments
  (exemple PF_XBTUSD : tickSize 1, contractSize 1, contractValueTradePrecision 4)
- REST tickers : https://docs.kraken.com/api/docs/futures-api/trading/get-tickers
- REST funding : https://docs.kraken.com/api/docs/futures-api/trading/historical-funding-rates
- spécifications PF_ (EEE) : https://support.kraken.com/articles/perpetual-contract-specifications-for-clients-in-the-eea
  (1 contrat = 1 BTC, lot 0,0001, funding horaire « standardised to a per-hour basis »,
  versé en continu, réglé chaque heure ou au changement de position)

Les points non confirmés par la documentation sont marqués « À VÉRIFIER ».

SÉCURITÉ : ce module ne parle qu'aux points d'accès PUBLICS (liste blanche ci-dessous). Il
n'existe ici aucun code d'authentification ni d'envoi d'ordre.
"""
from __future__ import annotations

import json
import math
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime

WS_URL = "wss://futures.kraken.com/ws/v1"
REST_URL = "https://futures.kraken.com/derivatives/api/v3"
PUBLIC_FEEDS = ("trade", "book", "ticker")
# listes blanches : tout le reste (flux privés open_orders, fills..., /sendorder, /accounts...)
# est refusé AVANT tout accès réseau.
ALLOWED_WS_FEEDS = ("trade", "book", "ticker", "ticker_lite", "heartbeat")
ALLOWED_REST_PATHS = ("instruments", "tickers", "historical-funding-rates")


class PrivateEndpointError(PermissionError):
    """Tentative d'accès à un point d'accès non public : interdit dans ce robot paper."""


# ---------------------------------------------------------------- messages sortants
def subscribe_msg(feed: str, product_ids: list[str] | None = None, event: str = "subscribe") -> str:
    if feed not in ALLOWED_WS_FEEDS or event not in ("subscribe", "unsubscribe"):
        raise PrivateEndpointError(f"flux WebSocket non public refusé : {event} {feed!r}")
    msg: dict = {"event": event, "feed": feed}
    if product_ids:
        msg["product_ids"] = list(product_ids)
    return json.dumps(msg)


def subscribe_all(product_id: str) -> list[str]:
    """Abonnements : trade, book, ticker pour l'instrument + heartbeat (heure serveur)."""
    return [subscribe_msg(f, [product_id]) for f in PUBLIC_FEEDS] + [subscribe_msg("heartbeat")]


# ---------------------------------------------------------------- événements normalisés
@dataclass(slots=True)
class Trade:
    ts: int            # ms UTC (horodatage Kraken du trade)
    px: float
    qty: float         # BTC pour PF_XBTUSD (contractSize 1, contrôlé au démarrage par check_instrument)
    taker_buy: bool    # side == "buy" : le taker est acheteur
    seq: int | None
    uid: str
    kind: str          # fill | liquidation | termination | block
    snapshot: bool = False


@dataclass(slots=True)
class BookSnapshot:
    ts: int
    seq: int | None
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]


@dataclass(slots=True)
class BookDelta:
    ts: int
    seq: int | None
    is_bid: bool       # side "buy" -> bid, "sell" -> ask — À VÉRIFIER (doc n'énumère pas les valeurs)
    px: float
    qty: float         # qty == 0 -> suppression du niveau — À VÉRIFIER (convention usuelle)


@dataclass(slots=True)
class Ticker:
    ts: int
    bid: float | None
    ask: float | None
    mark: float | None
    index: float | None
    funding_rate: float | None            # absolu : USD reçus par un short d'1 contrat pendant 1 h (support Kraken)
    relative_funding_rate: float | None   # taux HORAIRE relatif au prix (support Kraken) ; fraction et
                                          # non pourcentage : déduit des exemples (-1,38e-6), À VÉRIFIER en démo
    next_funding_ms: int | None           # doc : « temps jusqu'au funding » mais l'exemple est un
                                          # horodatage absolu — À VÉRIFIER (champ non utilisé)
    open_interest: float | None
    suspended: bool
    post_only: bool


@dataclass(slots=True)
class Heartbeat:
    ts: int


@dataclass(slots=True)
class Control:
    """Messages de contrôle : info, subscribed, error, alert, etc."""
    event: str
    raw: dict


def _f(x) -> float | None:
    """Nombre fini ou None (json.loads accepte NaN/Infinity, qui feraient planter les contrôles)."""
    try:
        v = None if x is None else float(x)
    except (TypeError, ValueError):
        return None
    return v if v is not None and math.isfinite(v) else None


def _i(x) -> int | None:
    try:
        return None if x is None else int(x)
    except (TypeError, ValueError):
        return None


def _trade(d: dict, product_id: str, snapshot: bool) -> Trade | None:
    if d.get("product_id", product_id) != product_id:
        return None
    ts, px, qty = _i(d.get("time")), _f(d.get("price")), _f(d.get("qty"))
    side = d.get("side")
    if ts is None or px is None or qty is None or side not in ("buy", "sell"):
        return None
    return Trade(ts, px, qty, side == "buy", _i(d.get("seq")), str(d.get("uid", "")),
                 str(d.get("type", "fill")), snapshot)


def _levels(rows) -> list[tuple[float, float]]:
    out = []
    for r in rows or []:
        if isinstance(r, dict):
            p, q = _f(r.get("price")), _f(r.get("qty"))
        elif isinstance(r, (list, tuple)) and len(r) >= 2:
            p, q = _f(r[0]), _f(r[1])
        else:
            continue
        if p is not None and q is not None:
            out.append((p, q))
    return out


def parse(msg: dict, product_id: str) -> list:
    """Message brut (dict JSON) -> liste d'événements normalisés ([] si ignoré)."""
    if not isinstance(msg, dict):
        return []
    if "event" in msg:
        return [Control(str(msg.get("event")), msg)]
    feed = msg.get("feed")
    if feed == "heartbeat":
        ts = _i(msg.get("time"))
        return [Heartbeat(ts)] if ts is not None else []
    if feed == "trade_snapshot":
        if msg.get("product_id", product_id) != product_id:
            return []
        trades = [t for t in (_trade(d, product_id, True) for d in msg.get("trades") or []) if t]
        trades.sort(key=lambda t: (t.ts, t.seq or 0))      # la doc les donne du plus récent au plus ancien
        return trades
    if feed == "trade":
        t = _trade(msg, product_id, False)
        return [t] if t else []
    if feed == "book_snapshot":
        if msg.get("product_id", product_id) != product_id:
            return []
        ts = _i(msg.get("timestamp")) or 0
        return [BookSnapshot(ts, _i(msg.get("seq")), _levels(msg.get("bids")), _levels(msg.get("asks")))]
    if feed == "book":
        if msg.get("product_id", product_id) != product_id:
            return []
        side, px, qty = msg.get("side"), _f(msg.get("price")), _f(msg.get("qty"))
        if side not in ("buy", "sell") or px is None or qty is None:
            return []
        return [BookDelta(_i(msg.get("timestamp")) or 0, _i(msg.get("seq")), side == "buy", px, qty)]
    if feed in ("ticker", "ticker_lite"):
        if msg.get("product_id", product_id) != product_id:
            return []
        ts = _i(msg.get("time"))
        if ts is None:
            return []
        rel = _f(msg.get("relative_funding_rate"))
        if rel is None and msg.get("tag") == "perpetual" and "relative_funding_rate" not in msg:
            rel = 0.0          # doc : « If zero, field is not populated » (sinon l'ancien taux resterait)
        return [Ticker(ts, _f(msg.get("bid")), _f(msg.get("ask")), _f(msg.get("markPrice")),
                       _f(msg.get("index")), _f(msg.get("funding_rate")),
                       rel, _i(msg.get("next_funding_rate_time")),
                       _f(msg.get("openInterest")), bool(msg.get("suspended", False)),
                       bool(msg.get("post_only", False)))]
    return []


# ---------------------------------------------------------------- REST public (stdlib)
def parse_server_time(s: str) -> int | None:
    """'2022-06-28T09:29:04.243Z' -> ms UTC."""
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def rest_get(path: str, params: dict | None = None, base: str = REST_URL,
             timeout: float = 10.0) -> tuple[dict, int, int]:
    """GET public. Renvoie (json, t_envoi_ms, t_réception_ms). Lève en cas d'erreur réseau.

    Seuls les chemins de ALLOWED_REST_PATHS sont acceptés (aucun en-tête d'authentification).
    """
    if path.strip("/") not in ALLOWED_REST_PATHS:
        raise PrivateEndpointError(f"point d'accès REST non public refusé : {path!r}")
    url = f"{base.rstrip('/')}/{path.strip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "btc-scalp-paper/0.1"})
    t0 = int(time.time() * 1000)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read().decode("utf-8"))
    return body, t0, int(time.time() * 1000)


def clock_offset_ms(body: dict, t_send: int, t_recv: int) -> int | None:
    """Décalage horloge locale - serveur (ms), corrigé de la moitié de l'aller-retour."""
    st = parse_server_time(body.get("serverTime", ""))
    if st is None:
        return None
    return (t_send + t_recv) // 2 - st


def find_instrument(body: dict, symbol: str) -> dict | None:
    for ins in body.get("instruments") or []:
        if str(ins.get("symbol", "")).upper() == symbol.upper():
            return ins
    return None


def check_instrument(ins: dict | None, tick: float, lot: float) -> list[str]:
    """Compare les règles publiées aux hypothèses (tick 1 USD, lot 0,0001). Liste des écarts."""
    if ins is None:
        return ["instrument introuvable dans /instruments"]
    out = []
    ts = _f(ins.get("tickSize"))
    if ts is None or abs(ts - tick) > 1e-12:
        out.append(f"tickSize={ins.get('tickSize')!r} (attendu {tick})")
    prec = _i(ins.get("contractValueTradePrecision"))
    if prec is None or abs(10.0 ** (-prec) - lot) > 1e-12:
        out.append(f"contractValueTradePrecision={ins.get('contractValueTradePrecision')!r} "
                   f"(attendu lot {lot})")
    cs = _f(ins.get("contractSize"))
    if cs is not None and abs(cs - 1.0) > 1e-12:      # qty des flux = contrats ; 1 contrat = 1 BTC attendu
        out.append(f"contractSize={ins.get('contractSize')!r} (attendu 1 : quantités en BTC)")
    if ins.get("tradeable") is False:
        out.append("instrument non négociable (tradeable=false)")
    return out
