"""Where a task's time and money went - every model, not just the agent.

A task's cost used to be the Claude agent's alone: the provider's bill at
the end of the SDK run. Everything around it was invisible - the Opus call
that wrote the script, the two Opus rescues with a screenshot each, Jev's
decisions, the small model that pulled a song name out of the request. On
the first live Notepad run the window said $0.67 and the real total was
higher.

So one ledger per task, fed by every place a model is called or time is
spent, and read by the window:

    decide    Jev (or a Claude decider) picking a script or a product
    details   filling a script's parameters from the task
    write     Claude writing a new script
    script    the script itself running
    rescue    Claude unsticking a step, screenshot and all
    agent     the full Claude agent (a takeover, or a task with no script)
    waiting   time spent waiting for YOU - shown, never counted as slow

Costs are what the provider reported where it reports one (the Claude CLI,
the SDK, OpenRouter with usage accounting); Jev's is its published
per-token price. An entry with no known cost is shown as such, never as $0.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

ORDER = ("decide", "details", "write", "script", "rescue", "agent", "waiting")
LABEL = {"decide": "deciding", "details": "details", "write": "writing script",
         "script": "script", "rescue": "Claude rescue", "agent": "Claude agent",
         "waiting": "waiting for you"}


@dataclass
class Entry:
    kind: str
    seconds: float
    cost: float | None = None
    detail: str = ""
    at: float = field(default_factory=time.time)


class Ledger:
    def __init__(self, task: str = "") -> None:
        self.task = task
        self.started = time.time()
        self.entries: list[Entry] = []
        self._lock = threading.Lock()

    def add(self, kind: str, seconds: float, cost: float | None = None,
            detail: str = "") -> None:
        with self._lock:
            self.entries.append(Entry(kind, max(0.0, float(seconds)),
                                      None if cost is None else float(cost),
                                      detail))

    def totals(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        with self._lock:
            for e in self.entries:
                t = out.setdefault(e.kind, {"seconds": 0.0, "cost": 0.0,
                                            "calls": 0, "unpriced": 0})
                t["seconds"] += e.seconds
                t["calls"] += 1
                if e.cost is None:
                    t["unpriced"] += 1
                else:
                    t["cost"] += e.cost
        return out

    def cost(self) -> float:
        return sum(t["cost"] for t in self.totals().values())

    def view(self) -> dict:
        tot = self.totals()
        return {"elapsed": time.time() - self.started, "cost": self.cost(),
                "parts": [{"kind": k, "label": LABEL.get(k, k), **tot[k]}
                          for k in ORDER if k in tot]}

    def summary(self) -> str:
        """Two short lines for the feed at the end of a task."""
        tot = self.totals()
        if not tot:
            return ""

        def secs(s: float) -> str:
            return f"{s:.1f}s" if s < 60 else f"{int(s // 60)}m {int(s % 60)}s"

        times, costs = [], []
        for k in ORDER:
            if k not in tot:
                continue
            t = tot[k]
            n = f" ×{t['calls']}" if t["calls"] > 1 and k in ("rescue",
                                                             "decide",
                                                             "details") else ""
            times.append(f"{LABEL[k]} {secs(t['seconds'])}{n}")
            if k in ("waiting", "script"):
                continue                     # no model involved: nothing to pay
            if t["cost"] or not t["unpriced"]:
                costs.append(f"{LABEL[k]} {money(t['cost'])}")
            else:
                costs.append(f"{LABEL[k]} (cost not reported)")
        line1 = "Time: " + " · ".join(times)
        total = self.cost()
        unknown = any(t["unpriced"] for k, t in tot.items()
                      if k not in ("waiting", "script"))
        head = (f"Cost: {money(total)}" if total or not unknown
                else "Cost: not reported")
        if not costs:
            head = "Cost: $0 — no model was used"
        if total and unknown:
            head += " + some not reported"
        line2 = head + ("  = " + " + ".join(costs) if costs else "")
        return line1 + "\n" + line2


def money(c: float) -> str:
    """$0.67, $0.004, $0.00003 - enough digits to not read as zero."""
    if c >= 0.01:
        return f"${c:.2f}"
    if c >= 0.0001:
        return f"${c:.4f}"
    return f"${c:.6f}" if c else "$0"


#: The ledger of the task in progress. Set by the app for the life of one
#: task; the helpers that call models record into it without needing to be
#: handed it.
current: Ledger | None = None


def record(kind: str, seconds: float, cost: float | None = None,
           detail: str = "") -> None:
    led = current
    if led is not None:
        led.add(kind, seconds, cost, detail)
