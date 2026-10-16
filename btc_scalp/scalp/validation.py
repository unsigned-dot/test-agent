"""Protocole de validation (cahier des charges §7) et orchestration de l'étape 1.

- Découpage chronologique 60/20/20 par JOURS ENTIERS (dev / val / test), sans recouvrement.
- Walk-forward glissant sur dev+val avec PURGE : un échantillon d'entraînement est retiré si
  sa cible utilise de l'information postérieure à (début du test − purge), purge ≥ horizon + latence.
- Seuil (et grille courte de cibles) choisis UNIQUEMENT sur val.
- Test final verrouillé : runs/<id>/final_test_lock.json (empreinte config + paramètres) écrit
  AVANT la lecture du test ; une 2e évaluation avec une configuration différente est refusée
  sauf --force, journalisé.
- Chaque configuration évaluée sur val est un essai compté pour le Deflated Sharpe Ratio.
- Modèle, références et benchmarks : mêmes périodes, même moteur (execution.simulate).
- Vraies données traitées par blocs mensuels (mémoire bornée).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from .risk import RiskManager
from . import execution, features, metrics, signals
from .config import config_hash, deep_update, entry_latency_ms, with_entry_latency
from .model import (TradeModel, LabelSpec, auc, feature_columns, feature_matrix,
                    label_specs_from_cfg, make_labels, threshold_grid_from_cfg)

DAY_MS = 86_400_000
LATENCY_GRID_MS = (250, 600, 1000)     # sensibilité : latence d'ENTRÉE effective (grâce de barre incluse)
DOW_NAMES = np.array(["0_lundi", "1_mardi", "2_mercredi", "3_jeudi", "4_vendredi", "5_samedi", "6_dimanche"],
                     dtype=object)
# Comparaison des types d'ordres sur les MÊMES signaux (cahier des charges, section 1 « Type d'ordres ») :
# le stop reste toujours un ordre au marché déclenché chez la plateforme (sécurité, section 8).
ORDER_TYPE_VARIANTS = {
    "ordres_tout_marche": {"maker_entry": {"enabled": False}, "exits": {"take_profit_as_maker": False}},
    "ordres_mixte": {"maker_entry": {"enabled": False}, "exits": {"take_profit_as_maker": True}},
    "ordres_tout_limite": {"maker_entry": {"enabled": True}, "exits": {"take_profit_as_maker": True}},
}
log = logging.getLogger("scalp")


# ============================================================================ découpages
def bar_day(ts_ms) -> np.ndarray:
    """Jour UTC (entier) d'une barre horodatée à sa FIN : la barre finissant à minuit
    appartient à la veille."""
    return (np.asarray(ts_ms, dtype=np.int64) - 1) // DAY_MS


def day_str(d: int) -> str:
    return (dt.date(1970, 1, 1) + dt.timedelta(days=int(d))).isoformat()


def chronological_split(days, dev_fraction: float = 0.6, val_fraction: float = 0.2) -> dict[str, list[int]]:
    """Découpe une liste de jours en dev / val / test contigus, ordonnés, sans recouvrement."""
    days = sorted(set(int(d) for d in days))
    n = len(days)
    n_dev = int(round(n * dev_fraction))
    n_val = int(round(n * val_fraction))
    if n_dev < 1 or n_val < 1 or n - n_dev - n_val < 1:
        raise ValueError(f"pas assez de jours ({n}) pour un découpage {dev_fraction}/{val_fraction}")
    return {"dev": days[:n_dev], "val": days[n_dev:n_dev + n_val], "test": days[n_dev + n_val:]}


def purge_mask(sample_ts: np.ndarray, label_end_ts: np.ndarray, test_start_ms: int,
               test_end_ms: int | None = None, purge_ms: int = 0) -> np.ndarray:
    """True = échantillon d'entraînement CONSERVÉ.

    Retire tout échantillon dont la fenêtre [sample_ts, label_end_ts] chevauche
    [test_start − purge, test_end + purge) (purge avant, embargo après).
    """
    s = np.asarray(sample_ts, np.int64)
    e = np.asarray(label_end_ts, np.int64)
    before = (e < test_start_ms) & (s < test_start_ms - purge_ms)
    if test_end_ms is None:
        return before
    after = s >= test_end_ms + purge_ms
    return before | after


def walkforward_folds(days, train_days: int, test_days: int, step_days: int | None = None) -> list[dict]:
    """Fenêtres glissantes : entraînement sur `train_days` jours, test sur les `test_days` suivants."""
    days = sorted(int(d) for d in days)
    step = step_days or test_days
    folds, i = [], train_days
    while i + test_days <= len(days):
        folds.append({"train": days[i - train_days:i], "test": days[i:i + test_days]})
        i += step
    return folds


def purge_ms_for(specs: list[LabelSpec], cfg: dict) -> int:
    return int(max(s.horizon_s for s in specs) * 1000 + entry_latency_ms(cfg)
               + cfg["costs"].get("max_fill_wait_ms", 5000))


# ============================================================================ verrou
class FinalTestLockError(RuntimeError):
    pass


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def acquire_final_test_lock(run_dir: str | Path, cfg_hash: str, params: dict, force: bool = False,
                            logger: logging.Logger | None = None) -> dict:
    """Écrit (ou vérifie) runs/<id>/final_test_lock.json AVANT toute lecture du test.

    Même empreinte -> réévaluation identique autorisée (et comptée). Empreinte différente ->
    FinalTestLockError, sauf force=True (journalisé dans le verrou et dans le log).
    """
    lg = logger or log
    p = Path(run_dir) / "final_test_lock.json"
    params = json.loads(json.dumps(params, sort_keys=True, default=str))
    fp = config_hash({"config_hash": cfg_hash, "params": params})
    entry = {"utc": _now(), "fingerprint": fp, "config_hash": cfg_hash, "forced": False}
    if not p.exists():
        lock = {"config_hash": cfg_hash, "params": params, "fingerprint": fp, "created_utc": entry["utc"],
                "evaluations": [entry]}
        lg.info("verrou du test final créé (empreinte %s)", fp)
    else:
        lock = json.loads(p.read_text(encoding="utf-8"))
        if lock["fingerprint"] == fp:
            lock["evaluations"].append(entry)
            lg.info("réévaluation du test final avec la MÊME configuration (n=%d)", len(lock["evaluations"]))
        elif not force:
            raise FinalTestLockError(
                f"Test final déjà évalué avec une autre configuration (verrou {lock['fingerprint']}, "
                f"demandé {fp}). Refus. Utiliser --force uniquement en connaissance de cause : "
                "le résultat ne sera plus un test hors échantillon vierge.")
        else:
            entry.update(forced=True, params=params)
            lock["evaluations"].append(entry)
            lg.warning("--force : test final RÉÉVALUÉ avec une configuration DIFFÉRENTE (%s ≠ %s) — "
                       "journalisé dans le verrou", fp, lock["fingerprint"])
    p.write_text(json.dumps(lock, indent=2, ensure_ascii=False), encoding="utf-8")
    return lock


def lock_matches(run_dir: str | Path, cfg_hash: str, params: dict) -> bool:
    p = Path(run_dir) / "final_test_lock.json"
    if not p.exists():
        return False
    lock = json.loads(p.read_text(encoding="utf-8"))
    params = json.loads(json.dumps(params, sort_keys=True, default=str))
    return lock["fingerprint"] == config_hash({"config_hash": cfg_hash, "params": params})


# ============================================================================ essais (DSR)
class TrialLog:
    """Registre JSONL des essais évalués sur val (comptés pour le Deflated Sharpe Ratio)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def add(self, key: str, cfg_hash: str, run_id: str, sr_daily: float, n_trades: int,
            exp_net_bps: float, group: str):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"utc": _now(), "run_id": run_id, "config_hash": cfg_hash, "key": key, "group": group,
               "sr_daily": None if not np.isfinite(sr_daily) else float(sr_daily),
               "n_trades": int(n_trades),
               "exp_net_bps": None if not np.isfinite(exp_net_bps) else float(exp_net_bps)}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text(encoding="utf-8").splitlines() if x.strip()]

    def n_trials(self) -> int:
        return len({(r["config_hash"], r["key"]) for r in self.records()})

    def sr_variance(self) -> float:
        """Variance des Sharpe journaliers des essais DISTINCTS (un essai réévalué n'est compté
        qu'une fois, avec sa dernière valeur)."""
        last = {(r["config_hash"], r["key"]): r["sr_daily"] for r in self.records()}
        sr = [v for v in last.values() if v is not None]
        return float(np.var(sr, ddof=1)) if len(sr) > 1 else float("nan")


