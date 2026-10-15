"""Tests de l'audit indépendant : loaders sur faux fichiers Binance, IC / DSR avec peu de jours,
critères go/no-go, registre du test final, test réellement fermé, cas limites d'exécution."""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scalp import metrics  # noqa: E402
from scalp import validation as V  # noqa: E402
from scalp.config import deep_update, load_config  # noqa: E402
from scalp.data import loaders  # noqa: E402
from scalp.execution import ExecParams, simulate  # noqa: E402
from scalp.features import add_features, make_bars  # noqa: E402

DAY = 86_400_000
MON = 1_780_272_000_000                   # lundi 01/06/2026 00:00 UTC

AGG_HEADER = "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker\n"
METRICS_HEADER = ("create_time,symbol,sum_open_interest,sum_open_interest_value,"
                  "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
                  "count_long_short_ratio,sum_taker_long_short_vol_ratio\n")
FUNDING_HEADER = "calc_time,funding_interval_hours,last_funding_rate\n"


def write_zip(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(path.with_suffix(".csv").name, text)
    return path


def agg_rows(t0_ms: int, n: int, unit: int = 1, aid0: int = 1000, eight_cols: bool = False) -> str:
    out = []
    for i in range(n):
        ts = (t0_ms + i * 100) * unit
        row = f"{aid0 + i},{100000.0 + i * 0.1:.1f},0.00{1 + i % 5},{3 * (aid0 + i)},{3 * (aid0 + i) + 1},{ts},{'true' if i % 2 else 'false'}"
        out.append(row + (",True" if eight_cols else ""))
    return "\n".join(out) + "\n"


class TestLoadersRealFormat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def check_trades(self, t, n, t0_ms):
        self.assertEqual(t["ts"].size, n)
        np.testing.assert_array_equal(t["ts"], t0_ms + np.arange(n) * 100)
        np.testing.assert_allclose(t["px"], 100000.0 + np.arange(n) * 0.1)
        np.testing.assert_array_equal(t["bm"], np.arange(n) % 2 == 1)
        np.testing.assert_array_equal(t["aid"], 1000 + np.arange(n))
        self.assertAlmostEqual(float(t["qty"][0]), 0.001, places=6)

    def test_futures_with_header(self):
        p = write_zip(self.d / "a.zip", AGG_HEADER + agg_rows(MON, 50))
        self.check_trades(loaders.parse_aggtrades(p), 50, MON)

    def test_futures_without_header(self):
        p = write_zip(self.d / "b.zip", agg_rows(MON, 50))
        self.check_trades(loaders.parse_aggtrades(p), 50, MON)

    def test_spot_8_columns_microseconds_no_header(self):
        # spot : pas d'en-tête, 8e colonne is_best_match, horodatage en µs depuis 2025.
        # Avant correction : la 1re colonne devenait l'index et TOUTES les colonnes étaient décalées.
        p = write_zip(self.d / "c.zip", agg_rows(MON, 50, unit=1000, eight_cols=True))
        self.check_trades(loaders.parse_aggtrades(p), 50, MON)

    def test_spot_8_columns_with_header_ms(self):
        p = write_zip(self.d / "d.zip", AGG_HEADER.strip() + ",is_best_match\n" + agg_rows(MON, 20, eight_cols=True))
        self.check_trades(loaders.parse_aggtrades(p), 20, MON)

    def test_to_ms_units(self):
        np.testing.assert_array_equal(loaders._to_ms(np.array([MON, MON * 1000, MON * 1_000_000])), [MON] * 3)

    def _cfg(self):
        return deep_update(load_config(), {"data": {"raw_dir": str(self.d / "raw"), "cache_dir": str(self.d / "cache"),
                                                    "start": "2026-06-01", "end": "2026-06-02"}})

    def test_metrics_and_funding_files(self):
        cfg = self._cfg()
        base = self.d / "raw" / "um"
        rows = []
        for i in range(30):
            t = pd.Timestamp(MON, unit="ms", tz="UTC") + pd.Timedelta(minutes=5 * i)
            rows.append(f"{t.strftime('%Y-%m-%d %H:%M:%S')},BTCUSDT,{80000 + i},{8e9 + i},1.1,1.2,1.3,{0.9 + i / 100}")
        write_zip(base / "metrics" / "BTCUSDT-metrics-2026-06-01.zip", METRICS_HEADER + "\n".join(rows) + "\n")
        write_zip(base / "metrics" / "BTCUSDT-metrics-2026-06-02.zip", "\n".join(rows[:2]).replace("2026-06-01", "2026-06-02") + "\n")
        write_zip(base / "fundingRate" / "BTCUSDT-fundingRate-2026-06.zip",
                  FUNDING_HEADER + f"{MON},8,0.0001\n{MON + 8 * 3600_000},8,-0.0002\n")
        m = loaders.load_metrics(cfg)
        self.assertEqual(len(m), 32)                      # fichier sans en-tête aussi lu
        self.assertEqual(int(m["ts"].iloc[0]), MON)
        self.assertEqual(int(m["ts"].iloc[1] - m["ts"].iloc[0]), 300_000)
        for c in ("sum_open_interest", "sum_open_interest_value", "count_toptrader_long_short_ratio",
                  "sum_toptrader_long_short_ratio", "count_long_short_ratio", "sum_taker_long_short_vol_ratio"):
            self.assertTrue(pd.api.types.is_numeric_dtype(m[c]), c)
        f = loaders.load_funding(cfg)
        self.assertEqual(list(f.columns), ["ts", "rate", "interval_h"])
        self.assertEqual(f["rate"].tolist(), [0.0001, -0.0002])
        self.assertEqual(f["interval_h"].tolist(), [8.0, 8.0])
        # sans en-tête
        write_zip(base / "fundingRate" / "BTCUSDT-fundingRate-2026-06.zip", f"{MON},8,0.0001\n")
        self.assertEqual(loaders.load_funding(cfg)["rate"].tolist(), [0.0001])
        # jointure causale : la ligne create_time = T n'est visible qu'à T + 360 s
        tt = {"ts": MON + np.arange(0, 3 * 3600_000, 500, dtype=np.int64)}
        n = tt["ts"].size
        tt.update(px=np.full(n, 100000.0), qty=np.full(n, 0.01, np.float32), bm=np.zeros(n, bool))
        df = add_features(make_bars(tt, 5), 5, metrics=m, funding=f, funding_interval_h=8.0)
        T = int(m["ts"].iloc[10])
        v_before = df.loc[df["ts"] == T + 355_000, "sum_taker_long_short_vol_ratio"].iloc[0]
        v_at = df.loc[df["ts"] == T + 360_000, "sum_taker_long_short_vol_ratio"].iloc[0]
        self.assertAlmostEqual(v_before, m["sum_taker_long_short_vol_ratio"].iloc[9])
        self.assertAlmostEqual(v_at, m["sum_taker_long_short_vol_ratio"].iloc[10])

    def test_compact_compressed_cache(self):
        cfg = self._cfg()
        raw = self.d / "raw" / "um" / "aggTrades"
        write_zip(raw / "BTCUSDT-aggTrades-2026-06-01.zip", AGG_HEADER + agg_rows(MON, 200))
        write_zip(raw / "BTCUSDT-aggTrades-2026-06-02.zip", AGG_HEADER + agg_rows(MON + DAY, 200, aid0=1200))
        t1 = loaders.load_trades(cfg)
        cache = self.d / "cache" / "um" / "aggTrades" / "2026-06-01.npz"
        self.assertTrue(cache.exists())
        with np.load(cache) as z:
            self.assertEqual(sorted(z.files), ["bm", "px", "qty", "ts"])        # pas d'identifiants
        t2 = loaders.load_trades(cfg)                                          # relu depuis le cache
        for k in loaders.CACHE_KEYS:
            np.testing.assert_array_equal(t1[k], t2[k])
        self.assertEqual(t2["ts"].size, 400)
        self.assertEqual(t2["qty"].dtype, np.float32)


    def test_validate_data_cli_on_raw_files(self):
        import json
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import run_pipeline
        cfg = deep_update(self._cfg(), {"data": {"end": "2026-06-03"}})
        raw = self.d / "raw" / "um" / "aggTrades"
        write_zip(raw / "BTCUSDT-aggTrades-2026-06-01.zip", AGG_HEADER + agg_rows(MON, 100))
        # jour 2 : identifiants qui sautent de 5 depuis la veille (trades manquants entre deux fichiers)
        write_zip(raw / "BTCUSDT-aggTrades-2026-06-02.zip", AGG_HEADER + agg_rows(MON + DAY, 100, aid0=1105))
        # 2026-06-03 absent
        cp = self.d / "cfg.toml"
        cp.write_text(run_pipeline.toml_dump(cfg), encoding="utf-8")
        run_pipeline.main(["--config", str(cp), "validate-data"])
        rep = json.loads((self.d / "cache" / "validation_report.json").read_text(encoding="utf-8"))
        m = rep["2026-06"]
        self.assertFalse(m["ok"])
        self.assertEqual(m["missing_days"], ["2026-06-03"])
        self.assertEqual(m["cross_day_agg_id_gaps"], 1)
        self.assertTrue(m["days"]["2026-06-01"]["ok"])
        self.assertTrue((self.d / "cache" / "um" / "aggTrades" / "2026-06-02.npz").exists())


class TestIntervalsNeedDays(unittest.TestCase):
    def tr(self, n_days, n=400, seed=0):
        rng = np.random.default_rng(seed)
        ts = MON + np.sort(rng.integers(0, n_days * DAY, n))
        x = rng.normal(5.0, 10.0, n)
        return pd.DataFrame({"entry_ts": ts, "exit_ts": ts + 60_000, "net_bps": x, "net_pnl": x * 0.1})

    def test_two_days_not_computable(self):
        lo, hi, p = metrics.trade_bootstrap_by_day(self.tr(2), 300, 1, block_days=3, min_days=20)
        self.assertTrue(np.isnan(lo) and np.isnan(hi) and np.isnan(p))

    def test_enough_days_computable_and_contains_mean(self):
        t = self.tr(40)
        lo, hi, p = metrics.trade_bootstrap_by_day(t, 500, 1, block_days=3, min_days=20)
        self.assertLess(lo, t["net_bps"].mean())
        self.assertGreater(hi, t["net_bps"].mean())

    def test_block_bootstrap_wider_with_day_correlation(self):
        # jours corrélés (régimes de 5 jours) : les blocs doivent donner un IC plus large que l'iid
        rng = np.random.default_rng(3)
        days = np.repeat(np.arange(60), 10)
        level = np.repeat(rng.normal(0, 8, 12), 50)
        t = pd.DataFrame({"entry_ts": MON + days * DAY + 1000, "net_bps": level + rng.normal(0, 3, 600)})
        lo1, hi1, _ = metrics.trade_bootstrap_by_day(t, 2000, 2, block_days=1)
        lo5, hi5, _ = metrics.trade_bootstrap_by_day(t, 2000, 2, block_days=5)
        self.assertGreater(hi5 - lo5, hi1 - lo1)

    def test_dsr_needs_days_and_trial_variance(self):
        rng = np.random.default_rng(0)
        daily = pd.Series(rng.normal(2.0, 5.0, 60))
        self.assertTrue(np.isnan(metrics.deflated_sharpe(daily[:10], 1000.0, 26, 0.01)))   # < 20 jours
        self.assertTrue(np.isnan(metrics.deflated_sharpe(daily, 1000.0, 26, float("nan"))))  # pas de déflation possible
        d1 = metrics.deflated_sharpe(daily, 1000.0, 2, 0.01)
        d2 = metrics.deflated_sharpe(daily, 1000.0, 100, 0.01)
        self.assertLess(d2, d1)                     # plus d'essais -> DSR plus bas


def passing_kdet(n_days=104):
    return {"summary": {"n_trades": 500, "exp_net_bps": 3.0, "net_pnl": 100.0},
            "ci95_exp_net_bps": [0.5, 5.5], "dsr": 0.97, "n_days": n_days,
            "by_regime": {"1_basse": {"exp_net_bps": 1.0}, "2_moyenne": {"exp_net_bps": 2.0},
                          "3_haute": {"exp_net_bps": -1.0}},
            "slippage_sensitivity": {"+1.0bps": 1.0}}


TABLE = [{"name": "B_naive_momentum", "group": "benchmark", "net_pnl": -50.0},
         {"name": "B_random_entries|x", "group": "benchmark", "net_pnl": -80.0}]
RND = {"exp_net_bps_p95": 1.0}


class TestCriteria(unittest.TestCase):
    def ok_ids(self, kdet, table=TABLE, rnd=RND, days=104):
        c = V.criteria(kdet, table, rnd, n_test_days=days, min_test_days=60)
        return {x["id"]: x["ok"] for x in c["items"]}, c

    def test_all_but_kraken_data(self):
        ok, c = self.ok_ids(passing_kdet())
        self.assertEqual(ok, {1: True, 2: True, 3: True, 4: True, 5: True, 6: True, 7: False, 8: True})
        self.assertTrue(c["verdict"].startswith("NO-GO"))        # critère 7 jamais rempli ici

    def test_each_criterion_can_fail(self):
        k = passing_kdet()
        cases = {
            1: deep_update(k, {"summary": {"n_trades": 299}}),
            2: deep_update(k, {"ci95_exp_net_bps": [-0.1, 5.0]}),
            3: deep_update(k, {"dsr": 0.94}),
            5: deep_update(k, {"by_regime": {"2_moyenne": {"exp_net_bps": -0.5}}}),
            6: deep_update(k, {"slippage_sensitivity": {"+1.0bps": -0.01}}),
        }
        for cid, kd in cases.items():
            self.assertFalse(self.ok_ids(kd)[0][cid], cid)
        self.assertFalse(self.ok_ids(k, rnd={"exp_net_bps_p95": 3.5})[0][4])          # aléatoire p95
        self.assertFalse(self.ok_ids(k, table=TABLE + [{"name": "B_naive_reversion", "group": "benchmark",
                                                        "net_pnl": 150.0}])[0][4])      # naïf meilleur
        self.assertFalse(self.ok_ids(k, days=59)[0][8])
        # IC non calculable (None après JSON) -> critère 2 non rempli, pas d'exception
        self.assertFalse(self.ok_ids(deep_update(k, {"ci95_exp_net_bps": [None, None], "dsr": None}))[0][2])

    def test_unknown_regime_not_counted(self):
        k = passing_kdet()
        k["by_regime"] = {"inconnu": {"exp_net_bps": 9.0}, "1_basse": {"exp_net_bps": 1.0}}
        self.assertFalse(self.ok_ids(k)[0][5])


class TestFinalTestRegistry(unittest.TestCase):
    def test_overlapping_period_other_config_refused(self):
        with tempfile.TemporaryDirectory() as d:
            reg = Path(d) / "final_test_registry.jsonl"
            reg.write_text('{"run_id": "r1", "fingerprint": "AAA", "test": ["2026-05-01", "2026-09-30"]}\n',
                           encoding="utf-8")
            with self.assertRaises(V.FinalTestLockError):
                V.check_final_test_registry(reg, ["2026-06-01", "2026-09-30"], "BBB")
            self.assertEqual(V.check_final_test_registry(reg, ["2026-06-01", "2026-09-30"], "AAA"), [])
            self.assertEqual(V.check_final_test_registry(reg, ["2026-10-01", "2026-12-31"], "BBB"), [])
            self.assertEqual(len(V.check_final_test_registry(reg, ["2026-06-01", "2026-09-30"], "BBB",
                                                             force=True)), 1)


class TestTestPeriodIsClosed(unittest.TestCase):
    """Tout ce qui est choisi avant le test (cible, seuil, régimes, essais) est INDÉPENDANT des
    données du test : on remplace le test par un autre marché, la sélection ne change pas."""

    def test_selection_ignores_test_data(self):
        cfg = deep_update(load_config(), {"ml": {"min_trades_val": 5, "threshold_grid": [0.5, 0.7]},
                                          "bootstrap": {"n_resamples": 50}})
        sp = {"kind": "synthetic", "null": False, "days": 8, "rate": 1.0, "seed": 11}
        sels = []
        for poison in (False, True):
            with tempfile.TemporaryDirectory() as d:
                run = V.Run(d, "t", cfg)
                run.save("source.json", sp)
                src = V.make_source(sp, cfg)
                split = V.chronological_split([x for x in src.days if (x + 3) % 7 not in (5, 6)], 0.6, 0.2)
                if poison:                       # marché du test remplacé (prix ×2, sens inversés)
                    m = src.trades["ts"] >= split["test"][0] * DAY
                    src.trades["px"][m] *= 2.0
                    src.trades["bm"][m] = ~src.trades["bm"][m]
                sel = V.run_research(run, src)
                vr = pd.read_csv(run.dir / "val_results.csv")
                sels.append((sel["spec"], sel["threshold"], sel["regime_quantiles_dev"],
                             sel["n_trials_total"], vr["net_pnl"].round(9).tolist()))
        self.assertEqual(sels[0], sels[1])
        # DSR : tous les essais sont comptés = cibles × seuils (modèle) + toutes les règles de référence
        from scalp import signals
        from scalp.model import label_specs_from_cfg, threshold_grid_from_cfg
        expected = (len(label_specs_from_cfg(cfg)) * len(threshold_grid_from_cfg(cfg))
                    + sum(len(s.param_sets()) for s in signals.REFERENCES))
        self.assertEqual(sels[0][3], expected)
        self.assertEqual(expected, 2 * 3 + 8 + 4)


class TestCriteriaUseKrakenProfile(unittest.TestCase):
    """La recherche n'accepte plus que le profil Kraken (refus testé dans test_validation) ;
    on vérifie ici que le verdict d'un run Kraken porte sur les 8 critères et la table Kraken."""
    def test_kraken_run_judged_with_kraken(self):
        cfg = deep_update(load_config(), {"venue": {"execution": "kraken"},
                                          "ml": {"min_trades_val": 5, "threshold_grid": [0.5],
                                                 "prob_threshold": 0.5, "n_random_benchmarks": 2},
                                          "walkforward": {"train_days": 3, "test_days": 1},
                                          "bootstrap": {"n_resamples": 50}})
        sp = {"kind": "synthetic", "null": True, "days": 8, "rate": 1.0, "seed": 5}
        with tempfile.TemporaryDirectory() as d:
            run = V.Run(d, "t", cfg)
            run.save("source.json", sp)
            src = V.make_source(sp, cfg)
            V.run_research(run, src)
            ft = V.run_final_test(run, src)
        kb = {r["name"]: r for r in ft["table_kraken_base"]}
        self.assertGreater(kb["B_naive_momentum"]["n_trades"], 0)
        ids = {c["id"] for c in ft["criteria"]["items"]}
        self.assertEqual(ids, set(range(1, 9)))
        self.assertFalse(ft["criteria"]["verdict"].startswith("GO"))


class TestExecutionEdges(unittest.TestCase):
    T0 = MON + 10 * 3600_000

    def tape(self, rows):
        rows = sorted(rows)
        n = len(rows)
        return {"ts": np.array([self.T0 + r[0] for r in rows], np.int64), "px": np.array([r[1] for r in rows], float),
                "qty": np.full(n, 0.01, np.float32), "bm": np.array([not r[2] for r in rows], bool)}

    def run_(self, rows, cfg=None):
        # cas construits pour une latence d'entrée de 250 ms (grâce 0 + envoi 250) et de sortie de 250 ms
        cfg = cfg or deep_update(load_config(), {"venues": {"kraken": {"extra_slippage_bps": 0.0,
                                                                         "impact_bps_per_btc": 0.0}},
                                                 "bars": {"close_grace_ms": 0}, "costs": {"latency_ms": 250}})
        ep = ExecParams.from_cfg(cfg)
        return simulate(self.tape(rows), np.array([self.T0], np.int64), np.array([1], np.int8), cfg, ep)

    def test_gap_through_stop_fills_at_gap_price(self):
        rows = [(-1000, 100000.0, True), (300, 100000.0, False),     # entrée à l'ask 100001
                (1000, 99500.0, False),                              # gap : stop (≈ 99751) traversé de loin
                (1100, 99400.0, False),                              # avant stop + latence : non exécutable
                (1300, 99300.0, False)]                              # exécution du stop ici
        r = self.run_(rows).iloc[0]
        self.assertEqual(r.exit_reason, "stop")
        self.assertEqual(r.exit_ts, self.T0 + 1300)
        self.assertEqual(r.exit_px, 99300.0)                         # pas au niveau du stop
        self.assertLess(r.net_bps, -25.0 - 10.0)

    def test_exit_after_data_gap_flagged(self):
        rows = [(-1000, 100000.0, True), (300, 100000.0, False), (3_600_000, 100100.0, False)]
        r = self.run_(rows).iloc[0]
        self.assertEqual(r.exit_reason, "data_gap_exit")
        self.assertEqual(r.exit_ts, self.T0 + 3_600_000)

    def test_end_of_data_flagged(self):
        rows = [(-1000, 100000.0, True), (300, 100000.0, False), (5000, 100010.0, True)]
        r = self.run_(rows).iloc[0]
        self.assertEqual(r.exit_reason, "end_of_data")

    def test_stop_before_tp_in_same_bar(self):
        # dans la même barre de 5 s : stop touché puis TP traversé -> le stop gagne (ordre des
        # trades) ; il s'exécute au 1er trade STRICTEMENT après déclenchement + 250 ms (pas à 1250)
        rows = [(-1000, 100000.0, True), (300, 100000.0, False), (1000, 99700.0, False),
                (1250, 99690.0, False), (1300, 99680.0, True), (1400, 100400.0, True)]
        r = self.run_(rows).iloc[0]
        self.assertEqual(r.exit_reason, "stop")
        self.assertEqual(r.exit_ts, self.T0 + 1300)
        self.assertEqual(r.exit_px, 99679.0)                         # vente au bid inféré (ask − 1 tick)


if __name__ == "__main__":
    unittest.main()
