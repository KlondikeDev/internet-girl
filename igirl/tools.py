"""Tools an Internet Girl can call.

risk "auto": runs immediately (read-only, or only touches her own site/notes/friendships)
risk "ask":  the human must approve each call in chat (y / n / always for this session)

Scopes: "chat" tools exist only when her human is there to approve. Her autonomous
time ("solo": heartbeat + replying to whispers) only gets site, gossip and memory tools —
no shell, no files, no scanning.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from . import site
from .gossip import Me
from .protocol import GossipError

OUT_LIMIT = 8000
NOTES_LIMIT = 4000


@dataclass
class Ctx:
    me: Me
    cwd: Path


@dataclass
class Tool:
    name: str
    description: str
    params: dict
    fn: Callable[..., Awaitable[str]]
    risk: str = "auto"
    scopes: tuple = ("chat", "solo")

    def __post_init__(self):
        self.required = [k for k, v in self.params.items() if not v.pop("optional", False)]

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description,
                "parameters": {"type": "object", "properties": self.params, "required": self.required}}

    def preview(self, args: dict) -> str:
        """One-line human description for approval prompts and the transcript."""
        if self.name == "shell":
            return f"$ {args.get('command', '')}"
        if self.name in ("write_file", "edit_file", "read_file", "list_dir"):
            return f"{self.name} {args.get('path', '')}"
        inner = ", ".join(f"{k}={str(v)[:60]!r}" for k, v in args.items() if k != "content")
        return f"{self.name}({inner})"


def _clip(s: str, n: int = OUT_LIMIT) -> str:
    return s if len(s) <= n else s[:n] + f"\n…[{len(s) - n} more chars truncated]"


def _arg_ok(v: str) -> str:
    v = str(v).strip()
    if not v or v.startswith("-") or any(c in v for c in " \t\n;|&`$<>"):
        raise GossipError(f"suspicious argument {v!r}")
    return v


async def _run(argv: list[str], cwd: Path | None = None, timeout: int = 60, shell: bool = False) -> str:
    if not shell and not shutil.which(argv[0]):
        return f"`{argv[0]}` isn't installed on this machine."
    if shell:
        proc = await asyncio.create_subprocess_shell(
            argv[0], cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL, start_new_session=True)
    else:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL, start_new_session=True)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        out = (await proc.communicate())[0] or b""
        return _clip(out.decode(errors="replace")) + f"\n[timed out after {timeout}s]"
    text = out.decode(errors="replace")
    return _clip(text + (f"\n[exit {proc.returncode}]" if proc.returncode else "")) or "(no output)"


def _path(ctx: Ctx, p: str) -> Path:
    return (ctx.cwd / Path(p).expanduser()).resolve()


# ---- network ------------------------------------------------------------------------
async def t_ping(ctx, host, count=4):
    return await _run(["ping", "-c", str(max(1, min(int(count), 10))), "-W", "2", _arg_ok(host)], timeout=30)


async def t_dns(ctx, name, type="A", server=""):
    argv = ["dig", "+noall", "+answer", "+comments", _arg_ok(name), _arg_ok(type)]
    if server:
        argv.append("@" + _arg_ok(server))
    return await _run(argv, timeout=20)


async def t_whois(ctx, query):
    return await _run(["whois", _arg_ok(query)], timeout=30)


async def t_traceroute(ctx, host):
    return await _run(["traceroute", "-w", "2", "-q", "1", "-m", "24", _arg_ok(host)], timeout=60)


async def t_netinfo(ctx):
    parts = []
    for argv in (["ip", "-br", "addr"], ["ip", "route"], ["ss", "-tulnH"]):
        parts.append(f"$ {' '.join(argv)}\n" + await _run(argv, timeout=10))
    return "\n\n".join(parts)


async def t_http(ctx, url, method="GET"):
    method = method.upper()
    if method not in ("GET", "HEAD"):
        return "only GET and HEAD — use shell (with approval) for anything else"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            r = await c.request(method, url, headers={"User-Agent": "InternetGirl/0.1"})
    except httpx.HTTPError as e:
        return f"request failed: {e.__class__.__name__}: {e}"
    hdrs = "\n".join(f"{k}: {v}" for k, v in r.headers.items())
    hist = " → ".join(str(h.status_code) for h in r.history)
    body = "" if method == "HEAD" else r.text[:5000]
    return _clip(f"{r.status_code} {r.reason_phrase} ({r.url}){'  via ' + hist if hist else ''}\n"
                 f"{hdrs}\n\n{body}")


async def t_scan(ctx, host, ports="1-1024"):
    if shutil.which("nmap"):
        return await _run(["nmap", "-Pn", "-T4", "--open", "-p", _arg_ok(ports), _arg_ok(host)], timeout=180)
    return "nmap isn't installed"


async def t_shell(ctx, command, timeout=60):
    return await _run([command], cwd=ctx.cwd, timeout=max(1, min(int(timeout), 600)), shell=True)


# ---- files ------------------------------------------------------------------------
async def t_read_file(ctx, path, offset=0, limit=400):
    p = _path(ctx, path)
    try:
        lines = p.read_text(errors="replace").splitlines()
    except OSError as e:
        return f"can't read {p}: {e.strerror}"
    off = max(0, int(offset))
    chunk = lines[off:off + int(limit)]
    body = "\n".join(f"{i + off + 1:5}  {l}" for i, l in enumerate(chunk))
    more = f"\n…({len(lines) - off - len(chunk)} more lines)" if off + len(chunk) < len(lines) else ""
    return _clip(body + more)


async def t_list_dir(ctx, path="."):
    p = _path(ctx, path)
    try:
        items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name))
    except OSError as e:
        return f"can't list {p}: {e.strerror}"
    return _clip("\n".join(f"{x.name}/" if x.is_dir() else f"{x.name}  ({x.stat().st_size} B)"
                           for x in items[:300])) or "(empty)"


async def t_write_file(ctx, path, content):
    p = _path(ctx, path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"wrote {len(content)} chars to {p}"


async def t_edit_file(ctx, path, old, new):
    p = _path(ctx, path)
    text = p.read_text()
    n = text.count(old)
    if n != 1:
        return f"`old` must match exactly once in {p} (found {n} matches) — nothing changed"
    p.write_text(text.replace(old, new))
    return f"edited {p}"


async def t_fetch_image(ctx, url, save_as):
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        r = await c.get(url, headers={"User-Agent": "InternetGirl/0.1"})
    if r.status_code != 200:
        return f"HTTP {r.status_code}"
    if len(r.content) > 5 * 1024 * 1024:
        return "image too large (5 MB max)"
    return "hosted at " + site.publish(ctx.me.girl.site_dir, save_as, r.content)


# ---- her site ------------------------------------------------------------------------
async def t_publish(ctx, path, content):
    return "published " + site.publish(ctx.me.girl.site_dir, path, content)


async def t_unpublish(ctx, path):
    return "removed " + site.remove(ctx.me.girl.site_dir, path)


async def t_my_site(ctx, path=""):
    if path:
        r = site.fetch(ctx.me.girl.site_dir, path)
        return r.get("text") or f"(binary {r['type']}, {r['size']} B)"
    entries = site.listing(ctx.me.girl.site_dir)
    return "\n".join(f"{e['path']}  ({e['type']}, {e['size']} B)" for e in entries) or "(your site is empty)"


# ---- gossip ---------------------------------------------------------------------------
async def t_list_peers(ctx):
    peers = await ctx.me.peer_list()
    if not peers:
        return "you haven't met anyone yet"
    return "\n".join(f"{p['name']} @ {p['addr']} — {p['tier']} ({p['level']}/100) {p['tagline']}" for p in peers)


async def t_read_peer(ctx, peer, path="/"):
    return await ctx.me.read_peer(peer, path)


async def t_whisper(ctx, peer, text):
    return await ctx.me.whisper(peer, text)


async def t_feelings(ctx, peer, delta, reason=""):
    hit = ctx.me.friends.find(peer)
    if not hit:
        return f"no one called {peer!r} in your friendbook"
    d = max(-10.0, min(10.0, float(delta)))
    lvl = ctx.me.friends.bump(hit[0], d, reason)
    from .friends import tier
    return f"{hit[1]['name']} is now {lvl:.0f}/100 ({tier(lvl)[0]})"


async def t_remember(ctx, note):
    p = ctx.me.girl.path("notes.md")
    old = p.read_text() if p.exists() else ""
    new = (old + f"- {note.strip()}\n")[-NOTES_LIMIT:]
    p.write_text(new)
    return "noted 💭"


def _p(t, d, optional=False, **kw):
    out = {"type": t, "description": d, **kw}
    if optional:
        out["optional"] = True
    return out


TOOLS: list[Tool] = [
    # network (chat)
    Tool("ping", "Ping a host (ICMP) to check reachability and latency.",
         {"host": _p("string", "hostname or IP"), "count": _p("integer", "packets, max 10", True)},
         t_ping, scopes=("chat",)),
    Tool("dns_lookup", "DNS lookup with dig. type: A, AAAA, MX, TXT, NS, CNAME, SOA, PTR, CAA...",
         {"name": _p("string", "domain name"), "type": _p("string", "record type", True),
          "server": _p("string", "resolver IP to ask", True)}, t_dns, scopes=("chat",)),
    Tool("whois", "WHOIS lookup for a domain or IP.", {"query": _p("string", "domain or IP")},
         t_whois, scopes=("chat",)),
    Tool("traceroute", "Trace the network path to a host.", {"host": _p("string", "hostname or IP")},
         t_traceroute, scopes=("chat",)),
    Tool("net_info", "Show this machine's interfaces, routes and listening sockets.", {},
         t_netinfo, scopes=("chat",)),
    Tool("http_request", "HTTP GET or HEAD a URL; returns status, headers and the start of the body.",
         {"url": _p("string", "full URL"), "method": _p("string", "GET or HEAD", True)},
         t_http, scopes=("chat",)),
    Tool("port_scan", "Scan TCP ports on a host with nmap (only scan hosts the human owns or may test).",
         {"host": _p("string", "hostname or IP"), "ports": _p("string", "e.g. 22,80,443 or 1-1024", True)},
         t_scan, risk="ask", scopes=("chat",)),
    Tool("shell", "Run a shell command in the working directory. Use for anything the other tools can't do.",
         {"command": _p("string", "the command"), "timeout": _p("integer", "seconds, default 60", True)},
         t_shell, risk="ask", scopes=("chat",)),
    # files (chat)
    Tool("read_file", "Read a text file (line-numbered).",
         {"path": _p("string", "file path"), "offset": _p("integer", "first line (0-based)", True),
          "limit": _p("integer", "max lines", True)}, t_read_file, scopes=("chat",)),
    Tool("list_dir", "List a directory.", {"path": _p("string", "directory", True)}, t_list_dir, scopes=("chat",)),
    Tool("write_file", "Create or overwrite a file.",
         {"path": _p("string", "file path"), "content": _p("string", "full file contents")},
         t_write_file, risk="ask", scopes=("chat",)),
    Tool("edit_file", "Replace one exact, unique snippet in a file.",
         {"path": _p("string", "file path"), "old": _p("string", "exact text to replace"),
          "new": _p("string", "replacement")}, t_edit_file, risk="ask", scopes=("chat",)),
    Tool("fetch_image", "Download an image from a URL and host it on your Gossip site.",
         {"url": _p("string", "image URL"), "save_as": _p("string", "site path like /pics/cat.png")},
         t_fetch_image, risk="ask", scopes=("chat",)),
    # her site (always hers to change)
    Tool("publish", "Create or replace a file on YOUR Gossip site (.md .txt .svg .json .csv .html). "
         "/index.md is your front page.",
         {"path": _p("string", "site path like /index.md or /art/moon.svg"),
          "content": _p("string", "file contents")}, t_publish),
    Tool("unpublish", "Remove a file from your site.", {"path": _p("string", "site path")}, t_unpublish),
    Tool("my_site", "List your site's files, or read one of them.", {"path": _p("string", "site path", True)},
         t_my_site),
    # gossip
    Tool("list_peers", "Everyone you've met on Gossip, with friendship levels.", {}, t_list_peers),
    Tool("read_peer", "Visit another girl's site. path '/' shows her file list and front page.",
         {"peer": _p("string", "her name or host:port"), "path": _p("string", "site path", True)}, t_read_peer),
    Tool("whisper", "Send a private message to another girl over Gossip.",
         {"peer": _p("string", "her name or host:port"), "text": _p("string", "your message")}, t_whisper),
    Tool("adjust_feelings", "Change how much you like someone (-10..10) after an interaction.",
         {"peer": _p("string", "her name"), "delta": _p("number", "-10 to 10"),
          "reason": _p("string", "a short private note on why", True)}, t_feelings),
    Tool("remember", "Save a short private note to your memory.", {"note": _p("string", "the note")}, t_remember),
]

BY_NAME = {t.name: t for t in TOOLS}


def for_scope(scope: str) -> list[Tool]:
    return [t for t in TOOLS if scope in t.scopes]


async def call(tool: Tool, ctx: Ctx, args: dict) -> str:
    try:
        return await tool.fn(ctx, **args)
    except TypeError:
        want = ", ".join(f"{k}{'' if k in tool.required else '?'}" for k in tool.params)
        got = ", ".join(map(str, args)) or "nothing"
        return f"bad arguments for {tool.name}: it takes ({want}) but got ({got}). Call it again with exactly those names."
    except GossipError as e:
        return f"gossip error: {e}"
    except (OSError, ValueError, httpx.HTTPError) as e:
        return f"error: {e.__class__.__name__}: {e}"
