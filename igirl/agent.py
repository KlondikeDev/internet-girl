"""The tool-calling loop shared by chat, heartbeats and whisper replies."""

from __future__ import annotations

from typing import Awaitable, Callable

from . import tools as T
from .llm import LLM, OnText

Approve = Callable[[T.Tool, dict], Awaitable[bool]]
OnTool = Callable[[T.Tool | None, str, dict], Awaitable[None] | None]
OnResult = Callable[[str, str, bool], Awaitable[None] | None]


async def _maybe(r):
    if hasattr(r, "__await__"):
        await r


async def run(llm: LLM, system: str, messages: list[dict], tools: list[T.Tool], ctx: T.Ctx,
              approve: Approve | None = None, on_text: OnText | None = None,
              on_tool: OnTool | None = None, on_result: OnResult | None = None,
              on_step_text: Callable[[str], Awaitable[None] | None] | None = None,
              on_reasoning: OnText | None = None, max_steps: int = 14) -> str:
    """Run until the model answers without calling tools. Mutates `messages`. Returns final text."""
    schemas = [t.schema() for t in tools]
    allowed = {t.name: t for t in tools}
    reply = None
    for _ in range(max_steps):
        reply = await llm.complete(system, messages, schemas, on_text=on_text, on_reasoning=on_reasoning)
        messages.append(reply.as_message())
        if on_step_text:
            await _maybe(on_step_text(reply.text))
        if not reply.tool_calls:
            return reply.text
        for call in reply.tool_calls:
            tool = allowed.get(call["name"])
            if on_tool:
                await _maybe(on_tool(tool, call["name"], call["args"]))
            if tool is None:
                out, ok = f"no tool called {call['name']!r} is available right now", False
            elif tool.risk == "ask" and not (approve and await approve(tool, call["args"])):
                out, ok = "the human declined to run this. Ask them what they'd prefer, or try another way.", False
            else:
                out, ok = await T.call(tool, ctx, call["args"]), True
            if on_result:
                await _maybe(on_result(call["name"], out, ok))
            messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"], "content": out})
    return (reply.text if reply else "") + "\n\n(…I stopped after too many steps in a row.)"
