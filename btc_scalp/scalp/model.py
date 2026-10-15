"""Étape 1 : un modèle de gradient boosting (un classifieur long, un classifieur short).

Cible (cahier des charges §4) — SANS FUITE :
    label_long[k] = 1 si, après l'arrivée théorique de l'ordre (ts_k + latence), le prix
    imprime STRICTEMENT au-dessus de entrée × (1 + TP) avant d'imprimer à entrée × (1 − stop) ou
    en dessous, dans l'horizon H ; 0 sinon (stop touché, ou rien dans l'horizon).
    Le prix d'entrée de référence est le 1er trade STRICTEMENT après ts_k + latence (comme
    execution.simulate).
    La cible n'utilise QUE des trades strictement postérieurs à ts_k + latence : elle est
    donc INTERDITE comme feature (les colonnes y_* / lbl_* ne sont jamais des features).

Décision à la clôture de la barre k :
    entrer long si p_long ≥ seuil ET gain attendu > 0, avec
    gain attendu = p × (TP − coûts_gagnant) − (1 − p) × (stop + coûts_perdant)
    (coûts : frais, slippage, 1 tick de spread estimé ; voir `cost_bps`). Symétrique en short.
    Un label 0 compte comme une perte de `stop` (hypothèse conservatrice : la sortie au
    time stop perd en général moins).

Hyperparamètres FIXÉS à l'avance (aucune recherche d'hyperparamètres), graine fixe.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import pickle
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

MODEL_FORMAT_VERSION = 1

# Fixés AVANT tout test. Ne pas ajuster sur les résultats (sinon : nouvel essai à compter).
HYPERPARAMS = dict(learning_rate=0.05, max_iter=200, max_leaf_nodes=15, min_samples_leaf=200,
                   l2_regularization=1.0, max_bins=255, early_stopping=False)
DEFAULT_SEED = 7
DEFAULT_THRESHOLD_GRID = (0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80)
FORBIDDEN_PREFIXES = ("y_", "lbl_", "p_long", "p_short")


@dataclass(frozen=True)
class LabelSpec:
    tp_bps: float
    stop_bps: float
    horizon_s: float

    @property
    def key(self) -> str:
        return f"tp{self.tp_bps:g}_sl{self.stop_bps:g}_h{self.horizon_s:g}"

    def exits_override(self) -> dict:
        """Les sorties du trade simulé = celles de la cible (cohérence proba ↔ gain attendu)."""
        return {"take_profit_bps": float(self.tp_bps), "stop_bps": float(self.stop_bps),
                "time_stop_seconds": float(self.horizon_s)}


def label_specs_from_cfg(cfg: dict) -> list[LabelSpec]:
    """Grille courte fixée avant les tests (§4).

    Par défaut : (X = [ml].min_edge_bps, stop = [exits].stop_bps, H = [ml].horizon_seconds)
    et (TP/stop/durée de [exits]). Surcharge possible : [ml].label_grid = [[tp, stop, H], ...].
    """
    ml, ex = cfg.get("ml", {}), cfg["exits"]
    if "label_grid" in ml:
        specs = [LabelSpec(float(a), float(b), float(c)) for a, b, c in ml["label_grid"]]
    else:
        specs = [LabelSpec(float(ml.get("min_edge_bps", 12.0)), float(ex["stop_bps"]),
                           float(ml.get("horizon_seconds", 60))),
                 LabelSpec(float(ex["take_profit_bps"]), float(ex["stop_bps"]),
                           float(ex["time_stop_seconds"]))]
    out = []
    for s in specs:
        if s not in out:
            out.append(s)
    return out


def threshold_grid_from_cfg(cfg: dict) -> list[float]:
    ml = cfg.get("ml", {})
    g = list(ml.get("threshold_grid", DEFAULT_THRESHOLD_GRID))
    if "prob_threshold" in ml and float(ml["prob_threshold"]) not in g:
        g.append(float(ml["prob_threshold"]))
    return sorted(float(x) for x in g)


# ----------------------------------------------------------------------------- cible
def make_labels(trades: dict[str, np.ndarray], bars_ts: np.ndarray, spec: LabelSpec,
                latency_ms: int, max_fill_wait_ms: int = 5000,
                rows: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Renvoie (y_long, y_short, label_end_ts) alignés sur bars_ts.

    y = NaN si aucune entrée n'est possible (pas de trade dans max_fill_wait_ms après
    l'arrivée, ou fin des données avant la fin de l'horizon). `label_end_ts` = dernier instant
    dont l'information est utilisée (sert à la purge). `rows` : indices à calculer (défaut : tous).
    """
    ts, px = trades["ts"], trades["px"]
    n = ts.size
    m = bars_ts.size
    yl = np.full(m, np.nan, np.float32)
    ys = np.full(m, np.nan, np.float32)
    arrival = bars_ts.astype(np.int64) + int(latency_ms)
    end_ts = arrival + int(spec.horizon_s * 1000)
    rows = np.arange(m) if rows is None else np.asarray(rows)
    if n == 0 or rows.size == 0:
        return yl, ys, end_ts
    i0 = np.searchsorted(ts, arrival[rows], side="right")
    up_f, dn_f = 1 + spec.tp_bps * 1e-4, 1 - spec.stop_bps * 1e-4      # long
    dn_t, up_s = 1 - spec.tp_bps * 1e-4, 1 + spec.stop_bps * 1e-4      # short
    H = int(spec.horizon_s * 1000)
    last_ts = ts[-1]
    for r, i in zip(rows.tolist(), i0.tolist()):
        if i >= n or ts[i] - arrival[r] > max_fill_wait_ms:
            continue
        t_entry = ts[i]
        if t_entry + H > last_ts:          # horizon non couvert par les données : inconnu
            continue
        j = int(np.searchsorted(ts, t_entry + H, side="right"))
        p = px[i]
        seg = px[i + 1:j]                  # strictement après le trade d'entrée
        if seg.size == 0:
            yl[r] = 0.0
            ys[r] = 0.0
            continue
        # long : TP traversé strictement (fill limite), stop touché (≤)
        a = np.flatnonzero(seg > p * up_f)
        b = np.flatnonzero(seg <= p * dn_f)
        yl[r] = 1.0 if a.size and (b.size == 0 or a[0] < b[0]) else 0.0
        a = np.flatnonzero(seg < p * dn_t)
        b = np.flatnonzero(seg >= p * up_s)
        ys[r] = 1.0 if a.size and (b.size == 0 or a[0] < b[0]) else 0.0
        end_ts[r] = t_entry + H
    return yl, ys, end_ts


