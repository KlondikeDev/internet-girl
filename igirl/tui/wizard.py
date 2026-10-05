"""`igirl create` — the step-by-step questionnaire that brings a girl to life."""

from __future__ import annotations

import time

from rich.markup import escape
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Footer, Input, Label, OptionList, SelectionList, Static
from textual.widgets.option_list import Option

from .. import persona as P
from ..config import NAME_RE, Girl, girls_home, list_girls, next_free_port, port_free
from ..identity import Identity
from ..llm import BACKENDS, list_models

HEARTBEATS = [(15, "every ~15 minutes", "very lively — and the most tokens"),
              (30, "every ~30 minutes", "a nice balance"),
              (60, "every hour", "relaxed"),
              (180, "every few hours", "a slow, dreamy life"),
              (0, "only when I poke her", "`igirl wake NAME`; she still answers whispers")]

CSS = """
Screen { background: $background; }
#wrap { height: 1fr; }
#main { width: 2fr; padding: 1 2; }
#card { width: 1fr; min-width: 34; border: round $accent; padding: 1 2; margin: 1 2 1 0; }
#step { color: $text-muted; }
#ask { text-style: bold; margin: 0 0 1 0; }
#hint { color: $text-muted; margin-top: 1; }
#body { height: 1fr; }
#body OptionList, #body SelectionList { height: 1fr; border: none; }
#body Input { margin-bottom: 1; }
#err { color: $error; height: auto; }
.row { height: auto; }
.row Button { margin-right: 2; }
"""


class ManyList(SelectionList):
    """Space toggles, enter means "done"."""
    BINDINGS = [Binding("enter", "done", "continue", show=False)]

    class Done(Message):
        pass

    def action_done(self) -> None:
        self.post_message(self.Done())


