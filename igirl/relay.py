"""Relay mode: how a girl behind a home router stays reachable.

She opens ONE outgoing connection to an index and sends a signed RELAY frame. The index
keeps that connection and, from then on, the roles flip on it: the index sends her
{"rid", "req"} frames and she answers {"rid", "resp"}. Anyone can then reach her at
<her pubkey>@index-host:port — requests carrying "relay_to" are passed down her link.

Only read verbs and WHISPER are relayed, so a relay can never be used to write to her site.
"""

from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable

from . import protocol
from .protocol import GossipError, recv_frame, send_frame

RELAYED_VERBS = {"HELLO", "LIST", "FETCH", "PEERS", "FRIENDS", "WHISPER"}
KEEPALIVE = 45            # seconds between relay pings
LINK_SILENCE = 180        # girl side: this long without a frame = dead link
MAX_LINKS = 2000
MAX_PENDING = 32          # concurrent forwarded requests per girl
CALL_TIMEOUT = 20


class Link:
    """The index's end of one girl's relay connection."""

    def __init__(self, pub: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.pub = pub
        self.reader, self.writer = reader, writer
        self.pending: dict[int, asyncio.Future] = {}
        self.next_rid = 1
        self.lock = asyncio.Lock()

    async def call(self, req: dict, timeout: float = CALL_TIMEOUT) -> dict:
        if len(self.pending) >= MAX_PENDING:
            raise GossipError("she's busy — try again in a moment")
        rid, self.next_rid = self.next_rid, self.next_rid + 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        try:
            async with self.lock:
                await send_frame(self.writer, {"rid": rid, "req": req})
            return await asyncio.wait_for(fut, timeout)
        except (asyncio.TimeoutError, ConnectionError, OSError):
            raise GossipError("she didn't answer through the relay") from None
        finally:
            self.pending.pop(rid, None)

    async def run(self) -> None:
        """Read her answers until the link drops."""
        try:
            while True:
                f = await recv_frame(self.reader)
                if f is None:
                    break
                fut = self.pending.get(f.get("rid"))
                resp = f.get("resp")
                if fut and not fut.done():
                    fut.set_result(resp if isinstance(resp, dict) else {"ok": False, "error": "bad relay answer"})
        except (asyncio.IncompleteReadError, ConnectionError, OSError, GossipError,
                json.JSONDecodeError, UnicodeDecodeError):
            pass
        finally:
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError())
            self.close()

    def close(self) -> None:
        self.writer.close()


class Hub:
    """Index-side registry of relay links."""

    def __init__(self, log, on_up: Callable[[dict, str], None], on_seen: Callable[[dict], None],
                 on_down: Callable[[str], None]):
        self.log = log
        self.on_up, self.on_seen, self.on_down = on_up, on_seen, on_down
        self.links: dict[str, Link] = {}

    async def h_relay(self, req: dict, host: str) -> dict:
        prof = await protocol.verify_signed(req, "RELAY", host, callback=False)
        pub = prof["pub"]
        if len(self.links) >= MAX_LINKS and pub not in self.links:
            raise GossipError("this relay is full")

        async def takeover(reader, writer):
            link = Link(pub, reader, writer)
            old = self.links.get(pub)
            if old:
                old.close()
            self.links[pub] = link
            self.on_up(prof, host)
            ka = asyncio.get_running_loop().create_task(self._keepalive(link))
            try:
                await link.run()
            finally:
                ka.cancel()
                if self.links.get(pub) is link:
                    del self.links[pub]
                    self.on_down(pub)

        return {"relaying": True, "_takeover": takeover}

    async def _keepalive(self, link: Link) -> None:
        while True:
            await asyncio.sleep(KEEPALIVE)
            try:
                resp = await link.call({"verb": "HELLO", "_peer": "relay"}, timeout=15)
                if resp.get("ok"):
                    self.on_seen(resp.get("profile", {}))
            except GossipError:
                link.close()
                return

    async def forward(self, req: dict, peer_host: str) -> dict:
        pub = str(req.pop("relay_to", ""))
        link = self.links.get(pub)
        if link is None:
            raise GossipError("she isn't connected to this relay right now")
        verb = str(req.get("verb", "")).upper()
        if verb not in RELAYED_VERBS:
            raise GossipError(f"{verb or 'that'} can't be relayed")
        req["_peer"] = peer_host  # set by us, never trusted from the client
        return await link.call(req)


async def hold_link(addr: str, identity, profile: Callable[[], dict],
                    dispatch: Callable[[dict, str], Awaitable[dict]],
                    on_state: Callable[[str | None], None], log) -> None:
    """Girl side: keep a relay link to `addr` up forever (reconnecting with backoff)."""
    backoff = 5
    host, port = protocol.parse_addr(addr, 7700)
    while True:
        writer = None
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 10)
            writer.write(protocol.MAGIC)
            await send_frame(writer, protocol.signed(identity, profile(), "RELAY"))
            resp = await asyncio.wait_for(recv_frame(reader), 15)
            if not resp or not resp.get("ok"):
                raise GossipError((resp or {}).get("error", "relay hung up"))
            on_state(addr)
            backoff = 5
            lock = asyncio.Lock()

            async def answer(frame: dict) -> None:
                req = frame.get("req") if isinstance(frame.get("req"), dict) else {}
                peer = str(req.pop("_peer", "?"))
                req.pop("relay_to", None)
                out = await dispatch(req, peer)
                async with lock:
                    await send_frame(writer, {"rid": frame.get("rid"), "resp": out})

            while True:
                frame = await asyncio.wait_for(recv_frame(reader), LINK_SILENCE)
                if frame is None:
                    break
                asyncio.get_running_loop().create_task(answer(frame))
            log(f"🛰️ relay {addr} hung up — reconnecting")
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, GossipError,
                json.JSONDecodeError) as e:
            log(f"🛰️ relay {addr}: {e or e.__class__.__name__} — retrying in {backoff}s")
        finally:
            on_state(None)
            if writer:
                writer.close()
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 120)
