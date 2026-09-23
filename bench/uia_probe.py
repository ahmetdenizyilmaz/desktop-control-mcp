"""Probe how much of a REAL app's UI is visible to UI Automation.

Answers: "will UIA work on my actual apps, or only on toy Tk windows?"

For each visible top-level window it walks the UIA tree and reports:
  elems        total elements found
  named        elements with a non-empty Name (semantics, not just geometry)
  actionable   elements exposing a clickable pattern (Invoke/Toggle/Select/Expand)
  depth        deepest nesting reached
  secs         wall clock for the walk
  verdict      RICH / PARTIAL / OPAQUE

OPAQUE means the app paints its own pixels and publishes almost nothing —
that is precisely where a vision fallback is required.

Run:  python bench/uia_probe.py
"""

import ctypes
import sys
import time

import uiautomation as auto

MAX_ELEMS = 1500     # stop walking a window after this many (keeps it fast)
MAX_DEPTH = 14
TIME_BUDGET = 6.0    # seconds per window

CLICK_PATTERNS = [
    (auto.PatternId.InvokePattern, "Invoke"),
    (auto.PatternId.TogglePattern, "Toggle"),
    (auto.PatternId.SelectionItemPattern, "Select"),
    (auto.PatternId.ExpandCollapsePattern, "Expand"),
]


def _is_cloaked(hwnd) -> bool:
    """DWM-cloaked windows exist but are NOT rendered.

    IsWindowVisible() still returns true for them, so without this check the
    probe counts invisible windows (suspended UWP apps, background shell
    surfaces) as UIA failures. cloaked: 0=shown, 1=app-cloaked, 2=shell-cloaked.
    """
    val = ctypes.c_int(0)
    try:
        ctypes.windll.dwmapi.DwmGetWindowAttribute(
            ctypes.wintypes.HWND(hwnd), 14, ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        return False
    return val.value != 0


def visible_windows(include_cloaked: bool = False):
    """Top-level windows with a title, actually rendered, non-trivial size."""
    u = ctypes.windll.user32
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _l):
        if not u.IsWindowVisible(hwnd):
            return True
        n = u.GetWindowTextLengthW(hwnd)
        if not n:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        r = ctypes.wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        if w > 200 and h > 150:
            if include_cloaked or not _is_cloaked(hwnd):
                out.append((hwnd, buf.value, w, h))
        return True

    u.EnumWindows(cb, None)
    return out


def probe(hwnd: str):
    """Walk one window's UIA tree under time/size budgets."""
    stats = {"elems": 0, "named": 0, "actionable": 0, "depth": 0, "truncated": False}
    t0 = time.time()

    try:
        root = auto.ControlFromHandle(hwnd)
    except Exception:
        return None
    if root is None:
        return None

    def walk(ctrl, depth):
        if stats["elems"] >= MAX_ELEMS or time.time() - t0 > TIME_BUDGET:
            stats["truncated"] = True
            return
        if depth > MAX_DEPTH:
            return
        try:
            children = ctrl.GetChildren()
        except Exception:
            return
        for ch in children:
            if stats["elems"] >= MAX_ELEMS or time.time() - t0 > TIME_BUDGET:
                stats["truncated"] = True
                return
            stats["elems"] += 1
            stats["depth"] = max(stats["depth"], depth)
            try:
                if (ch.Name or "").strip():
                    stats["named"] += 1
            except Exception:
                pass
            for pid, _label in CLICK_PATTERNS:
                try:
                    if ch.GetPattern(pid) is not None:
                        stats["actionable"] += 1
                        break
                except Exception:
                    pass
            walk(ch, depth + 1)

    walk(root, 1)
    stats["secs"] = round(time.time() - t0, 2)
    return stats


def verdict(s):
    if s["elems"] < 5:
        return "OPAQUE"
    if s["actionable"] >= 5 and s["named"] >= s["elems"] * 0.25:
        return "RICH"
    if s["elems"] >= 20 and s["named"] >= 3:
        return "PARTIAL"
    return "OPAQUE"


def main():
    import ctypes.wintypes  # noqa: F401
    wins = visible_windows()
    print(f"probing {len(wins)} visible windows\n")
    print(f"{'window':<38}{'elems':>7}{'named':>7}{'action':>8}{'depth':>7}{'secs':>7}  verdict")
    print("-" * 92)

    rows = []
    for hwnd, title, w, h in wins:
        s = probe(hwnd)
        if s is None:
            continue
        v = verdict(s)
        rows.append((title, s, v))
        t = (title[:35] + "...") if len(title) > 38 else title
        print(f"{t:<38}{s['elems']:>7}{s['named']:>7}{s['actionable']:>8}"
              f"{s['depth']:>7}{s['secs']:>7.2f}  {v}"
              + ("  (truncated)" if s["truncated"] else ""))

    print("-" * 92)
    tally = {}
    for _t, _s, v in rows:
        tally[v] = tally.get(v, 0) + 1
    print("verdicts:", tally)
    rich = tally.get("RICH", 0) + tally.get("PARTIAL", 0)
    if rows:
        print(f"\nUIA usable on {rich}/{len(rows)} windows "
              f"({rich/len(rows):.0%}) — the rest need a vision fallback.")


if __name__ == "__main__":
    main()
