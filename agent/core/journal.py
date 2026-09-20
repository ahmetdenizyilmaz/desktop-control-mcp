"""Action journal — record what the AGENT does, replay it verbatim.

recorder.py captures a *human* demonstration and turns it into a
locator-improvising Macro. This is the other direction: while the agent drives
the machine, every acting tool call it makes is written down exactly as called
— tool name and arguments — and a saved journal is replayed by calling those
same tools again, in order.

Nothing here interprets or repairs the steps, and it does not need to: the
tools themselves resolve targets by NAME and verify their own results, so a
journal of "click Save, type ..., press enter" is already as robust as the
live run was. What the journal adds is repeatability without the model in the
loop — the same task, redone, for free.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from . import paths


def _dir() -> Path:
    d = paths.data("workspace", "journals")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _valid_id(jid: str) -> bool:
    """A journal id names a file in one directory — never a path.

    Same guard as the stroke sets: an id with a separator or a parent segment
    could read or delete a file anywhere the process can reach.
    """
    return (bool(jid) and jid.startswith("j-") and len(jid) <= 40
            and "/" not in jid and "\\" not in jid and ".." not in jid)


def _clean(args: dict) -> dict:
    """A JSON-safe shallow copy of the call arguments, so a journal round-trips
    through disk unchanged."""
    out: dict = {}
    for k, v in (args or {}).items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        else:
            try:
                json.dumps(v)
                out[k] = v
            except Exception:
                out[k] = str(v)
    return out


def _count_steps(raw) -> int:
    if isinstance(raw, list):
        return len(raw)
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
            return len(v) if isinstance(v, list) else 1
        except Exception:
            return 1
    return 0


def summarise(tool: str, args: dict) -> str:
    """One plain-English line for an action, for the draft the user reads."""
    a = args or {}
    if tool == "click":
        return f"click {a.get('name', '?')!r}"
    if tool == "type_text":
        t = str(a.get("text", ""))
        return f"type {t[:40]!r}" + ("…" if len(t) > 40 else "")
    if tool == "press_key":
        return f"press {a.get('keys') or a.get('key') or '?'!r}"
    if tool == "focus_window":
        return f"focus window {a.get('title', '?')!r}"
    if tool == "open_app":
        return f"open app {a.get('name', '?')!r}"
    if tool == "do_steps":
        return f"do_steps ({_count_steps(a.get('steps'))} steps)"
    if tool == "mouse_path":
        if a.get("strokes_from"):
            return f"mouse_path (replay stroke set {a.get('strokes_from')})"
        return f"mouse_path ({a.get('action') or 'draw'})"
    if tool == "use_credential":
        return f"use credential {a.get('name', '?')!r}"
    if tool == "copy_to_clipboard":
        return "copy to clipboard"
    if tool.startswith("web_"):
        detail = a.get("url") or a.get("name") or a.get("label") or ""
        return f"{tool} {detail}".strip()
    return tool


class Recorder:
    """The live journal being written. One at a time by construction."""

    def __init__(self, name: str = "", description: str = "") -> None:
        self.name = name
        self.description = description
        self.entries: list[dict] = []
        self.started_at = time.time()
        self.video_path = ""
        self._lock = threading.Lock()

    def record(self, tool: str, args: dict) -> None:
        with self._lock:
            self.entries.append({
                "i": len(self.entries) + 1,
                "t": round(time.time() - self.started_at, 2),
                "tool": tool,
                "args": _clean(args),
            })

    def describe(self) -> str:
        if not self.entries:
            return "No actions were recorded."
        return "\n".join(f"  {e['i']:>2}. {summarise(e['tool'], e['args'])}"
                         for e in self.entries)


# --- the live recorder, shared across tool calls and model turns ----------
#: The one journal currently being written, or None. Set by the replay tool.
live: "Recorder | None" = None
#: True while a replay run is executing, so the tool calls a replay makes are
#: not themselves journaled back into a fresh recording.
replaying = False


def armed() -> bool:
    return live is not None


def record_call(tool: str, args: dict) -> None:
    """Append an acting tool call, if a journal is armed and this is not a
    replay of one. Called from the tool dispatch after a successful call."""
    if live is not None and not replaying:
        try:
            live.record(tool, args)
        except Exception:
            pass


# --- saved journals on disk -----------------------------------------------

def save(rec: "Recorder", name: str = "", description: str = "") -> dict:
    jid = "j-" + uuid.uuid4().hex[:8]
    payload = {
        "id": jid,
        "name": name or rec.name or "recorded session",
        "description": description or rec.description or "",
        "created": time.time(),
        "duration_s": round(time.time() - rec.started_at, 2),
        "entries": rec.entries,
        "video": rec.video_path or "",
    }
    (_dir() / f"{jid}.json").write_text(json.dumps(payload, indent=2),
                                        encoding="utf-8")
    return payload


def load(jid: str) -> dict | None:
    if not _valid_id(jid):
        return None
    p = _dir() / f"{jid}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_all() -> list[dict]:
    out: list[dict] = []
    for p in _dir().glob("j-*.json"):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    out.sort(key=lambda d: d.get("created", 0), reverse=True)
    return out


def delete(jid: str) -> bool:
    if not _valid_id(jid):
        return False
    p = _dir() / f"{jid}.json"
    if not p.exists():
        return False
    data = load(jid) or {}
    p.unlink()
    vid = data.get("video")
    if vid:
        try:
            Path(vid).unlink()
        except Exception:
            pass
    return True
