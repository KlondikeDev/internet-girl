"""`igirl chat NAME` — talk with her. OpenCode-style: streaming replies, visible tool calls,
approval prompts for anything risky, slash commands."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from rich.markup import escape
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Collapsible, Footer, Input, Label, Markdown, Static

from .. import agent, persona, site
from .. import tools as T
from ..config import Girl, atomic_write
from ..friends import tier
from ..gossip import Me
from ..llm import LLM, LLMError

HISTORY_KEEP = 80

HELP = """**Slash commands**
- `/site` — what she's hosting on Gossip right now
- `/friends` — her friendbook · `/whispers` — her recent girl-to-girl messages
- `/up` · `/down` — bring her node online / offline · `/wake` — poke her to do something on her own
- `/model [id]` — switch her brain (no id = searchable picker); her node picks it up too
- `/tools` — what she can do (⚠ = asks you first) · `/cwd PATH` — change her working directory
- `/clear` — forget this conversation · `/exit` — leave (`ctrl+c` works too)

`esc` interrupts her mid-reply."""


class Approve(ModalScreen[str]):
    BINDINGS = [Binding("y", "pick('yes')", "yes"), Binding("a", "pick('always')", "always"),
                Binding("n,escape", "pick('no')", "no")]
    DEFAULT_CSS = """
    Approve { align: center middle; }
    #box { width: 90%; max-width: 110; height: auto; max-height: 80%; border: thick $warning; padding: 1 2;
           background: $surface; }
    #detail { max-height: 20; margin: 1 0; }
    #btns { height: auto; } #btns Button { margin-right: 2; }
    """

    def __init__(self, girl: str, tool: T.Tool, args: dict):
        super().__init__()
        self.girl, self.tool, self.args = girl, tool, args

    def compose(self) -> ComposeResult:
        detail = self.tool.preview(self.args)
        if self.tool.name == "write_file":
            detail += "\n\n" + str(self.args.get("content", ""))[:3000]
        elif self.tool.name == "edit_file":
            detail += f"\n\n- {self.args.get('old', '')[:1500]}\n+ {self.args.get('new', '')[:1500]}"
        with Vertical(id="box"):
            yield Label(f"[b]{escape(self.girl)} wants to use [reverse] {self.tool.name} [/][/b]")
            with VerticalScroll(id="detail"):
                yield Static(escape(detail))
            with Horizontal(id="btns"):
                yield Button("[y]es", variant="success", id="yes")
                yield Button("[a]lways this session", variant="primary", id="always")
                yield Button("[n]o", variant="error", id="no")

    def action_pick(self, choice: str) -> None:
        self.dismiss(choice)

    @on(Button.Pressed)
    def pressed(self, e: Button.Pressed) -> None:
        self.dismiss(e.button.id)


class Bubble(Static):
    pass


class Thinking(Static):
    """'Fumie is thinking… 12s' plus the tail of her reasoning, so slow models don't look dead."""

    def __init__(self, name: str):
        super().__init__("", classes="thinking")
        self.who = name
        self.t0 = time.monotonic()
        self.thoughts = ""
        self.status = ""

    def on_mount(self) -> None:
        self.tick()
        self.set_interval(0.5, self.tick)

    def tick(self) -> None:
        dots = "·" * (1 + int(time.monotonic() * 2) % 3)
        tail = " ".join(self.thoughts.split())[-90:]
        extra = f"  [$warning]{escape(self.status)}[/]" if self.status else (
            f"  [dim i]…{escape(tail)}[/]" if tail else "")
        self.update(f"{escape(self.who)} is thinking {dots} {time.monotonic() - self.t0:.0f}s{extra}")


