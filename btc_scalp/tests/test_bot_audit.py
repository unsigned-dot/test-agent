"""Audit indépendant du robot paper (15/10/2026) : un test par exigence, sans réseau.

1. aucun chemin vers un ordre réel ; 2. veto du risque ; 3. reprise après crash ;
4. flux (péremption, seq, backoff, horloge, saut de prix, fermeture) ; 5. barres/facteurs live
== recherche ; 6. simulateur paper == hypothèses du backtest ; 7. déploiement ; 8. Windows.
"""
from __future__ import annotations

import ast
import asyncio
import configparser
import contextlib
import io
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

from bot import kraken
from bot.app import apply_clock_check, build, check_clock, replay, run_live
from bot.bars import BAR_COLUMNS, BarBuilder, required_window_bars
from bot.broker import Broker, OrderRequest, PaperBroker
from bot.config import ConfigError, load_bot_config
from bot.engine import Engine
from bot.feed import now_ms
from bot.recorder import list_files, read_records
from bot.state import State
from bot.telegram import NullNotifier, Telegram
from scalp.data.synthetic import make_trades
from scalp.execution import ExecParams, _round_tick, simulate
from scalp.features import FEATURE_COLS_ML, add_features, make_bars
from scalp.risk import RiskManager

sys.path.insert(0, str(Path(__file__).resolve().parent))      # aussi lançable hors « discover -s tests »
from test_bot_engine import MONDAY, AlwaysLong, EngineCase, FakeTelegram, make_cfg, msgs_for

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "bot"
DEPLOY = BOT / "deploy"
PID = "PF_XBTUSD"


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def trade_msg(ts: int, px: float, seq: int, side: str = "buy", qty: float = 0.01) -> dict:
    return {"feed": "trade", "product_id": PID, "uid": f"x{seq}", "side": side, "type": "fill", "seq": seq,
            "time": ts, "qty": qty, "price": px}


def run_until_open(eng, st, msgs) -> int:
    """Rejoue jusqu'à ce qu'une position soit OUVERTE (entrée remplie). Renvoie l'indice atteint."""
    eng.set_clock_offset(0, msgs[0][0])
    for i, (recv, m) in enumerate(msgs):
        eng.handle(recv, m)
        if any(p["status"] == "open" for p in st.open_positions()):
            return i
    raise AssertionError("aucune position ouverte")


def close_log_handlers():
    lg = logging.getLogger("bot")
    for h in list(lg.handlers):
        h.close()
        lg.removeHandler(h)


