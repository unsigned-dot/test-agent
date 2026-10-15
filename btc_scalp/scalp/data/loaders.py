"""Lecture des archives Binance -> tableaux numpy (cache .npz par jour).

Problème résolu : convertir des CSV zippés hétérogènes (avec ou sans en-tête, ms ou µs)
en un format interne unique, horodaté en millisecondes UTC, rapide à recharger.

Format interne des trades (dict de np.ndarray, triés par temps) :
    ts   int64   horodatage ms UTC (transact_time)
    px   float64 prix
    qty  float32 quantité BTC
    bm   bool    is_buyer_maker (True = vendeur agresseur, trade au bid)
    aid  int64   identifiant aggTrade
    fid, lid int64 premier/dernier trade id agrégés
"""
from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

AGG_COLS = ["agg_trade_id", "price", "quantity", "first_trade_id", "last_trade_id",
            "transact_time", "is_buyer_maker"]          # spot : 8e colonne is_best_match (ignorée)
METRICS_COLS = ["create_time", "symbol", "sum_open_interest", "sum_open_interest_value",
                "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
                "count_long_short_ratio", "sum_taker_long_short_vol_ratio"]
FUNDING_COLS = ["calc_time", "funding_interval_hours", "last_funding_rate"]
# colonnes conservées dans le cache .npz (les identifiants ne servent qu'au contrôle qualité,
# fait sur les fichiers bruts par `validate-data`)
CACHE_KEYS = ("ts", "px", "qty", "bm")


def _has_header(first_line: str) -> bool:
    """En-tête si le 1er champ est un identifiant (lettres / _), pas un nombre ni une date."""
    tok = first_line.split(",")[0].strip().strip('"').lstrip("\ufeff")
    return bool(tok) and (tok[0].isalpha() or tok[0] == "_") and tok.replace("_", "").isalnum()


def _read_zip_csv(path: Path, names: list[str] | None = None) -> pd.DataFrame:
    """CSV zippé Binance, avec ou sans en-tête. Sans en-tête, `names` nomme les PREMIÈRES
    colonnes dans l'ordre (les colonnes surnuméraires, ex. is_best_match du spot, sont gardées
    sous leur numéro) : jamais d'index implicite qui décalerait les colonnes."""
    with zipfile.ZipFile(path) as z:
        inner = [n for n in z.namelist() if n.endswith(".csv")][0]
        raw = z.read(inner)
    first = raw[:500].split(b"\n", 1)[0].decode("utf-8", errors="ignore")
    if _has_header(first):
        df = pd.read_csv(io.BytesIO(raw), index_col=False)
        df.columns = [str(c).strip().lower() for c in df.columns]
        return df
    df = pd.read_csv(io.BytesIO(raw), header=None, index_col=False)
    if names:
        if df.shape[1] < len(names):
            raise ValueError(f"{path.name} : {df.shape[1]} colonnes, au moins {len(names)} attendues")
        df.columns = list(names) + [str(i) for i in range(len(names), df.shape[1])]
    return df


