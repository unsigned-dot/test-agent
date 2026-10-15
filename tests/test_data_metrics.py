"""Validation des données, métriques (bootstrap, PSR/DSR), configuration."""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scalp import metrics  # noqa: E402
from scalp.config import config_hash, load_config, venue_profile  # noqa: E402
from scalp.data.download import build_urls  # noqa: E402
from scalp.data.synthetic import make_trades  # noqa: E402
from scalp.data.validate import validate_trades  # noqa: E402

DAY = 86_400_000


class TestValidate(unittest.TestCase):
    def setUp(self):
        self.t = make_trades(days=2, rate_per_s=0.5, seed=2)

    def test_clean(self):
        r = validate_trades(self.t)
        self.assertTrue(r["ok"], r)

    def test_id_gaps(self):
        t = {k: np.delete(v, [100, 101, 102, 5000]) for k, v in self.t.items()}
        r = validate_trades(t)
        self.assertEqual((r["agg_id_jumps"], r["missing_agg_ids_total"]), (2, 4))
        self.assertFalse(r["ok"])

    def test_duplicates(self):
        t = {k: np.insert(v, 50, v[49]) for k, v in self.t.items()}
        r = validate_trades(t)
        self.assertEqual(r["duplicate_agg_ids"], 1)
        self.assertFalse(r["ok"])

    def test_non_monotonic(self):
        t = {k: v.copy() for k, v in self.t.items()}
        t["ts"][10], t["ts"][11] = t["ts"][11], t["ts"][10] - 1
        r = validate_trades(t)
        self.assertGreaterEqual(r["non_monotonic_timestamps"], 1)
        self.assertFalse(r["ok"])

    def test_bad_prices_off_tick_and_empty_day(self):
        t = {k: v.copy() for k, v in self.t.items()}
        t["px"][3] = -1.0
        t["px"][4] = t["px"][5] + 0.05
        r = validate_trades(t)
        self.assertEqual(r["non_positive_prices"], 1)
        self.assertEqual(r["off_tick_prices"], 1)         # seul le prix +0,05 est hors tick 0,1
        self.assertFalse(r["ok"])
        t2 = make_trades(days=3, rate_per_s=0.2, seed=3)
        keep = t2["ts"] // DAY != t2["ts"][0] // DAY + 1      # 2e jour UTC entièrement vide
        t2 = {k: v[keep] for k, v in t2.items()}
        t2["aid"] = np.arange(t2["aid"].size)             # ids continus : seul le jour vide est fautif
        self.assertEqual(validate_trades(t2)["empty_days_inside_range"], 1)

    def test_kraken_tick(self):
        t = make_trades(days=1, rate_per_s=0.2, tick=1.0, seed=4)
        self.assertEqual(validate_trades(t, tick=1.0)["off_tick_prices"], 0)


class TestMetrics(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(0)
        n = 400
        ts = 1_780_272_000_000 + np.sort(rng.integers(0, 40 * DAY, n))
        net_bps = rng.normal(1.0, 10.0, n)
        self.tr = pd.DataFrame({"entry_ts": ts, "exit_ts": ts + 60_000, "net_bps": net_bps,
                                "net_pnl": net_bps * 0.1, "gross_pnl": net_bps * 0.1 + 0.2,
                                "gross_bps": net_bps + 2, "fees": 0.2, "funding": 0.0,
                                "notional": 1000.0, "side": rng.choice([-1, 1], n),
                                "exit_reason": "stop", "taker_legs": 2})

    def test_bootstrap_reproducible(self):
        a = metrics.trade_bootstrap_by_day(self.tr, 500, seed=7)
        b = metrics.trade_bootstrap_by_day(self.tr, 500, seed=7)
        self.assertEqual(a, b)
        lo, hi, p = a
        self.assertLess(lo, self.tr["net_bps"].mean())
        self.assertGreater(hi, self.tr["net_bps"].mean())
        self.assertTrue(0 <= p <= 1)
        c = metrics.block_bootstrap_mean(self.tr["net_bps"].values, 5, 300, seed=1)
        self.assertEqual(c, metrics.block_bootstrap_mean(self.tr["net_bps"].values, 5, 300, seed=1))

    def test_psr_dsr_in_unit_interval(self):
        daily = metrics.daily_pnl(self.tr, int(self.tr.entry_ts.min()), int(self.tr.exit_ts.max()))
        r = (daily / 1000).values
        sr = r.mean() / r.std(ddof=1)
        from scipy import stats
        psr = metrics.probabilistic_sharpe(sr, r.size, float(stats.skew(r)), float(stats.kurtosis(r, fisher=False)))
        dsr = metrics.deflated_sharpe(daily, 1000.0, n_trials=20, sr_trials_var=0.01)
        for v in (psr, dsr):
            self.assertTrue(0.0 <= v <= 1.0, v)
        self.assertLessEqual(dsr, psr + 1e-12)            # plus d'essais -> seuil plus exigeant
        self.assertAlmostEqual(daily.sum(), self.tr.net_pnl.sum())

    def test_summary_and_sensitivities(self):
        s = metrics.summarize(self.tr, 1000.0)
        self.assertEqual(s["n_trades"], 400)
        self.assertAlmostEqual(s["net_pnl"], self.tr.net_pnl.sum())
        sl = metrics.slippage_sensitivity(self.tr)
        self.assertAlmostEqual(sl["+1.0bps"], self.tr.net_bps.mean() - 2.0)
        fs = metrics.fee_sensitivity(self.tr, load_config())
        self.assertAlmostEqual(fs["maker2.0/taker5.0"], self.tr.net_bps.mean())
        self.assertAlmostEqual(fs["maker0.0/taker0.0"], self.tr.net_bps.mean() + 10.0)


class TestConfig(unittest.TestCase):
    def test_profiles(self):
        cfg = load_config()
        self.assertEqual(cfg["venue"]["execution"], "kraken")
        k, b = venue_profile(cfg), venue_profile(cfg, "binance")
        self.assertEqual((k["tick_size"], k["lot_size"], k["funding_interval_hours"]), (1.0, 0.0001, 1))
        self.assertEqual((b["tick_size"], b["funding_interval_hours"]), (0.1, 8))
        with self.assertRaises(KeyError):
            venue_profile(cfg, "ftx")
        self.assertEqual(config_hash(cfg), config_hash(load_config()))

    def test_urls(self):
        u = build_urls("BTCUSDT", "fundingRate", "2024-10-01", "2024-11-02")
        self.assertEqual(len(u), 2)
        self.assertTrue(u[0][0].endswith("/futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2024-10.zip"))
        self.assertEqual(len(build_urls("BTCUSDT", "aggTrades", "2024-10-01", "2024-10-03")), 3)


if __name__ == "__main__":
    unittest.main()
