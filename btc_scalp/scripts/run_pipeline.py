"""CLI du pipeline de recherche — étape 1 (un modèle de gradient boosting).

Ordre normal sur données réelles :
    python scripts/run_pipeline.py download
    python scripts/run_pipeline.py validate-data
    python scripts/run_pipeline.py research     --run-id r1
    python scripts/run_pipeline.py walkforward  --run-id r1
    python scripts/run_pipeline.py sensitivity  --run-id r1 --period val
    python scripts/run_pipeline.py final-test   --run-id r1        # UNE fois, verrouillé
    python scripts/run_pipeline.py sensitivity  --run-id r1 --period test
    python scripts/run_pipeline.py report       --run-id r1
Contrôle du code sur données synthétiques :
    python scripts/run_pipeline.py synthetic-demo
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from scalp import validation as V  # noqa: E402
from scalp.config import config_hash, deep_update, load_config  # noqa: E402

log = logging.getLogger("scalp")
DEFAULT_CFG = ROOT / "config" / "default.toml"

# Paramètres réduits pour la démo synthétique (quelques jours de données) — n'affectent pas
# config/default.toml. Écrits dans la copie de configuration du run.
DEMO_OVERRIDES = {
    "walkforward": {"train_days": 4, "test_days": 1, "min_trades_train": 30},
    "ml": {"min_trades_val": 10, "n_random_benchmarks": 10},
    "bootstrap": {"n_resamples": 500},
}


# ----------------------------------------------------------------------------- utilitaires
def _utf8_console():
    """Console Windows (cp1252) : éviter UnicodeEncodeError sur les accents et symboles."""
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def setup_logging(run_dir: Path | None):
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s : %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    if run_dir is not None:
        run_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(run_dir / "run.log", encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)


def toml_dump(d: dict, prefix: str = "") -> str:
    """Sérialiseur TOML minimal (pas de dépendance) pour la copie de configuration du run."""
    def val(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v) if not (isinstance(v, float) and v != v) else "nan"
        if isinstance(v, str):
            return json.dumps(v, ensure_ascii=False)
        if isinstance(v, (list, tuple)):
            return "[" + ", ".join(val(x) for x in v) + "]"
        raise TypeError(type(v))
    lines, subs = [], []
    for k, v in d.items():
        if isinstance(v, dict):
            subs.append((k, v))
        else:
            lines.append(f"{k} = {val(v)}")
    out = ""
    if prefix and lines:
        out += f"[{prefix}]\n"
    out += "\n".join(lines) + ("\n" if lines else "")
    for k, v in subs:
        out += "\n" + toml_dump(v, f"{prefix}.{k}" if prefix else k)
    return out


def open_run(args, create_source: dict | None = None, overrides: dict | None = None) -> V.Run:
    run_dir = Path(args.runs_dir) / args.run_id
    setup_logging(run_dir)
    copy = run_dir / "config.toml"
    if copy.exists() and not args.config:
        cfg = load_config(copy)
        log.info("configuration du run réutilisée : %s", copy)
    else:
        cfg = load_config(args.config or DEFAULT_CFG, overrides)
        if copy.exists():
            old = config_hash(load_config(copy))
            if old != config_hash(cfg):
                log.warning("configuration différente de celle du run (%s ≠ %s)", config_hash(cfg), old)
        else:
            copy.write_text(toml_dump(cfg), encoding="utf-8")
            if load_config(copy) != cfg:
                raise RuntimeError("copie de configuration non fidèle")
    run = V.Run(args.runs_dir, args.run_id, cfg)
    (run.dir / "config_hash.txt").write_text(run.cfg_hash, encoding="utf-8")
    if create_source is not None and not run.has("source.json"):
        run.save("source.json", create_source)
    log.info("run %s — config %s", run.id, run.cfg_hash)
    return run


def source_for(run: V.Run):
    sp = run.source_params()
    t = time.time()
    src = V.make_source(sp, run.cfg)
    log.info("source %s prête (%.1f s)", sp["kind"], time.time() - t)
    return src


def synth_params(args) -> dict | None:
    if not getattr(args, "synthetic", None):
        return None
    return {"kind": "synthetic", "null": args.synthetic == "null", "days": args.days, "rate": args.rate,
            "seed": args.seed}


# ----------------------------------------------------------------------------- commandes
def cmd_download(args):
    from scalp.data.download import download_all
    setup_logging(None)
    cfg = load_config(args.config or DEFAULT_CFG)
    rep = download_all(cfg)
    out = Path(cfg["data"]["raw_dir"]) / "download_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    for k, v in rep.items():
        c = {}
        for s in v.values():
            c[s] = c.get(s, 0) + 1
        log.info("%s : %s", k, c)
    log.info("rapport : %s", out)


def cmd_validate_data(args):
    """Contrôles qualité sur les fichiers BRUTS, jour par jour (mémoire bornée), avec continuité
    des identifiants aggTrade d'un jour au suivant ; écrit au passage le cache compact."""
    from scalp.data import loaders
    from scalp.data.download import daterange
    from scalp.data.validate import validate_trades
    setup_logging(None)
    cfg = load_config(args.config or DEFAULT_CFG)
    d = cfg["data"]
    tick = float(cfg["venues"]["binance"]["tick_size"])
    raw = Path(d["raw_dir"]) / d["market"] / "aggTrades"
    cache = Path(d["cache_dir"]) / d["market"] / "aggTrades"
    out: dict[str, dict] = {}
    prev = None                                   # (dernier aid, dernier ts) du jour précédent
    for day in daterange(d["start"], d["end"]):
        iso, month = day.isoformat(), day.isoformat()[:7]
        m = out.setdefault(month, {"days": {}, "missing_days": [], "cross_day_agg_id_gaps": 0})
        z = raw / f"{d['symbol']}-aggTrades-{iso}.zip"
        if not z.exists():
            m["missing_days"].append(iso)
            prev = None
            continue
        t = loaders.parse_aggtrades(z)
        rep = validate_trades(t, tick=tick)
        if prev is not None and t["aid"].size:
            if int(t["aid"][0]) != prev[0] + 1:
                m["cross_day_agg_id_gaps"] += 1
                rep["ok"] = False
            if int(t["ts"][0]) < prev[1]:
                rep["non_monotonic_across_days"] = True
                rep["ok"] = False
        prev = (int(t["aid"][-1]), int(t["ts"][-1])) if t["aid"].size else None
        m["days"][iso] = rep
        c = cache / f"{iso}.npz"
        if not c.exists():
            loaders.save_cache(c, t)
        del t
    for month, m in out.items():
        m["ok"] = (not m["missing_days"] and m["cross_day_agg_id_gaps"] == 0
                   and all(r.get("ok") for r in m["days"].values()))
        m["n_trades"] = int(sum(r["n_trades"] for r in m["days"].values()))
        log.info("%s : ok=%s, %d trades, jours manquants %d, jours en défaut %d", month, m["ok"], m["n_trades"],
                 len(m["missing_days"]), sum(not r.get("ok") for r in m["days"].values()))
    p = Path(d["cache_dir"]) / "validation_report.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    bad = [m for m, r in out.items() if not r.get("ok")]
    log.info("rapport : %s ; mois en défaut : %s", p, bad or "aucun")


