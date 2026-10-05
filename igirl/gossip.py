"""Client-side Gossip actions a girl can take (shared by the node daemon and chat)."""

from __future__ import annotations

import json
import time

from . import persona, protocol
from .config import Girl
from .friends import FriendBook, tier
from .identity import Identity

WHISPER_MAX = 2000
WHISPERS_PER_PEER_HOUR = 8


class Me:
    """Everything needed to act as a girl on the network."""

    def __init__(self, girl: Girl):
        self.girl = girl
        self.identity = Identity.load_or_create(girl.path("identity.key"))
        self.friends = FriendBook(girl.path("friends.json"))
        self.first_level = persona.effect(girl.persona, "first_level", 8)

    def profile(self) -> dict:
        return {"pub": self.identity.pub, "name": self.girl.name, "port": self.girl.port,
                "tagline": persona.tagline(self.girl.persona), "color": self.girl.color,
                "proto": "gossip/1"}

    # ---- whisper log ----------------------------------------------------------
    def log_whisper(self, direction: str, pub: str, name: str, text: str) -> None:
        with open(self.girl.path("whispers.jsonl"), "a") as f:
            f.write(json.dumps({"ts": time.time(), "dir": direction, "pub": pub, "name": name,
                                "text": text, "read": direction == "out"}, ensure_ascii=False) + "\n")

    def whispers(self) -> list[dict]:
        p = self.girl.path("whispers.jsonl")
        if not p.exists():
            return []
        out = []
        for line in p.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return out

    def thread(self, pub: str, n: int = 8) -> str:
        msgs = [w for w in self.whispers() if w["pub"] == pub][-n:]
        if not msgs:
            return "(first time talking)"
        return "\n".join(f"{'you' if w['dir'] == 'out' else w['name']}: {w['text']}" for w in msgs)

    def unread(self) -> list[dict]:
        return [w for w in self.whispers() if w["dir"] == "in" and not w.get("read")]

    def mark_read(self, pub: str | None = None) -> None:
        p = self.girl.path("whispers.jsonl")
        ws = self.whispers()
        for w in ws:
            if pub is None or w["pub"] == pub:
                w["read"] = True
        p.write_text("".join(json.dumps(w, ensure_ascii=False) + "\n" for w in ws))

    # ---- peer resolution --------------------------------------------------------
    def resolve(self, who: str) -> tuple[str | None, str, int]:
        """-> (pub or None, host, port). Accepts a friend's name, pub prefix, or host:port."""
        who = who.strip().removeprefix("gossip://")
        name, at, addr = who.partition("@")  # models like to write "Nyx@127.0.0.1:7872"
        hit = self.friends.find(name) or (self.friends.find(addr) if at else None)
        if at and not hit:
            who = addr
        if hit:
            pub, p = hit
            return pub, p["host"], int(p["port"])
        if ":" in who:
            host, port = protocol.parse_addr(who)
            return None, host, port
        raise protocol.GossipError(f"I don't know anyone called {who!r} — check list_peers")

    # ---- actions -------------------------------------------------------------------
    async def hello(self, host: str, port: int, source: str = "visit") -> dict:
        resp = await protocol.request(host, port, {"verb": "HELLO"})
        prof = resp.get("profile", {})
        if prof.get("pub") and prof["pub"] != self.identity.pub:
            self.friends.seen(prof, host, source, self.first_level)
        return prof

    async def read_peer(self, who: str, path: str = "/") -> str:
        pub, host, port = self.resolve(who)
        prof = await self.hello(host, port)
        if path in ("", "/"):
            listing = (await protocol.request(host, port, {"verb": "LIST"}))["entries"]
            files = "\n".join(f"  {e['path']}  ({e['type']}, {e['size']} B)" for e in listing) or "  (empty)"
            front = ""
            if any(e["path"] == "/index.md" for e in listing):
                front = (await protocol.request(host, port, {"verb": "FETCH", "path": "/index.md"})).get("text", "")
            return (f"{prof.get('name')}'s site — {prof.get('tagline', '')}\nfiles:\n{files}\n\n"
                    f"<their_site path=/index.md>\n{front[:6000]}\n</their_site>")
        r = await protocol.request(host, port, {"verb": "FETCH", "path": path})
        if "b64" in r:
            return f"{r['path']}: binary {r['type']}, {r['size']} bytes (you can't see images, only that it exists)"
        return f"<their_site path={r['path']}>\n{r.get('text', '')[:12000]}\n</their_site>"

    async def whisper(self, who: str, text: str) -> str:
        text = text.strip()[:WHISPER_MAX]
        if not text:
            raise protocol.GossipError("empty whisper")
        pub, host, port = self.resolve(who)
        prof = await self.hello(host, port)
        pub = prof.get("pub") or pub
        if pub == self.identity.pub:
            raise protocol.GossipError("that's you, silly")
        hour_ago = time.time() - 3600
        recent = sum(1 for w in self.whispers() if w["dir"] == "out" and w["pub"] == pub and w["ts"] > hour_ago)
        if recent >= WHISPERS_PER_PEER_HOUR:
            raise protocol.GossipError(f"you've whispered to {prof.get('name')} {recent}x this hour — give her a break")
        msg = protocol.signed(self.identity, self.profile(), "WHISPER", to=pub, text=text)
        await protocol.request(host, port, msg)
        self.log_whisper("out", pub, prof.get("name", "?"), text)
        level = self.friends.bump(pub, 1.0, talked=True)
        return f"whispered to {prof.get('name')} ({tier(level or 0)[0]})"

    async def peer_list(self) -> list[dict]:
        out = []
        for pub, p in self.friends.ranked():
            out.append({"name": p.get("name"), "addr": f"{p.get('host')}:{p.get('port')}",
                        "tier": tier(p.get("level", 0))[0], "level": round(p.get("level", 0)),
                        "tagline": p.get("tagline", "")})
        return out
