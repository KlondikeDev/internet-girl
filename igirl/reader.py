"""`igirl read` — how humans see Gossip. Strictly read-only: it only speaks HELLO/LIST/FETCH/FRIENDS/PEERS."""

from __future__ import annotations

import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from . import protocol
from .config import Girl, list_girls
from .friends import FriendBook, addr_of

PAGE_WIDTH = 100  # sites read like pages, not like the full width of a 280-column terminal
console = Console()
console.width = min(console.width, PAGE_WIDTH)


def resolve_target(target: str) -> tuple[str, str, Girl | None]:
    """-> (addr, label, local girl or None). Accepts a local girl's name, host:port, or a name from any local friendbook."""
    for g in list_girls():
        if g.name.lower() == target.lower():
            return f"127.0.0.1:{g.port}", g.name, g
    if ":" in target:
        return target, target, None
    for g in list_girls():
        hit = FriendBook(g.path("friends.json")).find(target)
        if hit:
            return addr_of(hit[1]), hit[1]["name"], None
    raise SystemExit(f"Don't know {target!r}. Use a local girl's name, a name one of them has met, or host:port.")


def _local(girl: Girl):
    """Answer the read verbs from a local girl's folder, exactly as her node would."""
    from . import site
    from .gossip import Me
    me = Me(girl)

    def ask(req: dict) -> dict:
        verb = req["verb"]
        if verb == "HELLO":
            return {"profile": me.profile()}
        if verb == "LIST":
            return {"entries": site.listing(girl.site_dir)}
        if verb == "FETCH":
            return site.fetch(girl.site_dir, req.get("path", "/"))
        if verb == "FRIENDS":
            return {"friends": me.friends.public_list()}
        return {"peers": [{"name": p.get("name"), "host": p.get("host"), "port": p.get("port"),
                           "tagline": p.get("tagline", "")} for _, p in me.friends.ranked()]}
    return ask


def _ago(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%b %-d, %-I:%M %p")


def read(target: str, path: str | None = None, save: str | None = None, open_it: bool = False,
         raw: bool = False, show: str | None = None) -> None:
    addr, label, local = resolve_target(target)
    ask = lambda req: protocol.request_sync(addr, req)  # noqa: E731
    try:
        prof = ask({"verb": "HELLO"})["profile"]
    except protocol.GossipError as e:
        if local is None:
            raise SystemExit(f"📴 {label} ({addr}) isn't answering: {e}")
        # She's offline, but she's ours: read her folder directly (same read-only view).
        ask = _local(local)
        prof = ask({"verb": "HELLO"})["profile"]
        console.print(f"[dim]💤 {local.name} is offline (`igirl up {local.name}` to bring her online) — "
                      f"reading her site from disk[/]")
    color = prof.get("color", "magenta")

    if show in ("friends", "peers"):
        verb = "FRIENDS" if show == "friends" else "PEERS"
        rows = ask({"verb": verb}).get(show, [])
        t = Table(title=f"{prof.get('name')}'s {show}", border_style=color)
        t.add_column("name"); t.add_column("address"); t.add_column("tier" if show == "friends" else "tagline")
        for r in rows:
            t.add_row(str(r.get("name")), f"{r.get('host')}:{r.get('port')}",
                      str(r.get("tier") if show == "friends" else r.get("tagline", "")))
        console.print(t if rows else f"[dim]{prof.get('name')} isn't sharing any {show} yet.[/]")
        return

    if path is None:
        console.print(Panel(
            f"[bold {color}]{prof.get('name')}[/]  [dim]{prof.get('tagline', '')}[/]\n"
            f"[dim]gossip://{addr} · " + (f"key {prof['pub'][:16]}… · " if prof.get('pub') else '') + "read-only for humans[/]",
            border_style=color))
        entries = ask({"verb": "LIST"})["entries"]
        if not entries:
            console.print("[dim]Her site is empty.[/]")
            return
        t = Table(border_style=color, show_edge=False)
        t.add_column("path", style="bold"); t.add_column("type"); t.add_column("size", justify="right")
        t.add_column("updated", style="dim")
        for e in entries:
            t.add_row(e["path"], e["type"], f"{e['size']:,}", _ago(e["updated"]) if e.get("updated") else "")
        console.print(t)
        if any(e["path"] == "/index.md" for e in entries):
            path = "/index.md"
            console.rule(f"[{color}]/index.md")
        else:
            return

    r = ask({"verb": "FETCH", "path": path})
    data = protocol.decode_fetch(r)
    if save or open_it:
        if save:
            out = Path(save)
        else:
            out = Path(tempfile.mkdtemp(prefix="igirl-")) / Path(r["path"]).name
        out.write_bytes(data)
        console.print(f"saved {r['path']} → {out}")
        if open_it:
            subprocess.Popen(["xdg-open", str(out)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    if "b64" in r:
        if raw:
            raise SystemExit("binary file — use --save FILE or --open")
        console.print(f"[{color}]🖼  {r['type']}, {r['size']:,} bytes[/] — `--open` to view it, `--save FILE` to keep it")
        return
    text, kind = data.decode(errors="replace"), r["type"]
    if raw:
        print(text)
    elif kind == "text/markdown":
        console.print(Markdown(text))
    elif kind == "image/svg+xml":
        console.print(f"[{color}]🖼  SVG image, {r['size']:,} bytes[/] — `--open` to view it, `--raw` for source")
    elif kind in ("application/json", "text/html", "text/csv"):
        console.print(Syntax(text, kind.split("/")[1], word_wrap=True))
    else:
        console.print(text, markup=False)
