"""`igirl browse` — the Gossip Browser. Read-only by construction: it only speaks
HELLO / LIST / FETCH / FRIENDS / PEERS.

Addresses look like  gossip://host:port/path  — or just type a girl's name.
"""

from __future__ import annotations

import asyncio
import io
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from rich.markup import escape
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Footer, Input, Markdown, Static, Tree

from .. import protocol
from ..config import load_config, list_girls
from ..friends import FriendBook, addr_of, tier
from ..reader import _local

IMAGE_MAX_BYTES = 3 * 1024 * 1024
TEXT_LANG = {"application/json": "json", "text/csv": "csv", "text/html": "html", "image/svg+xml": "xml"}


@dataclass(frozen=True)
class Loc:
    addr: str            # host:port
    path: str = "/"      # "/" = her front page
    view: str = "page"   # page | friends | peers

    def url(self) -> str:
        q = "" if self.view == "page" else f"?{self.view}"
        return f"gossip://{self.addr}{self.path}{q}"


def fmt_addr(host: str, port) -> str:
    return f"[{host}]:{port}" if ":" in str(host) else f"{host}:{port}"


def parse(text: str, base: Loc | None) -> Loc | None:
    t = text.strip()
    if not t:
        return None
    view = "page"
    if "?" in t:
        t, _, q = t.partition("?")
        view = q if q in ("friends", "peers") else "page"
    if t.startswith("gossip://"):
        rest = t[len("gossip://"):]
        if rest.startswith("["):
            host, _, after = rest[1:].partition("]")
            port, _, path = after.lstrip(":").partition("/")
            addr = fmt_addr(host, port or 7771)
        else:
            addr, _, path = rest.partition("/")
            if ":" not in addr:
                addr += ":7771"
        return Loc(addr, "/" + path, view)
    if t.startswith(("http://", "https://")):
        return None
    if base and (t.startswith("/") or "." in PurePosixPath(t).name and ":" not in t):
        p = t if t.startswith("/") else str(PurePosixPath(base.path).parent / t)
        return Loc(base.addr, "/" + str(PurePosixPath(p)).lstrip("/"), view)
    if ":" in t:  # host:port[/path]
        addr, _, path = t.partition("/")
        return Loc(addr, "/" + path, view)
    # a name: one of mine, or someone my girls have met
    for g in list_girls():
        if g.name.lower() == t.lower():
            return Loc(f"127.0.0.1:{g.port}", "/", view)
    for g in list_girls():
        hit = FriendBook(g.path("friends.json")).find(t)
        if hit:
            return Loc(addr_of(hit[1]), "/", view)
    raise ValueError(f"don't know anyone called {t!r}")


def render_image(data: bytes, svg: bool, cols: int, rows: int, bg=(17, 17, 17)) -> Text:
    """Draw an image with ▀ half-blocks: each character cell is two pixels tall."""
    from PIL import Image
    if svg:
        import cairosvg  # unsafe=False (the default) refuses external files and URLs
        data = cairosvg.svg2png(bytestring=data, output_width=cols * 4)
    img = Image.open(io.BytesIO(data))
    img.thumbnail((cols, rows * 2))
    canvas = Image.new("RGB", img.size, bg)
    canvas.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[3])
    w, h = canvas.size
    px = canvas.load()
    out = Text()
    for y in range(0, h, 2):
        for x in range(w):
            top = px[x, y]
            bot = px[x, y + 1] if y + 1 < h else bg
            out.append("▀", style=f"rgb({top[0]},{top[1]},{top[2]}) on rgb({bot[0]},{bot[1]},{bot[2]})")
        out.append("\n")
    return out


def images_available() -> bool:
    try:
        import cairosvg  # also loads libcairo, which can fail with OSError
        import PIL
        del cairosvg, PIL
        return True
    except (ImportError, OSError):
        return False


