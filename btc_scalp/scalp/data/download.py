"""Téléchargement licite des fichiers publics Binance (data.binance.vision).

Problème résolu : obtenir 24 mois d'archives publiques vérifiées (SHA-256) sans jamais
remplacer un fichier absent par une estimation.

- Source officielle : https://data.binance.vision (dépôt binance/binance-public-data).
- Chaque archive est vérifiée contre son fichier .CHECKSUM (SHA-256) publié par Binance.
- Téléchargements séquentiels, avec pause, sans contournement d'aucune protection.
- Un fichier absent (404) est journalisé comme manquant, jamais remplacé par une estimation.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)
BASE = "https://data.binance.vision/data"


def daterange(start: str, end: str):
    d0, d1 = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    d = d0
    while d <= d1:
        yield d
        d += dt.timedelta(days=1)


def months_between(start: str, end: str):
    seen = []
    for d in daterange(start, end):
        m = d.strftime("%Y-%m")
        if m not in seen:
            seen.append(m)
    return seen


def build_urls(symbol: str, dataset: str, start: str, end: str, market: str = "um") -> list[tuple[str, str]]:
    """Renvoie [(url_zip, nom_fichier)]. fundingRate n'existe qu'en mensuel."""
    out = []
    if market == "spot":
        prefix = f"{BASE}/spot"
    else:
        prefix = f"{BASE}/futures/{market}"
    if dataset == "fundingRate":
        for m in months_between(start, end):
            name = f"{symbol}-{dataset}-{m}.zip"
            out.append((f"{prefix}/monthly/{dataset}/{symbol}/{name}", name))
    else:
        for d in daterange(start, end):
            name = f"{symbol}-{dataset}-{d.isoformat()}.zip"
            out.append((f"{prefix}/daily/{dataset}/{symbol}/{name}", name))
    return out


def _fetch(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "btc-scalp-research/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def download_file(url: str, dest: Path, verify: bool = True, pause: float = 0.2) -> str:
    """Retourne 'ok', 'cached', 'missing' ou 'checksum_mismatch'."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return "cached"
    try:
        blob = _fetch(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            log.warning("absent sur le serveur : %s", url)
            return "missing"
        raise
    if verify:
        try:
            chk = _fetch(url + ".CHECKSUM").decode().split()[0].strip()
            if hashlib.sha256(blob).hexdigest() != chk:
                log.error("checksum invalide : %s", url)
                return "checksum_mismatch"
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            log.warning("pas de CHECKSUM publié pour %s (fichier conservé, NON vérifié)", url)
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(blob)
    tmp.replace(dest)                     # replace : écrase aussi sous Windows
    time.sleep(pause)
    return "ok"


def download_all(cfg: dict) -> dict[str, dict[str, str]]:
    d = cfg["data"]
    raw = Path(d["raw_dir"])
    report: dict[str, dict[str, str]] = {}
    jobs = [(ds, d["market"]) for ds in d["datasets"]]
    if d.get("spot_leadlag"):
        jobs.append(("aggTrades", "spot"))
    for ds, market in jobs:
        key = f"{market}/{ds}"
        report[key] = {}
        for url, name in build_urls(d["symbol"], ds, d["start"], d["end"], market):
            status = download_file(url, raw / market / ds / name)
            report[key][name] = status
            log.info("%s %s", status, name)
    return report
