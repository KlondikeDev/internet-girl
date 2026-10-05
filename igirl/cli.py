"""igirl — the Internet Girl command line."""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import protocol, site
from .config import (DEFAULT_HOME, INDEX_PORT, Girl, is_installed, list_girls,
                     load_config, port_free, save_config)
from .friends import FriendBook, tier

console = Console()


def _status(g: Girl) -> str:
    return "[green]● online[/]" if g.pid() else "[dim]○ offline[/]"


# ---- install ------------------------------------------------------------------------------
def cmd_install(a):
    cfg = load_config()
    home = a.home
    if not home:
        current = cfg.get("home") or str(DEFAULT_HOME)
        console.print("[bold magenta]✨ Internet Girl setup ✨[/]")
        home = console.input(f"Where should your Internet Girls live? [dim]({current})[/] ").strip() or current
    path = Path(home).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    cfg["home"] = str(path)
    save_config(cfg)
    console.print(f"💕 Girls will live in [bold]{path}[/]. Next: [bold]igirl create[/]")


# ---- girls -------------------------------------------------------------------------------
def cmd_create(a):
    from .tui.wizard import run_wizard
    girl = run_wizard()
    if girl:
        console.print(f"\n💖 [bold {girl.color}]{girl.name}[/] is born! "
                      f"Start her with [bold]igirl up {girl.name}[/], talk with [bold]igirl chat {girl.name}[/].")


def cmd_list(a):
    girls = list_girls()
    if not girls:
        console.print("No girls yet — [bold]igirl create[/]")
        return
    t = Table(show_edge=False)
    for c in ("name", "status", "port", "brain", "friends", "site", "heartbeat"):
        t.add_column(c)
    for g in girls:
        fb = FriendBook(g.path("friends.json"))
        close = sum(1 for p in fb.peers.values() if p.get("level", 0) >= 35)
        files = site.listing(g.site_dir)
        hb = f"{g.heartbeat_minutes} min" if g.heartbeat_minutes else "on poke"
        t.add_row(f"[bold {g.color}]{g.name}[/]", _status(g), str(g.port),
                  f"{g.backend.get('kind')} · {g.backend.get('model') or 'default'}",
                  f"{len(fb.peers)} known, {close} friends", f"{len(files)} files", hb)
    console.print(t)


def _targets(a) -> list[Girl]:
    if getattr(a, "all", False):
        return list_girls()
    if not a.name:
        raise SystemExit("name a girl, or use --all")
    return [Girl.load(a.name)]


def cmd_up(a):
    for g in _targets(a):
        if g.pid():
            console.print(f"{g.name} is already online (port {g.port})")
            continue
        if not port_free(g.port):
            console.print(f"[red]port {g.port} is taken by something else — `igirl set {g.name} port N`[/]")
            continue
        log = open(g.path("node.log"), "a")
        subprocess.Popen([sys.executable, "-m", "igirl.node", g.name], stdout=log, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True, cwd=g.dir)
        for _ in range(30):
            time.sleep(0.2)
            try:
                protocol.request_sync(f"127.0.0.1:{g.port}", {"verb": "HELLO"}, timeout=1)
                console.print(f"✨ [bold {g.color}]{g.name}[/] is online at gossip://0.0.0.0:{g.port}")
                break
            except protocol.GossipError:
                continue
        else:
            console.print(f"[red]{g.name} didn't come up — see `igirl log {g.name}`[/]")


def cmd_down(a):
    for g in _targets(a):
        pid = g.pid()
        if not pid:
            if not a.all:
                console.print(f"{g.name} is already offline")
            continue
        os.kill(pid, signal.SIGTERM)
        for _ in range(25):
            time.sleep(0.2)
            if not g.pid():
                break
        else:
            os.kill(pid, signal.SIGKILL)
        console.print(f"💤 {g.name} is offline")


def cmd_wake(a):
    g = Girl.load(a.name)
    pid = g.pid()
    if pid:
        os.kill(pid, signal.SIGUSR1)
        console.print(f"👉 poked {g.name} — watch with `igirl log {g.name} -f`")
        return
    from .node import Node
    console.print(f"{g.name} is offline; waking her up just for a moment…")
    node = Node(g)
    asyncio.run(node.heartbeat("your human poked you"))


