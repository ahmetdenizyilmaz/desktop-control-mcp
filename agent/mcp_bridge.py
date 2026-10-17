"""AGI Agent as an MCP server — the app's own tools, over stdio.

Run:   python agent/mcp_bridge.py
Add:   claude mcp add agi-agent -- "<venv>\\Scripts\\python.exe" "<repo>\\agent\\mcp_bridge.py"

The same tools the app drives (tools_sdk.ALL_TOOLS), on the same worker
thread, reading and writing the same lessons file — so what the app learns
this learns, and the other way round. What changes is the brain: whichever
MCP client is talking (Claude Code, Claude Desktop, anything else) plans
the task and calls the tools. Two things the app does *around* the tools
are done here instead:

  * the ARBITER. In the app a task starts it and Stop ends it. Here
    take_control() and release_control() do — and the first acting call
    starts it by itself, so a forgotten take_control cannot silently mean
    "no kill switch". ESC+ESC aborts; the user moving the mouse yields.
  * ask_user / notify_user. The app pops a dialog; here the client IS the
    conversation, so the question comes back as text for the model to ask
    the person directly.

stdout is the protocol. Once the transport is attached, sys.stdout is pointed
at stderr so a library that prints — the OCR engine announces "Using CPU" the
first time it loads — cannot corrupt a JSON-RPC frame. The client shows
stderr in its logs, which is where such chatter belongs.

The earlier agent_mcp.py is an older generation of the same idea, written
before the app moved its tools in-process; it is kept for reference only.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import mcp.types as T                                    # noqa: E402
from mcp.server import Server                            # noqa: E402
from mcp.server.stdio import stdio_server                # noqa: E402

import tools_sdk                                         # noqa: E402
from core.arbiter import Arbiter                         # noqa: E402
from core.machine_profile import load_profile            # noqa: E402

SERVER_NAME = "agi-agent"

_TOOLS = {t.name: t for t in tools_sdk.ALL_TOOLS}

#: Calls that only look. Everything else starts the arbiter first, because
#: everything else can move the mouse or press a key on the user's machine.
_READ_ONLY = {
    "list_windows", "find_controls", "read_labels", "screenshot", "machine_info",
    "web_status", "web_learned", "web_read", "web_page", "web_elements",
    "web_tabs", "list_credentials", "find_job", "recall_lessons", "save_lesson",
    "save_script", "wait_for_reply", "sample_colors", "trace_edges",
}

_PREAMBLE = """You are driving this Windows PC through the AGI Agent engine.

CONTROL AND SAFETY
- take_control() before acting; release_control() when the task is finished.
  Acting tools start control on their own if you forget, but say so.
- A result beginning "STOP:" means the user took the machine back (they moved
  the mouse or typed) or pressed ESC+ESC. Stop. Do not fight them for it.
- ask_user returns the question to YOU: ask the person in this conversation,
  wait for their answer, then continue. notify_user works the same way.

The rest of these instructions are the engine's own operating notes.

"""

_arbiter: Arbiter | None = None
_started = False


def _ensure_control() -> str | None:
    """Start the arbiter if it is not running. Returns a denial, or None."""
    global _arbiter, _started
    if _started and _arbiter is not None and not _arbiter.aborted:
        return None
    if _arbiter is not None and _arbiter.aborted:
        # ESC+ESC is sticky by design; a fresh task needs a fresh arbiter.
        _release()
    if _arbiter is None:
        _arbiter = Arbiter(owner="AGI Agent (MCP)")
    if not _arbiter.start():
        detail = _arbiter.state.detail
        _arbiter = None
        return f"DENIED — {detail}"
    _started = True
    tools_sdk.set_arbiter(_arbiter)
    tools_sdk.set_cancelled(False)
    tools_sdk.set_paused(False)
    return None


def _release() -> str:
    global _arbiter, _started
    tools_sdk.set_arbiter(None)
    if _arbiter is not None:
        try:
            _arbiter.stop()
        except Exception:
            pass
    _arbiter, _started = None, False
    return "Control released."


_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean",
               list: "array", dict: "object"}


def _schema(t) -> dict:
    """The SDK's own rule, reproduced: a {name: type} dict becomes an object
    schema with every key required; a real JSON schema passes through."""
    s = t.input_schema
    if isinstance(s, dict):
        if "type" in s and "properties" in s and isinstance(s["type"], str):
            return s
        props = {k: {"type": _JSON_TYPES.get(v, "string")} for k, v in s.items()}
        return {"type": "object", "properties": props, "required": list(props)}
    return {"type": "object", "properties": {}}


def _content(result: dict | None) -> list:
    out: list = []
    for b in (result or {}).get("content", []):
        if b.get("type") == "image":
            out.append(T.ImageContent(type="image", data=b["data"],
                                      mimeType=b.get("mimeType", "image/jpeg")))
        else:
            out.append(T.TextContent(type="text", text=str(b.get("text", ""))))
    return out or [T.TextContent(type="text", text="(no output)")]


def _text(s: str) -> list:
    return [T.TextContent(type="text", text=s)]


server = Server(SERVER_NAME, instructions=_PREAMBLE + tools_sdk.SYSTEM_PROMPT)


@server.list_tools()
async def _list_tools() -> list[T.Tool]:
    tools = [
        T.Tool(name="take_control",
               description="Start the safety arbiter before driving the PC: "
                           "ESC+ESC aborts, and the user moving the mouse pauses "
                           "the agent. Acting tools call this themselves if you "
                           "forget.",
               inputSchema={"type": "object", "properties": {}}),
        T.Tool(name="release_control",
               description="Stop the arbiter when the task is finished.",
               inputSchema={"type": "object", "properties": {}}),
    ]
    for t in tools_sdk.ALL_TOOLS:
        tools.append(T.Tool(name=t.name, description=t.description,
                            inputSchema=_schema(t)))
    return tools


@server.call_tool()
async def _call_tool(name: str, arguments: dict | None):
    args = arguments or {}
    if name == "take_control":
        denied = _ensure_control()
        if denied:
            return _text(denied)
        p = load_profile()
        return _text(f"Control acquired. Screen {p['screen']['w']}x{p['screen']['h']} "
                     f"@{p['screen']['dpi_scale']}x. The user can abort at any "
                     f"time with ESC+ESC.")
    if name == "release_control":
        return _text(_release())
    t = _TOOLS.get(name)
    if t is None:
        return _text(f"Unknown tool {name!r}.")
    if name == "ask_user":
        q = str(args.get("question") or "").strip()
        if not q:
            return _text("Nothing to ask.")
        return _text("QUESTION FOR THE USER — ask it in the conversation and "
                     "wait for the answer before continuing:\n" + q)
    if name == "notify_user":
        return _text(f"Tell the user: {args.get('message', '')}")
    if name not in _READ_ONLY:
        denied = _ensure_control()
        if denied:
            return _text(denied)
    try:
        result = await t.handler(args)
    except Exception as e:
        return _text(f"{name} failed: {type(e).__name__}: {e}")
    return _content(result)


async def _main() -> None:
    async with stdio_server() as (read, write):
        # The protocol owns the real stdout from here. Anything a library
        # prints after this goes to stderr, where the client logs it.
        sys.stdout = sys.stderr
        try:
            await server.run(read, write, server.create_initialization_options())
        finally:
            _release()


if __name__ == "__main__":
    asyncio.run(_main())
