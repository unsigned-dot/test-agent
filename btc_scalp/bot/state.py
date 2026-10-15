"""État persistant du robot (SQLite, bibliothèque standard).

Problème résolu : après un crash, une coupure du VPS ou un redémarrage, le robot doit
savoir EXACTEMENT ce qu'il avait décidé, quels ordres il avait l'intention d'envoyer ou
avait envoyés (identifiants client déterministes), quelle position il croit détenir et
où en sont les compteurs de risque (dont le coupe-circuit), pour se rapprocher de la
plateforme sans jamais envoyer un ordre en double.

Règle d'écriture : toute intention d'ordre est COMMITÉE avant l'appel au broker
(write-ahead) ; chaque changement est une transaction courte.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (
    bar_ts INTEGER PRIMARY KEY, decided_ms INTEGER NOT NULL, outcome TEXT NOT NULL,
    reason TEXT, side INTEGER, qty REAL, client_id TEXT, close REAL, extra TEXT);
CREATE TABLE IF NOT EXISTS orders (
    client_id TEXT PRIMARY KEY, base_id TEXT NOT NULL, leg TEXT NOT NULL, side INTEGER NOT NULL,
    type TEXT NOT NULL, qty REAL NOT NULL, price REAL, stop_px REAL, reduce_only INTEGER NOT NULL,
    status TEXT NOT NULL, created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
    fill_px REAL, fill_ms INTEGER);
CREATE TABLE IF NOT EXISTS positions (
    base_id TEXT PRIMARY KEY, side INTEGER NOT NULL, qty REAL NOT NULL, status TEXT NOT NULL,
    signal_ts INTEGER NOT NULL, entry_px REAL, entry_ms INTEGER, stop_px REAL, tp_px REAL,
    deadline_ms INTEGER, exit_px REAL, exit_ms INTEGER, exit_reason TEXT, gross REAL, fees REAL,
    funding REAL, net REAL, dry INTEGER NOT NULL DEFAULT 0, model_entry_px REAL, book_entry_px REAL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL, level TEXT NOT NULL,
    kind TEXT NOT NULL, msg TEXT NOT NULL);
"""

# statuts de position : pending_entry -> open -> closed | abandoned
OPEN_STATUSES = ("pending_entry", "open")


class State:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), isolation_level=None)   # autocommit, transactions explicites
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------- méta
    def get(self, key: str, default=None):
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return default if r is None else json.loads(r["value"])

    def set(self, key: str, value) -> None:
        self.db.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET "
                        "value=excluded.value", (key, json.dumps(value)))

    # -------------------------------------------------------- décisions
    def has_decision(self, bar_ts: int) -> bool:
        return self.db.execute("SELECT 1 FROM decisions WHERE bar_ts=?", (bar_ts,)).fetchone() is not None

    def add_decision(self, bar_ts: int, decided_ms: int, outcome: str, reason: str = "",
                     side: int | None = None, qty: float | None = None, client_id: str | None = None,
                     close: float | None = None, extra: dict | None = None) -> None:
        self.db.execute("INSERT OR IGNORE INTO decisions VALUES (?,?,?,?,?,?,?,?,?)",
                        (bar_ts, decided_ms, outcome, reason, side, qty, client_id, close,
                         json.dumps(extra) if extra else None))

    def decision_counts(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self.db.execute(
            "SELECT outcome, COUNT(*) FROM decisions GROUP BY outcome")}

    # ------------------------------------------------------------ ordres
    def add_order(self, o: dict) -> None:
        cols = ("client_id", "base_id", "leg", "side", "type", "qty", "price", "stop_px",
                "reduce_only", "status", "created_ms", "updated_ms")
        self.db.execute(f"INSERT OR IGNORE INTO orders({','.join(cols)}) VALUES "
                        f"({','.join('?' * len(cols))})", tuple(o.get(c) for c in cols))

    def update_order(self, client_id: str, **kw) -> None:
        if not kw:
            return
        sets = ", ".join(f"{k}=?" for k in kw)
        self.db.execute(f"UPDATE orders SET {sets} WHERE client_id=?", (*kw.values(), client_id))

    def order(self, client_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM orders WHERE client_id=?", (client_id,)).fetchone()
        return dict(r) if r else None

    def orders_for(self, base_id: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM orders WHERE base_id=? ORDER BY created_ms, client_id", (base_id,))]

    def live_orders(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM orders WHERE status IN ('intent','submitted') ORDER BY created_ms")]

    # --------------------------------------------------------- positions
    def add_position(self, p: dict) -> None:
        cols = ("base_id", "side", "qty", "status", "signal_ts", "dry")
        self.db.execute(f"INSERT OR IGNORE INTO positions({','.join(cols)}) VALUES (?,?,?,?,?,?)",
                        tuple(p[c] for c in cols))

    def update_position(self, base_id: str, **kw) -> None:
        sets = ", ".join(f"{k}=?" for k in kw)
        self.db.execute(f"UPDATE positions SET {sets} WHERE base_id=?", (*kw.values(), base_id))

    def position(self, base_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM positions WHERE base_id=?", (base_id,)).fetchone()
        return dict(r) if r else None

    def open_positions(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM positions WHERE status IN ('pending_entry','open') ORDER BY signal_ts")]

    def closed_positions(self, since_ms: int = 0) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM positions WHERE status='closed' AND exit_ms>=? ORDER BY exit_ms", (since_ms,))]

    # ------------------------------------------------------------ journal
    def log_event(self, ts_ms: int, level: str, kind: str, msg: str) -> None:
        self.db.execute("INSERT INTO events(ts_ms, level, kind, msg) VALUES (?,?,?,?)",
                        (ts_ms, level, kind, msg))

    def events(self, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]

    def transaction(self):
        return _Tx(self.db)


class _Tx:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        self.db.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, et, ev, tb):
        self.db.execute("COMMIT" if et is None else "ROLLBACK")
        return False