# ============================================================================ sources de données
@dataclass
class Chunk:
    days: list[int]
    trades: dict
    df: pd.DataFrame


class SyntheticSource:
    """Données SYNTHÉTIQUES (marche aléatoire nulle ou signal implanté) — un seul bloc."""
    kind = "synthetic"

    def __init__(self, null: bool, days: int, rate: float, seed: int, funding_interval_h: float):
        from .data.synthetic import make_trades
        # facteurs figés sur la plateforme d'exécution de la config du RUN (pas sur une variante de
        # sensibilité, ex. « binance ») : le modèle reçoit toujours les facteurs de son entraînement
        self.funding_interval_h = float(funding_interval_h)
        self.params = {"kind": "synthetic", "null": bool(null), "days": int(days), "rate": float(rate),
                       "seed": int(seed)}
        self.trades = make_trades(days=days, rate_per_s=rate, null=null, seed=seed)
        d = np.unique(self.trades["ts"] // DAY_MS)
        self.days = [int(x) for x in d]
        self.funding = None
        self._chunk = None

    def chunks(self) -> list[list[int]]:
        return [self.days]

    def load_chunk(self, chunk_days, cfg) -> Chunk:
        if self._chunk is None:
            F = int(cfg["bars"]["freq_seconds"])
            df = features.add_features(features.make_bars(self.trades, F), F,
                                       funding_interval_h=self.funding_interval_h)
            self._chunk = Chunk(self.days, self.trades, df)
        return self._chunk


class BinanceSource:
    """aggTrades Binance USDⓈ-M, traités MOIS PAR MOIS (+1 jour de chauffe avant, +1 jour de marge après)."""
    kind = "binance"

    def __init__(self, cfg: dict):
        from .data import loaders
        from .data.download import daterange
        self.cfg = cfg
        self.funding_interval_h = features.funding_interval_hours(cfg)   # figé (voir SyntheticSource)
        self.params = {"kind": "binance", "symbol": cfg["data"]["symbol"], "start": cfg["data"]["start"],
                       "end": cfg["data"]["end"]}
        d = cfg["data"]
        raw = Path(d["raw_dir"]) / d["market"] / "aggTrades"
        cache = Path(d["cache_dir"]) / d["market"] / "aggTrades"
        days = []
        for x in daterange(d["start"], d["end"]):
            iso = x.isoformat()
            if (cache / f"{iso}.npz").exists() or (raw / f"{d['symbol']}-aggTrades-{iso}.zip").exists():
                days.append((x - dt.date(1970, 1, 1)).days)
        if not days:
            raise FileNotFoundError(f"aucun fichier aggTrades dans {raw} : lancer d'abord 'download'")
        self.days = days
        self.metrics = loaders.load_metrics(cfg)
        self.funding = loaders.load_funding(cfg)
        self._key, self._chunk = None, None

    def chunks(self) -> list[list[int]]:
        out: dict[str, list[int]] = {}
        for d in self.days:
            out.setdefault(day_str(d)[:7], []).append(d)
        return list(out.values())

    def load_chunk(self, chunk_days, cfg) -> Chunk:
        key = (chunk_days[0], chunk_days[-1])
        if self._key == key:
            return self._chunk
        from .data import loaders
        self._chunk = None
        avail = set(self.days)
        want = [d for d in range(chunk_days[0] - 1, chunk_days[-1] + 2) if d in avail]
        tr = loaders.load_trades(self.cfg, days=[day_str(d) for d in want])
        tr = {k: tr[k] for k in ("ts", "px", "qty", "bm")}     # mémoire : ids inutiles ici
        F = int(cfg["bars"]["freq_seconds"])
        df = features.add_features(features.make_bars(tr, F), F, metrics=self.metrics, funding=self.funding,
                                   metrics_lag_s=int(cfg["data"].get("metrics_lag_seconds", 360)),
                                   funding_interval_h=self.funding_interval_h)
        keep = np.isin(bar_day(df["ts"].to_numpy()), chunk_days)
        df = df.loc[keep].reset_index(drop=True)
        self._key, self._chunk = key, Chunk(list(chunk_days), tr, df)
        return self._chunk


def make_source(params: dict, cfg: dict):
    if params["kind"] == "synthetic":
        return SyntheticSource(params["null"], params["days"], params["rate"], params["seed"],
                               features.funding_interval_hours(cfg))
    return BinanceSource(cfg)


# ============================================================================ jeu d'entraînement
def build_dataset(src, days: list[int], specs: list[LabelSpec], cfg: dict) -> dict:
    """Échantillons (une barre sur `stride`) des jours demandés, avec cibles pour chaque spec.

    Les jours sans trading (week-end) sont exclus : le modèle n'y trade jamais.
    """
    ml = cfg.get("ml", {})
    stride = int(ml.get("train_stride", 6))
    lat = entry_latency_ms(cfg)               # cible : même arrivée que l'entrée simulée et live
    wait = int(cfg["costs"].get("max_fill_wait_ms", 5000))
    F = int(cfg["bars"]["freq_seconds"]) * 1000
    no_wd = set(cfg["risk"].get("no_trade_weekdays", []))
    days_set = set(days)
    cols = None
    parts: dict[str, list] = {"X": [], "ts": [], "close": []}
    for s in specs:
        parts[f"yl_{s.key}"], parts[f"ys_{s.key}"], parts[f"end_{s.key}"] = [], [], []
    for ch_days in src.chunks():
        if not days_set.intersection(ch_days):
            continue
        ch = src.load_chunk(ch_days, cfg)
        df = ch.df
        if cols is None:
            cols = feature_columns(df)
        ts = df["ts"].to_numpy(np.int64)
        bd = bar_day(ts)
        wd = (bd + 3) % 7                         # 1970-01-01 = jeudi -> 0 = lundi
        sel = np.isin(bd, list(days_set)) & ~np.isin(wd, list(no_wd)) & ((ts // F) % stride == 0)
        sel &= np.arange(len(df)) >= 720          # chauffe des statistiques glissantes (1 h)
        rows = np.flatnonzero(sel)
        if rows.size == 0:
            continue
        parts["X"].append(feature_matrix(df.iloc[rows], cols))
        parts["ts"].append(ts[rows])
        parts["close"].append(df["close"].to_numpy(float)[rows])
        for s in specs:
            yl, ys, end = make_labels(ch.trades, ts, s, lat, wait, rows=rows)
            parts[f"yl_{s.key}"].append(yl[rows])
            parts[f"ys_{s.key}"].append(ys[rows])
            parts[f"end_{s.key}"].append(end[rows])
        log.info("jeu d'entraînement : bloc %s → %s, %d échantillons", day_str(ch_days[0]),
                 day_str(ch_days[-1]), rows.size)
    if cols is None:
        raise ValueError("aucun échantillon pour les jours demandés")
    out = {k: np.concatenate(v) for k, v in parts.items()}
    out["features"] = np.array(cols)
    return out


def fit_model(ds: dict, spec: LabelSpec, train_days, test_start_ms: int, test_end_ms: int | None,
              purge_ms: int, threshold: float, seed: int) -> tuple[TradeModel, dict]:
    ts = ds["ts"]
    end = ds[f"end_{spec.key}"]
    rows = np.isin(bar_day(ts), list(train_days)) & purge_mask(ts, end, test_start_ms, test_end_ms, purge_ms)
    m = TradeModel(spec, list(ds["features"]), threshold, seed)
    m.fit(ds["X"][rows], ds[f"yl_{spec.key}"][rows], ds[f"ys_{spec.key}"][rows])
    info = {"n_train": int(rows.sum()), "n_purged": int((np.isin(bar_day(ts), list(train_days)) & ~rows).sum()),
            **{f"{k}_{s}": v for s, d in m.train_info.items() for k, v in d.items()}}
    return m, info


# ============================================================================ moteur d'évaluation
@dataclass
class Strat:
    name: str
    fn: Callable[[pd.DataFrame, dict], np.ndarray]
    exits: dict | None = None
    group: str = "reference"          # model | reference | benchmark
    meta: dict = field(default_factory=dict)


def model_strat(name: str, model: TradeModel, threshold: float) -> Strat:
    def fn(df, ctx):
        ep = execution.ExecParams.from_cfg(ctx["cfg"], model.spec.exits_override())
        key = ("proba", id(model))
        if key not in ctx["cache"]:
            ctx["cache"][key] = model.predict_proba(df)
        return model.decide(df, ep, threshold, proba=ctx["cache"][key])
    return Strat(name, fn, model.spec.exits_override(), "model",
                 {"spec": model.spec.key, "threshold": threshold})


def rule_strat(s: signals.Strategy, p: dict, group: str, name: str | None = None) -> Strat:
    def fn(df, ctx):
        if not s.available(df):
            return np.zeros(len(df), np.int8)
        return s.signal(df, p)
    return Strat(name or f"{s.name}|{json.dumps(p, sort_keys=True)}", fn, getattr(s, "exits", None), group,
                 {"strategy": s.name, "params": p})


def random_matched_strat(name: str, ref: str, seed: int, exits: dict | None) -> Strat:
    """Entrées aléatoires : même nombre de trades et même part long/short que `ref` dans le bloc,
    mêmes sorties, mêmes coûts (benchmark « hasard » le plus juste)."""
    def fn(df, ctx):
        tr = ctx["chunk_trades"].get(ref)
        out = np.zeros(len(df), np.int8)
        cand = np.flatnonzero(ctx["in_period"])
        if tr is None or len(tr) == 0 or cand.size == 0:
            return out
        rng = np.random.default_rng([seed, ctx["chunk_idx"]])
        k = min(len(tr), cand.size)
        pick = rng.choice(cand, k, replace=False)
        p_long = float((tr["side"] > 0).mean())
        out[pick] = np.where(rng.random(k) < p_long, 1, -1)
        return out
    return Strat(name, fn, exits, "benchmark", {"matched_to": ref, "seed": seed})


def evaluate(src, days: list[int], strats: list[Strat], cfg: dict) -> tuple[dict[str, pd.DataFrame], dict]:
    """Backtest de plusieurs stratégies sur les mêmes jours, même moteur, bloc par bloc.

    Capital reporté d'un bloc à l'autre ; position chevauchant deux blocs respectée ;
    coupe-circuit (drawdown depuis le sommet) vérifié entre blocs.
    """
    r = cfg["risk"]
    eq0 = float(r["initial_equity"])
    kill_usdt = float(r.get("kill_drawdown_usdt", float("inf")))
    kill_frac = float(r.get("kill_drawdown_fraction", 1.0))
    no_wd = list(cfg["risk"].get("no_trade_weekdays", []))
    days_set = list(set(days))
    # un gestionnaire de risque PERSISTANT par stratégie : compteurs jour/semaine, sommet et
    # coupe-circuit se poursuivent d'un mois à l'autre (comme en live)
    risks = {s.name: RiskManager(deep_update(cfg, {"risk": {"initial_equity": eq0}})) for s in strats}
    killed: dict[str, bool] = {s.name: False for s in strats}
    busy_until = {s.name: -1 for s in strats}
    out: dict[str, list] = {s.name: [] for s in strats}
    mtm_t, mtm_c = [], []
    first_close = last_close = float("nan")
    for ci, ch_days in enumerate(src.chunks()):
        if not set(days_set).intersection(ch_days):
            continue
        ch = src.load_chunk(ch_days, cfg)
        df = ch.df
        ts = df["ts"].to_numpy(np.int64)
        close = df["close"].to_numpy(float)
        bd = bar_day(ts)
        in_p = np.isin(bd, days_set) & ~np.isin((bd + 3) % 7, no_wd)
        in_days = np.isin(bd, days_set)
        idx = np.flatnonzero(in_days)
        if idx.size:
            if not np.isfinite(first_close):
                first_close = float(close[idx[0]])
            last_close = float(close[idx[-1]])
            mtm_t.append(ts[idx][::12])
            mtm_c.append(close[idx][::12])
        vr = df["vol_regime"].to_numpy(float) if "vol_regime" in df else np.full(len(df), np.nan)
        ctx = {"cfg": cfg, "in_period": in_p, "chunk_idx": ci, "cache": {}, "chunk_trades": {}}
        for s in strats:
            if killed[s.name]:
                continue
            ent = np.asarray(s.fn(df, ctx), np.int8).copy()
            ent[~in_p] = 0
            ent[ts < busy_until[s.name]] = 0
            ep = execution.ExecParams.from_cfg(cfg, s.exits)
            tr = execution.simulate(ch.trades, ts, ent, cfg, ep, funding=getattr(src, "funding", None),
                                    ref_close=close, risk=risks[s.name])
            if risks[s.name].killed and not killed[s.name]:
                killed[s.name] = True
                log.warning("%s : coupe-circuit de drawdown atteint — arrêt définitif", s.name)
            if len(tr):
                k = np.clip(np.searchsorted(ts, tr["signal_ts"].to_numpy(np.int64)), 0, len(ts) - 1)
                tr["vol_regime"] = vr[k]
                tr["hour"] = pd.to_datetime(tr["entry_ts"], unit="ms", utc=True).dt.hour
                tr["dow"] = DOW_NAMES[pd.to_datetime(tr["entry_ts"], unit="ms", utc=True).dt.dayofweek.to_numpy()]
                tr["strategy"] = s.name
                busy_until[s.name] = int(tr["exit_ts"].max()) + int(r.get("cooldown_seconds", 0) * 1000)
            ctx["chunk_trades"][s.name] = tr
            out[s.name].append(tr)
    res = {k: (pd.concat(v, ignore_index=True) if v and any(len(x) for x in v) else pd.DataFrame())
           for k, v in out.items()}
    aux = {"start_ms": int(min(days) * DAY_MS), "end_ms": int((max(days) + 1) * DAY_MS - 1),
           "mtm_ts": np.concatenate(mtm_t) if mtm_t else np.array([], np.int64),
           "mtm_close": np.concatenate(mtm_c) if mtm_c else np.array([]),
           "first_close": first_close, "last_close": last_close, "killed": killed,
           "trading_days": [d for d in sorted(days_set) if (d + 3) % 7 not in no_wd]}
    return res, aux


# ============================================================================ métriques agrégées
def daily_series(tr: pd.DataFrame, aux: dict) -> pd.Series:
    d = metrics.daily_pnl(tr, aux["start_ms"], aux["end_ms"])
    td = [x for x in aux["trading_days"] if x in d.index]
    return d.loc[td]


def sr_daily(tr: pd.DataFrame, aux: dict, eq0: float) -> float:
    d = daily_series(tr, aux) / eq0
    sd = d.std(ddof=1)
    return float(d.mean() / sd) if len(d) > 2 and sd > 0 else float("nan")


def full_summary(tr: pd.DataFrame, aux: dict, cfg: dict) -> dict:
    eq0 = float(cfg["risk"]["initial_equity"])
    nd = max(1, len(aux["trading_days"]))
    s = metrics.summarize(tr, eq0, n_days=nd) if len(tr) else {"n_trades": 0, "net_pnl": 0.0,
                                                                 "exp_net_bps": float("nan")}
    s["sr_daily"] = sr_daily(tr, aux, eq0) if len(tr) else float("nan")
    return s


def regime_label(v: np.ndarray, q: list[float]) -> np.ndarray:
    lab = np.where(v <= q[0], "1_basse", np.where(v <= q[1], "2_moyenne", "3_haute")).astype(object)
    lab[~np.isfinite(v)] = "inconnu"
    return lab


def detailed(tr: pd.DataFrame, aux: dict, cfg: dict, n_trials: int, sr_var: float,
             regime_q: list[float]) -> dict:
    """Toutes les métriques obligatoires pour une stratégie (modèle sur val/test)."""
    eq0 = float(cfg["risk"]["initial_equity"])
    b = cfg.get("bootstrap", {})
    s = full_summary(tr, aux, cfg)
    if len(tr) == 0:
        return {"summary": s}
    nb, seed = int(b.get("n_resamples", 2000)), int(b.get("seed", 0))
    blk, min_days = int(b.get("block_days", 3)), int(b.get("min_days", metrics.MIN_DAYS_CI))
    lo, hi, p = metrics.trade_bootstrap_by_day(tr, nb, seed, blk, min_days)
    g = tr.assign(net_bps=tr["gross_bps"])
    glo, ghi, gp = metrics.trade_bootstrap_by_day(g, nb, seed, blk, min_days)
    daily = daily_series(tr, aux)
    dsr = metrics.deflated_sharpe(daily, eq0, n_trials, sr_var, min_days)
    n_days_tr = int(tr["entry_ts"].floordiv(DAY_MS).nunique())
    mtm = metrics.mtm_equity(tr, aux["mtm_ts"], aux["mtm_close"], eq0, step=1) if len(aux["mtm_ts"]) else pd.Series(dtype=float)
    tr = tr.assign(regime=regime_label(tr["vol_regime"].to_numpy(float), regime_q),
                   side_name=np.where(tr["side"] > 0, "long", "short"))

    def bd(by):
        x = metrics.breakdown(tr, by)
        return {str(k): {kk: float(vv) for kk, vv in row.items()} for k, row in x.iterrows()}
    return {
        "summary": s,
        "ci95_exp_net_bps": [lo, hi], "p_value_exp_net": p,
        "n_days": len(aux["trading_days"]), "n_days_with_trades": n_days_tr, "min_days_ci": min_days,
        "ci_note": "" if np.isfinite(lo) else
        f"IC non calculable : {n_days_tr} jour(s) avec trades < {min_days} requis",
        "ci95_exp_gross_bps": [glo, ghi],
        "dsr": dsr, "n_trials": n_trials, "sr_trials_var": sr_var,
        "sharpe_annualized": metrics.sharpe(daily, eq0, periods=252),
        "max_dd_mtm_pct": metrics.max_drawdown_pct(mtm) if len(mtm) else float("nan"),
        "slippage_sensitivity": metrics.slippage_sensitivity(tr),
        "fee_sensitivity": _safe(lambda: metrics.fee_sensitivity(tr, cfg)),
        "by_hour": bd("hour"), "by_dow": bd("dow"), "by_regime": bd("regime"), "by_side": bd("side_name"),
        "by_exit": bd("exit_reason"),
    }


def _safe(f):
    try:
        return f()
    except Exception as e:  # API metrics en évolution : ne pas bloquer le rapport
        log.warning("métrique indisponible : %r", e)
        return {"erreur": repr(e)}


def buy_and_hold(aux: dict, cfg: dict) -> dict:
    """Achat au premier cours de la période, vente au dernier, 1× le capital, 2 jambes taker
    (funding non compté : estimation)."""
    from .config import venue_profile
    eq0 = float(cfg["risk"]["initial_equity"])
    try:
        fee = venue_profile(cfg)["taker_fee_bps"]
    except Exception:
        fee = 5.0
    r = aux["last_close"] / aux["first_close"] - 1 if np.isfinite(aux["first_close"]) else float("nan")
    pnl = eq0 * r - 2 * fee * 1e-4 * eq0
    return {"n_trades": 1, "net_pnl": float(pnl), "net_return_pct": float(pnl / eq0 * 100),
            "exp_net_bps": float(r * 1e4 - 2 * fee), "note": "sans funding"}


# ============================================================================ exécution d'un run
class Run:
    def __init__(self, root: str | Path, run_id: str, cfg: dict):
        self.id = run_id
        self.dir = Path(root) / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.root = Path(root)
        self.cfg = cfg
        self.cfg_hash = config_hash(cfg)

    def save(self, name: str, obj) -> Path:
        p = self.dir / name
        p.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=_json_default), encoding="utf-8")
        return p

    def load(self, name: str):
        p = self.dir / name
        if not p.exists():
            raise FileNotFoundError(f"{p} absent : lancer d'abord l'étape qui le produit")
        return json.loads(p.read_text(encoding="utf-8"))

    def has(self, name: str) -> bool:
        return (self.dir / name).exists()

    def source_params(self) -> dict:
        return self.load("source.json")

    def trial_log(self) -> TrialLog:
        sp = self.source_params()
        if sp["kind"] == "synthetic":
            return TrialLog(self.dir / "trials.jsonl")
        return TrialLog(self.root / "trials_registry.jsonl")      # cumulé sur tous les runs réels


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (pd.Timestamp,)):
        return o.isoformat()
    return str(o)


