"""Drive a real shopping site with System-1 decisions, and stop before money.

    python jev_live_shop.py
    python jev_live_shop.py --url ... --goals "find milk|open the first one"
    python jev_live_shop.py --headed          watch it happen

This is the end-to-end test the whole branch exists for: a real page, a real
decision loop, real clicks. It is deliberately *not* a demo that completes a
purchase. The interesting question is not whether it can reach a checkout —
it is whether it **stops** at one, unprompted, because that is the only
property that makes a fast autonomous loop safe to leave running.

**A plan is a list of narrow goals.** That is the division of labour the
branch argues for: Claude decides what the steps *are*, Jev decides how each
one is carried out. Measured: a single compound goal ("add milk to the basket
and then finish the order") makes the operation head hesitate at 0.65,
because searching and browsing are both reasonable first moves — the model
correctly reporting an underspecified question rather than failing.

Every step: read the page into an indexed element table, ask for an operation
and a target in one round trip, run both guards, act. `blocked` or `done`
ends the goal; either guard firing ends the plan. A guard hit is a successful
outcome here, not a failure.

Set JEV_LOG to journal every question and answer, then read it with
`jev_log.py`.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import browser as browsermod   # noqa: E402
from core import fast, jev               # noqa: E402

MAX_STEPS = 6

PLAN = [
    "type milk (süt) into the site search box and run the search",
    "open the product page of the first milk product in the results",
    "add this product to the shopping basket",
    "go to the shopping basket to see what is in it",
]


def rows_from_snapshot(snap: dict) -> tuple[list[dict], list[int]]:
    """Indexed snapshot -> rows, plus the stamped index each row came from.

    The snapshot, not `elements()`. `snapshot()` writes `data-da-idx` onto
    every interactive node and `act()` resolves a target by that attribute,
    so the index the model answers with is an attribute selector rather than
    a search. `elements()` numbers a *different* list, and mixing the two is
    how an early run chose the right control and then failed with "no element
    for 8".

    Rows are renumbered densely for the model because unnamed nodes are
    dropped, so the second return value is the map back.
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


def pursue(b, c, goal: str, typed: str, history: list[str]) -> bool:
    """Drive one goal to `done`, or stop. True if the plan may continue.

    `history` carries across goals, because "already searched for süt" is
    still true while pursuing "open the first result".
    """
    recent: list[tuple[str, str]] = []

    for step in range(1, MAX_STEPS + 1):
        snap = b.snapshot(limit=120)
        rows, stamped = rows_from_snapshot(snap)
        if not rows:
            print(f"  [{step}] nothing readable on the page")
            return False

        title = snap.get("title", "")
        t0 = time.perf_counter()
        m = fast.decide(goal, rows, window=f"{title} {b._host()}",
                        history=history, client=c)
        dt = time.perf_counter() - t0

        target = rows[m.index]["name"] if m.index is not None else "-"
        print(f"  [{step}] {dt * 1000:4.0f}ms  op={m.operation:<11} "
              f"target={target[:44]!r}")
        if m.blocker:
            print(f"        blocker: {m.blocker}")
        if not m.verdict.act:
            print(f"        HELD: {m.verdict.why}")

        if m.operation == "done":
            print("        goal reached")
            return True
        if m.operation == "blocked":
            print("        handing back — this is where Claude takes over")
            return False

        needs_target = m.operation in ("click", "type", "select")
        if m.index is None and needs_target:
            return False

        dom = stamped[m.index] if m.index is not None else None

        if m.operation == "submit":
            r = b.act({"do": "press", "value": "Enter"})
        elif m.operation == "click":
            r = b.act({"do": "click", "target": dom})
        elif m.operation == "type":
            r = b.act({"do": "fill", "target": dom, "value": typed})
        elif m.operation in ("scroll_down", "scroll_up"):
            r = b.act({"do": "scroll",
                       "value": "down" if m.operation == "scroll_down" else "up"})
        elif m.operation == "wait":
            time.sleep(1.5)
            r = {"ok": True, "did": "waited"}
        else:
            r = {"ok": False, "error": f"no handler for {m.operation}"}

        print(f"        -> {r.get('did') or r.get('error')}")
        if not r.get("ok"):
            return False

        # Record what was actually carried out, in the model's own words for
        # the operation and the page's words for the target.
        history.append(f"{m.operation} {target}" if target != "-"
                       else m.operation)

        # A loop choosing the same move is not progressing, whatever the page
        # says. Carrying a field's current value fixed the known cause; this
        # catches the next one.
        recent.append((m.operation, target))
        if len(recent) >= 3 and len(set(recent[-3:])) == 1:
            print("        same move three times — stopping, not progressing")
            return False

        time.sleep(2.0)

    print(f"  step budget ({MAX_STEPS}) spent on this goal")
    return False


def run(url: str, goals: list[str], typed: str, headed: bool) -> int:
    c = jev.Client(backend="openrouter", timeout=20.0)
    if c.offline:
        print("No OPENROUTER_API_KEY set.")
        return 1
    if c.log_path:
        print(f"journalling every call to {c.log_path}")
    c.prewarm()

    b = browsermod.Browser(headless=not headed)
    if not b.start():
        print("browser failed to start")
        return 1

    try:
        print(f"opening {url}")
        b.goto(url)
        time.sleep(2.5)

        # Carried across goals: "already searched for süt" stays true while
        # pursuing "open the first result".
        history: list[str] = []
        for i, goal in enumerate(goals, 1):
            print(f"\n{'=' * 70}\nGOAL {i}/{len(goals)}: {goal}\n{'=' * 70}")
            if not pursue(b, c, goal, typed, history):
                print("  plan stops here")
                break

        try:
            print(f"\nfinal page: {b._ensure().url[:100]}")
        except Exception:
            pass
        print(c.stats.report())
    finally:
        b.stop()
    return 0


def main(argv: list[str]) -> int:
    url = "https://www.migros.com.tr"
    goals = list(PLAN)
    typed = "süt"
    headed = "--headed" in argv
    if "--url" in argv:
        url = argv[argv.index("--url") + 1]
    if "--goals" in argv:
        goals = [g.strip() for g in argv[argv.index("--goals") + 1].split("|")
                 if g.strip()]
    if "--type" in argv:
        typed = argv[argv.index("--type") + 1]
    return run(url, goals, typed, headed)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
