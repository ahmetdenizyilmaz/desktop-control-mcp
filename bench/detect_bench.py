"""Compare UI-element detection methods against known ground truth.

Methods:
  A  vision      YOLO + Florence-2 + EasyOCR      (the current pipeline)
  B  uia         UI Automation tree
  C  win32       EnumChildWindows + GetWindowRect
  D  ocr         EasyOCR only (no YOLO, no Florence)

Method E (plain screenshot, model estimates coordinates) is run separately —
this script just saves the screenshot for it.

Metrics per method:
  latency_s      wall clock for one full detection pass
  found          ground-truth buttons matched by at least one detection
  recall         found / total
  hit_rate       matched detections whose point lands INSIDE the true rect
                 (the metric that actually decides whether a click works)
  mean_err_px    distance from detection point to true center, matched only
  n_elements     raw detections returned (noise indicator)

Run:  python bench/detect_bench.py         (target_app.py must be running)
"""

import ctypes
import json
import math
import sys
import time
from pathlib import Path

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except (AttributeError, OSError):
    pass

sys.path.insert(0, str(Path(__file__).parent.parent))

BENCH_DIR = Path(__file__).parent
GROUND_TRUTH = BENCH_DIR / "ground_truth.json"
RESULTS = BENCH_DIR / "detect_results.json"


# ----------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------

def inside(pt, rect) -> bool:
    return rect["x1"] <= pt[0] <= rect["x2"] and rect["y1"] <= pt[1] <= rect["y2"]


def score(method: str, detections: list, truth: list, latency: float) -> dict:
    """detections: [{'point': (x,y), 'label': str}] — label may be ''."""
    matched, hits, errors = 0, 0, []
    per_hit = {}   # gt id -> bool, for the category breakdown

    for gt in truth:
        rect, center = gt["rect"], (gt["center"]["x"], gt["center"]["y"])

        # Prefer a detection whose point falls inside the true rect;
        # otherwise fall back to the nearest detection within 120px.
        best, best_d, best_in = None, float("inf"), False
        for det in detections:
            px, py = det["point"]
            d = math.hypot(px - center[0], py - center[1])
            is_in = inside((px, py), rect)
            if is_in and not best_in:
                best, best_d, best_in = det, d, True
            elif is_in and best_in and d < best_d:
                best, best_d = det, d
            elif not best_in and d < best_d:
                best, best_d = det, d

        per_hit[gt["id"]] = bool(best is not None and best_in)

        if best is None:
            continue
        if best_in:
            matched += 1
            hits += 1
            errors.append(best_d)
        elif best_d <= 120:
            matched += 1
            errors.append(best_d)

    # Break the hit rate down by category — this is where methods diverge.
    cats = {}
    for gt in truth:
        keys = [f'kind:{gt.get("kind","?")}', f'style:{gt.get("style","?")}']
        if gt.get("tiny"):
            keys.append("size:tiny")
        for k in keys:
            c = cats.setdefault(k, {"hit": 0, "n": 0})
            c["n"] += 1
            c["hit"] += int(per_hit.get(gt["id"], False))
    by_cat = {k: round(v["hit"] / v["n"], 2) for k, v in sorted(cats.items())}

    total = len(truth)
    return {
        "method": method,
        "latency_s": round(latency, 3),
        "n_elements": len(detections),
        "found": matched,
        "total": total,
        "recall": round(matched / total, 3) if total else 0.0,
        "hit_rate": round(hits / total, 3) if total else 0.0,
        "mean_err_px": round(sum(errors) / len(errors), 1) if errors else None,
        "by_category": by_cat,
    }


# ----------------------------------------------------------------------
# Method A — current vision pipeline
# ----------------------------------------------------------------------

def method_vision(img):
    import ui_parser
    els = ui_parser.parse_ui(img, confidence_threshold=0.5)
    return [
        {"point": (e["center"]["x"], e["center"]["y"]),
         "label": e.get("label") or e.get("content") or ""}
        for e in els["elements"]
    ]


# ----------------------------------------------------------------------
# Method B — UI Automation
# ----------------------------------------------------------------------

def method_uia(_img):
    import uiautomation as auto
    out = []

    def walk(ctrl, depth=0):
        if depth > 12:
            return
        for ch in ctrl.GetChildren():
            try:
                r = ch.BoundingRectangle
                if r and r.width() > 0 and r.height() > 0:
                    out.append({
                        "point": (r.xcenter(), r.ycenter()),
                        "label": ch.Name or ch.ControlTypeName,
                    })
            except Exception:
                pass
            walk(ch, depth + 1)

    hwnd = _find_target_hwnd()
    root = auto.ControlFromHandle(hwnd) if hwnd else auto.GetRootControl()
    walk(root)
    return out


def _find_target_hwnd():
    """Locate the BENCH TARGET window by title."""
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _lparam):
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if "BENCH TARGET" in buf.value:
                found.append(hwnd)
        return True

    user32.EnumWindows(cb, None)
    return found[0] if found else None


# ----------------------------------------------------------------------
# Method C — Win32 child enumeration
# ----------------------------------------------------------------------

def method_win32(_img):
    user32 = ctypes.windll.user32
    out = []
    parent = _find_target_hwnd()
    if not parent:
        return out

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _lparam):
        rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        out.append({
            "point": ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2),
            "label": buf.value,
        })
        return True

    user32.EnumChildWindows(parent, cb, None)
    return out