def _clean(o):
    """NaN/inf -> None pour un JSON valide."""
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (float, np.floating)):
        return float(o) if np.isfinite(o) else None
    if isinstance(o, np.integer):
        return int(o)
    return o


def _summ_row(name: str, group: str, s: dict) -> dict:
    keys = ("n_trades", "exp_net_bps", "exp_gross_bps", "net_pnl", "profit_factor", "win_rate",
            "max_dd_realized_pct", "sr_daily", "long_n", "short_n")
    return {"name": name, "group": group, **{k: s.get(k) for k in keys}}


# ----------------------------------------------------------------------------- research
def run_research(run: Run, src) -> dict:
    cfg = run.cfg
    # Le robot exécute sur Kraken : un modèle entraîné avec un autre profil (ex. funding 8 h ->
    # sec_to_funding différent) ne serait pas celui que le robot exécute. Refus explicite.
    venue = cfg.get("venue", {}).get("execution")
    if venue != "kraken":
        raise ValueError(f"[venue].execution = {venue!r} : la recherche doit être entraînée avec le "
                         "profil 'kraken' (plateforme d'exécution du robot). Le profil binance ne sert "
                         "qu'en analyse de sensibilité.")
    sp = cfg["split"]
    no_wd = set(cfg["risk"].get("no_trade_weekdays", []))
    # découpage sur les jours de TRADING entiers (les week-ends exclus ne portent aucun trade) ;
    # les jours sans trading restent chargés pour la chauffe des facteurs.
    split = chronological_split([d for d in src.days if (d + 3) % 7 not in no_wd],
                                sp["dev_fraction"], sp["val_fraction"])
    run.save("split.json", {k: [day_str(d) for d in v] for k, v in split.items()})
    log.info("découpage : dev %s→%s (%d j), val %s→%s (%d j), test %s→%s (%d j) [test NON lu]",
             *(x for k in ("dev", "val", "test") for x in (day_str(split[k][0]), day_str(split[k][-1]), len(split[k]))))
    specs = label_specs_from_cfg(cfg)
    thr_grid = threshold_grid_from_cfg(cfg)
    seed = int(cfg.get("ml", {}).get("seed", 7))
    purge = purge_ms_for(specs, cfg)
    ds = build_dataset(src, split["dev"] + split["val"], specs, cfg)
    np.savez_compressed(run.dir / "dataset_devval.npz", **ds)
    val_start = split["val"][0] * DAY_MS
    val_end = (split["val"][-1] + 1) * DAY_MS
    vmask = np.isin(bar_day(ds["ts"]), split["val"])
    strats, models, diag = [], {}, {}
    for spec in specs:
        m, info = fit_model(ds, spec, split["dev"], val_start, val_end, purge, thr_grid[0], seed)
        models[spec.key] = m
        pl, ps = m._proba("long", ds["X"][vmask]), m._proba("short", ds["X"][vmask])
        diag[spec.key] = {**info, "val_auc_long": auc(ds[f"yl_{spec.key}"][vmask], pl),
                          "val_auc_short": auc(ds[f"ys_{spec.key}"][vmask], ps),
                          "val_pos_rate_long": float(np.nanmean(ds[f"yl_{spec.key}"][vmask])),
                          "val_pos_rate_short": float(np.nanmean(ds[f"ys_{spec.key}"][vmask]))}
        log.info("cible %s : %s", spec.key, {k: round(v, 4) if isinstance(v, float) else v
                                               for k, v in diag[spec.key].items()})
        for thr in thr_grid:
            strats.append(model_strat(f"M|{spec.key}|p{thr:.2f}", m, thr))
    for s in signals.STRATEGIES:
        for p in s.param_sets():
            strats.append(rule_strat(s, p, "reference"))
    for s in signals.BENCHMARKS:
        for p in s.param_sets():
            strats.append(rule_strat(s, p, "benchmark"))
    log.info("évaluation sur val de %d configurations (modèle %d, règles %d)", len(strats),
             sum(s.group == "model" for s in strats), sum(s.group != "model" for s in strats))
    res, aux = evaluate(src, split["val"], strats, cfg)
    tl = run.trial_log()
    rows = []
    min_n = int(cfg.get("ml", {}).get("min_trades_val", 30))
    for s in strats:
        sm = full_summary(res[s.name], aux, cfg)
        rows.append({**_summ_row(s.name, s.group, sm), **s.meta})
        if s.group != "benchmark":
            tl.add(s.name, run.cfg_hash, run.id, sm.get("sr_daily", float("nan")), sm.get("n_trades", 0),
                   sm.get("exp_net_bps", float("nan")), s.group)
    table = pd.DataFrame(rows)
    table.to_csv(run.dir / "val_results.csv", index=False)

    def pick(df):
        ok = df[df["n_trades"] >= min_n]
        base = ok if len(ok) else df
        return base.sort_values("net_pnl", ascending=False).iloc[0], bool(len(ok))
    mt = table[table["group"] == "model"]
    best, sel_ok = pick(mt)
    spec = models[best["spec"]].spec
    thr = float(best["threshold"])
    ref_params = {}
    for s in signals.STRATEGIES:
        t = table[(table["group"] == "reference") & (table["strategy"] == s.name)]
        if len(t) and t["n_trades"].fillna(0).max() > 0:
            b, ok = pick(t)
            ref_params[s.name] = {"params": b["params"], "val_n_trades": int(b["n_trades"]),
                                  "val_net_pnl": float(b["net_pnl"]), "min_trades_ok": ok}
    # modèle retenu : entraîné sur dev (purgé), seuil choisi sur val
    model = models[spec.key]
    model.threshold = thr
    vr_idx = list(ds["features"]).index("vol_regime") if "vol_regime" in list(ds["features"]) else None
    dev_mask = np.isin(bar_day(ds["ts"]), split["dev"])
    if vr_idx is not None:
        q = np.nanquantile(ds["X"][dev_mask, vr_idx], [1 / 3, 2 / 3]).tolist()
    else:
        q = [float("nan"), float("nan")]
    meta = model.save(run.dir / "model" / "model.pkl", run.cfg_hash, day_str(split["dev"][0]),
                      day_str(split["dev"][-1]), extra={"run_id": run.id, "data": src.params,
                                                         "threshold_chosen_on": "val",
                                                         "val_period": [day_str(split["val"][0]),
                                                                        day_str(split["val"][-1])]})
    sel = {"config_hash": run.cfg_hash, "spec": spec.key, "label_spec": spec.__dict__, "threshold": thr,
           "selection_min_trades_ok": sel_ok, "min_trades_val": min_n,
           "model_path": "model/model.pkl", "model_sha256": meta["sha256"], "reference_params": ref_params,
           "regime_quantiles_dev": q, "purge_ms": purge, "label_diagnostics": diag,
           "n_trials_total": tl.n_trials(), "trial_log": str(tl.path),
           "val_best_model": _clean(best.to_dict()), "n_val_days": len(split["val"]),
           "small_sample_warning": (
               f"échantillon trop petit : {len(split['val'])} jours de val < "
               f"{int(cfg.get('bootstrap', {}).get('min_days', metrics.MIN_DAYS_CI))} ; AUC et sélection "
               "du seuil / de la cible dominées par le bruit"
               if len(split["val"]) < int(cfg.get("bootstrap", {}).get("min_days", metrics.MIN_DAYS_CI)) else "")}
    run.save("selection.json", _clean(sel))
    # détails du modèle retenu sur val (pour le rapport)
    name = f"M|{spec.key}|p{thr:.2f}"
    det = detailed(res[name], aux, cfg, tl.n_trials(), tl.sr_variance(), q)
    run.save("val_model.json", _clean(det))
    log.info("retenu sur val : cible %s, seuil %.2f, %s trades, espérance nette %s bps",
             spec.key, thr, best["n_trades"], _fmt(best["exp_net_bps"]))
    return sel


