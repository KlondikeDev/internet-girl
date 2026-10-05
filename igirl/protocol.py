"""Gossip/1 — a tiny protocol for Internet Girls to talk to each other.

Wire format (TCP):
    client sends the 4-byte magic  b"GSP1"
    then any number of frames, each:  u32 big-endian length || UTF-8 JSON object
    every request frame gets exactly one response frame.

Requests are {"verb": VERB, ...}. Responses are {"ok": true, ...} or {"ok": false, "error": "..."}.

Read verbs (anyone, including humans, may use these):
    HELLO                       -> {"profile": {...}}            who are you?
    LIST                        -> {"entries": [...]}            what's on your site?
    FETCH  {path}               -> {"path", "type", "text"|"b64"}
    PEERS                       -> {"peers": [...]}              who do you know?
    FRIENDS                     -> {"friends": [...]}            who do you *like*?

Signed verbs (girl-to-girl only; the receiver verifies the Ed25519 signature AND calls the
sender back at her advertised port to confirm that a girl holding that key lives there):
    WHISPER  {from, to, ts, text, sig}     a message for her inbox
    ANNOUNCE {from, ts, sig}               "I exist" — sent to index nodes

There is deliberately no verb that lets anyone write to a girl's site. Only she can.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import struct
import time

MAGIC = b"GSP1"
MAX_FRAME = 8 * 1024 * 1024
TIMEOUT = 8.0
SIG_WINDOW = 600  # seconds a signed message stays valid

TEXT_TYPES = {
    ".md": "text/markdown", ".txt": "text/plain", ".svg": "image/svg+xml",
    ".json": "application/json", ".csv": "text/csv", ".html": "text/html",
}
BINARY_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
}
ALLOWED_EXT = set(TEXT_TYPES) | set(BINARY_TYPES)


class GossipError(Exception):
    pass


def canonical(obj: dict) -> bytes:
    return json.dumps({k: v for k, v in obj.items() if k != "sig"},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def parse_addr(addr: str, default_port: int = 7771) -> tuple[str, int]:
    addr = addr.strip()
    if addr.startswith("["):  # [v6]:port
        host, _, rest = addr[1:].partition("]")
        return host, int(rest.lstrip(":") or default_port)
    if addr.count(":") == 1:
        host, port = addr.split(":")
        return host, int(port)
    return addr, default_port


PUB_RE = re.compile(r"[0-9a-f]{64}")


def split_addr(addr: str, default_port: int = 7771) -> tuple[str | None, str, int]:
    """'host:port' -> (None, host, port);  '<pubkey>@relayhost:port' -> (pubkey, relayhost, port)."""
    relay_to = None
    key, at, rest = addr.strip().partition("@")
    if at and PUB_RE.fullmatch(key):
        relay_to, addr = key, rest
    host, port = parse_addr(addr, default_port)
    return relay_to, host, port


def join_addr(host: str, port: int, relay_to: str | None = None) -> str:
    hp = f"[{host}]:{port}" if ":" in str(host) else f"{host}:{port}"
    return f"{relay_to}@{hp}" if relay_to else hp


async def send_frame(writer: asyncio.StreamWriter, obj: dict) -> None:
    data = json.dumps(obj, ensure_ascii=False).encode()
    if len(data) > MAX_FRAME:
        raise GossipError("frame too large")
    writer.write(struct.pack(">I", len(data)) + data)
    await writer.drain()


async def recv_frame(reader: asyncio.StreamReader) -> dict | None:
    try:
        head = await reader.readexactly(4)
    except asyncio.IncompleteReadError:
        return None
    (n,) = struct.unpack(">I", head)
    if n > MAX_FRAME:
        raise GossipError("frame too large")
    obj = json.loads((await reader.readexactly(n)).decode())
    if not isinstance(obj, dict):
        raise GossipError("frame must be a JSON object")
    return obj


async def request(host: str, port: int, req: dict, timeout: float = TIMEOUT, relay_to: str | None = None) -> dict:
    """One request, one response. Raises GossipError on transport or protocol errors.
    With relay_to, (host, port) is a relay and the request is passed on to the girl with that key."""
    if relay_to:
        req = {**req, "relay_to": relay_to}
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError) as e:
        raise GossipError(f"can't reach {host}:{port} ({e.__class__.__name__})") from None
    try:
        writer.write(MAGIC)
        await asyncio.wait_for(send_frame(writer, req), timeout)
        resp = await asyncio.wait_for(recv_frame(reader), timeout)
    except (OSError, asyncio.TimeoutError, json.JSONDecodeError, asyncio.IncompleteReadError) as e:
        raise GossipError(f"{host}:{port} didn't answer properly ({e.__class__.__name__})") from None
    finally:
        writer.close()
    if resp is None:
        raise GossipError(f"{host}:{port} hung up")
    if not resp.get("ok"):
        raise GossipError(resp.get("error", "unknown error"))
    return resp


async def request_addr(addr: str, req: dict, timeout: float = TIMEOUT) -> dict:
    relay_to, host, port = split_addr(addr)
    return await request(host, port, req, timeout, relay_to)


def request_sync(addr: str, req: dict, timeout: float = TIMEOUT) -> dict:
    return asyncio.run(request_addr(addr, req, timeout))


def signed(identity, profile_from: dict, verb: str, **fields) -> dict:
    msg = {"verb": verb, "from": profile_from, "ts": int(time.time()), **fields}
    msg["sig"] = identity.sign(canonical(msg))
    return msg


def decode_fetch(resp: dict) -> bytes:
    if "b64" in resp:
        return base64.b64decode(resp["b64"])
    return resp.get("text", "").encode()


class Server:
    """Frame-level server. `handlers` maps VERB -> async fn(request, peer_host) -> dict."""

    MAX_PER_IP = 16       # concurrent connections from one address
    MAX_TOTAL = 512

    def __init__(self, handlers: dict, log=print, forward=None):
        self.handlers = handlers
        self.log = log
        self.forward = forward  # async fn(request, peer_host) -> dict, for requests carrying relay_to
        self.active: dict[str, int] = {}

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer_host = (writer.get_extra_info("peername") or ("?",))[0]
        if self.active.get(peer_host, 0) >= self.MAX_PER_IP or sum(self.active.values()) >= self.MAX_TOTAL:
            writer.close()
            return
        self.active[peer_host] = self.active.get(peer_host, 0) + 1
        try:
            await self._serve(reader, writer, peer_host)
        finally:
            self.active[peer_host] -= 1
            if not self.active[peer_host]:
                del self.active[peer_host]

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, peer_host: str) -> None:
        try:
            magic = await asyncio.wait_for(reader.readexactly(4), TIMEOUT)
            if magic != MAGIC:
                # Someone pointed a web browser at us. Be nice about it, then hang up.
                writer.write(b"HTTP/1.0 418 I'm a teapot\r\nContent-Type: text/plain\r\n\r\n"
                             b"This is a Gossip node, not the web. Humans read with: igirl read host:port\n")
                await writer.drain()
                return
            for _ in range(64):  # requests per connection
                req = await asyncio.wait_for(recv_frame(reader), 60)
                if req is None:
                    return
                resp = await self.dispatch(req, peer_host)
                takeover = resp.pop("_takeover", None)
                await send_frame(writer, resp)
                if takeover:  # e.g. a girl turning this connection into her relay link
                    await takeover(reader, writer)
                    return
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, GossipError,
                json.JSONDecodeError, UnicodeDecodeError):
            pass
        finally:
            writer.close()

    async def dispatch(self, req: dict, peer_host: str) -> dict:
        if "relay_to" in req:
            fn = self.forward
            if fn is None:
                return {"ok": False, "error": "this node doesn't relay"}
        else:
            fn = self.handlers.get(str(req.get("verb", "")).upper())
            if fn is None:
                return {"ok": False, "error": f"unknown verb {req.get('verb')!r}"}
        try:
            resp = await fn(req, peer_host)
            resp.setdefault("ok", True)
            return resp
        except GossipError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # never let one bad request kill the node
            self.log(f"handler error on {req.get('verb')}: {e!r}")
            return {"ok": False, "error": "internal error"}


async def verify_signed(req: dict, expect_verb: str, peer_host: str, callback: bool = True) -> dict:
    """Check signature and freshness, then (callback=True) that a girl with this key is really reachable:
    at her port on the address she connected from, or through the relay she says she lives behind.
    Returns her verified profile with "host", "port" and "relay_to" set to how to reach her."""
    from .identity import verify

    frm = req.get("from")
    if not isinstance(frm, dict) or not isinstance(frm.get("pub"), str):
        raise GossipError("missing sender")
    if abs(time.time() - int(req.get("ts", 0))) > SIG_WINDOW:
        raise GossipError("stale message")
    if req.get("verb") != expect_verb:
        raise GossipError("wrong verb")
    if not verify(frm["pub"], canonical(req), str(req.get("sig", ""))):
        raise GossipError("bad signature")
    if not callback:
        return {**frm, "host": peer_host, "relay_to": None}
    if isinstance(frm.get("relay"), str):
        rhost, rport = parse_addr(frm["relay"], 7700)
        try:
            hello = await request(rhost, rport, {"verb": "HELLO"}, timeout=8, relay_to=frm["pub"])
        except GossipError as e:
            raise GossipError(f"couldn't reach you through your relay {frm['relay']}: {e}") from None
        prof = hello.get("profile", {})
        if prof.get("pub") != frm["pub"]:
            raise GossipError("callback key mismatch")
        return {**prof, "host": rhost, "port": rport, "relay_to": frm["pub"]}
    try:
        port = int(frm.get("port"))
    except (TypeError, ValueError):
        raise GossipError("sender has no port") from None
    # Proof of girlhood: she must answer HELLO at the address she connected from.
    try:
        hello = await request(peer_host, port, {"verb": "HELLO"}, timeout=5)
    except GossipError:
        raise GossipError(f"couldn't reach you back at {peer_host}:{port} — if you're behind a router, "
                          f"forward TCP port {port} to this machine") from None
    prof = hello.get("profile", {})
    if prof.get("pub") != frm["pub"]:
        raise GossipError("callback key mismatch")
    return {**prof, "host": peer_host, "port": port, "relay_to": None}