# ----------------------------------------------------------------------
# Method D — OCR only
# ----------------------------------------------------------------------

def method_ocr(img):
    import numpy as np
    import ui_parser
    reader = ui_parser._models["easyocr"]
    out = []
    for bbox, text, conf in reader.readtext(np.array(img)):
        xs = [int(p[0]) for p in bbox]
        ys = [int(p[1]) for p in bbox]
        out.append({
            "point": ((min(xs) + max(xs)) // 2, (min(ys) + max(ys)) // 2),
            "label": text,
        })
    return out


# ----------------------------------------------------------------------
# Runner
# ----------------------------------------------------------------------

METHODS = {
    "A_vision": method_vision,
    "B_uia": method_uia,
    "C_win32": method_win32,
    "D_ocr": method_ocr,
}


def _wake_display():
    """Send a real relative mouse move via SendInput.

    SetCursorPos alone does NOT count as user activity for display power state —
    only genuine injected input wakes a slept monitor.
    """
    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                    ("mouseData", ctypes.c_uint32), ("dwFlags", ctypes.c_uint32),
                    ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_uint32), ("u", _U)]

    MOUSEEVENTF_MOVE = 0x0001
    for dx in (1, -1):
        inp = INPUT(0, _U(MOUSEINPUT(dx, 0, 0, MOUSEEVENTF_MOVE, 0, None)))
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(0.05)


def _capture_awake(controller, attempts: int = 6):
    """Capture, retrying through a slept display.

    Blankness is measured on the fraction of non-black pixels, NOT std — the
    cursor that _draw_cursor() composites in is itself enough content to push
    std over a naive threshold on an otherwise black frame.
    """
    import numpy as np
    for i in range(attempts):
        img, _ = controller._capture_screen()
        arr = np.asarray(img)
        lit = float((arr.mean(axis=2) > 10).mean())
        if lit > 0.005:                       # >0.5% of pixels showing anything
            if i:
                print(f"  display awake after {i} nudge(s)\n", flush=True)
            return img
        print(f"  blank frame ({lit*100:.3f}% lit) — display asleep, "
              f"waking {i+1}/{attempts}", flush=True)
        _wake_display()
        time.sleep(2.5)

    print("\nERROR: display will not wake — capture is black.\n"
          "Vision methods cannot be scored against a black frame.\n"
          "Wake the screen (move the mouse / press a key) and rerun.")
    sys.exit(2)


def main():
    import ctypes.wintypes  # noqa: F401  (RECT used in method_win32)

    if not GROUND_TRUTH.exists():
        print(f"ERROR: {GROUND_TRUTH} not found — run bench/target_app.py first.")
        sys.exit(1)

    gt = json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))
    truth = gt["buttons"]
    print(f"ground truth: {len(truth)} buttons\n")

    import controller
    import ui_parser
    print("loading models...", flush=True)
    t0 = time.time()
    ui_parser.load_models()
    print(f"models loaded in {time.time()-t0:.1f}s\n")

    # Keep the display powered for the whole run. A slept monitor captures as
    # black and silently scores every vision method at 0%.
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    ES_DISPLAY_REQUIRED = 0x00000002
    ctypes.windll.kernel32.SetThreadExecutionState(
        ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)

    img = _capture_awake(controller)

    shot = BENCH_DIR / "bench_screen.png"
    img.save(shot)
    print(f"screenshot for method E -> {shot}\n")

    results = []
    for name, fn in METHODS.items():
        print(f"running {name} ...", flush=True)
        try:
            t = time.time()
            dets = fn(img)
            lat = time.time() - t
            r = score(name, dets, truth, lat)
        except Exception as e:
            r = {"method": name, "error": f"{type(e).__name__}: {e}"}
        results.append(r)
        print(f"  {r}\n", flush=True)

    RESULTS.write_text(json.dumps(results, indent=2), encoding="utf-8")

    print("\n" + "=" * 78)
    print(f"{'method':<12} {'latency':>9} {'elems':>7} {'recall':>8} {'hit':>7} {'err_px':>8}")
    print("-" * 78)
    for r in results:
        if "error" in r:
            print(f"{r['method']:<12} {'ERROR':>9}  {r['error'][:44]}")
        else:
            err = "-" if r["mean_err_px"] is None else f"{r['mean_err_px']:.1f}"
            print(f"{r['method']:<12} {r['latency_s']:>8.2f}s {r['n_elements']:>7} "
                  f"{r['recall']:>8.0%} {r['hit_rate']:>7.0%} {err:>8}")
    print("=" * 78)

    # Per-category hit rate — the interesting part
    cat_keys = sorted({k for r in results if "by_category" in r for k in r["by_category"]})
    if cat_keys:
        print(f"\nHIT RATE BY CATEGORY\n{'category':<16}" +
              "".join(f"{r['method']:>12}" for r in results if "by_category" in r))
        print("-" * (16 + 12 * sum(1 for r in results if "by_category" in r)))
        for k in cat_keys:
            row = f"{k:<16}"
            for r in results:
                if "by_category" in r:
                    v = r["by_category"].get(k)
                    row += f"{('-' if v is None else f'{v:.0%}'):>12}"
            print(row)

    print(f"\nresults -> {RESULTS}")


if __name__ == "__main__":
    main()
