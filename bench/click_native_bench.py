"""Click-method benchmark against a NATIVE Windows app.

The Tkinter benchmark was unrepresentative: Tk buttons are not native controls
and do not implement InvokePattern, so UIA Invoke and PostMessage both failed
for reasons that say nothing about real applications.

This uses Windows Calculator (WinUI, proper UIA support) and verifies each
click by reading the calculator's own display — ground truth from the app
itself rather than from the API's return value.

The question this answers is architectural: if Invoke/PostMessage work on real
apps, macros can drive BACKGROUND windows without stealing the user's cursor.
That decides whether the actuator is cursor-based or message-based.

Run:  python bench/click_native_bench.py
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import subprocess
import sys
import time
from pathlib import Path

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except (AttributeError, OSError):
    pass

import uiautomation as auto

user32 = ctypes.windll.user32
RESULTS = Path(__file__).parent / "click_native_results.json"

# ---------------------------------------------------------------- SendInput
INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
BM_CLICK = 0x00F5
MK_LBUTTON = 0x0001


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.wintypes.DWORD),
                ("dwFlags", ctypes.wintypes.DWORD),
                ("time", ctypes.wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.wintypes.DWORD), ("u", _U)]


def _norm(x, y):
    vx = user32.GetSystemMetrics(76)
    vy = user32.GetSystemMetrics(77)
    vw = user32.GetSystemMetrics(78)
    vh = user32.GetSystemMetrics(79)
    return (int((x - vx) * 65535 / max(vw - 1, 1)),
            int((y - vy) * 65535 / max(vh - 1, 1)))


def cursor_pos():
    p = ctypes.wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return (p.x, p.y)


# ---------------------------------------------------------------- methods

def click_sendinput(ctrl, x, y):
    nx, ny = _norm(x, y)
    f = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    evs = [(f,), (f | MOUSEEVENTF_LEFTDOWN,), (f | MOUSEEVENTF_LEFTUP,)]
    arr = (INPUT * 3)()
    for i, (fl,) in enumerate(evs):
        arr[i].type = INPUT_MOUSE
        arr[i].u.mi = MOUSEINPUT(nx, ny, 0, fl, 0, None)
    return user32.SendInput(3, arr, ctypes.sizeof(INPUT)) == 3


def click_mouse_event(ctrl, x, y):
    import pyautogui
    pyautogui.click(x, y)
    return True


def click_postmessage(ctrl, x, y):
    """Post to the HWND under the point, using CLIENT coordinates."""
    hwnd = user32.WindowFromPoint(ctypes.wintypes.POINT(x, y))
    if not hwnd:
        return False
    pt = ctypes.wintypes.POINT(x, y)
    user32.ScreenToClient(hwnd, ctypes.byref(pt))
    lp = (pt.y << 16) | (pt.x & 0xFFFF)
    user32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp)
    user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lp)
    return True


def click_uia_invoke(ctrl, x, y):
    """No coordinates at all — ask the control to invoke itself."""
    try:
        ctrl.GetInvokePattern().Invoke()
        return True
    except Exception:
        return False


def click_uia_legacy(ctrl, x, y):
    """LegacyIAccessible.DoDefaultAction — works where InvokePattern is absent."""
    try:
        ctrl.GetLegacyIAccessiblePattern().DoDefaultAction()
        return True
    except Exception:
        return False


METHODS = {
    "1_mouse_event": click_mouse_event,
    "2_sendinput": click_sendinput,
    "3_postmessage": click_postmessage,
    "4_uia_invoke": click_uia_invoke,
    "5_uia_legacy": click_uia_legacy,
}

# Methods that do not require the window to be in the foreground
BACKGROUND_CAPABLE = {"3_postmessage", "4_uia_invoke", "5_uia_legacy"}


# ---------------------------------------------------------------- target

def launch_calculator():
    subprocess.Popen(["calc.exe"], shell=True)
    for _ in range(40):
        time.sleep(0.5)
        win = auto.WindowControl(searchDepth=1, ClassName="ApplicationFrameWindow",
                                 SubName="Calculator")
        if win.Exists(0.4, 0.2):
            return win
        win = auto.WindowControl(searchDepth=1, SubName="Calculator")
        if win.Exists(0.4, 0.2):
            return win
    return None


def find_button(win, auto_id: str, name: str):
    b = win.ButtonControl(AutomationId=auto_id)
    if b.Exists(1.0, 0.3):
        return b
    b = win.ButtonControl(Name=name)
    if b.Exists(1.0, 0.3):
        return b
    return None


def read_display(win) -> str:
    for aid in ("CalculatorResults", "CalculatorExpression"):
        t = win.TextControl(AutomationId=aid)
        if t.Exists(0.6, 0.2):
            return (t.Name or "").strip()
    return ""


def press_clear(win):
    for aid, nm in (("clearButton", "Clear"), ("clearEntryButton", "Clear entry")):
        b = find_button(win, aid, nm)
        if b:
            try:
                b.GetInvokePattern().Invoke()
                time.sleep(0.25)
                return
            except Exception:
                pass


# ---------------------------------------------------------------- runner

def main():
    print("launching Calculator...", flush=True)
    win = launch_calculator()
    if win is None:
        print("ERROR: could not find a Calculator window")
        sys.exit(1)

    win.SetActive()
    time.sleep(1.2)
    hwnd = win.NativeWindowHandle
    print(f"target: {win.Name!r}  hwnd={hwnd}  rect={win.BoundingRectangle}\n")

    digits = [("num7Button", "Seven", "7"), ("num5Button", "Five", "5")]
    results = []

    for mname, fn in METHODS.items():
        for aid, nm, expect in digits:
            win.SetActive()
            time.sleep(0.35)
            press_clear(win)

            btn = find_button(win, aid, nm)
            if btn is None:
                results.append({"method": mname, "button": nm,
                                "error": "button not found"})
                print(f"  {mname:<15} {nm:<6} button not found")
                continue

            r = btn.BoundingRectangle
            cx, cy = r.xcenter(), r.ycenter()
            before_cur = cursor_pos()
            before_disp = read_display(win)

            t0 = time.time()
            try:
                api_ok = fn(btn, cx, cy)
                err = None
            except Exception as e:
                api_ok, err = False, f"{type(e).__name__}: {e}"
            lat = (time.time() - t0) * 1000

            time.sleep(0.55)
            after_disp = read_display(win)
            registered = expect in after_disp and after_disp != before_disp

            row = {
                "method": mname, "button": nm, "expect": expect,
                "api_ok": api_ok, "registered": bool(registered),
                "display_before": before_disp, "display_after": after_disp,
                "latency_ms": round(lat, 1),
                "cursor_moved": cursor_pos() != before_cur,
                "error": err,
            }
            results.append(row)
            print(f"  {mname:<15} {nm:<6} registered={str(registered):<5} "
                  f"{lat:>6.0f}ms  cursor={'moved' if row['cursor_moved'] else 'still':<5} "
                  f"disp={after_disp[:28]!r}", flush=True)

    # ---- background test: can it click without the window in front? -----
    print("\nbackground test — Calculator NOT foreground:", flush=True)
    bg_rows = []
    for mname in BACKGROUND_CAPABLE:
        win.SetActive()
        time.sleep(0.3)
        press_clear(win)
        btn = find_button(win, "num7Button", "Seven")
        if btn is None:
            continue
        r = btn.BoundingRectangle
        before = read_display(win)

        # push a different window to the front
        user32.ShowWindow(hwnd, 6)      # SW_MINIMIZE
        time.sleep(0.8)

        t0 = time.time()
        try:
            ok = METHODS[mname](btn, r.xcenter(), r.ycenter())
        except Exception:
            ok = False
        lat = (time.time() - t0) * 1000
        time.sleep(0.6)

        user32.ShowWindow(hwnd, 9)      # SW_RESTORE
        time.sleep(0.7)
        after = read_display(win)
        reg = "7" in after and after != before

        bg_rows.append({"method": mname, "registered_while_minimized": bool(reg),
                        "latency_ms": round(lat, 1),
                        "display_after": after})
        print(f"  {mname:<15} minimized -> registered={str(reg):<5} "
              f"{lat:>6.0f}ms  disp={after[:28]!r}", flush=True)

    payload = {"foreground": results, "background": bg_rows}
    RESULTS.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print("\n" + "=" * 86)
    print(f"{'method':<16}{'fg works':>10}{'bg works':>10}{'latency':>10}{'cursor':>9}   verdict")
    print("-" * 86)
    for mname in METHODS:
        rows = [r for r in results if r["method"] == mname and "registered" in r]
        fg = all(r["registered"] for r in rows) and rows != []
        lat = sum(r["latency_ms"] for r in rows) / len(rows) if rows else 0
        cur = any(r.get("cursor_moved") for r in rows)
        bg = next((b["registered_while_minimized"] for b in bg_rows
                   if b["method"] == mname), None)
        bg_s = "-" if bg is None else ("yes" if bg else "no")
        if fg and bg is True and not cur:
            v = "BEST — invisible, background-capable"
        elif fg and not cur:
            v = "good — no cursor theft"
        elif fg:
            v = "works, steals cursor"
        else:
            v = "unusable"
        print(f"{mname:<16}{('yes' if fg else 'no'):>10}{bg_s:>10}"
              f"{lat:>9.0f}ms{('moved' if cur else 'still'):>9}   {v}")
    print("=" * 86)
    print(f"\nresults -> {RESULTS}")


if __name__ == "__main__":
    main()
