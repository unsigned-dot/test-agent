"""Alertes et commandes Telegram (API Bot HTTP, urllib de la bibliothèque standard).

Problème résolu : prévenir Hugo sur son téléphone (alertes, trades, coupe-circuit) et
lui permettre UNIQUEMENT /status et /stop. Les messages venant d'un autre chat que le
chat_id configuré sont ignorés. Le transport HTTP est injectable : les tests utilisent
un faux Telegram, sans réseau.

API utilisée (https://core.telegram.org/bots/api) : sendMessage, getUpdates (long polling
avec `offset` = dernier update_id + 1 pour acquitter).
"""
from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request
from collections import deque

API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4000


def _http_post(url: str, data: dict, timeout: float) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class Telegram:
    def __init__(self, token: str, chat_id: str, transport=None, timeout: float = 15.0):
        self.token = token
        self.chat_id = str(chat_id)
        self.transport = transport or _http_post
        self.timeout = timeout
        self.offset: int | None = None
        self.pending: deque[str] = deque(maxlen=200)    # messages non envoyés (réseau coupé)
        self.failures = 0
        # deferred=True (robot en direct) : send() ne fait que mettre en file ; l'envoi HTTP est
        # fait par flush() depuis un thread, pour ne JAMAIS bloquer la boucle asyncio (jusqu'à
        # 15 s par alerte quand Telegram est injoignable : ticks, péremption et sorties retardés).
        self.deferred = False
        self._lock = threading.Lock()

    def _call(self, method: str, data: dict, timeout: float | None = None) -> dict:
        return self.transport(API.format(token=self.token, method=method), data, timeout or self.timeout)

    def send(self, text: str) -> None:
        """Mise en file ; envoi immédiat sauf en mode différé (retenté au prochain flush/poll)."""
        self.pending.append(text[:MAX_LEN])
        if not self.deferred:
            self.flush()

    def flush(self) -> None:
        if not self._lock.acquire(blocking=False):
            return                        # un autre thread est déjà en train d'envoyer
        try:
            while self.pending:
                try:
                    r = self._call("sendMessage", {"chat_id": self.chat_id, "text": self.pending[0]})
                    if not r.get("ok", False):
                        raise RuntimeError(r.get("description", "réponse non ok"))
                except Exception:  # noqa: BLE001
                    self.failures += 1
                    return
                self.pending.popleft()
        finally:
            self._lock.release()

    def poll_commands(self, long_poll_s: int = 0) -> list[str]:
        """Textes reçus du chat autorisé depuis le dernier appel (acquittés via offset)."""
        data = {"timeout": long_poll_s, "allowed_updates": json.dumps(["message"])}
        if self.offset is not None:
            data["offset"] = self.offset
        try:
            r = self._call("getUpdates", data, timeout=self.timeout + long_poll_s)
        except Exception:  # noqa: BLE001
            self.failures += 1
            return []
        out = []
        for u in r.get("result", []) or []:
            uid = u.get("update_id")
            if isinstance(uid, int):
                self.offset = max(self.offset or 0, uid + 1)
            m = u.get("message") or {}
            if str((m.get("chat") or {}).get("id")) != self.chat_id:
                continue                       # autre interlocuteur : ignoré
            if isinstance(m.get("text"), str):
                out.append(m["text"])
        self.flush()
        return out


class NullNotifier:
    """Telegram désactivé ou non configuré : les alertes vont seulement dans les logs/état."""

    pending: tuple = ()

    def send(self, text: str) -> None:
        pass

    def flush(self) -> None:
        pass

    def poll_commands(self, long_poll_s: int = 0) -> list[str]:
        return []
