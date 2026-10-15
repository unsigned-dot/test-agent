"""Mandat de risque (section 2 du cahier des charges) — droit de veto sur chaque entrée.

Problème résolu : garantir que ni la stratégie, ni le modèle, ni une commande ne peuvent
ouvrir une position hors mandat. Logique PURE (bibliothèque standard uniquement, temps en
ms UTC) : réutilisable telle quelle par le backtest et par le robot live.

Règles :
- une seule position à la fois ; cooldown après chaque sortie ; pas d'entrée sam./dim. UTC ;
- plafond de trades par jour UTC (compté à l'entrée) ;
- perte jour <= -3 % (equity de début de jour) : arrêt jusqu'au lendemain 00:00 UTC ;
  perte semaine <= -6 % (equity du lundi 00:00 UTC) : arrêt jusqu'au lundi suivant ;
  reprise automatique à 50 % de taille, retour à 100 % après une période complète
  (1 jour, resp. 1 semaine) suivant la reprise sans nouvelle limite atteinte ;
- coupe-circuit DÉFINITIF : perte depuis le sommet >= 150 USDT ou >= 15 % du sommet
  (le premier atteint) ; levée uniquement par `manual_reset_kill` (décision d'Hugo) ;
- sizing par risque fixe : notionnel = equity * risk_fraction * mult / (stop + coûts estimés),
  plafonné par le levier max ET par la réserve de capital libre (marge isolée) ;
- refus si le stop n'est pas à moins de 50 % de la distance de liquidation ;
- refus si le gain attendu (s'il est fourni) ne couvre pas les coûts estimés.

Le seul chemin d'entrée est `request_entry` (qui appelle `can_trade`). L'état complet est
exportable / importable (`to_state` / `from_state`, dict JSON-sérialisable).
"""
from __future__ import annotations

import math

DAY_MS = 86_400_000
WEEK_MS = 7 * DAY_MS
STATE_VERSION = 1


def day_index(t_ms: int) -> int:
    return int(t_ms) // DAY_MS


def weekday(t_ms: int) -> int:
    """0 = lundi ... 6 = dimanche (UTC). Le 01/01/1970 était un jeudi (3)."""
    return (day_index(t_ms) + 3) % 7


def week_start_ms(t_ms: int) -> int:
    """Lundi 00:00 UTC de la semaine contenant t_ms."""
    d = day_index(t_ms)
    return (d - (d + 3) % 7) * DAY_MS