# =========================================================================== 1
class TestNoRealOrders(unittest.TestCase):
    FORBIDDEN = ("sendorder", "editorder", "cancelorder", "batchorder", "cancelallorders", "withdraw",
                 "transfer", "apikey", "api_key", "api_secret", "authent", "signed_challenge", "hmac",
                 "openpositions", "accounts", "challenge")

    def _code_strings_and_names(self, path: Path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docs = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    docs.add(id(first.value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
                yield node.value
            elif isinstance(node, ast.Name):
                yield node.id
            elif isinstance(node, ast.Attribute):
                yield node.attr
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                yield node.name

    def test_no_private_endpoint_or_credential_in_code(self):
        """Analyse statique (hors commentaires et docstrings) de tout bot/*.py."""
        hits = []
        for p in BOT.rglob("*.py"):
            for s in self._code_strings_and_names(p):
                low = s.lower()
                hits += [(p.name, f, s[:60]) for f in self.FORBIDDEN if f in low]
        self.assertEqual(hits, [])

    def test_network_calls_only_in_two_places(self):
        """urlopen : REST public Kraken (GET) et Telegram ; aucun autre accès réseau sortant."""
        users = sorted(p.name for p in BOT.rglob("*.py") if "urlopen(" in p.read_text(encoding="utf-8"))
        self.assertEqual(users, ["kraken.py", "telegram.py"])
        src = (BOT / "kraken.py").read_text(encoding="utf-8")
        self.assertNotIn("data=", src.split("def rest_get")[1].split("def ")[0])      # GET seulement

    def test_rest_and_ws_whitelists_block_private_before_network(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("réseau appelé")):
            for path in ("sendorder", "/sendorder", "accounts", "openpositions", "instruments/../sendorder",
                         "transfer", "batchorder"):
                with self.assertRaises(kraken.PrivateEndpointError):
                    kraken.rest_get(path)
        for f in ("open_orders", "fills", "open_positions", "account_log", "balances", "notifications_auth"):
            with self.assertRaises(kraken.PrivateEndpointError):
                kraken.subscribe_msg(f)
        import json
        subs = [json.loads(m) for m in kraken.subscribe_all(PID)]
        self.assertEqual({(m["event"], m["feed"]) for m in subs},
                         {("subscribe", f) for f in ("trade", "book", "ticker", "heartbeat")})

    def test_no_api_key_is_read_from_environment(self):
        """Seules les variables Telegram sont lues ; une clé Kraken présente n'est jamais consultée."""
        seen = []

        class Env(dict):
            def get(self, k, d=None):
                seen.append(k)
                return super().get(k, d)

            def __getitem__(self, k):
                seen.append(k)
                return super().__getitem__(k)

        env = Env(os.environ)
        env.update({"KRAKEN_API_KEY": "SECRET", "KRAKEN_API_SECRET": "SECRET", "API_KEY": "SECRET"})
        with tempfile.TemporaryDirectory() as d, mock.patch.object(os, "environ", env):
            cfg = make_cfg(d)
            from bot.config import telegram_secrets
            telegram_secrets(cfg)
            eng, st, br, _ = build(cfg, record=False)
            st.close()
            br.close()
        self.assertFalse([k for k in seen if re.search("KRAKEN|SECRET|API", str(k).upper())], seen)

    def test_engine_refuses_any_broker_not_explicitly_paper(self):
        cfg = load_bot_config()

        class Forgot(Broker):            # futur connecteur qui oublie de se déclarer : traité comme réel
            def submit(self, req, now_ms): ...
            def cancel(self, cid, now_ms): ...
            def get_order(self, cid): ...
            def open_orders(self): return []
            def position(self): return {"qty": 0}

        self.assertFalse(Forgot().is_paper)
        with self.assertRaises(SystemExit):
            Engine(cfg, None, Forgot())
        with tempfile.TemporaryDirectory() as d:
            class Lying(PaperBroker):
                is_paper = False
            b = Lying(Path(d) / "p.sqlite", cfg.venue, cfg.research["costs"])
            with self.assertRaises(SystemExit):
                Engine(cfg, None, b)
            b.close()

    def test_cli_run_refuses_non_paper_before_any_network(self):
        with tempfile.TemporaryDirectory() as d:
            src = (BOT / "config.toml").read_text(encoding="utf-8")
            p = Path(d) / "config.toml"
            p.write_text(src.replace('mode = "paper"', 'mode = "live"').replace(
                '"../config/default.toml"', '"' + (ROOT / "config" / "default.toml").as_posix() + '"'),
                encoding="utf-8")
            from bot.__main__ import main
            with mock.patch("bot.app.run_live", side_effect=AssertionError("démarrage")), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                main(["--config", str(p), "run"])
            self.assertEqual(cm.exception.code, 2)


# =========================================================================== 2
class TestRiskVetoAudit(EngineCase):
    def test_no_entry_without_can_trade(self):
        eng, st, br = self.engine(strategy=AlwaysLong())
        eng.risk.block_reason = lambda t_ms: "audit_block"          # le risque refuse tout
        replay(eng, msgs_for(20))
        self.assertGreater(eng.strategy.calls, 50)                   # la stratégie voulait entrer
        self.assertEqual(br.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)
        reasons = {r[0] for r in st.db.execute("SELECT reason FROM decisions WHERE outcome='veto'")}
        self.assertEqual(reasons, {"audit_block"})

    def test_every_entry_order_was_granted_by_request_entry(self):
        eng, st, br = self.engine(strategy=AlwaysLong())
        orig, granted = eng.risk.request_entry, []

        def spy(*a, **k):
            q, why = orig(*a, **k)
            if not why:
                granted.append(q)
            return q, why
        eng.risk.request_entry = spy
        replay(eng, msgs_for(40))
        entries = br.db.execute("SELECT qty FROM orders WHERE client_id LIKE '%-E'").fetchall()
        self.assertGreater(len(entries), 0)
        self.assertEqual(sorted(r[0] for r in entries), sorted(granted))

    def test_kill_switch_on_unrealized_loss_flattens_and_persists(self):
        cfg = make_cfg(self.tmp)
        eng, st, br = self.engine(cfg, strategy=AlwaysLong())
        msgs = msgs_for(30)
        i = run_until_open(eng, st, msgs)
        p = next(p for p in st.open_positions() if p["status"] == "open")
        t = msgs[i][0] + 100
        crash_px = float(p["entry_px"]) - 151.0 / float(p["qty"]) - 1      # latent <= -151 USDT
        eng.handle(t, {"feed": "ticker", "product_id": PID, "time": t, "markPrice": crash_px, "tag": "perpetual"})
        self.assertTrue(eng.risk.killed)
        self.assertEqual(st.get(f"exit_reason:{p['base_id']}"), "risk_limit")
        self.assertIsNotNone(br.get_order(p["base_id"] + "-X"))
        self.assertTrue(st.get("risk_state")["killed"])
        st.close(); br.close(); self.opened.clear()
        eng2, st2, br2 = self.engine(cfg, strategy=AlwaysLong())
        self.assertTrue(eng2.risk.killed)
        self.assertEqual(eng2.risk.block_reason(MONDAY + 30 * 86_400_000), "kill_switch")
        for cmd in ("/resume", "/reset_kill HUGO_AUTORISE_REDEMARRAGE", "/start", "/risk 1"):
            eng2.command(cmd, t)
        self.assertTrue(eng2.risk.killed)

    def test_kill_threshold_is_150_usdt_or_15_percent(self):
        rm = RiskManager(load_bot_config().research)
        rm.on_trade_closed(MONDAY, -149.0)
        self.assertFalse(rm.killed)
        rm.on_trade_closed(MONDAY + 7 * 86_400_000, -1.0)
        self.assertTrue(rm.killed)
        rm2 = RiskManager(load_bot_config().research)
        rm2.on_trade_closed(MONDAY, +1000.0)                 # sommet 2000 : 15 % = 300 > 150 USDT
        rm2.on_trade_closed(MONDAY + 7 * 86_400_000, -150.0)
        self.assertTrue(rm2.killed)

    def test_only_authorized_chat_is_obeyed(self):
        tg = FakeTelegram(chat_id="42")
        eng, st, br = self.engine(notifier=tg)
        tg.updates = [{"update_id": 1, "message": {"chat": {"id": 7}, "text": "/stop"}},
                      {"update_id": 2, "message": {"chat": {"id": -42}, "text": "/stop"}},
                      {"update_id": 3, "edited_message": {"chat": {"id": 42}, "text": "/stop"}},
                      {"update_id": 4, "message": {"chat": {"id": "42"}, "text": "/status"}}]
        texts = tg.poll_commands()
        self.assertEqual(texts, ["/status"])
        for t in texts:
            eng.command(t, MONDAY)
        self.assertFalse(eng.stopped)


# =========================================================================== 3
class TestCrashRecovery(EngineCase):
    def test_open_position_recovered_protected_and_closed_without_duplicates(self):
        cfg = make_cfg(self.tmp)
        eng, st, br = self.engine(cfg, strategy=AlwaysLong())
        msgs = msgs_for(40)
        i = run_until_open(eng, st, msgs)
        base = next(p for p in st.open_positions() if p["status"] == "open")["base_id"]
        st.close(); br.close(); self.opened.clear()                 # crash brutal
        eng2, st2, br2 = self.engine(cfg, strategy=AlwaysLong())
        eng2.start(msgs[i][0] + 5000)
        self.assertEqual(eng2.reconcile_divergence, "")
        self.assertEqual(st2.position(base)["status"], "open")
        self.assertIn(br2.get_order(base + "-S")["status"], ("open", "triggered"))
        self.assertIn("position_open", eng2.gate_reasons(msgs[i][0] + 5000))
        replay(eng2, [(msgs[i + 1][0] - 1, {"_feed": "connected"})] + msgs[i + 1:])
        self.assertEqual(st2.position(base)["status"], "closed")
        ids = [r[0] for r in br2.db.execute("SELECT client_id FROM orders")]
        self.assertEqual(len(ids), len(set(ids)))
        lpos = st2.open_positions()                      # éventuelle NOUVELLE position en fin de rejeu
        self.assertLessEqual(len(lpos), 1)
        self.assertAlmostEqual(br2.position()["qty"], sum(p["side"] * p["qty"] for p in lpos if p["status"] == "open"))
        for p in lpos:
            self.assertNotEqual(p["base_id"], base)
            self.assertTrue(p["status"] == "pending_entry" or eng2._has_live_stop(p["base_id"]))
        eng2.reconcile(msgs[-1][0] + 6000)
        self.assertEqual(eng2.reconcile_divergence, "")
        pos = st2.db.execute("SELECT entry_ms, exit_ms FROM positions WHERE status='closed' ORDER BY entry_ms").fetchall()
        for a, b in zip(pos, pos[1:]):
            self.assertLessEqual(a["exit_ms"], b["entry_ms"])

    def test_position_without_stop_order_gets_stop_at_restart(self):
        """Crash entre « position ouverte » et la création du stop : le stop est posé au démarrage."""
        cfg = make_cfg(self.tmp)
        eng, st, br = self.engine(cfg)
        st.add_position({"base_id": "L7", "side": 1, "qty": 0.01, "status": "open", "signal_ts": 7, "dry": 0})
        st.update_position("L7", entry_px=100000.0, entry_ms=MONDAY, stop_px=99750.0, tp_px=100350.0,
                           deadline_ms=MONDAY + 300_000)
        st.add_order({"client_id": "L7-E", "base_id": "L7", "leg": "E", "side": 1, "type": "market", "qty": 0.01,
                      "price": None, "stop_px": None, "reduce_only": 0, "status": "filled", "created_ms": 1,
                      "updated_ms": 1})
        br.submit(OrderRequest("L7-E", 1, "market", 0.01), MONDAY - 1000)
        br.on_trade(MONDAY, 100000.0, True)
        st.close(); br.close(); self.opened.clear()
        eng2, st2, br2 = self.engine(cfg)
        eng2.start(MONDAY + 1000)
        s = br2.get_order("L7-S")
        self.assertEqual((s["status"], s["stop_px"], s["reduce_only"], s["side"]), ("open", 99750.0, 1, -1))
        self.assertEqual(eng2.reconcile_divergence, "")
        eng2.reconcile(MONDAY + 2000)
        self.assertEqual(br2.db.execute("SELECT COUNT(*) FROM orders WHERE client_id='L7-S'").fetchone()[0], 1)


# =========================================================================== 4
class TestFeedAudit(EngineCase):
    def test_flatten_after_flatten_after_s_of_stale_feed(self):
        eng, st, br = self.engine(strategy=AlwaysLong())
        msgs = msgs_for(30)
        i = run_until_open(eng, st, msgs)
        p = next(p for p in st.open_positions() if p["status"] == "open")
        last = eng.last_msg_ms
        for t in range(last, last + 125_000, 250):
            eng.tick(t)
            has_x = st.order(p["base_id"] + "-X") is not None
            if t - last <= 15_000:
                self.assertNotIn("stale_feed", eng.gate_reasons(t))
            elif t - last <= 120_000:
                self.assertIn("stale_feed", eng.gate_reasons(t))
                self.assertFalse(has_x, f"fermeture trop tôt à {t - last} ms")
        self.assertTrue(st.order(p["base_id"] + "-X") is not None)
        t = last + 126_000
        eng.handle(t, trade_msg(t, eng.last_trade_px, (eng.last_trade_seq or 0) + 1))
        self.assertEqual(st.position(p["base_id"])["exit_reason"], "stale_feed")

    def test_price_jump_and_non_finite_values_pause_without_crash(self):
        eng, st, br = self.engine(strategy=AlwaysLong())
        msgs = msgs_for(5)
        replay(eng, msgs)
        t, px, seq = msgs[-1][0] + 100, eng.last_trade_px, eng.last_trade_seq
        eng.handle(t, trade_msg(t, round(px * 1.03), seq + 1))                 # +300 bp
        self.assertIn("data_anomaly", eng.gate_reasons(t))
        for bad in ("NaN", float("inf"), float("nan")):
            eng.handle(t + 1, {**trade_msg(t + 1, px, seq + 2), "price": bad})
        eng.handle(t + 2, {"feed": "ticker", "product_id": PID, "time": t + 2, "markPrice": float("nan"),
                           "relative_funding_rate": float("inf"), "tag": "perpetual"})
        eng.tick(t + 10_000)
        self.assertIn("data_anomaly", {e["kind"] for e in st.events(100)})

    def test_out_of_order_trades_in_a_bar_do_not_crash(self):
        eng, st, br = self.engine()
        eng.set_clock_offset(0, MONDAY)
        eng.handle(MONDAY + 1000, trade_msg(MONDAY + 1000, 100000.0, 1))
        eng.handle(MONDAY + 1010, trade_msg(MONDAY + 900, 100001.0, 2))       # plus ancien, même barre
        eng.tick(MONDAY + 6000)                                               # avant : ValueError (make_bars)
        bar = eng.builder.bars[-1]
        self.assertEqual((bar["open"], bar["close"], bar["n"]), (100001.0, 100000.0, 2.0))

    def test_book_gap_does_not_pause_entries_and_resync_is_throttled(self):
        eng, st, br = self.engine()
        eng.set_clock_offset(0, MONDAY)
        snap = {"feed": "book_snapshot", "product_id": PID, "timestamp": MONDAY, "seq": 10,
                "bids": [{"price": 99999.0, "qty": 1}], "asks": [{"price": 100000.0, "qty": 1}]}
        eng.handle(MONDAY, {"_feed": "connected"})
        eng.handle(MONDAY, snap)
        eng.handle(MONDAY + 1, {"feed": "book", "product_id": PID, "side": "buy", "seq": 12, "price": 99998.0,
                                "qty": 1, "timestamp": MONDAY + 1})
        self.assertTrue(eng.resync_requested)
        eng.resync_requested = False
        self.assertFalse(any("book" in k or "gap" in k for k in eng.pause_until))
        eng.handle(MONDAY + 2, snap)
        eng.handle(MONDAY + 3, {"feed": "book", "product_id": PID, "side": "buy", "seq": 15, "price": 99998.0,
                                "qty": 1, "timestamp": MONDAY + 3})
        self.assertFalse(eng.resync_requested)                                   # < 60 s : limité
        self.assertEqual(eng.monitor.counters.get("book_resync_throttled"), 1)
        for k in range(5):                                                       # carnet invalide : ignoré
            eng.handle(MONDAY + 10 + k, {"feed": "book", "product_id": PID, "side": "sell", "seq": 20 + k,
                                         "price": 100001.0, "qty": 1, "timestamp": MONDAY + 10 + k})
        self.assertEqual(eng.monitor.counters.get("book_seq_gaps"), 2)

    def test_clock_desync_pauses_instead_of_crashing(self):
        """Avant : SQLite utilisé depuis le thread du contrôle REST => ProgrammingError, crash en boucle."""
        eng, st, br = self.engine(strategy=AlwaysLong())
        eng.start(MONDAY)
        body = {"serverTime": iso(MONDAY - 5000),
                "instruments": [{"symbol": PID, "tickSize": 1, "contractSize": 1, "contractValueTradePrecision": 4}]}
        asyncio.run(check_clock(eng, "x", fetch=lambda url: (body, MONDAY, MONDAY)))
        self.assertEqual(eng.clock_offset_ms, 5000)
        self.assertIn("clock_offset", eng.gate_reasons(MONDAY))
        self.assertIn("clock_offset", {e["kind"] for e in st.events(20)})
        asyncio.run(check_clock(eng, "x", fetch=lambda url: (body, MONDAY - 5000, MONDAY - 5000)))
        self.assertNotIn("clock_offset", eng.gate_reasons(MONDAY))
        # règles d'instrument différentes (tick, lot, contractSize) : alerte + pause
        bad = {"serverTime": iso(MONDAY), "instruments": [{"symbol": PID, "tickSize": 0.5, "contractSize": 1,
                                                           "contractValueTradePrecision": 4}]}
        apply_clock_check(eng, (bad, MONDAY, MONDAY))
        self.assertIn("instrument_rules", eng.pause_until)
        self.assertEqual(len(kraken.check_instrument({"tickSize": 1, "contractSize": 10,
                                                      "contractValueTradePrecision": 4}, 1.0, 1e-4)), 1)
        # REST injoignable : pas d'exception ; sans contrôle réussi au démarrage => pas d'entrée
        eng2, st2, br2 = self.engine(cfg=make_cfg(self.tmp + "/b"))
        asyncio.run(check_clock(eng2, "x", fetch=lambda url: OSError("réseau")))
        self.assertIn("clock_unchecked", eng2.gate_reasons(MONDAY))


# =========================================================================== 5
class TestLiveFeaturesEqualResearch(EngineCase):
    def test_engine_bars_and_features_equal_batch(self):
        """Même flux simulé : barres du moteur (grâce 300 ms, horloge par pas de 250 ms) ==
        scalp.features.make_bars sur l'historique ; facteurs == add_features (même fonction)."""
        t = make_trades(days=40 / 1440, rate_per_s=2.0, daily_vol=0.03, tick=1.0, start_ms=MONDAY, seed=11)
        from bot.feed import fake_messages
        eng, st, br = self.engine()
        replay(eng, fake_messages(t, PID))
        live = pd.DataFrame(list(eng.builder.bars), columns=BAR_COLUMNS)
        ref = make_bars(t, 5)[BAR_COLUMNS]
        n = len(live)
        self.assertGreaterEqual(n, len(ref) - 1)
        pd.testing.assert_frame_equal(live, ref.iloc[:n].reset_index(drop=True), check_dtype=False, rtol=1e-12)
        f_live = eng.builder.features(funding_interval_h=eng.ep.funding_interval_h)[FEATURE_COLS_ML]
        f_ref = add_features(ref.iloc[:n].reset_index(drop=True), 5, funding_interval_h=1.0)[FEATURE_COLS_ML]
        pd.testing.assert_frame_equal(f_live, f_ref, check_dtype=False, rtol=1e-12)

    def test_no_bar_closed_before_grace_and_late_trade_within_grace_counted(self):
        b = BarBuilder(5, grace_ms=300)
        b.add_trade(1000, 100.0, 1.0, True)
        b.add_trade(5100, 102.0, 1.0, True)          # barre suivante déjà commencée
        self.assertEqual(b.close_until(5299), [])     # fin de barre 5000 + grâce 300 non atteinte
        b.add_trade(4900, 101.0, 1.0, False)          # en désordre, dans la grâce : compté
        (bar,) = b.close_until(5300)
        self.assertEqual((bar["ts"], bar["n"], bar["close"]), (5000, 2.0, 101.0))
        self.assertFalse(b.add_trade(4950, 99.0, 1.0, True))     # après la grâce : en retard, ignoré

    def test_window_is_long_enough_for_identical_factors(self):
        t = make_trades(days=26 / 24, rate_per_s=0.3, tick=1.0, seed=2)
        bars = make_bars(t, 5)
        need = required_window_bars(5)
        full = add_features(bars, 5, funding_interval_h=1.0).iloc[-1][FEATURE_COLS_ML].astype(float)
        part = add_features(bars.tail(need).reset_index(drop=True), 5, funding_interval_h=1.0).iloc[-1]
        np.testing.assert_allclose(part[FEATURE_COLS_ML].astype(float), full, rtol=1e-9, atol=1e-9)
        cfg = load_bot_config()
        self.assertGreaterEqual(int(cfg.bot["bars"]["window_hours"] * 3600 / 5), need)
        self.assertGreaterEqual(cfg.bot["bars"]["warmup_s"], need * 5)      # pas d'entrée sur fenêtre tronquée
        with self.assertRaises(ConfigError):
            load_bot_config(overrides={"bars": {"window_hours": 24}})

    def test_bar_grace_has_a_single_source(self):
        """La grâce de clôture de barre entre dans la latence d'entrée du backtest : une seule source
        (config/default.toml [bars].close_grace_ms), l'ancienne clé du robot est refusée."""
        cfg = load_bot_config()
        self.assertEqual(cfg.research["bars"]["close_grace_ms"], 300)
        with self.assertRaises(ConfigError):
            load_bot_config(overrides={"feed": {"bar_close_grace_ms": 100}})


# =========================================================================== 6
class TestPaperMatchesBacktest(unittest.TestCase):
    """Mêmes trades, mêmes signaux : PaperBroker (piloté comme le moteur) == scalp.execution.simulate
    (profil Kraken : tick 1 USD, frais 2/5 bp, entrée envoyée après la grâce de clôture de barre puis
    latence d'envoi = latence d'entrée du backtest, limite remplie si traversée,
    funding continu horaire)."""

    def test_same_fills_fees_and_funding(self):
        cfg = load_bot_config()
        research = dict(cfg.research)
        research["venue"] = {"execution": "kraken"}
        ep = ExecParams.from_cfg(research)
        self.assertEqual((ep.tick, ep.funding_interval_h, ep.funding_accrual), (1.0, 1.0, "continuous"))
        t = make_trades(days=0.2, rate_per_s=2.0, daily_vol=0.04, tick=1.0, start_ms=MONDAY, seed=4)
        bars = make_bars(t, 5)
        entries = np.zeros(len(bars), np.int8)
        entries[60::90] = 1
        entries[105::180] = -1
        bt = simulate(t, bars["ts"].to_numpy(), entries, research, ep, risk=RiskManager(research))
        self.assertGreater(len(bt), 15)
        self.assertEqual(set(bt["exit_reason"]), {"stop", "take_profit", "time_stop"})
        with tempfile.TemporaryDirectory() as d:
            for k, r in enumerate(bt.itertuples()):
                b = PaperBroker(Path(d) / f"p{k}.sqlite", cfg.venue, cfg.research["costs"])
                # le moteur n'envoie l'entrée qu'à la clôture de barre (fin + grâce)
                grace = int(cfg.research["bars"]["close_grace_ms"])
                p = self._paper_trade(b, t, ep, int(r.signal_ts) + grace, int(r.side), float(r.qty))
                b.close()
                self.assertEqual((p["entry_ts"], p["entry_px"], p["exit_ts"], p["exit_px"], p["reason"]),
                                 (r.entry_ts, r.entry_px, r.exit_ts, r.exit_px, r.exit_reason), f"trade {k}")
                self.assertAlmostEqual(p["fees"], r.fees, places=9)
                self.assertAlmostEqual(p["funding"], r.funding, places=9)

    @staticmethod
    def _paper_trade(b, t, ep, sig_ts, side, q):
        """Comme le moteur : entrée au marché au signal ; dès le fill, stop + TP reduce-only ;
        à la durée max, annulation du TP et sortie au marché."""
        b.submit(OrderRequest("E", side, "market", q), sig_ts)
        ts, px, bm = t["ts"], t["px"], t["bm"]
        i, e, deadline = int(np.searchsorted(ts, sig_ts, side="right")), None, None
        while i < ts.size:
            tt = int(ts[i])
            if e is not None and tt >= deadline and b.get_order("X") is None:
                b.cancel("T", deadline)
                b.submit(OrderRequest("X", -side, "market", q, reduce_only=True), deadline)
            b.on_time(tt)
            b.on_trade(tt, float(px[i]), not bool(bm[i]))
            if e is None and b.get_order("E")["status"] == "filled":
                e = b.get_order("E")
                stop_px = e["fill_px"] * (1 - side * ep.stop_bps * 1e-4)
                tp = _round_tick(e["fill_px"] * (1 + side * ep.tp_bps * 1e-4), ep.tick, up=side > 0)
                b.submit(OrderRequest("S", -side, "stop", q, stop_px=stop_px, reduce_only=True), tt)
                b.submit(OrderRequest("T", -side, "limit", q, price=tp, reduce_only=True), tt)
                deadline = int(e["fill_ms"]) + int(ep.time_stop_s * 1000)
            for leg, reason in (("S", "stop"), ("T", "take_profit"), ("X", "time_stop")):
                o = b.get_order(leg)
                if o and o["status"] == "filled":
                    return {"entry_ts": e["fill_ms"], "entry_px": e["fill_px"], "exit_ts": o["fill_ms"],
                            "exit_px": o["fill_px"], "reason": reason, "fees": e["fee"] + o["fee"],
                            "funding": o["funding"]}
            i += 1
        raise AssertionError("trade paper non clos")

    def test_ticker_funding_is_hourly_and_missing_field_means_zero(self):
        cfg = load_bot_config()
        with tempfile.TemporaryDirectory() as d:
            b = PaperBroker(Path(d) / "p.sqlite", cfg.venue, cfg.research["costs"])
            b.on_ticker(1e-4)                                   # 0,01 % par heure
            b.submit(OrderRequest("E", 1, "market", 0.1), 0)
            b.on_trade(1000, 100000.0, True)
            b.submit(OrderRequest("X", -1, "market", 0.1, reduce_only=True), 3_601_000 - b.latency_ms)
            b.on_trade(3_601_001, 100000.0, False)
            x = b.get_order("X")
            notional = b.get_order("E")["fill_px"] * 0.1
            self.assertAlmostEqual(x["funding"], notional * 1e-4 * (x["fill_ms"] - 1000) / 3_600_000, places=9)
            b.close()
        (tk,) = kraken.parse({"feed": "ticker", "product_id": PID, "time": 1, "tag": "perpetual",
                              "markPrice": 1.0}, PID)
        self.assertEqual(tk.relative_funding_rate, 0.0)                 # doc : « If zero, field is not populated »


# =========================================================================== 7
class TestDeployment(unittest.TestCase):
    def _unit(self):
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        cp.optionxform = str
        cp.read(DEPLOY / "btc-scalp-bot.service", encoding="utf-8")
        return cp

    def test_systemd_unit_unprivileged_hardened_autorestart(self):
        u = self._unit()
        s = u["Service"]
        self.assertEqual(s["User"], "btcbot")
        self.assertNotIn(s.get("User"), ("root", ""))
        self.assertEqual(s["Restart"], "always")
        self.assertEqual(s["RestartPreventExitStatus"], "2")       # config refusée : pas de boucle
        self.assertEqual(s["NoNewPrivileges"], "true")
        self.assertEqual(s["ProtectSystem"], "strict")
        self.assertEqual(s["ReadWritePaths"], "/opt/btc_scalp/var")
        self.assertEqual(s["CapabilityBoundingSet"], "")
        self.assertTrue(s["ExecStart"].endswith("-m bot run"))
        self.assertIn("StartLimitBurst", u["Unit"])                # [Unit] : sinon ignoré par systemd
        self.assertNotIn("StartLimitIntervalSec", s)
        self.assertLessEqual(15 + 10, int(s["TimeoutStopSec"]))   # délai HTTP + long poll Telegram
        from bot.app import TELEGRAM_LONG_POLL_S
        self.assertEqual(TELEGRAM_LONG_POLL_S, 10)

    def test_install_script(self):
        src = (DEPLOY / "install.sh").read_text(encoding="utf-8")
        self.assertIn("set -euo pipefail", src)
        self.assertIn("--shell /usr/sbin/nologin", src)
        self.assertIn("chmod 640 /etc/btc_scalp/telegram.env", src)
        self.assertIn('chown root:"$USER_NAME" /etc/btc_scalp/telegram.env', src)
        self.assertNotRegex(src, r"--exclude '(data|var|\.venv)'")   # motifs non ancrés : scalp/data exclu
        self.assertIn("--exclude '/var'", src)
        self.assertIn("check-config", src)
        self.assertNotRegex(src, r"chmod\s+-?R?\s*7[0-7]7")
        self.assertNotIn("NOPASSWD", src)

    def test_scripts_parse_with_bash(self):
        bash = shutil.which("bash")
        if bash is None or os.name == "nt":
            self.skipTest("bash indisponible")
        for f in ("install.sh", "emergency_stop.sh"):
            r = subprocess.run([bash, "-n", str(DEPLOY / f)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
        es = (DEPLOY / "emergency_stop.sh").read_text(encoding="utf-8")
        self.assertLess(es.index("bot stop"), es.index("systemctl stop"))
        self.assertLess(es.index("systemctl stop"), es.index("bot emergency-stop"))

    def test_logrotate_keeps_everything(self):
        src = (DEPLOY / "logrotate.conf").read_text(encoding="utf-8")
        self.assertIn("su btcbot btcbot", src)
        self.assertIn("compress", src)
        self.assertNotRegex(src, r"rotate\s+[0-9]{1,2}\b")

    def test_cli_check_config_and_demo_120_minutes(self):
        r = subprocess.run([sys.executable, "-m", "bot", "check-config"], cwd=ROOT, capture_output=True,
                           text=True, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("mode=paper", r.stdout)
        from bot.__main__ import main
        with tempfile.TemporaryDirectory() as d:
            out = io.StringIO()
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                    rc = main(["demo", "--minutes", "120", "--out", d])
            finally:
                close_log_handlers()
            self.assertEqual(rc, 0)
            txt = out.getvalue()
            self.assertIn("Démo : 120.0 min simulées", txt)
            self.assertIn("Commande refusée", txt)                    # /set risk refusé
            n_files = len(list_files(Path(d) / "data", PID))
            self.assertGreaterEqual(n_files, 5)
            st = State(Path(d) / "state.sqlite")
            self.assertTrue(st.get("stopped"))
            self.assertEqual(st.open_positions(), [])
            st.close()


# =========================================================================== 8
class TestWindowsAndLiveLoop(unittest.TestCase):
    def test_run_live_cleanup_when_cancelled_like_ctrl_c_on_windows(self):
        """Windows : pas de add_signal_handler ; Ctrl+C annule la tâche principale. Les tampons
        d'enregistrement doivent être vidés et l'état fermé proprement (bloc finally)."""
        with tempfile.TemporaryDirectory() as d:
            cfg = make_cfg(d)
            tg = FakeTelegram()

            async def fake_feed(url, pid, *a, stop=None, resync=None):
                yield now_ms(), {"_feed": "connected"}
                for k in range(40):
                    t = now_ms()
                    yield t, trade_msg(t - 30, 100000.0 + (k % 3), 1000 + k)
                    await asyncio.sleep(0.005)
                await asyncio.sleep(3600)

            def fetch(url):
                t = now_ms()
                return {"serverTime": iso(t), "instruments": [{"symbol": PID, "tickSize": 1, "contractSize": 1,
                                                               "contractValueTradePrecision": 4}]}, t, t

            async def main():
                loop = asyncio.get_running_loop()
                with mock.patch.object(type(loop), "add_signal_handler", side_effect=NotImplementedError):
                    task = asyncio.create_task(run_live(cfg, messages=fake_feed, fetch=fetch, notifier=tg))
                    await asyncio.sleep(1.5)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task

            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    asyncio.run(main())
            finally:
                close_log_handlers()
            rows = [r for f in list_files(cfg.storage_path("data_dir"), PID, "trades") for r in read_records(f)]
            self.assertEqual(len(rows), 40)
            st = State(cfg.storage_path("state_db"))
            kinds = [e["kind"] for e in st.events(50)]
            st.close()
            self.assertIn("start", kinds)
            self.assertIn("shutdown", kinds)
            self.assertTrue(tg.deferred)
            self.assertTrue(any("start" in s for s in tg.sent))
            self.assertTrue(any("shutdown" in s for s in tg.sent))

    def test_deferred_telegram_never_blocks_the_engine(self):
        calls = []

        def slow(url, data, timeout):
            calls.append(url)
            time.sleep(2)
            return {"ok": True}
        tg = Telegram("T", "42", transport=slow)
        tg.deferred = True
        t0 = time.monotonic()
        for k in range(5):
            tg.send(f"alerte {k}")
        self.assertLess(time.monotonic() - t0, 0.1)
        self.assertEqual(calls, [])
        self.assertEqual(len(tg.pending), 5)

    def test_paths_and_console_text_are_windows_safe(self):
        from bot.recorder import Recorder
        r = Recorder("C:/x", PID)
        name = r.path_for("trades", "20260601T13").name
        self.assertFalse(set(name) & set('<>:"/\\|?*'))
        for p in BOT.rglob("*.py"):              # console Windows redirigée : cp1252
            p.read_text(encoding="utf-8").encode("cp1252")
        cfg = load_bot_config()
        for k in ("data_dir", "state_db", "paper_db", "log_dir"):
            self.assertTrue(cfg.storage_path(k).is_absolute())


if __name__ == "__main__":
    unittest.main()
