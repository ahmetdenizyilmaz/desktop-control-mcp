"""Check a Jev key works, and measure what it actually costs from here.

    python jev_probe.py                 whichever backend is configured
    python jev_probe.py openrouter      force one
    python jev_probe.py --calls 10      more samples for a better p50

The number this exists to get is **round-trip latency from this machine**.
TypeSafe quote 70-500ms and their Doom demo held 212ms median, but those were
measured next to the API. Every design decision on this branch — the 5Hz loop,
asking the commit guard before every click, batching four heads into one call
— rests on the real figure from here, and nobody has measured it.

It also answers the cheaper question of whether a key works at all, which is
worth its own command when access arrives in waves and you have three possible
routes in.

No key configured anywhere? It says so and exits, rather than running the
offline stub and printing a latency that means nothing.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import jev   # noqa: E402

#: Small, realistic, and shaped like what the desktop loop actually sends: a
#: control table plus the questions `fast.decide()` asks about one.
STATE = {
    "window": "Flights — results",
    "controls": [
        {"i": 0, "t": "Button", "n": "Search"},
        {"i": 1, "t": "Edit", "n": "From"},
        {"i": 2, "t": "Edit", "n": "To"},
        {"i": 3, "t": "Button", "n": "Sort by price"},
        {"i": 4, "t": "Text", "n": "Loading results…"},
    ],
}

QUESTIONS = {
    "op": jev.Choice(
        instructions={"goal": "sort the results so the cheapest is first",
                      "task": "What kind of action moves this goal forward?"},
        criteria={"click": "Press a control", "wait": "Still loading",
                  "scroll_down": "What is needed is below the fold",
                  "done": "Already achieved"}),
    "pick": jev.Choice(
        instructions={"goal": "sort the results so the cheapest is first",
                      "task": "Which control does that? Answer with its index."},
        criteria={"0": "Button: Search", "1": "Edit: From", "2": "Edit: To",
                  "3": "Button: Sort by price", "4": "Text: Loading results…",
                  "none": "No control here does that"}),
    "loading": jev.Noul(
        instructions="Is the page still loading?",
        criteria={"true": "A spinner, skeleton or 'loading' text is present",
                  "false": "The content looks settled"}),
}


def configured() -> list[str]:
    return [name for name, spec in jev.BACKENDS.items()
            if os.environ.get(spec["env"])]


def probe(backend: str, calls: int) -> int:
    print(f"\n=== {backend} ===")
    try:
        c = jev.Client(backend=backend, timeout=10.0)
    except ValueError as e:
        print(f"  cannot build client: {e}")
        return 1
    if c.offline:
        print(f"  no key in {jev.BACKENDS[backend]['env']} — skipped")
        return 1
    print(f"  endpoint {c.endpoint}")
    print(f"  model    {c.model}")

    try:
        first = c.ask(STATE, QUESTIONS)
    except jev.JevError as e:
        print(f"  FAILED: {e}")
        return 1

    print(f"  answered by {first.model}")
    for key, a in first.answers.items():
        print(f"    {key:<8} {a}")
    print(f"  {first.input_tokens} input tokens, ${first.cost_usd:.6f}")

    # The answers themselves are a smoke test: this state has a control that
    # plainly sorts by price and a plainly visible loading indicator. If it
    # picks 3 and says the page is loading, the wiring is right end to end.
    op = first.answers.get("op")
    pick = first.answers.get("pick")
    sane = (getattr(op, "choice", "") in {"click", "wait"}
            and getattr(pick, "choice", "") in {"3", "none"})
    print(f"  sanity: {'looks right' if sane else 'UNEXPECTED — check the state'}")

    for _ in range(max(0, calls - 1)):
        try:
            c.ask(STATE, QUESTIONS)
        except jev.JevError as e:
            print(f"  call failed: {e}")

    s = c.stats
    print(f"  {s.report()}")
    p50 = s._pct(0.5)
    if p50:
        print(f"\n  At 1 call per action that is {p50 * 1000:.0f}ms of thinking "
              f"per step.")
        print(f"  A 5Hz loop needs <200ms: "
              f"{'comfortable' if p50 < 0.2 else 'tight' if p50 < 0.4 else 'too slow — drop to 2Hz'}")
        hourly = s.cost_usd / max(s.calls, 1) * 5 * 3600
        print(f"  Sustained 5Hz would cost about ${hourly:.2f}/hour.")
    return 0


def main(argv: list[str]) -> int:
    calls = 5
    if "--calls" in argv:
        i = argv.index("--calls")
        calls = int(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    wanted = [a for a in argv[1:] if not a.startswith("-")]

    have = configured()
    if not wanted and not have:
        print("No Jev key found in the environment. Set one of:\n")
        for name, spec in jev.BACKENDS.items():
            extra = " (+ CLOUDFLARE_ACCOUNT_ID)" if name == "cloudflare" else ""
            print(f"  {name:<11} {spec['env']}{extra}")
        print("\nOpenRouter needs no TypeSafe invite and is the quickest route:")
        print("  key at openrouter.ai/keys, then")
        print("  $env:OPENROUTER_API_KEY = 'sk-or-...'   (PowerShell)")
        return 1

    targets = wanted or have
    print(f"probing: {', '.join(targets)}  ({calls} calls each)")
    worst = 0
    for name in targets:
        worst = max(worst, probe(name, calls))
    return worst


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
