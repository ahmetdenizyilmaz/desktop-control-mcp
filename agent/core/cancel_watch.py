""""cancel" from wherever the person is, at any moment of a task.

The first live errand run exposed the gap: the person wrote "cancel" twice
in WhatsApp while the script was switching tabs, and nothing listened -
cancel only counted inside a step that happened to be waiting for chat
messages. The Stop button and ESC+ESC work anywhere, but only for someone
at the machine.

Two listeners, on their own thread, for the whole task:

  telegram   a message "cancel" / "iptal" / "stop" / "/stop" to the paired
             bot. Polled every few seconds - but NOT while a question is
             out on Telegram: `Telegram.ask` reads the same update stream,
             and two readers would each swallow the other's messages. While
             a question is pending, its own answer path is how to say no.
  chat       a window a script names (`"cancel": {"window": "WhatsApp"}`):
             a NEW message whose whole text is a cancel word. Read through
             UI Automation, so it works while another window is in front -
             but only for what that window is showing: a chat in a
             background TAB is not in the tree, a chat in its own window is.

When either fires, `on_cancel(reason)` is called once - the app wires that
to the same stop the Stop button uses.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

WORDS = ("cancel", "iptal", "stop", "dur", "/stop", "/cancel")


def is_cancel(text: str) -> bool:
    t = (text or "").strip().lower().rstrip(".!")
    if t in WORDS:
        return True
    # A chat control's name can carry the time after the text: "cancel 01:16"
    first, _, rest = t.partition(" ")
    return first in WORDS and len(rest) <= 12 and ":" in rest


class CancelWatch:
    def __init__(self, on_cancel: Callable[[str], None],
                 poll_s: float = 4.0) -> None:
        self.on_cancel = on_cancel
        self.poll_s = poll_s
        self.fired = ""
        self.chat_window = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._chat_base: set[str] | None = None
        self._chat_pending: set[str] = set()

    def start(self) -> "CancelWatch":
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="cancel-watch")
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def watch_chat(self, window: str) -> None:
        """Also listen in this window (a title substring). Empty = off."""
        if window != self.chat_window:
            self.chat_window = window
            self._chat_base = None
            self._chat_pending = set()

    def _fire(self, reason: str) -> None:
        if self.fired:
            return
        self.fired = reason
        try:
            self.on_cancel(reason)
        except Exception:
            pass

    # -- telegram -------------------------------------------------------------
    def _telegram(self) -> None:
        from .channel import channel
        tg = channel.telegram
        if not tg.configured or channel.pending is not None:
            return
        for upd in tg._updates(timeout=0):
            msg = upd.get("message") or {}
            if is_cancel(msg.get("text") or ""):
                self._fire("'cancel' on Telegram")
                return

    # -- a chat window, through UIA on THIS thread ------------------------------
    def _chat_names(self) -> set[str] | None:
        import uiautomation as auto
        from . import locator
        h = locator.find_window(title_contains=self.chat_window)
        if not h:
            return None
        root = auto.ControlFromHandle(h)
        names: set[str] = set()
        stack, seen = [root], 0
        while stack and seen < 2500:
            c = stack.pop()
            seen += 1
            try:
                n = (c.Name or "").strip()
                if n and len(n) <= 40:
                    names.add(n.lower())
                stack.extend(c.GetChildren())
            except Exception:
                continue
        return names

    def _chat(self) -> None:
        if not self.chat_window:
            return
        names = self._chat_names()
        if names is None:
            return
        if self._chat_base is None:
            self._chat_base = names
            return
        fresh = {n for n in names - self._chat_base if is_cancel(n)}
        # Seen twice before it counts: chat apps flash transient controls.
        confirmed = fresh & self._chat_pending
        if confirmed:
            self._fire(f"'{sorted(confirmed)[0]}' in {self.chat_window}")
        self._chat_pending = fresh

    def _loop(self) -> None:
        com = False
        try:
            import ctypes
            ctypes.windll.ole32.CoInitializeEx(None, 0x2)
            com = True
        except Exception:
            pass
        while not self._stop.is_set() and not self.fired:
            for part in (self._telegram, self._chat):
                try:
                    part()
                except Exception:
                    pass
            self._stop.wait(self.poll_s)
        if com:
            try:
                import ctypes
                ctypes.windll.ole32.CoUninitialize()
            except Exception:
                pass
