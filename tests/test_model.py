"""Tests du modèle : cible = futur uniquement, features = passé uniquement, persistance."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scalp import features  # noqa: E402
from scalp.config import load_config  # noqa: E402
from scalp.data.synthetic import make_trades  # noqa: E402
from scalp.execution import ExecParams  # noqa: E402
from scalp.model import (LabelSpec, TradeModel, cost_bps, expected_gain_bps, feature_columns,  # noqa: E402
                         feature_matrix, make_labels)

CFG = load_config(Path(__file__).resolve().parents[1] / "config" / "default.toml")


def tiny_trades():
    """Trades écrits à la main (ms) : prix 100 puis montée à 100.2 à t=3000."""
    ts = np.array([0, 500, 1000, 1500, 2000, 3000, 4000, 9000], np.int64)
    px = np.array([100, 100, 100, 100, 100, 100.2, 100.2, 100.0])
    return {"ts": ts, "px": px, "qty": np.ones(ts.size, np.float32), "bm": np.zeros(ts.size, bool)}


class TestLabels(unittest.TestCase):
    def test_label_basic(self):
        t = tiny_trades()
        spec = LabelSpec(tp_bps=12, stop_bps=25, horizon_s=5)
        yl, ys, end = make_labels(t, np.array([1000]), spec, latency_ms=250)
        # entrée au 1er trade après 1250 ms = 1500 (px 100) ; 100.2 > 100.12 avant tout stop
        self.assertEqual(yl[0], 1.0)
        self.assertEqual(ys[0], 0.0)
        self.assertEqual(end[0], 1500 + 5000)

    def test_label_uses_only_future_after_latency(self):
        """Modifier les trades ≤ ts_k + latence (sauf le trade d'entrée lui-même, postérieur)
        ne change pas la cible ; modifier un trade postérieur peut la changer."""
        t = make_trades(days=1, rate_per_s=2.0, null=True, seed=3)
        bars_ts = np.arange(t["ts"][0] + 3_600_000, t["ts"][0] + 7_200_000, 5000, dtype=np.int64)
        spec = LabelSpec(12, 25, 60)
        lat = 250
        y0 = make_labels(t, bars_ts, spec, lat)
        k = 100
        cut = bars_ts[k] + lat
        t2 = {kk: v.copy() for kk, v in t.items()}
        past = t2["ts"] <= cut
        t2["px"][past] = t2["px"][past] * (1 + 0.05 * np.random.default_rng(0).standard_normal(past.sum()))
        y1 = make_labels(t2, bars_ts, spec, lat)
        for a, b in zip(y0, y1):
            self.assertTrue(np.array_equal(a[k:], b[k:], equal_nan=True))
        # le futur influence bien la cible : un saut de +1 % juste après l'entrée -> long = 1
        t3 = {kk: v.copy() for kk, v in t.items()}
        i = int(np.searchsorted(t3["ts"], cut, side="right"))
        t3["px"][i + 1:i + 50] = t3["px"][i] * 1.01
        self.assertEqual(make_labels(t3, bars_ts, spec, lat)[0][k], 1.0)

    def test_label_nan_when_horizon_not_covered(self):
        t = tiny_trades()
        yl, ys, _ = make_labels(t, np.array([8000]), LabelSpec(12, 25, 60), 250)
        self.assertTrue(np.isnan(yl[0]) and np.isnan(ys[0]))


class TestFeaturesCausal(unittest.TestCase):
    def test_features_use_only_past(self):
        """Perturber les trades après ts_k ne change aucune feature des barres ≤ k."""
        F = int(CFG["bars"]["freq_seconds"])
        t = make_trades(days=1, rate_per_s=2.0, null=False, seed=1)
        df0 = features.add_features(features.make_bars(t, F), F, funding_interval_h=1.0)
        cols = feature_columns(df0)
        k = len(df0) // 2
        cut = int(df0["ts"].iloc[k])
        t2 = {kk: v.copy() for kk, v in t.items()}
        fut = t2["ts"] >= cut          # la barre k couvre [ts_k − F, ts_k)
        t2["px"][fut] *= 1.03
        t2["bm"][fut] = ~t2["bm"][fut]
        t2["qty"][fut] *= 5
        df1 = features.add_features(features.make_bars(t2, F), F, funding_interval_h=1.0)
        a = feature_matrix(df0.iloc[:k + 1], cols)
        b = feature_matrix(df1.iloc[:k + 1], cols)
        np.testing.assert_allclose(a, b, rtol=1e-6, atol=1e-6, equal_nan=True)

    def test_no_label_column_in_features(self):
        F = int(CFG["bars"]["freq_seconds"])
        df = features.add_features(features.make_bars(make_trades(days=1, rate_per_s=1.0, seed=2), F), F,
                                    funding_interval_h=1.0)
        df["y_long"] = 0.0
        self.assertNotIn("y_long", feature_columns(df))
        for c in feature_columns(df):
            self.assertFalse(c.startswith(("y_", "lbl_")))


class TestDecisionAndPersistence(unittest.TestCase):
    def test_expected_gain_and_threshold(self):
        ep = ExecParams.from_cfg(CFG)
        spec = LabelSpec(35, 25, 300)
        cw, cl = cost_bps(ep, 100_000.0)
        self.assertGreater(cl, 0)
        p_be = (spec.stop_bps + cl) / (spec.tp_bps - cw + spec.stop_bps + cl)   # seuil de rentabilité
        self.assertLess(expected_gain_bps(np.array([p_be - 0.01]), spec, cw, cl)[0], 0)
        self.assertGreater(expected_gain_bps(np.array([p_be + 0.01]), spec, cw, cl)[0], 0)

    def test_fit_decide_save_load(self):
        F = int(CFG["bars"]["freq_seconds"])
        t = make_trades(days=2, rate_per_s=2.0, null=False, seed=4)
        df = features.add_features(features.make_bars(t, F), F, funding_interval_h=1.0)
        spec = LabelSpec(12, 25, 60)
        yl, ys, _ = make_labels(t, df["ts"].to_numpy(), spec, 250, rows=np.arange(720, len(df), 6))
        cols = feature_columns(df)
        X = feature_matrix(df, cols)
        m = TradeModel(spec, cols, threshold=0.6, seed=7,
                       hyperparams={"max_iter": 20, "max_leaf_nodes": 7, "min_samples_leaf": 50})
        m.fit(X, yl, ys)
        ep = ExecParams.from_cfg(CFG, spec.exits_override())
        e = m.decide(df, ep)
        self.assertEqual(e.dtype, np.int8)
        self.assertEqual(len(e), len(df))
        self.assertTrue(set(np.unique(e)).issubset({-1, 0, 1}))
        pl, ps = m.predict_proba(df)
        thr_hi = m.decide(df, ep, threshold=1.01)
        self.assertEqual(np.abs(thr_hi).sum(), 0)          # sous le seuil : ne trade pas
        with tempfile.TemporaryDirectory() as d:
            meta = m.save(Path(d) / "m.pkl", "abc123", "2026-01-01", "2026-01-02")
            self.assertTrue((Path(d) / "m.json").exists())
            self.assertEqual(meta["features"], cols)
            m2 = TradeModel.load(Path(d) / "m.pkl", expected_config_hash="abc123")
            pl2, ps2 = m2.predict_proba(df)
            np.testing.assert_allclose(pl, pl2)
            np.testing.assert_array_equal(e, m2.decide(df, ep))
            with self.assertRaises(ValueError):
                TradeModel.load(Path(d) / "m.pkl", expected_config_hash="autre")

    def test_deterministic(self):
        rng = np.random.default_rng(0)
        X = rng.standard_normal((3000, 5)).astype(np.float32)
        y = (X[:, 0] + rng.standard_normal(3000) > 0).astype(np.float32)
        spec = LabelSpec(12, 25, 60)
        p = [TradeModel(spec, features.FEATURE_COLS_ML[:5]).fit(X, y, 1 - y)._proba("long", X) for _ in range(2)]
        np.testing.assert_array_equal(p[0], p[1])


if __name__ == "__main__":
    unittest.main()
