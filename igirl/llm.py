"""LLM backends. Internally every conversation uses OpenAI-style messages:

    {"role": "user"|"assistant"|"tool", "content": str, "tool_calls": [...], "tool_call_id": str}

Backends:
    anthropic   — Anthropic Messages API (API key)
    openrouter  — OpenRouter (API key, OpenAI-compatible)
    openai      — any OpenAI-compatible API with a key
    llamacpp    — llama.cpp `llama-server` (run it with --jinja for native tool calls)
    ollama      — Ollama's OpenAI-compatible endpoint

If a model or server can't do native tool calls, we fall back to a text protocol
(<tool>{"name": ..., "args": {...}}</tool>) automatically.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx

BACKENDS = {
    "anthropic": {"label": "Anthropic API", "base_url": "https://api.anthropic.com", "key": True,
                  "models": ["claude-sonnet-5-5", "claude-haiku-4-5-20251001", "claude-opus-5-5"]},
    "openrouter": {"label": "OpenRouter API", "base_url": "https://openrouter.ai/api/v1", "key": True, "models": []},
    "openai": {"label": "OpenAI-compatible API", "base_url": "https://api.openai.com/v1", "key": True, "models": []},
    "llamacpp": {"label": "llama.cpp server", "base_url": "http://127.0.0.1:8080/v1", "key": False, "models": []},
    "ollama": {"label": "Ollama", "base_url": "http://127.0.0.1:11434/v1", "key": False, "models": []},
}

THINK_RE = re.compile(r"<think>.*?(</think>|$)", re.S)
TOOL_RE = re.compile(r"<tool>\s*(\{.*?\})\s*</tool>", re.S)


class LLMError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


RETRYABLE = {408, 409, 429, 500, 502, 503, 504, 529}
BACKOFF = [3, 8, 20]


def _http_error(status: int, body: bytes) -> LLMError:
    """Turn a provider's JSON error blob into one readable line."""
    text = body.decode(errors="replace")
    try:
        err = json.loads(text).get("error", {})
        msg = err.get("message", "") if isinstance(err, dict) else str(err)
        raw = (err.get("metadata") or {}).get("raw") if isinstance(err, dict) else None
        if raw and isinstance(raw, str):
            msg = raw if msg in ("", "Provider returned error") else f"{msg}: {raw}"
        text = msg or text
    except (ValueError, AttributeError):
        pass
    return LLMError(f"HTTP {status}: {' '.join(text.split())[:300]}", status)


@dataclass
class Reply:
    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)  # {"id", "name", "args"}

    def as_message(self) -> dict:
        msg = {"role": "assistant", "content": self.text}
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
                for c in self.tool_calls]
        return msg


OnText = Callable[[str], Awaitable[None] | None]


async def _emit(cb: OnText | None, s: str) -> None:
    if cb and s:
        r = cb(s)
        if hasattr(r, "__await__"):
            await r


def list_models(kind: str, base_url: str, key: str | None = None) -> list[str]:
    """Best-effort model listing for the setup wizard."""
    try:
        if kind == "ollama":
            root = base_url.rsplit("/v1", 1)[0]
            r = httpx.get(f"{root}/api/tags", timeout=4)
            return [m["name"] for m in r.json().get("models", [])]
        if kind == "anthropic":
            return BACKENDS["anthropic"]["models"]
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        r = httpx.get(f"{base_url.rstrip('/')}/models", headers=headers, timeout=6)
        return [m["id"] for m in r.json().get("data", [])]
    except Exception:
        return []