def _fmt(x, nd=2):
    try:
        return f"{float(x):.{nd}f}"
    except (TypeError, ValueError):
        return "n/a"


# ----------------------------------------------------------------------------- walk-forward
def run_walkforward(run: Run, src) -> dict:
    cfg = run.cfg
    sel = run.load("selection.json")
    split = {k: [int((dt.date.fromisoformat(x) - dt.date(1970, 1, 1)).days) for x in v]
             for k, v in run.load("split.json").items()}
    spec = LabelSpec(**sel["label_spec"])
    thr = float(sel["threshold"])
    seed = int(cfg.get("ml", {}).get("seed", 7))
    wf = cfg["walkforward"]
    p = run.dir / "dataset_devval.npz"
    if p.exists():
        with np.load(p, allow_pickle=False) as f:
            ds = {k: f[k] for k in f.files}
    else:
        ds = build_dataset(src, split["dev"] + split["val"], [spec], cfg)
    if f"end_{spec.key}" not in ds:
        ds = build_dataset(src, split["dev"] + split["val"], [spec], cfg)
    purge = purge_ms_for([spec], cfg)
    days = split["dev"] + split["val"]        # le test reste verrouillé
    folds = walkforward_folds(days, int(wf["train_days"]), int(wf["test_days"]))
    if not folds:
        raise ValueError("pas assez de jours pour le walk-forward (réduire [walkforward].train_days)")
    min_tr = int(wf.get("min_trades_train", 30))
    rows, all_tr, frozen_tr = [], [], []
    frozen = None      # modèle du 1er pli valide, JAMAIS réentraîné (référence de l'étape 2 : figé vs réentraîné)
    for i, f in enumerate(folds):
        t0, t1 = f["test"][0] * DAY_MS, (f["test"][-1] + 1) * DAY_MS
        m, info = fit_model(ds, spec, f["train"], t0, t1, purge, thr, seed)
        if min(info.get("n_long", 0), info.get("n_short", 0)) < min_tr:
            log.warning("pli %d ignoré : trop peu d'échantillons d'entraînement", i)
            continue
        first = frozen is None
        if first:
            frozen = m
        rnd = next(b for b in signals.BENCHMARKS if "random" in b.name)
        strats = [model_strat("model", m, thr), rule_strat(rnd, rnd.param_sets()[0], "benchmark", "random")]
        if not first:     # au 1er pli les deux modèles sont identiques : comparaison sans objet
            strats.append(model_strat("frozen", frozen, thr))
        res, aux = evaluate(src, f["test"], strats, cfg)
        sm = full_summary(res["model"], aux, cfg)
        sr = full_summary(res["random"], aux, cfg)
        sf = full_summary(res["frozen"], aux, cfg) if not first else {}
        rows.append({"fold": i, "train": f"{day_str(f['train'][0])}→{day_str(f['train'][-1])}",
                     "test": f"{day_str(f['test'][0])}→{day_str(f['test'][-1])}", **info,
                     "n_trades": sm.get("n_trades"), "exp_net_bps": sm.get("exp_net_bps"),
                     "exp_gross_bps": sm.get("exp_gross_bps"), "net_pnl": sm.get("net_pnl"),
                     "random_n": sr.get("n_trades"), "random_exp_net_bps": sr.get("exp_net_bps"),
                     "frozen_n": sf.get("n_trades"), "frozen_exp_net_bps": sf.get("exp_net_bps"),
                     "frozen_net_pnl": sf.get("net_pnl")})
        if len(res["model"]):
            all_tr.append(res["model"].assign(fold=i))
        if not first and len(res["frozen"]):
            frozen_tr.append(res["frozen"].assign(fold=i))
        log.info("pli %d %s : %s trades, espérance nette %s bps", i, rows[-1]["test"], sm.get("n_trades"),
                 _fmt(sm.get("exp_net_bps")))
    tr = pd.concat(all_tr, ignore_index=True) if all_tr else pd.DataFrame()
    if len(tr):
        tr.to_csv(run.dir / "walkforward_trades.csv", index=False)
    pd.DataFrame(rows).to_csv(run.dir / "walkforward_folds.csv", index=False)
    test_days = sorted({d for f in folds for d in f["test"]})
    no_wd = cfg["risk"].get("no_trade_weekdays", [])
    aux = {"start_ms": test_days[0] * DAY_MS, "end_ms": (test_days[-1] + 1) * DAY_MS - 1,
           "trading_days": [d for d in test_days if (d + 3) % 7 not in no_wd]}
    cmp_days = sorted({d for r in rows if r.get("frozen_n") is not None for d in folds[r["fold"]]["test"]})
    aux_cmp = {"start_ms": cmp_days[0] * DAY_MS, "end_ms": (cmp_days[-1] + 1) * DAY_MS - 1,
               "trading_days": [d for d in cmp_days if (d + 3) % 7 not in no_wd]} if cmp_days else aux
    out = {"spec": spec.key, "threshold": thr, "purge_ms": purge, "n_folds": len(rows),
           "folds": rows, "aggregate": full_summary(tr, aux, cfg) if len(tr) else {"n_trades": 0},
           "positive_folds": int(sum((r["net_pnl"] or 0) > 0 for r in rows)),
           "retrain_vs_frozen": retrain_vs_frozen(rows, tr, frozen_tr, aux_cmp, cfg)}
    run.save("walkforward.json", _clean(out))
    return out


