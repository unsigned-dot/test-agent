"""Point d'entrée : `python -m bot <commande>`.

Problème résolu : une seule interface en ligne de commande, identique sous Linux (VPS,
systemd) et Windows (tests), pour démarrer, inspecter, arrêter et — sur le serveur
seulement, jamais par Telegram — reprendre après un /stop.

Commandes :
  run            robot paper sur le flux Kraken réel (nécessite `websockets` et le réseau)
  demo           rejeu d'un faux flux synthétique (aucun réseau) dans un dossier temporaire
  check-config   vérifie la configuration (mode paper, sections interdites, secrets) et sort
  status         affiche l'état depuis la base SQLite (sans démarrer le robot)
  stop           pose l'arrêt persistant (équivalent /stop) ; le robot ferme la position au prochain tick
  emergency-stop arrêt + fermeture IMMÉDIATE de la position paper et annulation des ordres
  resume         lève un /stop (sur le serveur uniquement ; confirmation explicite)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import time
from pathlib import Path

from .config import ConfigError, DEFAULT_BOT_CONFIG, ModeError, load_bot_config, telegram_secrets


def _cfg(args):
    try:
        return load_bot_config(args.config)
    except (ModeError, ConfigError) as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(2) from None


def cmd_run(args) -> int:
    cfg = _cfg(args)
    from .app import run_live
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("La bibliothèque 'websockets' est requise : pip install -r bot/requirements.txt", file=sys.stderr)
        return 3
    try:
        return asyncio.run(run_live(cfg))
    except KeyboardInterrupt:
        return 0


def cmd_demo(args) -> int:
    cfg = _cfg(args)
    from scalp.data.synthetic import make_trades
    from .app import build, replay, setup_logging
    from .feed import fake_messages
    from .recorder import list_files, read_records
    out = Path(args.out or tempfile.mkdtemp(prefix="bot_demo_"))
    cfg.bot["storage"].update({"data_dir": str(out / "data"), "state_db": str(out / "state.sqlite"),
                               "paper_db": str(out / "paper.sqlite"), "log_dir": str(out / "log")})
    cfg.bot["strategy"]["name"] = args.strategy
    cfg.bot["bars"]["warmup_s"] = args.warmup_s
    # démo : une journée synthétique un lundi, au tick Kraken (1 USD)
    trades = make_trades(days=args.minutes / 1440, rate_per_s=3.0, daily_vol=0.04, tick=1.0,
                         p0=100_000.0, seed=args.seed)
    msgs = fake_messages(trades, cfg.bot["market"]["product_id"])
    log = setup_logging(cfg, to_file=True)
    from .telegram import NullNotifier

    class PrintNotifier(NullNotifier):
        def send(self, text):
            print("  [telegram] " + text.replace("\n", " | ")[:300])

    eng, state, broker, rec = build(cfg, notifier=PrintNotifier(), log=log)
    t0 = time.time()
    replies = replay(eng, msgs, commands={msgs[len(msgs) // 2][0]: "/status",
                                          msgs[-1][0] - 60_000: "/stop",
                                          msgs[-1][0] - 30_000: "/set risk 0.05"})
    print(f"\n=== Démo : {args.minutes} min simulées, {len(msgs)} messages, {time.time() - t0:.1f} s réelles ===")
    for r in replies:
        print("réponse commande :", r.replace("\n", "\n    "))
    print("décisions :", state.decision_counts())
    closed = state.closed_positions()
    print(f"positions closes : {len(closed)} ; net cumulé {sum(c['net'] for c in closed):+.2f} USD (dry, sans valeur probante)")
    files = list_files(cfg.storage_path("data_dir"), cfg.bot["market"]["product_id"])
    n = sum(1 for f in files for _ in read_records(f))
    print(f"enregistrement : {len(files)} fichiers, {n} lignes relues")
    print("dossier :", out)
    state.close()
    broker.close()
    return 0


def cmd_check(args) -> int:
    cfg = _cfg(args)
    token, chat = telegram_secrets(cfg)
    print(f"mode={cfg.mode} produit={cfg.bot['market']['product_id']} stratégie={cfg.bot['strategy']['name']}")
    print(f"config recherche : empreinte {cfg.research_hash}")
    print(f"Telegram : {'configuré' if token and chat else 'NON configuré (alertes dans les logs seulement)'}")
    for k in ("data_dir", "state_db", "paper_db", "log_dir"):
        print(f"{k} = {cfg.storage_path(k)}")
    return 0


def _engine_offline(cfg):
    from .app import build
    from .telegram import NullNotifier
    return build(cfg, notifier=NullNotifier(), record=False)


def cmd_status(args) -> int:
    cfg = _cfg(args)
    eng, state, broker, _ = _engine_offline(cfg)
    eng.started_ms = state.get("last_start_ms")
    print(eng.status_text(int(time.time() * 1000)))
    print("\nDerniers événements :")
    for e in state.events(15):
        print(f"  {e['ts_ms']} {e['level']} {e['kind']}: {e['msg'][:160]}")
    return 0


def cmd_stop(args) -> int:
    cfg = _cfg(args)
    from .state import State
    st = State(cfg.storage_path("state_db"))
    st.set("stopped", True)
    st.set("stopped_at_ms", int(time.time() * 1000))
    st.log_event(int(time.time() * 1000), "CRIT", "manual_stop", "arrêt posé depuis le serveur (CLI)")
    print("Arrêt persistant posé. Le robot (s'il tourne) ferme la position au prochain tick.")
    return 0


def cmd_emergency(args) -> int:
    """Arrêt d'urgence hors ligne : à lancer robot ARRÊTÉ (systemctl stop) — voir RUNBOOK."""
    cfg = _cfg(args)
    eng, state, broker, _ = _engine_offline(cfg)
    now = int(time.time() * 1000)
    state.set("stopped", True)
    pos = broker.position()
    for o in broker.open_orders():             # entrées annulées ; stops/TP reduce-only CONSERVÉS
        if not o["reduce_only"]:
            broker.cancel(o["client_id"], now)
    if pos["qty"] != 0:
        # paper : fermeture au dernier prix connu (aucun marché réel à atteindre ici)
        last = broker.db.execute("SELECT fill_px FROM orders WHERE fill_px IS NOT NULL ORDER BY fill_ms DESC "
                                 "LIMIT 1").fetchone()
        print(f"Position paper {pos['qty']} BTC : marquée pour fermeture au marché au redémarrage "
              f"(dernier prix connu {last[0] if last else '?'}).")
    state.log_event(now, "CRIT", "emergency_stop", "arrêt d'urgence CLI : entrées annulées, arrêt posé")
    print("Entrées paper annulées (stops conservés), arrêt persistant posé. Au redémarrage le robot "
          "fermera toute position au marché et n'ouvrira rien.")
    return 0


