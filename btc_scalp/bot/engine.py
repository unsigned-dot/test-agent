"""Moteur de décision et de suivi des positions (cœur du robot paper).

Problème résolu : enchaîner, pour chaque message de marché, sans état caché :
enregistrement -> contrôle qualité (périmé, trous, sauts, horloge) -> barres 5 s ->
facteurs -> stratégie -> VETO du RiskManager (scalp/risk.py) -> ordres au broker avec
identifiants client déterministes et écriture préalable dans l'état SQLite -> stop
reduce-only posé dès le fill -> take-profit limite -> durée max -> clôture, PnL, risque.

Le moteur est synchrone et piloté par le temps qu'on lui donne (`now_ms`) : le même code
tourne en direct (horloge réelle) et en rejeu (horloge simulée), ce qui rend tous les
scénarios de panne testables sans réseau.

Commandes acceptées : /status et /stop UNIQUEMENT. Aucune commande ne peut lever un
arrêt, relever une limite ou ouvrir une position.
"""
from __future__ import annotations

import math
import time
from collections import deque
from datetime import datetime, timezone

from scalp.execution import ExecParams, _round_tick
from scalp.risk import RiskManager

from . import kraken
from .bars import BarBuilder
from .broker import Broker, OrderRequest, PaperBroker
from .config import BotConfig
from .feed import LocalBook
from .monitor import Monitor
from .state import State
from .strategy import NoStrategy, Strategy, make_strategy

ALLOWED_COMMANDS = ("/status", "/stop")
# Resynchronisation du carnet (réabonnement au flux book, SANS reconnexion) : au plus une par
# minute. Le carnet ne sert qu'à la surveillance (spread, comparaison des coûts) : un carnet
# invalide ne bloque jamais les entrées et ne doit pas provoquer de rafale de réabonnements.
BOOK_RESYNC_MIN_INTERVAL_MS = 60_000


