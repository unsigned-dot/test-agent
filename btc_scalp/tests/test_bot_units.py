"""Tests unitaires du robot : parsing Kraken, carnet, barres incrémentales, enregistreur, PaperBroker."""
from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from bot import kraken
from bot.bars import BAR_COLUMNS, BarBuilder
from bot.broker import OrderRequest, PaperBroker
from bot.config import load_bot_config
from bot.feed import LocalBook, fake_messages
from bot.recorder import Recorder, list_files, read_records
from scalp.data.synthetic import make_trades
from scalp.features import make_bars

PID = "PF_XBTUSD"


class TestKrakenParsing(unittest.TestCase):
    def test_documented_trade_messages(self):
        # exemples de https://docs.kraken.com/api/docs/futures-api/websocket/trade
        snap = {"feed": "trade_snapshot", "product_id": PID, "trades": [
            {"feed": "trade", "product_id": PID, "uid": "a", "side": "sell", "type": "fill", "seq": 655508,
             "time": 1612269657781, "qty": 440, "price": 34893},
            {"feed": "trade", "product_id": PID, "uid": "b", "side": "sell", "type": "fill", "seq": 655507,
             "time": 1612269656839, "qty": 9643, "price": 34891}]}
        ev = kraken.parse(snap, PID)
        self.assertEqual([e.seq for e in ev], [655507, 655508])          # remis dans l'ordre chronologique
        self.assertTrue(all(e.snapshot and not e.taker_buy for e in ev))
        upd = {"feed": "trade", "product_id": PID, "uid": "c", "side": "buy", "type": "liquidation",
               "seq": 653355, "time": 1612266317519, "qty": 15000, "price": 34969.5}
        (t,) = kraken.parse(upd, PID)
        self.assertTrue(t.taker_buy)
        self.assertEqual((t.px, t.kind, t.ts), (34969.5, "liquidation", 1612266317519))

    def test_documented_book_ticker_heartbeat(self):
        bs = {"feed": "book_snapshot", "product_id": PID, "timestamp": 1612269825817, "seq": 326072249,
              "tickSize": None, "bids": [{"price": 34892.5, "qty": 6385}], "asks": [{"price": 34911.5, "qty": 20598}]}
        (s,) = kraken.parse(bs, PID)
        self.assertEqual(s.bids, [(34892.5, 6385.0)])
        bd = {"feed": "book", "product_id": PID, "side": "sell", "seq": 326094134, "price": 34981, "qty": 0,
              "timestamp": 1612269953629}
        (d,) = kraken.parse(bd, PID)
        self.assertFalse(d.is_bid)
        tk = {"time": 1676393235406, "product_id": PID, "funding_rate": -6.26e-11,
              "relative_funding_rate": -1.38e-6, "next_funding_rate_time": 1676394000000, "feed": "ticker",
              "bid": 21978.5, "ask": 21987.0, "markPrice": 21979.68, "index": 21984.54, "suspended": False,
              "post_only": False, "openInterest": 30072580.0}
        (t,) = kraken.parse(tk, PID)
        self.assertAlmostEqual(t.relative_funding_rate, -1.38e-6)
        (h,) = kraken.parse({"feed": "heartbeat", "time": 1534262350627}, PID)
        self.assertEqual(h.ts, 1534262350627)

    def test_tolerant_parsing(self):
        for bad in [None, [], {}, {"feed": "trade"}, {"feed": "trade", "product_id": PID, "side": "x",
                                                     "time": 1, "price": 1, "qty": 1},
                    {"feed": "trade", "product_id": "PF_ETHUSD", "side": "buy", "time": 1, "price": 1, "qty": 1},
                    {"feed": "book", "product_id": PID, "price": "abc", "qty": 1, "side": "buy"},
                    {"feed": "unknown_feed"}]:
            self.assertEqual(kraken.parse(bad, PID), [])
        (c,) = kraken.parse({"event": "error", "message": "Invalid product id"}, PID)
        self.assertEqual(c.event, "error")

    def test_subscribe_and_rest_helpers(self):
        msgs = [json.loads(m) for m in kraken.subscribe_all(PID)]
        self.assertEqual({m["feed"] for m in msgs}, {"trade", "book", "ticker", "heartbeat"})
        self.assertTrue(all(m["event"] == "subscribe" for m in msgs))
        self.assertEqual(kraken.parse_server_time("2022-06-28T09:29:04.243Z"), 1656408544243)
        off = kraken.clock_offset_ms({"serverTime": "2022-06-28T09:29:04.243Z"}, 1656408544243 + 900,
                                     1656408544243 + 1100)
        self.assertEqual(off, 1000)
        ins = {"instruments": [{"symbol": "PF_XBTUSD", "tickSize": 1, "contractValueTradePrecision": 4,
                                "tradeable": True}]}
        self.assertEqual(kraken.check_instrument(kraken.find_instrument(ins, PID), 1.0, 0.0001), [])
        ins["instruments"][0]["tickSize"] = 0.5
        self.assertEqual(len(kraken.check_instrument(kraken.find_instrument(ins, PID), 1.0, 0.0001)), 1)


