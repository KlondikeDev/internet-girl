"""An index node: a Gossip node with no personality that keeps a directory of girls.

    igirl index [--port 7700] [--state index.json]

Girls send a signed ANNOUNCE; the index verifies the signature and calls her back at her
port before listing her. PEERS / FRIENDS return everyone seen in the last few hours.
Humans can read it like any other node: `igirl read index.kunix.org:7700`.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from . import protocol, relay
from .config import INDEX_PORT, atomic_write
from .node import stamp

TTL = 6 * 3600
MAX_ENTRIES = 5000
ANNOUNCE_EVERY = 20       # seconds between announces from one girl
GIRLS_PER_ADDRESS = 32    # one household can't flood the directory with fresh keys


class Index:
    def __init__(self, state: Path, name: str, public_addr: str | None = None):
        self.state = state
        self.name = name
        self.public = protocol.parse_addr(public_addr, INDEX_PORT) if public_addr else None
        try:
            self.girls: dict[str, dict] = json.loads(state.read_text())
        except (OSError, json.JSONDecodeError):
            self.girls = {}
        self.girls = {k: g for k, g in self.girls.items() if not g.get("relay")}  # links died with the old process
        self.last_announce: dict[str, float] = {}
        self.hub = relay.Hub(self.log, self.relay_up, self.relay_seen, self.relay_down)

    def save(self) -> None:
        atomic_write(self.state, json.dumps(self.girls))

    # ---- relayed girls --------------------------------------------------------------------
    def relay_up(self, prof: dict, host: str) -> None:
        self.girls[prof["pub"]] = {"pub": prof["pub"], "name": str(prof.get("name", "?"))[:24], "host": host,
                                   "port": int(prof.get("port") or 0), "tagline": str(prof.get("tagline", ""))[:140],
                                   "seen": time.time(), "relay": True}
        self.save()
        self.log(f"🛰️ {prof.get('name')} connected through the relay (from {host})")

    def relay_seen(self, prof: dict) -> None:
        g = self.girls.get(prof.get("pub"))
        if g:
            g.update(seen=time.time(), name=str(prof.get("name", g["name"]))[:24],
                     tagline=str(prof.get("tagline", g["tagline"]))[:140])

    def relay_down(self, pub: str) -> None:
        g = self.girls.pop(pub, None)
        self.save()
        if g:
            self.log(f"🛰️ {g['name']} left the relay")

    def entry(self, g: dict) -> dict:
        """How others should reach her: directly, or <pub>@this index."""
        out = {k: g[k] for k in ("pub", "name", "tagline")}
        if g.get("relay"):
            host, port = self.public or (None, None)
            out.update(host=host, port=port, relay_to=g["pub"])
        else:
            out.update(host=g["host"], port=g["port"], relay_to=None)
        return out

    def log(self, msg):
        print(f"[{stamp()}] {msg}", flush=True)

    def live(self) -> list[dict]:
        cutoff = time.time() - TTL
        return sorted((g for g in self.girls.values() if g["seen"] > cutoff), key=lambda g: -g["seen"])

    async def h_hello(self, req, host):
        return {"profile": {"kind": "index", "name": self.name, "tagline": f"{len(self.live())} girls online",
                            "proto": "gossip/1"}}

    async def h_announce(self, req, host):
        now = time.time()
        pub = str((req.get("from") or {}).get("pub"))
        key = f"{host}|{pub}"
        if now - self.last_announce.get(key, 0) < ANNOUNCE_EVERY:
            raise protocol.GossipError("announcing too often — once every few minutes is plenty")
        self.last_announce[key] = now
        if len(self.last_announce) > 10000:
            self.last_announce = {k: t for k, t in self.last_announce.items() if now - t < 3600}
        same_ip = [g for g in self.live() if g["host"] == host and g["pub"] != pub]
        if len(same_ip) >= GIRLS_PER_ADDRESS:
            raise protocol.GossipError(f"this address already has {len(same_ip)} girls listed")
        prof = await protocol.verify_signed(req, "ANNOUNCE", host)
        new = prof["pub"] not in self.girls
        self.girls[prof["pub"]] = {"pub": prof["pub"], "name": str(prof.get("name", "?"))[:24],
                                   "host": host, "port": int(prof.get("port", 0)),
                                   "tagline": str(prof.get("tagline", ""))[:140], "seen": time.time()}
        if len(self.girls) > MAX_ENTRIES:
            for g in sorted(self.girls.values(), key=lambda g: g["seen"])[:len(self.girls) - MAX_ENTRIES]:
                del self.girls[g["pub"]]
        self.save()
        if new:
            self.log(f"👋 {prof.get('name')} joined from {host}:{prof.get('port')}")
        return {"listed": True}

    async def h_peers(self, req, host):
        return {"peers": [self.entry(g) for g in self.live()[:200]]}

    async def h_list(self, req, host):
        return {"entries": [{"path": "/index.md", "type": "text/markdown", "size": 0, "updated": int(time.time())}]}

    async def h_fetch(self, req, host):
        rows = []
        for g in self.live():
            e = self.entry(g)
            link = f"gossip://{protocol.join_addr(e['host'], e['port'], e['relay_to'])}/" if e["host"] else None
            rows.append(f"- [**{e['name']}**]({link}) — {e['tagline']}" if link else f"- **{e['name']}** — {e['tagline']}")
        rows = "\n".join(rows)
        return {"path": "/index.md", "type": "text/markdown",
                "text": f"# 📇 {self.name}\n\nGirls seen in the last {TTL // 3600} hours:\n\n{rows or '(nobody yet)'}\n"}


async def run(port: int = INDEX_PORT, state: str = "index.json", name: str = "Gossip Index",
              public_addr: str | None = None) -> None:
    idx = Index(Path(state), name, public_addr)
    handlers = {"HELLO": idx.h_hello, "ANNOUNCE": idx.h_announce, "PEERS": idx.h_peers,
                "FRIENDS": idx.h_peers, "LIST": idx.h_list, "FETCH": idx.h_fetch, "RELAY": idx.hub.h_relay}
    srv = protocol.Server(handlers, idx.log, forward=idx.hub.forward)
    srv.MAX_PER_IP = 64  # a household's relayed girls each hold a connection
    server = await asyncio.start_server(srv.handle, "0.0.0.0", port)
    idx.log(f"📇 index node on Gossip port {port} ({len(idx.girls)} known)")
    async with server:
        await server.serve_forever()
