"""Barres et facteurs : causalité stricte (anti-lookahead) et cohérence des barres."""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scalp.data.synthetic import make_trades  # noqa: E402
from scalp.features import (FEATURE_COLS_ML, FEATURE_COLS_RESEARCH_ONLY, add_features,  # noqa: E402
                            feature_cols, funding_interval_hours, make_bars)
from scalp.model import LabelSpec, TradeModel  # noqa: E402

F = 5


def metrics_frame(t0, t1, seed=0):
    rng = np.random.default_rng(seed)
    ts = np.arange((t0 // 300_000) * 300_000, t1, 300_000, dtype=np.int64)
    ts = np.delete(ts, [7, 8])                       # trous dans le fichier metrics
    oi = 80_000 * np.exp(np.cumsum(rng.normal(0, 1e-3, ts.size)))
    return pd.DataFrame({"ts": ts, "sum_open_interest": oi,
                         "sum_taker_long_short_vol_ratio": rng.uniform(0.8, 1.2, ts.size)})


class TestCausality(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = make_trades(days=1, rate_per_s=1.5, null=False, seed=5)
        t0, t1 = int(cls.t["ts"][0]), int(cls.t["ts"][-1])
        cls.metrics = metrics_frame(t0, t1)
        cls.funding = pd.DataFrame({"ts": np.arange((t0 // 28_800_000) * 28_800_000, t1, 28_800_000),
                                    "rate": 0.0001})
        cls.full = add_features(make_bars(cls.t, F), F, metrics=cls.metrics, funding=cls.funding,
                                funding_interval_h=8.0)

    def test_truncated_equals_full(self):
        cols = [c for c in self.full.columns]
        for k in (100, 900, 5000, len(self.full) - 300):
            cut_ts = int(self.full["ts"].iloc[k])
            # tout ce qui est connu à la fin de la barre k : trades de ts < cut_ts
            m = self.t["ts"] < cut_ts
            tt = {key: v[m] for key, v in self.t.items()}
            part = add_features(make_bars(tt, F), F,
                                metrics=self.metrics[self.metrics["ts"] < cut_ts],
                                funding=self.funding[self.funding["ts"] < cut_ts], funding_interval_h=8.0)
            self.assertEqual(int(part["ts"].iloc[-1]), cut_ts)
            a = self.full.iloc[: len(part)][cols].reset_index(drop=True)
            pd.testing.assert_frame_equal(a, part[cols], check_dtype=False, rtol=1e-9, atol=1e-9,
                                          obj=f"troncature k={k}")

    def test_future_shock_does_not_change_past(self):
        t2 = {k: v.copy() for k, v in self.t.items()}
        cut = int(self.full["ts"].iloc[3000])
        t2["px"][t2["ts"] >= cut] *= 1.05                          # choc après la barre 3000
        t2["qty"][t2["ts"] >= cut] *= 10
        f2 = add_features(make_bars(t2, F), F, funding_interval_h=1.0)
        f1 = add_features(make_bars(self.t, F), F, funding_interval_h=1.0)
        pd.testing.assert_frame_equal(f1.iloc[:3001], f2.iloc[:3001])

    def test_metrics_publication_lag(self):
        # une ligne metrics horodatée T (début possible de la période de 5 min) n'est visible
        # qu'à partir de T + 360 s (5 min de période + 60 s de publication)
        m = self.metrics
        row = m.iloc[20]
        b = self.full
        before = b[(b["ts"] >= row.ts + 60_000) & (b["ts"] < row.ts + 360_000)]  # ligne 19 visible
        after = b[b["ts"] == row.ts + 360_000]
        self.assertEqual(len(before), 60)
        prev_row = m.iloc[19]
        self.assertTrue(np.isfinite(before["oi_chg_5m"]).all())
        self.assertTrue(np.all(before["sum_taker_long_short_vol_ratio"] == prev_row.sum_taker_long_short_vol_ratio))
        self.assertEqual(after["sum_taker_long_short_vol_ratio"].iloc[0], row.sum_taker_long_short_vol_ratio)

    def test_oi_change_uses_time_not_rows(self):
        m = self.metrics
        # ligne juste après le trou : la valeur 5 min avant n'existe pas -> NaN, pas un faux 15 min
        i_after_gap = 7
        ts = int(m["ts"].iloc[i_after_gap])
        b = self.full[self.full["ts"] == ts + 360_000]
        self.assertTrue(np.isnan(b["oi_chg_5m"].iloc[0]))
        self.assertTrue(np.isfinite(b["oi_chg_15m"].iloc[0]))

    def test_feature_columns(self):
        self.assertEqual(len(FEATURE_COLS_ML), 20)
        self.assertTrue(set(FEATURE_COLS_ML) <= set(self.full.columns))
        # metrics / funding / spot fournis : calculés (règles de référence) mais JAMAIS dans le modèle
        self.assertIn("oi_chg_5m", self.full.columns)
        self.assertIn("last_funding", self.full.columns)
        self.assertEqual(feature_cols(self.full), FEATURE_COLS_ML)
        self.assertFalse(set(FEATURE_COLS_RESEARCH_ONLY) & set(feature_cols(self.full)))
        with self.assertRaises(ValueError):                         # garde du modèle
            TradeModel(LabelSpec(12, 25, 60), FEATURE_COLS_ML + ["oi_chg_5m"])
        late = self.full.iloc[17_280:]                              # après 1 jour de chauffe
        self.assertFalse(late[FEATURE_COLS_ML].isna().any().any())
        self.assertFalse(np.isinf(late[FEATURE_COLS_ML].to_numpy(float)).any())


class TestBars(unittest.TestCase):
    def test_bar_boundaries_and_values(self):
        T = 1_780_272_000_000
        t = {"ts": np.array([T, T + 4999, T + 5000, T + 16000], np.int64),
             "px": np.array([100.0, 101.0, 99.0, 102.0]),
             "qty": np.array([1.0, 2.0, 3.0, 4.0], np.float32),
             "bm": np.array([False, True, False, True])}
        b = make_bars(t, 5)
        self.assertEqual(b["ts"].tolist(), [T + 5000, T + 10000, T + 15000, T + 20000])
        self.assertEqual(b["close"].tolist(), [101.0, 99.0, 99.0, 102.0])     # barre vide : close reporté
        self.assertEqual(b["high"].tolist(), [101.0, 99.0, 99.0, 102.0])
        self.assertEqual(b["buy_vol"].tolist(), [1.0, 3.0, 0.0, 0.0])
        self.assertEqual(b["vol"].tolist(), [3.0, 3.0, 0.0, 4.0])
        self.assertAlmostEqual(b["vwap"].iloc[0], (100 + 202) / 3)
        self.assertEqual(b["vwap"].iloc[2], 99.0)
        self.assertEqual(b["bid_proxy"].tolist(), [101.0, 101.0, 101.0, 102.0])
        self.assertEqual(b["ask_proxy"].tolist(), [100.0, 99.0, 99.0, 99.0])

    def test_unsorted_rejected(self):
        t = {"ts": np.array([2, 1], np.int64), "px": np.ones(2), "qty": np.ones(2, np.float32),
             "bm": np.zeros(2, bool)}
        with self.assertRaises(ValueError):
            make_bars(t, 5)

    def test_sec_to_funding_interval(self):
        t = make_trades(days=1, rate_per_s=0.2, seed=1)
        f1 = add_features(make_bars(t, 5), 5, funding_interval_h=1)
        self.assertTrue(((f1["sec_to_funding"] > 0) & (f1["sec_to_funding"] <= 3600)).all())



class TestLiveFeatureParity(unittest.TestCase):
    """Chaque facteur du MODÈLE est calculé par le chemin live (bot/bars.py : BarBuilder alimenté
    trade par trade, comme le moteur) et vaut exactement la valeur du chemin batch de la recherche
    (SyntheticSource.load_chunk, même config) sur le même flux de trades."""

    def test_model_features_live_equal_batch(self):
        from bot.bars import BarBuilder
        from scalp.config import deep_update, load_config
        from scalp import validation as V
        cfg = load_config()
        self.assertEqual(funding_interval_hours(cfg), 1.0)          # Kraken : funding horaire
        src = V.make_source({"kind": "synthetic", "null": False, "days": 0.08, "rate": 2.0, "seed": 3}, cfg)
        # une variante de sensibilité « binance » (funding 8 h) chargée en premier ne doit pas changer
        # les facteurs du modèle : ils restent ceux de la plateforme d'exécution du run (Kraken, 1 h)
        batch = src.load_chunk(src.days, deep_update(cfg, {"venue": {"execution": "binance"}})).df
        t = src.trades
        bb = BarBuilder(F, window_bars=10 ** 6, grace_ms=int(cfg["bars"]["close_grace_ms"]))
        for i in range(t["ts"].size):
            bb.add_trade(int(t["ts"][i]), float(t["px"][i]), float(t["qty"][i]), not bool(t["bm"][i]))
            bb.close_until(int(t["ts"][i]))
        bb.close_until(int(t["ts"][-1]) + 10 * F * 1000)
        live = bb.features(funding_interval_h=funding_interval_hours(cfg))
        cols = feature_cols(batch)
        self.assertEqual(cols, FEATURE_COLS_ML)
        self.assertTrue(set(cols) <= set(live.columns), set(cols) - set(live.columns))
        n = min(len(live), len(batch))
        self.assertGreater(n, 1000)
        pd.testing.assert_frame_equal(live[cols].iloc[:n].reset_index(drop=True),
                                      batch[cols].iloc[:n].reset_index(drop=True),
                                      check_dtype=False, rtol=1e-12, atol=1e-12)
        self.assertTrue((batch["sec_to_funding"] <= 3600).all())


if __name__ == "__main__":
    unittest.main()
