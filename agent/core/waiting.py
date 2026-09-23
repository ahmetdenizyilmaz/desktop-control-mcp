"""Waiting for a person is not being stuck.

A script that waits for "buy" in a chat, for an SMS code, or for an answer
to "may I click Pay?" is doing its job. Treating that as stuck would hand a
perfectly healthy run to Claude; treating it as normal forever would leave
the task hanging while the person has forgotten it exists. So:

    0 min      orange: "Waiting for you: 'buy' in the chat"
    5 min      a reminder, wherever the person can be reached
    60 min     the task PAUSES - not fails. Resume carries on from the
               same wait, with the clock reset.

The minutes come from Settings. `tick()` is called about four times a
second by whoever owns the run; nothing here sleeps.
"""

from __future__ import annotations

import time
from typing import Callable


class WaitPolicy:
    def __init__(self, remind_after_s: float, pause_after_s: float,
                 notify: Callable[[str], object],
                 say: Callable[[str, str], None],
                 set_paused: Callable[[bool], None],
                 is_paused: Callable[[], bool],
                 task: str = "") -> None:
        self.remind_after_s = remind_after_s
        self.pause_after_s = pause_after_s
        self.notify = notify
        self.say = say
        self.set_paused = set_paused
        self.is_paused = is_paused
        self.task = task
        self.reason = ""
        self.since = 0.0
        self.reminded = False
        self.paused_by_us = False

    @property
    def active(self) -> bool:
        return bool(self.reason)

    def begin(self, reason: str) -> None:
        self.reason = reason
        self.since = time.time()
        self.reminded = False
        self.paused_by_us = False

    def end(self) -> None:
        self.reason = ""
        if self.paused_by_us and self.is_paused():
            # The wait ended on its own (the answer came while paused).
            self.set_paused(False)
        self.paused_by_us = False

    def waited(self) -> float:
        return time.time() - self.since if self.active else 0.0

    def view(self) -> dict:
        if not self.active:
            return {}
        return {"waiting_for": self.reason, "since": self.since,
                "remind_at": self.since + self.remind_after_s,
                "pause_at": self.since + self.pause_after_s,
                "reminded": self.reminded, "paused": self.paused_by_us}

    def tick(self) -> None:
        if not self.active:
            return
        if self.paused_by_us:
            if not self.is_paused():
                # The person pressed Resume: wait again, full clock.
                self.say("system", f"Resumed — still waiting for "
                                   f"{self.reason}.")
                self.begin(self.reason)
            return
        w = self.waited()
        if not self.reminded and w >= self.remind_after_s:
            self.reminded = True
            msg = (f"Still waiting for you: {self.reason}"
                   + (f" — task: {self.task[:80]}" if self.task else ""))
            self.say("system", msg)
            try:
                self.notify(msg)
            except Exception:
                pass
        if w >= self.pause_after_s:
            self.paused_by_us = True
            self.set_paused(True)
            took = f"{w / 60:.0f} min" if w >= 90 else f"{w:.0f} s"
            msg = (f"Paused after {took} waiting for "
                   f"{self.reason}. Press Resume to keep waiting.")
            self.say("system", msg)
            try:
                self.notify(msg)
            except Exception:
                pass
