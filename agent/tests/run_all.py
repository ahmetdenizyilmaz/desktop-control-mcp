"""Run every suite and report one number.

Each suite runs as a separate process. That is deliberate: several of them
install low-level Windows hooks, take the cross-process actuation lock, or
change display settings and restore them on exit. Sharing one interpreter
would let one suite's teardown break the next one's setup, and a false pass
here is worse than no suite at all.

    python tests/run_all.py            all suites
    python tests/run_all.py 05 04      only the ones matching

Suites that drive real applications are marked interactive — they move the
mouse and take focus, so do not use the machine while they run.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

SUITES = [
    ("test_phase01", "element resolution and actuation", True),
    ("test_phase02", "arbiter — who drives, and how you take it back", True),
    ("test_phase04", "portability, trust and learning", False),
    ("test_phase05", "credentials and channel", False),
    ("test_phase08", "finding a job by describing it", False),
    ("test_lessons", "task memory — save on struggle, recall next time", False),
    ("test_clipboard", "clipboard — paste a screenshot into a web app", False),
    ("test_desktop_layer", "seeing and typing — layout, names, walk, capture", False),
    ("test_phase09", "scheduling — running work on a cadence", False),
    ("test_phase10", "demonstration to job, and editing one", False),
    ("test_stop_during_question", "stop and close while asking", False),
    ("test_labelling", "OCR labelling of unnamed controls", True),
    ("test_browser", "browser control", False),
    ("test_phase11", "indexed page, batched actions, remembered selectors", False),
    ("test_phase12", "recording the browser, and replaying it", False),
    ("test_codex_backend", "Codex routing, login status and cancellation", False),
    ("test_engine", "script engine — Jev first, Claude second, you third", False),
    ("test_grounding", "vision grounding fallback — mapping, gates, degradation", False),
    ("test_endtoend", "drive a real application", True),
]

def machine_is_busy(seconds: float = 3.0) -> str:
    """Is somebody using this computer right now?

    The interactive suites drive real applications and fight for the
    foreground. Run while the machine is in use, they do not fail cleanly —
    they report things like "click 'Save' registered: app logged None" and
    "could not foreground window", which read as regressions in the code
    rather than as what they are. One such run cost a real diagnosis before
    the cause turned out to be a disk imager and a calculator.

    Returns a reason to skip, or "" if the machine looks idle.
    """
    try:
        sys.path.insert(0, str(HERE.parent))
        import ctypes

        from core import locator
        from core.input_monitor import ActivityTracker, monitor
    except Exception:
        return ""                      # cannot tell; do not block the run

    tracker = ActivityTracker()
    sub = None
    try:
        monitor.start()
        sub = monitor.subscribe(tracker)
        time.sleep(seconds)
        if tracker.intent_count:
            return (f"physical input {tracker.intent_count} time(s) in the last "
                    f"{seconds:.0f}s")
    except Exception:
        return ""
    finally:
        if sub is not None:
            try:
                monitor.unsubscribe(sub)
            except Exception:
                pass

    # Something long-running and visible is a good sign the machine is in use
    # even when nobody is touching it this second.
    # A bare "%" was tried and is far too broad: a Chrome tab showing a stock
    # price ("SILVER 69,4250 - +2.01%") read as a job in progress. A real
    # progress title names the verb, so require that — and only treat a
    # percentage as evidence when it sits beside one.
    BUSY_WORDS = ("flashing", "installing", "updating", "copying",
                  "downloading", "burning", "extracting", "verifying")
    pct_progress = re.compile(r"\d{1,3}\s?%\s*(" + "|".join(BUSY_WORDS) + ")")
    try:
        for w in locator.list_windows():
            title = (w.get("title") or "").lower()
            if any(k in title for k in BUSY_WORDS) or pct_progress.search(title):
                return f"a job looks to be running: {w['title'][:44]!r}"
    except Exception:
        pass
    return ""


COUNT_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s+(?:checks\s+)?passed")


def main(argv: list[str]) -> int:
    picks = [a.lstrip("-") for a in argv[1:] if not a.startswith("--")]
    suites = [s for s in SUITES
              if not picks or any(p in s[0] for p in picks)]
    if not suites:
        print(f"no suite matches {picks}. known: "
              + ", ".join(s[0] for s in SUITES))
        return 2

    width = max(len(s[0]) for s in suites)
    print("=" * 78)
    print(f"AGI AGENT — {len(suites)} suite(s)")
    if any(s[2] for s in suites):
        print("Some suites drive real applications: they move the mouse and take")
        print("focus. Physical input will pause them by design, so leave the")
        print("machine alone while they run.")
    print("=" * 78)

    total_pass = total_all = 0
    rows = []
    t_start = time.time()

    busy = ""
    if any(s[2] for s in suites) and "--force" not in argv:
        busy = machine_is_busy()
        if busy:
            print("")
            print(f"MACHINE IN USE — {busy}")
            print("Skipping the suites that drive real applications. They would")
            print("fight you for the foreground and report failures that are")
            print("really contention. Re-run with --force to do it anyway.")
            suites = [s for s in suites if not s[2]]

    for name, blurb, interactive in suites:
        t0 = time.time()
        print(f"\n>>> {name}  —  {blurb}", flush=True)
        # Output goes to a FILE, not a pipe. Several suites launch a fixture
        # window as a grandchild, and a grandchild inherits the pipe: the
        # suite finishes, the fixture keeps the write end open, and
        # subprocess.run blocks forever waiting for an EOF that never comes.
        # This is not hypothetical — it hung the whole runner for ten minutes.
        with tempfile.NamedTemporaryFile(suffix=f"_{name}.log", delete=False,
                                         mode="w", encoding="utf-8") as fh:
            log_path = Path(fh.name)
        try:
            with open(log_path, "w", encoding="utf-8") as sink:
                p = subprocess.run([sys.executable, "-u", str(HERE / f"{name}.py")],
                                   stdout=sink, stderr=subprocess.STDOUT,
                                   timeout=900)
            out = log_path.read_text(encoding="utf-8", errors="replace")
        finally:
            try:
                log_path.unlink(missing_ok=True)
            except OSError:
                # A fixture window the suite launched inherited the handle and
                # is still holding it. Harmless — it is a temp file, and the OS
                # will reclaim it. Failing the whole run over it would not be.
                pass
        m = None
        for m in COUNT_RE.finditer(out):
            pass
        took = time.time() - t0
        if m:
            got, want = int(m.group(1)), int(m.group(2))
        else:
            got, want = 0, 0
        total_pass += got
        total_all += want
        # A crash is not a pass, even if the counters looked fine before it.
        crashed = p.returncode != 0 and got == want and want > 0
        status = ("OK" if (p.returncode == 0 and got == want and want)
                  else "CRASH" if crashed or want == 0 else "FAIL")
        rows.append((name, status, got, want, took))
        print(f"    {status}  {got}/{want}  {took:.1f}s", flush=True)
        if status != "OK":
            for line in out.splitlines():
                if "FAIL" in line or "Error" in line or "Traceback" in line:
                    print(f"      {line.strip()[:110]}")
            if want == 0:
                print(f"      (no result line found; exit={p.returncode})")
                print("      " + "\n      ".join(out.strip().splitlines()[-6:]))

    # A suite that crashes mid-way leaves its fixture window on screen, and a
    # stray fixture is the next run's phantom failure. Always sweep — windows
    # and the temp directories a crashed suite never got to remove.
    cleanup = HERE / "cleanup.py"
    if cleanup.exists():
        c = subprocess.run([sys.executable, str(cleanup)], capture_output=True,
                           text=True, timeout=120, encoding="utf-8",
                           errors="replace")
        for line in (c.stdout or "").splitlines():
            if line.startswith(("closed windows", "temp dirs")) \
                    and not re.search(r"(?::|removed) 0\b", line):
                print(f"\n[cleanup] {line.strip()}")

    print("\n" + "=" * 78)
    for name, status, got, want, took in rows:
        print(f"  {name:<{width}}  {status:<5}  {got:>3}/{want:<3}  {took:>5.1f}s")
    print("-" * 78)
    ok = all(r[1] == "OK" for r in rows)
    print(f"  {'TOTAL':<{width}}  {'OK' if ok else 'FAIL':<5}  "
          f"{total_pass:>3}/{total_all:<3}  {time.time() - t_start:>5.1f}s")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
