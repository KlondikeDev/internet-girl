"""Global config + per-girl storage layout.

~/.config/internet-girl/config.json   -> where girls live, manual peers, index nodes
<home>/<name>/girl.json               -> persona, backend, port, heartbeat
<home>/<name>/secret.key              -> API key (0600), only for API backends
<home>/<name>/identity.key            -> Ed25519 private key (0600)
<home>/<name>/site/                   -> what she hosts on Gossip (humans: read only)
<home>/<name>/friends.json            -> friendbook with friendship levels
<home>/<name>/whispers.jsonl          -> girl-to-girl messages, in and out
<home>/<name>/history.json            -> chat history with her human
<home>/<name>/notes.md                -> her private memory
<home>/<name>/node.pid / node.log     -> daemon bookkeeping
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path

def _home() -> Path:
    # Services running as a DynamicUser have no home directory; don't crash at import.
    try:
        return Path.home()
    except RuntimeError:
        return Path(os.environ.get("STATE_DIRECTORY", "/tmp"))


CONFIG_DIR = Path(os.environ.get("IGIRL_CONFIG_DIR") or _home() / ".config" / "internet-girl")
CONFIG_PATH = CONFIG_DIR / "config.json"
DEFAULT_HOME = _home() / ".local" / "share" / "internet-girls"

DISCOVERY_PORT = 7770
FIRST_GIRL_PORT = 7771
INDEX_PORT = 7700
PUBLIC_INDEX = "index.kunix.org:7700"

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,23}$")


def atomic_write(path: Path, data: str, mode: int | None = None) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(data)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def load_config() -> dict:
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text())
    else:
        cfg = {}
    cfg.setdefault("home", None)
    cfg.setdefault("peers", [])          # ["host:port", ...] manual peers
    cfg.setdefault("indexes", [])        # ["index.kunix.org:7700", ...]
    cfg.setdefault("lan_discovery", True)
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write(CONFIG_PATH, json.dumps(cfg, indent=2) + "\n")


def is_installed() -> bool:
    return bool(load_config().get("home"))


def girls_home() -> Path:
    home = load_config().get("home")
    if not home:
        raise SystemExit("Internet Girl isn't installed yet — run `igirl install` first.")
    return Path(home).expanduser()


def list_girls() -> list["Girl"]:
    home = girls_home()
    if not home.exists():
        return []
    out = []
    for d in sorted(home.iterdir()):
        if (d / "girl.json").exists():
            out.append(Girl.load(d.name))
    return out


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def next_free_port() -> int:
    taken = {g.port for g in list_girls()}
    p = FIRST_GIRL_PORT
    while p in taken or not port_free(p):
        p += 1
    return p


@dataclass
class Girl:
    name: str
    persona: dict = field(default_factory=dict)
    backend: dict = field(default_factory=dict)
    port: int = FIRST_GIRL_PORT
    heartbeat_minutes: int = 30          # 0 = only wakes when poked
    solo_turns_per_hour: int = 12        # autonomy budget: heartbeats + whisper replies (chat isn't counted)
    created: float = field(default_factory=time.time)

    # ---- paths -------------------------------------------------------------
    @property
    def dir(self) -> Path:
        return girls_home() / self.name

    @property
    def site_dir(self) -> Path:
        return self.dir / "site"

    def path(self, name: str) -> Path:
        return self.dir / name

    # ---- persistence -------------------------------------------------------
    @classmethod
    def load(cls, name: str) -> "Girl":
        p = girls_home() / name / "girl.json"
        if not p.exists():
            known = ", ".join(g.name for g in list_girls()) or "none yet"
            raise SystemExit(f"No Internet Girl named {name!r} (known: {known}).")
        data = json.loads(p.read_text())
        fields = cls.__dataclass_fields__
        return cls(**{k: v for k, v in data.items() if k in fields})

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.site_dir.mkdir(exist_ok=True)
        data = {k: getattr(self, k) for k in self.__dataclass_fields__}
        atomic_write(self.dir / "girl.json", json.dumps(data, indent=2) + "\n")

    def api_key(self) -> str | None:
        env = self.backend.get("key_env")
        if env and os.environ.get(env):
            return os.environ[env]
        p = self.dir / "secret.key"
        return p.read_text().strip() if p.exists() else None

    def set_api_key(self, key: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        atomic_write(self.dir / "secret.key", key.strip() + "\n", mode=0o600)

    @property
    def color(self) -> str:
        return self.persona.get("color", "#ff4fa3")

    # ---- daemon bookkeeping ------------------------------------------------
    def pid(self) -> int | None:
        p = self.dir / "node.pid"
        if not p.exists():
            return None
        try:
            pid = int(p.read_text().strip())
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except (ValueError, OSError):
            return None
        if b"igirl.node" in cmd and self.name.encode() in cmd:
            return pid
        return None