def retrain_vs_frozen(rows: list[dict], tr: pd.DataFrame, frozen_tr: list[pd.DataFrame], aux: dict,
                      cfg: dict) -> dict:
    """Étape 2 (condition d'activation) : le réentraînement glissant bat-il le modèle figé ?

    Mesuré sur les plis 2..n uniquement (au 1er pli les deux modèles sont le même), sur les
    mêmes jours et avec le même moteur. Indicatif : la cible et le seuil ont été choisis sur val
    (voir les limites du walk-forward). « Le réentraînement aide » exige ici un PnL net cumulé
    supérieur ET une majorité de plis gagnés ; c'est une condition nécessaire, pas une preuve.
    """
    cmp = [r for r in rows if r.get("frozen_n") is not None]
    if not cmp:
        return {"n_folds_compared": 0, "verdict": "non mesurable (moins de 2 plis valides)"}
    tr_cmp = tr[tr["fold"].isin([r["fold"] for r in cmp])] if len(tr) else tr
    fz = pd.concat(frozen_tr, ignore_index=True) if frozen_tr else pd.DataFrame()
    ret_pnl = float(sum(r["net_pnl"] or 0.0 for r in cmp))
    frz_pnl = float(sum(r["frozen_net_pnl"] or 0.0 for r in cmp))
    wins = int(sum((r["net_pnl"] or 0.0) > (r["frozen_net_pnl"] or 0.0) for r in cmp))
    helps = ret_pnl > frz_pnl and wins > len(cmp) / 2
    return {"n_folds_compared": len(cmp), "retrained_net_pnl": ret_pnl, "frozen_net_pnl": frz_pnl,
            "folds_retrained_better": wins,
            "retrained": full_summary(tr_cmp, aux, cfg) if len(tr_cmp) else {"n_trades": 0},
            "frozen": full_summary(fz, aux, cfg) if len(fz) else {"n_trades": 0},
            "verdict": "le réentraînement fait mieux que le modèle figé (indicatif)" if helps
            else "le réentraînement ne fait PAS mieux que le modèle figé"}