# ----------------------------------------------------------------------------- features
def feature_columns(df: pd.DataFrame) -> list[str]:
    """Liste des features du modèle : FEATURE_COLS_ML (calculables à l'identique en live)."""
    from . import features
    cols = list(features.feature_cols(df))
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise KeyError(f"features absentes du DataFrame : {missing}")
    bad = [c for c in cols if c.startswith(FORBIDDEN_PREFIXES)]
    if bad:
        raise ValueError(f"colonnes interdites comme features (cible / futur) : {bad}")
    return cols


def feature_matrix(df: pd.DataFrame, cols: list[str]) -> np.ndarray:
    X = np.empty((len(df), len(cols)), np.float32)
    for j, c in enumerate(cols):
        X[:, j] = df[c].to_numpy(np.float32) if c in df.columns else np.nan
    X[~np.isfinite(X)] = np.nan
    return X


# ----------------------------------------------------------------------------- coûts
def cost_bps(ep, price: np.ndarray | float) -> tuple[np.ndarray, np.ndarray]:
    """Coûts estimés (bps du notionnel) d'un trade gagnant (TP) et perdant (stop).

    Entrée taker : frais taker + slippage + 1 tick de spread (pessimiste).
    TP : frais maker si TP limite, sinon taker + slippage. Stop : taker + slippage.
    Funding ignoré à l'horizon ≤ 15 min (mesuré a posteriori dans le backtest).
    """
    tick_bps = ep.tick / np.asarray(price, float) * 1e4
    entry = ep.taker_fee_bps + ep.slip_bps + tick_bps
    tp_leg = ep.maker_fee_bps if ep.tp_as_maker else ep.taker_fee_bps + ep.slip_bps
    stop_leg = ep.taker_fee_bps + ep.slip_bps
    return entry + tp_leg, entry + stop_leg


def expected_gain_bps(p: np.ndarray, spec: LabelSpec, c_win, c_loss) -> np.ndarray:
    return p * (spec.tp_bps - c_win) - (1 - p) * (spec.stop_bps + c_loss)


