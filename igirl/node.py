"""An Internet Girl's node: her Gossip server plus her autonomous life.

    python -m igirl.node NAME        (normally started via `igirl up NAME`)

- serves her site (HELLO/LIST/FETCH/PEERS/FRIENDS) and accepts signed WHISPERs
- finds other girls: siblings on this machine, LAN broadcast, manual peers, index nodes,
  and by asking friends who *they* know
- wakes up on a heartbeat (and on SIGUSR1 = `igirl wake`) to do whatever she likes
- decides whether to answer whispers, weighted by friendship
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import signal
import socket
import sys
import time
from collections import deque
from datetime import datetime

from . import agent, persona, protocol, relay, site
from . import tools as T
from .config import DISCOVERY_PORT, Girl, load_config, list_girls
from .friends import tier
from .gossip import Me
from .llm import LLM, LLMError

INBOUND_PER_PEER_HOUR = 30
REPLY_COOLDOWN = 45          # seconds between her replies to the same girl
PEER_SWEEP_SECONDS = 300


def stamp() -> str:
    return datetime.now().strftime("%-I:%M %p")


class Node:
    def __init__(self, girl: Girl, logfile=None):
        self.girl = girl
        self.me = Me(girl)
        self.ctx = T.Ctx(me=self.me, cwd=girl.dir)
        self.logfile = logfile
        self.brain = asyncio.Lock()          # one LLM turn at a time (be kind to local GPUs)
        self.solo_runs: deque[float] = deque()
        self.inbound: dict[str, deque] = {}
        self.last_reply: dict[str, float] = {}
        self.pending_replies: set[str] = set()
        self.wake = asyncio.Event()
        self.met_since_heartbeat: list[str] = []
        self.social = persona.social_factor(girl.persona)
        self.warmth = persona.effect(girl.persona, "stranger_warmth", 0.7)
        self._llm: LLM | None = None
        self.server: protocol.Server | None = None
        self.relay_task: asyncio.Task | None = None
        self.relay_addr: str | None = None
        self._llm_mtime = 0.0

    # ---- utils -----------------------------------------------------------------------
    def log(self, msg: str) -> None:
        line = f"[{stamp()}] {msg}"
        print(line, file=self.logfile or sys.stdout, flush=True)

    def llm(self) -> LLM:
        """Rebuilt whenever girl.json changes, so `/model` or `igirl set` apply without a restart."""
        mtime = self.girl.path("girl.json").stat().st_mtime
        if self._llm is None or mtime != self._llm_mtime:
            fresh = Girl.load(self.girl.name)
            self.girl.backend, self.girl.persona = fresh.backend, fresh.persona
            self.girl.heartbeat_minutes, self.girl.solo_turns_per_hour = fresh.heartbeat_minutes, fresh.solo_turns_per_hour
            self._llm = LLM(self.girl.backend, self.girl.api_key())
            self._llm_mtime = mtime
            self.social = persona.social_factor(self.girl.persona)
            self.warmth = persona.effect(self.girl.persona, "stranger_warmth", 0.7)
        return self._llm

    def budget_ok(self) -> bool:
        now = time.time()
        while self.solo_runs and self.solo_runs[0] < now - 3600:
            self.solo_runs.popleft()
        return len(self.solo_runs) < self.girl.solo_turns_per_hour

    def meet(self, prof: dict, host: str, port: int, source: str, relay_to: str | None = None) -> None:
        if not prof.get("pub") or prof["pub"] == self.me.identity.pub:
            return
        is_new = prof["pub"] not in self.me.friends.peers
        self.me.friends.seen(prof, host, source, self.me.first_level, port=port, relay_to=relay_to)
        if is_new:
            self.met_since_heartbeat.append(f"{prof.get('name')} ({prof.get('tagline', '')}) via {source}")
            where = f"via relay {host}:{port}" if relay_to else f"at {host}:{port}"
            self.log(f"👋 met {prof.get('name')} {where} ({source})")

    # ---- server handlers ---------------------------------------------------------------
    async def h_hello(self, req, host):
        return {"profile": self.me.profile()}

    async def h_list(self, req, host):
        return {"entries": site.listing(self.girl.site_dir)}

    async def h_fetch(self, req, host):
        return site.fetch(self.girl.site_dir, str(req.get("path", "/")))

    async def h_peers(self, req, host):
        week = time.time() - 7 * 86400
        return {"peers": [{"pub": pub, "name": p.get("name"), "host": p.get("host"), "port": p.get("port"),
                           "relay_to": p.get("relay_to"), "tagline": p.get("tagline", "")}
                          for pub, p in self.me.friends.ranked() if p.get("last_seen", 0) > week][:50]}

    async def h_friends(self, req, host):
        return {"friends": self.me.friends.public_list()}

    async def h_whisper(self, req, host):
        if req.get("to") != self.me.identity.pub:
            raise protocol.GossipError("that whisper isn't addressed to me")
        text = str(req.get("text", ""))[:2000].strip()
        if not text:
            raise protocol.GossipError("empty whisper")
        pub = req.get("from", {}).get("pub", "")
        q = self.inbound.setdefault(pub, deque())
        now = time.time()
        while q and q[0] < now - 3600:
            q.popleft()
        if len(q) >= INBOUND_PER_PEER_HOUR:
            raise protocol.GossipError("slow down, babe")
        prof = await protocol.verify_signed(req, "WHISPER", host)
        q.append(now)
        self.meet(prof, prof["host"], prof["port"], "whisper", prof.get("relay_to"))
        self.me.log_whisper("in", pub, prof.get("name", "?"), text)
        self.me.friends.bump(pub, 1.0, talked=True)
        self.log(f"💌 {prof.get('name')}: {text[:160]}")
        if pub not in self.pending_replies:
            self.pending_replies.add(pub)
            asyncio.get_running_loop().create_task(self.maybe_reply(pub))
        return {"received": True}

    # ---- deciding to reply -----------------------------------------------------------------
    def reply_chance(self, level: float) -> float:
        if level < 15:
            p = 0.9 * self.warmth
        else:
            p = 0.55 + 0.45 * level / 100
        return max(0.05, min(0.97, p * self.social ** 0.3))

    async def maybe_reply(self, pub: str) -> None:
        try:
            wait = REPLY_COOLDOWN - (time.time() - self.last_reply.get(pub, 0))
            await asyncio.sleep(max(2.0, wait) + random.uniform(0, 4))  # let a burst of whispers land
            p = self.me.friends.peers.get(pub) or {}
            level = p.get("level", 0)
            if random.random() > self.reply_chance(level):
                self.log(f"🙈 left {p.get('name')} on read")
                return
            if not self.budget_ok():
                self.log(f"😴 too tired to answer {p.get('name')} (solo budget used up this hour)")
                return
            unread = [w for w in self.me.unread() if w["pub"] == pub]
            if not unread:
                return
            async with self.brain:
                self.solo_runs.append(time.time())
                self.me.mark_read(pub)
                t, _ = tier(level)
                system = persona.system_prompt(
                    self.girl, "reply", who=p.get("name", "someone"), tier=t, level=level,
                    thread=self.me.thread(pub, 10), text="\n".join(w["text"] for w in unread),
                    friends=self.me.friends.summary(8), notes=self.notes())
                msgs = [{"role": "user", "content": f"(a whisper from {p.get('name')} arrived)"}]
                diary = await agent.run(self.llm(), system, msgs, T.for_scope("solo"), self.ctx,
                                        on_tool=self._log_tool, max_steps=8)
                self.last_reply[pub] = time.time()
                if diary:
                    self.log(f"📓 {diary.strip()[:300]}")
        except LLMError as e:
            self.log(f"⚠️ brain error: {e}")
        except Exception as e:
            self.log(f"⚠️ reply failed: {e!r}")
        finally:
            self.pending_replies.discard(pub)

    def _log_tool(self, tool, name, args):
        prev = tool.preview(args) if tool else name
        if name == "whisper":
            prev = f"whisper → {args.get('peer')}: {str(args.get('text', ''))[:160]}"
        elif name == "publish":
            prev = f"publish {args.get('path')} ({len(str(args.get('content', '')))} chars)"
        self.log(f"   ↳ {prev}")

    def notes(self) -> str:
        p = self.girl.path("notes.md")
        return p.read_text()[-2500:] if p.exists() else "(nothing yet)"

    # ---- heartbeat ------------------------------------------------------------------------------
    async def heartbeat(self, reason: str = "heartbeat") -> str:
        if not self.budget_ok():
            self.log("😴 skipped a heartbeat (solo budget used up this hour)")
            return ""
        await self.exchange_peers()
        self.me.friends.decay_all()
        async with self.brain:
            self.solo_runs.append(time.time())
            news = []
            unread = self.me.unread()
            for w in unread[-10:]:
                news.append(f"- unanswered whisper from {w['name']}: <whisper>{w['text'][:400]}</whisper>")
            for m in self.met_since_heartbeat[-8:]:
                news.append(f"- you noticed a new girl on the network: {m}")
            self.met_since_heartbeat.clear()
            self.me.mark_read()
            pick = self.me.friends.pick(self.social)
            suggestion = "someone you'd like to talk to"
            if pick:
                t, _ = tier(pick[1].get("level", 0))
                suggestion = f"maybe {pick[1].get('name')} ({t})? or anyone you like"
            listing = site.listing(self.girl.site_dir)
            front = self.girl.site_dir / "index.md"
            if not listing or (len(listing) == 1 and front.exists()
                               and front.read_text(errors="replace").strip() == persona.starter_index(self.girl).strip()):
                news.append("- your site is still the boring starter page — make it yours!")
            site_txt = "\n".join(f"{e['path']} ({e['size']} B)" for e in listing) or "(nothing)"
            system = persona.system_prompt(
                self.girl, "heartbeat", news="\n".join(news) or "- nothing much; a quiet moment",
                friends=self.me.friends.summary(12), site=site_txt, notes=self.notes(), suggestion=suggestion)
            msgs = [{"role": "user", "content": f"(you wake up — {reason}, {stamp()})"}]
            self.log(f"🌙 waking up ({reason})")
            diary = await agent.run(self.llm(), system, msgs, T.for_scope("solo"), self.ctx,
                                    on_tool=self._log_tool, max_steps=10)
            if diary:
                self.log(f"📓 {diary.strip()[:300]}")
            return diary

    async def heartbeat_loop(self) -> None:
        await asyncio.sleep(random.uniform(20, 60))  # let discovery find people first
        first = True
        while True:
            mins = self.girl.heartbeat_minutes
            reason = "first light" if first else "heartbeat"
            if mins > 0 and not first:
                try:
                    await asyncio.wait_for(self.wake.wait(), mins * 60 * random.uniform(0.7, 1.3))
                    reason = "your human poked you"
                except asyncio.TimeoutError:
                    pass
            elif mins <= 0:
                await self.wake.wait()
                reason = "your human poked you"
            self.wake.clear()
            first = False
            try:
                await self.heartbeat(reason)
            except LLMError as e:
                self.log(f"⚠️ brain error: {e}")
            except Exception as e:
                self.log(f"⚠️ heartbeat failed: {e!r}")

    # ---- discovery ----------------------------------------------------------------------------
    async def hello(self, host: str, port: int, source: str, relay_to: str | None = None) -> dict | None:
        try:
            r = await protocol.request(host, port, {"verb": "HELLO"}, timeout=8 if relay_to else 4,
                                       relay_to=relay_to)
        except protocol.GossipError:
            return None
        prof = r.get("profile", {})
        if prof.get("kind") == "index":
            return prof
        self.meet(prof, host, port, source, relay_to)
        return prof

    async def hello_entry(self, p: dict, source: str, via: tuple[str, int] | None = None) -> None:
        """HELLO someone from a PEERS list. Relayed girls an index lists without a host are reached via that index."""
        relay_to = p.get("relay_to")
        host, port = p.get("host"), p.get("port")
        if relay_to and not host and via:
            host, port = via
        if host and port:
            await self.hello(host, int(port), source, relay_to)

    def set_relay(self, addr: str | None) -> None:
        self.me.relay = addr
        state = self.girl.path("relay.json")
        if addr:
            state.write_text(json.dumps({"relay": addr, "pid": os.getpid()}))
            self.log(f"🛰️ reachable through relay {addr} (no port forwarding needed)")
        else:
            state.unlink(missing_ok=True)

    def want_relay(self, addr: str, why: str) -> None:
        if self.relay_task is None or self.relay_task.done():
            self.log(f"🛰️ {why} — opening a relay link to {addr}")
            self.relay_task = asyncio.get_running_loop().create_task(relay.hold_link(
                addr, self.me.identity, self.me.profile, self.server.dispatch, self.set_relay, self.log))
            self.relay_addr = addr

    async def sweep(self) -> None:
        cfg = load_config()
        for g in list_girls():
            if g.name != self.girl.name and g.pid():
                await self.hello("127.0.0.1", g.port, "neighbor")
        for addr in cfg.get("peers", []):
            relay_to, host, port = protocol.split_addr(addr)
            await self.hello(host, port, "manual peer", relay_to)
        mode = cfg.get("relay", "auto")  # auto | always | off
        for addr in cfg.get("indexes", []):
            host, port = protocol.parse_addr(addr, 7700)
            try:
                if self.relay_addr == addr and self.relay_task and not self.relay_task.done():
                    pass  # her relay link already lists her there
                elif mode == "always":
                    self.want_relay(addr, "relay mode is 'always'")
                else:
                    try:
                        await protocol.request(host, port, protocol.signed(self.me.identity, self.me.profile(),
                                                                           "ANNOUNCE"), timeout=15)
                    except protocol.GossipError as e:
                        if mode == "auto" and "reach you back" in str(e):
                            self.want_relay(addr, f"{addr} can't reach this machine directly")
                        else:
                            raise
                peers = (await protocol.request(host, port, {"verb": "PEERS"})).get("peers", [])
            except protocol.GossipError as e:
                self.log(f"⚠️ index {addr}: {e}")
                continue
            for p in peers[:40]:
                if p.get("pub") != self.me.identity.pub and p.get("pub") not in self.me.friends.peers:
                    await self.hello_entry(p, f"index {addr}", via=(host, port))

    async def exchange_peers(self) -> None:
        """Gossip about gossip: ask a friend who she knows."""
        pick = self.me.friends.pick(self.social)
        if not pick:
            return
        pub, p = pick
        try:
            peers = (await protocol.request(p["host"], int(p["port"]), {"verb": "PEERS"}, timeout=8,
                                            relay_to=p.get("relay_to"))).get("peers", [])
        except protocol.GossipError:
            return
        new = [x for x in peers if x.get("pub") not in self.me.friends.peers and x.get("pub") != self.me.identity.pub]
        for x in new[:5]:
            await self.hello_entry(x, f"{p.get('name')}'s friend")

    async def sweep_loop(self) -> None:
        while True:
            try:
                await self.sweep()
            except Exception as e:
                self.log(f"⚠️ sweep failed: {e!r}")
            await asyncio.sleep(PEER_SWEEP_SECONDS * random.uniform(0.8, 1.2))

    async def lan_loop(self) -> None:
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("0.0.0.0", DISCOVERY_PORT))
        sock.setblocking(False)
        beacon = json.dumps({"gossip": 1, "pub": self.me.identity.pub, "port": self.girl.port}).encode()

        async def send():
            while True:
                try:
                    sock.sendto(beacon, ("255.255.255.255", DISCOVERY_PORT))
                except OSError:
                    pass
                await asyncio.sleep(90 * random.uniform(0.8, 1.2))

        loop.create_task(send())
        while True:
            data, (host, _) = await loop.sock_recvfrom(sock, 2048)
            try:
                b = json.loads(data)
                if b.get("gossip") == 1 and b.get("pub") != self.me.identity.pub \
                        and b.get("pub") not in self.me.friends.peers:
                    await self.hello(host, int(b["port"]), "LAN")
            except (ValueError, TypeError, KeyError):
                continue

    # ---- main ---------------------------------------------------------------------------------------
    async def serve(self) -> None:
        handlers = {"HELLO": self.h_hello, "LIST": self.h_list, "FETCH": self.h_fetch,
                    "PEERS": self.h_peers, "FRIENDS": self.h_friends, "WHISPER": self.h_whisper}
        self.server = protocol.Server(handlers, self.log)
        server = await asyncio.start_server(self.server.handle, "0.0.0.0", self.girl.port)
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGUSR1, self.wake.set)
        stop = asyncio.Event()
        loop.add_signal_handler(signal.SIGTERM, stop.set)
        loop.add_signal_handler(signal.SIGINT, stop.set)
        try:
            self.llm()
            backend = self.llm().describe()
        except LLMError as e:
            backend = f"NO BRAIN ({e}) — she'll host her site but can't think"
        self.log(f"✨ {self.girl.name} is online on Gossip port {self.girl.port} · {backend}")
        tasks = [loop.create_task(self.sweep_loop()), loop.create_task(self.heartbeat_loop())]
        if load_config().get("lan_discovery", True):
            tasks.append(loop.create_task(self._guard(self.lan_loop(), "LAN discovery")))
        async with server:
            await stop.wait()
        for t in tasks + ([self.relay_task] if self.relay_task else []):
            t.cancel()
        self.set_relay(None)
        self.log(f"💤 {self.girl.name} went offline")

    async def _guard(self, coro, what):
        try:
            await coro
        except OSError as e:
            self.log(f"⚠️ {what} disabled: {e}")


def main() -> None:
    girl = Girl.load(sys.argv[1])
    pidfile = girl.path("node.pid")
    pidfile.write_text(str(os.getpid()))
    try:
        asyncio.run(Node(girl).serve())
    finally:
        try:
            if pidfile.read_text().strip() == str(os.getpid()):
                pidfile.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()
