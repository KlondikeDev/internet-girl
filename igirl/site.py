"""Her site: a directory of files she chooses to host on her Gossip port."""

from __future__ import annotations

import base64
from pathlib import Path, PurePosixPath

from .protocol import ALLOWED_EXT, BINARY_TYPES, TEXT_TYPES, GossipError

SITE_QUOTA = 25 * 1024 * 1024
MAX_FILES = 300


def resolve(site_dir: Path, path: str) -> Path:
    """Map a site path like '/art/moon.svg' to a file inside site_dir, refusing escapes."""
    rel = PurePosixPath(str(path).strip().lstrip("/"))
    if not rel.parts:
        rel = PurePosixPath("index.md")
    if any(p in ("..", "") or p.startswith(".") for p in rel.parts):
        raise GossipError("bad path")
    full = (site_dir / Path(*rel.parts)).resolve()
    if site_dir.resolve() not in full.parents:
        raise GossipError("bad path")
    return full


def mime(path: Path) -> str:
    ext = path.suffix.lower()
    return TEXT_TYPES.get(ext) or BINARY_TYPES.get(ext) or "application/octet-stream"


def listing(site_dir: Path) -> list[dict]:
    out = []
    if not site_dir.exists():
        return out
    for f in sorted(site_dir.rglob("*")):
        if f.is_file() and not any(p.startswith(".") for p in f.relative_to(site_dir).parts):
            st = f.stat()
            out.append({
                "path": "/" + f.relative_to(site_dir).as_posix(),
                "type": mime(f),
                "size": st.st_size,
                "updated": int(st.st_mtime),
            })
    return out


def fetch(site_dir: Path, path: str) -> dict:
    f = resolve(site_dir, path)
    if not f.is_file():
        raise GossipError(f"nothing at {path}")
    t = mime(f)
    out = {"path": "/" + f.relative_to(site_dir.resolve()).as_posix(), "type": t, "size": f.stat().st_size}
    if f.suffix.lower() in BINARY_TYPES:
        out["b64"] = base64.b64encode(f.read_bytes()).decode()
    else:
        out["text"] = f.read_text(errors="replace")
    return out


def _check_room(site_dir: Path, target: Path, new_size: int) -> None:
    entries = listing(site_dir)
    used = sum(e["size"] for e in entries)
    if target.exists():
        used -= target.stat().st_size
    elif len(entries) >= MAX_FILES:
        raise GossipError(f"site is full ({MAX_FILES} files) — remove something first")
    if used + new_size > SITE_QUOTA:
        raise GossipError(f"site quota ({SITE_QUOTA // 1024 // 1024} MB) exceeded")


def publish(site_dir: Path, path: str, content: str | bytes) -> str:
    f = resolve(site_dir, path)
    if f.suffix.lower() not in ALLOWED_EXT:
        raise GossipError(f"can't host {f.suffix or 'extensionless'} files; allowed: {' '.join(sorted(ALLOWED_EXT))}")
    data = content.encode() if isinstance(content, str) else content
    _check_room(site_dir, f, len(data))
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    return "/" + f.relative_to(site_dir.resolve()).as_posix()


def remove(site_dir: Path, path: str) -> str:
    f = resolve(site_dir, path)
    if not f.is_file():
        raise GossipError(f"nothing at {path}")
    f.unlink()
    # tidy empty dirs
    d = f.parent
    while d != site_dir.resolve() and not any(d.iterdir()):
        d.rmdir()
        d = d.parent
    return path
