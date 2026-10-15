"""Tableau de bord (lecture seule) et sonde Kraken, sans réseau.

Vérifie : instantané d'état sérialisable et lisible par le tableau de bord ; base absente ou
instantané périmé signalés ; aucune écriture possible via HTTP ; parsing tolérant des
exécutions Kraken ; sonde de profondeur et verdict sur réponses simulées.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))      # aussi lançable hors « discover -s tests »

import kraken_probe as kp  # noqa: E402
from bot.app import build, replay  # noqa: E402
from bot.dashboard import make_server, read_dashboard  # noqa: E402
from bot.telegram import NullNotifier  # noqa: E402
from test_bot_engine import AlwaysLong, make_cfg, msgs_for  # noqa: E402


class TestDashboard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = make_cfg(self.tmp)
        self.eng, self.state, self.broker, _ = build(self.cfg, notifier=NullNotifier(), record=False,
                                                     strategy=AlwaysLong())
        self.msgs = msgs_for(20)
        replay(self.eng, self.msgs)
        self.end = self.msgs[-1][0]
        self.eng.write_snapshot(self.end)
        self.db = self.cfg.storage_path("state_db")

    def tearDown(self):
        self.state.close()
        self.broker.close()

    def test_snapshot_is_json_and_complete(self):
        snap = json.loads(json.dumps(self.eng.snapshot(self.end)))
        for k in ("feed", "market", "risk", "gates", "warmup_left_s", "alerts"):
            self.assertIn(k, snap)
        self.assertEqual(snap["risk"]["max_drawdown_usdt"], self.eng.risk.kill_dd_usdt)
        self.assertIsNotNone(snap["market"]["last_px"])

    def test_snapshot_failure_never_stops_the_robot(self):
        self.eng.snapshot = lambda now: {"x": object()}          # non sérialisable
        self.eng.write_snapshot(self.end)
        self.eng.write_snapshot(self.end)
        self.assertEqual(self.eng.monitor.counters["snapshot_errors"], 2)

    def test_read_dashboard_while_robot_holds_db(self):
        d = read_dashboard(self.db, now_ms=self.end + 1000)
        self.assertTrue(d["available"])
        self.assertFalse(d["stale"])
        self.assertGreater(len(d["prices"]), 100)
        self.assertLessEqual(len(d["prices"]), 1501)
        self.assertGreater(sum(d["decisions_24h"].values()), 0)
        n_closed = len(self.state.closed_positions())
        self.assertEqual(len(d["closed_positions"]), min(n_closed, 30))

    def test_stale_and_missing(self):
        d = read_dashboard(self.db, now_ms=self.end + 3_600_000)
        self.assertTrue(d["stale"])
        self.assertGreater(len(d["prices"]), 0)          # dernières heures du robot toujours visibles
        miss = read_dashboard(Path(self.tmp) / "absent.sqlite")
        self.assertFalse(miss["available"])
        self.assertIn("introuvable", miss["error"])
        self.assertFalse((Path(self.tmp) / "absent.sqlite").exists())   # lecture seule : rien créé

    def test_http_read_only(self):
        srv = make_server(self.db, port=0)
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            base = f"http://127.0.0.1:{port}"
            page = urllib.request.urlopen(base + "/", timeout=5).read().decode("utf-8")
            self.assertIn("Robot BTC", page)
            self.assertNotIn("http://", page.replace("http://127.0.0.1", ""))   # aucune ressource externe
            api = json.loads(urllib.request.urlopen(base + "/api/state", timeout=5).read())
            self.assertTrue(api["available"])
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(urllib.request.Request(base + "/api/state", data=b"x", method="POST"),
                                       timeout=5)
            self.assertEqual(cm.exception.code, 405)
        finally:
            srv.shutdown()
            srv.server_close()


def _exec_body(ts_list, token="tok"):
    """Forme supposée de /api/history/v3/market/.../executions (à confirmer sur Kraken)."""
    return {"elements": [{"uid": str(i), "timestamp": t, "event": {"Execution": {"execution": {
        "timestamp": t, "price": "100000", "quantity": "0.01", "takerOrder": {"direction": "Buy"}}}}}
        for i, t in enumerate(ts_list)], "continuationToken": token}


class TestKrakenProbe(unittest.TestCase):
    def test_parse_executions_tolerant(self):
        p = kp.parse_executions(_exec_body([1000, 2000]))
        self.assertEqual((p["n"], p["ts"], p["token"]), (2, [1000, 2000], "tok"))
        self.assertEqual(p["sample"]["price"], "100000")
        iso = kp.parse_executions({"trades": [{"price": 1, "qty": 2, "timestamp": "2026-01-01T00:00:00Z"}]})
        self.assertEqual(iso["ts"], [1767225600000])
        self.assertEqual(kp.parse_executions("texte")["n"], 0)

    def test_depth_and_verdict(self):
        now = 1_790_000_000_000
        limit_days = 200            # historique simulé disponible sur 200 jours

        def fake_get(url):
            q = dict(kp.urllib.parse.parse_qsl(url.split("?", 1)[1]))
            t = int(q["since"])
            if now - t > limit_days * kp.DAY_MS:
                return {"status": 200, "ms": 50, "body": {"elements": []}, "error": None}
            return {"status": 200, "ms": 50, "body": _exec_body([t + 1000, t + 2000]), "error": None}

        depth = kp.probe_depth(fake_get, now, sleep=lambda s: None)
        ok = [d["days_back"] for d in depth if d["n_in_window"]]
        self.assertEqual(ok, [1, 7, 30, 90, 180])
        rep = {"access": {k: {"status": 200, "error": None} for k in ("kraken_prod", "kraken_demo", "binance_data")},
               "exec_depth": depth, "exec_rate": {}}
        text = " ".join(kp.verdict(rep))
        self.assertIn("180 jours", text)
        self.assertIn("Moins de 12 mois", text)

    def test_rate_extrapolation_and_unreachable(self):
        now = 1_790_000_000_000
        calls = {"n": 0}

        def fake_get(url):
            calls["n"] += 1
            base = now - kp.DAY_MS + calls["n"] * 60_000
            return {"status": 200, "ms": 100, "body": _exec_body([base + i * 600 for i in range(100)]), "error": None}

        r = kp.probe_rate(fake_get, now, pages=5, sleep=lambda s: None)
        self.assertEqual((r["requests"], r["trades"]), (5, 500))
        self.assertTrue(r["pagination_works"])
        self.assertGreater(r["requests_for_24_months"], 0)
        down = kp.probe_rate(lambda u: {"status": None, "ms": 1, "body": None, "error": "403"}, now, 5,
                             sleep=lambda s: None)
        self.assertFalse(down["pagination_works"])
        self.assertEqual(down["trades"], 0)


if __name__ == "__main__":
    unittest.main()