def cmd_research(args):
    sp = synth_params(args) or {"kind": "binance"}
    run = open_run(args, create_source=sp, overrides=DEMO_OVERRIDES if sp["kind"] == "synthetic" else None)
    if sp["kind"] == "binance":
        src = V.BinanceSource(run.cfg)
        run.save("source.json", src.params)
    else:
        src = source_for(run)
    V.run_research(run, src)


def cmd_walkforward(args):
    run = open_run(args)
    V.run_walkforward(run, source_for(run))


def cmd_final_test(args):
    run = open_run(args)
    V.run_final_test(run, source_for(run), force=args.force)


def cmd_sensitivity(args):
    run = open_run(args)
    V.run_sensitivity(run, source_for(run), period=args.period)


def cmd_report(args):
    from scalp.report import write_report
    run = open_run(args)
    p = write_report(run.dir)
    log.info("rapport : %s", p)


def cmd_synthetic_demo(args):
    from scalp.report import write_report
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    results = {}
    for kind in ("null", "signal"):
        ns = argparse.Namespace(runs_dir=args.runs_dir, run_id=f"synth_{kind}_{stamp}", config=args.config)
        sp = {"kind": "synthetic", "null": kind == "null", "days": args.days, "rate": args.rate, "seed": args.seed}
        run = open_run(ns, create_source=sp, overrides=DEMO_OVERRIDES)
        log.info("=== DÉMO SYNTHÉTIQUE (%s) — AUCUNE CONCLUSION SUR BTC ===", kind)
        t0 = time.time()
        src = V.make_source(sp, run.cfg)
        log.info("%d trades synthétiques générés", src.trades["ts"].size)
        sel = V.run_research(run, src)
        wf = V.run_walkforward(run, src)
        ft = V.run_final_test(run, src)
        if not args.quick:
            V.run_sensitivity(run, src, "test")
        m = ft["model"]
        s = m["summary"]
        diag = sel["label_diagnostics"][sel["spec"]]
        res = {
            "run_id": run.id, "seconds": round(time.time() - t0, 1),
            "val_auc_long": diag["val_auc_long"], "val_auc_short": diag["val_auc_short"],
            "test_n_trades": s.get("n_trades"), "test_exp_net_bps": s.get("exp_net_bps"),
            "test_ci95_net": m.get("ci95_exp_net_bps"), "test_exp_gross_bps": s.get("exp_gross_bps"),
            "test_ci95_gross": m.get("ci95_exp_gross_bps"),
            "test_exp_net_bps_random_matched": ft["random_matched"]["exp_net_bps_mean"],
            "walkforward_exp_gross_bps": wf["aggregate"].get("exp_gross_bps"),
            "walkforward_exp_net_bps": wf["aggregate"].get("exp_net_bps"),
            "verdict": ft["criteria"]["verdict"],
        }
        res["test_n_days"] = m.get("n_days")
        res["test_ci_note"] = m.get("ci_note", "")
        lo_net = (m.get("ci95_exp_net_bps") or [None])[0]
        auc_mean = np.nanmean([diag["val_auc_long"] or np.nan, diag["val_auc_short"] or np.nan])
        if kind == "null":
            # attendu : aucun avantage net validé (IC bas ≤ 0, non calculable, ou aucun trade) et NO-GO
            passed = (not ft["criteria"]["verdict"].startswith("GO")) and not (lo_net is not None and lo_net > 0)
            res["check"] = "null → aucun avantage net validé"
        else:
            # attendu : avantage BRUT détecté. Le test de la démo ne compte que quelques jours :
            # l'IC y est volontairement « non calculable » ; on contrôle donc l'AUC sur val et le
            # signe de l'espérance brute sur le test ET sur le walk-forward (contrôle du CODE,
            # pas une preuve statistique).
            g_test = s.get("exp_gross_bps")
            g_wf = wf["aggregate"].get("exp_gross_bps")
            passed = bool(auc_mean > 0.55 and g_test is not None and np.isfinite(g_test) and g_test > 0
                          and g_wf is not None and np.isfinite(g_wf) and g_wf > 0)
            res["check"] = ("signal implanté → avantage brut détecté (AUC val > 0,55, espérance brute > 0 "
                            "sur le test et le walk-forward ; IC non exigé : trop peu de jours)")
        res["passed"] = bool(passed)
        results[kind] = res
        run.save("synthetic_demo.json", V._clean(res))
        write_report(run.dir)
        log.info("contrôle %s : %s — %s", kind, "OK" if passed else "ÉCHEC", res["check"])
    print("\n=== RÉSUMÉ DÉMO — DONNÉES SYNTHÉTIQUES, AUCUNE CONCLUSION SUR BTC ===")
    print(json.dumps(V._clean(results), indent=2, ensure_ascii=False))
    if not all(r["passed"] for r in results.values()):
        sys.exit(1)