class ModelPicker(ModalScreen[str | None]):
    """Type to filter, ↓ into the list to pick, enter on the field to use exactly what you typed."""
    BINDINGS = [Binding("escape", "dismiss(None)", "cancel")]
    DEFAULT_CSS = """
    ModelPicker { align: center middle; }
    #mbox { width: 90%; max-width: 100; height: 80%; border: thick $accent; padding: 1 2; background: $surface; }
    #mlist { height: 1fr; }
    """

    def __init__(self, girl: Girl):
        super().__init__()
        self.girl = girl
        self.models: list[str] = []

    def compose(self) -> ComposeResult:
        from textual.widgets import OptionList
        with Vertical(id="mbox"):
            yield Label(f"[b]{escape(self.girl.name)}'s model[/b]  [dim]{escape(self.girl.backend.get('kind', ''))} · "
                        f"now: {escape(self.girl.backend.get('model') or 'default')}[/dim]")
            yield Input(value="", placeholder="type to filter, or type any model id and press enter", id="mfilter")
            yield OptionList(id="mlist")
            yield Label("[dim]loading models…[/dim]", id="mstatus")

    def on_mount(self) -> None:
        self.query_one("#mfilter").focus()
        self.load()

    @work(thread=True)
    def load(self) -> None:
        from ..llm import BACKENDS, list_models
        b = self.girl.backend
        models = list_models(b.get("kind", ""), b.get("base_url") or BACKENDS[b["kind"]]["base_url"], self.girl.api_key())
        self.app.call_from_thread(self._loaded, models)

    def _loaded(self, models: list[str]) -> None:
        self.models = models
        self.query_one("#mstatus", Label).update(
            f"[dim]{len(models)} models · ↓ to pick · enter on the field uses what you typed · esc cancels[/dim]"
            if models else "[dim]couldn't list models — type an id and press enter[/dim]")
        self._filter()

    def _filter(self) -> None:
        from textual.widgets import OptionList
        from textual.widgets.option_list import Option
        typed = self.query_one("#mfilter", Input).value.strip().lower()
        ol = self.query_one("#mlist", OptionList)
        ol.clear_options()
        ol.add_options([Option(escape(m), id=m) for m in self.models if typed in m.lower()][:300])

    def on_key(self, event) -> None:
        from textual.widgets import OptionList
        ol = self.query_one("#mlist", OptionList)
        if event.key == "down" and self.focused is self.query_one("#mfilter") and ol.option_count:
            ol.focus()
            ol.highlighted = 0
            event.stop()
        elif event.key == "up" and self.focused is ol and ol.highlighted in (0, None):
            self.query_one("#mfilter").focus()
            event.stop()

    @on(Input.Changed, "#mfilter")
    def changed(self) -> None:
        self._filter()

    @on(Input.Submitted, "#mfilter")
    def typed(self, e: Input.Submitted) -> None:
        self.dismiss(e.value.strip() or None)

    def on_option_list_option_selected(self, e) -> None:
        self.dismiss(e.option.id)