def cmd_resume(args) -> int:
    cfg = _cfg(args)
    from .state import State
    if args.confirm != "JE_CONFIRME_LA_REPRISE":
        print("Refus : ajouter --confirm JE_CONFIRME_LA_REPRISE (reprise volontaire, sur le serveur).")
        return 2
    st = State(cfg.storage_path("state_db"))
    rs = st.get("risk_state") or {}
    if rs.get("killed"):
        print("Le COUPE-CIRCUIT est actif : /stop levé mais AUCUNE entrée ne reprendra. Sa levée exige une "
              "décision d'Hugo (RiskManager.manual_reset_kill) — voir RUNBOOK.")
    st.set("stopped", False)
    st.set("reconcile_divergence", "")
    st.log_event(int(time.time() * 1000), "WARN", "resume", "reprise manuelle (CLI) après /stop")
    print("Arrêt levé. Redémarrer le service pour reprendre : sudo systemctl restart btc-scalp-bot")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m bot", description="Robot PAPER Kraken PF_XBTUSD")
    p.add_argument("--config", default=str(DEFAULT_BOT_CONFIG))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    d = sub.add_parser("demo")
    d.add_argument("--minutes", type=float, default=240)
    d.add_argument("--strategy", default="dry_signal", choices=["none", "dry_signal"])
    d.add_argument("--warmup-s", type=int, default=3600)
    d.add_argument("--seed", type=int, default=3)
    d.add_argument("--out", default=None)
    sub.add_parser("check-config")
    sub.add_parser("status")
    sub.add_parser("stop")
    sub.add_parser("emergency-stop")
    r = sub.add_parser("resume")
    r.add_argument("--confirm", default="")
    args = p.parse_args(argv)
    return {"run": cmd_run, "demo": cmd_demo, "check-config": cmd_check, "status": cmd_status,
            "stop": cmd_stop, "emergency-stop": cmd_emergency, "resume": cmd_resume}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