class TestLocalBook(unittest.TestCase):
    def test_snapshot_delta_gap(self):
        b = LocalBook()
        b.snapshot(kraken.BookSnapshot(0, 10, [(100.0, 1.0), (99.0, 2.0)], [(101.0, 1.0), (102.0, 3.0)]))
        self.assertEqual(b.best(), (100.0, 101.0))
        self.assertTrue(b.delta(kraken.BookDelta(1, 11, False, 101.0, 0.0)))     # suppression
        self.assertEqual(b.best(), (100.0, 102.0))
        self.assertAlmostEqual(b.sweep_price(+1, 2.0), 102.0)
        self.assertFalse(b.delta(kraken.BookDelta(2, 13, True, 100.0, 5.0)))     # trou de séquence
        self.assertFalse(b.valid)
        self.assertEqual(b.best(), (None, None))


class TestIncrementalBars(unittest.TestCase):
    def test_same_bars_as_research(self):
        """Barres construites en direct == scalp.features.make_bars sur l'historique complet."""
        t = make_trades(days=0.02, rate_per_s=2.0, tick=1.0, seed=5)
        ref = make_bars(t, 5)
        bb = BarBuilder(5, window_bars=100000, grace_ms=0)
        out = []
        for i in range(t["ts"].size):
            bb.add_trade(int(t["ts"][i]), float(t["px"][i]), float(t["qty"][i]), not bool(t["bm"][i]))
            out += bb.close_until(int(t["ts"][i]))
        out += bb.close_until(int(ref["ts"].iloc[-1]))
        live = pd.DataFrame(out, columns=BAR_COLUMNS)
        n = len(live)
        self.assertGreater(n, 300)
        pd.testing.assert_frame_equal(live.reset_index(drop=True), ref[BAR_COLUMNS].iloc[:n].reset_index(drop=True),
                                      check_dtype=False, rtol=1e-12)

    def test_late_trade_ignored(self):
        bb = BarBuilder(5, grace_ms=0)
        bb.add_trade(10_000, 100.0, 1.0, True)
        bb.close_until(16_000)
        self.assertFalse(bb.add_trade(12_000, 101.0, 1.0, True))
        self.assertEqual(bb.late_trades, 1)


