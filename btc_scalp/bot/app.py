"""Assemblage et boucles d'exécution (asyncio) : direct Kraken ou rejeu d'un faux flux.

Problème résolu : construire les composants à partir de la configuration (une seule
fois, au même endroit), puis faire tourner trois tâches simples : flux de marché ->
moteur, minuterie (barres, péremption, durée max, contrôle d'horloge, vidage disque),
commandes Telegram. Le rejeu utilise exactement le même moteur avec une horloge simulée.
"""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import signal
import time

from . import kraken
from .broker import PaperBroker
from .config import BotConfig, telegram_secrets
from .engine import Engine
from .feed import kraken_messages, now_ms
from .recorder import Recorder
from .state import State
from .telegram import NullNotifier, Telegram


def setup_logging(cfg: BotConfig, to_file: bool = True) -> logging.Logger:
    log = logging.getLogger("bot")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    h = logging.StreamHandler()
    h.setFormatter(fmt)
    log.addHandler(h)
    if to_file:
        d = cfg.storage_path("log_dir")
        d.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.TimedRotatingFileHandler(d / "bot.log", when="midnight", utc=True,
                                                       backupCount=0, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    return log


def build(cfg: BotConfig, notifier=None, log: logging.Logger | None = None, record: bool = True,
          strategy=None) -> tuple[Engine, State, PaperBroker, Recorder | None]:
    state = State(cfg.storage_path("state_db"))
    broker = PaperBroker(cfg.storage_path("paper_db"), cfg.venue, cfg.research["costs"])
    rec = Recorder(cfg.storage_path("data_dir"), cfg.bot["market"]["product_id"],
                   cfg.bot["storage"]["flush_interval_s"]) if record else None
    if notifier is None:
        token, chat = telegram_secrets(cfg)
        if cfg.bot["telegram"].get("enabled", True) and token and chat:
            notifier = Telegram(token, chat)
        else:
            notifier = NullNotifier()
            if log:
                log.warning("Telegram non configuré : alertes dans les logs seulement")

    levels = {"INFO": logging.INFO, "WARN": logging.WARNING, "ERROR": logging.ERROR,
              "CRIT": logging.CRITICAL, "TRADE": logging.INFO}

    def _log(level, msg):
        if log:
            log.log(levels.get(level, logging.INFO), msg)

    eng = Engine(cfg, state, broker, notifier, strategy=strategy, recorder=rec, log=_log)
    return eng, state, broker, rec


def fetch_instruments(rest_url: str):
    """Appel réseau SEUL (exécuté dans un thread) : (body, t0, t1) ou l'exception levée."""
    try:
        return kraken.rest_get("instruments", base=rest_url)
    except Exception as e:  # noqa: BLE001
        return e


async def check_clock(eng: Engine, rest_url: str, fetch=fetch_instruments) -> None:
    """Contrôle horloge locale vs serveur Kraken (serverTime de /instruments) + règles instrument.

    Le réseau passe par un thread ; le moteur (et donc SQLite, qui refuse d'être utilisé depuis
    un autre thread) n'est touché QUE depuis la boucle asyncio. Auparavant tout était fait dans
    le thread : une horloge décalée faisait planter le robot (ProgrammingError) au lieu de le
    mettre en pause, et systemd le relançait en boucle.
    """
    res = await asyncio.to_thread(fetch, rest_url)
    apply_clock_check(eng, res)


def apply_clock_check(eng: Engine, res) -> None:
    if isinstance(res, BaseException) or res is None:
        eng.log("WARN", f"contrôle d'horloge impossible : {res}")
        eng.set_clock_offset(None, now_ms())
        return
    body, t0, t1 = res
    eng.set_clock_offset(kraken.clock_offset_ms(body, t0, t1), t1)
    issues = kraken.check_instrument(kraken.find_instrument(body, eng.pid), eng.ep.tick, eng.ep.lot)
    eng.monitor.set_condition("instrument_rules", bool(issues),
                              "Règles PF_XBTUSD différentes des hypothèses : " + "; ".join(issues), "CRIT")
    if issues:
        eng._pause("instrument_rules", now_ms() + 24 * 3_600_000)


TELEGRAM_LONG_POLL_S = 10      # + 15 s de délai HTTP < TimeoutStopSec=30 : arrêt propre sous systemd
SNAPSHOT_INTERVAL_S = 1.0      # instantané d'état pour le tableau de bord (une écriture SQLite par seconde)


async def run_live(cfg: BotConfig, messages=kraken_messages, fetch=fetch_instruments, notifier=None) -> int:
    """Robot en direct. `messages`, `fetch` et `notifier` sont injectables (tests sans réseau)."""
    log = setup_logging(cfg)
    eng, state, broker, rec = build(cfg, log=log, notifier=notifier)
    if isinstance(eng.notifier, Telegram):
        eng.notifier.deferred = True       # aucun appel HTTP bloquant dans la boucle asyncio
    b = cfg.bot
    stop_ev = asyncio.Event()
    resync = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_ev.set)
        except (NotImplementedError, RuntimeError, ValueError):
            pass       # Windows : Ctrl+C annule la tâche principale (asyncio.run) -> bloc finally ci-dessous
    tasks: list[asyncio.Task] = []
    done: set = set()
    try:
        eng.start(now_ms())
        await check_clock(eng, b["market"]["rest_url"], fetch)
        tasks = [asyncio.create_task(c()) for c in _live_tasks(eng, cfg, rec, stop_ev, resync, messages, fetch)]
        stopper = asyncio.create_task(stop_ev.wait())
        done, _ = await asyncio.wait(tasks + [stopper], return_when=asyncio.FIRST_COMPLETED)
        done.discard(stopper)
        stopper.cancel()
        for t in done:
            if t.exception() is not None:
                log.exception("tâche arrêtée sur erreur", exc_info=t.exception())
                eng._alert("CRIT", "crash", f"Erreur interne : {t.exception()!r} — le service va redémarrer.")
    finally:
        # exécuté aussi sur annulation (Ctrl+C sous Windows) : tampons d'enregistrement vidés
        stop_ev.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if rec is not None:
            rec.close()
        eng._alert("INFO", "shutdown", "Robot arrêté (processus). Les positions paper restent protégées par leur stop.")
        try:
            eng.notifier.flush()             # dernier envoi (bloquant, la boucle s'arrête)
        except Exception:  # noqa: BLE001
            pass
        state.close()
        broker.close()
    return 0 if not any(t.exception() for t in done) else 1


