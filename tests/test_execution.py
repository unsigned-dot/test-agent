"""Moteur d'exécution : cas construits à la main, résultats exacts (section 5)."""
import math
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scalp.config import deep_update, load_config  # noqa: E402
from scalp.execution import ExecParams, funding_rate_total, simulate, taker_price  # noqa: E402

MON = 1_780_272_000_000          # lundi 01/06/2026 00:00 UTC
T0 = MON + 10 * 3_600_000        # signal lundi 10:00 UTC
H8 = 8 * 3_600_000


def tape(rows):
    """rows : [(décalage_ms depuis T0, prix, acheteur_agresseur)]."""
    rows = sorted(rows)
    n = len(rows)
    aid = np.arange(n, dtype=np.int64)
    return {"ts": np.array([T0 + r[0] for r in rows], np.int64),
            "px": np.array([r[1] for r in rows], float),
            "qty": np.full(n, 0.01, np.float32),
            "bm": np.array([not r[2] for r in rows], bool),
            "aid": aid, "fid": aid, "lid": aid}


def cfg_for(venue="kraken", slip=0.0, impact=0.0, **extra):
    # cas construits à la main pour une latence d'entrée de 250 ms (grâce 0 + envoi 250) ;
    # la latence par défaut (grâce 300 + envoi 300) est testée dans TestEntryLatency
    over = {"venue": {"execution": venue}, "bars": {"close_grace_ms": 0}, "costs": {"latency_ms": 250},
            "venues": {venue: {"extra_slippage_bps": slip, "impact_bps_per_btc": impact}}}
    return deep_update(load_config(), deep_update(over, extra))


def run(t, cfg, side=1, signal_ts=T0, funding=None, **kw):
    ep = ExecParams.from_cfg(cfg)
    bars_ts = np.array([signal_ts], np.int64)
    return simulate(t, bars_ts, np.array([side], np.int8), cfg, ep, funding=funding, **kw), ep


# bande de base : signal à T0, latence 250 ms -> arrivée T0+250
BASE = [(-1000, 100000.0, True),        # dernier trade connu au signal (sert au sizing)
        (100, 100005.0, True),          # avant l'arrivée : interdit
        (250, 100010.0, True),          # exactement à l'arrivée : pas « après »
        (300, 100000.0, False),         # 1er trade après arrivée, vendeur-agresseur -> bid 100000, ask 100001
        (400, 100400.0, True),          # au-delà du TP mais avant que le TP ne soit posé (fill + 250 ms)
        (1000, 100352.0, True),         # TP touché, PAS traversé
        (2000, 100353.0, True)]         # TP traversé -> fill au prix limite