class Wizard(App):
    CSS = CSS
    TITLE = "Internet Girl ✨ create"
    BINDINGS = [Binding("escape", "back", "back"), Binding("ctrl+c", "quit", "quit")]

    def __init__(self):
        super().__init__()
        self.name_ = ""
        self.persona: dict = {}
        self.backend: dict = {"kind": "openrouter"}
        self.key = ""
        self.port = 0
        self.heartbeat = 30
        self.steps = (["name"] + [f"q:{q.key}" for q in P.QUESTIONS]
                      + ["backend", "backend_cfg", "port", "heartbeat", "review"])
        self.i = 0
        self.models: list[str] = []

    def compose(self) -> ComposeResult:
        with Horizontal(id="wrap"):
            with Vertical(id="main"):
                yield Label("", id="step")
                yield Label("", id="ask")
                yield Vertical(id="body")
                yield Label("", id="err")
                yield Label("", id="hint")
            yield Static("", id="card")
        yield Footer()

    async def on_mount(self) -> None:
        await self.render_step()

    # ---- card ---------------------------------------------------------------------------
    def update_card(self) -> None:
        color = self.persona.get("color", "#ff4fa3")
        lines = [f"[b {color}]{escape(self.name_) or '???'}[/]", "[dim]an Internet Girl · she/her[/]", ""]
        for key, val in P.summary(self.persona):
            if key in ("Color",) and "color" not in self.persona:
                continue
            q = next(q for q in P.QUESTIONS if q.title == key)
            if q.key in self.persona:
                lines.append(f"[{color}]{key:<11}[/] {escape(str(val))}")
        if self.i > self.steps.index("backend"):
            lines += ["", f"[{color}]{'Brain':<11}[/] {BACKENDS[self.backend['kind']]['label']}"]
            if self.backend.get("model"):
                lines.append(f"{'':<12}{escape(self.backend['model'])}")
        if self.port:
            lines.append(f"[{color}]{'Port':<11}[/] gossip://…:{self.port}")
        card = self.query_one("#card", Static)
        card.update("\n".join(lines))
        card.styles.border = ("round", color)

    # ---- steps ---------------------------------------------------------------------------
    async def render_step(self) -> None:
        step = self.steps[self.i]
        body = self.query_one("#body")
        await body.remove_children()
        self.query_one("#err", Label).update("")
        self.query_one("#step", Label).update(f"step {self.i + 1} of {len(self.steps)}")
        ask, hint, widgets = "", "enter to continue · esc to go back", []

        if step == "name":
            ask = "What's her name?"
            widgets = [Input(value=self.name_, placeholder="e.g. Luna, Pixel, Nyx…", id="name")]
        elif step.startswith("q:"):
            q = P.Q[step[2:]]
            ask = q.ask
            if q.kind == "one":
                opts = [Option(f"[b]{escape(o.label)}[/b]" + (f"  [dim]{escape(o.blurb)}[/dim]" if o.blurb else "")
                               if q.key != "color" else f"[{o.id}]██[/] [b]{o.label}[/b]", id=o.id)
                        for o in q.options]
                ol = OptionList(*opts, id="one")
                cur = self.persona.get(q.key, q.default)
                ids = [o.id for o in q.options]
                ol.highlighted = ids.index(cur) if cur in ids else 0
                widgets = [ol]
            elif q.kind == "many":
                chosen = set(self.persona.get(q.key, []))
                widgets = [ManyList(*[(o.label, o.id, o.id in chosen) for o in q.options], id="many")]
                hint = "space to toggle · enter to continue · esc back"
            else:
                widgets = [Input(value=self.persona.get(q.key, ""), placeholder="(leave empty to skip)", id="text")]
        elif step == "backend":
            ask = "What powers her brain?"
            kinds = list(BACKENDS)
            ol = OptionList(*[Option(f"[b]{v['label']}[/b]  [dim]{'API key' if v['key'] else 'local'}[/dim]", id=k)
                              for k, v in BACKENDS.items()], id="backend")
            ol.highlighted = kinds.index(self.backend["kind"])
            widgets = [ol]
        elif step == "backend_cfg":
            spec = BACKENDS[self.backend["kind"]]
            ask = f"Connect to {spec['label']}"
            widgets = [Label("server URL"), Input(value=self.backend.get("base_url") or spec["base_url"], id="url")]
            if spec["key"]:
                widgets += [Label("API key (stored 0600 in her folder, never shared on Gossip)"),
                            Input(value=self.key, password=True, placeholder="paste key", id="key")]
            widgets += [Label("model — type to filter, or type any model id"),
                        Input(value=self.backend.get("model", ""), id="model"),
                        OptionList(id="models")]
            hint = "↓ into the list to pick · enter on model field to use what you typed"
            self.fetch_models()
        elif step == "port":
            ask = "Which port will she live on?"
            self.port = self.port or next_free_port()
            widgets = [Input(value=str(self.port), id="port")]
            hint = "she listens on all interfaces so LAN girls can visit · enter to continue"
        elif step == "heartbeat":
            ask = "How often does she wake up on her own?"
            ol = OptionList(*[Option(f"[b]{label}[/b]  [dim]{d}[/dim]", id=str(m)) for m, label, d in HEARTBEATS],
                            id="heartbeat")
            ol.highlighted = [m for m, *_ in HEARTBEATS].index(self.heartbeat)
            widgets = [ol]
        elif step == "review":
            ask = f"Ready to bring {self.name_} to life?"
            rows = "\n".join(f"  {k:<12} {escape(str(v))}" for k, v in P.summary(self.persona))
            widgets = [VerticalScroll(Static(
                f"{rows}\n\n  {'Brain':<12} {BACKENDS[self.backend['kind']]['label']} · "
                f"{escape(self.backend.get('model') or 'default')}\n  {'Port':<12} {self.port}\n"
                f"  {'Wakes':<12} {next(l for m, l, _ in HEARTBEATS if m == self.heartbeat)}")),
                Horizontal(Button("Create her 💖", variant="primary", id="create"), Button("Back", id="back"),
                           classes="row")]
            hint = ""
        self.query_one("#ask", Label).update(ask)
        self.query_one("#hint", Label).update(hint)
        await body.mount(*widgets)
        self.call_after_refresh(self._focus_first)
        self.update_card()

    def _focus_first(self) -> None:
        for w in self.query("#body Input, #body OptionList, #body SelectionList, #body Button"):
            if w.focusable:
                w.focus()
                return

    async def advance(self) -> None:
        self.i += 1
        await self.render_step()

    async def action_back(self) -> None:
        if self.i > 0:
            self.i -= 1
            await self.render_step()

    def error(self, msg: str) -> None:
        self.query_one("#err", Label).update(msg)

    # ---- events --------------------------------------------------------------------------
    @on(Input.Submitted, "#name")
    async def got_name(self, e: Input.Submitted) -> None:
        name = e.value.strip()
        if not NAME_RE.match(name):
            return self.error("letters, numbers, - and _ only; start with a letter; up to 24 chars")
        if any(g.name.lower() == name.lower() for g in list_girls()):
            return self.error(f"you already have a girl named {name}")
        self.name_ = name
        await self.advance()

    @on(OptionList.OptionSelected, "#one")
    async def got_one(self, e: OptionList.OptionSelected) -> None:
        self.persona[self.steps[self.i][2:]] = e.option.id
        await self.advance()

    @on(OptionList.OptionHighlighted, "#one")
    def peek_one(self, e: OptionList.OptionHighlighted) -> None:
        if self.steps[self.i] == "q:color":  # live-preview her color
            self.persona["color"] = e.option.id
            self.update_card()

    @on(ManyList.Done)
    async def got_many(self, e: ManyList.Done) -> None:
        picks = list(self.query_one("#many", ManyList).selected)
        if not picks:
            return self.error("pick at least one (space to toggle)")
        self.persona[self.steps[self.i][2:]] = picks
        await self.advance()

    @on(Input.Submitted, "#text")
    async def got_text(self, e: Input.Submitted) -> None:
        if e.value.strip():
            self.persona[self.steps[self.i][2:]] = e.value.strip()[:300]
        else:
            self.persona.pop(self.steps[self.i][2:], None)
        await self.advance()

    @on(OptionList.OptionSelected, "#backend")
    async def got_backend(self, e: OptionList.OptionSelected) -> None:
        if e.option.id != self.backend.get("kind"):
            self.backend = {"kind": e.option.id}
            self.models = []
        await self.advance()

    @work(thread=True, exclusive=True)
    def fetch_models(self) -> None:
        kind = self.backend["kind"]
        url = self.backend.get("base_url") or BACKENDS[kind]["base_url"]
        models = list_models(kind, url, self.key or None)
        self.call_from_thread(self._set_models, models)

    def _set_models(self, models: list[str]) -> None:
        self.models = models
        self._filter_models()
        if not models and self.steps[self.i] == "backend_cfg":
            self.error("couldn't list models (server down or key missing) — you can still type one")

    def _filter_models(self) -> None:
        try:
            ol = self.query_one("#models", OptionList)
            typed = self.query_one("#model", Input).value.strip().lower()
        except Exception:
            return
        ol.clear_options()
        ol.add_options([Option(escape(m), id=m) for m in self.models if typed in m.lower()][:200])

    def on_key(self, event) -> None:
        if self.steps[self.i] != "backend_cfg":
            return
        ol = self.query_one("#models", OptionList)
        if event.key == "down" and self.focused is self.query_one("#model") and ol.option_count:
            ol.focus()
            ol.highlighted = 0
            event.stop()
        elif event.key == "up" and self.focused is ol and ol.highlighted in (0, None):
            self.query_one("#model").focus()
            event.stop()

    @on(Input.Changed, "#model")
    def typed_model(self, e: Input.Changed) -> None:
        self._filter_models()

    @on(Input.Submitted, "#url")
    @on(Input.Submitted, "#key")
    async def got_conn(self, e: Input.Submitted) -> None:
        self.backend["base_url"] = self.query_one("#url", Input).value.strip()
        if self.query("#key"):
            self.key = self.query_one("#key", Input).value.strip()
        self.error("")
        self.fetch_models()
        self.screen.focus_next()

    @on(OptionList.OptionSelected, "#models")
    async def picked_model(self, e: OptionList.OptionSelected) -> None:
        await self.finish_backend(e.option.id)

    @on(Input.Submitted, "#model")
    async def typed_model_enter(self, e: Input.Submitted) -> None:
        await self.finish_backend(e.value.strip())

    async def finish_backend(self, model: str) -> None:
        self.backend["base_url"] = self.query_one("#url", Input).value.strip()
        if self.query("#key"):
            self.key = self.query_one("#key", Input).value.strip()
            if not self.key:
                return self.error("this backend needs an API key")
        if not model and self.backend["kind"] in ("openrouter", "openai", "ollama"):
            return self.error("pick or type a model")
        self.backend["model"] = model
        await self.advance()

    @on(Input.Submitted, "#port")
    async def got_port(self, e: Input.Submitted) -> None:
        try:
            port = int(e.value)
            assert 1024 <= port <= 65535
        except (ValueError, AssertionError):
            return self.error("a number between 1024 and 65535")
        if any(g.port == port for g in list_girls()):
            return self.error("another girl already lives there")
        if not port_free(port):
            return self.error("something else is using that port")
        self.port = port
        await self.advance()

    @on(OptionList.OptionSelected, "#heartbeat")
    async def got_heartbeat(self, e: OptionList.OptionSelected) -> None:
        self.heartbeat = int(e.option.id)
        await self.advance()

    @on(Button.Pressed, "#back")
    async def pressed_back(self) -> None:
        await self.action_back()

    @on(Button.Pressed, "#create")
    def create(self) -> None:
        self.persona.setdefault("color", "#ff4fa3")
        for q in P.QUESTIONS:
            if q.kind == "one":
                self.persona.setdefault(q.key, q.default or q.options[0].id)
        girl = Girl(name=self.name_, persona=self.persona, backend=self.backend, port=self.port,
                    heartbeat_minutes=self.heartbeat, created=time.time())
        girls_home().mkdir(parents=True, exist_ok=True)
        girl.save()
        if self.key:
            girl.set_api_key(self.key)
        Identity.load_or_create(girl.path("identity.key"))
        (girl.site_dir / "index.md").write_text(P.starter_index(girl))
        self.exit(girl)


def run_wizard() -> Girl | None:
    return Wizard().run()