class ChatApp(App):
    BINDINGS = [Binding("ctrl+c", "quit", "quit", priority=True), Binding("escape", "interrupt", "interrupt"),
                Binding("ctrl+l", "clear_screen", "clear screen")]

    def __init__(self, girl: Girl, cwd: Path):
        super().__init__()
        self.girl = girl
        self.me = Me(girl)
        self.ctx = T.Ctx(me=self.me, cwd=cwd)
        self.history: list[dict] = self.load_history()
        self.always: set[str] = set()
        self.llm: LLM | None = None
        self.busy = False
        c = girl.color
        self.CSS = f"""
        Screen {{ background: $background; }}
        #top {{ height: 1; background: {c} 25%; color: {c}; padding: 0 1; text-style: bold; }}
        #log {{ height: 1fr; padding: 0 1; scrollbar-color: {c} 40%; }}
        .user {{ margin: 1 0 0 0; color: $text; }}
        .thinking {{ color: {c} 70%; padding: 0 0 0 2; margin: 1 0 0 0; }}
        .her-name {{ color: {c}; text-style: bold; margin: 1 0 0 0; }}
        .her Markdown, Markdown.her {{ margin: 0; padding: 0 0 0 2; background: transparent; }}
        .tool {{ color: $text-muted; padding: 0 0 0 2; }}
        .tool-err {{ color: $warning; padding: 0 0 0 2; }}
        .note {{ color: $text-muted; margin: 1 0 0 0; padding: 0 0 0 2; }}
        Collapsible {{ padding: 0 0 0 2; border: none; background: transparent; margin: 0; }}
        Collapsible Static {{ color: $text-muted; }}
        #prompt {{ border: tall {c} 60%; margin: 0 1; }}
        #prompt:focus {{ border: tall {c}; }}
        """

    # ---- persistence ------------------------------------------------------------------------
    def load_history(self) -> list[dict]:
        try:
            return json.loads(self.girl.path("history.json").read_text())
        except (OSError, json.JSONDecodeError):
            return []

    def save_history(self) -> None:
        h = self.history[-HISTORY_KEEP:]
        while h and h[0]["role"] != "user":  # never start mid tool-exchange
            h = h[1:]
        self.history = h
        atomic_write(self.girl.path("history.json"), json.dumps(h, ensure_ascii=False))

    # ---- layout --------------------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Label("", id="top")
        yield VerticalScroll(id="log")
        yield Input(placeholder=f"talk to {self.girl.name}…  (/help)", id="prompt")
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"{self.girl.name} ✨"
        self.refresh_top()
        self.set_interval(5, self.refresh_top)
        try:
            self.llm = LLM(self.girl.backend, self.girl.api_key())
        except LLMError as e:
            self.note(f"⚠️ {e}  Fix with `igirl set {self.girl.name} api_key …`")
        # replay recent conversation
        for m in self.history[-20:]:
            if m["role"] == "user":
                self.add_user(m["content"])
            elif m["role"] == "assistant" and m.get("content"):
                self.add_her(m["content"])
        if not self.history:
            self.note(f"{self.girl.name} · {persona.tagline(self.girl.persona)} — say hi (/help for commands)")
        self.query_one("#prompt").focus()

    def refresh_top(self) -> None:
        if not self.query("#top"):  # a modal is up, or we're shutting down
            return
        online = f"● online :{self.girl.port}" if self.girl.pid() else "○ offline (/up)"
        brain = self.llm.describe() if self.llm else "no brain"
        n = len(self.me.friends.peers)
        unread = len(self.me.unread())
        self.query_one("#top", Label).update(
            f"✨ {escape(self.girl.name)} · {online} · {n} girls known"
            + (f" · 💌 {unread}" if unread else "") + f" · {escape(brain)} · {escape(str(self.ctx.cwd))}")

    def log_widget(self) -> VerticalScroll:
        return self.query_one("#log", VerticalScroll)

    def mount_log(self, w) -> None:
        log = self.log_widget()
        log.mount(w)
        log.scroll_end(animate=False)

    def add_user(self, text: str) -> None:
        self.mount_log(Bubble(f"[b $text-muted]›[/] {escape(text)}", classes="user"))

    def add_her(self, text: str) -> Markdown:
        self.mount_log(Label(self.girl.name, classes="her-name"))
        md = Markdown(text, classes="her")
        self.mount_log(md)
        return md

    def note(self, text: str, md: bool = False) -> None:
        self.mount_log(Markdown(text, classes="note") if md else Static(escape(text), classes="note"))

    # ---- input ---------------------------------------------------------------------------------
    @on(Input.Submitted, "#prompt")
    def submitted(self, e: Input.Submitted) -> None:
        text = e.value.strip()
        e.input.value = ""
        if not text:
            return
        if text.startswith("/"):
            self.slash(text)
            return
        if self.busy:
            self.note("she's still talking — esc to interrupt")
            return
        if not self.llm:
            self.note("she has no brain configured")
            return
        self.add_user(text)
        self.history.append({"role": "user", "content": text})
        self.respond()

    def action_interrupt(self) -> None:
        if self.busy:
            self.workers.cancel_group(self, "respond")

    def action_clear_screen(self) -> None:
        self.log_widget().remove_children()

    # ---- the agent turn ----------------------------------------------------------------------------
    @work(exclusive=True, group="respond")
    async def respond(self) -> None:
        self.busy = True
        md: Markdown | None = None
        buf: list[str] = []
        last = 0.0
        thinking: Thinking | None = None

        def think_start():
            nonlocal thinking
            think_stop()
            thinking = Thinking(self.girl.name)
            self.mount_log(thinking)

        def think_stop():
            nonlocal thinking
            if thinking is not None:
                thinking.remove()
                thinking = None

        def on_status(msg: str):
            if thinking is not None:
                thinking.status = msg

        self.llm.on_status = on_status

        def on_reasoning(piece: str):
            if thinking is not None:
                thinking.thoughts = (thinking.thoughts + piece)[-400:]

        async def on_text(piece: str):
            nonlocal md, last
            think_stop()
            if md is None:
                md = self.add_her("")
            buf.append(piece)
            if time.monotonic() - last > 0.08:
                last = time.monotonic()
                await md.update("".join(buf))
                self.log_widget().scroll_end(animate=False)

        async def on_step_text(final: str):
            nonlocal md
            if md is not None:
                await md.update(final or "".join(buf))
            elif final:
                md = self.add_her(final)
            md = None
            buf.clear()

        def on_tool(tool, name, args):
            think_stop()
            prev = tool.preview(args) if tool else name
            mark = "⚠" if tool and tool.risk == "ask" and name not in self.always else "⚙"
            self.mount_log(Static(f"{mark} {escape(prev)}", classes="tool"))

        def on_result(name, out, ok):
            lines = out.strip().splitlines() or ["(no output)"]
            if len(lines) == 1:
                self.mount_log(Static(f"  ↳ {escape(lines[0][:200])}", classes="tool"))
            else:
                title = f"{lines[0][:90]}  (+{len(lines) - 1} lines)"
                self.mount_log(Collapsible(Static(escape("\n".join(lines[1:]))), title=escape(title), collapsed=True))
            think_start()

        async def approve(tool: T.Tool, args: dict) -> bool:
            if tool.name in self.always:
                return True
            choice = await self.push_screen_wait(Approve(self.girl.name, tool, args))
            if choice == "always":
                self.always.add(tool.name)
            return choice in ("yes", "always")

        notes_p = self.girl.path("notes.md")
        entries = site.listing(self.girl.site_dir)
        site_txt = "\n".join(f"- {e['path']} ({e['type']}, {e['size']} B)" for e in entries) or "(empty)"
        front = self.girl.site_dir / "index.md"
        if front.exists():
            site_txt += "\n\nYour /index.md currently says:\n" + front.read_text(errors="replace")[:1500]
        system = persona.system_prompt(self.girl, "chat", cwd=self.ctx.cwd, site=site_txt,
                                       friends=self.me.friends.summary(10),
                                       notes=notes_p.read_text()[-2500:] if notes_p.exists() else "(nothing yet)")
        think_start()
        try:
            await agent.run(self.llm, system, self.history, T.for_scope("chat"), self.ctx, approve=approve,
                            on_text=on_text, on_tool=on_tool, on_result=on_result, on_step_text=on_step_text,
                            on_reasoning=on_reasoning)
        except LLMError as e:
            self.note(f"⚠️ {e}")
            if self.history and self.history[-1]["role"] == "user":
                self.history.pop()
        except asyncio.CancelledError:
            self.note("(interrupted)")
            self.repair_history()
            raise
        finally:
            think_stop()
            self.busy = False
            self.save_history()
            self.refresh_top()

    def repair_history(self) -> None:
        """After an interrupt, make sure every tool call has a result so the history stays valid."""
        answered = {m.get("tool_call_id") for m in self.history if m["role"] == "tool"}
        for m in list(self.history):
            for c in m.get("tool_calls") or []:
                if c["id"] not in answered:
                    self.history.append({"role": "tool", "tool_call_id": c["id"], "name": c["function"]["name"],
                                         "content": "(interrupted by the human)"})

    def set_model(self, model: str) -> None:
        if self.busy:
            self.note("wait for her to finish (or esc) before switching brains")
            return
        self.girl.backend["model"] = model
        self.girl.save()
        try:
            self.llm = LLM(self.girl.backend, self.girl.api_key())
        except LLMError as e:
            self.note(f"⚠️ {e}")
            return
        self.note(f"🧠 now thinking with {model}" + (" — her node picks it up on its next turn" if self.girl.pid() else ""))
        self.refresh_top()

    # ---- slash commands ---------------------------------------------------------------------------
    def slash(self, text: str) -> None:
        cmd, _, arg = text[1:].partition(" ")
        cmd = cmd.lower()
        g = self.girl
        if cmd in ("exit", "quit", "q"):
            self.exit()
        elif cmd == "help":
            self.note(HELP, md=True)
        elif cmd == "clear":
            self.history = []
            self.save_history()
            self.action_clear_screen()
            self.note("fresh start ✨")
        elif cmd == "tools":
            lines = [f"- {'⚠ ' if t.risk == 'ask' else ''}`{t.name}` — {t.description}" for t in T.for_scope("chat")]
            self.note("**What she can do** (⚠ asks you first)\n" + "\n".join(lines), md=True)
        elif cmd == "site":
            entries = site.listing(g.site_dir)
            rows = "\n".join(f"- `{e['path']}` {e['type']}, {e['size']:,} B" for e in entries) or "(empty)"
            idx = g.site_dir / "index.md"
            front = f"\n\n---\n{idx.read_text()[:3000]}" if idx.exists() else ""
            self.note(f"**{g.name}'s site** (gossip port {g.port})\n{rows}{front}", md=True)
        elif cmd == "friends":
            rows = []
            for pub, p in self.me.friends.ranked()[:25]:
                t, icon = tier(p.get("level", 0))
                rows.append(f"{icon} {p.get('name'):<16} {p.get('level', 0):5.0f}  {t:<13} {p.get('host')}:{p.get('port')}")
            self.note("\n".join(rows) or "she hasn't met anyone yet")
        elif cmd == "whispers":
            ws = self.me.whispers()[-15:]
            self.note("\n".join(f"{'→ ' + w['name'] if w['dir'] == 'out' else w['name'] + ' →'}: {w['text']}"
                                for w in ws) or "no whispers yet")
        elif cmd == "up":
            subprocess.run([sys.executable, "-m", "igirl.cli", "up", g.name], capture_output=True)
            self.note("✨ online" if g.pid() else "couldn't start — check `igirl log`")
            self.refresh_top()
        elif cmd == "down":
            pid = g.pid()
            if pid:
                os.kill(pid, signal.SIGTERM)
            self.note("💤 offline")
            self.set_timer(1, self.refresh_top)
        elif cmd == "wake":
            pid = g.pid()
            if pid:
                os.kill(pid, signal.SIGUSR1)
                self.note(f"👉 poked — `igirl log {g.name} -f` to watch what she does")
            else:
                self.note("she's offline — /up first")
        elif cmd == "model":
            if arg:
                self.set_model(arg.strip())
            else:
                def picked(m):
                    if m:
                        self.set_model(m)
                    self.query_one("#prompt").focus()
                self.push_screen(ModelPicker(g), picked)
        elif cmd == "cwd":
            p = (self.ctx.cwd / Path(arg).expanduser()).resolve() if arg else self.ctx.cwd
            if p.is_dir():
                self.ctx.cwd = p
                self.note(f"working directory: {p}")
                self.refresh_top()
            else:
                self.note(f"not a directory: {p}")
        else:
            self.note(f"unknown command /{cmd} — try /help")


def run_chat(girl: Girl, cwd: Path) -> None:
    ChatApp(girl, cwd).run()
