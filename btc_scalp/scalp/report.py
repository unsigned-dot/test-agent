"""Rapport Markdown (runs/<id>/report.md) et graphiques matplotlib.

Sépare : mesuré (backtest sur données) / estimé (hypothèses de coûts non mesurées) / inconnu.
Le verdict go/no-go reprend les critères chiffrés de la section 7 (profil Kraken, latence d'entrée de base de la config,
grâce de clôture de barre incluse : 600 ms par défaut).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

SYNTH_BANNER = ("> **DONNÉES SYNTHÉTIQUES — AUCUNE CONCLUSION SUR BTC.** Ces chiffres testent uniquement "
                "le code (marche aléatoire nulle ou signal implanté artificiellement).\n")


def _f(x, nd=2):
    if x is None:
        return "n/a"
    try:
        x = float(x)
    except (TypeError, ValueError):
        return str(x)
    if not np.isfinite(x):
        return "n/a" if np.isnan(x) else ("∞" if x > 0 else "−∞")
    if float(x).is_integer() and abs(x) < 1e12:
        return str(int(x))
    return f"{x:.{nd}f}"


def _load(d: Path, name: str):
    p = d / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _table(rows: list[dict], cols: list[tuple[str, str]]) -> str:
    if not rows:
        return "_aucune donnée_\n"
    out = ["| " + " | ".join(h for h, _ in cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        out.append("| " + " | ".join(_f(r.get(k)) if not isinstance(r.get(k), str) else r.get(k)
                                     for _, k in cols) + " |")
    return "\n".join(out) + "\n"


def _breakdown(d: dict | None, title: str) -> str:
    if not d:
        return f"_{title} : aucune donnée_\n"
    rows = [{"k": k, **v} for k, v in d.items()]
    return _table(rows, [(title, "k"), ("trades", "n"), ("esp. nette (bps)", "exp_net_bps"),
                         ("PnL net (USDT)", "net_pnl"), ("taux de réussite", "win_rate")])


def _metrics_block(det: dict, label: str) -> str:
    s = det.get("summary", {})
    if not s or not s.get("n_trades"):
        return f"**{label}** : aucun trade.\n"
    ci = det.get("ci95_exp_net_bps", [None, None])
    cig = det.get("ci95_exp_gross_bps", [None, None])
    rows = [
        ("Trades", s.get("n_trades"), "mesuré"), ("Trades / jour de trading", s.get("trades_per_day"), "mesuré"),
        ("Espérance nette / trade (bps)", s.get("exp_net_bps"), "mesuré (coûts estimés)"),
        ("Jours de trading / jours avec trades", f"{det.get('n_days')} / {det.get('n_days_with_trades')}", "mesuré"),
        ("IC 95 % espérance nette (bootstrap par blocs de jours)",
         f"[{_f(ci[0])} ; {_f(ci[1])}]" if ci[0] is not None else det.get("ci_note") or "non calculable",
         "mesuré"),
        ("p-value (H0 : espérance ≤ 0)", det.get("p_value_exp_net"), "mesuré"),
        ("Espérance brute / trade (bps)", s.get("exp_gross_bps"), "mesuré"),
        ("IC 95 % espérance brute", f"[{_f(cig[0])} ; {_f(cig[1])}]" if cig[0] is not None else "non calculable",
         "mesuré"),
        ("Espérance idéale (clôture→clôture, sans coûts)", s.get("exp_ideal_bps"), "mesuré"),
        ("PnL net (USDT)", s.get("net_pnl"), "mesuré (coûts estimés)"),
        ("PnL brut (USDT)", s.get("gross_pnl"), "mesuré"),
        ("Frais (USDT)", s.get("fees"), "estimé (grille de frais)"),
        ("Funding (USDT)", s.get("funding"), "fichier officiel si présent, sinon estimé"),
        ("Profit factor", s.get("profit_factor"), "mesuré"),
        ("Taux de réussite", s.get("win_rate"), "mesuré"),
        ("Gain moyen (USDT)", s.get("avg_win"), "mesuré"), ("Perte moyenne (USDT)", s.get("avg_loss"), "mesuré"),
        ("Drawdown max réalisé (%)", s.get("max_dd_realized_pct"), "mesuré"),
        ("Drawdown max mark-to-market (%)", det.get("max_dd_mtm_pct"), "mesuré (cours de clôture 1 min)"),
        ("Détention moyenne / médiane (s)", f"{_f(s.get('avg_hold_s'), 0)} / {_f(s.get('median_hold_s'), 0)}", "mesuré"),
        ("Turnover (× capital)", s.get("turnover_x"), "mesuré"),
        ("Long : trades / esp. nette (bps)", f"{s.get('long_n')} / {_f(s.get('long_exp_net_bps'))}", "mesuré"),
        ("Short : trades / esp. nette (bps)", f"{s.get('short_n')} / {_f(s.get('short_exp_net_bps'))}", "mesuré"),
        ("Sharpe journalier annualisé (252 j)", det.get("sharpe_annualized"), "mesuré"),
        ("Deflated Sharpe Ratio", det.get("dsr"), f"mesuré, {det.get('n_trials')} essais comptés"),
        ("Sorties", json.dumps(s.get("exit_reasons", {}), ensure_ascii=False), "mesuré"),
    ]
    out = [f"**{label}**\n", "| Métrique | Valeur | Nature |", "|---|---|---|"]
    out += [f"| {a} | {b if isinstance(b, str) else _f(b, 3 if 'Sharpe' in a or 'p-value' in a else 2)} | {c} |"
            for a, b, c in rows]
    out.append("\nSensibilité au slippage (+x bp par jambe taker, espérance nette bps) : "
               + ", ".join(f"{k} → {_f(v)}" for k, v in det.get("slippage_sensitivity", {}).items()))
    fs = det.get("fee_sensitivity", {})
    out.append("\nSensibilité aux frais (espérance nette bps) : "
               + ", ".join(f"{k} → {_f(v)}" for k, v in fs.items()))
    out.append("\n\nPar heure UTC :\n\n" + _breakdown(det.get("by_hour"), "heure"))
    out.append("\nPar jour de la semaine (UTC ; samedi et dimanche non tradés) :\n\n"
               + _breakdown(det.get("by_dow"), "jour"))
    out.append("\nPar régime de volatilité (terciles de vol 1 h / médiane 24 h, bornes fixées sur dev) :\n\n"
               + _breakdown(det.get("by_regime"), "régime"))
    out.append("\nLong / short :\n\n" + _breakdown(det.get("by_side"), "sens"))
    out.append("\nPar motif de sortie :\n\n" + _breakdown(det.get("by_exit"), "sortie"))
    return "\n".join(out) + "\n"


def write_report(run_dir: str | Path) -> Path:
    d = Path(run_dir)
    src = _load(d, "source.json") or {}
    synth = src.get("kind") == "synthetic"
    cfg_hash = (d / "config_hash.txt").read_text(encoding="utf-8").strip() if (d / "config_hash.txt").exists() else "?"
    L = [f"# Rapport de recherche — run `{d.name}`\n"]
    if synth:
        L.append(SYNTH_BANNER)
        L.append(f"Jeu synthétique : `{json.dumps(src)}`\n")
    L.append(f"- Empreinte de configuration : `{cfg_hash}` (copie : `config.toml`)\n"
             f"- Données : `{json.dumps(src, ensure_ascii=False)}`\n"
             "- Mesuré = calculé sur les données du run ; estimé = hypothèses de coûts non mesurées "
             "(slippage, impact, spread inféré à 1 tick) ; inconnu = non disponible (carnet, file d'attente, "
             "données Kraken).\n")
    split = _load(d, "split.json")
    if split:
        L.append("## Découpage chronologique (jours de trading entiers)\n")
        L.append("| Période | Début | Fin | Jours |\n|---|---|---|---|")
        for k in ("dev", "val", "test"):
            L.append(f"| {k} | {split[k][0]} | {split[k][-1]} | {len(split[k])} |")
        L.append("")
    sel = _load(d, "selection.json")
    if sel:
        L.append("## Modèle et sélection (sur val uniquement)\n")
        L.append(f"- Cible retenue : `{sel['spec']}` (TP / stop en bps, horizon en s) ; seuil de probabilité "
                 f"**{sel['threshold']}** ; purge {sel['purge_ms'] / 1000:.0f} s.\n"
                 f"- Essais comptés pour le DSR : {sel['n_trials_total']} (`{sel['trial_log']}`).\n"
                 f"- Seuil de trades minimal pour la sélection : {sel['min_trades_val']} "
                 f"({'respecté' if sel['selection_min_trades_ok'] else 'NON respecté : sélection fragile'}).\n")
        if sel.get("small_sample_warning"):
            L.append(f"> **AVERTISSEMENT — {sel['small_sample_warning']}.**\n")
        rows = [{"k": k, **v} for k, v in sel.get("label_diagnostics", {}).items()]
        L.append("Diagnostic des cibles (modèle entraîné sur dev purgé, évalué sur val) :\n")
        L.append(_table(rows, [("cible", "k"), ("n entr.", "n_train"), ("purgés", "n_purged"),
                               ("taux pos. long", "pos_rate_long"), ("taux pos. short", "pos_rate_short"),
                               ("AUC val long", "val_auc_long"), ("AUC val short", "val_auc_short")]))
    vm = _load(d, "val_model.json")
    if vm:
        L.append("## Modèle retenu sur val (in-sample pour le choix du seuil — biaisé à la hausse)\n")
        L.append(_metrics_block(vm, "Validation"))
    if (d / "val_results.csv").exists():
        vr = pd.read_csv(d / "val_results.csv")
        top = vr.sort_values("net_pnl", ascending=False)
        L.append("### Toutes les configurations évaluées sur val\n")
        L.append(_table(top.to_dict("records"), [("nom", "name"), ("groupe", "group"), ("trades", "n_trades"),
                                                  ("esp. nette", "exp_net_bps"), ("esp. brute", "exp_gross_bps"),
                                                  ("PnL net", "net_pnl"), ("PF", "profit_factor")]))
    wf = _load(d, "walkforward.json")
    if wf:
        L.append("## Walk-forward purgé (dev + val, test exclu)\n")
        a = wf.get("aggregate", {})
        L.append(f"Cible {wf['spec']}, seuil {wf['threshold']}, purge {wf['purge_ms'] / 1000:.0f} s, "
                 f"{wf['n_folds']} plis, plis positifs : {wf['positive_folds']}. Agrégé : {a.get('n_trades')} trades, "
                 f"espérance nette {_f(a.get('exp_net_bps'))} bps, brute {_f(a.get('exp_gross_bps'))} bps, "
                 f"PnL net {_f(a.get('net_pnl'))} USDT.\n")
        L.append(_table(wf["folds"], [("pli", "fold"), ("test", "test"), ("n entr.", "n_train"),
                                      ("purgés", "n_purged"), ("trades", "n_trades"),
                                      ("esp. nette", "exp_net_bps"), ("esp. brute", "exp_gross_bps"),
                                      ("aléatoire esp. nette", "random_exp_net_bps"),
                                      ("figé esp. nette", "frozen_exp_net_bps")]))
        rv = wf.get("retrain_vs_frozen")
        if rv:
            L.append("### Réentraînement glissant vs modèle figé (condition d'activation de l'étape 2)\n")
            if rv.get("n_folds_compared"):
                L.append(f"Plis comparés : {rv['n_folds_compared']} (le 1er pli est exclu : les deux modèles y "
                         f"sont identiques). PnL net réentraîné {_f(rv['retrained_net_pnl'])} USDT contre "
                         f"figé {_f(rv['frozen_net_pnl'])} USDT ; réentraîné meilleur sur "
                         f"{rv['folds_retrained_better']}/{rv['n_folds_compared']} plis. "
                         f"Espérance nette : réentraîné {_f(rv['retrained'].get('exp_net_bps'))} bps, "
                         f"figé {_f(rv['frozen'].get('exp_net_bps'))} bps.\n")
            L.append(f"**{rv['verdict']}.** Indicatif seulement : la cible et le seuil ont été choisis sur val.\n")
    for per in ("val", "test"):
        se = _load(d, f"sensitivity_{per}.json")
        if se:
            L.append(f"## Sensibilités ({per}) : profil, latence, seuil, types d'ordres\n")
            L.append("Types d'ordres (profil Kraken, mêmes signaux) : `ordres_tout_marche` = entrée et take-profit "
                     "au marché ; `ordres_mixte` = entrée au marché, take-profit limite (défaut) ; "
                     "`ordres_tout_limite` = entrée limite passive (annulée après le délai) et take-profit "
                     "limite. Le stop reste toujours au marché.\n")
            rows = []
            for r in se["rows"]:
                rr = dict(r)
                rr["slip+1"] = (r.get("slippage") or {}).get("+1.0bps")
                rr["slip+5"] = (r.get("slippage") or {}).get("+5.0bps")
                rows.append(rr)
            L.append(_table(rows, [("variante", "name"), ("trades", "n_trades"), ("esp. nette", "exp_net_bps"),
                                   ("esp. brute", "exp_gross_bps"), ("+1 bp", "slip+1"), ("+5 bp", "slip+5"),
                                   ("PnL net", "net_pnl")]))
    ft = _load(d, "final_test.json")
    if ft:
        L.append(f"## TEST FINAL verrouillé ({ft['period'][0]} → {ft['period'][1]})\n")
        lk = ft["lock"]
        forced = [e for e in lk["evaluations"] if e.get("forced")]
        L.append(f"Verrou : empreinte `{lk['fingerprint']}`, créé {lk['created_utc']}, "
                 f"{len(lk['evaluations'])} évaluation(s)"
                 + (f", **dont {len(forced)} FORCÉE(S) avec une autre configuration**" if forced else "") + ".\n")
        if ft.get("prior_test_evaluations_other_config"):
            L.append(f"**ATTENTION : {ft['prior_test_evaluations_other_config']} évaluation(s) antérieure(s) de cette "
                     "période de test avec une autre configuration (registre global) : test NON vierge.**\n")
        L.append("### Modèle vs références vs benchmarks (mêmes jours, même moteur, profil "
                 f"{ft['venue']}, {ft['latency_ms']} ms)\n")
        L.append(_table(ft["table"], [("nom", "name"), ("groupe", "group"), ("trades", "n_trades"),
                                      ("esp. nette (bps)", "exp_net_bps"), ("esp. brute (bps)", "exp_gross_bps"),
                                      ("PnL net (USDT)", "net_pnl"), ("PF", "profit_factor"),
                                      ("DD réalisé %", "max_dd_realized_pct")]))
        rm = ft["random_matched"]
        L.append(f"Entrées aléatoires appariées ({rm['n_seeds']} graines, même nombre de trades et mêmes sorties) : "
                 f"espérance nette moyenne {_f(rm['exp_net_bps_mean'])} bps, 95e centile {_f(rm['exp_net_bps_p95'])} bps.\n")
        L.append("### Métriques détaillées du modèle\n")
        L.append(_metrics_block(ft["model"], f"Profil {ft['venue']}, {ft['latency_ms']} ms"))
        L.append(_metrics_block(ft["model_kraken_base"],
                                f"Profil Kraken, {ft.get('verdict_latency_ms')} ms (base du verdict)"))
        L.append("### Graphiques\n\n![equity](equity_test.png)\n\n![heures](pnl_by_hour_test.png)\n\n"
                 "![régimes](pnl_by_regime_test.png)\n")
        L.append("## Verdict go/no-go (critères §7, fixés avant le test)\n")
        L.append("| # | Critère | Valeur | OK |\n|---|---|---|---|")
        for c in ft["criteria"]["items"]:
            L.append(f"| {c['id']} | {c['critere']} | {c['valeur']} | {'oui' if c['ok'] else 'NON'} |")
        L.append(f"\n**{ft['criteria']['verdict']}**\n")
        if synth:
            L.append("\n" + SYNTH_BANNER)
    else:
        L.append("## Test final\n\nNon évalué (lancer `final-test`). Aucun verdict possible.\n")
    demo = _load(d, "synthetic_demo.json")
    if demo:
        L.append("## Contrôles synthétiques\n")
        L.append("```json\n" + json.dumps(demo, indent=2, ensure_ascii=False) + "\n```\n")
    L.append("## Limites\n\n- Pas de carnet historique : spread et file d'attente inférés (pessimiste).\n"
             "- Slippage et impact Kraken = hypothèses non mesurées.\n"
             "- Critère 7 (signal confirmé sur données Kraken) jamais rempli par ce pipeline : à faire.\n"
             "- Aucune performance passée ne garantit une performance future.\n")
    p = d / "report.md"
    p.write_text("\n".join(L), encoding="utf-8")
    return p


# ----------------------------------------------------------------------------- graphiques
def plot_final(run_dir: str | Path, tr: pd.DataFrame, aux: dict, cfg: dict, regime_q, tag: str = "test"):
    import matplotlib
    matplotlib.use("Agg")
    import logging

    import matplotlib.pyplot as plt
    logging.getLogger("matplotlib").setLevel(logging.WARNING)

    from . import metrics
    from .validation import regime_label
    d = Path(run_dir)
    eq0 = float(cfg["risk"]["initial_equity"])
    fig, ax = plt.subplots(figsize=(10, 4))
    if len(tr):
        t = pd.to_datetime(tr["exit_ts"], unit="ms", utc=True)
        ax.step(t, eq0 + tr["net_pnl"].cumsum(), where="post", label="réalisée (nette)", color="#1f5fa8")
        ax.step(t, eq0 + tr["gross_pnl"].cumsum(), where="post", label="réalisée (brute)", color="#9aa5b1",
                linestyle="--")
        if len(aux.get("mtm_ts", [])):
            mtm = metrics.mtm_equity(tr, aux["mtm_ts"], aux["mtm_close"], eq0, step=1)
            ax.plot(mtm.index, mtm.values, label="mark-to-market", color="#d9822b", lw=0.8)
    ax.axhline(eq0, color="#666", lw=0.6)
    ax.set_title(f"Equity modèle — {tag}")
    ax.set_ylabel("USDT")
    if len(tr):
        ax.legend(loc="best", frameon=False)
    else:
        ax.text(0.5, 0.5, "aucun trade", transform=ax.transAxes, ha="center")
    fig.tight_layout()
    fig.savefig(d / f"equity_{tag}.png", dpi=110)
    plt.close(fig)
    for by, fname, lab in (("hour", f"pnl_by_hour_{tag}.png", "heure UTC"),
                           ("regime", f"pnl_by_regime_{tag}.png", "régime de volatilité")):
        fig, ax = plt.subplots(figsize=(8, 3.5))
        if len(tr):
            x = tr.assign(regime=regime_label(tr["vol_regime"].to_numpy(float), regime_q))
            g = x.groupby(by)["net_pnl"].sum()
            ax.bar(g.index.astype(str), g.values, color=np.where(g.values >= 0, "#1f5fa8", "#c23b22"))
        ax.axhline(0, color="#666", lw=0.6)
        ax.set_title(f"PnL net par {lab} — {tag}")
        ax.set_ylabel("USDT")
        fig.tight_layout()
        fig.savefig(d / fname, dpi=110)
        plt.close(fig)
