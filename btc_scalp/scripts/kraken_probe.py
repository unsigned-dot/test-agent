"""Sonde Kraken : mesure ce que les API PUBLIQUES de Kraken Futures permettent réellement.

Problème résolu : décider sur une MESURE (et non sur une supposition) si la recherche peut se
faire entièrement sur des données Kraken. Le script ne télécharge pas l'historique : il mesure
  1. l'accès réseau (Kraken prod, Kraken démo, Binance data) depuis la machine qui l'exécute ;
  2. les règles de l'instrument PF_XBTUSD (tick, lot) et le décalage d'horloge ;
  3. la profondeur de l'historique de trades : endpoint /history (doc : 7 jours max) et
     historique des exécutions publiques (/api/history/v3/market/PF_XBTUSD/executions),
     sondé à 1 j, 7 j, 30 j, 90 j, 180 j, 365 j, 540 j et 730 j en arrière ;
  4. le débit de pagination (trades par requête, latence, refus 429) et une extrapolation
     du temps nécessaire pour télécharger 24 mois ;
  5. (option) le flux WebSocket public : échantillons bruts de chaque type de message, pour
     valider les points « démo Kraken » de la section 0 (side, unité de qty, seq).

Les paramètres exacts de l'endpoint d'exécutions n'ont PAS pu être vérifiés (doc inaccessible
depuis l'environnement de développement) : le script consigne les réponses brutes (tronquées)
pour qu'on adapte le parseur si besoin. Aucune clé API, aucun ordre, uniquement des GET publics,
séquentiels et espacés.

Usage :  python scripts/kraken_probe.py [--ws-seconds 30] [--pages 30] [--out runs]
Sortie : un rapport JSON (runs/kraken_probe_<date>.json) + un résumé à l'écran.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PID = "PF_XBTUSD"
PROD = "https://futures.kraken.com"
DEMO = "https://demo-futures.kraken.com"
EXEC_PATH = f"/api/history/v3/market/{PID}/executions"
BINANCE_PROBE = ("https://data.binance.vision/data/futures/um/daily/aggTrades/BTCUSDT/"
                 "BTCUSDT-aggTrades-{day}.zip.CHECKSUM")
DEPTHS_DAYS = (1, 7, 30, 90, 180, 365, 540, 730)
PAUSE_S = 0.35            # entre deux requêtes : sonde polie, jamais en rafale
DAY_MS = 86_400_000


def http_get(url: str, timeout: float = 15.0) -> dict:
    """GET public. Renvoie {status, ms, body (json ou texte tronqué), error}. Ne lève jamais."""
    req = urllib.request.Request(url, headers={"User-Agent": "btc-scalp-probe/0.1"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read() or b"", e.code
    except Exception as e:  # noqa: BLE001 — réseau coupé, DNS, TLS, proxy...
        return {"status": None, "ms": round((time.perf_counter() - t0) * 1000), "body": None,
                "error": f"{type(e).__name__}: {e}"}
    ms = round((time.perf_counter() - t0) * 1000)
    text = raw.decode("utf-8", "replace")
    try:
        body = json.loads(text)
    except ValueError:
        body = text[:500]
    return {"status": status, "ms": ms, "body": body, "error": None}


def _trunc(obj, n: int = 1500) -> str:
    s = json.dumps(obj, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + "…"


# ------------------------------------------------------------------ exécutions publiques
def _find_exec(obj) -> dict | None:
    """Premier dict contenant price et quantity (forme de réponse non vérifiée : recherche tolérante)."""
    if isinstance(obj, dict):
        if "price" in obj and ("quantity" in obj or "qty" in obj):
            return obj
        for v in obj.values():
            r = _find_exec(v)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_exec(v)
            if r is not None:
                return r
    return None


def _ts(x) -> int | None:
    if isinstance(x, (int, float)):
        return int(x)
    if isinstance(x, str):
        if x.isdigit():
            return int(x)
        try:
            return int(datetime.fromisoformat(x.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return None
    return None


def parse_executions(body) -> dict:
    """{n, ts: [..], token, sample} depuis une réponse d'exécutions (forme tolérante)."""
    if not isinstance(body, dict):
        return {"n": 0, "ts": [], "token": None, "sample": None}
    elems = body.get("elements")
    if elems is None:
        elems = next((v for v in body.values() if isinstance(v, list)), [])
    ts = []
    for e in elems:
        ex = _find_exec(e) or {}
        t = _ts(e.get("timestamp") if isinstance(e, dict) else None) or _ts(ex.get("timestamp"))
        if t is not None:
            ts.append(t)
    token = body.get("continuationToken") or body.get("continuation_token")
    return {"n": len(elems), "ts": ts, "token": token,
            "sample": _find_exec(elems[0]) if elems else None}