class RiskManager:
    def __init__(self, cfg: dict):
        r = cfg["risk"] if "risk" in cfg else cfg
        self.initial_equity = float(r["initial_equity"])
        self.risk_fraction = float(r["risk_per_trade_fraction"])
        self.max_leverage = float(r["max_leverage"])
        self.min_free_fraction = float(r["min_free_capital_fraction"])
        self.mmr = float(r["maintenance_margin_rate"])
        self.stop_max_fraction_of_liq = float(r["stop_max_fraction_of_liq"])
        self.max_daily_loss = float(r["max_daily_loss_fraction"])
        self.max_weekly_loss = float(r["max_weekly_loss_fraction"])
        self.reduced_mult = float(r["reduced_size_mult"])
        self.kill_dd_usdt = float(r["kill_drawdown_usdt"])
        self.kill_dd_fraction = float(r["kill_drawdown_fraction"])
        self.max_trades = int(r["max_trades_per_day"])
        self.cooldown_ms = int(round(float(r["cooldown_seconds"]) * 1000))
        self.no_trade_weekdays = tuple(int(x) for x in r["no_trade_weekdays"])
        self._reset_state()

    # ------------------------------------------------------------------ état
    def _reset_state(self) -> None:
        self.equity = self.initial_equity
        self.peak_equity = self.initial_equity
        self.day = None                  # index du jour UTC courant
        self.day_start_equity = self.equity
        self.day_trades = 0
        self.week_start = None           # ms du lundi 00:00 UTC courant
        self.week_start_equity = self.equity
        self.halt_until_ms = 0           # aucune entrée avant cet instant
        self.recovery_until_ms = 0       # taille réduite jusqu'à cet instant
        self.last_exit_ms = None
        self.position_open = False
        self.killed = False
        self.kill_reason = ""
        self.events: list[dict] = []     # journal des limites atteintes

    _STATE_FIELDS = ("equity", "peak_equity", "day", "day_start_equity", "day_trades", "week_start",
                     "week_start_equity", "halt_until_ms", "recovery_until_ms", "last_exit_ms",
                     "position_open", "killed", "kill_reason", "events")

    def to_state(self) -> dict:
        """État complet, JSON-sérialisable (à persister après chaque événement)."""
        st = {k: getattr(self, k) for k in self._STATE_FIELDS}
        st["events"] = [dict(e) for e in self.events]
        st["version"] = STATE_VERSION
        return st

    @classmethod
    def from_state(cls, cfg: dict, state: dict) -> "RiskManager":
        """Reconstruit le gestionnaire : limites lues dans `cfg`, compteurs dans `state`."""
        rm = cls(cfg)
        if int(state.get("version", -1)) != STATE_VERSION:
            raise ValueError(f"version d'état de risque inattendue : {state.get('version')!r}")
        missing = [k for k in cls._STATE_FIELDS if k not in state]
        if missing:
            raise ValueError(f"état de risque incomplet : {missing}")
        for k in cls._STATE_FIELDS:
            setattr(rm, k, state[k])
        rm.equity, rm.peak_equity = float(rm.equity), float(rm.peak_equity)
        rm.day_start_equity, rm.week_start_equity = float(rm.day_start_equity), float(rm.week_start_equity)
        rm.events = [dict(e) for e in rm.events]
        return rm

    # -------------------------------------------------------------- horloge
    def _roll(self, t_ms: int) -> None:
        d = day_index(t_ms)
        if self.day is None or d != self.day:
            self.day, self.day_start_equity, self.day_trades = d, self.equity, 0
        w = week_start_ms(t_ms)
        if self.week_start is None or w != self.week_start:
            self.week_start, self.week_start_equity = w, self.equity

    # ------------------------------------------------------------ décisions
    def size_mult(self, t_ms: int) -> float:
        """Multiplicateur de taille imposé par le mandat (1.0 ou 0.5 en reprise)."""
        return self.reduced_mult if t_ms < self.recovery_until_ms else 1.0

    def block_reason(self, t_ms: int) -> str:
        """'' si une entrée est autorisée à t_ms, sinon la raison du refus."""
        self._roll(t_ms)
        if self.killed:
            return "kill_switch"
        if self.equity <= 0:
            return "no_equity"
        if self.position_open:
            return "position_open"
        if weekday(t_ms) in self.no_trade_weekdays:
            return "weekend"
        if t_ms < self.halt_until_ms:
            return "loss_limit_halt"
        if self.day_trades >= self.max_trades:
            return "max_trades_per_day"
        if self.last_exit_ms is not None and t_ms < self.last_exit_ms + self.cooldown_ms:
            return "cooldown"
        return ""

    def can_trade(self, t_ms: int) -> bool:
        return self.block_reason(t_ms) == ""

    def liquidation_distance_frac(self) -> float:
        """Distance entrée -> liquidation en marge isolée au levier max (approximation)."""
        return max(0.0, 1.0 / self.max_leverage - self.mmr)

    def stop_is_safe(self, stop_frac: float) -> bool:
        return 0.0 < stop_frac < self.stop_max_fraction_of_liq * self.liquidation_distance_frac()

    def max_notional(self) -> float:
        """Plafond de notionnel : levier max ET réserve de capital libre (marge isolée)."""
        by_leverage = self.max_leverage * self.equity
        by_reserve = (1.0 - self.min_free_fraction) * self.equity * self.max_leverage
        return max(0.0, min(by_leverage, by_reserve))

    def target_notional(self, t_ms: int, stop_frac: float, cost_frac: float = 0.0,
                        mult: float = 1.0) -> float:
        """Notionnel au risque fixe. `mult` (signal) est borné à [0, 1] : il ne peut que réduire."""
        dist = stop_frac + max(0.0, cost_frac)
        if dist <= 0:
            return 0.0
        mult = min(1.0, max(0.0, float(mult)))
        raw = self.equity * self.risk_fraction / dist
        return min(raw, self.max_notional()) * self.size_mult(t_ms) * mult

    def request_entry(self, t_ms: int, price: float, stop_frac: float, cost_frac: float = 0.0,
                      lot: float = 0.0001, mult: float = 1.0,
                      expected_gain_frac: float | None = None) -> tuple[float, str]:
        """SEUL chemin d'entrée. Renvoie (quantité, '') si accepté, (0.0, raison) sinon.

        Si accepté, la position est réputée ouverte et comptée dans le plafond du jour :
        appeler ensuite `on_trade_closed`, ou `cancel_entry` si l'ordre n'a pas été rempli.
        """
        why = self.block_reason(t_ms)
        if why:
            return 0.0, why
        if not self.stop_is_safe(stop_frac):
            return 0.0, "stop_too_close_to_liquidation"
        if expected_gain_frac is not None and expected_gain_frac <= cost_frac:
            return 0.0, "expected_gain_below_costs"
        if not (price > 0 and lot > 0):
            return 0.0, "bad_price_or_lot"
        notional = self.target_notional(t_ms, stop_frac, cost_frac, mult)
        qty = math.floor(notional / price / lot + 1e-9) * lot
        qty = round(qty, 12)
        if qty < lot:
            return 0.0, "size_below_lot"
        self.position_open = True
        self.day_trades += 1
        return qty, ""

    def cancel_entry(self, t_ms: int) -> None:
        """Ordre d'entrée non rempli : libère la position, ne compte pas de trade."""
        if self.position_open:
            self.position_open = False
            self.day_trades = max(0, self.day_trades - 1)

    def on_trade_closed(self, t_ms: int, net_pnl: float) -> None:
        self._roll(t_ms)
        self.position_open = False
        self.last_exit_ms = int(t_ms)
        self.equity += float(net_pnl)
        self.peak_equity = max(self.peak_equity, self.equity)
        self.check_limits(t_ms)

    def check_limits(self, t_ms: int, unrealized_pnl: float = 0.0) -> bool:
        """Évalue limites et coupe-circuit avec l'equity (+ latent éventuel, pour le live).

        Renvoie True si une limite est atteinte (le robot live doit alors fermer la position).
        """
        self._roll(t_ms)
        eq = self.equity + float(unrealized_pnl)
        dd = self.peak_equity - eq
        hit = False
        if not self.killed and (dd >= self.kill_dd_usdt - 1e-9
                                or dd >= self.kill_dd_fraction * self.peak_equity - 1e-9):
            self.killed = True
            self.kill_reason = f"drawdown {dd:.2f} depuis le sommet {self.peak_equity:.2f}"
            self._log(t_ms, "kill_switch", eq)
            hit = True
        if eq - self.week_start_equity <= -self.max_weekly_loss * self.week_start_equity + 1e-12:
            until = self.week_start + WEEK_MS
            if until > self.halt_until_ms:
                self._halt(t_ms, until, WEEK_MS, "weekly_loss_limit", eq)
            hit = True
        if eq - self.day_start_equity <= -self.max_daily_loss * self.day_start_equity + 1e-12:
            until = (self.day + 1) * DAY_MS
            if until > self.halt_until_ms:
                self._halt(t_ms, until, DAY_MS, "daily_loss_limit", eq)
            hit = True
        return hit or self.killed

    def _halt(self, t_ms: int, until: int, period: int, kind: str, eq: float) -> None:
        self.halt_until_ms = max(self.halt_until_ms, until)
        # reprise à 50 % dès la fin de l'arrêt, puis une période complète sans limite
        self.recovery_until_ms = max(self.recovery_until_ms, self.halt_until_ms + period)
        self._log(t_ms, kind, eq)

    def _log(self, t_ms: int, kind: str, eq: float) -> None:
        self.events.append({"t_ms": int(t_ms), "event": kind, "equity": round(eq, 8)})

    def manual_reset_kill(self, confirm: str) -> None:
        """Levée du coupe-circuit : décision explicite d'Hugo uniquement."""
        if confirm != "HUGO_AUTORISE_REDEMARRAGE":
            raise PermissionError("levée du coupe-circuit refusée : confirmation manuelle requise")
        self.killed, self.kill_reason = False, ""
        self.peak_equity = self.equity
