"""Input actuation — invoke first, inject only when necessary.

Measured on Windows Calculator (native WinUI), Calculator occluded behind
another window but NOT minimized:

    method        foreground   background   cursor   latency
    UIA Invoke       yes          YES        still     530ms
    SendInput        yes          no         moved       6ms
    PostMessage      no           no         still        0ms
    mouse_event      yes          no         moved     251ms

The background row is the important one. SendInput on an occluded window does
not fail safely — it clicks whatever is on top instead, i.e. the wrong
application. UIA Invoke targets the element object, so it cannot miss and it
never steals the physical cursor.

Hence: Invoke is the default, SendInput is the fallback for elements that
expose no invocable pattern (canvas, games, custom-drawn surfaces).
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import time
from dataclasses import dataclass

from .locator import Element, window_rect

user32 = ctypes.windll.user32

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except (AttributeError, OSError):
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass

#: DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
_DPI_PM_V2 = ctypes.c_void_p(-4)


def make_thread_dpi_aware() -> bool:
    """Pin THIS thread to per-monitor-v2, whatever the process ended up as.

    Process DPI awareness is first-come-first-served: this module claims
    per-monitor at import, but in the app pywebview's GUI stack makes its own
    claim around window start, and whoever loses fails silently. The result
    was two coordinate spaces in one process — UIA rects are always physical
    pixels, while _norm()'s GetSystemMetrics answered in the winning mode's
    space — and on the 2880x1800 @2x display every SendInput click reported
    success and landed somewhere else: chat rows, the search box and typed
    text all went nowhere (the run of 2026-09-08, saved as a lesson). The
    test suite never showed it because tests import no GUI, so their process
    kept this module's claim. A thread context overrides the process default
    for every DPI-sensitive call made on that thread.
    """
    try:
        return bool(ctypes.windll.user32.SetThreadDpiAwarenessContext(_DPI_PM_V2))
    except (AttributeError, OSError):
        return False

# ---------------------------------------------------------------- SendInput
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_VIRTUALDESK = 0x8000, 0x4000
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0002, 0x0004


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.wintypes.DWORD),
                ("dwFlags", ctypes.wintypes.DWORD),
                ("time", ctypes.wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.wintypes.WORD), ("wScan", ctypes.wintypes.WORD),
                ("dwFlags", ctypes.wintypes.DWORD),
                ("time", ctypes.wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.wintypes.DWORD), ("u", _U)]


def _norm(x: int, y: int) -> tuple[int, int]:
    vx = user32.GetSystemMetrics(76)
    vy = user32.GetSystemMetrics(77)
    vw = user32.GetSystemMetrics(78)
    vh = user32.GetSystemMetrics(79)
    return (int((x - vx) * 65535 / max(vw - 1, 1)),
            int((y - vy) * 65535 / max(vh - 1, 1)))


def _send(inputs: list[INPUT]) -> bool:
    # An empty batch would make SendInput return 0 == len([]) and report
    # success for having done nothing — which is how a malformed combo used
    # to look like it worked.
    if not inputs:
        return False
    arr = (INPUT * len(inputs))(*inputs)
    return user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT)) == len(inputs)


def _mouse(dx: int, dy: int, flags: int, data: int = 0) -> INPUT:
    return INPUT(INPUT_MOUSE, _U(mi=MOUSEINPUT(dx, dy, data, flags, 0, None)))


# ---------------------------------------------------------------- result

@dataclass
class ActResult:
    ok: bool
    method: str
    latency_ms: float
    cursor_moved: bool = False
    detail: str = ""


def _cursor() -> tuple[int, int]:
    p = ctypes.wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return (p.x, p.y)


# ---------------------------------------------------------------- clicking

def _try_invoke(el: Element) -> tuple[bool, str]:
    """Pattern-based activation. No coordinates, no cursor, works occluded.

    A pattern call that does not raise is NOT proof it did anything. Tk
    publishes LegacyIAccessible on every button with an empty DefaultAction —
    DoDefaultAction() then returns cleanly having done nothing, and the caller
    believes it clicked. So the legacy path is gated on the control actually
    declaring a default action.
    """
    ctrl = el.control
    if ctrl is None:
        return False, "no uia control"

    for pattern, label in (("GetInvokePattern", "invoke"),
                           ("GetTogglePattern", "toggle"),
                           ("GetSelectionItemPattern", "select"),
                           ("GetExpandCollapsePattern", "expand")):
        try:
            p = getattr(ctrl, pattern)()
            if p is None:
                continue
            if label == "invoke":
                p.Invoke()
            elif label == "toggle":
                p.Toggle()
            elif label == "select":
                p.Select()
            else:
                p.Expand()
            return True, label
        except Exception:
            continue

    # Legacy last, and only when it declares something to do.
    try:
        p = ctrl.GetLegacyIAccessiblePattern()
        if p is not None:
            action = ""
            try:
                action = (p.DefaultAction or "").strip()
            except Exception:
                action = ""
            if action:
                p.DoDefaultAction()
                return True, "legacy"
            return False, "legacy pattern has no default action"
    except Exception:
        pass

    return False, "no invocable pattern"


def virtual_desktop() -> tuple[int, int, int, int]:
    """(left, top, width, height) of the virtual desktop — physical pixels on
    a DPI-aware thread, the space UIA rects and SendInput share."""
    return (user32.GetSystemMetrics(76), user32.GetSystemMetrics(77),
            user32.GetSystemMetrics(78), user32.GetSystemMetrics(79))


def _interpolate(stroke: list[tuple[int, int]], step_px: int) -> list[tuple[int, int]]:
    """The stroke's points plus intermediate ones at most step_px apart.

    A paint program draws only where it saw the pointer: a jump of 300 px in
    one event is a straight line at best and a missed segment at worst. So a
    drag goes out as the continuous motion a hand would make.
    """
    out: list[tuple[int, int]] = [tuple(stroke[0])]
    for (x0, y0), (x1, y1) in zip(stroke, stroke[1:]):
        dist = max(abs(x1 - x0), abs(y1 - y0))
        n = max(1, int(dist / max(1, step_px)))
        for k in range(1, n + 1):
            out.append((round(x0 + (x1 - x0) * k / n),
                        round(y0 + (y1 - y0) * k / n)))
    return out


#: One call's worth of pointer events. A 2000 px stroke at 3 px spacing is
#: about 700, so this is dozens of strokes — a drawing, not a runaway loop.
MAX_PATH_EVENTS = 40000


def pointer_path(strokes: list[list[tuple[int, int]]], action: str = "drag",
                 button: str = "left", step_px: int = 3) -> ActResult:
    """Move the pointer through strokes of physical-pixel points.

    drag  — button down at the first point of each stroke, move through the
            rest, button up at the last: one stroke per list.
    click — press and release at every point.
    move  — travel through the points with no button held.

    Events go out in batches of a couple of dozen with a few milliseconds
    between: one SendInput call cannot be interleaved by anything else, and
    the pauses let the target's message loop see motion rather than a queue
    it collapses into one jump. The hook marks all of it as injected, so the
    arbiter does not mistake a long drag for the user taking the mouse.
    """
    t0 = time.time()
    c0 = _cursor()
    down, up = {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }.get(button, (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP))
    base = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    method = f"sendinput:{action}"
    events = 0

    def fail(detail: str, release_at: tuple[int, int] | None) -> ActResult:
        # Never leave a button held: an app with the mouse stuck down is
        # worse than a stroke that stopped short.
        if release_at is not None:
            nx, ny = _norm(*release_at)
            _send([_mouse(nx, ny, base | up)])
        return ActResult(False, method, (time.time() - t0) * 1000,
                         _cursor() != c0, detail=detail)

    for stroke in strokes:
        if not stroke:
            continue
        if action == "click":
            for x, y in stroke:
                nx, ny = _norm(x, y)
                if not _send([_mouse(nx, ny, base), _mouse(nx, ny, base | down),
                              _mouse(nx, ny, base | up)]):
                    return fail("SendInput refused a click", None)
                events += 3
                time.sleep(0.02)
            continue
        pts = _interpolate([tuple(p) for p in stroke], step_px)
        if events + len(pts) > MAX_PATH_EVENTS:
            return fail(f"more than {MAX_PATH_EVENTS} pointer events in one call",
                        None)
        held = action == "drag"
        batch: list[INPUT] = []
        nx, ny = _norm(*pts[0])
        batch.append(_mouse(nx, ny, base))
        if held:
            batch.append(_mouse(nx, ny, base | down))
        last = pts[0]
        for x, y in pts[1:]:
            nx, ny = _norm(x, y)
            batch.append(_mouse(nx, ny, base))
            last = (x, y)
            if len(batch) >= 24:
                if not _send(batch):
                    return fail("SendInput refused mid-stroke", last if held else None)
                events += len(batch)
                batch = []
                time.sleep(0.004)
        if held:
            nx, ny = _norm(*pts[-1])
            batch.append(_mouse(nx, ny, base | up))
        if batch and not _send(batch):
            return fail("SendInput refused the end of a stroke", last if held else None)
        events += len(batch)
        # A mosaic is thousands of tiny strokes; 12 ms each is still a
        # comfortable gap for a paint program, and a 2000-cell picture stays
        # under a minute per colour.
        time.sleep(0.012)
    return ActResult(True, method, (time.time() - t0) * 1000,
                     _cursor() != c0, detail=f"{events} pointer events")


def click_injected(x: int, y: int, button: str = "left") -> bool:
    """Atomic move+down+up in ONE SendInput call.

    Non-atomic sequences leave a race: anything moving the cursor between the
    move and the button-down relocates the click. One call cannot interleave.
    """
    down, up = {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }[button]
    nx, ny = _norm(x, y)
    base = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    return _send([_mouse(nx, ny, base),
                  _mouse(nx, ny, base | down),
                  _mouse(nx, ny, base | up)])


def click(el: Element, button: str = "left", prefer: str = "invoke",
          autofocus: bool = True) -> ActResult:
    """Click an element. Invoke where possible, inject only when it is not.

    prefer="inject" forces the coordinate path — needed when a control exposes
    a pattern but the app only reacts to real input (some canvas hybrids).

    Injection requires the target window to be foreground. If it is not, and
    we cannot bring it forward, this FAILS rather than clicking — a coordinate
    click into a covered window lands on whatever is on top instead, which is
    silently the wrong application.
    """
    c0 = _cursor()
    t0 = time.time()

    if prefer == "invoke" and button == "left":
        ok, how = _try_invoke(el)
        if ok:
            return ActResult(True, f"uia:{how}", (time.time() - t0) * 1000,
                             _cursor() != c0)

    target = el.hwnd or _window_at(*el.click_point())
    if target and not is_foreground(target):
        if not autofocus:
            return ActResult(False, "sendinput", (time.time() - t0) * 1000,
                             detail="target not foreground; refused to inject")
        top = _root_owner(target)
        if not focus_window(top):
            return ActResult(
                False, "sendinput", (time.time() - t0) * 1000,
                detail=f"could not foreground window {top}; refused to inject "
                       "(a coordinate click would hit the covering window)")
        # A window that just came forward needs a moment before it accepts
        # input; clicking immediately is intermittently swallowed.
        time.sleep(0.25)

    x, y = el.click_point()
    ok = click_injected(x, y, button)
    return ActResult(ok, "sendinput", (time.time() - t0) * 1000,
                     _cursor() != c0, detail=f"({x},{y})")


def _window_at(x: int, y: int) -> int:
    return user32.WindowFromPoint(ctypes.wintypes.POINT(x, y)) or 0


def _root_owner(hwnd: int) -> int:
    """Walk up to the top-level window — child HWNDs cannot be foregrounded."""
    GA_ROOT = 2
    try:
        root = user32.GetAncestor(hwnd, GA_ROOT)
        return root or hwnd
    except Exception:
        return hwnd


def double_click(el: Element) -> ActResult:
    c0 = _cursor()
    t0 = time.time()
    x, y = el.click_point()
    nx, ny = _norm(x, y)
    base = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    ok = _send([_mouse(nx, ny, base),
                _mouse(nx, ny, base | MOUSEEVENTF_LEFTDOWN),
                _mouse(nx, ny, base | MOUSEEVENTF_LEFTUP),
                _mouse(nx, ny, base | MOUSEEVENTF_LEFTDOWN),
                _mouse(nx, ny, base | MOUSEEVENTF_LEFTUP)])
    return ActResult(ok, "sendinput:double", (time.time() - t0) * 1000,
                     _cursor() != c0)


def scroll(el: Element | None, direction: str = "down", amount: int = 3) -> ActResult:
    t0 = time.time()
    c0 = _cursor()
    inputs = []
    if el is not None:
        x, y = el.click_point()
        nx, ny = _norm(x, y)
        inputs.append(_mouse(nx, ny, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
                             | MOUSEEVENTF_VIRTUALDESK))
    delta = 120 * amount
    if direction == "down":
        inputs.append(_mouse(0, 0, MOUSEEVENTF_WHEEL, -delta))
    elif direction == "up":
        inputs.append(_mouse(0, 0, MOUSEEVENTF_WHEEL, delta))
    elif direction == "left":
        inputs.append(_mouse(0, 0, MOUSEEVENTF_HWHEEL, -delta))
    else:
        inputs.append(_mouse(0, 0, MOUSEEVENTF_HWHEEL, delta))
    ok = _send(inputs)
    return ActResult(ok, "sendinput:scroll", (time.time() - t0) * 1000,
                     _cursor() != c0)


def drag(a: Element, b: Element, steps: int = 24) -> ActResult:
    """Press, glide, release. Intermediate moves are required — apps ignore
    a teleporting drag because they never see the intervening motion."""
    t0 = time.time()
    c0 = _cursor()
    x1, y1 = a.click_point()
    x2, y2 = b.click_point()
    base = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK

    nx, ny = _norm(x1, y1)
    _send([_mouse(nx, ny, base), _mouse(nx, ny, base | MOUSEEVENTF_LEFTDOWN)])
    for i in range(1, steps + 1):
        ix = int(x1 + (x2 - x1) * i / steps)
        iy = int(y1 + (y2 - y1) * i / steps)
        nx, ny = _norm(ix, iy)
        _send([_mouse(nx, ny, base)])
        time.sleep(0.012)
    nx, ny = _norm(x2, y2)
    ok = _send([_mouse(nx, ny, base | MOUSEEVENTF_LEFTUP)])
    return ActResult(ok, "sendinput:drag", (time.time() - t0) * 1000,
                     _cursor() != c0)


# ---------------------------------------------------------------- keyboard

VK = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "escape": 0x1B, "esc": 0x1B,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "space": 0x20,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "insert": 0x2D, "ctrl": 0x11, "control": 0x11, "alt": 0x12,
    "shift": 0x10, "win": 0x5B,
    # F1..F24. Stopping at F12 silently broke the F15 idle keepalive: the key
    # was unmapped, press_key returned False, and nothing was ever sent.
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
}

# The spellings a caller reasonably guesses, for keys the table already has.
# A real run spent a turn on `Failed: unknown key 'page_up'` — the key was
# there as "pageup", and a name that is a dead end costs the round trip plus
# whatever gets tried instead. Alternate spellings only: nothing here adds a
# key that does not exist above.
VK.update({
    "page_up": VK["pageup"], "pgup": VK["pageup"], "pg_up": VK["pageup"],
    "page_down": VK["pagedown"], "pagedn": VK["pagedown"],
    "pgdn": VK["pagedown"], "pg_dn": VK["pagedown"],
    "ins": VK["insert"], "spacebar": VK["space"],
    "windows": VK["win"], "super": VK["win"], "meta": VK["win"],
    # Playwright and the browser spell the arrows ArrowUp; callers lowercase
    # before looking up, so "arrowup" is what arrives here.
    **{f"{p}{d}": VK[d] for d in ("up", "down", "left", "right")
       for p in ("arrow", "arrow_")},
    **{f"{d}{s}": VK[d] for d in ("up", "down", "left", "right")
       for s in ("arrow", "_arrow")},
})


def _key(vk: int, up: bool = False) -> INPUT:
    return INPUT(INPUT_KEYBOARD,
                 _U(ki=KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, None)))


def _unicode_char(ch: str) -> list[INPUT]:
    """One character as UTF-16 code units.

    KEYBDINPUT.wScan is a 16-bit field, so `ord(ch)` truncated anything above
    U+FFFF: an emoji (U+1F60A) went out as U+F60A and pasted as a random
    glyph. Encoding to UTF-16-LE emits the surrogate PAIR, which is what
    Windows expects for astral characters.
    """
    raw = ch.encode("utf-16-le")
    out: list[INPUT] = []
    for i in range(0, len(raw), 2):
        code = raw[i] | (raw[i + 1] << 8)
        out.append(INPUT(INPUT_KEYBOARD,
                         _U(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, None))))
        out.append(INPUT(INPUT_KEYBOARD,
                         _U(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
                                          0, None))))
    return out


def _segment(text: str) -> list[str]:
    """Split into runs of ordinary characters and the control keys \\n and \\t.

    Kept as a plain scan rather than a regex so the two control characters are
    the only thing that is special, and everything else is passed through
    untouched — including the Turkish letters this app types constantly.
    """
    out: list[str] = []
    buf: list[str] = []
    for ch in text.replace("\r\n", "\n").replace("\r", "\n"):
        if ch in ("\n", "\t"):
            if buf:
                out.append("".join(buf))
                buf = []
            out.append(ch)
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def type_text(text: str, el: Element | None = None) -> ActResult:
    """Type literally, with \\n as a real Enter and \\t as a real Tab.

    Uses KEYEVENTF_UNICODE so the text is independent of keyboard layout —
    a Turkish layout would otherwise mangle characters typed by virtual-key.

    Newlines and tabs are sent as VIRTUAL KEYS, not as characters. A U+000A
    delivered through KEYEVENTF_UNICODE is silently dropped by most
    applications: typing "https://example.com\\n" into the address bar filled
    the bar and then just sat there, so every caller had to follow it with a
    separate Enter — an extra model round trip on the single most common
    action there is. A newline now submits.
    """
    t0 = time.time()
    pieces = _segment(text)
    has_keys = any(p in ("\n", "\t") for p in pieces)

    # SetValue stores the string verbatim, so a trailing newline would be
    # written into the field instead of pressed. When the text carries control
    # keys, type it instead, so "\\n means Enter" holds whichever path is used.
    if el is not None and el.control is not None:
        if not has_keys:
            try:
                vp = el.control.GetValuePattern()
                if vp is not None:
                    vp.SetValue(text)
                    return ActResult(True, "uia:setvalue", (time.time() - t0) * 1000)
            except Exception:
                pass
        try:
            el.control.SetFocus()
        except Exception:
            pass

    ok = True
    for piece in pieces:
        if piece in ("\n", "\t"):
            vk = VK["enter"] if piece == "\n" else VK["tab"]
            ok &= _send([_key(vk), _key(vk, up=True)])
            time.sleep(0.012)
            continue
        for i in range(0, len(piece), 24):
            ok &= _send(sum((_unicode_char(c) for c in piece[i:i + 24]), []))
            time.sleep(0.004)
    return ActResult(ok, "sendinput:unicode", (time.time() - t0) * 1000)


def char_key(ch: str) -> tuple[int, list[int]] | None:
    """The virtual key and the modifiers that produce `ch` on THIS layout.

    VkKeyScanW packs the key in the low byte and the shift state in the high
    byte. Masking the high byte off — which this used to do — silently typed
    the wrong character on any non-US layout. Measured on the Turkish Q
    layout of this machine:

        '/'  ->  shift + 7      typed as bare 7
        '?'  ->  shift + OEM_8  typed as the unshifted key
        '@'  ->  ctrl+alt + Q   typed as q
        '+'  ->  shift + 4      typed as 4

    So press_key('/') typed a 7, and WhatsApp Web's ctrl+alt+/ search
    shortcut went out as ctrl+alt+7 and did nothing at all — which is what
    sent a real task off hunting for another way in.

    Returns None for a character the layout cannot produce, rather than
    sending virtual key 0xFF.
    """
    r = user32.VkKeyScanW(ctypes.c_wchar(ch))
    if r == -1:
        return None
    state = (r >> 8) & 0xFF
    mods = []
    if state & 1:
        mods.append(VK["shift"])
    if state & 2:
        mods.append(VK["ctrl"])
    if state & 4:
        mods.append(VK["alt"])
    return r & 0xFF, mods


def _press_seq(vk: int, mods: list[int]) -> list[INPUT]:
    """Hold the modifiers, tap the key, release in reverse."""
    return ([_key(m) for m in mods] + [_key(vk), _key(vk, up=True)]
            + [_key(m, up=True) for m in reversed(mods)])


def press_key(key: str, presses: int = 1) -> ActResult:
    t0 = time.time()
    k = key.strip()
    vk, mods = VK.get(k.lower()), []
    if vk is None:
        if len(k) != 1:
            return ActResult(False, "press_key", 0, detail=f"unknown key {key!r}")
        found = char_key(k)
        if found is None:
            return ActResult(False, "press_key", 0,
                             detail=f"{key!r} is not on the current keyboard layout")
        vk, mods = found
    ok = True
    for _ in range(max(1, presses)):
        ok &= _send(_press_seq(vk, mods))
        time.sleep(0.012)
    return ActResult(ok, "sendinput:key", (time.time() - t0) * 1000)


def _split_combo(combo: str) -> list[str]:
    """Split on '+', treating a trailing '+' as the plus KEY, not a separator.

    'ctrl++' used to filter down to ['ctrl'] and quietly press only Control.
    """
    s = combo.strip()
    if s.endswith("+") and len(s) > 1:
        return [p for p in s[:-1].split("+") if p.strip()] + ["+"]
    return [p for p in s.split("+") if p.strip()]


def send_keys(combo: str, times: int = 1) -> ActResult:
    """Modifier combo, e.g. ctrl+shift+s — pressed and released as one batch."""
    t0 = time.time()
    parts = [p.strip().lower() for p in _split_combo(combo)]
    if not parts:
        return ActResult(False, "send_keys", 0, detail=f"empty combo {combo!r}")
    vks: list[int] = []
    extra: list[int] = []
    for p in parts:
        vk = VK.get(p)
        if vk is None:
            if len(p) != 1:
                return ActResult(False, "send_keys", 0, detail=f"unknown key {p!r}")
            found = char_key(p)
            if found is None:
                return ActResult(False, "send_keys", 0,
                                 detail=f"{p!r} is not on the current keyboard layout")
            vk, need = found
            # The character itself needs a modifier on this layout (Turkish
            # '/' is shift+7), so ctrl+/ must go out as ctrl+shift+7.
            extra += [m for m in need if m not in vks and m not in extra]
        vks.append(vk)
    order = extra + vks
    seq = [_key(v) for v in order] + [_key(v, up=True) for v in reversed(order)]
    ok = True
    for _ in range(max(1, times)):
        ok &= _send(seq)
        time.sleep(0.012)
    return ActResult(ok, "sendinput:combo", (time.time() - t0) * 1000)


SPI_GETFOREGROUNDLOCKTIMEOUT = 0x2000
SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001
SPIF_SENDCHANGE = 0x02
KEYEVENTF_KEYUP = 0x0002
VK_MENU = 0x12


def _raise_once(hwnd: int) -> None:
    """One attempt at the standard attach-and-raise."""
    fg = user32.GetForegroundWindow()
    cur_tid = ctypes.windll.kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = False
    if fg_tid and fg_tid != cur_tid:
        attached = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetActiveWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(cur_tid, fg_tid, False)


def _settled(hwnd: int, timeout_s: float) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if user32.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.03)
    return False


def focus_window(hwnd: int, timeout_s: float = 1.5) -> bool:
    """Bring a window to the foreground, and confirm it actually got there.

    Windows refuses SetForegroundWindow from a process that does not already
    own the foreground — it returns 0 and does nothing. Injection into a window
    that never came forward does not fail safely: SendInput clicks whatever IS
    on top, i.e. the wrong application.

    Attaching to the foreground thread's input queue is the usual answer and it
    is not sufficient on its own. There is a second gate, the foreground LOCK
    TIMEOUT, and it is exactly the unattended case that trips it: a process
    nobody has interacted with, several levels down a spawn tree, is refused.
    Measured — the same suite passed run from a shell and failed run as a
    grandchild of the test runner, every time, on the same machine.

    So the attempts escalate, each verified before moving on:

      1. attach and raise             — enough when we already have some claim
      2. synthetic ALT, then retry    — Windows treats a key event as evidence
                                        this process is being interacted with
      3. drop the lock timeout        — restored immediately afterwards
      4. minimise and restore         — a restoring window is granted focus

    Returns False if none of them worked, and the caller must NOT then inject.
    """
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)          # SW_RESTORE
    if user32.GetForegroundWindow() == hwnd:
        return True

    # 1 — the ordinary path
    _raise_once(hwnd)
    if _settled(hwnd, timeout_s * 0.4):
        return True

    # 2 — a synthetic ALT press. Injected, so the arbiter does not read it as
    #     the user taking over, but Windows counts it as input to this process.
    user32.keybd_event(VK_MENU, 0, 0, 0)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    _raise_once(hwnd)
    if _settled(hwnd, timeout_s * 0.3):
        return True

    # 3 — clear the foreground lock timeout, then put it back. Leaving it at
    #     zero would let any process steal focus from the user afterwards.
    prev = ctypes.c_uint(0)
    got = user32.SystemParametersInfoW(SPI_GETFOREGROUNDLOCKTIMEOUT, 0,
                                       ctypes.byref(prev), 0)
    try:
        user32.SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0,
                                     ctypes.c_void_p(0), SPIF_SENDCHANGE)
        _raise_once(hwnd)
        if _settled(hwnd, timeout_s * 0.3):
            return True
    finally:
        if got:
            user32.SystemParametersInfoW(
                SPI_SETFOREGROUNDLOCKTIMEOUT, 0,
                ctypes.c_void_p(prev.value), SPIF_SENDCHANGE)

    # 4 — last resort. A window coming back from minimised is allowed focus.
    user32.ShowWindow(hwnd, 6)              # SW_MINIMIZE
    time.sleep(0.05)
    user32.ShowWindow(hwnd, 9)              # SW_RESTORE
    _raise_once(hwnd)
    return _settled(hwnd, timeout_s * 0.4)


def is_foreground(hwnd: int) -> bool:
    return bool(hwnd) and user32.GetForegroundWindow() == hwnd


# ---------------------------------------------------------------- window size

SW_MAXIMIZE = 3
SW_RESTORE = 9
MONITOR_DEFAULTTONEAREST = 2


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.wintypes.DWORD),
                ("rcMonitor", ctypes.wintypes.RECT),
                ("rcWork", ctypes.wintypes.RECT),
                ("dwFlags", ctypes.wintypes.DWORD)]


def is_maximized(hwnd: int) -> bool:
    """Windows' own answer, not a guess from the rectangle."""
    return bool(hwnd) and bool(user32.IsZoomed(hwnd))


