"""The app's drawn cursor: where the agent is acting, and who is driving.

Replaces the older cartoon pointer (`overlay.PointerOverlay` + `pointer.py`)
while a task runs. The drawing itself lives in `pipeline/cursor.py` — two
black wings with a coloured outline, splaying with speed, breathing in and
out when idle, throwing four short lines on every click — and is used here
as-is, so there is one cursor design in the project rather than two.

What this module adds is the WIRING:

  * position   `actuator.action_hooks` announces every click, UIA invoke,
               keystroke and typed string just before it lands, with the
               point it lands on (a UIA invoke's element, the caret or the
               focused control for keys). The drawn cursor goes there — which
               is the whole point, because a UIA invoke and a keystroke never
               move the real pointer and would otherwise happen invisibly.

  * colour     red while the app is touching the machine, blue while it is
               thinking or waiting — from the same state the screen border
               uses — and green when a PERSON moves the mouse or types.

  * manual     the "person" signal comes from the shared low-level input hook
               (`input_monitor.monitor`), which reads the OS's own injected
               flag, so the app's clicks are never mistaken for a person.
               While a person moves the mouse the drawn cursor follows the
               real one: the system cursor is hidden while this is up, and
               leaving the only visible pointer parked on the app's last
               target would leave them blind.

The system cursor is hidden on `show` and restored on `hide`, and restored
again at exit whatever happens, because a crash between the two would
otherwise leave the machine with no pointer at all.
"""

from __future__ import annotations

import atexit
import sys
import threading
from pathlib import Path

_PIPELINE = Path(__file__).resolve().parents[2] / "pipeline"

#: overlay colour (r, g, b) -> cursor mode. The border's orange ("waiting on
#: a person") has no cursor colour of its own; the cursor only says who is
#: driving, and while a question is open the app is not driving.
_RED, _ORANGE, _BLUE = (230, 40, 40), (245, 150, 30), (40, 120, 235)

_lock = threading.Lock()
_cursor = None
_mod = None
_shown = False
_system_hidden = False
_sub = None


def _module():
    global _mod
    if _mod is None:
        if str(_PIPELINE) not in sys.path:
            sys.path.insert(0, str(_PIPELINE))
        import cursor as mod                       # pipeline/cursor.py
        _mod = mod
    return _mod


def _mode_for(color) -> str:
    if color is None:
        return "thinking"
    try:
        from . import overlay
        if tuple(color) == tuple(overlay.RED):
            return "control"
    except Exception:
        if tuple(color) == _RED:
            return "control"
    return "thinking"


def _on_action(kind: str, x: int, y: int) -> None:
    """actuator hook: an action is about to land at (x, y)."""
    c = _cursor
    if c is None or not _shown:
        return
    c.state({"click": "click", "drag": "click", "scroll": "moving",
             "type": "type", "key": "type"}.get(kind, "click"))
    c.move_to(x, y)
    if kind == "click":
        c.burst(x, y)


def _on_input(ev) -> None:
    """input hook: only PHYSICAL input counts as a person taking over."""
    c = _cursor
    if c is None or not _shown or ev.physical is False:
        return
    if ev.kind == "mouse" and ev.pos is not None:
        c.manual_at(*ev.pos)
        if ev.action in ("left_down", "right_down"):
            c.burst(*ev.pos)                 # a person's click bursts too
    elif ev.kind == "key" and ev.action == "down":
        c.manual_at()


def _ensure():
    """Create and start the cursor once, and wire both hooks to it."""
    global _cursor, _sub
    if _cursor is not None:
        return _cursor
    mod = _module()
    c = mod.Cursor()
    if not c.start():
        return None
    try:
        from . import actuator
        if _on_action not in actuator.action_hooks:
            actuator.action_hooks.append(_on_action)
    except Exception:
        pass
    try:
        from .input_monitor import monitor
        if monitor.running:
            # A real hook is better evidence than the cursor's own guess
            # from pointer movement, so turn the guess off.
            c.auto_manual = False
        _sub = monitor.subscribe(_on_input)
    except Exception:
        pass
    _cursor = c
    atexit.register(_restore_system)
    return c


def _restore_system() -> None:
    global _system_hidden
    try:
        _module().restore_system_cursor()
    except Exception:
        pass
    _system_hidden = False


def show(color=None) -> None:
    """Up, in the colour for this state; hides the system cursor."""
    global _shown, _system_hidden
    with _lock:
        c = _ensure()
        if c is None:
            return
        c.mode(_mode_for(color))
        if not _shown:
            try:
                from .input_monitor import monitor
                c.auto_manual = not monitor.running
            except Exception:
                pass
            c.show()
            _shown = True
        if not _system_hidden:
            try:
                _system_hidden = bool(_module().hide_system_cursor())
            except Exception:
                _system_hidden = False


def hide() -> None:
    """Down, and the person's own cursor back."""
    global _shown
    with _lock:
        _shown = False
        if _cursor is not None:
            _cursor.hide()
        _restore_system()


def stop() -> None:
    global _cursor, _shown
    with _lock:
        _shown = False
        try:
            from . import actuator
            if _on_action in actuator.action_hooks:
                actuator.action_hooks.remove(_on_action)
        except Exception:
            pass
        try:
            from .input_monitor import monitor
            if _sub is not None:
                monitor.unsubscribe(_sub)
        except Exception:
            pass
        if _cursor is not None:
            _cursor.stop()
            _cursor = None
        _restore_system()