class TestRecorder(unittest.TestCase):
    def test_writes_readable_hourly_gz(self):
        with tempfile.TemporaryDirectory() as d:
            r = Recorder(d, PID, flush_interval_s=0)
            t = make_trades(days=0.05, rate_per_s=1.0, tick=1.0, seed=1)
            msgs = fake_messages(t, PID)
            for recv, m in msgs:
                r.record(recv, m)
            r.close()
            files = list_files(d, PID)
            self.assertTrue(any(f.name.startswith("trades_") for f in files))
            self.assertTrue(any(f.name.startswith("book_") for f in files))
            self.assertTrue(any(f.name.startswith("ticker_") for f in files))
            back = [rec for f in files for rec in read_records(f)]
            self.assertEqual(len(back), len(msgs))
            trades_back = [rec["m"] for f in list_files(d, PID, "trades") for rec in read_records(f)]
            n_tr = sum(1 for _, m in msgs if m.get("feed") == "trade" and "event" not in m)
            self.assertEqual(len(trades_back), n_tr)
            self.assertEqual(trades_back[0]["price"], float(t["px"][0]))
            # fichier tronqué (crash pendant l'écriture) : relu sans exception
            f0 = files[0]
            raw = f0.read_bytes()
            f0.write_bytes(raw + gzip.compress(b'{"r":1,"m":{}}\n{"r":2,"m"')[:-10])
            list(read_records(f0))


class TestPaperBroker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = load_bot_config()
        self.b = PaperBroker(Path(self.tmp.name) / "p.sqlite", cfg.venue, cfg.research["costs"])

    def tearDown(self):
        self.b.close()
        self.tmp.cleanup()

    def test_market_fill_after_latency_and_idempotent(self):
        o = self.b.submit(OrderRequest("X-E", 1, "market", 0.01), 1000)
        self.assertEqual(self.b.submit(OrderRequest("X-E", 1, "market", 0.01), 1001)["seq"], o["seq"])
        self.b.on_trade(1300, 100000.0, True)            # pas après l'arrivée (latence 300 ms)
        self.assertEqual(self.b.get_order("X-E")["status"], "pending")
        self.b.on_trade(1400, 100000.0, False)           # vendeur agresseur => ask = bid + 1 tick
        o = self.b.get_order("X-E")
        self.assertEqual(o["status"], "filled")
        self.assertGreaterEqual(o["fill_px"], 100001.0)
        self.assertEqual(o["liquidity"], "taker")
        self.assertAlmostEqual(self.b.position()["qty"], 0.01)

    def test_limit_needs_trade_through_and_reduce_only(self):
        self.b.submit(OrderRequest("A-E", 1, "market", 0.01), 0)
        self.b.on_trade(300, 100000.0, True)
        self.b.submit(OrderRequest("A-T", -1, "limit", 0.01, price=100100.0, reduce_only=True), 400)
        self.b.on_trade(700, 100100.0, True)             # touché, pas traversé
        self.assertEqual(self.b.get_order("A-T")["status"], "open")
        self.b.on_trade(800, 100101.0, True)             # traversé
        o = self.b.get_order("A-T")
        self.assertEqual((o["status"], o["fill_px"], o["liquidity"]), ("filled", 100100.0, "maker"))
        self.assertEqual(self.b.position()["qty"], 0)
        # reduce-only sans position : ne peut rien ouvrir
        self.b.submit(OrderRequest("B-X", 1, "market", 0.01, reduce_only=True), 1000)
        self.b.on_trade(1400, 100000.0, True)
        self.assertEqual(self.b.get_order("B-X")["status"], "cancelled")
        self.assertEqual(self.b.position()["qty"], 0)

    def test_stop_triggers_then_market_after_latency(self):
        self.b.submit(OrderRequest("C-E", 1, "market", 0.01), 0)
        self.b.on_trade(300, 100000.0, True)
        self.b.submit(OrderRequest("C-S", -1, "stop", 0.01, stop_px=99750.0, reduce_only=True), 300)
        self.b.on_trade(1000, 99750.0, False)
        self.assertEqual(self.b.get_order("C-S")["status"], "triggered")
        self.b.on_trade(1100, 99700.0, False)            # avant stop_latency
        self.assertEqual(self.b.get_order("C-S")["status"], "triggered")
        self.b.on_trade(1400, 99600.0, False)
        o = self.b.get_order("C-S")
        self.assertEqual(o["status"], "filled")
        self.assertLessEqual(o["fill_px"], 99600.0)

    def test_entry_rejected_on_data_gap(self):
        self.b.submit(OrderRequest("D-E", 1, "market", 0.01), 0)
        self.b.on_time(6000)
        self.assertEqual(self.b.get_order("D-E")["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
