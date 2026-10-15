"""Test anti-faux-positif (section 7) et signaux de référence.

Sur une marche aléatoire synthétique sans avantage, les benchmarks (aléatoires et naïfs)
doivent avoir une espérance nette négative : ils perdent leurs coûts.
"""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scalp import signals  # noqa: E402
from scalp.config import deep_update, load_config  # noqa: E402
from scalp.data.synthetic import make_trades  # noqa: E402
from scalp.execution import ExecParams, simulate  # noqa: E402
from scalp.features import add_features, make_bars  # noqa: E402

F = 5


class TestNull(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 5 jours ouvrés (lundi -> vendredi), tick Kraken, limites jour/semaine neutralisées
        # pour mesurer l'espérance par trade et non l'effet du mandat
        cls.t = make_trades(days=5, rate_per_s=2.0, tick=1.0, null=True, seed=11)
        cls.bars = add_features(make_bars(cls.t, F), F, funding_interval_h=1)
        cls.cfg = deep_update(load_config(), {"risk": {"max_daily_loss_fraction": 1.0,
                                                       "max_weekly_loss_fraction": 1.0,
                                                       "kill_drawdown_usdt": 1e9,
                                                       "kill_drawdown_fraction": 1.0}})

    def _run(self, strat, p, venue):
        cfg = deep_update(self.cfg, {"venue": {"execution": venue}})
        e = strat.signal(self.bars, p, F)
        return simulate(self.t, self.bars["ts"].to_numpy(), e, cfg, ExecParams.from_cfg(cfg))

    def test_benchmarks_lose_costs(self):
        for venue in ("kraken", "binance"):
            for strat in signals.BENCHMARKS:
                for p in strat.param_sets():
                    tr = self._run(strat, p, venue)
                    with self.subTest(venue=venue, strat=strat.name, p=p):
                        self.assertGreaterEqual(len(tr), 30)
                        self.assertLess(tr["net_bps"].mean(), 0.0)
                        # le coût moyen (brut - net) est au moins les frais d'une jambe taker + maker
                        self.assertGreater((tr["gross_bps"] - tr["net_bps"]).mean(), 6.9)

    def test_random_pooled_net_close_to_minus_costs(self):
        tr = [self._run(signals.by_name("B_random_entries"), {"prob": 0.003, "seed": s}, "kraken")
              for s in range(4)]
        net = np.concatenate([x["net_bps"].to_numpy() for x in tr])
        gross = np.concatenate([x["gross_bps"].to_numpy() for x in tr])
        se = net.std(ddof=1) / np.sqrt(net.size)
        self.assertLess(net.mean() + 2 * se, 0.0)           # perte significative
        self.assertLess(abs(gross.mean()), 3 * se + 3.0)    # brut ≈ 0 (± spread inféré)


class TestSignals(unittest.TestCase):
    def test_warmup_and_dtype(self):
        t = make_trades(days=1, rate_per_s=0.5, seed=1)
        bars = add_features(make_bars(t, F), F, funding_interval_h=1)
        s = signals.by_name("B_random_entries")
        e = s.signal(bars, {"prob": 0.5, "seed": 0}, F)
        self.assertEqual(e.dtype, np.int8)
        self.assertEqual(signals.warmup_bars(bars, F), 720)
        self.assertEqual(signals.warmup_bars(bars, 10), 360)
        self.assertEqual(signals.warmup_bars(bars), 720)        # déduit de ts
        self.assertFalse(e[:720].any())
        self.assertTrue(e[720:].any())
        self.assertTrue(set(np.unique(e)) <= {-1, 0, 1})

    def test_reference_rules_only(self):
        names = [s.name for s in signals.REFERENCES]
        self.assertEqual(names, ["R1_exhaustion_reversal", "R2_flow_continuation"])
        n_trials = sum(len(s.param_sets()) for s in signals.REFERENCES + signals.BENCHMARKS)
        self.assertLessEqual(n_trials, 20)                      # petites grilles

    def test_exhaustion_needs_oi(self):
        t = make_trades(days=1, rate_per_s=0.5, seed=1)
        bars = add_features(make_bars(t, F), F, funding_interval_h=1)
        self.assertFalse(signals.by_name("R1_exhaustion_reversal").available(bars))
        bars["oi_chg_5m"] = -20.0
        bars.loc[2000, "z_12"], bars.loc[2000, "volz_12"] = -5.0, 3.0
        e = signals.by_name("R1_exhaustion_reversal").signal(
            bars, {"w": 12, "z": 4.0, "vz": 2.0, "oi_drop_bps": 10.0}, F)
        self.assertEqual(e[2000], 1)                            # chute extrême épuisée -> long


if __name__ == "__main__":
    unittest.main()
