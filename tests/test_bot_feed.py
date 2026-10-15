"""Flux WebSocket : abonnements, reconnexion avec backoff, resynchronisation (faux serveur, sans réseau)."""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

from bot import feed


class FakeWS:
    def __init__(self, frames, fail=False):
        self.frames, self.fail, self.sent = list(frames), fail, []

    async def __aenter__(self):
        if self.fail:
            raise ConnectionRefusedError("refusé")
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, m):
        self.sent.append(json.loads(m))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.frames:
            raise StopAsyncIteration
        await asyncio.sleep(0)
        return self.frames.pop(0)


class TestLiveFeedLoop(unittest.TestCase):
    def test_reconnects_and_subscribes(self):
        conns = [FakeWS([], fail=True),
                 FakeWS(['{"feed":"heartbeat","time":1}', "pas du json", '{"feed":"heartbeat","time":2}']),
                 FakeWS(['{"feed":"heartbeat","time":3}'])]
        made = []

        def connect(url, ping):
            c = conns[len(made)]
            made.append(c)
            return c

        async def run():
            stop = asyncio.Event()
            out = []
            with mock.patch.object(feed.asyncio, "sleep", new=mock.AsyncMock()):
                async for recv, m in feed.kraken_messages("wss://x", "PF_XBTUSD", 1, 8, 20, connect=connect,
                                                          stop=stop):
                    out.append(m)
                    if m.get("time") == 3:
                        stop.set()
            return out

        out = asyncio.run(run())
        kinds = [m.get("_feed") or m.get("feed") for m in out]
        self.assertEqual(kinds, ["disconnected", "connected", "heartbeat", "bad_json", "heartbeat",
                                 "disconnected", "connected", "heartbeat"])
        self.assertEqual({m["feed"] for m in made[1].sent}, {"trade", "book", "ticker", "heartbeat"})
        self.assertTrue(all(m["event"] == "subscribe" for m in made[1].sent))

    def test_backoff_exponential_capped_and_reset(self):
        """Échecs successifs : délais 1, 2, 4, 8, 8 s (x gigue 0,5–1,5) ; plafond reconnect_max_s."""
        n_fail = 6
        conns = [FakeWS([], fail=True) for _ in range(n_fail)] + [FakeWS(['{"feed":"heartbeat","time":3}'])]
        made, delays = [], []

        def connect(url, ping):
            made.append(conns[len(made)])
            return made[-1]

        async def fake_sleep(d):
            delays.append(d)

        async def run():
            stop = asyncio.Event()
            with mock.patch.object(feed.asyncio, "sleep", new=fake_sleep), \
                    mock.patch.object(feed.random, "random", return_value=0.5):      # gigue neutre (x1)
                async for _, m in feed.kraken_messages("wss://x", "PF_XBTUSD", 1.0, 8.0, 20, connect=connect,
                                                       stop=stop):
                    if m.get("time") == 3:
                        stop.set()

        asyncio.run(run())
        self.assertEqual([d for d in delays if d], [1.0, 2.0, 4.0, 8.0, 8.0, 8.0])   # (0 : sleep du faux WS)

    def test_resync_resubscribes_book_without_reconnecting(self):
        """Trou de séquence carnet : désabonnement + réabonnement au book sur la MÊME connexion
        (avant : reconnexion complète => trou dans les trades + pause des entrées de 15 min)."""
        ws = FakeWS(['{"feed":"heartbeat","time":1}', '{"feed":"heartbeat","time":2}',
                     '{"feed":"heartbeat","time":9}'])
        made = []

        def connect(url, ping):
            made.append(ws)
            return ws

        async def run():
            stop, resync = asyncio.Event(), asyncio.Event()
            out = []
            async for _, m in feed.kraken_messages("wss://x", "PF_XBTUSD", connect=connect, stop=stop,
                                                   resync=resync):
                out.append(m)
                if m.get("time") == 1:
                    resync.set()
                if m.get("time") == 9:
                    stop.set()
            return out

        out = asyncio.run(run())
        self.assertEqual(len(made), 1)                                    # aucune reconnexion
        self.assertNotIn("disconnected", [m.get("_feed") for m in out])
        self.assertIn("book_resubscribed", [m.get("_feed") for m in out])
        self.assertEqual([(m["event"], m["feed"]) for m in ws.sent[-2:]],
                         [("unsubscribe", "book"), ("subscribe", "book")])


if __name__ == "__main__":
    unittest.main()
