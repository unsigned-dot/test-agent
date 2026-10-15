"""Enregistreur des données publiques Kraken (trades, carnet L2, ticker/funding).

Problème résolu : on n'a AUCUN historique Kraken (pas de dump en masse) ; le robot doit
donc enregistrer lui-même, dès le paper trading, tout ce qu'il reçoit pour permettre plus
tard la revalidation du signal et des coûts sur données Kraken (sections 1 et 6).

Format : un fichier par flux et par heure UTC, JSON Lines compressé gzip :
    <data_dir>/kraken/<produit>/<AAAA-MM-JJ>/<flux>_<AAAAMMJJ>T<HH>.jsonl.gz
chaque ligne = {"r": ms_réception_locale, "m": message_brut_kraken}. Le message brut est
conservé tel quel (aucune perte si le parsing évolue). Plusieurs membres gzip dans un même
fichier (après redémarrage, ou à chaque vidage) restent lisibles par gzip.open.
"""
from __future__ import annotations

import gzip
import io
import json
import shutil
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

FEED_FILES = {"trade": "trades", "trade_snapshot": "trades", "book": "book", "book_snapshot": "book",
              "ticker": "ticker", "ticker_lite": "ticker", "heartbeat": "heartbeat"}


def hour_key(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y%m%dT%H")


class Recorder:
    def __init__(self, data_dir: str | Path, product_id: str, flush_interval_s: float = 5.0,
                 level: int = 6):
        self.root = Path(data_dir) / "kraken" / product_id
        self.flush_ms = int(flush_interval_s * 1000)
        self.level = level
        self._buf: dict[tuple[str, str], io.StringIO] = {}
        self._last_flush = 0
        self.lines = 0
        self.bytes_written = 0
        self.errors = 0

    def path_for(self, stream: str, hk: str) -> Path:
        return self.root / f"{hk[:4]}-{hk[4:6]}-{hk[6:8]}" / f"{stream}_{hk}.jsonl.gz"

    def record(self, recv_ms: int, msg: dict) -> None:
        if not isinstance(msg, dict):
            return
        if "event" in msg or "_feed" in msg:          # contrôle (subscribed, error...) / (dé)connexions
            stream = "events"
        else:
            stream = FEED_FILES.get(msg.get("feed"), "other")
        if stream is None:
            return
        key = (stream, hour_key(recv_ms))
        buf = self._buf.get(key)
        if buf is None:
            buf = self._buf[key] = io.StringIO()
        buf.write(json.dumps({"r": recv_ms, "m": msg}, separators=(",", ":")))
        buf.write("\n")
        self.lines += 1
        if recv_ms - self._last_flush >= self.flush_ms:
            self.flush(recv_ms)

    def flush(self, now: int | None = None) -> None:
        """Ajoute chaque tampon comme un membre gzip au fichier horaire (append, crash-safe)."""
        for (stream, hk), buf in list(self._buf.items()):
            data = buf.getvalue()
            if not data:
                continue
            p = self.path_for(stream, hk)
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                comp = gzip.compress(data.encode("utf-8"), compresslevel=self.level)
                with open(p, "ab") as f:
                    f.write(comp)
                    f.flush()
                self.bytes_written += len(comp)
            except OSError:
                self.errors += 1          # disque plein, etc. : compté et remonté par le monitor
                continue
            del self._buf[(stream, hk)]
        self._last_flush = int(now if now is not None else time.time() * 1000)

    def close(self) -> None:
        self.flush()

    def free_disk_gb(self) -> float:
        self.root.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(self.root).free / 1e9


def read_records(path: str | Path) -> Iterator[dict]:
    """Relit un fichier enregistré ; tolère une dernière ligne tronquée (crash pendant l'écriture)."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except (EOFError, zlib.error, gzip.BadGzipFile):
        return


def list_files(data_dir: str | Path, product_id: str, stream: str | None = None) -> list[Path]:
    root = Path(data_dir) / "kraken" / product_id
    pat = f"{stream}_*.jsonl.gz" if stream else "*.jsonl.gz"
    return sorted(root.glob(f"*/{pat}"))
