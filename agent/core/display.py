"""Display state — keep the screen awake, and never trust a blank capture.

This exists because the same failure cost three benchmark runs and then
reappeared in the labelling pass: a slept monitor captures as pure black, and
every pixel-based method reports "found nothing" with no error. That is
indistinguishable from an empty screen, so a caller acts on it.

Two rules enforced here:
  * anything that reads pixels asserts the frame is not blank
  * anything that runs for a while holds the display awake first
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import time

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.wintypes.DWORD),
                ("dwFlags", ctypes.wintypes.DWORD),
                ("time", ctypes.wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _U(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.wintypes.DWORD), ("u", _U)]


class DisplayAsleep(Exception):
    """The screen is not rendering; pixel methods cannot be trusted."""


class WorkstationLocked(Exception):
    """The session is locked. Pixels show the lock screen, not the desktop."""


def is_locked() -> bool:
    """True when the secure desktop is in front.

    Distinct from asleep: the display is lit and OCR happily returns text —
    the clock and date off the lock screen — which is worse than a black
    frame because it looks like a successful read of the wrong screen.
    """
    DESKTOP_SWITCHDESKTOP = 0x0100
    h = user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
    if not h:
        return True                     # cannot open input desktop => secure desktop
    try:
        name = ctypes.create_unicode_buffer(256)
        need = ctypes.c_ulong()
        user32.GetUserObjectInformationW(h, 2, name, 256, ctypes.byref(need))
        if name.value and name.value.lower() != "default":
            return True
    finally:
        user32.CloseDesktop(h)

    # A lit "Default" desktop can still be showing the lock UI, so check for
    # the lock app owning the foreground window.
    fg = user32.GetForegroundWindow()
    if fg:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(fg, buf, 256)
        if buf.value in ("Windows.UI.Core.CoreWindow", "LockScreenControllerWindow"):
            title = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(fg, title, 256)
            if "lock" in (title.value or "").lower():
                return True
    return False


def keep_awake(system: bool = True) -> None:
    """Hold the display on. Call before any long pixel-based operation."""
    flags = ES_CONTINUOUS | ES_DISPLAY_REQUIRED
    if system:
        flags |= ES_SYSTEM_REQUIRED
    kernel32.SetThreadExecutionState(flags)


def nudge_awake() -> None:
    """Reset the idle timers ONCE, without holding them.

    Deliberately not ES_CONTINUOUS. `keep_awake()` sets a *persistent* state,
    and that state belongs to the thread that set it. `assert_awake()` runs on
    the long-lived tools worker, so calling keep_awake() there — on every
    screenshot and every OCR pass — pinned the display and the system awake
    for as long as the app was open, and no release from any other thread
    could undo it. The user's screen simply stopped sleeping.

    "I am about to read pixels" only needs the timers pushed back, which is
    exactly what a one-shot does.
    """
    kernel32.SetThreadExecutionState(ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED)


def release_awake() -> None:
    kernel32.SetThreadExecutionState(ES_CONTINUOUS)


def wake() -> None:
    """Nudge the display awake with real injected motion.

    SetCursorPos does NOT count as user activity for display power state —
    only genuine input does, which is why a naive cursor jiggle fails here.
    """
    for dx in (1, -1):
        inp = _INPUT(INPUT_MOUSE, _U(mi=_MOUSEINPUT(dx, 0, 0, MOUSEEVENTF_MOVE, 0, None)))
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# Keeping the session usable for the length of a run
# ---------------------------------------------------------------------------
#
# Three independent mechanisms can take the screen away, and they need
# different countermeasures:
#
#   1. display / system sleep   -> SetThreadExecutionState
#   2. screensaver + "on resume, show logon"  -> screensaver settings
#   3. inactivity lock policy   -> only an input event resets the idle timer
#
# Only (1) is handled by the execution-state call everyone reaches for first,
# which is why a run can still get locked out despite "keep awake" being on.

SPI_GETSCREENSAVEACTIVE = 0x0010
SPI_SETSCREENSAVEACTIVE = 0x0011
SPI_GETSCREENSAVETIMEOUT = 0x000E
SPI_SETSCREENSAVETIMEOUT = 0x000F
SPIF_SENDCHANGE = 0x0002

VK_F15 = 0x7E
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.wintypes.WORD), ("wScan", ctypes.wintypes.WORD),
                ("dwFlags", ctypes.wintypes.DWORD),
                ("time", ctypes.wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _UK(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUTK(ctypes.Structure):
    _fields_ = [("type", ctypes.wintypes.DWORD), ("u", _UK)]


def _screensaver_state() -> tuple[bool, int]:
    active = ctypes.c_int(0)
    timeout = ctypes.c_int(0)
    user32.SystemParametersInfoW(SPI_GETSCREENSAVEACTIVE, 0, ctypes.byref(active), 0)
    user32.SystemParametersInfoW(SPI_GETSCREENSAVETIMEOUT, 0, ctypes.byref(timeout), 0)
    return bool(active.value), int(timeout.value)


def _set_screensaver(active: bool, timeout_s: int | None = None) -> None:
    user32.SystemParametersInfoW(SPI_SETSCREENSAVEACTIVE, int(active), None,
                                 SPIF_SENDCHANGE)
    if timeout_s is not None:
        user32.SystemParametersInfoW(SPI_SETSCREENSAVETIMEOUT, int(timeout_s),
                                     None, SPIF_SENDCHANGE)


def policy_lock_timeout() -> int | None:
    """Machine inactivity-lock policy, if one is set.

    A group-policy lock cannot be overridden from user space — it re-applies.
    Detecting it lets us say so instead of silently failing to prevent a lock.
    """
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System")
        val, _ = winreg.QueryValueEx(key, "InactivityTimeoutSecs")
        winreg.CloseKey(key)
        return int(val) or None
    except Exception:
        return None


def nudge_idle_timer() -> None:
    """Reset the inactivity timer with a keystroke nothing reacts to.

    F15 does not exist on modern keyboards and no application binds it, so it
    counts as user activity without side effects. A mouse move would fight the
    user's cursor; a modifier key can alter a focused app's state.
    """
    up = _INPUTK(INPUT_KEYBOARD,
                 _UK(ki=_KEYBDINPUT(VK_F15, 0, KEYEVENTF_KEYUP, 0, None)))
    user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(_INPUTK))


class KeepSessionOpen:
    """Hold the session usable for the length of a run, then restore exactly.

        with KeepSessionOpen() as k:
            ...                      # display stays on, session stays unlocked
        # every setting restored, even on exception

    Nothing is changed permanently: the screensaver's prior state and timeout
    are captured on entry and written back on exit, including on crash.
    """

    def __init__(self, keepalive_s: float = 50.0, manage_screensaver: bool = True):
        self.keepalive_s = keepalive_s
        self.manage_screensaver = manage_screensaver
        self._prev_saver: tuple[bool, int] | None = None
        self._stop = None
        self._thread = None
        self.policy_timeout = policy_lock_timeout()

    def __enter__(self) -> "KeepSessionOpen":
        import threading
        keep_awake()

        if self.manage_screensaver:
            self._prev_saver = _screensaver_state()
            if self._prev_saver[0]:
                _set_screensaver(False)

        self._stop = threading.Event()

        def loop():
            while not self._stop.wait(self.keepalive_s):
                try:
                    nudge_idle_timer()
                    keep_awake()        # re-assert; some drivers clear it
                except Exception:
                    pass

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> bool:
        if self._stop is not None:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._prev_saver is not None:
            active, timeout = self._prev_saver
            # Restore faithfully, but never re-arm a screensaver that was
            # enabled with no timeout and no saver selected. That combination
            # is inert as a screensaver yet still trips the lock path, so
            # writing it back would silently undo a user's decision to
            # disable it — the restore would look correct and behave wrong.
            if active and timeout > 0:
                _set_screensaver(True, timeout)
            else:
                _set_screensaver(False)
        release_awake()
        return False

    def status(self) -> dict:
        return {
            "display_hold": True,
            "screensaver_managed": self.manage_screensaver,
            "screensaver_was": self._prev_saver,
            "keepalive_s": self.keepalive_s,
            "policy_lock_timeout_s": self.policy_timeout,
            "policy_override_possible": self.policy_timeout is None,
        }


def lit_fraction(img) -> float:
    """Fraction of pixels showing anything.

    Measured on lit pixels rather than std: the capture path composites the
    mouse cursor into its own screenshot, and those ~90 pixels are enough to
    push std over a naive threshold on an otherwise black frame.
    """
    import numpy as np
    a = np.asarray(img)
    if a.ndim == 3:
        a = a.mean(axis=2)
    return float((a > 10).mean())


def assert_usable(attempts: int = 5, settle_s: float = 2.0) -> None:
    """Assert the screen is both awake AND showing the user's desktop."""
    assert_awake(attempts, settle_s)
    if is_locked():
        raise WorkstationLocked(
            "the workstation is locked — captured pixels are the lock screen, "
            "not your desktop. OCR and vision will read the clock and nothing "
            "else. Unlock the session to use pixel-based methods; UIA and "
            "Win32 are unaffected.")


#: When a probe grab last came back lit. Screenshots arrive one per model
#: turn, seconds apart, and each was paying for a SECOND full-screen capture
#: just to learn the display was still on. A screen lit this recently has not
#: had time to sleep since; the lock check below still runs every time.
_lit_at = 0.0
LIT_MEMO_S = 3.0


def assert_awake(attempts: int = 5, settle_s: float = 2.0) -> None:
    """Raise if the screen will not render. Wakes it first if it can."""
    global _lit_at
    nudge_awake()
    if time.time() - _lit_at < LIT_MEMO_S:
        return
    import mss
    from PIL import Image

    for i in range(attempts):
        with mss.mss() as sct:
            mon = sct.monitors[1]
            shot = sct.grab(mon)
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        lit = lit_fraction(img)
        if lit > 0.005:
            _lit_at = time.time()
            return
        if i < attempts - 1:
            wake()
            time.sleep(settle_s)
    raise DisplayAsleep(
        "screen capture is blank after %d wake attempts — the display is off. "
        "Pixel-based methods (OCR, vision) cannot run; UIA and Win32 still can."
        % attempts)
