"""Screen recorder — an optional, low-size video of the session at ~15 fps.

A stream apart from anything the model reasons over: the agent never sees these
frames. They are for the user, to watch back what actually happened. So the
whole design leans on being cheap and small — a downscaled grab at a fixed
cadence, written straight into a compressed video file, with no per-frame
image files left behind to clean up.

Encoded with OpenCV's VideoWriter: mp4v into an .mp4, falling back to MJPG in
an .avi if that codec is missing on the machine. If neither writer opens,
start() reports it rather than leaving an empty file behind — a silently
failing VideoWriter is a 0-byte file, which is the one outcome worse than an
honest error.

Its own thread with its own mss instance (mss objects have thread affinity),
made per-monitor DPI-aware first so the grab covers the whole physical screen
rather than a scaled-down corner of it.

The pointer is drawn in. A screen grab never contains the cursor — it is
composited by the display driver, not the framebuffer — so a recording of an
agent clicking around showed things changing with no visible cause. Every
frame gets the same cartoon pointer the screen shows (core/pointer.py): the
halo in the state's colour, the squash of a press, the ripple of a click.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

#: A half-size recording would have a half-size pointer. Its job is to be
#: seen, so it never shrinks below this.
MIN_POINTER_SCALE = 0.75


class ScreenCast:
    def __init__(self, out_path, fps: float = 15.0, scale: float = 0.5,
                 whole_desktop: bool = False) -> None:
        self.out_path = Path(out_path)
        self.fps = max(1.0, min(30.0, float(fps)))
        self.scale = max(0.1, min(1.0, float(scale)))
        self.whole_desktop = bool(whole_desktop)
        self.frames = 0
        self.started_at = 0.0
        self.stopped_at = 0.0
        self.dims = (0, 0)
        self.error = ""
        self._writer = None
        self._thread: "threading.Thread | None" = None
        self._stop = threading.Event()
        self._ready = threading.Event()

    def start(self) -> bool:
        """Begin recording. Blocks briefly until the first grab and the video
        writer are open, so a codec failure is reported here and not at stop."""
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._loop, name="screencast",
                                        daemon=True)
        self._thread.start()
        self._ready.wait(timeout=4.0)
        return self.error == "" and self._writer is not None

    def _open_writer(self, w: int, h: int):
        import cv2
        for fourcc_name, suffix in (("mp4v", ".mp4"), ("MJPG", ".avi")):
            path = self.out_path.with_suffix(suffix)
            try:
                fourcc = cv2.VideoWriter_fourcc(*fourcc_name)
                vw = cv2.VideoWriter(str(path), fourcc, self.fps, (w, h))
                if vw.isOpened():
                    self.out_path = path
                    return vw
                vw.release()
            except Exception:
                continue
        return None

    def _loop(self) -> None:
        try:
            import cv2
            import mss
            import numpy as np
            from core import actuator
            from core import pointer as ptr
            try:
                actuator.make_thread_dpi_aware()
            except Exception:
                pass
            try:
                import ctypes
                dpi = max(1.0, ctypes.windll.user32.GetDpiForSystem() / 96.0)
            except Exception:
                dpi = 1.0
        except Exception as e:                       # pragma: no cover
            self.error = f"missing dependency: {e}"
            self._ready.set()
            return

        interval = 1.0 / self.fps
        self.started_at = time.time()
        try:
            with mss.mss() as sct:
                # monitors[0] is the whole virtual desktop, [1] the primary.
                mon = sct.monitors[0 if self.whole_desktop else 1]
                first = sct.grab(mon)
                # Even dimensions — some codecs reject odd width/height.
                w = max(2, int(first.width * self.scale)) // 2 * 2
                h = max(2, int(first.height * self.scale)) // 2 * 2
                self.dims = (w, h)
                self._writer = self._open_writer(w, h)
                if self._writer is None:
                    self.error = "no working video codec (tried mp4v, MJPG)"
                    self._ready.set()
                    return
                self._ready.set()

                next_t = time.perf_counter()
                while not self._stop.is_set():
                    shot = sct.grab(mon)
                    arr = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(
                        shot.height, shot.width, 4)
                    bgr = arr[:, :, :3]              # mss gives B,G,R,A — drop A
                    # Either branch yields a fresh, writable frame: the buffer
                    # view is read-only, and the pointer has to be drawn on it.
                    if (shot.width, shot.height) != (w, h):
                        frame = cv2.resize(bgr, (w, h),
                                           interpolation=cv2.INTER_AREA)
                    else:
                        frame = bgr.copy()
                    pos = ptr.cursor_pos()
                    if pos is not None:
                        now = time.time()
                        # Feed the model too: a recording made with no live
                        # overlay up still gets its clicks animated.
                        ptr.sample(pos[0], pos[1], ptr.button_down(), now)
                        fscale = w / shot.width
                        fx = int((pos[0] - mon["left"]) * fscale)
                        fy = int((pos[1] - mon["top"]) * h / shot.height)
                        ptr.composite(frame, pos[0], pos[1], fx, fy,
                                      frame_scale=fscale,
                                      sprite_scale=max(MIN_POINTER_SCALE,
                                                       dpi * fscale),
                                      now=now)
                    self._writer.write(frame)
                    self.frames += 1
                    # Hold the cadence; if encoding fell behind, resync rather
                    # than sprint to catch up (which would speed the video up).
                    next_t += interval
                    delay = next_t - time.perf_counter()
                    if delay > 0:
                        self._stop.wait(delay)
                    else:
                        next_t = time.perf_counter()
        except Exception as e:
            self.error = self.error or f"{type(e).__name__}: {e}"
            self._ready.set()
        finally:
            self.stopped_at = time.time()
            if self._writer is not None:
                try:
                    self._writer.release()
                except Exception:
                    pass

    def stop(self) -> dict:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=6.0)
        dur = max(0.0, (self.stopped_at or time.time())
                  - (self.started_at or time.time()))
        size = self.out_path.stat().st_size if self.out_path.exists() else 0
        return {
            "path": str(self.out_path),
            "seconds": round(dur, 1),
            "frames": self.frames,
            "target_fps": self.fps,
            "actual_fps": round(self.frames / dur, 1) if dur > 0 else 0.0,
            "dims": self.dims,
            "bytes": size,
            "error": self.error,
        }