# ----------------------------------------------------------------------------- test final
def _lock_params(sel: dict, split: dict) -> dict:
    return {"model_sha256": sel["model_sha256"], "spec": sel["spec"], "threshold": sel["threshold"],
            "reference_params": {k: v["params"] for k, v in sel["reference_params"].items()},
            "regime_quantiles_dev": sel["regime_quantiles_dev"], "test_days": [split["test"][0], split["test"][-1]]}


def _days(run: Run) -> dict[str, list[int]]:
    return {k: [int((dt.date.fromisoformat(x) - dt.date(1970, 1, 1)).days) for x in v]
            for k, v in run.load("split.json").items()}


def kraken_cfg(cfg: dict) -> dict:
    """Profil Kraken à la latence de BASE de la config (grâce de clôture de barre incluse)."""
    return deep_update(cfg, {"venue": {"execution": "kraken"}})


def _lock_fingerprint(cfg_hash: str, params: dict) -> str:
    params = json.loads(json.dumps(params, sort_keys=True, default=str))
    return config_hash({"config_hash": cfg_hash, "params": params})


def check_final_test_registry(registry: Path, test_period: list[str], fingerprint: str,
                              force: bool = False) -> list[dict]:
    """Registre global (données réelles) : refuse d'évaluer une période de test qui CHEVAUCHE une
    période déjà évaluée avec une autre empreinte (nouveau --run-id ou verrou supprimé = même
    fuite que réévaluer le test), sauf force. Renvoie les évaluations antérieures concernées."""
    if not registry.exists():
        return []
    prior = []
    for line in registry.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        t = e.get("test") or [None, None]
        if t[0] is None or t[0] > test_period[-1] or t[-1] < test_period[0]:
            continue
        if e.get("fingerprint") != fingerprint:
            prior.append(e)
    if prior and not force:
        raise FinalTestLockError(
            f"{len(prior)} évaluation(s) antérieure(s) d'une période de test chevauchante avec une autre "
            f"configuration (ex. run {prior[0].get('run_id')}). Le test n'est plus vierge : refus. "
            "--force seulement en connaissance de cause (journalisé).")
    return prior


