"""Recording — turn what happened into something replayable.

Two streams, deliberately unequal:

  events   precise, cheap, resolved at capture time. The signal.
  frames   0.5s cadence, kept only on change. Context for intent inference.

Resolving each click to a UIA element AT CAPTURE TIME is what makes a
recording portable. Ten milliseconds spent here is the difference between
storing a coordinate (dead the moment anything moves) and storing identity
(survives moves, resizes, themes, DPI, and other machines).

Frames are deduplicated because a naive 0.5s cadence over a five minute
session is 600 images, and intent inference over 600 images is unaffordable.
Sitting still produces one frame, not forty.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from . import display, locator
from .input_monitor import InputEvent, monitor
from .schema import Locator


@dataclass
class RecordedEvent:
    t: float
    type: str                       # click | key | type | scroll
    action: str = ""
    keys: str = ""
    text: str = ""
    pos: tuple[int, int] | None = None
    wheel: int = 0
    # resolved identity — the whole point of capturing live
    locators: list[dict] = field(default_factory=list)
    element_name: str = ""
    element_type: str = ""
    window_title: str = ""
    window_exe: str = ""
    frame: str | None = None


@dataclass
class Frame:
    name: str
    t: float
    changed_pct: float
    window: str


class Recorder:
    """Capture a demonstration into events + deduplicated frames."""

    FRAME_INTERVAL = 0.5
    #: below this much visual change a frame is redundant
    FRAME_CHANGE_MIN = 2.0
    #: keystrokes closer together than this are one typing burst
    TYPING_GAP = 1.0

    def __init__(self, out_dir: Path | str, capture_frames: bool = True):
        self.dir = Path(out_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.frames_dir = self.dir / "frames"
        self.capture_frames = capture_frames
        self.events: list[RecordedEvent] = []
        self.frames: list[Frame] = []

        self._sub = None
        self._frame_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._started_at = 0.0
        self._prev_gray = None
        self._pending_keys: list[tuple[float, str]] = []
        self._lock = threading.Lock()
        self._keeper: display.KeepSessionOpen | None = None

    # -- lifecycle -------------------------------------------------------
    def start(self) -> bool:
        if not monitor.start():
            return False
        self._started_at = time.time()
        self._stop.clear()
        self._keeper = display.KeepSessionOpen()
        self._keeper.__enter__()

        if self.capture_frames:
            self.frames_dir.mkdir(exist_ok=True)
            self._frame_thread = threading.Thread(target=self._frame_loop, daemon=True)
            self._frame_thread.start()

        self._sub = monitor.subscribe(self._on_input)
        return True

    def stop(self) -> dict:
        self._stop.set()
        if self._sub is not None:
            monitor.unsubscribe(self._sub)
            self._sub = None
        if self._frame_thread is not None:
            self._frame_thread.join(timeout=3.0)
        self._flush_typing()
        if self._keeper is not None:
            self._keeper.__exit__(None, None, None)
            self._keeper = None
        return self.save()

    def __enter__(self) -> "Recorder":
        self.start()
        return self

    def __exit__(self, *exc) -> bool:
        self.stop()
        return False

    # -- input capture ---------------------------------------------------
    def _on_input(self, ev: InputEvent) -> None:
        # Only the user is being recorded. Anything this process injects is
        # our own replay or probing and must never enter a recording.
        if not ev.physical:
            return

        if ev.kind == "mouse" and ev.action in ("left_down", "right_down", "middle_down"):
            self._flush_typing()
            self._record_click(ev)
        elif ev.kind == "mouse" and ev.action == "wheel":
            self._flush_typing()
            self._append(RecordedEvent(t=ev.t - self._started_at, type="scroll",
                                       pos=ev.pos, wheel=ev.wheel))
        elif ev.kind == "key" and ev.action == "down":
            self._record_key(ev)

    def _record_click(self, ev: InputEvent) -> None:
        x, y = ev.pos or (0, 0)
        rec = RecordedEvent(t=ev.t - self._started_at, type="click",
                            action=ev.action, pos=(x, y))
        try:
            el = locator.hit_test(x, y)
            if el is not None:
                rec.locators = [{"type": l.type.value, "value": l.value}
                                for l in locator.describe(el)]
                rec.element_name = el.name
                rec.element_type = el.control_type
                if el.hwnd:
                    rec.window_title = locator.window_title(el.hwnd)
        except Exception:
            pass
        if not rec.window_title:
            fg = locator.foreground_window()
            rec.window_title = locator.window_title(fg)
        rec.frame = self.frames[-1].name if self.frames else None
        self._append(rec)

    def _record_key(self, ev: InputEvent) -> None:
        vk = ev.vk or 0
        # Printable characters accumulate into a typing burst; everything else
        # is a discrete key press worth its own step.
        if 0x30 <= vk <= 0x5A or 0x60 <= vk <= 0x6F or vk in (0x20, 0xBA, 0xBC,
                                                              0xBE, 0xBD, 0xC0):
            with self._lock:
                self._pending_keys.append((ev.t, chr(vk) if 0x20 <= vk < 0x7F else ""))
            return
        self._flush_typing()
        self._append(RecordedEvent(t=ev.t - self._started_at, type="key",
                                   keys=_vk_name(vk)))

    def _flush_typing(self) -> None:
        with self._lock:
            if not self._pending_keys:
                return
            t0 = self._pending_keys[0][0]
            text = "".join(c for _, c in self._pending_keys)
            self._pending_keys.clear()
        if text.strip():
            self._append(RecordedEvent(t=t0 - self._started_at, type="type",
                                       text=text))

    def _append(self, rec: RecordedEvent) -> None:
        with self._lock:
            self.events.append(rec)

    # -- frames ----------------------------------------------------------
    def _frame_loop(self) -> None:
        import numpy as np
        while not self._stop.wait(self.FRAME_INTERVAL):
            try:
                img, _ = _capture()
                gray = np.asarray(img.convert("L").resize((160, 100)), dtype="int16")
                if self._prev_gray is None:
                    changed = 100.0
                else:
                    diff = np.abs(gray - self._prev_gray)
                    changed = float((diff > 18).mean() * 100)
                if changed < self.FRAME_CHANGE_MIN:
                    continue        # nothing happened; a duplicate frame is noise
                self._prev_gray = gray
                name = f"f_{len(self.frames):04d}.jpg"
                img.save(self.frames_dir / name, format="JPEG", quality=45)
                fg = locator.foreground_window()
                self.frames.append(Frame(name=name, t=time.time() - self._started_at,
                                         changed_pct=round(changed, 1),
                                         window=locator.window_title(fg)))
            except Exception:
                continue

    # -- output ----------------------------------------------------------
    def save(self) -> dict:
        events_path = self.dir / "events.jsonl"
        with open(events_path, "w", encoding="utf-8") as f:
            for e in self.events:
                f.write(json.dumps(asdict(e), separators=(",", ":")) + "\n")

        frames_path = self.dir / "frames.jsonl"
        with open(frames_path, "w", encoding="utf-8") as f:
            for fr in self.frames:
                f.write(json.dumps(asdict(fr), separators=(",", ":")) + "\n")

        meta = {
            "duration_s": round(time.time() - self._started_at, 2),
            "events": len(self.events),
            "frames_kept": len(self.frames),
            "dir": str(self.dir),
        }
        (self.dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return meta

    # -- summarisation for intent inference -------------------------------
    def summary(self, max_frames: int = 15) -> dict:
        """Compact payload for a model to infer intent from.

        Events lead because they state precisely what happened. Frames are
        subsampled to the largest visual changes — those are the moments that
        explain *why*, and sending all of them is what makes this unaffordable.
        """
        top = sorted(self.frames, key=lambda f: f.changed_pct, reverse=True)[:max_frames]
        top = sorted(top, key=lambda f: f.t)
        return {
            "duration_s": round(time.time() - self._started_at, 2),
            "events": [
                {k: v for k, v in asdict(e).items()
                 if v not in (None, "", [], 0)}
                for e in self.events
            ],
            "keyframes": [{"name": f.name, "t": round(f.t, 1),
                           "changed_pct": f.changed_pct, "window": f.window}
                          for f in top],
            "windows_touched": sorted({e.window_title for e in self.events
                                       if e.window_title}),
        }


def _capture():
    import sys as _sys
    from pathlib import Path as _P
    root = str(_P(__file__).resolve().parents[2])
    if root not in _sys.path:
        _sys.path.append(root)   # append: repo root must not shadow agent/
    import controller
    return controller._capture_screen()


_VK_NAMES = {
    0x0D: "enter", 0x09: "tab", 0x1B: "escape", 0x08: "backspace",
    0x2E: "delete", 0x20: "space", 0x26: "up", 0x28: "down",
    0x25: "left", 0x27: "right", 0x24: "home", 0x23: "end",
    0x21: "pageup", 0x22: "pagedown", 0x11: "ctrl", 0x12: "alt",
    0x10: "shift", 0x5B: "win",
}


def _vk_name(vk: int) -> str:
    if vk in _VK_NAMES:
        return _VK_NAMES[vk]
    if 0x70 <= vk <= 0x87:
        return f"f{vk - 0x6F}"
    if 0x20 <= vk < 0x7F:
        return chr(vk).lower()
    return f"vk_{vk:02x}"