def _to_ms(ts: np.ndarray) -> np.ndarray:
    """ms UTC ; convertit les µs (spot depuis 2025) et les ns. Décision par valeur (pas par
    médiane) pour qu'un fichier mixte soit aussi converti correctement."""
    ts = np.asarray(ts).astype(np.int64)
    ts = np.where(ts > 10**17, ts // 1_000_000, np.where(ts > 10**14, ts // 1000, ts))
    return ts.astype(np.int64)


def _bool_col(s: pd.Series) -> np.ndarray:
    if s.dtype == bool:
        return s.to_numpy(bool)
    return s.astype(str).str.strip().str.lower().isin(("true", "1")).to_numpy(bool)


def parse_aggtrades(path: Path) -> dict[str, np.ndarray]:
    df = _read_zip_csv(path, AGG_COLS)
    missing = [c for c in AGG_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"{path.name} : colonnes aggTrades absentes {missing}")
    out = {
        "ts": _to_ms(df["transact_time"].to_numpy()),
        "px": df["price"].to_numpy(np.float64),
        "qty": df["quantity"].to_numpy(np.float32),
        "bm": _bool_col(df["is_buyer_maker"]),
        "aid": df["agg_trade_id"].to_numpy(np.int64),
        "fid": df["first_trade_id"].to_numpy(np.int64),
        "lid": df["last_trade_id"].to_numpy(np.int64),
    }
    order = np.argsort(out["ts"], kind="stable")
    if not np.all(order == np.arange(order.size)):
        log.warning("%s : trades non triés, re-tri appliqué", path.name)
        out = {k: v[order] for k, v in out.items()}
    return out


def compact(t: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Colonnes utiles au backtest seulement, types compacts (≈ 21 octets/trade avant compression)."""
    return {"ts": np.asarray(t["ts"], np.int64), "px": np.asarray(t["px"], np.float64),
            "qty": np.asarray(t["qty"], np.float32), "bm": np.asarray(t["bm"], bool)}


def save_cache(path: Path, t: dict[str, np.ndarray]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".part.npz")
    np.savez_compressed(tmp, **compact(t))
    tmp.replace(path)                     # écriture atomique (pas de cache tronqué après un arrêt)


def load_trades(cfg: dict, market: str | None = None, days: list[str] | None = None) -> dict[str, np.ndarray]:
    """Charge les trades de la période (cache .npz compressé par jour, colonnes CACHE_KEYS).

    Concaténation colonne par colonne : le pic mémoire reste ≈ taille finale + une colonne.
    """
    from .download import daterange
    d = cfg["data"]
    market = market or d["market"]
    raw = Path(d["raw_dir"]) / market / "aggTrades"
    cache = Path(d["cache_dir"]) / market / "aggTrades"
    days = days or [x.isoformat() for x in daterange(d["start"], d["end"])]
    parts = []
    for day in days:
        c = cache / f"{day}.npz"
        z = raw / f"{d['symbol']}-aggTrades-{day}.zip"
        if c.exists():
            with np.load(c, allow_pickle=False) as f:
                parts.append({k: f[k] for k in CACHE_KEYS})
        elif z.exists():
            t = compact(parse_aggtrades(z))
            save_cache(c, t)
            parts.append(t)
        else:
            log.warning("jour manquant (aucune donnée) : %s", day)
    if not parts:
        raise FileNotFoundError(f"Aucun fichier aggTrades trouvé dans {raw}. Lancez d'abord 'download'.")
    out = {}
    for k in CACHE_KEYS:
        out[k] = np.concatenate([p[k] for p in parts])
        for p in parts:
            del p[k]
    if out["ts"].size > 1 and np.any(np.diff(out["ts"]) < 0):
        raise ValueError("trades non monotones entre deux jours : vérifier les fichiers (validate-data)")
    return out


def load_metrics(cfg: dict) -> pd.DataFrame | None:
    """Métriques 5 min (open interest, ratios long/short, taker vol ratio).

    `ts` = create_time en ms UTC. ATTENTION : l'instant de publication n'est pas documenté ;
    features.add_features n'utilise une ligne qu'à partir de ts + [data].metrics_lag_seconds.
    """
    d = cfg["data"]
    raw = Path(d["raw_dir"]) / d["market"] / "metrics"
    files = sorted(raw.glob("*.zip"))
    if not files:
        return None
    df = pd.concat([_read_zip_csv(f, METRICS_COLS) for f in files], ignore_index=True)
    if "create_time" not in df.columns:
        raise ValueError(f"fichiers metrics sans colonne create_time : {list(df.columns)}")
    ct = df["create_time"]
    if pd.api.types.is_numeric_dtype(ct):
        df["ts"] = _to_ms(ct.to_numpy())
    else:
        # pandas >= 2 peut renvoyer une résolution s/ms/us/ns : on force la milliseconde
        df["ts"] = pd.to_datetime(ct, utc=True).astype("datetime64[ms, UTC]").astype("int64")
    df = df.sort_values("ts").drop_duplicates("ts")
    num = [c for c in df.columns if c not in ("create_time", "symbol", "ts")]
    df[num] = df[num].apply(pd.to_numeric, errors="coerce")
    return df.reset_index(drop=True)


def load_funding(cfg: dict) -> pd.DataFrame | None:
    """Funding Binance USDⓈ-M -> DataFrame (ts, rate, interval_h), `ts` = FIN de période.

    Convention Binance : `calc_time` est l'instant de RÈGLEMENT (00h, 08h, 16h UTC, parfois quelques
    ms après l'heure pile) ; `last_funding_rate` est le taux réglé à cet instant pour la période
    (calc_time - interval, calc_time]. C'est déjà la convention interne (fin de période) : aucun
    décalage. Le facteur `last_funding` n'utilise la ligne qu'à partir de calc_time.
    """
    d = cfg["data"]
    raw = Path(d["raw_dir"]) / d["market"] / "fundingRate"
    files = sorted(raw.glob("*.zip"))
    if not files:
        return None
    df = pd.concat([_read_zip_csv(f, FUNDING_COLS) for f in files], ignore_index=True)
    df = df.rename(columns={"calc_time": "ts", "last_funding_rate": "rate",
                            "funding_interval_hours": "interval_h"})
    if "ts" not in df.columns or "rate" not in df.columns:
        raise ValueError(f"fichiers fundingRate inattendus : colonnes {list(df.columns)}")
    df["ts"] = _to_ms(df["ts"].to_numpy())
    df = df.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    if "interval_h" not in df.columns:
        # intervalle inféré de l'écart médian entre deux fundings (8 h sur Binance)
        med = float(np.median(np.diff(df["ts"]))) / 3_600_000 if len(df) > 1 else 8.0
        df["interval_h"] = max(1.0, round(med))
    return df[["ts", "rate", "interval_h"]].astype({"rate": float, "interval_h": float})


def kraken_funding(rates: list[dict], interval_h: float = 1.0) -> pd.DataFrame:
    """Funding historique Kraken Futures (GET /derivatives/api/v4/historicalfundingrates?symbol=PF_XBTUSD,
    liste `rates` de {"timestamp": ISO-8601, "fundingRate", "relativeFundingRate"}) -> DataFrame
    (ts, rate, interval_h) à la convention INTERNE.

    Convention Kraken : `timestamp` = DÉBUT de la période horaire (audit). Le taux couvre donc
    [timestamp, timestamp + 1 h) : on le ramène à la convention interne `ts` = FIN de période
    (timestamp + interval). `rate` = relativeFundingRate (fraction du notionnel par période ;
    fundingRate est en USD par contrat et n'est pas utilisé).
    """
    if not rates:
        return pd.DataFrame({"ts": pd.Series(dtype="int64"), "rate": pd.Series(dtype=float),
                             "interval_h": pd.Series(dtype=float)})
    df = pd.DataFrame(rates)
    start = pd.to_datetime(df["timestamp"], utc=True).astype("datetime64[ms, UTC]").astype("int64")
    out = pd.DataFrame({"ts": start + int(round(interval_h * 3_600_000)),
                        "rate": df["relativeFundingRate"].astype(float), "interval_h": float(interval_h)})
    return out.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)


def load_bookticker(cfg: dict, day: str) -> pd.DataFrame | None:
    d = cfg["data"]
    z = Path(d["raw_dir"]) / d["market"] / "bookTicker" / f"{d['symbol']}-bookTicker-{day}.zip"
    if not z.exists():
        return None
    df = _read_zip_csv(z, ["update_id", "best_bid_price", "best_bid_qty", "best_ask_price",
                           "best_ask_qty", "transaction_time", "event_time"])
    df["ts"] = _to_ms(df["transaction_time"].to_numpy())
    return df.sort_values("ts").reset_index(drop=True)
