"""Drive a real shopping site with System-1 decisions, and stop before money.

    python jev_live_shop.py                          migros, search for milk
    python jev_live_shop.py --url ... --goal "..."   anything else
    python jev_live_shop.py --headed                 watch it happen

This is the end-to-end test the whole branch exists for: a real page, a real
decision loop, real clicks. It is deliberately *not* a demo that completes a
purchase. The interesting question is not whether it can reach a checkout —
it is whether it **stops** at one, unprompted, because that is the only
property that makes a fast autonomous loop safe to leave running.

Every step: read the page into an indexed element table, ask for an operation
and a target in one round trip, run both guards, act. A `blocked` or `done`
operation ends the run, and so does either guard firing — a guard hit is a
successful outcome here, not a failure.

Nothing is ever typed into a payment field, and the run has a hard step
budget so a loop cannot wander.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import browser as browsermod   # noqa: E402
from core import fast, jev               # noqa: E402

MAX_STEPS = 8


def rows_from_snapshot(snap: dict) -> tuple[list[dict], list[int]]:
    """Indexed snapshot -> rows, plus the stamped index each row came from.

    The snapshot, not `elements()`. `snapshot()` writes `data-da-idx` onto
    every interactive node, and `act()` resolves a target by that attribute —
    so the index the model answers with is an attribute selector, not a
    search. `elements()` numbers a *different* list, and mixing the two is
    how the first run of this produced "no element for 8" after choosing the
    right control.

    The second return value is the map: rows are renumbered densely for the
    model, because dropping unnamed nodes makes the two sequences diverge.
    """
    rows, idx = [], []
    for e in snap.get("elements", []):
        name = (e.get("name") or "").strip()
        if not name or e.get("disabled"):
            continue
        rows.append({"type": e.get("role") or e.get("tag") or "",
                     "name": name[:80],
                     "id": e.get("testid") or "",
                     "value": e.get("value") or "",
                     "hint": "" if e.get("inview", True) else "off-screen"})
        idx.append(int(e["i"]))
    return rows, idx


def run(url: str, goal: str, typed: str, headed: bool) -> int:
    c = jev.Client(backend="openrouter", timeout=20.0)
    if c.offline:
        print("No OPENROUTER_API_KEY set.")
        return 1
    c.prewarm()

    b = browsermod.Browser(headless=not headed)
    if not b.start():
        print("browser failed to start")
        return 1

    try:
        print(f"opening {url}")
        b.goto(url)
        time.sleep(2.0)
        recent: list[tuple[str, str]] = []

        for step in range(1, MAX_STEPS + 1):
            snap = b.snapshot(limit=120)
            rows, stamped = rows_from_snapshot(snap)
            if not rows:
                print(f"[{step}] nothing readable on the page")
                break

            title = snap.get("title", "")

            t0 = time.perf_counter()
            m = fast.decide(goal, rows, window=f"{title} {b._host()}", client=c)
            dt = time.perf_counter() - t0

            target = rows[m.index]["name"] if m.index is not None else "-"
            print(f"\n[{step}] {dt * 1000:.0f}ms  op={m.operation}  "
                  f"target={target!r}")
            if m.blocker:
                print(f"     blocker: {m.blocker}")
            if not m.verdict.act:
                print(f"     HELD: {m.verdict.why}")

            if m.operation == "done":
                print("     goal reached")
                break
            if m.operation == "blocked":
                print("     handing back — this is where Claude takes over")
                break
            # Operations that act on a control need one; submit, scroll and
            # wait do not, and must not be dropped for lacking a target.
            needs_target = m.operation in ("click", "type", "select")
            if m.index is None and needs_target:
                # Either a guard fired or confidence was short. Both mean the
                # loop has gone as far as it should on its own.
                break

            # Back from the model's dense row number to the stamped DOM index.
            dom = stamped[m.index] if m.index is not None else None

            if m.operation == "submit":
                r = b.act({"do": "press", "value": "Enter"})
            elif m.operation == "click":
                r = b.act({"do": "click", "target": dom})
            elif m.operation == "type":
                r = b.act({"do": "fill", "target": dom, "value": typed})
            elif m.operation in ("scroll_down", "scroll_up"):
                r = b.act({"do": "scroll",
                           "value": "down" if m.operation == "scroll_down"
                                    else "up"})
            elif m.operation == "wait":
                time.sleep(1.5)
                r = {"ok": True, "did": "waited"}
            else:
                r = {"ok": False, "error": f"no handler for {m.operation}"}

            print(f"     -> {r.get('did') or r.get('error')}")
            if not r.get("ok"):
                break

            # A loop that keeps choosing the same move is not making progress,
            # whatever the page says. Showing the field's current value fixed
            # the cause; this catches whatever causes it next.
            recent.append((m.operation, target))
            if len(recent) >= 3 and len(set(recent[-3:])) == 1:
                print("     same move three times — stopping, not progressing")
                break

            time.sleep(1.5)

        print(f"\n{c.stats.report()}")
    finally:
        b.stop()
    return 0


def main(argv: list[str]) -> int:
    url = "https://www.migros.com.tr"
    goal = "search the site for milk (süt) and show the milk products"
    typed = "süt"
    headed = "--headed" in argv
    if "--url" in argv:
        url = argv[argv.index("--url") + 1]
    if "--goal" in argv:
        goal = argv[argv.index("--goal") + 1]
    if "--type" in argv:
        typed = argv[argv.index("--type") + 1]
    return run(url, goal, typed, headed)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