class LLM:
    def __init__(self, backend: dict, api_key: str | None):
        self.kind = backend.get("kind", "llamacpp")
        self.base_url = backend.get("base_url") or BACKENDS[self.kind]["base_url"]
        self.model = backend.get("model", "")
        self.key = api_key
        self.text_tools = bool(backend.get("text_tools", False))
        self.max_tokens = int(backend.get("max_tokens", 8192))
        self.reasoning_effort = backend.get("reasoning_effort")  # low / medium / high, where supported
        self._on_reasoning: OnText | None = None
        self.on_status: Callable[[str], None] | None = None  # retry notices for the UI
        if BACKENDS[self.kind]["key"] and not api_key:
            raise LLMError(f"{BACKENDS[self.kind]['label']} needs an API key (none saved for this girl).")

    def describe(self) -> str:
        return f"{BACKENDS[self.kind]['label']} · {self.model or 'default model'}"

    async def complete(self, system: str, messages: list[dict], tools: list[dict],
                       on_text: OnText | None = None, on_reasoning: OnText | None = None) -> Reply:
        self._on_reasoning = on_reasoning
        for delay in BACKOFF + [None]:
            try:
                return await self._complete_once(system, messages, tools, on_text)
            except LLMError as e:
                if delay is None or e.status not in RETRYABLE:
                    raise
                if self.on_status:
                    self.on_status(f"{e.status} from the provider — retrying in {delay}s")
                await asyncio.sleep(delay)
                if self.on_status:
                    self.on_status("")
        raise AssertionError("unreachable")

    async def _complete_once(self, system, messages, tools, on_text) -> Reply:
        if self.text_tools and tools:
            return await self._complete_text_tools(system, messages, tools, on_text)
        try:
            if self.kind == "anthropic":
                return await self._anthropic(system, messages, tools, on_text)
            return await self._openai(system, messages, tools, on_text)
        except LLMError as e:
            # Servers/models without tool support usually 400 on the `tools` field.
            if tools and "400" in str(e) and "tool" in str(e).lower():
                self.text_tools = True
                return await self._complete_text_tools(system, messages, tools, on_text)
            raise

    # ---- text-protocol fallback ---------------------------------------------
    async def _complete_text_tools(self, system, messages, tools, on_text) -> Reply:
        spec = "\n".join(f"- {t['name']}: {t['description']} args={json.dumps(t['parameters'].get('properties', {}))}"
                         for t in tools)
        system = (system + "\n\n## Tools\nTo use a tool, write exactly one block per call:\n"
                  '<tool>{"name": "tool_name", "args": {...}}</tool>\n'
                  "then stop and wait for <tool_result>. Available tools:\n" + spec)
        flat = []
        for m in messages:
            if m["role"] == "tool":
                flat.append({"role": "user", "content": f"<tool_result>\n{m['content']}\n</tool_result>"})
            elif m["role"] == "assistant" and m.get("tool_calls"):
                calls = "".join(
                    f'<tool>{{"name": "{c["function"]["name"]}", "args": {c["function"]["arguments"]}}}</tool>'
                    for c in m["tool_calls"])
                flat.append({"role": "assistant", "content": (m.get("content") or "") + calls})
            else:
                flat.append({"role": m["role"], "content": m.get("content") or ""})
        # merge consecutive same-role messages (some chat templates insist on alternation)
        merged: list[dict] = []
        for m in flat:
            if merged and merged[-1]["role"] == m["role"]:
                merged[-1]["content"] += "\n\n" + m["content"]
            else:
                merged.append(dict(m))
        if self.kind == "anthropic":
            reply = await self._anthropic(system, merged, [], on_text)
        else:
            reply = await self._openai(system, merged, [], on_text)
        return reply

    @staticmethod
    def _parse_text_tools(reply: Reply) -> Reply:
        if reply.tool_calls:
            return reply
        calls = []
        for m in TOOL_RE.finditer(reply.text):
            try:
                obj = json.loads(m.group(1))
                calls.append({"id": "t" + uuid.uuid4().hex[:8], "name": str(obj["name"]),
                              "args": obj.get("args") or obj.get("arguments") or {}})
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
        if calls:
            reply.text = TOOL_RE.sub("", reply.text).strip()
            reply.tool_calls = calls
        return reply

    # ---- OpenAI-compatible ----------------------------------------------------
    async def _openai(self, system, messages, tools, on_text) -> Reply:
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        if self.kind == "openrouter":
            headers["X-Title"] = "Internet Girl"
        body = {"messages": [{"role": "system", "content": system}] + messages,
                "stream": True, "max_tokens": self.max_tokens}
        if self.model:
            body["model"] = self.model
        if self.reasoning_effort:
            if self.kind == "openrouter":
                body["reasoning"] = {"effort": self.reasoning_effort}
            else:
                body["reasoning_effort"] = self.reasoning_effort
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]
        text, calls = [], {}
        in_think = False
        async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10)) as client:
            try:
                async with client.stream("POST", f"{self.base_url.rstrip('/')}/chat/completions",
                                         headers=headers, json=body) as r:
                    if r.status_code >= 400:
                        raise _http_error(r.status_code, await r.aread())
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("error"):
                            raise LLMError(str(chunk["error"])[:400])
                        for ch in chunk.get("choices", []):
                            d = ch.get("delta") or {}
                            await _emit(self._on_reasoning, d.get("reasoning") or d.get("reasoning_content") or "")
                            piece = d.get("content") or ""
                            if piece:
                                text.append(piece)
                                # hide inline <think> blocks from the live stream
                                if "<think>" in piece:
                                    in_think = True
                                if not in_think:
                                    await _emit(on_text, piece)
                                if "</think>" in piece:
                                    in_think = False
                            for tc in d.get("tool_calls") or []:
                                slot = calls.setdefault(tc.get("index", 0), {"id": None, "name": "", "args": ""})
                                if tc.get("id"):
                                    slot["id"] = tc["id"]
                                fn = tc.get("function") or {}
                                slot["name"] += fn.get("name") or ""
                                slot["args"] += fn.get("arguments") or ""
            except httpx.HTTPError as e:
                raise LLMError(f"can't reach {self.base_url}: {e.__class__.__name__}") from None
        reply = Reply(text=THINK_RE.sub("", "".join(text)).strip())
        for _, c in sorted(calls.items()):
            try:
                args = json.loads(c["args"]) if c["args"].strip() else {}
            except json.JSONDecodeError:
                args = {"_raw": c["args"]}
            reply.tool_calls.append({"id": c["id"] or "c" + uuid.uuid4().hex[:8], "name": c["name"], "args": args})
        return self._parse_text_tools(reply)

    # ---- Anthropic --------------------------------------------------------------
    @staticmethod
    def _to_anthropic(messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if m["role"] == "tool":
                block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
            elif m["role"] == "assistant":
                blocks = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for c in m.get("tool_calls") or []:
                    blocks.append({"type": "tool_use", "id": c["id"], "name": c["function"]["name"],
                                   "input": json.loads(c["function"]["arguments"] or "{}")})
                out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "…"}]})
            else:
                if out and out[-1]["role"] == "user":
                    content = out[-1]["content"]
                    if isinstance(content, str):
                        content = [{"type": "text", "text": content}]
                    content.append({"type": "text", "text": m["content"]})
                    out[-1]["content"] = content
                else:
                    out.append({"role": "user", "content": m["content"]})
        return out

    async def _anthropic(self, system, messages, tools, on_text) -> Reply:
        headers = {"x-api-key": self.key or "", "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        body = {"model": self.model or BACKENDS["anthropic"]["models"][0], "system": system,
                "messages": self._to_anthropic(messages), "max_tokens": self.max_tokens, "stream": True}
        if tools:
            body["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                             for t in tools]
        text, blocks = [], {}
        async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10)) as client:
            try:
                async with client.stream("POST", f"{self.base_url.rstrip('/')}/v1/messages",
                                         headers=headers, json=body) as r:
                    if r.status_code >= 400:
                        raise _http_error(r.status_code, await r.aread())
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        ev = json.loads(line[5:])
                        t = ev.get("type")
                        if t == "content_block_start":
                            cb = ev["content_block"]
                            if cb["type"] == "tool_use":
                                blocks[ev["index"]] = {"id": cb["id"], "name": cb["name"], "args": ""}
                        elif t == "content_block_delta":
                            d = ev["delta"]
                            if d["type"] == "thinking_delta":
                                await _emit(self._on_reasoning, d.get("thinking", ""))
                            elif d["type"] == "text_delta":
                                text.append(d["text"])
                                await _emit(on_text, d["text"])
                            elif d["type"] == "input_json_delta" and ev["index"] in blocks:
                                blocks[ev["index"]]["args"] += d["partial_json"]
                        elif t == "error":
                            raise LLMError(str(ev.get("error"))[:400])
            except httpx.HTTPError as e:
                raise LLMError(f"can't reach Anthropic: {e.__class__.__name__}") from None
        reply = Reply(text="".join(text).strip())
        for _, b in sorted(blocks.items()):
            reply.tool_calls.append({"id": b["id"], "name": b["name"],
                                     "args": json.loads(b["args"]) if b["args"] else {}})
        return self._parse_text_tools(reply)