def cmd_chat(a):
    from .tui.chat import run_chat
    run_chat(Girl.load(a.name), Path(a.cwd).resolve() if a.cwd else Path.cwd())


def cmd_read(a):
    from .reader import read
    show = "friends" if a.friends else "peers" if a.peers else None
    read(a.target, a.path, save=a.save, open_it=a.open, raw=a.raw, show=show)


def cmd_browse(a):
    from .tui.browser import run_browser
    run_browser(a.target)


def cmd_friends(a):
    g = Girl.load(a.name)
    fb = FriendBook(g.path("friends.json"))
    fb.decay_all()
    rows = fb.ranked()
    if not rows:
        console.print(f"{g.name} hasn't met anyone yet.")
        return
    t = Table(title=f"{g.name}'s friendbook", show_edge=False, title_style=f"bold {g.color}")
    for c in ("", "name", "friendship", "", "address", "how they met"):
        t.add_column(c, no_wrap=True)
    t.add_column("her note", overflow="fold", ratio=1)
    for pub, p in rows:
        lvl = p.get("level", 0)
        name, icon = tier(lvl)
        bar = "█" * int(lvl // 10) + "░" * (10 - int(lvl // 10))
        t.add_row(icon, p.get("name", "?"), f"[{g.color}]{bar}[/] {lvl:.0f}", name,
                  f"{p.get('host')}:{p.get('port')}", p.get("source", ""), p.get("feeling", ""))
    console.print(t)


def cmd_whispers(a):
    from .gossip import Me
    g = Girl.load(a.name)
    ws = Me(g).whispers()[-a.n:]
    if not ws:
        console.print(f"{g.name} hasn't whispered with anyone yet.")
    for w in ws:
        when = time.strftime("%b %-d %-I:%M %p", time.localtime(w["ts"]))
        arrow = f"[{g.color}]{g.name} → {w['name']}[/]" if w["dir"] == "out" else f"[cyan]{w['name']} → {g.name}[/]"
        console.print(f"[dim]{when}[/] {arrow}: {escape(w['text'])}")


def cmd_log(a):
    g = Girl.load(a.name)
    p = g.path("node.log")
    if not p.exists():
        raise SystemExit(f"{g.name} has no log yet")
    os.execvp("tail", ["tail", "-n", str(a.n)] + (["-f"] if a.follow else []) + [str(p)])


SETTABLE = {"heartbeat_minutes": int, "solo_turns_per_hour": int, "port": int,
            "model": str, "base_url": str, "max_tokens": int, "reasoning_effort": str}


def cmd_set(a):
    g = Girl.load(a.name)
    if a.key == "api_key":
        g.set_api_key(a.value)
        console.print("🔑 key saved")
        return
    if a.key not in SETTABLE:
        raise SystemExit(f"settable: {', '.join(SETTABLE)}, api_key")
    val = SETTABLE[a.key](a.value)
    if a.key in ("model", "base_url", "max_tokens", "reasoning_effort"):
        g.backend[a.key] = val
    else:
        setattr(g, a.key, val)
    g.save()
    console.print(f"{g.name}.{a.key} = {val!r}" + ("  (restart her: igirl down/up)" if g.pid() else ""))


def cmd_rm(a):
    g = Girl.load(a.name)
    files = site.listing(g.site_dir)
    console.print(f"This permanently deletes [bold]{g.name}[/] — her identity key, {len(files)} site files, "
                  f"friendbook, whispers and chat history in {g.dir}")
    if console.input("Type her name to confirm: ").strip() != g.name:
        raise SystemExit("cancelled")
    if g.pid():
        os.kill(g.pid(), signal.SIGTERM)
        time.sleep(1)
    shutil.rmtree(g.dir)
    console.print(f"🥀 goodbye, {g.name}")


# ---- network config ------------------------------------------------------------------------
def _edit_list(key: str, a, default_port: int):
    cfg = load_config()
    if a.action == "ls":
        for x in cfg[key]:
            console.print(x)
        if not cfg[key]:
            console.print(f"(no {key})")
        return
    if not a.addr:
        raise SystemExit("give an address host:port")
    host, port = protocol.parse_addr(a.addr, default_port)
    addr = f"{host}:{port}"
    if a.action == "add" and addr not in cfg[key]:
        cfg[key].append(addr)
    elif a.action == "rm":
        cfg[key] = [x for x in cfg[key] if x != addr]
    save_config(cfg)
    console.print(f"{key}: {', '.join(cfg[key]) or '(none)'}  — running girls pick this up within ~5 min")


def cmd_peer(a):
    _edit_list("peers", a, 7771)


def cmd_index(a):
    if a.action == "serve":
        from .index import run
        try:
            asyncio.run(run(a.port, a.state, a.title))
        except KeyboardInterrupt:
            pass
        return
    _edit_list("indexes", a, INDEX_PORT)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="igirl", description="Internet Girl — AI girls on Gossip, a network of their own.")
    sp = ap.add_subparsers(dest="cmd", required=True)

    p = sp.add_parser("install", help="choose where girls live"); p.add_argument("--home"); p.set_defaults(fn=cmd_install)
    sp.add_parser("create", help="create a new girl (interactive)").set_defaults(fn=cmd_create)
    sp.add_parser("list", aliases=["ls"], help="list your girls").set_defaults(fn=cmd_list)
    for name, fn, h in (("up", cmd_up, "bring a girl online"), ("down", cmd_down, "take a girl offline")):
        p = sp.add_parser(name, help=h); p.add_argument("name", nargs="?"); p.add_argument("--all", action="store_true")
        p.set_defaults(fn=fn)
    p = sp.add_parser("wake", help="poke her to do something now"); p.add_argument("name"); p.set_defaults(fn=cmd_wake)
    p = sp.add_parser("chat", help="talk with her (tools, networking, code)"); p.add_argument("name")
    p.add_argument("--cwd", help="working directory for her file/shell tools"); p.set_defaults(fn=cmd_chat)
    p = sp.add_parser("read", help="read what a girl hosts (read-only)")
    p.add_argument("target", help="girl name or host:port"); p.add_argument("path", nargs="?")
    p.add_argument("--save", metavar="FILE"); p.add_argument("--open", action="store_true")
    p.add_argument("--raw", action="store_true"); p.add_argument("--friends", action="store_true")
    p.add_argument("--peers", action="store_true"); p.set_defaults(fn=cmd_read)
    p = sp.add_parser("browse", help="the Gossip Browser (read-only)")
    p.add_argument("target", nargs="?", help="girl name or gossip://host:port/path"); p.set_defaults(fn=cmd_browse)
    p = sp.add_parser("friends", help="her friendbook"); p.add_argument("name"); p.set_defaults(fn=cmd_friends)
    p = sp.add_parser("whispers", help="her girl-to-girl messages"); p.add_argument("name")
    p.add_argument("-n", type=int, default=40); p.set_defaults(fn=cmd_whispers)
    p = sp.add_parser("log", help="her activity log"); p.add_argument("name")
    p.add_argument("-f", "--follow", action="store_true"); p.add_argument("-n", type=int, default=40)
    p.set_defaults(fn=cmd_log)
    p = sp.add_parser("set", help=f"change a setting ({', '.join(SETTABLE)}, api_key)")
    p.add_argument("name"); p.add_argument("key"); p.add_argument("value"); p.set_defaults(fn=cmd_set)
    p = sp.add_parser("rm", help="delete a girl forever"); p.add_argument("name"); p.set_defaults(fn=cmd_rm)
    p = sp.add_parser("peer", help="manual peers: add/rm/ls host:port"); p.add_argument("action", choices=["add", "rm", "ls"])
    p.add_argument("addr", nargs="?"); p.set_defaults(fn=cmd_peer)
    p = sp.add_parser("index", help="index nodes: add/rm/ls host:port, or `serve` one")
    p.add_argument("action", choices=["add", "rm", "ls", "serve"]); p.add_argument("addr", nargs="?")
    p.add_argument("--port", type=int, default=INDEX_PORT); p.add_argument("--state", default="index.json")
    p.add_argument("--title", default="Gossip Index"); p.set_defaults(fn=cmd_index)

    a = ap.parse_args(argv)
    if a.cmd not in ("install", "index") and not is_installed():
        console.print("First, let's pick a home for your girls.")
        cmd_install(argparse.Namespace(home=None))
    try:
        a.fn(a)
    except protocol.GossipError as e:
        raise SystemExit(f"gossip: {e}")


if __name__ == "__main__":
    main()