def _bench_table(strats: list[Strat], res: dict, aux: dict, cfg: dict) -> tuple[list[dict], dict]:
    table = []
    for s in strats:
        if s.name.startswith("RANDOM_MATCHED_"):
            continue
        table.append(_summ_row(s.name, s.group, full_summary(res[s.name], aux, cfg)))
    rnd = [full_summary(res[s.name], aux, cfg) for s in strats if s.name.startswith("RANDOM_MATCHED_")]
    R = len(rnd)
    rnd_exp = np.array([x.get("exp_net_bps", np.nan) for x in rnd], float)
    rnd_pnl = np.array([x.get("net_pnl", 0.0) for x in rnd], float)
    has_rnd = bool(np.isfinite(rnd_exp).any())
    random_matched = {"n_seeds": R, "exp_net_bps_mean": float(np.nanmean(rnd_exp)) if has_rnd else None,
                      "exp_net_bps_p95": float(np.nanpercentile(rnd_exp, 95)) if has_rnd else None,
                      "net_pnl_mean": float(rnd_pnl.mean()) if R else None,
                      "net_pnl_p95": float(np.percentile(rnd_pnl, 95)) if R else None}
    table.append({"name": "RANDOM_MATCHED (moyenne)", "group": "benchmark",
                  "n_trades": float(np.mean([x.get("n_trades", 0) for x in rnd])) if R else 0,
                  "exp_net_bps": random_matched["exp_net_bps_mean"], "net_pnl": random_matched["net_pnl_mean"]})
    table.append({"name": "NE_RIEN_FAIRE", "group": "benchmark", "n_trades": 0, "net_pnl": 0.0,
                  "exp_net_bps": None})
    bh = buy_and_hold(aux, cfg)
    table.append({"name": "BUY_AND_HOLD", "group": "benchmark", **{k: bh[k] for k in ("n_trades", "net_pnl", "exp_net_bps")}})
    return table, random_matched


def run_final_test(run: Run, src, force: bool = False) -> dict:
    cfg = run.cfg
    sel = run.load("selection.json")
    split = run.load("split.json")
    days = _days(run)
    if sel["config_hash"] != run.cfg_hash and not force:
        raise FinalTestLockError(f"le modèle a été sélectionné avec la config {sel['config_hash']}, "
                                 f"config courante {run.cfg_hash} : refus (relancer research ou --force)")
    params = _lock_params(sel, split)
    fp = _lock_fingerprint(run.cfg_hash, params)
    sp = run.source_params()
    prior: list[dict] = []
    if sp["kind"] != "synthetic":
        prior = check_final_test_registry(run.root / "final_test_registry.jsonl", split["test"], fp, force)
    acquire_final_test_lock(run.dir, run.cfg_hash, params, force=force)
    if sp["kind"] != "synthetic":
        with open(run.root / "final_test_registry.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"utc": _now(), "run_id": run.id, "config_hash": run.cfg_hash,
                                "fingerprint": fp, "forced": bool(force and prior),
                                "test": [split["test"][0], split["test"][-1]]}) + "\n")
    model = TradeModel.load(run.dir / sel["model_path"])
    thr = float(sel["threshold"])
    R = int(cfg.get("ml", {}).get("n_random_benchmarks", 20))
    strats = [model_strat("MODEL", model, thr)]
    for s in signals.STRATEGIES:
        if s.name in sel["reference_params"]:
            strats.append(rule_strat(s, sel["reference_params"][s.name]["params"], "reference", s.name))
    for s in signals.BENCHMARKS:
        for p in s.param_sets():
            strats.append(rule_strat(s, p, "benchmark", s.name if len(s.param_sets()) == 1 else None))
    for i in range(R):
        strats.append(random_matched_strat(f"RANDOM_MATCHED_{i:02d}", "MODEL", 1000 + i, model.spec.exits_override()))
    log.info("TEST FINAL (verrouillé) : %s → %s, %d stratégies", split["test"][0], split["test"][-1], len(strats))
    res, aux = evaluate(src, days["test"], strats, cfg)
    tl = run.trial_log()
    n_trials, sr_var = max(1, tl.n_trials()), tl.sr_variance()
    q = sel["regime_quantiles_dev"]
    det = detailed(res["MODEL"], aux, cfg, n_trials, sr_var, q)
    table, random_matched = _bench_table(strats, res, aux, cfg)
    # critères : TOUT (modèle, naïfs, aléatoires) avec le profil Kraken à la latence d'entrée de
    # base (grâce de clôture + envoi, comme le robot) — réutilise le résultat si c'est déjà la config
    kc = kraken_cfg(cfg)
    base_lat = entry_latency_ms(kc)
    if config_hash(kc) == config_hash(cfg):
        kres, kaux, ktable, krm = res, aux, table, random_matched
    else:
        kres, kaux = evaluate(src, days["test"], strats, kc)
        ktable, krm = _bench_table(strats, kres, kaux, kc)
    kdet = detailed(kres["MODEL"], kaux, kc, n_trials, sr_var, q)
    pd.DataFrame(table).to_csv(run.dir / "final_test_table.csv", index=False)
    if len(res["MODEL"]):
        res["MODEL"].to_csv(run.dir / "final_test_trades_model.csv", index=False)
    min_test_days = int(cfg["split"].get("min_test_days", 60))
    crit = criteria(kdet, ktable, krm, n_test_days=len(kaux["trading_days"]), min_test_days=min_test_days,
                    base_latency_ms=base_lat)
    out = {"period": [split["test"][0], split["test"][-1]], "venue": cfg["venue"]["execution"],
           "latency_ms": entry_latency_ms(cfg), "verdict_latency_ms": base_lat, "model": det,
           "model_kraken_base": kdet, "table": table, "table_kraken_base": ktable,
           "random_matched": random_matched, "random_matched_kraken_base": krm, "buy_and_hold": buy_and_hold(aux, cfg), "criteria": crit,
           "prior_test_evaluations_other_config": len(prior),
           "lock": json.loads((run.dir / "final_test_lock.json").read_text(encoding="utf-8"))}
    run.save("final_test.json", _clean(out))
    from . import report
    report.plot_final(run.dir, res["MODEL"], aux, cfg, q)
    log.info("VERDICT : %s", crit["verdict"])
    return out