def fills_screen(hwnd: int) -> bool:
    """Maximised, or borderless over the whole monitor — a browser in F11, a
    game, a video. IsZoomed says no to the second kind, and "maximising" it
    would take the browser OUT of full screen: a smaller view than it had,
    which is the opposite of what the caller wanted. So both count as done.

    A maximised window overhangs the monitor's edges by its border and stops
    short of the taskbar, so it does not cover the monitor rectangle; that is
    why the two tests are separate rather than one geometric one.
    """
    if not hwnd or not user32.IsWindow(hwnd) or user32.IsIconic(hwnd):
        return False
    if user32.IsZoomed(hwnd):
        return True
    mon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    if not mon:
        return False
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(mon, ctypes.byref(info)):
        return False
    l, t, r, b = window_rect(hwnd)
    m = info.rcMonitor
    return l <= m.left and t <= m.top and r >= m.right and b >= m.bottom


def maximize_window(hwnd: int, timeout_s: float = 1.5) -> bool:
    """Make a window fill its screen, and confirm that it did.

    A browser in a small or half-covered window shows less of the page, and
    what is off screen cannot be found or clicked; at narrow widths sites also
    fold their menus away behind icons. Maximised, the whole page is there.

    Does not touch the foreground — focus_window does that, with the
    escalation it needs. ShowWindow's return value is the window's PREVIOUS
    visibility, not whether the request was honoured, so the state is read
    back rather than trusted: a fixed-size window can refuse, and the caller
    must know. A window that already fills the screen is left alone, because
    re-sending SW_MAXIMIZE only flickers, or drops a full-screen browser back
    to a framed one.
    """
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    top = _root_owner(hwnd)
    if fills_screen(top):
        return True
    user32.ShowWindow(top, SW_MAXIMIZE)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if is_maximized(top):
            return True
        time.sleep(0.03)
    return is_maximized(top)