# ----------------------------------------------------------------------------- modèle
class TradeModel:
    """Deux HistGradientBoostingClassifier (long / short), hyperparamètres fixés."""

    def __init__(self, spec: LabelSpec, features: list[str], threshold: float = 0.6,
                 seed: int = DEFAULT_SEED, hyperparams: dict | None = None):
        from .features import FEATURE_COLS_ML
        not_live = [c for c in features if c not in FEATURE_COLS_ML]
        if not_live:
            raise ValueError(f"facteurs non calculés par le robot live (interdits au modèle) : {not_live}")
        self.spec = spec
        self.features = list(features)
        self.threshold = float(threshold)
        self.seed = int(seed)
        self.hyperparams = dict(HYPERPARAMS if hyperparams is None else hyperparams)
        self.models: dict[str, object] = {}
        self.meta: dict = {}
        self.train_info: dict = {}

    def _new(self):
        from sklearn.ensemble import HistGradientBoostingClassifier
        return HistGradientBoostingClassifier(random_state=self.seed, **self.hyperparams)

    def fit(self, X: np.ndarray, y_long: np.ndarray, y_short: np.ndarray) -> "TradeModel":
        for side, y in (("long", y_long), ("short", y_short)):
            ok = np.isfinite(y)
            yy = y[ok].astype(np.int8)
            if yy.size == 0 or yy.min() == yy.max():
                self.models[side] = float(yy.mean()) if yy.size else 0.0   # constante (dégénéré)
            else:
                self.models[side] = self._new().fit(X[ok], yy)
            self.train_info[side] = {"n": int(yy.size), "pos_rate": float(yy.mean()) if yy.size else float("nan")}
        return self

    def _proba(self, side: str, X: np.ndarray) -> np.ndarray:
        m = self.models[side]
        if isinstance(m, float):
            return np.full(X.shape[0], m)
        return m.predict_proba(X)[:, 1]

    def predict_proba(self, df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        X = feature_matrix(df, self.features)
        return self._proba("long", X), self._proba("short", X)

    def decide(self, df: pd.DataFrame, ep, threshold: float | None = None,
               proba: tuple[np.ndarray, np.ndarray] | None = None) -> np.ndarray:
        """Vecteur d'entrées int8 (+1 / −1 / 0) aligné sur df."""
        thr = self.threshold if threshold is None else float(threshold)
        pl, ps = self.predict_proba(df) if proba is None else proba
        cw, cl = cost_bps(ep, df["close"].to_numpy(float))
        el = expected_gain_bps(pl, self.spec, cw, cl)
        es = expected_gain_bps(ps, self.spec, cw, cl)
        ok_l = (pl >= thr) & (el > 0)
        ok_s = (ps >= thr) & (es > 0)
        out = np.where(ok_l & (~ok_s | (el >= es)), 1, np.where(ok_s, -1, 0)).astype(np.int8)
        if "n" in df.columns:          # données périmées : aucun trade dans les 30 dernières s
            stale = df["n"].rolling(6, min_periods=1).sum().to_numpy() == 0
            out[stale] = 0
        out[~np.isfinite(df["close"].to_numpy(float))] = 0
        return out

    # ------------------------------------------------------------------ persistance
    def save(self, path: str | Path, config_hash: str, train_start: str, train_end: str,
             extra: dict | None = None) -> dict:
        import sklearn

        from . import __version__
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.meta = {
            "format_version": MODEL_FORMAT_VERSION, "scalp_version": __version__,
            "sklearn_version": sklearn.__version__, "config_hash": config_hash,
            "features": self.features, "label_spec": asdict(self.spec), "threshold": self.threshold,
            "seed": self.seed, "hyperparams": self.hyperparams, "train_start": train_start,
            "train_end": train_end, "train_info": self.train_info,
            "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            **(extra or {}),
        }
        with open(path, "wb") as f:
            pickle.dump({"meta": self.meta, "models": self.models}, f, protocol=pickle.HIGHEST_PROTOCOL)
        self.meta["sha256"] = file_sha256(path)
        path.with_suffix(".json").write_text(json.dumps(self.meta, indent=2, ensure_ascii=False), encoding="utf-8")
        return self.meta

    @classmethod
    def load(cls, path: str | Path, expected_config_hash: str | None = None) -> "TradeModel":
        """Charge un modèle sauvegardé (fichier de confiance uniquement : pickle)."""
        with open(path, "rb") as f:
            blob = pickle.load(f)
        meta = blob["meta"]
        if meta.get("format_version") != MODEL_FORMAT_VERSION:
            raise ValueError(f"format de modèle {meta.get('format_version')} ≠ {MODEL_FORMAT_VERSION}")
        if expected_config_hash is not None and meta["config_hash"] != expected_config_hash:
            raise ValueError(f"modèle entraîné avec config {meta['config_hash']}, "
                             f"config courante {expected_config_hash}")
        m = cls(LabelSpec(**meta["label_spec"]), meta["features"], meta["threshold"], meta["seed"],
                meta["hyperparams"])
        m.models, m.meta, m.train_info = blob["models"], meta, meta.get("train_info", {})
        return m


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def auc(y: np.ndarray, p: np.ndarray) -> float:
    ok = np.isfinite(y) & np.isfinite(p)
    y, p = y[ok], p[ok]
    if y.size == 0 or y.min() == y.max():
        return float("nan")
    from sklearn.metrics import roc_auc_score
    return float(roc_auc_score(y, p))