def _live_tasks(eng: Engine, cfg: BotConfig, rec, stop_ev: asyncio.Event, resync: asyncio.Event,
                messages, fetch):
    b = cfg.bot

    async def market():
        fd = b["feed"]
        async for recv, msg in messages(b["market"]["ws_url"], eng.pid, fd["reconnect_min_s"],
                                        fd["reconnect_max_s"], fd["ping_interval_s"],
                                        stop=stop_ev, resync=resync):
            eng.handle(recv, msg)
            if eng.resync_requested:
                eng.resync_requested = False
                resync.set()

    async def timer():
        last_clock = time.monotonic()
        last_disk = 0.0
        last_snap = 0.0
        while not stop_ev.is_set():
            await asyncio.sleep(0.25)
            t = now_ms()
            eng.tick(t)
            if time.monotonic() - last_snap >= SNAPSHOT_INTERVAL_S:
                last_snap = time.monotonic()
                eng.write_snapshot(t)       # lu par le tableau de bord (python -m bot dashboard)
            if rec is not None and t - rec._last_flush >= rec.flush_ms:
                rec.flush(t)
            if time.monotonic() - last_clock > b["feed"]["clock_check_interval_s"]:
                last_clock = time.monotonic()
                await check_clock(eng, b["market"]["rest_url"], fetch)
            if time.monotonic() - last_disk > 300 and rec is not None:
                last_disk = time.monotonic()
                free = rec.free_disk_gb()
                eng.monitor.set_condition("disk_low", free < b["storage"]["min_free_disk_gb"],
                                          f"Espace disque faible : {free:.1f} Go libres.", "CRIT")
                eng.monitor.set_condition("recorder_errors", rec.errors > 0,
                                          f"Erreurs d'écriture de l'enregistreur : {rec.errors}.", "CRIT")
            if t - eng.last_status_ms > b["monitor"]["status_interval_s"] * 1000:
                eng.last_status_ms = t
                eng._alert("INFO", "status", eng.status_text(t))

    async def commands():
        while not stop_ev.is_set():
            texts = await asyncio.to_thread(eng.notifier.poll_commands, TELEGRAM_LONG_POLL_S)
            for text in texts:
                reply = eng.command(text, now_ms())
                eng.notifier.send(reply)
            if not texts:
                await asyncio.sleep(b["telegram"]["poll_interval_s"])

    async def sender():
        """Envoi Telegram hors de la boucle (réseau lent ou coupé : le moteur n'est jamais bloqué)."""
        while not stop_ev.is_set():
            if eng.notifier.pending:
                await asyncio.to_thread(eng.notifier.flush)
            await asyncio.sleep(1.0)

    return market, timer, commands, sender


def replay(eng: Engine, messages: list[tuple[int, dict]], step_ms: int = 250,
           commands: dict[int, str] | None = None, until_ms: int | None = None) -> list[str]:
    """Rejoue des messages (recv_ms, dict) avec une horloge simulée ; ticks réguliers entre messages.

    `commands` : {instant_ms: texte} injectés comme des commandes Telegram. Renvoie les réponses.
    """
    replies: list[str] = []
    cmds = sorted((commands or {}).items())
    if not messages:
        return replies
    t = messages[0][0]
    eng.set_clock_offset(0, t)
    end = until_ms if until_ms is not None else messages[-1][0] + 5000
    i = 0
    while t <= end:
        nxt = t + step_ms
        while i < len(messages) and messages[i][0] < nxt:
            eng.handle(messages[i][0], messages[i][1])
            i += 1
        while cmds and cmds[0][0] < nxt:
            t_cmd, text = cmds.pop(0)
            replies.append(eng.command(text, t_cmd))
        eng.tick(nxt)
        t = nxt
    if eng.recorder is not None:
        eng.recorder.flush(t)
    return replies
