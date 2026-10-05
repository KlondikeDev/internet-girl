"""The friendbook: everyone she's met, and how much she likes them.

Levels run 0-100. Talking raises it, she can nudge it herself after a conversation
(adjust_feelings), and it slowly fades if two girls stop talking. Who she reaches out
to is a weighted pick: close friends get most of her attention, but she still says hi
to strangers sometimes — that's how strangers become friends.
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path

from .config import atomic_write

TIERS = [
    (85, "bestie", "💖"),
    (60, "close friend", "💕"),
    (35, "friend", "🌸"),
    (15, "acquaintance", "👋"),
    (0, "stranger", "·"),
]

DECAY_GRACE_DAYS = 3      # no fading for the first few quiet days
DECAY_PER_DAY = 1.0


def addr_of(p: dict) -> str:
    """How to reach a friendbook entry: host:port, or <pubkey>@relay:port."""
    from .protocol import join_addr
    return join_addr(p.get("host", "?"), p.get("port", 0), p.get("relay_to"))


def tier(level: float) -> tuple[str, str]:
    for floor, name, icon in TIERS:
        if level >= floor:
            return name, icon
    return TIERS[-1][1], TIERS[-1][2]


class FriendBook:
    def __init__(self, path: Path):
        self.path = path
        self.peers: dict[str, dict] = {}
        self.reload()

    def reload(self) -> None:
        try:
            self.peers = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            self.peers = {}

    def save(self) -> None:
        atomic_write(self.path, json.dumps(self.peers, indent=1, ensure_ascii=False))

    # ---- updates (each reloads first: chat and the node share this file) ----
    def seen(self, profile: dict, host: str, source: str, first_level: float = 0.0,
             port: int | None = None, relay_to: str | None = None) -> dict | None:
        """Record that we met her. (host, port) is how to reach her — her own port, or a relay's
        port when relay_to is set."""
        pub = profile.get("pub")
        if not isinstance(pub, str) or len(pub) != 64:
            return None
        self.reload()
        p = self.peers.setdefault(pub, {"level": float(first_level), "met": time.time(), "source": source, "last_talk": 0})
        p.update({
            "name": str(profile.get("name", "?"))[:24],
            "host": host,
            "port": int(port if port is not None else profile.get("port", 0)),
            "relay_to": relay_to,
            "tagline": str(profile.get("tagline", ""))[:140],
            "last_seen": time.time(),
        })
        self.save()
        return p

    def bump(self, pub: str, delta: float, reason: str = "", talked: bool = False) -> float | None:
        self.reload()
        p = self.peers.get(pub)
        if p is None:
            return None
        self._decay_one(p)
        p["level"] = max(0.0, min(100.0, p.get("level", 0.0) + delta))
        if talked:
            p["last_talk"] = time.time()
        if reason:
            p["feeling"] = reason[:200]
        self.save()
        return p["level"]

    def _decay_one(self, p: dict) -> None:
        quiet_days = (time.time() - max(p.get("last_talk", 0), p.get("met", 0))) / 86400
        fade = max(0.0, quiet_days - DECAY_GRACE_DAYS) * DECAY_PER_DAY
        already = p.get("faded", 0.0)
        if fade > already:
            p["level"] = max(0.0, p.get("level", 0.0) - (fade - already))
            p["faded"] = fade
        if p.get("last_talk", 0) >= time.time() - 60:
            p["faded"] = 0.0

    def decay_all(self) -> None:
        self.reload()
        for p in self.peers.values():
            self._decay_one(p)
        self.save()

    # ---- queries -----------------------------------------------------------
    def find(self, who: str) -> tuple[str, dict] | None:
        """Look up by name (case-insensitive), pubkey prefix, or host:port."""
        self.reload()
        w = who.strip().lower()
        for pub, p in self.peers.items():
            if p.get("name", "").lower() == w or pub.startswith(w) or addr_of(p).lower() == w \
                    or f"{p.get('host')}:{p.get('port')}" == w:
                return pub, p
        return None

    def ranked(self) -> list[tuple[str, dict]]:
        self.reload()
        return sorted(self.peers.items(), key=lambda kv: -kv[1].get("level", 0))

    def pick(self, social: float = 1.0, exclude: set[str] = frozenset()) -> tuple[str, dict] | None:
        """Choose who to reach out to. Weight grows steeply with friendship;
        strangers keep a floor weight (higher for social butterflies)."""
        cands = [(pub, p) for pub, p in self.ranked()
                 if pub not in exclude and time.time() - p.get("last_seen", 0) < 7 * 86400]
        if not cands:
            return None
        weights = [(p.get("level", 0) + 6 * social) ** 1.6 for _, p in cands]
        return random.choices(cands, weights=weights, k=1)[0]

    def summary(self, limit: int = 12) -> str:
        rows = []
        for pub, p in self.ranked()[:limit]:
            t, icon = tier(p.get("level", 0))
            line = f"- {icon} {p.get('name')} ({t}, {p.get('level', 0):.0f}/100)"
            if p.get("tagline"):
                line += f" — “{p['tagline']}”"
            if p.get("feeling"):
                line += f" [your note: {p['feeling']}]"
            rows.append(line)
        return "\n".join(rows) or "(you haven't met anyone yet)"

    def public_list(self) -> list[dict]:
        """What she shares on FRIENDS: only actual friends, no strangers."""
        out = []
        for pub, p in self.ranked():
            if p.get("level", 0) >= 35:
                out.append({"pub": pub, "name": p.get("name"), "host": p.get("host"), "port": p.get("port"),
                            "relay_to": p.get("relay_to"), "tier": tier(p["level"])[0]})
        return out