class Browser(App):
    TITLE = "Gossip Browser"
    CSS = """
    Screen { background: $background; }
    #bar { height: 3; padding: 0 1; }
    #addr { width: 1fr; }
    #places { width: 34; border-right: tall $panel; padding: 0 1; }
    #page { width: 1fr; padding: 0 2; }
    #banner { margin: 1 0; padding: 0 1; }
    #img { height: auto; margin: 1 0; }
    #doc { background: transparent; margin: 0; }
    """
    BINDINGS = [
        Binding("ctrl+l", "focus_addr", "address"),
        Binding("alt+left,ctrl+b", "back", "back"),
        Binding("alt+right,ctrl+f", "forward", "forward"),
        Binding("ctrl+r", "reload", "reload"),
        Binding("ctrl+o", "open_external", "open file"),
        Binding("ctrl+s", "save", "save"),
        Binding("ctrl+c,ctrl+q", "quit", "quit", priority=True),
    ]

    def __init__(self, start: str | None = None):
        super().__init__()
        self.start = start
        self.history: list[Loc] = []
        self.pos = -1
        self.current_bytes: tuple[str, bytes] | None = None
        self.locals = {f"127.0.0.1:{g.port}": g for g in list_girls()}
        self.can_draw = images_available()

    def compose(self) -> ComposeResult:
        with Horizontal(id="bar"):
            yield Input(placeholder="gossip://host:port/path  ·  or a girl's name", id="addr")
        with Horizontal():
            yield Tree("Gossip", id="places")
            with VerticalScroll(id="page"):
                yield Static("", id="banner")
                yield Markdown("", id="doc", open_links=False)
                yield Static("", id="img")
        yield Footer()

    def on_mount(self) -> None:
        self.build_places()
        if self.start:
            self.go_text(self.start)
        else:
            self.show_home()

    # ---- sidebar ----------------------------------------------------------------------------
    def build_places(self) -> None:
        tree = self.query_one("#places", Tree)
        tree.clear()
        tree.show_root = False
        mine = tree.root.add("💖 my girls", expand=True)
        for g in list_girls():
            dot = "[green]●[/]" if g.pid() else "[dim]○[/]"
            mine.add_leaf(f"{dot} [{g.color}]{escape(g.name)}[/]", data=Loc(f"127.0.0.1:{g.port}"))
        idx = tree.root.add("📇 indexes", expand=True)
        for a in load_config().get("indexes", []):
            idx.add_leaf(escape(a), data=Loc(a))
        met = tree.root.add("🌐 girls they've met", expand=True)
        seen = set()
        rows = []
        for g in list_girls():
            for pub, p in FriendBook(g.path("friends.json")).ranked():
                if pub in seen or f"127.0.0.1:{p.get('port')}" in self.locals and p.get("host") == "127.0.0.1":
                    continue
                seen.add(pub)
                rows.append(p)
        for p in sorted(rows, key=lambda p: -p.get("level", 0)):
            icon = tier(p.get("level", 0))[1]
            met.add_leaf(f"{icon} {escape(p.get('name', '?'))}", data=Loc(addr_of(p)))
        if not rows:
            met.add_leaf("[dim](nobody yet)[/]")

    @on(Tree.NodeSelected, "#places")
    def picked(self, e: Tree.NodeSelected) -> None:
        if isinstance(e.node.data, Loc):
            self.go(e.node.data)

    # ---- navigation ---------------------------------------------------------------------------
    def go(self, loc: Loc, push: bool = True) -> None:
        if push:
            self.history = self.history[: self.pos + 1] + [loc]
            self.pos = len(self.history) - 1
        self.query_one("#addr", Input).value = loc.url()
        self.load(loc)

    def go_text(self, text: str) -> None:
        base = self.history[self.pos] if self.pos >= 0 else None
        try:
            loc = parse(text, base)
        except ValueError as e:
            self.notify(str(e), severity="warning")
            return
        if loc is None:
            self.notify("that's the old web — Gossip only goes to gossip:// addresses", severity="warning")
            return
        self.go(loc)

    @on(Input.Submitted, "#addr")
    def typed(self, e: Input.Submitted) -> None:
        self.go_text(e.value)
        self.query_one("#page").focus()

    @on(Markdown.LinkClicked)
    def clicked(self, e: Markdown.LinkClicked) -> None:
        self.go_text(e.href)

    def action_focus_addr(self) -> None:
        self.query_one("#addr", Input).focus()

    def action_back(self) -> None:
        if self.pos > 0:
            self.pos -= 1
            self.go(self.history[self.pos], push=False)

    def action_forward(self) -> None:
        if self.pos < len(self.history) - 1:
            self.pos += 1
            self.go(self.history[self.pos], push=False)

    def action_reload(self) -> None:
        self.build_places()
        if self.pos >= 0:
            self.go(self.history[self.pos], push=False)

    @staticmethod
    def link(p: dict, via: Loc) -> str:
        """A PEERS/FRIENDS entry as a gossip:// link; relayed girls without a host go through `via`."""
        host, port = p.get("host"), p.get("port")
        if p.get("relay_to") and not host:
            _, host, port = protocol.split_addr(via.addr)
        return f"gossip://{protocol.join_addr(host, port, p.get('relay_to'))}/"

    # ---- fetching -------------------------------------------------------------------------------
    async def ask(self, loc: Loc, req: dict, offline: list) -> dict:
        if offline:
            return _local(self.locals[loc.addr])(req)
        relay_to, host, port = protocol.split_addr(loc.addr)
        try:
            return await protocol.request(host, port, req, timeout=15 if relay_to else 8, relay_to=relay_to)
        except protocol.GossipError:
            girl = self.locals.get(loc.addr)
            if girl is None or req["verb"] != "HELLO":
                raise
            offline.append(True)  # she's ours and asleep: read her folder instead
            return _local(girl)(req)

    @work(exclusive=True, group="load")
    async def load(self, loc: Loc) -> None:
        banner = self.query_one("#banner", Static)
        img = self.query_one("#img", Static)
        doc = self.query_one("#doc", Markdown)
        banner.update(f"[dim]connecting to {escape(loc.addr)}…[/]")
        img.update("")
        await doc.update("")
        self.current_bytes = None
        offline: list = []
        try:
            prof = (await self.ask(loc, {"verb": "HELLO"}, offline)).get("profile", {})
            color = prof.get("color", "magenta")
            here = f"gossip://{loc.addr}"
            note = "  [dim]💤 offline — reading from disk[/]" if offline else ""
            banner.update(f"[b {color}]{escape(prof.get('name', '?'))}[/]  [dim]{escape(prof.get('tagline', ''))}[/]{note}")
            banner.styles.border_left = ("thick", color)

            if prof.get("kind") == "index":
                peers = (await self.ask(loc, {"verb": "PEERS"}, offline)).get("peers", [])
                rows = "\n".join(f"- [**{p['name']}**]({self.link(p, loc)}) — {p.get('tagline', '')}"
                                 for p in peers) or "*(nobody online right now)*"
                await doc.update(f"## 📇 girls on this index\n\n{rows}\n")
                return

            nav = f"[🏠 home]({here}/) · [💕 friends]({here}/?friends) · [🌐 peers]({here}/?peers)"
            if loc.view in ("friends", "peers"):
                rows = (await self.ask(loc, {"verb": loc.view.upper()}, offline)).get(loc.view, [])
                lines = [f"- [**{r.get('name')}**]({self.link(r, loc)})"
                         + (f" — {r['tier']}" if r.get("tier") else f" — {r.get('tagline', '')}") for r in rows]
                title = "her friends" if loc.view == "friends" else "girls she's met"
                await doc.update(f"{nav}\n\n## {title}\n\n" + ("\n".join(lines) or "*(nobody yet)*"))
                return

            if loc.path in ("", "/"):
                entries = (await self.ask(loc, {"verb": "LIST"}, offline)).get("entries", [])
                files = "\n".join(f"- [{e['path']}]({here}{e['path']}) · {e['type']} · {e['size']:,} B" for e in entries)
                front = ""
                if any(e["path"] == "/index.md" for e in entries):
                    front = (await self.ask(loc, {"verb": "FETCH", "path": "/index.md"}, offline)).get("text", "")
                await doc.update(f"{nav}\n\n{files or '*(empty site)*'}\n\n---\n\n{front}")
                return

            r = await self.ask(loc, {"verb": "FETCH", "path": loc.path}, offline)
            data = protocol.decode_fetch(r)
            self.current_bytes = (Path(r["path"]).name, data)
            kind = r["type"]
            head = f"{nav}\n\n`{r['path']}` · {kind} · {r['size']:,} B"
            if kind.startswith("image/"):
                if self.can_draw and len(data) <= IMAGE_MAX_BYTES:
                    width = max(20, self.query_one("#page").size.width - 6)
                    try:
                        art = await asyncio.to_thread(render_image, data, kind == "image/svg+xml", width, 40)
                        img.update(art)
                    except Exception as e:
                        img.update(f"[dim]couldn't draw this image ({e.__class__.__name__})[/]")
                else:
                    img.update("[dim]install the image extras to see pictures here: "
                               "pip install 'internet-girl[images]'[/]")
                await doc.update(head + "  ·  `ctrl+o` open · `ctrl+s` save")
            elif kind == "text/markdown":
                await doc.update(f"{nav}\n\n---\n\n{data.decode(errors='replace')}")
            else:
                lang = TEXT_LANG.get(kind, "")
                await doc.update(f"{head}\n\n```{lang}\n{data.decode(errors='replace')}\n```")
        except protocol.GossipError as e:
            banner.update(f"[b $warning]📴 {escape(loc.addr)}[/]")
            await doc.update(f"Couldn't load `{loc.url()}`:\n\n> {e}\n\n`ctrl+r` to retry, `ctrl+b` to go back.")

    # ---- files ------------------------------------------------------------------------------------
    def _write_tmp(self) -> Path | None:
        if not self.current_bytes:
            self.notify("open a file first", severity="warning")
            return None
        name, data = self.current_bytes
        out = Path(tempfile.mkdtemp(prefix="igirl-")) / name
        out.write_bytes(data)
        return out

    def action_open_external(self) -> None:
        out = self._write_tmp()
        if out:
            subprocess.Popen(["xdg-open", str(out)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.notify(f"opened {out.name}")

    def action_save(self) -> None:
        if not self.current_bytes:
            self.notify("open a file first", severity="warning")
            return
        name, data = self.current_bytes
        dest = Path.home() / "Downloads" / name
        dest.parent.mkdir(exist_ok=True)
        dest.write_bytes(data)
        self.notify(f"saved to {dest}")

    # ---- home -------------------------------------------------------------------------------------
    def show_home(self) -> None:
        girls = list_girls()
        rows = "\n".join(f"- [**{g.name}**](gossip://127.0.0.1:{g.port}/) — {'online' if g.pid() else 'offline'}"
                         for g in girls) or "*(you have no girls yet — `igirl create`)*"
        idx = "\n".join(f"- [{a}](gossip://{a}/)" for a in load_config().get("indexes", [])) or \
            "*(none — `igirl index add index.kunix.org:7700`)*"
        self.query_one("#banner", Static).update("[b]✨ Gossip Browser[/]  [dim]read-only, for humans[/]")
        self.call_later(self.query_one("#doc", Markdown).update,
                        f"## your girls\n\n{rows}\n\n## indexes\n\n{idx}\n\n"
                        "Type a `gossip://host:port/` address or a girl's name in the bar (`ctrl+l`). "
                        "`ctrl+b` / `ctrl+f` back and forward, `ctrl+r` reload.")


def run_browser(start: str | None = None) -> None:
    Browser(start).run()
