"""Naming-gap test: does OCR labelling make unnamed controls addressable?

Measured gap this closes — a Tk window publishes 29 ButtonControls of which
only 3 carry a Name. UIA locates every one of them perfectly and cannot say
what any of them is. Teams shows the same shape (64 elements, 9 named).

Run:  python agent/tests/test_labelling.py
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except (AttributeError, OSError):
    pass

from core import act, actuator, display, locator  # noqa: E402
from core.schema import Locator, LocatorType     # noqa: E402

REPO = Path(__file__).resolve().parents[2]
TARGET = REPO / "bench" / "target_app.py"
CLICK_LOG = REPO / "bench" / "clicks.log"

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""),
          flush=True)
    return ok


def clicks():
    import json
    if not CLICK_LOG.exists():
        return []
    return [json.loads(l) for l in CLICK_LOG.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def _norm_of(hwnd, label):
    """Window-relative coords for an OCR-labelled control."""
    el = locator.find_labelled(hwnd, label)
    wx1, wy1, wx2, wy2 = locator.window_rect(hwnd)
    w, h = max(wx2 - wx1, 1), max(wy2 - wy1, 1)
    cx, cy = el.center
    return {"x": (cx - wx1) / w, "y": (cy - wy1) / h, "of": "window"}


def ensure_target():
    h = locator.find_window(title_contains="BENCH TARGET")
    if h:
        return h
    subprocess.Popen([sys.executable, str(TARGET)], cwd=str(REPO))
    for _ in range(30):
        time.sleep(0.5)
        h = locator.find_window(title_contains="BENCH TARGET")
        if h:
            time.sleep(1.0)
            return h
    return None


def main():
    print("=" * 72)
    print("NAMING GAP — UIA locates, OCR labels")
    print("=" * 72)
    if display.is_locked():
        print("\nABORT: workstation locked — OCR would read the lock screen.")
        return 2

    keeper = display.KeepSessionOpen()
    keeper.__enter__()
    try:
        return _run()
    finally:
        keeper.__exit__(None, None, None)


def _run():
    h = ensure_target()
    if not check("bench target running", h is not None):
        return 1
    actuator.focus_window(h)
    time.sleep(0.8)
    locator.invalidate_cache()

    # --- baseline: how many controls does UIA name on its own? ------------
    root = locator.uia_root(h)
    total = named = 0
    for c in locator.walk(root, max_depth=14, max_nodes=1200, budget_s=4.0):
        try:
            if c.ControlTypeName == "ButtonControl":
                total += 1
                if (c.Name or "").strip():
                    named += 1
        except Exception:
            pass
    check("baseline naming measured", total > 0,
          f"{named}/{total} ButtonControls named by UIA alone")

    # --- labelling pass ---------------------------------------------------
    print("\n  running OCR labelling pass (first call loads models)...", flush=True)
    t0 = time.time()
    labels = locator.label_elements(h, force=True)
    dt = time.time() - t0
    check("labelling produced names", len(labels) > 0,
          f"{len(labels)} elements labelled in {dt:.1f}s")

    coverage = (named + len(labels)) / total if total else 0
    check("coverage improved over UIA alone", len(labels) > named,
          f"{named}/{total} -> ~{named+len(labels)}/{total} ({coverage:.0%})")

    flat = locator.labels_for(h)
    print("    sample:", ", ".join(f"{l!r}" for l in list(flat.values())[:8]))

    # --- cache ------------------------------------------------------------
    t0 = time.time()
    locator.label_elements(h)
    cached_ms = (time.time() - t0) * 1000
    check("second call is cached", cached_ms < 50, f"{cached_ms:.1f}ms")

    # --- resolution by label ---------------------------------------------
    print()
    hits = 0
    wanted = ["Save", "Open", "Delete", "Export", "Import", "Commit"]
    for want in wanted:
        el = locator.find_labelled(h, want)
        ok = el is not None
        hits += ok
        check(f"find_labelled({want!r})", ok,
              f"rect={el.rect} src={el.source}" if el else "not found")
    check("majority of named targets resolvable", hits >= len(wanted) * 0.6,
          f"{hits}/{len(wanted)}")

    # Resolvable is not the same as correct, and six hand-picked targets are
    # not a sample. Score EVERY text-bearing control against ground truth.
    # Glyph and canvas-drawn controls are excluded because they draw no text
    # to read — counting them would understate the method, ignoring them
    # silently would overstate it, so they are reported separately.
    import json
    all_gt = json.loads(
        (REPO / "bench" / "ground_truth.json").read_text(encoding="utf-8"))["buttons"]
    textual = [b for b in all_gt if b["kind"] == "text"]
    glyphs = [b for b in all_gt if b["kind"] != "text"]

    correct, wrong, missing = [], [], []
    for b in textual:
        el = locator.find_labelled(h, b["label"])
        if el is None:
            missing.append(b["label"])
            continue
        r = b["rect"]
        cx, cy = el.center
        if r["x1"] <= cx <= r["x2"] and r["y1"] <= cy <= r["y2"]:
            correct.append(b["label"])
        else:
            wrong.append(f"{b['label']}(got {el.size[0]}x{el.size[1]})")

    n = len(textual)
    print(f"       scored {n} text targets ({len(glyphs)} glyph/drawn excluded — "
          f"no text to read)")

    # Precision is the property that matters: a label landing on the WRONG
    # control makes a macro click the wrong thing, which is worse than not
    # finding it at all.
    check("zero mis-assignments", not wrong,
          "; ".join(wrong) if wrong else "every label found landed on its own control")
    check("recall on normal-size controls",
          all(b["label"] in correct for b in textual if not b["tiny"]),
          f"{sum(1 for b in textual if not b['tiny'] and b['label'] in correct)}"
          f"/{sum(1 for b in textual if not b['tiny'])}")
    acc = len(correct) / n if n else 0
    check("overall accuracy >= 85%", acc >= 0.85,
          f"{len(correct)}/{n} ({acc:.0%})"
          + (f" — missed {', '.join(missing)}: sub-26px text is at EasyOCR's "
             f"limit; 2x upscale trades one miss for another, not a fix"
             if missing else ""))

    # --- end to end: click a previously-unaddressable control by name -----
    print()
    el = locator.find_labelled(h, "Export")
    if el:
        check("resolved 'Export' to a plausible button",
              (el.rect[2] - el.rect[0]) >= 60,
              f"rect={el.rect} size={el.size}")
        # Direct actuator call: Tk publishes an InvokePattern that no-ops, so
        # the low-level primitive cannot tell success from silence. Real input
        # is the correct choice for this toolkit.
        before = len(clicks())
        r = actuator.click(el, prefer="inject")
        time.sleep(0.6)
        after = clicks()
        landed = after[-1]["label"] if len(after) > before else None
        check("click an unnamed control BY NAME", landed == "Export",
              f"{r.method} {r.latency_ms:.0f}ms, app logged {landed!r}")

        # And through the real API, where a failed post-condition triggers the
        # invoke -> input fallback automatically.
        from core.schema import Action, Step, Verify
        before = len(clicks())
        step = Step(
            action=Action.CLICK,
            intent="Press Import, addressed only by its drawn label",
            locators=[Locator(LocatorType.NORM_COORD, _norm_of(h, "Import"))],
            verify=Verify(window_title_regex="BENCH TARGET", timeout_s=3),
        )
        sr = act.do_step(step, h)
        time.sleep(0.6)
        after = clicks()
        landed = after[-1]["label"] if len(after) > before else None
        print(f"       {sr.summary()}")
        check("do_step reaches an OCR-labelled control", landed == "Import",
              f"app logged {landed!r}")

    passed = sum(1 for _, ok, _ in results if ok)
    print("\n" + "=" * 72)
    print(f"{passed}/{len(results)} checks passed")
    fails = [(n, d) for n, ok, d in results if not ok]
    if fails:
        print("\nfailures:")
        for n, d in fails:
            print(f"  - {n}  {d}")
    print("=" * 72)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
