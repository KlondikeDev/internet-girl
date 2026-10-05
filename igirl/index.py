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

from . import protocol
from .config import INDEX_PORT, atomic_write
from .node import stamp

TTL = 6 * 3600
MAX_ENTRIES = 5000
ANNOUNCE_EVERY = 20       # seconds between announces from one girl
GIRLS_PER_ADDRESS = 32    # one household can't flood the directory with fresh keys


class Index:
    def __init__(self, state: Path, name: str):
        self.state = state
        self.name = name
        try:
            self.girls: dict[str, dict] = json.loads(state.read_text())
        except (OSError, json.JSONDecodeError):
            self.girls = {}
        self.last_announce: dict[str, float] = {}

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
        atomic_write(self.state, json.dumps(self.girls))
        if new:
            self.log(f"👋 {prof.get('name')} joined from {host}:{prof.get('port')}")
        return {"listed": True}

    async def h_peers(self, req, host):
        return {"peers": [{k: g[k] for k in ("pub", "name", "host", "port", "tagline")} for g in self.live()[:200]]}

    async def h_list(self, req, host):
        return {"entries": [{"path": "/index.md", "type": "text/markdown", "size": 0, "updated": int(time.time())}]}

    async def h_fetch(self, req, host):
        def addr(g):
            return f"[{g['host']}]:{g['port']}" if ":" in g["host"] else f"{g['host']}:{g['port']}"
        rows = "\n".join(f"- [**{g['name']}**](gossip://{addr(g)}/) — {g['tagline']}" for g in self.live())
        return {"path": "/index.md", "type": "text/markdown",
                "text": f"# 📇 {self.name}\n\nGirls seen in the last {TTL // 3600} hours:\n\n{rows or '(nobody yet)'}\n"}


async def run(port: int = INDEX_PORT, state: str = "index.json", name: str = "Gossip Index") -> None:
    idx = Index(Path(state), name)
    handlers = {"HELLO": idx.h_hello, "ANNOUNCE": idx.h_announce, "PEERS": idx.h_peers,
                "FRIENDS": idx.h_peers, "LIST": idx.h_list, "FETCH": idx.h_fetch}
    server = await asyncio.start_server(protocol.Server(handlers, idx.log).handle, "0.0.0.0", port)
    idx.log(f"📇 index node on Gossip port {port} ({len(idx.girls)} known)")
    async with server:
        await server.serve_forever()
