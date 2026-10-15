"""Mandat de risque (section 2) : chaque règle, veto, export/import d'état."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scalp.config import deep_update, load_config  # noqa: E402
from scalp.risk import DAY_MS, RiskManager, weekday  # noqa: E402

MON = 1_780_272_000_000          # lundi 01/06/2026 00:00 UTC
H = 3_600_000
STOP = 0.0025                    # 25 bps
PX = 100_000.0


def cfg_with(**risk):
    return deep_update(load_config(), {"risk": risk}) if risk else load_config()


def open_close(rm, t, pnl, hold_ms=60_000):
    q, why = rm.request_entry(t, PX, STOP)
    assert why == "", why
    rm.on_trade_closed(t + hold_ms, pnl)
    return q


class TestCalendar(unittest.TestCase):
    def test_weekday(self):
        self.assertEqual(weekday(MON), 0)
        self.assertEqual(weekday(MON + 5 * DAY_MS), 5)

    def test_no_trading_weekend(self):
        rm = RiskManager(load_config())
        self.assertTrue(rm.can_trade(MON + 10 * H))
        self.assertEqual(rm.block_reason(MON + 5 * DAY_MS + 10 * H), "weekend")    # samedi
        self.assertEqual(rm.block_reason(MON + 6 * DAY_MS + 23 * H), "weekend")    # dimanche
        self.assertTrue(rm.can_trade(MON + 7 * DAY_MS))                            # lundi suivant
        q, why = rm.request_entry(MON + 5 * DAY_MS, PX, STOP)
        self.assertEqual((q, why), (0.0, "weekend"))


class TestSizing(unittest.TestCase):
    def test_fixed_risk(self):
        rm = RiskManager(load_config())
        # 1000 * 0.5 % / (25 bps + 10 bps de coûts) = 1428.57 < plafond 1500
        n = rm.target_notional(MON, 0.0025, 0.0010)
        self.assertAlmostEqual(n, 5.0 / 0.0035, places=9)
        q, _ = rm.request_entry(MON + H, PX, 0.0025, 0.0010, lot=0.0001)
        self.assertAlmostEqual(q, 0.0142, places=12)        # arrondi au lot, vers le bas

    def test_capped_by_reserve_and_leverage(self):
        rm = RiskManager(load_config())
        # stop très serré -> notionnel brut 10 000 ; réserve 50 % au levier 3 -> 1500
        self.assertAlmostEqual(rm.target_notional(MON, 0.0005), 1500.0)
        rm0 = RiskManager(cfg_with(min_free_capital_fraction=0.0))
        self.assertAlmostEqual(rm0.target_notional(MON, 0.0005), 3000.0)    # levier seul
        self.assertLessEqual(rm.max_notional(), rm.max_leverage * rm.equity)

    def test_signal_mult_can_only_reduce(self):
        rm = RiskManager(load_config())
        base = rm.target_notional(MON, 0.0025)
        self.assertAlmostEqual(rm.target_notional(MON, 0.0025, mult=0.5), base / 2)
        self.assertAlmostEqual(rm.target_notional(MON, 0.0025, mult=5.0), base)

    def test_stop_vs_liquidation(self):
        cfg = load_config()
        # Kraken : marge de maintenance = moitié de la marge initiale du contrat (10 % au levier 10x)
        self.assertEqual(cfg["risk"]["maintenance_margin_rate"], 0.05)
        self.assertLessEqual(cfg["risk"]["max_leverage"], 3.0)
        rm = RiskManager(cfg)                 # levier 3, mmr 5 % : liq ≈ 28.3 %, plafond stop ≈ 14.2 %
        self.assertAlmostEqual(rm.liquidation_distance_frac(), 1 / 3 - 0.05)
        self.assertTrue(rm.stop_is_safe(cfg["exits"]["stop_bps"] * 1e-4))   # stop 25 bp : très loin
        self.assertTrue(rm.stop_is_safe(0.14))
        self.assertFalse(rm.stop_is_safe(0.15))
        self.assertEqual(rm.request_entry(MON + H, PX, 0.20)[1], "stop_too_close_to_liquidation")
        rm10 = RiskManager(cfg_with(max_leverage=10.0))    # liq = 10 % - 5 % = 5 %, plafond 2,5 %
        self.assertFalse(rm10.stop_is_safe(0.03))
        self.assertTrue(rm10.stop_is_safe(0.02))

    def test_cost_threshold(self):
        rm = RiskManager(load_config())
        self.assertEqual(rm.request_entry(MON + H, PX, STOP, cost_frac=0.001,
                                          expected_gain_frac=0.0008)[1], "expected_gain_below_costs")
        self.assertEqual(rm.request_entry(MON + H, PX, STOP, cost_frac=0.001,
                                          expected_gain_frac=0.002)[1], "")

    def test_size_below_lot(self):
        rm = RiskManager(cfg_with(initial_equity=1.0))
        self.assertEqual(rm.request_entry(MON + H, PX, STOP, lot=0.001)[1], "size_below_lot")


class TestLimits(unittest.TestCase):
    def test_single_position_and_cooldown(self):
        rm = RiskManager(load_config())       # cooldown 10 s
        t = MON + H
        rm.request_entry(t, PX, STOP)
        self.assertEqual(rm.request_entry(t + 1000, PX, STOP)[1], "position_open")
        rm.on_trade_closed(t + 60_000, 0.0)
        self.assertEqual(rm.block_reason(t + 60_000 + 9_999), "cooldown")
        self.assertTrue(rm.can_trade(t + 60_000 + 10_000))

    def test_cancel_entry_frees_slot(self):
        rm = RiskManager(load_config())
        rm.request_entry(MON + H, PX, STOP)
        rm.cancel_entry(MON + H)
        self.assertEqual(rm.day_trades, 0)
        self.assertTrue(rm.can_trade(MON + H + 1))

    def test_max_trades_per_day(self):
        rm = RiskManager(cfg_with(max_trades_per_day=3, cooldown_seconds=0))
        for i in range(3):
            open_close(rm, MON + H + i * 120_000, 0.0)
        self.assertEqual(rm.block_reason(MON + 2 * H), "max_trades_per_day")
        self.assertTrue(rm.can_trade(MON + DAY_MS))            # remis à zéro le lendemain UTC

    def test_daily_limit_then_half_size_then_full(self):
        rm = RiskManager(load_config())
        open_close(rm, MON + H, -29.0)
        self.assertTrue(rm.can_trade(MON + 2 * H))               # -2,9 % : encore autorisé
        open_close(rm, MON + 2 * H, -1.0)                        # -3,0 % : limite atteinte
        self.assertEqual(rm.block_reason(MON + 3 * H), "loss_limit_halt")
        self.assertEqual(rm.block_reason(MON + DAY_MS - 1), "loss_limit_halt")
        tue = MON + DAY_MS
        self.assertTrue(rm.can_trade(tue))                       # reprise auto le lendemain UTC
        self.assertEqual(rm.size_mult(tue), 0.5)                 # ... à 50 % de taille
        # brut 970*0,5 %/25 bps = 1940, plafonné par la réserve à 970*0,5*3 = 1455, puis x0,5
        self.assertAlmostEqual(rm.target_notional(tue, STOP), 0.5 * 1455.0)
        self.assertEqual(rm.size_mult(tue + DAY_MS - 1), 0.5)
        self.assertEqual(rm.size_mult(tue + DAY_MS), 1.0)        # 1 jour complet sans limite -> 100 %
        self.assertEqual([e["event"] for e in rm.events], ["daily_loss_limit"])

    def test_new_limit_during_recovery_extends_it(self):
        rm = RiskManager(load_config())
        open_close(rm, MON + H, -30.0)
        tue = MON + DAY_MS
        open_close(rm, tue + H, -29.5)        # -3,04 % jour, -5,95 % semaine : limite jour seule
        self.assertEqual(rm.block_reason(tue + 2 * H), "loss_limit_halt")
        self.assertEqual(rm.size_mult(tue + DAY_MS), 0.5)        # mercredi : de nouveau 50 %
        self.assertEqual(rm.size_mult(tue + 2 * DAY_MS), 1.0)    # jeudi : 100 %

    def test_weekly_limit(self):
        rm = RiskManager(load_config())
        open_close(rm, MON + H, -25.0)                            # 975
        open_close(rm, MON + DAY_MS + H, -25.0)                   # 950 (jour : -2,56 %)
        self.assertTrue(rm.can_trade(MON + 2 * DAY_MS + H))
        open_close(rm, MON + 2 * DAY_MS + H, -15.0)               # 935 : semaine -6,5 %, jour -1,6 %
        self.assertEqual(rm.events[-1]["event"], "weekly_loss_limit")
        self.assertEqual(rm.block_reason(MON + 3 * DAY_MS + H), "loss_limit_halt")   # jeudi
        self.assertEqual(rm.block_reason(MON + 4 * DAY_MS + H), "loss_limit_halt")   # vendredi
        nmon = MON + 7 * DAY_MS
        self.assertTrue(rm.can_trade(nmon))                       # reprise lundi suivant
        self.assertEqual(rm.size_mult(nmon + 4 * DAY_MS), 0.5)    # toute la semaine à 50 %
        self.assertEqual(rm.size_mult(nmon + 7 * DAY_MS), 1.0)    # semaine complète sans limite

    def test_kill_switch_usdt(self):
        rm = RiskManager(load_config())
        open_close(rm, MON + H, +1000.0)                          # sommet 2000 : 15 % = 300 > 150
        self.assertFalse(rm.check_limits(MON + 2 * H, unrealized_pnl=-149.0))
        self.assertFalse(rm.killed)
        self.assertTrue(rm.check_limits(MON + 2 * H, unrealized_pnl=-150.0))  # 150 USDT atteint d'abord
        self.assertTrue(rm.killed)
        for dt in (H, 8 * DAY_MS, 400 * DAY_MS):                  # définitif
            self.assertEqual(rm.block_reason(MON + 2 * H + dt), "kill_switch")
        with self.assertRaises(PermissionError):
            rm.manual_reset_kill("oui")
        rm.manual_reset_kill("HUGO_AUTORISE_REDEMARRAGE")
        self.assertFalse(rm.killed)

    def test_kill_switch_fraction_first(self):
        rm = RiskManager(cfg_with(initial_equity=500.0, max_daily_loss_fraction=1.0,
                                  max_weekly_loss_fraction=1.0))
        open_close(rm, MON + H, -74.0)                            # 14,8 % du sommet
        self.assertFalse(rm.killed)
        open_close(rm, MON + 2 * H, -1.0)                         # 75 = 15 % de 500 < 150 USDT
        self.assertTrue(rm.killed)
        self.assertEqual(rm.request_entry(MON + 3 * H, PX, STOP), (0.0, "kill_switch"))


class TestState(unittest.TestCase):
    def test_export_import_roundtrip(self):
        cfg = load_config()
        rm = RiskManager(cfg)
        open_close(rm, MON + H, -30.0)                            # limite jour -> reprise 50 %
        open_close(rm, MON + DAY_MS + H, +2.0)
        rm.request_entry(MON + DAY_MS + 2 * H, PX, STOP)          # position ouverte au redémarrage
        st = json.loads(json.dumps(rm.to_state()))                # JSON-sérialisable
        rm2 = RiskManager.from_state(cfg, st)
        self.assertEqual(rm2.to_state(), rm.to_state())
        for t in (MON + DAY_MS + 3 * H, MON + 2 * DAY_MS + H):
            self.assertEqual(rm2.block_reason(t), rm.block_reason(t))
            self.assertEqual(rm2.size_mult(t), rm.size_mult(t))
        self.assertEqual(rm2.block_reason(MON + DAY_MS + 3 * H), "position_open")

    def test_kill_survives_restart(self):
        cfg = load_config()
        rm = RiskManager(cfg)
        rm.check_limits(MON + H, unrealized_pnl=-200.0)
        rm2 = RiskManager.from_state(cfg, json.loads(json.dumps(rm.to_state())))
        self.assertTrue(rm2.killed)
        self.assertEqual(rm2.block_reason(MON + 30 * DAY_MS), "kill_switch")

    def test_bad_state_rejected(self):
        cfg = load_config()
        with self.assertRaises(ValueError):
            RiskManager.from_state(cfg, {"version": 1})
        with self.assertRaises(ValueError):
            RiskManager.from_state(cfg, {**RiskManager(cfg).to_state(), "version": 99})


if __name__ == "__main__":
    unittest.main()
