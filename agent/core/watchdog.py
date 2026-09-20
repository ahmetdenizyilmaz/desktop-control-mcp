"""Progress watchdog — keep a long task moving instead of letting it die quietly.

The failure this exists to prevent: an agent stops mid-task and nothing
notices. Not a crash — a crash is loud. It declares an intention and never
acts on it, or waits on something that will never arrive, and the run simply
sits there looking busy.

The watchdog is deliberately not a timeout. A timeout kills work that is merely
slow; this distinguishes *slow* from *stalled* by asking what has changed:

    heartbeat   the task reports progress (a step completed, an event emitted)
    stall       no heartbeat for `stall_after`, while the task claims to run
    nudge       the task is told to continue, and given what it last did
    escalate    after `max_nudges`, stop and ask the user

Nudging matters more than alerting: an unattended overnight run that pauses at
2am and waits for a human has failed, even though nothing errored.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class TaskState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    STALLED = "stalled"
    NUDGED = "nudged"
    DONE = "done"
    FAILED = "failed"
    ESCALATED = "escalated"     # gave up; needs a human


@dataclass
class TaskProgress:
    task_id: str
    label: str = ""
    state: TaskState = TaskState.IDLE
    started_at: float = 0.0
    last_beat: float = 0.0
    beats: int = 0
    nudges: int = 0
    last_note: str = ""
    history: list[tuple[float, str]] = field(default_factory=list)

    @property
    def idle_s(self) -> float:
        return time.time() - self.last_beat if self.last_beat else 0.0

    @property
    def elapsed_s(self) -> float:
        return time.time() - self.started_at if self.started_at else 0.0

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id, "label": self.label,
            "state": self.state.value,
            "elapsed_s": round(self.elapsed_s, 1),
            "idle_s": round(self.idle_s, 1),
            "beats": self.beats, "nudges": self.nudges,
            "last_note": self.last_note,
            "recent": [{"t": round(t, 1), "note": n} for t, n in self.history[-8:]],
        }


class Watchdog:
    """Watches registered tasks and nudges the ones that stop progressing.

    `check_every` is the sweep cadence; `stall_after` is how long without a
    heartbeat counts as stalled. They are separate on purpose — checking
    hourly but only tolerating ten minutes of silence is a reasonable
    configuration, and so is the reverse.
    """

    def __init__(self, check_every_s: float = 3600.0,
                 stall_after_s: float = 600.0,
                 max_nudges: int = 3,
                 on_nudge: Callable[[TaskProgress], None] | None = None,
                 on_escalate: Callable[[TaskProgress], None] | None = None):
        self.check_every_s = check_every_s
        self.stall_after_s = stall_after_s
        self.max_nudges = max_nudges
        self.on_nudge = on_nudge
        self.on_escalate = on_escalate

        self.tasks: dict[str, TaskProgress] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_sweep: float = 0.0
        self.sweeps: int = 0

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # -- task registration ------------------------------------------------
    def begin(self, task_id: str, label: str = "") -> TaskProgress:
        with self._lock:
            p = TaskProgress(task_id=task_id, label=label,
                             state=TaskState.RUNNING,
                             started_at=time.time(), last_beat=time.time())
            self.tasks[task_id] = p
            return p

    def beat(self, task_id: str, note: str = "") -> None:
        """Report progress. Anything that advances the task should call this."""
        with self._lock:
            p = self.tasks.get(task_id)
            if p is None:
                return
            p.last_beat = time.time()
            p.beats += 1
            if note:
                p.last_note = note
                p.history.append((p.elapsed_s, note))
                del p.history[:-40]
            # A heartbeat after a nudge means the nudge worked.
            if p.state in (TaskState.STALLED, TaskState.NUDGED):
                p.state = TaskState.RUNNING

    def finish(self, task_id: str, ok: bool = True, note: str = "") -> None:
        with self._lock:
            p = self.tasks.get(task_id)
            if p is None:
                return
            p.state = TaskState.DONE if ok else TaskState.FAILED
            if note:
                p.last_note = note
                p.history.append((p.elapsed_s, note))

    def forget(self, task_id: str) -> None:
        with self._lock:
            self.tasks.pop(task_id, None)

    # -- the sweep -------------------------------------------------------
    def sweep(self) -> list[TaskProgress]:
        """One pass. Returns the tasks acted on."""
        acted: list[TaskProgress] = []
        self.last_sweep = time.time()
        self.sweeps += 1

        with self._lock:
            candidates = [p for p in self.tasks.values()
                          if p.state in (TaskState.RUNNING, TaskState.STALLED,
                                         TaskState.NUDGED)]

        for p in candidates:
            if p.idle_s < self.stall_after_s:
                continue

            if p.nudges >= self.max_nudges:
                p.state = TaskState.ESCALATED
                acted.append(p)
                if self.on_escalate:
                    try:
                        self.on_escalate(p)
                    except Exception:
                        pass
                continue

            p.state = TaskState.NUDGED
            p.nudges += 1
            acted.append(p)
            if self.on_nudge:
                try:
                    self.on_nudge(p)
                except Exception:
                    pass
        return acted

    def _loop(self) -> None:
        # Sweep on a shorter tick than check_every so a stall is noticed near
        # when it happens, not up to a full interval later.
        tick = max(min(self.check_every_s, self.stall_after_s) / 4, 5.0)
        next_full = time.time() + self.check_every_s
        while not self._stop.wait(tick):
            try:
                self.sweep()
                if time.time() >= next_full:
                    next_full = time.time() + self.check_every_s
            except Exception:
                continue

    # -- reporting -------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            tasks = [p.as_dict() for p in self.tasks.values()]
        return {
            "running": self.running,
            "check_every_s": self.check_every_s,
            "stall_after_s": self.stall_after_s,
            "max_nudges": self.max_nudges,
            "sweeps": self.sweeps,
            "last_sweep": self.last_sweep,
            "tasks": tasks,
            "stalled": [t for t in tasks
                        if t["state"] in ("stalled", "nudged", "escalated")],
        }

    def continuation_prompt(self, p: TaskProgress) -> str:
        """What to tell an agent that stopped mid-task.

        Restating what it was doing and what it last achieved is what makes a
        nudge useful rather than a bare 'continue' into an empty context.
        """
        recent = "; ".join(n for _, n in p.history[-4:]) or "no recorded progress"
        return (
            f"The task '{p.label or p.task_id}' has not reported progress for "
            f"{p.idle_s / 60:.0f} minutes and is not finished. "
            f"It has been running {p.elapsed_s / 60:.0f} minutes with {p.beats} "
            f"steps completed. Most recent progress: {recent}. "
            f"Continue from where it stopped. Do not restart from the beginning, "
            f"and do not wait for confirmation — if something is blocking, say "
            f"what it is and take the next action you can."
        )


#: process-wide instance the daemon shares
watchdog = Watchdog()
