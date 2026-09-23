"""Is the screen still doing something? Pixels, not the accessibility tree.

A step that waits for something to appear has two very different ways of
not seeing it yet:

    loading     the page is still arriving - a spinner turns, a progress
                bar moves, the cursor is the hourglass. Waiting longer is
                right, even past the step's own timeout.
    dead        nothing has moved for ten seconds. Whatever this step is
                waiting for is not coming, and sitting out the rest of a
                45-second timeout only delays the rescue.

`ScreenPulse` samples the window being driven (the foreground window) once
a second at low resolution and keeps two clocks: when it last CHANGED (a
real repaint - more than a sliver of pixels) and when it last MOVED at all
(any pixels - a spinner, a blinking caret). Plus the Windows cursor, which
apps set to busy while they work.

It runs on its own thread with its own mss handle (mss handles are thread-
local) and never touches UI Automation, so it cannot interfere with the
tool thread the script runs on.
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes

#: A pixel counts as different past this much (0-255, green channel).
PIXEL_DELTA = 18
#: A CHANGE is more than this fraction of the sampled pixels differing.
CHANGE_FRACTION = 0.004
#: Width the window is sampled down to. Enough for a spinner, cheap to diff.
SAMPLE_W = 240

_user32 = ctypes.WinDLL("user32", use_last_error=True)


class _CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hCursor", wintypes.HANDLE), ("ptScreenPos", wintypes.POINT)]


_user32.GetCursorInfo.argtypes = [ctypes.POINTER(_CURSORINFO)]
_user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPVOID]
_user32.LoadCursorW.restype = wintypes.HANDLE
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]

_BUSY = set()
for _idc in (32514, 32650):                  # IDC_WAIT, IDC_APPSTARTING
    try:
        _h = _user32.LoadCursorW(None, ctypes.c_void_p(_idc))
        if _h:
            _BUSY.add(int(_h))
    except Exception:
        pass


def cursor_busy() -> bool:
    ci = _CURSORINFO()
    ci.cbSize = ctypes.sizeof(ci)
    try:
        if not _user32.GetCursorInfo(ctypes.byref(ci)):
            return False
        return int(ci.hCursor or 0) in _BUSY
    except Exception:
        return False


def diff_frames(a, b) -> tuple[bool, bool]:
    """(changed, moved) between two same-shape uint8 arrays."""
    import numpy as np
    if a is None or b is None or a.shape != b.shape:
        return True, True
    d = np.abs(a.astype(np.int16) - b.astype(np.int16)) > PIXEL_DELTA
    n = int(d.sum())
    return n > CHANGE_FRACTION * d.size, n >= 2


class ScreenPulse:
    def __init__(self, interval: float = 1.0) -> None:
        self.interval = interval
        now = time.time()
        self.last_change = now
        self.last_motion = now
        self.samples = 0
        self._prev = None
        self._prev_hwnd = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> "ScreenPulse":
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="screen-pulse")
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread = None

    def touch(self) -> None:
        """An action just happened: count it as a change, so 'still for N
        seconds' is measured from the action rather than from before it."""
        self.last_change = self.last_motion = time.time()

    # -- what callers ask ---------------------------------------------------
    def still_for(self) -> float:
        """Seconds since anything at all moved in the driven window."""
        return time.time() - max(self.last_change, self.last_motion)

    def unchanged_for(self) -> float:
        return time.time() - self.last_change

    def busy(self, recent_s: float = 2.5) -> bool:
        return (time.time() - self.last_motion) < recent_s or cursor_busy()

    # -- the sampling thread -------------------------------------------------
    def _grab(self, sct, hwnd):
        import numpy as np
        r = wintypes.RECT()
        if not hwnd or not _user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return None
        w, h = r.right - r.left, r.bottom - r.top
        if w < 50 or h < 50 or r.left < -10000:
            return None                      # minimised
        shot = sct.grab({"left": r.left, "top": r.top, "width": w,
                         "height": h})
        arr = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(
            shot.height, shot.width, 4)[:, :, 1]
        step = max(1, w // SAMPLE_W)
        return np.ascontiguousarray(arr[::step, ::step])

    def _loop(self) -> None:
        try:
            import mss
        except ImportError:
            return
        with mss.mss() as sct:
            while not self._stop.is_set():
                try:
                    hwnd = _user32.GetForegroundWindow()
                    frame = self._grab(sct, hwnd)
                    now = time.time()
                    if hwnd != self._prev_hwnd:
                        # A different window in front IS a change.
                        self.last_change = self.last_motion = now
                    elif frame is not None:
                        changed, moved = diff_frames(self._prev, frame)
                        if changed:
                            self.last_change = now
                        if moved:
                            self.last_motion = now
                    if cursor_busy():
                        self.last_motion = now
                    self._prev, self._prev_hwnd = frame, hwnd
                    self.samples += 1
                except Exception:
                    pass
                self._stop.wait(self.interval)
