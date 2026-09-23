"""A script run that was cut short can carry on where it stopped.

The app closed, the machine restarted, or someone pressed Stop at product
nine of twenty. Starting again from the top would add the first nine
twice; giving up would throw them away. So while a script runs, each item
of its `repeat` that finishes is written down, and a run that does not end
cleanly leaves that record behind:

    runs/<id>.json   {task, script, args, done: [...], total, status, ...}

On the next start the window offers it: Resume runs the same script with
the same arguments and skips the items already done; Dismiss forgets it.

Only ITEMS are skipped, never the steps around them. The steps before a
repeat (open Chrome, go to the site) re-establish state, and running them
again is harmless; skipping them would leave the resumed items acting on
whatever happens to be on screen. That is the same rule that makes a
failed item safe to skip in the first place (see pipeline/script/runner.py).
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from . import paths

_lock = threading.Lock()


def _dir() -> Path:
    return paths.data("runs", ".keep").parent


class Checkpoint:
    def __init__(self, task: str, script: str, args: dict,
                 skip: list[str] | None = None, run_id: str = "") -> None:
        self.id = run_id or f"{time.strftime('%Y%m%d-%H%M%S')}-" \
                            f"{uuid.uuid4().hex[:6]}"
        self.data = {"id": self.id, "task": task, "script": script,
                     "args": dict(args), "done": list(skip or []),
                     "failed": [], "status": "running",
                     "started": time.time(), "updated": time.time()}
        self.path = _dir() / f"{self.id}.json"
        self._save()

    def _save(self) -> None:
        self.data["updated"] = time.time()
        with _lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(self.path)          # never a half-written file

    def item(self, item: str, ok: bool) -> None:
        key = "done" if ok else "failed"
        if item not in self.data[key]:
            self.data[key].append(item)
        if ok and item in self.data["failed"]:
            self.data["failed"].remove(item)
        self._save()

    def total(self, n: int) -> None:
        self.data["total"] = n
        self._save()

    def finish(self, status: str) -> None:
        """done: forget it. Anything else: keep it, so it can be resumed."""
        if status == "done":
            self.path.unlink(missing_ok=True)
            return
        self.data["status"] = status
        self._save()


def unfinished() -> list[dict]:
    """Runs that did not end cleanly, newest first. A run still marked
    'running' when nothing is running was cut off by a crash or restart."""
    out = []
    for f in sorted(_dir().glob("*.json"), reverse=True):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("status") == "running":
            d["status"] = "interrupted"
        out.append(d)
    return out


def load(run_id: str) -> dict | None:
    f = _dir() / f"{run_id}.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def dismiss(run_id: str) -> bool:
    f = _dir() / f"{run_id}.json"
    if f.exists():
        f.unlink()
        return True
    return False