def fmt_ts(ms: int | None) -> str:
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class Engine:
    def __init__(self, cfg: BotConfig, state: State, broker: Broker, notifier=None,
                 strategy: Strategy | None = None, recorder=None, log=None):
        if cfg.mode != "paper" or not isinstance(broker, PaperBroker) or not broker.is_paper:
            raise SystemExit("REFUS : le moteur n'accepte que le mode paper avec le PaperBroker")
        self.cfg = cfg
        b = cfg.bot
        self.state, self.broker, self.recorder = state, broker, recorder
        self.notifier = notifier
        self.log = log or (lambda level, msg: None)
        self.pid = b["market"]["product_id"]
        self.strategy = strategy or make_strategy(b)
        research = dict(cfg.research)
        research["venue"] = {"execution": "kraken"}
        self.ep = ExecParams.from_cfg(research)
        self.freq_s = int(cfg.research["bars"]["freq_seconds"])
        fd = b["feed"]
        self.stale_ms = int(fd["stale_after_s"] * 1000)
        self.flatten_ms = int(fd["flatten_after_s"] * 1000)
        self.post_gap_ms = int(fd["post_gap_pause_s"] * 1000)
        self.max_lag_ms = int(fd["max_feed_lag_ms"])
        self.max_jump = float(fd["max_jump_bps"])
        self.seq_gap_action = fd.get("seq_gap_action", "pause")
        self.max_clock_ms = int(fd["max_clock_offset_ms"])
        self.require_clock = bool(fd.get("require_clock_check", True))
        window_bars = int(b["bars"]["window_hours"] * 3600 / self.freq_s)
        self.builder = BarBuilder(self.freq_s, window_bars, int(cfg.research["bars"]["close_grace_ms"]))
        self.warmup_ms = int(b["bars"]["warmup_s"] * 1000)
        self.book = LocalBook()
        self.monitor = Monitor(b, self._alert)
        self.resync_requested = False
        self._last_resync_ms: int | None = None
        self._crossed_once = False

        # --- état restauré ---------------------------------------------------------
        rs = state.get("risk_state")
        self.risk = RiskManager.from_state(cfg.research, rs) if rs else RiskManager(cfg.research)
        self.started_ms: int | None = None
        self.connected = False
        self.connections = 0
        self.last_msg_ms: int | None = None
        self.last_trade_seq: int | None = None
        self.last_trade_px: float | None = None
        self.last_trade_ts: int | None = None
        self.seen_uids: deque[str] = deque(maxlen=5000)
        self._uid_set: set[str] = set()
        self.pause_until: dict[str, int] = {}          # pauses temporisées (trou, anomalie)
        self.clock_offset_ms: int | None = None        # local - serveur
        self.clock_checked = not self.require_clock
        self.ticker: kraken.Ticker | None = None
        self.mark_px: float | None = None
        self.last_status_ms = 0
        self.now_ms: int | None = None                 # dernier instant fourni (réel ou simulé)
        self.reconcile_divergence = state.get("reconcile_divergence", "")
        self._risk_events_seen = len(self.risk.events)
        self.resume_after_ts = int(state.get("last_trade_ts", 0) or 0)   # trades déjà traités avant l'arrêt

    # ===================================================================== alertes
    def _alert(self, level: str, kind: str, msg: str) -> None:
        self.state.log_event(self.now_ms or int(time.time() * 1000), level, kind, msg)
        self.log(level, f"[{kind}] {msg}")
        if self.notifier is not None and level in ("WARN", "CRIT", "INFO", "TRADE"):
            try:
                self.notifier.send(f"{level} {kind}\n{msg}")
            except Exception as e:  # noqa: BLE001 — une alerte ratée ne doit pas arrêter le robot
                self.log("ERROR", f"envoi Telegram impossible : {e}")

    # ===================================================================== démarrage
    def start(self, now_ms: int) -> None:
        """Contrôles de démarrage + rapprochement avec la plateforme (section 8)."""
        self.started_ms = now_ms
        self.state.set("last_start_ms", now_ms)
        self.reconcile(now_ms)
        msg = (f"Robot démarré — mode PAPER, {self.pid}, stratégie {self.strategy.name}, "
               f"config recherche {self.cfg.research_hash}.")
        if self.stopped:
            msg += " ATTENTION : arrêt (/stop) actif — aucune entrée."
        if self.risk.killed:
            msg += f" COUPE-CIRCUIT ACTIF ({self.risk.kill_reason}) — redémarrage manuel requis."
        self._alert("INFO", "start", msg)

    @property
    def stopped(self) -> bool:
        return bool(self.state.get("stopped", False))

    def reconcile(self, now_ms: int) -> None:
        """Compare l'état local à la plateforme ; complète ce qui est sûr, pause sinon."""
        problems: list[str] = []
        local_open = self.state.open_positions()
        for p in local_open:
            base = p["base_id"]
            entry = self.state.order(base + "-E")
            ex = self.broker.get_order(base + "-E")
            if p["status"] == "pending_entry":
                if ex is None:
                    # intention jamais transmise : on ABANDONNE (signal périmé), jamais de renvoi d'entrée
                    self._abandon(p, now_ms, "entrée jamais transmise (crash avant envoi)")
                    continue
                if entry and entry["status"] == "intent":
                    self.state.update_order(base + "-E", status="submitted", updated_ms=now_ms)
            # ordres de protection : renvoyés (idempotent) si l'intention n'a pas atteint la plateforme
            for o in self.state.orders_for(base):
                if o["leg"] in ("S", "T", "X") and o["status"] in ("intent", "submitted"):
                    if self.broker.get_order(o["client_id"]) is None:
                        self._send(o, now_ms)
            # crash entre « position ouverte » et la création du stop : la position n'aurait AUCUN
            # stop. On le pose maintenant (même identifiant déterministe), au niveau enregistré.
            if p["status"] == "open" and self.state.order(base + "-S") is None and p["stop_px"]:
                self._new_order(base, "S", -int(p["side"]), "stop", float(p["qty"]), now_ms,
                                stop_px=float(p["stop_px"]))
                self.log("WARN", f"stop manquant de {base} reposé au démarrage")
        self._sync_positions(now_ms)
        # position plateforme vs locale
        bpos = self.broker.position()
        lpos = self.state.open_positions()
        lqty = sum(p["side"] * p["qty"] for p in lpos if p["status"] == "open")
        if abs(bpos["qty"] - lqty) > 1e-9:
            problems.append(f"position plateforme {bpos['qty']} != locale {lqty}")
        known = {o["client_id"] for o in self.state.live_orders()}
        for o in self.broker.open_orders():
            st = self.state.order(o["client_id"])
            if st is None:
                problems.append(f"ordre inconnu sur la plateforme : {o['client_id']}")
            elif o["client_id"] not in known and st["status"] not in ("intent", "submitted"):
                problems.append(f"ordre {o['client_id']} actif sur la plateforme mais {st['status']} localement")
        open_now = [p for p in self.state.open_positions()]
        if self.risk.position_open != bool(open_now):
            self.log("WARN", f"risk.position_open={self.risk.position_open} corrigé selon l'état local")
            self.risk.position_open = bool(open_now)
            self._persist_risk()
        for p in open_now:
            if p["status"] == "open" and not self._has_live_stop(p["base_id"]):
                problems.append(f"position {p['base_id']} SANS stop actif")
        if problems:
            self.reconcile_divergence = " ; ".join(problems)
            self.state.set("reconcile_divergence", self.reconcile_divergence)
            self._alert("CRIT", "reconcile", "Divergence d'état au démarrage — PAUSE des entrées "
                        "jusqu'à vérification manuelle :\n" + self.reconcile_divergence)
        else:
            self.reconcile_divergence = ""
            self.state.set("reconcile_divergence", "")

    def _has_live_stop(self, base: str) -> bool:
        o = self.broker.get_order(base + "-S")
        return o is not None and o["status"] in ("open", "triggered")

    # ===================================================================== messages
    def handle(self, recv_ms: int, msg: dict) -> None:
        self.now_ms = recv_ms
        if self.recorder is not None:
            self.recorder.record(recv_ms, msg)
        if isinstance(msg, dict) and "_feed" in msg:
            self._feed_event(recv_ms, msg)
            self.tick(recv_ms)
            return
        events = kraken.parse(msg, self.pid)
        if not events:
            self.monitor.inc("ignored_msgs")
        for ev in events:
            if isinstance(ev, kraken.Trade):
                self._on_trade(recv_ms, ev)
            elif isinstance(ev, kraken.BookSnapshot):
                self.last_msg_ms = recv_ms
                self.book.snapshot(ev)
            elif isinstance(ev, kraken.BookDelta):
                self.last_msg_ms = recv_ms
                if not self.book.valid:
                    continue                      # en attente d'un book_snapshot : delta ignoré
                if not self.book.delta(ev):
                    self.monitor.inc("book_seq_gaps")
                    self._request_book_resync(recv_ms, "trou de séquence carnet")
            elif isinstance(ev, kraken.Ticker):
                self._on_ticker(recv_ms, ev)
            elif isinstance(ev, kraken.Heartbeat):
                self.last_msg_ms = recv_ms
                self.monitor.add_lag(recv_ms - ev.ts)
            elif isinstance(ev, kraken.Control):
                if ev.event in ("error", "subscribed_failed", "alert"):
                    self._alert("WARN", "kraken_control", f"message Kraken : {ev.raw}")
        self.tick(recv_ms)

    def _request_book_resync(self, now_ms: int, why: str) -> None:
        """Demande un réabonnement au carnet (nouveau snapshot), limité à 1 par minute."""
        self.book.valid = False
        if self._last_resync_ms is not None and now_ms - self._last_resync_ms < BOOK_RESYNC_MIN_INTERVAL_MS:
            self.monitor.inc("book_resync_throttled")
            return
        self._last_resync_ms = now_ms
        self.resync_requested = True
        self.log("WARN", f"{why} : réabonnement au carnet demandé")

    def _feed_event(self, recv_ms: int, msg: dict) -> None:
        kind = msg["_feed"]
        if kind == "connected":
            self.connections += 1
            self.connected = True
            self.last_trade_seq = None
            self.book.valid = False
            self.last_msg_ms = recv_ms
            if self.connections > 1:
                self._pause("reconnect_gap", recv_ms + self.post_gap_ms)
            self.monitor.set_condition("feed_disconnected", False, "")
        elif kind == "disconnected":
            self.connected = False
            self.monitor.inc("disconnects")
            self.monitor.set_condition("feed_disconnected", True,
                                       f"Flux Kraken coupé ({msg.get('error', '')}) — reconnexion en cours, "
                                       "aucune nouvelle entrée.")
        elif kind == "bad_json":
            self.monitor.inc("bad_json")

    def _on_trade(self, recv_ms: int, t: kraken.Trade) -> None:
        self.last_msg_ms = recv_ms
        if t.uid:
            if t.uid in self._uid_set:
                return
            if len(self.seen_uids) == self.seen_uids.maxlen:
                self._uid_set.discard(self.seen_uids[0])
            self.seen_uids.append(t.uid)
            self._uid_set.add(t.uid)
        if t.snapshot:
            # historique de reprise : barres seulement si postérieur au dernier trade vu
            if self.last_trade_ts is not None and t.ts > self.last_trade_ts:
                self.builder.add_trade(t.ts, t.px, t.qty, t.taker_buy)
            return
        self.monitor.add_lag(recv_ms - t.ts)
        if t.seq is not None:
            if self.last_trade_seq is not None and t.seq != self.last_trade_seq + 1 and t.seq > self.last_trade_seq:
                self.monitor.inc("trade_seq_gaps")
                if self.seq_gap_action == "pause":
                    self._pause("trade_seq_gap", recv_ms + self.post_gap_ms,
                                f"trou de séquence trades {self.last_trade_seq} -> {t.seq}")
            self.last_trade_seq = max(t.seq, self.last_trade_seq or 0)
        bad = ""
        if t.px <= 0 or t.qty <= 0:
            bad = f"prix/quantité invalide {t.px}/{t.qty}"
        elif abs(t.px / self.ep.tick - round(t.px / self.ep.tick)) > 1e-6:
            bad = f"prix {t.px} hors tick {self.ep.tick}"
        elif self.last_trade_px and abs(math.log(t.px / self.last_trade_px)) * 1e4 > self.max_jump:
            bad = f"saut de prix {self.last_trade_px} -> {t.px}"
        if bad:
            self.monitor.inc("data_anomalies")
            self._pause("data_anomaly", recv_ms + self.post_gap_ms, bad)
            if t.px <= 0 or t.qty <= 0:
                return
        self.last_trade_px, self.last_trade_ts = t.px, max(t.ts, self.last_trade_ts or 0)
        if not self.builder.add_trade(t.ts, t.px, t.qty, t.taker_buy):
            self.monitor.inc("late_trades")
        if t.ts <= self.resume_after_ts:
            self.monitor.inc("replayed_trades_skipped")      # déjà vus avant le redémarrage
            return
        self.broker.on_trade(t.ts, t.px, t.taker_buy)
        self._sync_positions(recv_ms)

    def _on_ticker(self, recv_ms: int, tk: kraken.Ticker) -> None:
        self.last_msg_ms = recv_ms
        self.ticker = tk
        self.mark_px = tk.mark or self.mark_px
        rel = tk.relative_funding_rate
        if rel is not None and abs(rel) > 0.01:      # garde-fou d'unité (À VÉRIFIER)
            self.monitor.inc("funding_rate_suspect")
            rel = None
        self.broker.on_ticker(rel)
        self.monitor.set_condition("market_suspended", tk.suspended or tk.post_only,
                                   f"Marché {self.pid} suspendu ou post-only : aucune entrée.")

    # ===================================================================== horloge
    def set_clock_offset(self, offset_ms: int | None, now_ms: int) -> None:
        """Résultat du contrôle d'horloge REST (local - serveur Kraken)."""
        if offset_ms is None:
            self.monitor.inc("clock_check_failed")
            return
        self.clock_offset_ms = int(offset_ms)
        self.clock_checked = True
        self.monitor.set_condition("clock_offset", abs(offset_ms) > self.max_clock_ms,
                                   f"Horloge locale décalée de {offset_ms} ms vs Kraken "
                                   f"(max {self.max_clock_ms}) : pause des entrées. Vérifier chrony/NTP.",
                                   "CRIT")

    def server_now(self, now_ms: int) -> int:
        return now_ms - (self.clock_offset_ms or 0)

    def _pause(self, reason: str, until_ms: int, detail: str = "") -> None:
        if until_ms > self.pause_until.get(reason, 0):
            if reason not in self.pause_until or self.pause_until[reason] < until_ms - self.post_gap_ms // 2:
                self.log("WARN", f"pause {reason} jusqu'à {fmt_ts(until_ms)} {detail}")
            self.pause_until[reason] = until_ms
        if detail:
            self.state.log_event(self.now_ms or 0, "WARN", reason, detail)

    def gate_reasons(self, now_ms: int) -> list[str]:
        """Raisons (côté robot) d'interdire une nouvelle entrée maintenant."""
        r = []
        if self.stopped:
            r.append("stopped")
        if self.reconcile_divergence:
            r.append("reconcile_divergence")
        if not self.connected:
            r.append("feed_disconnected")
        if self.last_msg_ms is None or now_ms - self.last_msg_ms > self.stale_ms:
            r.append("stale_feed")
        if self.started_ms is not None and now_ms - self.started_ms < self.warmup_ms:
            r.append("warmup")
        for k, until in self.pause_until.items():
            if now_ms < until:
                r.append(k)
        if self.require_clock and not self.clock_checked:
            r.append("clock_unchecked")
        if self.clock_offset_ms is not None and abs(self.clock_offset_ms) > self.max_clock_ms:
            r.append("clock_offset")
        lag = self.monitor.median_lag()
        if lag is not None and lag > self.max_lag_ms:
            r.append("feed_lag")
        if self.ticker is not None and (self.ticker.suspended or self.ticker.post_only):
            r.append("market_suspended")
        if self.state.open_positions():
            r.append("position_open")
        return r

    # ===================================================================== tick
    def tick(self, now_ms: int) -> None:
        """Travail périodique : barres closes, décisions, péremption, durée max, limites."""
        self.now_ms = now_ms
        if self.started_ms is None:
            self.start(now_ms)
        snow = self.server_now(now_ms)
        self.broker.on_time(snow)
        self._sync_positions(now_ms)
        stale = self.last_msg_ms is not None and now_ms - self.last_msg_ms > self.stale_ms
        self.monitor.set_condition("stale_feed", stale,
                                   f"Aucune donnée Kraken depuis plus de {self.stale_ms // 1000} s : "
                                   "pause des entrées.", "CRIT")
        if stale:
            self._pause("stale_gap", now_ms + self.post_gap_ms)
        for bar in self.builder.close_until(snow):
            self._on_bar(bar, now_ms)
        self._manage_open(now_ms, stale)
        if self.stopped:
            self._enforce_stop(now_ms)
        self._alert_risk_events()

    def _alert_risk_events(self) -> None:
        """Alerte pour chaque nouvelle limite atteinte (jour, semaine, coupe-circuit)."""
        new = self.risk.events[self._risk_events_seen:]
        self._risk_events_seen = len(self.risk.events)
        for e in new:
            if e["event"] == "kill_switch":
                self._alert("CRIT", "kill_switch", f"COUPE-CIRCUIT : {self.risk.kill_reason}. Arrêt total, "
                            "position fermée ; redémarrage manuel uniquement (décision d'Hugo).")
            else:
                self._alert("CRIT", e["event"], f"Limite de perte atteinte ({e['event']}, equity "
                            f"{e['equity']:.2f}) : arrêt des entrées jusqu'à {fmt_ts(self.risk.halt_until_ms)}, "
                            "reprise automatique à 50 % de taille.")
        if new:
            self._persist_risk()

    # ===================================================================== décision
    def _check_book(self) -> None:
        """Échantillonne le spread L2 à la clôture de barre ; carnet croisé 2 fois de suite => resync."""
        bb, ba = self.book.best()
        if bb is None or ba is None:
            return
        if ba <= bb:
            self.monitor.inc("book_crossed")
            if self._crossed_once:
                self._crossed_once = False
                self._request_book_resync(self.now_ms or 0, "carnet croisé")
                return
            self._crossed_once = True
            return
        self._crossed_once = False
        self.monitor.add_spread((ba - bb) / self.ep.tick)

    def _on_bar(self, bar: dict, now_ms: int) -> None:
        bar_ts = int(bar["ts"])
        self._check_book()
        if self.last_trade_ts:
            self.state.set("last_trade_ts", self.last_trade_ts)
        if self.state.has_decision(bar_ts):
            return                         # déjà décidé (reprise) : jamais deux décisions par barre
        gates = self.gate_reasons(now_ms)
        if self.server_now(now_ms) - bar_ts > 2000:
            gates.append("late_bar")
        sig = None
        # facteurs calculés seulement si une entrée est possible (économie CPU sur petit VPS)
        if not gates and not isinstance(self.strategy, NoStrategy):
            feats = self.builder.features(funding_interval_h=self.ep.funding_interval_h)
            sig = self.strategy.decide(feats)
        if sig is None:
            # pas de signal : décision consignée (sans les barres « vides » pour limiter le volume)
            outcome = "no_signal" if not gates else "paused"
            self.state.add_decision(bar_ts, now_ms, outcome, ",".join(gates), close=bar["close"])
            return
        if gates:
            self.state.add_decision(bar_ts, now_ms, "blocked", ",".join(gates), sig.side, close=bar["close"])
            self.monitor.inc("blocked_signals")
            return
        ref_px = float(bar["close"])     # comme le backtest : dernier prix connu à la fin de la barre
        stop_frac = self.ep.stop_bps * 1e-4
        cost_frac = self.ep.est_cost_frac(ref_px)
        qty, why = self.risk.request_entry(self.server_now(now_ms), ref_px, stop_frac, cost_frac,
                                           lot=self.ep.lot, mult=sig.mult,
                                           expected_gain_frac=sig.expected_gain_frac)
        if why:
            self.state.add_decision(bar_ts, now_ms, "veto", why, sig.side, close=bar["close"])
            self.monitor.inc(f"veto_{why}")
            self._persist_risk()
            return
        base = f"{'D' if self.strategy.dry else 'L'}{bar_ts}"
        cid = base + "-E"
        book_px = self.book.sweep_price(sig.side, qty)
        with self.state.transaction():
            self.state.add_decision(bar_ts, now_ms, "entry", sig.note, sig.side, qty, cid, bar["close"])
            self.state.add_position({"base_id": base, "side": sig.side, "qty": qty, "status": "pending_entry",
                                     "signal_ts": bar_ts, "dry": int(self.strategy.dry)})
            self.state.update_position(base, book_entry_px=book_px)
            self.state.add_order({"client_id": cid, "base_id": base, "leg": "E", "side": sig.side,
                                  "type": "market", "qty": qty, "price": None, "stop_px": None,
                                  "reduce_only": 0, "status": "intent", "created_ms": now_ms,
                                  "updated_ms": now_ms})
            self.state.set("risk_state", self.risk.to_state())
        self._send(self.state.order(cid), now_ms)
        tag = " [DRY — règle de référence, aucune valeur probante]" if self.strategy.dry else ""
        self._alert("TRADE", "entry_sent", f"{'LONG' if sig.side > 0 else 'SHORT'} {qty} BTC au marché "
                    f"(réf {ref_px:.0f}){tag}")

    def _send(self, o: dict, now_ms: int) -> None:
        """Transmet un ordre déjà écrit dans l'état (write-ahead), puis le marque transmis."""
        req = OrderRequest(o["client_id"], int(o["side"]), o["type"], float(o["qty"]), o["price"],
                           o["stop_px"], bool(o["reduce_only"]))
        self.broker.submit(req, self.server_now(now_ms))
        self.state.update_order(o["client_id"], status="submitted", updated_ms=now_ms)

    def _new_order(self, base: str, leg: str, side: int, typ: str, qty: float, now_ms: int,
                   price: float | None = None, stop_px: float | None = None) -> None:
        cid = f"{base}-{leg}"
        if self.state.order(cid) is not None:
            return                                     # déjà créé : idempotence
        self.state.add_order({"client_id": cid, "base_id": base, "leg": leg, "side": side, "type": typ,
                              "qty": qty, "price": price, "stop_px": stop_px, "reduce_only": 1,
                              "status": "intent", "created_ms": now_ms, "updated_ms": now_ms})
        self._send(self.state.order(cid), now_ms)

    # ===================================================================== suivi
    def _sync_positions(self, now_ms: int) -> None:
        for p in self.state.open_positions():
            base = p["base_id"]
            if p["status"] == "pending_entry":
                e = self.broker.get_order(base + "-E")
                if e is None:
                    continue
                if e["status"] == "filled":
                    self._on_entry_filled(p, e, now_ms)
                elif e["status"] in ("rejected", "cancelled"):
                    self.state.update_order(base + "-E", status=e["status"], updated_ms=now_ms)
                    self._abandon(p, now_ms, f"entrée non remplie ({e['reason']})")
                continue
            for leg in ("S", "T", "X"):
                o = self.broker.get_order(f"{base}-{leg}")
                if o is not None and o["status"] == "filled":
                    self._on_exit_filled(self.state.position(base), o, leg, now_ms)
                    break

    def _on_entry_filled(self, p: dict, e: dict, now_ms: int) -> None:
        base, side = p["base_id"], p["side"]
        px, qty = float(e["fill_px"]), float(e["fill_qty"])
        stop_px = px * (1 - side * self.ep.stop_bps * 1e-4)
        tp_px = _round_tick(px * (1 + side * self.ep.tp_bps * 1e-4), self.ep.tick, up=side > 0)
        deadline = int(e["fill_ms"]) + int(self.ep.time_stop_s * 1000)
        with self.state.transaction():
            self.state.update_order(base + "-E", status="filled", fill_px=px, fill_ms=e["fill_ms"],
                                    updated_ms=now_ms)
            self.state.update_position(base, status="open", qty=qty, entry_px=px, entry_ms=e["fill_ms"],
                                       stop_px=stop_px, tp_px=tp_px, deadline_ms=deadline,
                                       model_entry_px=px)
        # stop reduce-only posé IMMÉDIATEMENT, puis take-profit limite
        self._new_order(base, "S", -side, "stop", qty, now_ms, stop_px=stop_px)
        if self.ep.tp_bps < 1e8:
            self._new_order(base, "T", -side, "limit", qty, now_ms, price=tp_px)
        self.monitor.add_entry_comparison(side, px, self.state.position(base)["book_entry_px"])
        self._alert("TRADE", "entry_filled", f"Entrée remplie {base} : {qty} BTC à {px:.0f} ; "
                    f"stop {stop_px:.1f}, TP {tp_px:.0f}, durée max {fmt_ts(deadline)}")

    def _on_exit_filled(self, p: dict, o: dict, leg: str, now_ms: int) -> None:
        base, side, qty = p["base_id"], p["side"], float(p["qty"])
        exit_px = float(o["fill_px"])
        entry_o = self.broker.get_order(base + "-E")
        fees = float(entry_o["fee"] or 0.0) + float(o["fee"] or 0.0)
        funding = float(o["funding"] or 0.0)
        gross = side * (exit_px - float(p["entry_px"])) * qty
        net = gross - fees - funding
        reason = {"S": "stop", "T": "take_profit", "X": self.state.get(f"exit_reason:{base}", "time_stop")}[leg]
        self.risk.on_trade_closed(int(o["fill_ms"]), net)
        with self.state.transaction():
            self.state.update_order(o["client_id"], status="filled", fill_px=exit_px, fill_ms=o["fill_ms"],
                                    updated_ms=now_ms)
            for leg2 in ("S", "T", "X"):
                cid = f"{base}-{leg2}"
                if cid != o["client_id"] and self.state.order(cid) is not None:
                    bo = self.broker.get_order(cid)
                    self.state.update_order(cid, status=bo["status"] if bo else "cancelled", updated_ms=now_ms)
            self.state.update_position(base, status="closed", exit_px=exit_px, exit_ms=o["fill_ms"],
                                       exit_reason=reason, gross=gross, fees=fees, funding=funding, net=net)
            self.state.set("risk_state", self.risk.to_state())
        for leg2 in ("S", "T", "X"):                    # par sécurité (le paper les annule déjà)
            self.broker.cancel(f"{base}-{leg2}", self.server_now(now_ms))
        tag = " [DRY]" if p["dry"] else ""
        self._alert("TRADE", "exit", f"Sortie {base} ({reason}) à {exit_px:.0f} : net {net:+.2f} USD "
                    f"(brut {gross:+.2f}, frais {fees:.2f}, funding {funding:+.4f}) ; equity "
                    f"{self.risk.equity:.2f}{tag}")

    def _abandon(self, p: dict, now_ms: int, why: str) -> None:
        with self.state.transaction():
            self.state.update_position(p["base_id"], status="abandoned", exit_reason=why)
            self.risk.cancel_entry(now_ms)
            self.state.set("risk_state", self.risk.to_state())
        self.log("WARN", f"position {p['base_id']} abandonnée : {why}")

    def _manage_open(self, now_ms: int, stale: bool) -> None:
        for p in self.state.open_positions():
            if p["status"] != "open":
                continue
            snow = self.server_now(now_ms)
            if snow >= int(p["deadline_ms"]):
                self.flatten(p, now_ms, "time_stop")
            elif stale and self.last_msg_ms is not None and now_ms - self.last_msg_ms > self.flatten_ms:
                self.flatten(p, now_ms, "stale_feed")
            px = self.mark_px or self.last_trade_px
            if px:
                unreal = p["side"] * (px - float(p["entry_px"])) * float(p["qty"])
                if self.risk.check_limits(snow, unreal):
                    self._persist_risk()
                    self.flatten(p, now_ms, "risk_limit")
        if not self.state.open_positions() and self.risk.check_limits(self.server_now(now_ms)):
            self._persist_risk()

    def flatten(self, p: dict, now_ms: int, reason: str) -> None:
        """Sortie au marché reduce-only (le stop reste posé jusqu'à la sortie effective)."""
        base = p["base_id"]
        if self.state.order(base + "-X") is not None:
            return
        self.state.set(f"exit_reason:{base}", reason)
        self.broker.cancel(base + "-T", self.server_now(now_ms))
        self._new_order(base, "X", -p["side"], "market", float(p["qty"]), now_ms)
        self.log("INFO", f"fermeture {base} : {reason}")

    def _enforce_stop(self, now_ms: int) -> None:
        for p in self.state.open_positions():
            if p["status"] == "pending_entry":
                self.broker.cancel(p["base_id"] + "-E", self.server_now(now_ms))
            elif p["status"] == "open":
                self.flatten(p, now_ms, "manual_stop")

    def _persist_risk(self) -> None:
        self.state.set("risk_state", self.risk.to_state())

    # ===================================================================== commandes
    def command(self, text: str, now_ms: int) -> str:
        """Commandes Telegram. SEULES /status et /stop existent ; tout le reste est refusé."""
        cmd = (text or "").strip().split()
        name = cmd[0].split("@")[0].lower() if cmd else ""
        if name == "/status" and len(cmd) == 1:
            return self.status_text(now_ms)
        if name == "/stop" and len(cmd) == 1:
            first = not self.stopped
            self.state.set("stopped", True)
            self.state.set("stopped_at_ms", now_ms)
            self._enforce_stop(now_ms)
            if first:
                self._alert("CRIT", "manual_stop", "/stop reçu : plus aucune entrée, position éventuelle "
                            "fermée au marché. Reprise uniquement sur le serveur (python -m bot resume).")
            return "Arrêt enregistré (persistant). Aucune nouvelle entrée."
        self.monitor.inc("refused_commands")
        return "Commande refusée. Seules /status et /stop sont acceptées (aucune commande ne modifie le risque)."

    def status_text(self, now_ms: int) -> str:
        r = self.risk
        lines = [f"Mode PAPER — {self.pid} — stratégie {self.strategy.name}",
                 f"Heure : {fmt_ts(now_ms)} ; démarré {fmt_ts(self.started_ms)}",
                 f"Flux : {'connecté' if self.connected else 'DÉCONNECTÉ'} ; dernier msg il y a "
                 f"{(now_ms - self.last_msg_ms) / 1000 if self.last_msg_ms else float('nan'):.1f} s ; "
                 f"retard médian {self.monitor.median_lag()} ms ; horloge {self.clock_offset_ms} ms",
                 f"Equity {r.equity:.2f} (sommet {r.peak_equity:.2f}) ; trades du jour {r.day_trades} ; "
                 f"taille x{r.size_mult(self.server_now(now_ms))}",
                 f"Coupe-circuit : {'ACTIF — ' + r.kill_reason if r.killed else 'non'} ; arrêt jusqu'à "
                 f"{fmt_ts(r.halt_until_ms) if r.halt_until_ms > now_ms else '-'} ; /stop : "
                 f"{'ACTIF' if self.stopped else 'non'}",
                 f"Blocages entrée : {', '.join(self.gate_reasons(now_ms)) or 'aucun'}"]
        for p in self.state.open_positions():
            lines.append(f"Position {p['base_id']} {p['status']} {'LONG' if p['side'] > 0 else 'SHORT'} "
                         f"{p['qty']} @ {p['entry_px']} stop {p['stop_px']} TP {p['tp_px']}")
        closed = self.state.closed_positions()
        if closed:
            net = sum(c["net"] for c in closed)
            lines.append(f"Trades clos : {len(closed)} ; PnL net cumulé {net:+.2f} USD")
        sp = self.monitor.spread_summary()
        if sp:
            lines.append(f"Spread carnet : médiane {sp['median_ticks']:.1f} tick, p90 {sp['p90_ticks']:.1f}")
        if self.recorder is not None:
            lines.append(f"Enregistrement : {self.recorder.lines} lignes, {self.recorder.bytes_written / 1e6:.1f} Mo")
        al = self.monitor.active()
        if al:
            lines.append("Alertes actives : " + ", ".join(al))
        return "\n".join(lines)