def fmt(ms: int | None) -> str:
    return "—" if ms is None else datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def probe_depth(get, now_ms: int, sleep=time.sleep) -> list[dict]:
    """Pour chaque profondeur : demande la fenêtre [T, T+1 h] et vérifie que les trades y tombent."""
    out = []
    for d in DEPTHS_DAYS:
        t = now_ms - d * DAY_MS
        q = urllib.parse.urlencode({"since": t, "before": t + 3_600_000, "sort": "asc"})
        r = get(f"{PROD}{EXEC_PATH}?{q}")
        p = parse_executions(r["body"])
        in_win = sum(t <= x < t + 3_600_000 for x in p["ts"])
        out.append({"days_back": d, "status": r["status"], "error": r["error"], "n": p["n"],
                    "n_in_window": in_win, "first": fmt(min(p["ts"])) if p["ts"] else None,
                    "last": fmt(max(p["ts"])) if p["ts"] else None, "has_token": bool(p["token"]),
                    "raw": None if in_win else _trunc(r["body"], 600)})
        sleep(PAUSE_S)
    return out


def probe_rate(get, now_ms: int, pages: int, sleep=time.sleep) -> dict:
    """Pagination continue depuis il y a 1 jour : trades/requête, latence, refus, extrapolation 24 mois."""
    start = now_ms - DAY_MS
    params = {"since": start, "sort": "asc"}
    n_tot, lat, codes, ts_all, token = 0, [], {}, [], None
    for _ in range(pages):
        q = dict(params)
        if token:
            q["continuation_token"] = token
        r = get(f"{PROD}{EXEC_PATH}?{urllib.parse.urlencode(q)}")
        codes[str(r["status"])] = codes.get(str(r["status"]), 0) + 1
        lat.append(r["ms"])
        if r["status"] != 200:
            if r["status"] == 429:
                sleep(5)
                continue
            break
        p = parse_executions(r["body"])
        n_tot += p["n"]
        ts_all += p["ts"]
        token = p["token"]
        if not token or not p["n"]:
            break
        sleep(PAUSE_S)
    res = {"requests": len(lat), "status_codes": codes, "trades": n_tot,
           "median_latency_ms": sorted(lat)[len(lat) // 2] if lat else None,
           "pagination_works": token is not None and n_tot > 0}
    if len(ts_all) >= 2 and n_tot:
        span_ms = max(ts_all) - min(ts_all)
        per_req = n_tot / max(1, len(lat))
        res.update(per_request=round(per_req, 1), span_covered=f"{span_ms / 60000:.1f} min",
                   from_=fmt(min(ts_all)), to=fmt(max(ts_all)))
        if span_ms > 0:
            per_day = n_tot / (span_ms / DAY_MS)
            req_24m = per_day * 730 / per_req
            s_per_req = (res["median_latency_ms"] or 0) / 1000 + PAUSE_S
            res.update(trades_per_day_est=round(per_day), requests_for_24_months=round(req_24m),
                       hours_for_24_months_sequential=round(req_24m * s_per_req / 3600, 1),
                       note="extrapolation grossière sur un échantillon court (activité variable selon l'heure)")
    return res


# ------------------------------------------------------------------ WebSocket
async def _ws_sample(seconds: float) -> dict:
    from bot.feed import kraken_messages
    stop = asyncio.Event()
    samples: dict[str, list] = {}
    counts: dict[str, int] = {}
    seqs: list[int] = []

    async def run():
        async for _recv, msg in kraken_messages("wss://futures.kraken.com/ws/v1", PID, stop=stop):
            kind = msg.get("feed") or msg.get("event") or msg.get("_feed") or "?"
            counts[kind] = counts.get(kind, 0) + 1
            if len(samples.setdefault(kind, [])) < 3:
                samples[kind].append(_trunc(msg, 800))
            if msg.get("feed") == "trade" and isinstance(msg.get("seq"), int):
                seqs.append(msg["seq"])

    task = asyncio.create_task(run())
    await asyncio.sleep(seconds)
    stop.set()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    gaps = sum(1 for a, b in zip(seqs, seqs[1:]) if b != a + 1)
    return {"seconds": seconds, "counts": counts, "samples": samples,
            "trade_seq_checked": len(seqs), "trade_seq_gaps": gaps}


def probe_ws(seconds: float) -> dict:
    try:
        import websockets  # noqa: F401
    except ImportError:
        return {"skipped": "bibliothèque websockets absente (pip install -r bot/requirements.txt)"}
    try:
        return asyncio.run(_ws_sample(seconds))
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


# ------------------------------------------------------------------ verdict
def verdict(rep: dict) -> list[str]:
    v = []
    acc = rep["access"]
    for k, lbl in (("kraken_prod", "Kraken (production)"), ("kraken_demo", "Kraken démo"),
                   ("binance_data", "Binance data.binance.vision")):
        a = acc.get(k, {})
        v.append(f"{lbl} : {'accessible' if a.get('status') == 200 else 'INACCESSIBLE'} "
                 f"(HTTP {a.get('status')}{', ' + a['error'] if a.get('error') else ''})")
    deep = [d["days_back"] for d in rep.get("exec_depth", []) if d["n_in_window"] > 0]
    if deep:
        v.append(f"Historique d'exécutions Kraken : trades trouvés jusqu'à {max(deep)} jours en arrière "
                 f"(profondeurs OK : {deep}).")
        if max(deep) >= 365:
            v.append("=> Au moins 12 mois disponibles : la recherche 100 % Kraken est envisageable "
                     "(téléchargeur à écrire).")
        else:
            v.append("=> Moins de 12 mois : garder Binance pour la recherche, Kraken pour la confirmation "
                     "et l'enregistrement en continu.")
    else:
        v.append("Historique d'exécutions Kraken : aucune fenêtre confirmée (paramètres ou accès à revoir : "
                 "voir les réponses brutes dans le rapport).")
    rt = rep.get("exec_rate", {})
    if rt.get("hours_for_24_months_sequential") is not None:
        v.append(f"Débit : ~{rt['per_request']} trades/requête, ~{rt['trades_per_day_est']:,} trades/jour estimés, "
                 f"~{rt['requests_for_24_months']:,} requêtes et ~{rt['hours_for_24_months_sequential']} h pour 24 mois "
                 "(estimation grossière).".replace(",", " "))
    return v


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Sonde des API publiques Kraken Futures (aucune clé, aucun ordre)")
    ap.add_argument("--pages", type=int, default=30, help="requêtes max pour la mesure de débit")
    ap.add_argument("--ws-seconds", type=float, default=30, help="durée d'écoute WebSocket (0 = pas de test)")
    ap.add_argument("--out", default=str(ROOT / "runs"))
    a = ap.parse_args(argv)
    from bot import kraken

    now = int(time.time() * 1000)
    rep: dict = {"started_utc": fmt(now), "access": {}}
    print("1/5 accès réseau…")
    rep["access"]["kraken_prod"] = http_get(f"{PROD}/derivatives/api/v3/instruments")
    rep["access"]["kraken_demo"] = http_get(f"{DEMO}/derivatives/api/v3/instruments")
    day = datetime.fromtimestamp((now - 3 * DAY_MS) / 1000, timezone.utc).strftime("%Y-%m-%d")
    rep["access"]["binance_data"] = http_get(BINANCE_PROBE.format(day=day))
    print("2/5 instrument et horloge…")
    inst = rep["access"]["kraken_prod"]
    if inst["status"] == 200 and isinstance(inst["body"], dict):
        found = kraken.find_instrument(inst["body"], PID)
        rep["instrument"] = found
        rep["instrument_issues"] = kraken.check_instrument(found, 1.0, 0.0001)
        try:
            body, t0, t1 = kraken.rest_get("instruments", base=f"{PROD}/derivatives/api/v3")
            rep["clock_offset_ms"] = kraken.clock_offset_ms(body, t0, t1)
        except Exception as e:  # noqa: BLE001
            rep["clock_offset_ms"] = f"erreur : {e}"
    for k in rep["access"]:          # le corps complet des instruments est inutile dans le rapport
        rep["access"][k]["body"] = _trunc(rep["access"][k]["body"], 300)
    print("3/5 historique /history (7 jours max selon la doc)…")
    h = http_get(f"{PROD}/derivatives/api/v3/history?symbol={PID}")
    hist = h["body"].get("history", []) if isinstance(h["body"], dict) else []
    hts = [x for x in (_ts(e.get("time")) for e in hist if isinstance(e, dict)) if x]
    rep["history_v3"] = {"status": h["status"], "error": h["error"], "n": len(hist),
                         "oldest": fmt(min(hts)) if hts else None, "newest": fmt(max(hts)) if hts else None,
                         "sample": _trunc(hist[0], 600) if hist else _trunc(h["body"], 600)}
    print("4/5 profondeur et débit de l'historique d'exécutions…")
    rep["exec_depth"] = probe_depth(http_get, now)
    rep["exec_rate"] = probe_rate(http_get, now, a.pages)
    if a.ws_seconds > 0:
        print(f"5/5 flux WebSocket ({a.ws_seconds:g} s)…")
        rep["websocket"] = probe_ws(a.ws_seconds)
    rep["verdict"] = verdict(rep)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"kraken_probe_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps(rep, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("\n=== Verdict (mesuré) ===")
    for line in rep["verdict"]:
        print(" -", line)
    print(f"\nRapport complet : {path}\nEnvoie-moi ce fichier : il contient les réponses brutes utiles pour la suite.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
