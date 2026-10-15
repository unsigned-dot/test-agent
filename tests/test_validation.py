"""Tests du protocole : split chronologique, purge, verrou du test final, essais DSR."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scalp import validation as V  # noqa: E402

DAY = V.DAY_MS


class TestSplit(unittest.TestCase):
    def test_chronological_no_overlap(self):
        days = list(range(20000, 20100))
        s = V.chronological_split(days[::-1], 0.6, 0.2)       # ordre d'entrée indifférent
        self.assertEqual((len(s["dev"]), len(s["val"]), len(s["test"])), (60, 20, 20))
        self.assertLess(max(s["dev"]), min(s["val"]))
        self.assertLess(max(s["val"]), min(s["test"]))
        self.assertEqual(set(s["dev"]) | set(s["val"]) | set(s["test"]), set(days))
        self.assertEqual(len(set(s["dev"]) & set(s["val"])) + len(set(s["val"]) & set(s["test"])), 0)

    def test_whole_days(self):
        # barre finissant exactement à minuit = veille
        self.assertEqual(V.bar_day(np.array([DAY * 10]))[0], 9)
        self.assertEqual(V.bar_day(np.array([DAY * 10 + 5000]))[0], 10)

    def test_too_few_days(self):
        with self.assertRaises(ValueError):
            V.chronological_split([1, 2], 0.6, 0.2)


class TestPurge(unittest.TestCase):
    def test_purge_removes_overlapping_labels(self):
        test_start, test_end = 10 * DAY, 12 * DAY
        H = 300_000
        ts = np.arange(9 * DAY, 13 * DAY, 60_000, dtype=np.int64)
        end = ts + H
        keep = V.purge_mask(ts, end, test_start, test_end, purge_ms=H)
        # aucun échantillon conservé dont la cible déborde dans le test
        self.assertTrue(np.all(end[keep & (ts < test_start)] < test_start))
        self.assertTrue(np.all(ts[keep & (ts < test_start)] < test_start - H))
        # aucun échantillon du test lui-même
        self.assertFalse(np.any(keep & (ts >= test_start) & (ts < test_end)))
        # embargo après le test
        self.assertFalse(np.any(keep & (ts >= test_end) & (ts < test_end + H)))
        # les échantillons lointains sont conservés
        self.assertTrue(keep[ts < test_start - 2 * H].all())
        self.assertTrue(keep[ts >= test_end + H].all())

    def test_purge_ge_horizon(self):
        from scalp.model import LabelSpec
        cfg = {"bars": {"close_grace_ms": 300}, "costs": {"latency_ms": 300, "max_fill_wait_ms": 5000}}
        self.assertGreaterEqual(V.purge_ms_for([LabelSpec(12, 25, 60), LabelSpec(35, 25, 300)], cfg), 300_000)

    def test_walkforward_folds_ordered(self):
        f = V.walkforward_folds(list(range(30)), 10, 5)
        self.assertEqual(len(f), 4)
        for x in f:
            self.assertLess(max(x["train"]), min(x["test"]))
        tests = [d for x in f for d in x["test"]]
        self.assertEqual(len(tests), len(set(tests)))


class TestLock(unittest.TestCase):
    def test_lock_refuses_second_config(self):
        with tempfile.TemporaryDirectory() as d:
            p1 = {"threshold": 0.6, "spec": "a"}
            V.acquire_final_test_lock(d, "hashA", p1)
            V.acquire_final_test_lock(d, "hashA", p1)                     # même config : autorisé
            with self.assertRaises(V.FinalTestLockError):
                V.acquire_final_test_lock(d, "hashB", p1)                 # autre config
            with self.assertRaises(V.FinalTestLockError):
                V.acquire_final_test_lock(d, "hashA", {"threshold": 0.7, "spec": "a"})   # autres paramètres
            lock = V.acquire_final_test_lock(d, "hashB", p1, force=True)   # forcé : journalisé
            self.assertEqual(len(lock["evaluations"]), 3)
            self.assertTrue(lock["evaluations"][-1]["forced"])
            saved = json.loads((Path(d) / "final_test_lock.json").read_text())
            self.assertEqual(saved["config_hash"], "hashA")               # le verrou d'origine reste
            self.assertTrue(V.lock_matches(d, "hashA", p1))
            self.assertFalse(V.lock_matches(d, "hashB", p1))


class TestTrials(unittest.TestCase):
    def test_trial_count(self):
        with tempfile.TemporaryDirectory() as d:
            tl = V.TrialLog(Path(d) / "t.jsonl")
            tl.add("a", "h1", "r", 0.1, 10, 1.0, "model")
            tl.add("b", "h1", "r", -0.1, 10, 1.0, "model")
            tl.add("a", "h1", "r2", 0.1, 10, 1.0, "model")                # même essai rejoué
            tl.add("a", "h2", "r3", float("nan"), 0, float("nan"), "model")
            self.assertEqual(tl.n_trials(), 3)
            # essai rejoué compté une seule fois, y compris dans la variance des Sharpe
            self.assertAlmostEqual(tl.sr_variance(), np.var([0.1, -0.1], ddof=1))


class TestEndToEndSynthetic(unittest.TestCase):
    """Petit run complet sur données synthétiques nulles : le test final ne doit rien valider."""

    def test_null_pipeline(self):
        from scalp.config import deep_update, load_config
        cfg = load_config(Path(__file__).resolve().parents[1] / "config" / "default.toml")
        cfg = deep_update(cfg, {"walkforward": {"train_days": 3, "test_days": 1},
                                "ml": {"min_trades_val": 5, "n_random_benchmarks": 2,
                                       "threshold_grid": [0.5, 0.7]},
                                "bootstrap": {"n_resamples": 100}})
        with tempfile.TemporaryDirectory() as d:
            run = V.Run(d, "t", cfg)
            sp = {"kind": "synthetic", "null": True, "days": 8, "rate": 1.0, "seed": 5}
            run.save("source.json", sp)
            src = V.make_source(sp, cfg)
            V.run_research(run, src)
            V.run_walkforward(run, src)
            ft = V.run_final_test(run, src)
            self.assertFalse(ft["criteria"]["verdict"].startswith("GO"))
            # 2e évaluation avec une autre config : refusée
            run2 = V.Run(d, "t", deep_update(cfg, {"ml": {"threshold_grid": [0.9]}}))
            with self.assertRaises(V.FinalTestLockError):
                V.run_final_test(run2, src)


if __name__ == "__main__":
    unittest.main()


class TestResearchRequiresKraken(unittest.TestCase):
    def test_non_kraken_venue_is_refused(self):
        import tempfile
        from scalp import validation as V
        from scalp.config import load_config, deep_update
        cfg = deep_update(load_config("config/default.toml"), {"venue": {"execution": "binance"}})
        with tempfile.TemporaryDirectory() as d:
            run = V.Run(d, "x", cfg)
            with self.assertRaises(ValueError):
                V.run_research(run, None)
