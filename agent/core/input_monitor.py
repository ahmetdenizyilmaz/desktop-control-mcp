"""One low-level input hook, many subscribers.

The arbiter needs to know when the USER touches the machine (to yield control
and to catch ESC+ESC). The recorder needs the same events (to capture a
demonstration). Installing two WH_KEYBOARD_LL / WH_MOUSE_LL hooks means two
message pumps competing in one process, and duplicating the physical-vs-
injected logic that both depend on.

So: a single hook pair, a subscriber list, and one place where the
LLKHF_INJECTED / LLMHF_INJECTED distinction is made.

That distinction is load-bearing. Everything this system does arrives as
injected input; if the abort key could be synthesised, an agent could suppress
its own kill switch.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Declare the 64-bit signatures explicitly. Without argtypes/restype, ctypes
# guesses and CallNextHookEx raises "OverflowError: int too long to convert"
# on the pointer-sized lParam — the callback then dies on every event, which
# looks like a hook that silently does nothing.
LRESULT = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
HHOOK = ctypes.wintypes.HANDLE

user32.CallNextHookEx.argtypes = [HHOOK, ctypes.c_int,
                                  ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.SetWindowsHookExW.restype = HHOOK
user32.UnhookWindowsHookEx.argtypes = [HHOOK]

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14

WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
WM_MOUSEWHEEL = 0x020A

LLKHF_INJECTED = 0x00000010
LLMHF_INJECTED = 0x00000001

VK_ESCAPE = 0x1B

_MOUSE_NAMES = {
    WM_LBUTTONDOWN: "left_down", WM_LBUTTONUP: "left_up",
    WM_RBUTTONDOWN: "right_down", WM_RBUTTONUP: "right_up",
    WM_MBUTTONDOWN: "middle_down", WM_MBUTTONUP: "middle_up",
    WM_MOUSEWHEEL: "wheel", WM_MOUSEMOVE: "move",
}


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", ctypes.wintypes.DWORD),
                ("scanCode", ctypes.wintypes.DWORD),
                ("flags", ctypes.wintypes.DWORD),
                ("time", ctypes.wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", POINT), ("mouseData", ctypes.wintypes.DWORD),
                ("flags", ctypes.wintypes.DWORD),
                ("time", ctypes.wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


@dataclass
class InputEvent:
    kind: str                    # "key" | "mouse"
    action: str                  # "down"/"up" | "left_down"/"move"/...
    physical: bool               # False => injected by software (us)
    t: float = field(default_factory=time.time)
    vk: int | None = None
    pos: tuple[int, int] | None = None
    wheel: int = 0


Subscriber = Callable[[InputEvent], None]


class InputMonitor:
    """Single shared hook. Start once; subscribe many times."""

    def __init__(self) -> None:
        self._subs: list[Subscriber] = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._tid: int | None = None
        self._running = threading.Event()
        self._kb_ref = None
        self._ms_ref = None
        # Coalesce mouse-move spam: moves fire hundreds of times a second and
        # every subscriber would pay for each one.
        self.move_min_interval = 0.05
        self._last_move = 0.0

    # -- subscription ----------------------------------------------------
    def subscribe(self, fn: Subscriber) -> Subscriber:
        with self._lock:
            self._subs.append(fn)
        return fn

    def unsubscribe(self, fn: Subscriber) -> None:
        with self._lock:
            if fn in self._subs:
                self._subs.remove(fn)

    def _emit(self, ev: InputEvent) -> None:
        with self._lock:
            subs = list(self._subs)
        for fn in subs:
            try:
                fn(ev)
            except Exception:
                pass            # a bad subscriber must not kill the hook

    # -- lifecycle -------------------------------------------------------
    def start(self, timeout_s: float = 3.0) -> bool:
        if self._running.is_set():
            return True
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        return self._running.wait(timeout_s)

    def stop(self) -> None:
        if self._tid:
            user32.PostThreadMessageW(self._tid, 0x0012, 0, 0)   # WM_QUIT
        self._running.clear()

    @property
    def running(self) -> bool:
        return self._running.is_set()

    # -- hook thread -----------------------------------------------------
    def _pump(self) -> None:
        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int,
                                      ctypes.wintypes.WPARAM,
                                      ctypes.wintypes.LPARAM)

        def kb_proc(nCode, wParam, lParam):
            if nCode >= 0:
                kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if wParam in (WM_KEYDOWN, WM_SYSKEYDOWN, WM_KEYUP, WM_SYSKEYUP):
                    self._emit(InputEvent(
                        kind="key",
                        action="down" if wParam in (WM_KEYDOWN, WM_SYSKEYDOWN) else "up",
                        physical=not (kb.flags & LLKHF_INJECTED),
                        vk=kb.vkCode))
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        def ms_proc(nCode, wParam, lParam):
            if nCode >= 0:
                ms = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                action = _MOUSE_NAMES.get(wParam)
                if action is not None:
                    if action == "move":
                        now = time.time()
                        if now - self._last_move < self.move_min_interval:
                            return user32.CallNextHookEx(None, nCode, wParam, lParam)
                        self._last_move = now
                    wheel = 0
                    if action == "wheel":
                        wheel = ctypes.c_short((ms.mouseData >> 16) & 0xFFFF).value
                    self._emit(InputEvent(
                        kind="mouse", action=action,
                        physical=not (ms.flags & LLMHF_INJECTED),
                        pos=(ms.pt.x, ms.pt.y), wheel=wheel))
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        # Keep strong refs — a garbage-collected callback crashes the process.
        self._kb_ref = HOOKPROC(kb_proc)
        self._ms_ref = HOOKPROC(ms_proc)

        kb_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_ref, None, 0)
        ms_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._ms_ref, None, 0)
        if not kb_hook and not ms_hook:
            return

        self._tid = kernel32.GetCurrentThreadId()
        self._running.set()

        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        if kb_hook:
            user32.UnhookWindowsHookEx(kb_hook)
        if ms_hook:
            user32.UnhookWindowsHookEx(ms_hook)
        self._running.clear()


#: process-wide singleton — there must only ever be one hook pair
monitor = InputMonitor()


# ---------------------------------------------------------------------------
# Helpers built on the shared stream
# ---------------------------------------------------------------------------

class DoubleTapWatcher:
    """Fire when a key is pressed twice, physically, inside a window.

    Injected presses are ignored by construction, so an agent cannot press its
    own abort key.
    """

    def __init__(self, vk: int = VK_ESCAPE, window_s: float = 0.5,
                 on_trigger: Callable[[], None] | None = None):
        self.vk = vk
        self.window_s = window_s
        self.on_trigger = on_trigger
        self._last = 0.0

    def __call__(self, ev: InputEvent) -> None:
        if ev.kind != "key" or ev.action != "down" or ev.vk != self.vk:
            return
        if not ev.physical:
            return
        now = ev.t
        if now - self._last < self.window_s:
            self._last = 0.0
            if self.on_trigger:
                threading.Thread(target=self.on_trigger, daemon=True).start()
        else:
            self._last = now


class ActivityTracker:
    """Track physical user activity, ignoring anything we injected ourselves.

    Movement and intent are tracked separately, and that distinction matters
    for perceived speed: someone watching the agent work moves the mouse
    constantly without meaning to intervene. Treating a stray mouse move as a
    takeover made every action pause for seconds.

    Clicks, keystrokes and scrolls are intent. Bare cursor movement is not.
    """

    def __init__(self) -> None:
        self.last_physical: float = 0.0     # any physical event
        self.last_intent: float = 0.0       # click / key / scroll only
        self.last_kind: str = ""
        self.physical_count: int = 0
        self.intent_count: int = 0
        self.injected_count: int = 0

    def __call__(self, ev: InputEvent) -> None:
        if not ev.physical:
            self.injected_count += 1
            return
        self.last_physical = ev.t
        self.last_kind = f"{ev.kind}:{ev.action}"
        self.physical_count += 1
        if ev.kind == "key" or ev.action not in ("move",):
            self.last_intent = ev.t
            self.intent_count += 1

    def idle_s(self) -> float:
        """Seconds since the user did something deliberate."""
        return time.time() - self.last_intent if self.last_intent else float("inf")

    def motion_idle_s(self) -> float:
        """Seconds since any physical event at all, including bare movement."""
        return time.time() - self.last_physical if self.last_physical else float("inf")
