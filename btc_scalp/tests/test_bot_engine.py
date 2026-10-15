"""Scénarios du robot paper rejoués sur un faux flux Kraken (aucun réseau).

Vérifie : refus hors mode paper ; aucune entrée sans stratégie validée ; veto du risque ;
arrêt sur données périmées ; reprise après redémarrage sans double ordre ; /stop et refus
de toute autre commande ; persistance du coupe-circuit ; idempotence au crash.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bot.app import build, replay
from bot.broker import OrderRequest
from bot.config import ConfigError, ModeError, load_bot_config
from bot.feed import fake_messages
from bot.strategy import Signal, Strategy
from bot.telegram import NullNotifier, Telegram
from scalp.data.synthetic import make_trades

MONDAY = 1_780_272_000_000          # 2026-06-01 00:00 UTC, un lundi
SATURDAY = MONDAY + 5 * 86_400_000


class AlwaysLong(Strategy):
    """Stratégie de test : veut entrer long à chaque barre (pour exercer veto et mécanique)."""
    name = "test_always_long"
    dry = True

    def __init__(self):
        self.calls = 0

    def decide(self, feats):
        self.calls += 1
        return Signal(+1, note="test")


class FakeTelegram(Telegram):
    """Faux Telegram : enregistre les envois, sert des updates préparés."""

    def __init__(self, chat_id="42"):
        self.sent, self.updates = [], []
        super().__init__("TOKEN", chat_id, transport=self._transport)

    def _transport(self, url, data, timeout):
        if url.endswith("/sendMessage"):
            self.sent.append(data["text"])
            return {"ok": True}
        if url.endswith("/getUpdates"):
            off = data.get("offset", 0)
            return {"ok": True, "result": [u for u in self.updates if u["update_id"] >= off]}
        raise AssertionError(url)


def make_cfg(tmp: str, **bot_overrides):
    ov = {"storage": {"data_dir": f"{tmp}/data", "state_db": f"{tmp}/state.sqlite",
                      "paper_db": f"{tmp}/paper.sqlite", "log_dir": f"{tmp}/log"},
          "bars": {"warmup_s": 60, "window_hours": 25}, "feed": {"post_gap_pause_s": 60}}
    for k, v in bot_overrides.items():
        ov.setdefault(k, {}).update(v)
    return load_bot_config(overrides=ov)


def msgs_for(minutes: float, start=MONDAY, seed=0, **kw):
    t = make_trades(days=minutes / 1440, rate_per_s=2.0, daily_vol=0.03, tick=1.0, start_ms=start, seed=seed)
    return fake_messages(t, "PF_XBTUSD", **kw)


class EngineCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.opened = []

    def tearDown(self):
        for st, br in self.opened:
            try:
                st.close()
                br.close()
            except Exception:
                pass
        self._tmp.cleanup()

    def engine(self, cfg=None, strategy=None, notifier=None, record=False):
        cfg = cfg or make_cfg(self.tmp)
        eng, st, br, rec = build(cfg, notifier=notifier or NullNotifier(), record=record, strategy=strategy)
        self.opened.append((st, br))
        return eng, st, br


class TestStartupMode(unittest.TestCase):
    def test_refuses_non_paper_mode(self):
        for mode in ("live", "real", "demo", "", None):
            with self.assertRaises(ModeError):
                load_bot_config(overrides={"mode": mode})

    def test_refuses_risk_section_in_bot_config(self):
        with self.assertRaises(ConfigError):
            load_bot_config(overrides={"risk": {"max_leverage": 10}})

    def test_cli_refuses_non_paper(self):
        with tempfile.TemporaryDirectory() as d:
            src = (Path(__file__).resolve().parents[1] / "bot" / "config.toml").read_text(encoding="utf-8")
            p = Path(d) / "config.toml"
            p.write_text(src.replace('mode = "paper"', 'mode = "live"').replace(
                '"../config/default.toml"', repr(str(Path(__file__).resolve().parents[1] / "config" / "default.toml")).replace("'", '"')),
                encoding="utf-8")
            from bot.__main__ import main
            with self.assertRaises(SystemExit) as cm:
                main(["--config", str(p), "check-config"])
            self.assertEqual(cm.exception.code, 2)

    def test_engine_refuses_non_paper_broker(self):
        from bot.engine import Engine
        cfg = load_bot_config()

        class RealBroker:
            is_paper = False
        with self.assertRaises(SystemExit):
            Engine(cfg, None, RealBroker())


class TestNoStrategy(EngineCase):
    def test_default_strategy_never_enters(self):
        eng, st, br = self.engine()
        self.assertEqual(eng.strategy.name, "none")
        replay(eng, msgs_for(30))
        counts = st.decision_counts()
        self.assertNotIn("entry", counts)
        self.assertGreater(counts.get("no_signal", 0), 100)
        self.assertEqual(st.open_positions() + st.closed_positions(), [])
        self.assertEqual(br.open_orders(), [])
        self.assertEqual(br.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)


class TestRiskVeto(EngineCase):
    def test_weekend_veto_blocks(self):
        eng, st, br = self.engine(strategy=AlwaysLong())
        replay(eng, msgs_for(20, start=SATURDAY))
        rows = st.db.execute("SELECT outcome, reason FROM decisions WHERE outcome='veto'").fetchall()
        self.assertTrue(rows)
        self.assertTrue(all(r["reason"] == "weekend" for r in rows))
        self.assertEqual(br.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)

    def test_daily_loss_halt_blocks(self):
        eng, st, br = self.engine(strategy=AlwaysLong())
        eng.risk._roll(MONDAY)
        eng.risk.on_trade_closed(MONDAY, -40.0)          # -4 % > limite journalière -3 %
        eng._persist_risk()
        replay(eng, msgs_for(15))
        reasons = {r[0] for r in st.db.execute("SELECT reason FROM decisions WHERE outcome='veto'")}
        self.assertEqual(reasons, {"loss_limit_halt"})
        self.assertEqual(br.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)

    def test_dry_trades_follow_mandate(self):
        eng, st, br = self.engine(strategy=AlwaysLong())
        replay(eng, msgs_for(60))
        closed = st.closed_positions()
        self.assertGreater(len(closed), 0)
        for p in closed:
            # sizing : ~0,5 % de risque / (stop + coûts) ; jamais au-delà du plafond de marge
            self.assertLessEqual(p["qty"] * p["entry_px"], 1000 * 3 * 0.5 + 1e-6)
            self.assertTrue(p["dry"])
            orders = {o["leg"]: o for o in st.orders_for(p["base_id"])}
            self.assertIn("S", orders)                   # stop posé
            self.assertEqual(orders["S"]["reduce_only"], 1)
            self.assertLessEqual(orders["S"]["created_ms"] - p["entry_ms"], 1000)
        veto = {r[0] for r in st.db.execute("SELECT reason FROM decisions WHERE outcome='veto'")}
        self.assertTrue(veto <= {"cooldown", "max_trades_per_day", "loss_limit_halt", "kill_switch"}, veto)


class TestStaleData(EngineCase):
    def test_stale_feed_pauses_and_flattens(self):
        t0 = MONDAY
        silence = (t0 + 10 * 60_000, t0 + 14 * 60_000)    # 4 min sans aucun message
        eng, st, br = self.engine(strategy=AlwaysLong())
        msgs = msgs_for(20, silences=[silence])
        replay(eng, msgs)
        rows = st.db.execute("SELECT bar_ts, outcome, reason FROM decisions WHERE bar_ts > ? AND bar_ts < ?",
                             (silence[0] + 20_000, silence[1])).fetchall()
        self.assertTrue(rows)
        self.assertTrue(all(r["outcome"] in ("blocked", "paused") and "stale_feed" in r["reason"] for r in rows))
        self.assertEqual(st.db.execute("SELECT COUNT(*) FROM decisions WHERE outcome='entry' AND bar_ts>? AND bar_ts<?",
                                       (silence[0], silence[1] + 60_000)).fetchone()[0], 0)
        # une position ouverte avant la coupure a été fermée (stop, durée max ou péremption)
        for p in st.db.execute("SELECT * FROM positions WHERE entry_ms < ? AND status!='abandoned'", (silence[0],)):
            self.assertEqual(p["status"], "closed")
        kinds = {e["kind"] for e in st.events(500)}
        self.assertIn("stale_feed", kinds)

    def test_trade_seq_gap_pauses(self):
        eng, st, br = self.engine(strategy=AlwaysLong())
        replay(eng, msgs_for(10, seq_gap_at=600))
        self.assertGreaterEqual(eng.monitor.counters.get("trade_seq_gaps", 0), 1)
        self.assertTrue(st.db.execute("SELECT COUNT(*) FROM decisions WHERE reason LIKE '%trade_seq_gap%'").fetchone()[0])

    def test_clock_offset_pauses(self):
        eng, st, br = self.engine(strategy=AlwaysLong())
        msgs = msgs_for(5)
        eng.start(msgs[0][0])
        eng.set_clock_offset(5000, msgs[0][0])
        eng.started_ms = msgs[0][0] - 10**9                # warmup passé
        n = len(msgs) // 2
        for recv, m in msgs[:n]:
            eng.handle(recv, m)
        self.assertIn("clock_offset", eng.gate_reasons(msgs[n][0]))
        self.assertIn("clock_offset", eng.monitor.active())


class TestRestart(EngineCase):
    def test_restart_no_duplicate_orders(self):
        cfg = make_cfg(self.tmp)
        msgs = msgs_for(40)
        cut = len(msgs) // 2
        eng, st, br = self.engine(cfg, strategy=AlwaysLong())
        replay(eng, msgs[:cut])
        n_orders_1 = br.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        self.assertGreater(n_orders_1, 0)
        st.close(); br.close(); self.opened.clear()
        # redémarrage : même base, flux qui reprend (avec recouvrement de messages)
        eng2, st2, br2 = self.engine(cfg, strategy=AlwaysLong())
        self.assertEqual(st2.get("reconcile_divergence", ""), "")
        replay(eng2, [(msgs[cut][0] - 1, {"_feed": "connected"})] + msgs[cut - 2000:])
        ids = [r[0] for r in br2.db.execute("SELECT client_id FROM orders")]
        self.assertEqual(len(ids), len(set(ids)))
        entries = [i for i in ids if i.endswith("-E")]
        bars = [r[0] for r in st2.db.execute("SELECT bar_ts FROM decisions WHERE outcome='entry'")]
        self.assertEqual(len(entries), len(bars))
        self.assertEqual(len(bars), len(set(bars)))
        # jamais deux positions simultanées
        pos = st2.db.execute("SELECT entry_ms, exit_ms FROM positions WHERE status='closed' ORDER BY entry_ms").fetchall()
        for a, b in zip(pos, pos[1:]):
            self.assertLessEqual(a["exit_ms"], b["entry_ms"])

    def test_crash_between_intent_and_send(self):
        """Intention écrite mais jamais transmise : abandonnée au redémarrage, jamais renvoyée."""
        cfg = make_cfg(self.tmp)
        eng, st, br = self.engine(cfg)
        st.add_position({"base_id": "L123", "side": 1, "qty": 0.01, "status": "pending_entry", "signal_ts": 123, "dry": 0})
        st.add_order({"client_id": "L123-E", "base_id": "L123", "leg": "E", "side": 1, "type": "market", "qty": 0.01,
                      "price": None, "stop_px": None, "reduce_only": 0, "status": "intent", "created_ms": 1,
                      "updated_ms": 1})
        eng.risk.position_open = True
        eng._persist_risk()
        st.close(); br.close(); self.opened.clear()
        eng2, st2, br2 = self.engine(cfg)
        eng2.start(MONDAY)
        self.assertEqual(st2.position("L123")["status"], "abandoned")
        self.assertIsNone(br2.get_order("L123-E"))
        self.assertFalse(eng2.risk.position_open)

    def test_crash_after_fill_before_stop_resends_stop(self):
        """Entrée remplie mais stop pas encore transmis : le stop est posé au redémarrage (même id)."""
        cfg = make_cfg(self.tmp)
        eng, st, br = self.engine(cfg)
        st.add_position({"base_id": "L9", "side": 1, "qty": 0.01, "status": "open", "signal_ts": 9, "dry": 0})
        st.update_position("L9", entry_px=100000.0, entry_ms=MONDAY, stop_px=99750.0, tp_px=100350.0,
                           deadline_ms=MONDAY + 300_000)
        st.add_order({"client_id": "L9-E", "base_id": "L9", "leg": "E", "side": 1, "type": "market", "qty": 0.01,
                      "price": None, "stop_px": None, "reduce_only": 0, "status": "filled", "created_ms": 1, "updated_ms": 1})
        br.submit(OrderRequest("L9-E", 1, "market", 0.01), MONDAY - 1000)
        br.on_trade(MONDAY, 100000.0, True)
        st.add_order({"client_id": "L9-S", "base_id": "L9", "leg": "S", "side": -1, "type": "stop", "qty": 0.01,
                      "price": None, "stop_px": 99750.0, "reduce_only": 1, "status": "intent", "created_ms": 2,
                      "updated_ms": 2})
        st.close(); br.close(); self.opened.clear()
        eng2, st2, br2 = self.engine(cfg)
        eng2.start(MONDAY + 1000)
        self.assertEqual(br2.get_order("L9-S")["status"], "open")
        self.assertEqual(st2.get("reconcile_divergence"), "")
        eng2.reconcile(MONDAY + 2000)                     # second rapprochement : aucun double
        self.assertEqual(br2.db.execute("SELECT COUNT(*) FROM orders WHERE client_id='L9-S'").fetchone()[0], 1)

    def test_divergence_pauses(self):
        cfg = make_cfg(self.tmp)
        eng, st, br = self.engine(cfg)
        br.submit(OrderRequest("ZZ-E", 1, "market", 0.01), MONDAY)      # ordre inconnu de l'état local
        br.on_trade(MONDAY + 500, 100000.0, True)
        eng.start(MONDAY + 1000)
        self.assertIn("reconcile_divergence", eng.gate_reasons(MONDAY + 1000))


class TestCommands(EngineCase):
    def test_stop_and_refusals_via_fake_telegram(self):
        tg = FakeTelegram()
        eng, st, br = self.engine(strategy=AlwaysLong(), notifier=tg)
        msgs = msgs_for(30)
        t_mid = msgs[len(msgs) // 2][0]
        before = dict(eng.risk.to_state())
        tg.updates = [{"update_id": i + 1, "message": {"chat": {"id": 42}, "text": txt}} for i, txt in enumerate(
            ["/set max_leverage 10", "/resume", "/risk 0.05", "/start", "/status extra", "/reset_kill"])]
        tg.updates.append({"update_id": 99, "message": {"chat": {"id": 666}, "text": "/stop"}})   # autre chat
        texts = tg.poll_commands()
        self.assertEqual(len(texts), 6)                   # le chat étranger est ignoré
        replies = [eng.command(t, msgs[0][0]) for t in texts]
        self.assertTrue(all(r.startswith("Commande refusée") for r in replies))
        self.assertEqual(eng.risk.max_leverage, 3.0)
        self.assertEqual(eng.risk.risk_fraction, 0.005)
        self.assertEqual(eng.risk.to_state()["killed"], before["killed"])
        self.assertFalse(eng.stopped)
        replies = replay(eng, msgs, commands={t_mid: "/status", t_mid + 1000: "/stop"})
        self.assertIn("Mode PAPER", replies[0])
        self.assertIn("Arrêt", replies[1])
        self.assertTrue(st.get("stopped"))
        late = st.db.execute("SELECT COUNT(*) FROM decisions WHERE outcome='entry' AND decided_ms > ?",
                             (t_mid + 1000,)).fetchone()[0]
        self.assertEqual(late, 0)
        self.assertEqual(st.open_positions(), [])         # position fermée par /stop
        self.assertTrue(any("manual_stop" in s for s in tg.sent))

    def test_stop_persists_after_restart(self):
        cfg = make_cfg(self.tmp)
        eng, st, br = self.engine(cfg, strategy=AlwaysLong())
        eng.command("/stop", MONDAY)
        st.close(); br.close(); self.opened.clear()
        eng2, st2, br2 = self.engine(cfg, strategy=AlwaysLong())
        replay(eng2, msgs_for(10))
        self.assertEqual(st2.db.execute("SELECT COUNT(*) FROM decisions WHERE outcome='entry'").fetchone()[0], 0)
        self.assertIn("stopped", eng2.gate_reasons(MONDAY + 600_000))


class TestKillSwitch(EngineCase):
    def test_kill_switch_persists_after_restart(self):
        cfg = make_cfg(self.tmp)
        eng, st, br = self.engine(cfg, strategy=AlwaysLong())
        eng.risk._roll(MONDAY)
        eng.risk.on_trade_closed(MONDAY, -151.0)          # perte >= 150 USDT depuis le sommet
        self.assertTrue(eng.risk.killed)
        eng._persist_risk()
        st.close(); br.close(); self.opened.clear()
        eng2, st2, br2 = self.engine(cfg, strategy=AlwaysLong())
        self.assertTrue(eng2.risk.killed)
        replay(eng2, msgs_for(15, start=MONDAY + 7 * 86_400_000))   # même une semaine plus tard
        reasons = {r[0] for r in st2.db.execute("SELECT reason FROM decisions WHERE outcome='veto'")}
        self.assertEqual(reasons, {"kill_switch"})
        self.assertEqual(br2.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)
        self.assertTrue(eng2.command("/stop", MONDAY).startswith("Arrêt"))
        self.assertTrue(eng2.command("/reset_kill HUGO_AUTORISE_REDEMARRAGE", MONDAY).startswith("Commande refusée"))
        self.assertTrue(eng2.risk.killed)


if __name__ == "__main__":
    unittest.main()