class TestEngine(unittest.TestCase):
    def test_long_take_profit_exact(self):
        cfg = cfg_for()
        tr, ep = run(tape(BASE), cfg)
        self.assertEqual(len(tr), 1)
        r = tr.iloc[0]
        # sizing : 1000*0,5 % / (25 bps + coûts 2*5 bps + 1 tick/100000) = 1424,5 -> 0,0142 BTC
        self.assertAlmostEqual(r.qty, 0.0142, places=12)
        self.assertEqual(r.entry_ts, T0 + 300)
        self.assertEqual(r.entry_px, 100001.0)                      # ask inféré, tick Kraken 1 USD
        self.assertEqual(r.exit_reason, "take_profit")
        self.assertEqual(r.exit_ts, T0 + 2000)
        self.assertEqual(r.exit_px, 100352.0)                       # ceil(100001*1,0035) au tick
        gross = 351.0 * 0.0142
        fees = (5.0 * 100001.0 + 2.0 * 100352.0) * 0.0142 * 1e-4
        fund = 100001.0 * 0.0142 * 0.0001 / H8 * 1700               # continu, taux défaut proratisé
        self.assertAlmostEqual(r.gross_pnl, gross, places=10)
        self.assertAlmostEqual(r.fees, fees, places=10)
        self.assertAlmostEqual(r.funding, fund, places=12)
        self.assertAlmostEqual(r.net_pnl, gross - fees - fund, places=10)
        self.assertEqual(r.taker_legs, 1)
        self.assertEqual(r.venue, "kraken")

    def test_limit_touched_not_crossed_is_not_filled(self):
        rows = [x for x in BASE if x[0] != 2000] + [(300_600, 100100.0, True)]
        tr, _ = run(tape(rows), cfg_for())
        r = tr.iloc[0]
        self.assertEqual(r.exit_reason, "time_stop")
        # time stop : envoi à fill + 300 s, exécution au 1er trade après + latence
        self.assertEqual(r.exit_ts, T0 + 300_600)
        self.assertEqual(r.exit_px, 100099.0)                       # vente au bid inféré (ask - 1 tick)

    def test_latency_shifts_fill(self):
        rows = BASE[:4] + [(700, 100050.0, True), (400_000, 100000.0, False)]
        cfg = cfg_for(costs={"latency_ms": 500})
        tr, _ = run(tape(rows), cfg)
        self.assertEqual(tr.iloc[0].entry_ts, T0 + 700)             # le trade à +300 est trop tôt
        self.assertEqual(tr.iloc[0].entry_px, 100050.0)             # acheteur-agresseur : ask = prix

    def test_no_trade_after_data_gap(self):
        rows = [(-1000, 100000.0, True), (6000, 100000.0, True)]    # 1er trade > 5 s après arrivée
        tr, _ = run(tape(rows), cfg_for())
        self.assertEqual(len(tr), 0)
        self.assertEqual(tr.attrs["skipped"], {"data_gap_no_fill": 1})

    def test_no_trade_without_data_after_signal(self):
        tr, _ = run(tape([(-1000, 100000.0, True), (200, 100000.0, True)]), cfg_for())
        self.assertEqual(len(tr), 0)

    def test_short_stop_exact(self):
        rows = [(-1000, 100000.0, False),
                (300, 100000.0, False),         # vente au bid 100000
                (5000, 100250.0, True),         # stop 100250 atteint (>=)
                (5100, 100260.0, True),         # avant latence du stop
                (5300, 100270.0, True)]         # exécution du stop : achat à l'ask 100270
        tr, _ = run(tape(rows), cfg_for(), side=-1)
        r = tr.iloc[0]
        self.assertEqual((r.entry_px, r.exit_reason, r.exit_ts, r.exit_px),
                         (100000.0, "stop", T0 + 5300, 100270.0))
        q = r.qty
        self.assertAlmostEqual(r.gross_pnl, -270.0 * q, places=10)
        self.assertAlmostEqual(r.fees, 5.0 * (100000.0 + 100270.0) * q * 1e-4, places=10)
        # short : reçoit le funding quand le taux est positif
        self.assertAlmostEqual(r.funding, -100000.0 * q * 0.0001 / H8 * 5000, places=12)
        self.assertEqual(r.taker_legs, 2)

    def test_slippage_impact_rounded_to_tick(self):
        cfg = cfg_for(slip=1.0, impact=0.5)
        tr, _ = run(tape(BASE), cfg)
        q = tr.iloc[0].qty
        expect = math.ceil(100001.0 * (1 + (1.0 + 0.5 * q) * 1e-4))
        self.assertEqual(tr.iloc[0].entry_px, float(expect))

    def test_kraken_vs_binance_profiles(self):
        rows = [(-1000, 100000.3, True), (300, 100000.3, False), (400_000, 100000.3, False)]
        trk, epk = run(tape(rows), cfg_for("kraken"))
        trb, epb = run(tape(rows), cfg_for("binance"))
        self.assertEqual((epk.tick, epk.lot, epk.funding_interval_h), (1.0, 0.0001, 1.0))
        self.assertEqual((epb.tick, epb.lot, epb.funding_interval_h), (0.1, 0.001, 8.0))
        self.assertEqual(trk.iloc[0].entry_px, 100001.0)            # bid 100000 + 1 tick de 1 USD
        self.assertAlmostEqual(trb.iloc[0].entry_px, 100000.4)      # bid 100000,3 + 0,1
        self.assertAlmostEqual(trb.iloc[0].qty, 0.014)              # lot Binance 0,001
        self.assertLess(trk.iloc[0].net_pnl, trb.iloc[0].net_pnl)   # Kraken plus pénalisant

    def test_discrete_funding_binance_with_file(self):
        t_sig = MON + H8 - 60_000                                   # 07:59 UTC, funding à 08:00
        rows = [(-1000, 100000.0, True), (300, 100000.0, True), (400_000, 100000.0, False)]
        t = tape(rows)
        t["ts"] = t["ts"] - T0 + t_sig
        funding = pd.DataFrame({"ts": [MON, MON + H8, MON + 2 * H8], "rate": [0.0002, 0.0003, 0.0004],
                                "interval_h": [8.0, 8.0, 8.0]})
        tr, ep = run(t, cfg_for("binance"), signal_ts=t_sig, funding=funding)
        r = tr.iloc[0]
        self.assertEqual(r.entry_px, 100000.0)
        self.assertAlmostEqual(r.funding, 100000.0 * r.qty * 0.0003, places=10)   # 1 instant traversé
        self.assertEqual(funding_rate_total(MON + 1, MON + H8 - 1, ep, funding), 0.0)
        self.assertAlmostEqual(funding_rate_total(MON - 1, MON + H8, ep, funding), 0.0005)

    def test_continuous_funding_from_file(self):
        ep = ExecParams.from_cfg(cfg_for("kraken"))
        hr = 3_600_000
        f = pd.DataFrame({"ts": [MON + hr, MON + 2 * hr], "rate": [0.00001, 0.00003], "interval_h": [1.0, 1.0]})
        # 30 min dans la 1re heure + 30 min dans la 2e
        tot = funding_rate_total(MON + hr // 2, MON + 3 * hr // 2, ep, f)
        self.assertAlmostEqual(tot, 0.5 * 0.00001 + 0.5 * 0.00003, places=15)
        # sans fichier : 0,01 %/8 h proratisé
        self.assertAlmostEqual(funding_rate_total(MON, MON + hr, ep, None), 0.0001 / 8, places=15)

    def test_binance_calc_time_is_period_end_even_with_ms_offset(self):
        """Binance : calc_time = instant de règlement (fin de période), parfois quelques ms après
        l'heure pile ; le taux réglé à 08:00:00.003 est bien celui payé à l'instant 08:00."""
        ep = ExecParams.from_cfg(cfg_for("binance"))
        f = pd.DataFrame({"ts": [MON + 3, MON + H8 + 3, MON + 2 * H8 + 3], "rate": [0.0002, 0.0003, 0.0004],
                          "interval_h": [8.0, 8.0, 8.0]})
        self.assertAlmostEqual(funding_rate_total(MON + H8 - 60_000, MON + H8 + 60_000, ep, f), 0.0003)

    def test_kraken_historical_funding_timestamp_is_period_start(self):
        """Kraken historicalfundingrates : timestamp = DÉBUT de la période horaire. kraken_funding
        ramène à la convention interne (ts = fin) : le taux de 00:00 couvre [00:00, 01:00)."""
        from scalp.data.loaders import kraken_funding
        f = kraken_funding([{"timestamp": "2026-06-01T01:00:00.000Z", "fundingRate": 9.9, "relativeFundingRate": 0.00003},
                            {"timestamp": "2026-06-01T00:00:00.000Z", "fundingRate": 9.9, "relativeFundingRate": 0.00001}])
        hr = 3_600_000
        self.assertEqual(f["ts"].tolist(), [MON + hr, MON + 2 * hr])
        self.assertEqual(f["rate"].tolist(), [0.00001, 0.00003])
        self.assertEqual(f["interval_h"].tolist(), [1.0, 1.0])
        ep = ExecParams.from_cfg(cfg_for("kraken"))
        # 00:30 -> 01:30 : 30 min au taux de la période 00h, 30 min au taux de la période 01h
        tot = funding_rate_total(MON + hr // 2, MON + 3 * hr // 2, ep, f)
        self.assertAlmostEqual(tot, 0.5 * 0.00001 + 0.5 * 0.00003, places=15)
        # 00:10 -> 00:50 : entièrement dans la période commençant à 00h
        self.assertAlmostEqual(funding_rate_total(MON + hr // 6, MON + 5 * hr // 6, ep, f),
                               (4 / 6) * 0.00001, places=15)

    def test_maker_entry_needs_trade_through(self):
        cfg = cfg_for(maker_entry={"enabled": True})
        rows = [(-2000, 99990.0, False),        # bid connu au signal = 99990
                (-1000, 100000.0, True),
                (300, 99990.0, False),          # touché : pas de fill
                (800, 99989.0, False),          # traversé : fill au prix limite 99990
                (400_000, 99990.0, False)]
        tr, _ = run(tape(rows), cfg)
        r = tr.iloc[0]
        self.assertEqual((r.entry_px, r.entry_ts, r.entry_kind), (99990.0, T0 + 800, "maker"))
        self.assertAlmostEqual(r.fees, (2.0 * 99990.0 + 5.0 * r.exit_px) * r.qty * 1e-4, places=10)
        tr2, _ = run(tape([x for x in rows if x[0] != 800]), cfg)
        self.assertEqual(len(tr2), 0)
        self.assertEqual(tr2.attrs["skipped"], {"maker_not_filled": 1})

    def test_risk_veto_weekend_and_size_mult(self):
        sat = MON + 5 * 86_400_000 + 10 * 3_600_000
        t = tape(BASE)
        t["ts"] = t["ts"] - T0 + sat
        tr, _ = run(t, cfg_for(), signal_ts=sat)
        self.assertEqual(len(tr), 0)
        self.assertEqual(tr.attrs["skipped"], {"weekend": 1})
        tr, _ = run(tape(BASE), cfg_for(), size_mult=np.array([0.5]))
        self.assertAlmostEqual(tr.iloc[0].qty, 0.0071, places=12)

    def test_one_position_at_a_time(self):
        t = tape(BASE + [(400_000, 100000.0, False)])
        bars_ts = np.array([T0, T0 + 500, T0 + 3000], np.int64)
        cfg = cfg_for()
        tr = simulate(t, bars_ts, np.array([1, 1, -1], np.int8), cfg, ExecParams.from_cfg(cfg))
        self.assertEqual(len(tr), 1)                                # signaux pendant la position ignorés,
        self.assertEqual(tr.attrs["skipped"].get("position_open"), 1)   # le 3e bloqué par le cooldown 10 s
        self.assertEqual(tr.attrs["skipped"].get("cooldown"), 1)

    def test_shared_risk_manager_across_calls(self):
        from scalp.risk import RiskManager
        cfg = cfg_for(risk={"max_trades_per_day": 1})
        rm = RiskManager(cfg)
        tr1, _ = run(tape(BASE), cfg, risk=rm)
        self.assertEqual(len(tr1), 1)
        self.assertAlmostEqual(rm.equity, 1000.0 + tr1.iloc[0].net_pnl)
        t2 = tape(BASE)
        t2["ts"] = t2["ts"] + 3_600_000                             # 2e morceau, même jour UTC
        tr2, _ = run(t2, cfg, signal_ts=T0 + 3_600_000, risk=rm)
        self.assertEqual(len(tr2), 0)                               # plafond du jour conservé
        self.assertEqual(tr2.attrs["skipped"], {"max_trades_per_day": 1})

    def test_taker_price_rounding(self):
        self.assertEqual(taker_price(100000.4, True, 1, 1.0, 0, 0), 100001.0)
        self.assertEqual(taker_price(100000.4, False, -1, 1.0, 0, 0), 100000.0)
        self.assertEqual(taker_price(100000.0, True, -1, 1.0, 0, 0), 99999.0)



class TestEntryLatency(unittest.TestCase):
    """Latence d'entrée du backtest = grâce de clôture de barre du robot + latence d'envoi (600 ms)."""

    def test_default_entry_latency_includes_bar_grace(self):
        from scalp.config import entry_latency_ms
        from bot.config import load_bot_config
        cfg = load_config()
        bot = load_bot_config()
        self.assertEqual(entry_latency_ms(cfg), 600)
        ep = ExecParams.from_cfg(cfg)
        self.assertEqual((ep.entry_latency_ms, ep.latency_ms), (600, 300))
        # même grâce que celle utilisée par le robot (source unique : config/default.toml)
        self.assertEqual(int(bot.research["bars"]["close_grace_ms"]), int(cfg["bars"]["close_grace_ms"]))
        cfg0 = deep_update(cfg, {"venues": {"kraken": {"extra_slippage_bps": 0.0, "impact_bps_per_btc": 0.0}}})
        rows = [(-1000, 100000.0, True), (300, 100000.0, False), (600, 100010.0, True),
                (650, 100020.0, True), (400_000, 100000.0, False)]
        tr, _ = run(tape(rows), cfg0)
        self.assertEqual(tr.iloc[0].entry_ts, T0 + 650)             # 1er trade strictement après T0 + 600
        self.assertEqual(tr.iloc[0].entry_px, 100020.0)

    def test_sensitivity_grid_and_purge(self):
        from scalp import validation as V
        from scalp.config import entry_latency_ms, with_entry_latency
        from scalp.model import LabelSpec
        cfg = load_config()
        self.assertEqual(V.LATENCY_GRID_MS, (250, 600, 1000))
        got = [(entry_latency_ms(c), c["bars"]["close_grace_ms"], c["costs"]["latency_ms"])
               for c in (with_entry_latency(cfg, x) for x in V.LATENCY_GRID_MS)]
        self.assertEqual(got, [(250, 250, 0), (600, 300, 300), (1000, 300, 700)])
        self.assertEqual(V.purge_ms_for([LabelSpec(12, 25, 60)], cfg), 60_000 + 600 + 5000)


if __name__ == "__main__":
    unittest.main()
