"""Who is allowed to drive the desktop, and how the user takes it back.

Before this module the repository had 751 lines of lock, overlay and ESC-abort
code that nothing imported. Combined with a launcher that disables permission
prompts and pyautogui's failsafe being off, there was no abort path at any
layer. This wires that work in and adds the parts it was missing.

Four guarantees:

  1. one driver at a time      — a file lock across processes, so two Claude
                                 sessions cannot interleave clicks
  2. the user can always stop  — physical ESC+ESC aborts; injected ESC cannot,
                                 because the shared hook separates the two
  3. touching the machine wins — physical input pauses the run, and a window
                                 or focus change is treated as a takeover
  4. the screen stays usable   — display wake-lock held for the run, restored
                                 afterwards

The overlay makes all of this visible: a red border and an action log, so it
is never ambiguous whether something else is driving.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from . import display
from .input_monitor import (ActivityTracker, DoubleTapWatcher, InputEvent,
                            VK_ESCAPE, monitor)

user32 = ctypes.windll.user32

# Reuse the lock manager and overlay already written in the repository root.
# APPENDED, never inserted: the repo root holds an older app.py, and putting it
# ahead of agent/ makes `import app` resolve to the wrong module — a shadowing
# bug that only shows up once something imports the app by name.
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.append(str(_REPO))


class Control(str, Enum):
    IDLE = "idle"                 # nobody driving
    DRIVING = "driving"           # we hold the lock and are acting
    PAUSED = "paused"             # user touched the machine; waiting them out
    YIELDED = "yielded"           # user took over; run must stop
    BLOCKED = "blocked"           # another process holds the lock
    ABORTED = "aborted"           # ESC+ESC


@dataclass
class Interference:
    """What the user did while we were driving."""
    physical_events: int = 0
    first_at: float = 0.0
    last_at: float = 0.0
    window_changed: bool = False
    reason: str = ""


@dataclass
class ArbiterState:
    control: Control = Control.IDLE
    owner: str = ""
    since: float = field(default_factory=time.time)
    interference: Interference = field(default_factory=Interference)
    detail: str = ""


class Arbiter:
    """Single point of authority over desktop actuation."""

    #: Escalating re-check after DELIBERATE input (a click, a keystroke). Bare
    #: mouse movement no longer triggers this: someone watching the agent work
    #: moves the cursor constantly, and pausing for it cost seconds on every
    #: single action, which is most of why the app felt slow.
    BACKOFF = (0.25, 0.5, 1.0)
    #: How recently deliberate input counts as "the user is doing something".
    INTENT_WINDOW_S = 0.8

    def __init__(self, owner: str | None = None, use_overlay: bool = True):
        self.owner = owner or self._detect_owner()
        self.state = ArbiterState(owner=self.owner)
        self._lock_mgr = None
        self._overlay = None
        self._use_overlay = use_overlay
        self._keeper: display.KeepSessionOpen | None = None
        self._activity = ActivityTracker()
        self._esc = DoubleTapWatcher(VK_ESCAPE, 0.5, self._on_abort)
        self._subs: list = []
        self._aborted = threading.Event()
        self._baseline_window = 0
        self._started = False

    # -- identity --------------------------------------------------------
    @staticmethod
    def _detect_owner() -> str:
        try:
            import psutil
            parent = psutil.Process(os.getppid())
            cmd = " ".join(parent.cmdline()).lower()
            for needle, name in (("claude", "Claude Code"), ("cursor", "Cursor"),
                                 ("windsurf", "Windsurf"), ("code", "VS Code")):
                if needle in cmd:
                    return name
            return f"{parent.name()} (PID {parent.pid})"
        except Exception:
            return f"PID {os.getpid()}"

    # -- lifecycle -------------------------------------------------------
    def start(self) -> bool:
        """Acquire control. False if another process already holds it."""
        if self._started:
            return self.state.control is not Control.BLOCKED

        try:
            from lock_manager import LockManager
            self._lock_mgr = LockManager()
            ok, msg = self._lock_mgr.ensure_locked()
            if not ok:
                self.state.control = Control.BLOCKED
                self.state.detail = msg
                return False
            self._overlay = getattr(self._lock_mgr, "_overlay", None)
        except Exception as e:
            # No cross-process lock available. Continue single-process rather
            # than refusing to run, but say so — a silent downgrade of the
            # only arbitration mechanism would be worse than the failure.
            self.state.detail = f"lock manager unavailable: {type(e).__name__}: {e}"

        self._keeper = display.KeepSessionOpen()
        self._keeper.__enter__()

        monitor.start()
        self._subs = [monitor.subscribe(self._activity),
                      monitor.subscribe(self._esc)]
        # If the low-level hook did not install, ESC+ESC cannot be seen and
        # the user's input cannot pause the run. Both are safety promises the
        # UI makes on screen, so failing silently is the worst option.
        self.hook_ok = bool(monitor.running)
        if not self.hook_ok:
            self.state.detail = ("keyboard hook unavailable — ESC+ESC will not "
                                 "abort; stop from the window instead")

        self._baseline_window = user32.GetForegroundWindow()
        self.state.control = Control.DRIVING
        self.state.since = time.time()
        self._started = True
        self.note(f"{self.owner} has control")
        return True

    def stop(self) -> None:
        for s in self._subs:
            monitor.unsubscribe(s)
        self._subs.clear()
        if self._keeper is not None:
            self._keeper.__exit__(None, None, None)
            self._keeper = None
        if self._lock_mgr is not None:
            try:
                self._lock_mgr.release()
            except Exception:
                pass
        if self.state.control is Control.DRIVING:
            self.state.control = Control.IDLE
        self._started = False

    def __enter__(self) -> "Arbiter":
        self.start()
        return self

    def __exit__(self, *exc) -> bool:
        self.stop()
        return False

    # -- visibility ------------------------------------------------------
    def note(self, text: str) -> None:
        """Show what is being done, on screen, while it happens."""
        if self._overlay is not None and self._use_overlay:
            try:
                self._overlay.add_action(text)
            except Exception:
                pass

    # -- abort -----------------------------------------------------------
    def _on_abort(self) -> None:
        self._aborted.set()
        self.state.control = Control.ABORTED
        self.state.detail = "physical ESC+ESC"
        if self._lock_mgr is not None:
            try:
                self._lock_mgr.emergency_release()
            except Exception:
                pass

    @property
    def aborted(self) -> bool:
        return self._aborted.is_set()

    # -- the check every action must pass --------------------------------
    def check(self, settle: bool = True) -> ArbiterState:
        """Call before every mutating action.

        Returns the current state. DRIVING means proceed; anything else means
        the caller must not act.
        """
        if self._aborted.is_set():
            self.state.control = Control.ABORTED
            return self.state

        if self._lock_mgr is not None:
            try:
                ok, msg = self._lock_mgr.ensure_locked()
                if not ok:
                    self.state.control = Control.BLOCKED
                    self.state.detail = msg
                    return self.state
            except Exception:
                pass

        # A foreground change is decisive on its own — the user has gone
        # somewhere else, and acting now would act inside whatever they opened.
        fg = user32.GetForegroundWindow()
        if fg and fg != self._baseline_window and self._activity.intent_count:
            if self._activity.motion_idle_s() < 3.0:
                self.state.interference.window_changed = True
                self.state.control = Control.YIELDED
                self.state.detail = "user switched windows — handing back"
                return self.state
            # They moved us there themselves, or a dialog appeared; adopt it.
            self._baseline_window = fg

        idle = self._activity.idle_s()
        if idle < self.INTENT_WINDOW_S:
            self.state.interference.physical_events = self._activity.intent_count
            if not self.state.interference.first_at:
                self.state.interference.first_at = time.time()
            self.state.interference.last_at = self._activity.last_intent
            self.state.control = Control.PAUSED
            self.state.detail = (f"user input {idle:.1f}s ago "
                                 f"({self._activity.last_kind})")
            if settle:
                return self._settle()
            return self.state

        self.state.control = Control.DRIVING
        return self.state

    def _settle(self) -> ArbiterState:
        """Wait out a nudge; concede to a takeover.

        A window or focus change while the user is active is decisive — they
        have gone somewhere else, and continuing would act inside whatever
        they opened.
        """
        for wait in self.BACKOFF:
            if self._aborted.is_set():
                self.state.control = Control.ABORTED
                return self.state
            time.sleep(wait)

            fg = user32.GetForegroundWindow()
            if fg != self._baseline_window:
                self.state.interference.window_changed = True
                self.state.interference.reason = "foreground window changed"
                self.state.control = Control.YIELDED
                self.state.detail = "user switched windows — handing back"
                self.note("user took over — stopping")
                return self.state

            if self._activity.idle_s() >= self.INTENT_WINDOW_S:
                self.state.control = Control.DRIVING
                self.state.detail = "user idle again — resuming"
                return self.state

        self.state.interference.reason = "sustained physical input"
        self.state.control = Control.YIELDED
        self.state.detail = "user kept typing/moving — handing back"
        self.note("user took over — stopping")
        return self.state

    # -- invited interaction ---------------------------------------------
    def rebaseline(self, settle_s: float = 0.8) -> None:
        """Forget interference caused by an interaction we asked for.

        When the agent puts a question on screen and the user clicks Approve,
        that click is physical and it changes the foreground window — the two
        signals that normally mean "the user has taken over". Without this the
        run would stop the instant they answered, which is precisely backwards:
        they answered in order for it to continue.

        Only ever call this after an interaction the agent itself solicited.
        Calling it speculatively would discard the takeover detection that the
        user's ability to reclaim the machine depends on.
        """
        time.sleep(settle_s)                       # let the click and focus land
        self._activity.last_intent = 0.0
        self._activity.last_physical = 0.0
        self._baseline_window = user32.GetForegroundWindow()
        self.state.interference = Interference()
        if self.state.control in (Control.PAUSED, Control.YIELDED):
            self.state.control = Control.DRIVING
            self.state.detail = "resumed after an answer we asked for"

    # -- convenience -----------------------------------------------------
    def guard(self, action_desc: str = "") -> bool:
        """True if it is safe to act right now; records the action if so."""
        st = self.check()
        if st.control is Control.DRIVING:
            if action_desc:
                self.note(action_desc)
            return True
        return False

    def status(self) -> dict:
        return {
            "owner": self.owner,
            "control": self.state.control.value,
            "detail": self.state.detail,
            "physical_events": self._activity.physical_count,
            "injected_events": self._activity.injected_count,
            # Finite or absent: the UI parses this as JSON, and JSON has no
            # Infinity. Bare motion sets last_physical but not last_intent,
            # which is exactly when idle_s() is infinite.
            "idle_s": round(self._activity.idle_s(), 2)
            if self._activity.last_intent else None,
            "hook_running": monitor.running,
            "hook_ok": getattr(self, "hook_ok", True),
            "aborted": self.aborted,
            "lock": (self._lock_mgr.get_status() if self._lock_mgr else None),
        }