# ----------------------------------------------------------------------------- argparse
def main(argv=None):
    p = argparse.ArgumentParser(description="Pipeline de recherche BTC — étape 1")
    p.add_argument("--config", default=None, help=f"fichier TOML (défaut : {DEFAULT_CFG})")
    p.add_argument("--runs-dir", default=str(ROOT / "runs"))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("download", help="télécharge les données publiques Binance (CHECKSUM)").set_defaults(f=cmd_download)
    sub.add_parser("validate-data", help="contrôles qualité mois par mois").set_defaults(f=cmd_validate_data)

    def with_run(name, f, help_):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--run-id", required=True)
        sp.set_defaults(f=f)
        return sp
    r = with_run("research", cmd_research, "entraîne sur dev, choisit cible et seuil sur val")
    r.add_argument("--synthetic", choices=["null", "signal"], help="données synthétiques au lieu de Binance")
    r.add_argument("--days", type=int, default=14)
    r.add_argument("--rate", type=float, default=3.0)
    r.add_argument("--seed", type=int, default=0)
    with_run("walkforward", cmd_walkforward, "walk-forward purgé sur dev+val")
    ft = with_run("final-test", cmd_final_test, "test final verrouillé (une seule configuration)")
    ft.add_argument("--force", action="store_true", help="réévaluer malgré une config différente (journalisé)")
    se = with_run("sensitivity", cmd_sensitivity, "frais, latence d'entrée 250/600/1000 ms, slippage, binance vs kraken")
    se.add_argument("--period", choices=["val", "test"], default="val")
    with_run("report", cmd_report, "écrit runs/<id>/report.md")
    d = sub.add_parser("synthetic-demo", help="contrôles anti-faux-positif / sensibilité sur données synthétiques")
    d.add_argument("--days", type=int, default=14)
    d.add_argument("--rate", type=float, default=3.0)
    d.add_argument("--seed", type=int, default=0)
    d.add_argument("--quick", action="store_true", help="sans les sensibilités")
    d.set_defaults(f=cmd_synthetic_demo)
    args = p.parse_args(argv)
    _utf8_console()
    args.f(args)


if __name__ == "__main__":
    main()
