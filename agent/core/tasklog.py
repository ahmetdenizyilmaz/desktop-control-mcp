"""Log everything a task did, to one folder per task - when asked to.

Settings → Behaviour → "Log everything". Off by default: a full log holds
screenshots of the desktop and the text of every question and answer, which
is exactly what you want when a task went wrong and exactly what should not
pile up unasked.

    <data>/logs/tasks/2026-09-23_14-02-11_haftalik-migros-alisverisimi/
        events.jsonl     every event, in order, one JSON object per line
        summary.txt      the task, the outcome, where time and money went
        shots/           every screenshot a model was shown, as a file

Event kinds:
    feed        a line the window showed
    agent       the Claude agent: its text, each tool call with its input,
                each tool result (images saved to shots/, referenced by path)
    model       a model call outside the agent: who (jev / claude CLI /
                openrouter), why (decide, details, write, rescue), the
                prompt, the reply, the time and the cost
    question    a question put to the person, and the answer
    step        a script step's outcome
    ledger      the final time/cost breakdown

Secrets: the vault's values are never written - prompts and tool inputs go
through the same `redact` the channel uses before they reach the file.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from pathlib import Path

from . import paths

#: The log of the task in progress, or None when logging is off.
current: "TaskLog | None" = None

_MAX_TEXT = 200_000        # one field; a whole page dump is cut, not dropped


def _slug(text: str) -> str:
    t = re.sub(r"[^a-z0-9]+", "-", (text or "task").lower()
               .translate(str.maketrans("çğıöşüâî", "cgiosuai")))
    return t.strip("-")[:48] or "task"


def _redact(text: str) -> str:
    try:
        from .secrets import redact
        return redact(text)
    except Exception:
        return text


def _clip(v):
    if isinstance(v, str):
        v = _redact(v)
        return v if len(v) <= _MAX_TEXT else v[:_MAX_TEXT] + " …[cut]"
    if isinstance(v, dict):
        return {k: _clip(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clip(x) for x in v]
    return v


class TaskLog:
    def __init__(self, task: str) -> None:
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        self.dir = paths.data("logs", "tasks", f"{stamp}_{_slug(task)}",
                              ".keep").parent
        (self.dir / "shots").mkdir(parents=True, exist_ok=True)
        self.task = task
        self.t0 = time.time()
        self._lock = threading.Lock()
        self._n = 0
        self.event("start", task=task)

    # -- writing -----------------------------------------------------------
    def event(self, kind: str, **fields) -> None:
        rec = {"t": round(time.time() - self.t0, 3),
               "at": time.strftime("%H:%M:%S"), "kind": kind}
        rec.update(_clip(fields))
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with self._lock:
            with open(self.dir / "events.jsonl", "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def shot_bytes(self, data: bytes, ext: str = "png",
                   label: str = "") -> str:
        """Keep an image; return its path relative to the log folder."""
        with self._lock:
            self._n += 1
            name = f"{self._n:03d}_{_slug(label)[:24]}.{ext}"
        (self.dir / "shots" / name).write_bytes(data)
        return f"shots/{name}"

    def shot_file(self, path: str | Path, label: str = "") -> str:
        p = Path(path)
        try:
            return self.shot_bytes(p.read_bytes(), p.suffix.lstrip(".") or
                                   "png", label or p.stem)
        except Exception:
            return str(path)

    def shot_b64(self, b64: str, media_type: str = "image/png",
                 label: str = "") -> str:
        ext = (media_type.split("/")[-1] or "png").replace("jpeg", "jpg")
        try:
            return self.shot_bytes(base64.b64decode(b64), ext, label)
        except Exception:
            return "(image could not be decoded)"

    def finish(self, outcome: str, summary: str = "") -> None:
        self.event("end", outcome=outcome,
                   seconds=round(time.time() - self.t0, 1))
        text = (f"TASK: {self.task}\nOUTCOME: {outcome}\n"
                f"DURATION: {time.time() - self.t0:.1f}s\n\n{summary}\n\n"
                f"Every event: events.jsonl · screenshots: shots/\n")
        (self.dir / "summary.txt").write_text(text, encoding="utf-8")


# -- module-level helpers: a no-op when logging is off -----------------------
def start(task: str, enabled: bool) -> "TaskLog | None":
    global current
    current = TaskLog(task) if enabled else None
    return current


def stop(outcome: str, summary: str = "") -> None:
    global current
    if current is not None:
        try:
            current.finish(outcome, summary)
        except Exception:
            pass
    current = None


def event(kind: str, **fields) -> None:
    log = current
    if log is not None:
        try:
            log.event(kind, **fields)
        except Exception:
            pass


def model(who: str, why: str, prompt: str, reply: str, seconds: float,
          cost=None, shot: str | Path | None = None, **extra) -> None:
    """One model call outside the agent, in full."""
    log = current
    if log is None:
        return
    try:
        rel = log.shot_file(shot, why) if shot else None
        log.event("model", who=who, why=why, seconds=round(seconds, 3),
                  cost=cost, prompt=prompt, reply=reply, screenshot=rel,
                  **extra)
    except Exception:
        pass


def root() -> Path:
    return paths.data("logs", "tasks", ".keep").parent