def criteria(kdet: dict, table: list[dict], random_matched: dict, n_test_days: int | None = None,
             min_test_days: int = 60, base_latency_ms: int = 600) -> dict:
    """Critères chiffrés « Backtest → démo » (§7), évalués avec le profil Kraken à la latence
    d'entrée de base `base_latency_ms` (grâce de clôture de barre incluse) ; `kdet`, `table`,
    `random_matched` doivent TOUS venir de ce profil.

    Critère 8 (ajout de l'audit) : au moins `min_test_days` jours de trading dans le test final.
    """
    s = kdet["summary"]
    n = s.get("n_trades", 0) or 0
    exp = s.get("exp_net_bps", float("nan"))
    exp = float("nan") if exp is None else exp
    lo = (kdet.get("ci95_exp_net_bps") or [float("nan")])[0]
    lo = float("nan") if lo is None else lo
    dsr = kdet.get("dsr", float("nan"))
    dsr = float("nan") if dsr is None else dsr
    by_reg = kdet.get("by_regime", {})
    pos_reg = sum(1 for k, v in by_reg.items() if k != "inconnu" and (v.get("exp_net_bps") or -1) > 0)
    slip1 = kdet.get("slippage_sensitivity", {}).get("+1.0bps", float("nan"))
    slip1 = float("nan") if slip1 is None else slip1
    model_pnl = s.get("net_pnl", 0.0) or 0.0
    naive = [r for r in table if r["group"] == "benchmark" and r["name"].startswith("B_")]
    beats_naive = all(model_pnl > (r.get("net_pnl") or 0.0) for r in naive) and len(naive) > 0
    p95 = random_matched.get("exp_net_bps_p95")
    beats_rand = bool(p95 is not None and np.isfinite(exp) and exp > p95)
    nd = n_test_days if n_test_days is not None else kdet.get("n_days")

    def ok(x):
        return bool(x) if x is not None else False
    c = [
        {"id": 1, "critere": "≥ 300 trades sur le test", "valeur": n, "ok": ok(n >= 300)},
        {"id": 2, "critere": "espérance nette > 0 ET borne basse IC 95 % > 0 (bootstrap par blocs de jours)",
         "valeur": f"{_fmt(exp)} bps, IC bas {_fmt(lo)}" + (f" ({kdet['ci_note']})" if kdet.get("ci_note") else ""),
         "ok": ok(np.isfinite(exp) and exp > 0 and np.isfinite(lo) and lo > 0)},
        {"id": 3, "critere": "DSR ≥ 0,95", "valeur": _fmt(dsr, 3), "ok": ok(np.isfinite(dsr) and dsr >= 0.95)},
        {"id": 4, "critere": "bat les entrées aléatoires (p95 de l'espérance) et les benchmarks naïfs (PnL net)",
         "valeur": f"aléatoire p95 {_fmt(p95)} bps ; naïfs battus : {beats_naive}", "ok": ok(beats_rand and beats_naive)},
        {"id": 5, "critere": "positif dans ≥ 2 régimes de volatilité sur 3", "valeur": pos_reg, "ok": ok(pos_reg >= 2)},
        {"id": 6, "critere": f"positif à la latence de base ({base_latency_ms} ms) et +1 bp de slippage, "
                             "profil Kraken",
         "valeur": f"{_fmt(slip1)} bps", "ok": ok(np.isfinite(slip1) and slip1 > 0)},
        {"id": 7, "critere": "signal confirmé sur données Kraken", "valeur": "NON ÉVALUÉ (pas de données Kraken dans ce pipeline)",
         "ok": False},
        {"id": 8, "critere": f"≥ {min_test_days} jours de trading dans le test final (ajout audit : "
                             "IC et DSR crédibles, plusieurs régimes)",
         "valeur": nd, "ok": ok(nd is not None and nd >= min_test_days)},
    ]
    n_ok = sum(x["ok"] for x in c)
    return {"items": c, "n_ok": n_ok, "n_total": len(c),
            "verdict": "GO (backtest → démo)" if n_ok == len(c) else
            f"NO-GO — stratégie non validée ({n_ok}/{len(c)} critères remplis)"}


# ----------------------------------------------------------------------------- sensibilités
def run_sensitivity(run: Run, src, period: str = "val") -> dict:
    cfg = run.cfg
    sel = run.load("selection.json")
    split = run.load("split.json")
    days = _days(run)
    if period == "test" and not lock_matches(run.dir, run.cfg_hash, _lock_params(sel, split)):
        raise FinalTestLockError("sensibilités sur le test : lancer d'abord final-test avec cette même configuration")
    model = TradeModel.load(run.dir / sel["model_path"])
    thr = float(sel["threshold"])
    grid = threshold_grid_from_cfg(cfg)
    i = grid.index(thr) if thr in grid else 0
    variants = []
    for venue in ("binance", "kraken"):
        for lat in LATENCY_GRID_MS:
            variants.append((f"{venue}_{lat}ms_p{thr:.2f}",
                             with_entry_latency(deep_update(cfg, {"venue": {"execution": venue}}), lat), thr))
    for j in (i - 1, i + 1):           # voisinage du seuil (profil et latence de base)
        if 0 <= j < len(grid):
            variants.append((f"base_p{grid[j]:.2f}", cfg, grid[j]))
    for name, ov in ORDER_TYPE_VARIANTS.items():     # profil Kraken (le plus pénalisant), latence de base
        variants.append((name, deep_update(kraken_cfg(cfg), ov), thr))
    rows, base = [], None
    for name, c, t in variants:
        res, aux = evaluate(src, days[period], [model_strat("MODEL", model, t)], c)
        sm = full_summary(res["MODEL"], aux, c)
        row = _summ_row(name, "sensibilite", sm)
        row.update({"venue": c["venue"]["execution"], "latency_ms": entry_latency_ms(c), "threshold": t,
                    "maker_entry": bool(c["maker_entry"]["enabled"]),
                    "tp_as_maker": bool(c["exits"]["take_profit_as_maker"])})
        if len(res["MODEL"]):
            row["slippage"] = metrics.slippage_sensitivity(res["MODEL"])
            row["fees"] = _safe(lambda: metrics.fee_sensitivity(res["MODEL"], c))
        rows.append(row)
        log.info("sensibilité %s : %s trades, espérance nette %s bps", name, sm.get("n_trades"),
                 _fmt(sm.get("exp_net_bps")))
    out = {"period": period, "rows": rows}
    run.save(f"sensitivity_{period}.json", _clean(out))
    return out
