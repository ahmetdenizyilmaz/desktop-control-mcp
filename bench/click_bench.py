"""Compare click-injection methods against the bench target.

Methods:
  1 mouse_event   SetCursorPos + mouse_event      (what pyautogui/controller.py does)
  2 sendinput     SendInput, move+down+up in ONE atomic call
  3 postmessage   WM_LBUTTONDOWN/UP posted to the child HWND (no cursor move)
  4 uia_invoke    UIA InvokePattern.Invoke()      (no coordinates at all)

Ground truth for success is the target app itself: it appends every click it
receives to clicks.log, so we verify the RIGHT button actually fired rather
than trusting the API's return value.

Also measured:
  cursor_moved   did the physical cursor end up somewhere new?
  works_bg       does it still land when the target is NOT foreground?

Run:  python bench/click_bench.py       (target_app.py must be running)
"""

import ctypes
import ctypes.wintypes
import json
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
CLICK_LOG = BENCH_DIR / "clicks.log"
RESULTS = BENCH_DIR / "click_results.json"

user32 = ctypes.windll.user32

# ---------------------------------------------------------------- SendInput
INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
MK_LBUTTON = 0x0001


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.wintypes.DWORD),
                ("dwFlags", ctypes.wintypes.DWORD),
                ("time", ctypes.wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUTunion(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.wintypes.DWORD), ("union", _INPUTunion)]


def _abs(x, y):
    """Convert pixel coords to the 0..65535 virtual-desktop space SendInput wants."""
    vx = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
    vy = user32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
    vw = user32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
    vh = user32.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN
    nx = int((x - vx) * 65535 / max(vw - 1, 1))
    ny = int((y - vy) * 65535 / max(vh - 1, 1))
    return nx, ny


def click_sendinput(x, y, hwnd=None):
    """Atomic move+down+up — one SendInput call, cannot be interleaved."""
    nx, ny = _abs(x, y)
    flags_move = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    events = [
        (nx, ny, flags_move),
        (nx, ny, flags_move | MOUSEEVENTF_LEFTDOWN),
        (nx, ny, flags_move | MOUSEEVENTF_LEFTUP),
    ]
    arr = (INPUT * len(events))()
    for i, (ex, ey, fl) in enumerate(events):
        arr[i].type = INPUT_MOUSE
        arr[i].union.mi = MOUSEINPUT(ex, ey, 0, fl, 0, None)
    sent = user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))
    return sent == len(events)


def click_mouse_event(x, y, hwnd=None):
    """What controller.py does today, via pyautogui."""
    import pyautogui
    pyautogui.click(x, y)
    return True


def click_postmessage(x, y, hwnd=None):
    """Post window messages straight to the child under (x,y). No cursor move."""
    pt = ctypes.wintypes.POINT(x, y)
    child = user32.WindowFromPoint(pt)
    if not child:
        return False
    # lParam wants CLIENT coords, not screen
    cpt = ctypes.wintypes.POINT(x, y)
    user32.ScreenToClient(child, ctypes.byref(cpt))
    lparam = (cpt.y << 16) | (cpt.x & 0xFFFF)
    user32.PostMessageW(child, WM_LBUTTONDOWN, MK_LBUTTON, lparam)
    user32.PostMessageW(child, WM_LBUTTONUP, 0, lparam)
    return True


def click_uia_invoke(x, y, hwnd=None):
    """Ask the control to invoke itself — no coordinates involved."""
    import uiautomation as auto
    ctrl = auto.ControlFromPoint(x, y)
    if ctrl is None:
        return False
    try:
        ctrl.GetInvokePattern().Invoke()
        return True
    except Exception:
        try:
            ctrl.Click(simulateMove=False)   # fallback path
            return True
        except Exception:
            return False


METHODS = {
    "1_mouse_event": click_mouse_event,
    "2_sendinput": click_sendinput,
    "3_postmessage": click_postmessage,
    "4_uia_invoke": click_uia_invoke,
}


def _cursor_pos():
    p = ctypes.wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return (p.x, p.y)


def _read_log():
    if not CLICK_LOG.exists():
        return []
    return [json.loads(l) for l in CLICK_LOG.read_text(encoding="utf-8").splitlines() if l.strip()]


def _find_target_hwnd():
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _l):
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if "BENCH TARGET" in buf.value:
                found.append(hwnd)
        return True

    user32.EnumWindows(cb, None)
    return found[0] if found else None


def main():
    if not GROUND_TRUTH.exists():
        print("ERROR: run bench/target_app.py first")
        sys.exit(1)

    gt = json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))
    buttons = {b["id"]: b for b in gt["buttons"]}
    hwnd = _find_target_hwnd()
    print(f"target hwnd: {hwnd}\n")

    # One large and one tiny button per method — small targets are where
    # coordinate-based methods are expected to struggle.
    probes = ["b01", "b17"]

    results = []
    for name, fn in METHODS.items():
        for bid in probes:
            b = buttons[bid]
            cx, cy = b["center"]["x"], b["center"]["y"]

            user32.SetForegroundWindow(hwnd)
            time.sleep(0.3)
            before = _read_log()
            start_cursor = _cursor_pos()

            t = time.time()
            try:
                ok = fn(cx, cy, hwnd)
                err = None
            except Exception as e:
                ok, err = False, f"{type(e).__name__}: {e}"
            lat = time.time() - t

            time.sleep(0.45)   # let the app process the event
            after = _read_log()
            new = after[len(before):]
            landed = new[-1]["id"] if new else None

            results.append({
                "method": name,
                "button": bid,
                "size": f'{b["size"]["w"]}x{b["size"]["h"]}',
                "api_ok": ok,
                "registered": landed == bid,
                "landed_on": landed,
                "latency_ms": round(lat * 1000, 1),
                "cursor_moved": _cursor_pos() != start_cursor,
                "error": err,
            })
            print(f"  {name:<14} {bid} ({b['size']['w']}x{b['size']['h']:<3}) "
                  f"registered={landed == bid!s:<5} landed={landed} "
                  f"{lat*1000:.0f}ms", flush=True)

    RESULTS.write_text(json.dumps(results, indent=2), encoding="utf-8")

    print("\n" + "=" * 82)
    print(f"{'method':<15} {'button':<7} {'size':<9} {'registered':>11} {'latency':>9} {'cursor':>8}")
    print("-" * 82)
    for r in results:
        print(f"{r['method']:<15} {r['button']:<7} {r['size']:<9} "
              f"{str(r['registered']):>11} {r['latency_ms']:>8.0f}ms "
              f"{'moved' if r['cursor_moved'] else 'still':>8}")
    print("=" * 82)
    print(f"\nresults -> {RESULTS}")


if __name__ == "__main__":
    main()
