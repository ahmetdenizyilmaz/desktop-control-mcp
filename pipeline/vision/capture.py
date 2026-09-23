"""Getting pixels, and knowing when they are worth reading.

Measured on this machine (2880x1800): a full-screen grab costs 51 ms and a
300x200 region 16.6 ms — and a single pixel also costs ~16 ms, because the
price is the call, not the pixels. That one fact shapes everything here:

  * ask for a REGION whenever you know one. Full screen is the fallback,
    not the default.
  * grab ONCE and pass the image around. Two consumers of the same frame
    must not grab twice.
  * before reading, ask whether the pixels even changed. A 64x64 greyscale
    fingerprint answers that in ~1 ms, against 305 ms to OCR a full screen
    that turns out to be identical to the last one.

The blank-frame rule is inherited from the existing engine and is not
optional: a slept display captures as pure black and OCR then reports "no
text", which reads as a successful look at an empty screen. That silent wrong
answer cost the other project three benchmark runs, so a dark frame raises
here instead of returning nothing.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Fraction of pixels that must be above near-black for a frame to count as
#: lit. Chosen to survive a cursor composited onto an otherwise black screen.
LIT_MIN = 0.005

#: Side of the square the change-fingerprint is reduced to, and how many grey
#: levels survive. The pair is load-bearing and was measured, not guessed:
#:
#:      side x levels   caret      subtle dialog   dark dialog
#:      64 x 16         ignored    MISSED          caught
#:      64 x 32         false      caught          caught
#:      128 x 16        ignored    caught          caught     <- chosen
#:
#: 64x16 shipped first and was wrong in the dangerous direction: a white
#: dialog opening on a near-white page did not change the hash at all, so a
#: cached scene would have been reused for a screen that had visibly moved,
#: and the agent would have clicked coordinates that no longer meant
#: anything. Note the fix is more SPATIAL resolution, not more grey levels —
#: a dialog is a change of shape, so seeing finer detail catches it, while
#: quantising brightness coarsely still absorbs a blinking caret.
FINGERPRINT_SIDE = 128
FINGERPRINT_LEVELS = 16


class DisplayDark(RuntimeError):
    """The capture came back black. Never report this as 'nothing on screen'."""


@dataclass(frozen=True)
class Frame:
    """One grab: the image, where it came from, at what scale, and when.

    `width`/`height` are the SOURCE region on screen, not the image's own
    size — they differ whenever `scale` is not 1.0. Keeping the source
    dimensions here is what lets `to_screen` undo the scaling without the
    caller knowing any of it happened.
    """

    image: Any                      # PIL.Image.Image
    left: int
    top: int
    width: int
    height: int
    at: float
    #: Uniform factor the image was shrunk by. 1.0 = untouched. Uniform on
    #: purpose: a non-square scale changes the aspect ratio, which turns a
    #: circular icon into an ellipse and breaks template matching outright.
    scale: float = 1.0

    @property
    def region(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.width, self.height)

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        """Image coordinates -> absolute screen pixels.

        Undoes BOTH transforms: the scale first, then the region offset.
        This is the only place that arithmetic lives, and everything in this
        package routes through it, so a click computed from a shrunk crop
        lands in the same pixel as one computed from a full-size grab.
        Getting this wrong is invisible in a log and obvious on screen — the
        agent clicks steadily further from the target the further right it
        goes — so there is exactly one implementation.
        """
        return (int(round(self.left + x / self.scale)),
                int(round(self.top + y / self.scale)))

    def to_image(self, sx: float, sy: float) -> tuple[int, int]:
        """Absolute screen pixels -> image coordinates. The inverse."""
        return (int(round((sx - self.left) * self.scale)),
                int(round((sy - self.top) * self.scale)))


def from_image(source, left: int = 0, top: int = 0) -> Frame:
    """Wrap a saved picture, or a PIL image, as a Frame.

    Why this exists: a live grab is not reproducible. A saved screenshot can
    be re-read a hundred times while a threshold is tuned, shared with
    someone else, checked into a benchmark, and compared against the same
    ground truth months later — none of which a moving screen allows.

    `left`/`top` are where the picture came from on screen. A full-screen
    shot is (0, 0) and needs nothing; a crop must say where it was cut from,
    or every coordinate that comes back will be wrong by that offset and
    wrong in a way that looks plausible.
    """
    from PIL import Image

    if isinstance(source, (str, bytes)) or hasattr(source, "__fspath__"):
        img = Image.open(source).convert("RGB")
    else:
        img = source.convert("RGB") if source.mode != "RGB" else source
    return Frame(img, int(left), int(top), img.width, img.height, time.time())


class Screen:
    """A reusable grabber. One `mss` instance, kept warm.

    `mss` is not thread-safe and its handle is per-thread, so the instance is
    thread-local and created on first use in whichever thread asks.
    """

    def __init__(self) -> None:
        self._local = threading.local()

    def _sct(self):
        got = getattr(self._local, "sct", None)
        if got is None:
            import mss
            got = mss.mss()
            self._local.sct = got
        return got

    def size(self) -> tuple[int, int]:
        m = self._sct().monitors[1]
        return m["width"], m["height"]

    def prewarm(self) -> float:
        """Take one throwaway full grab, so the first real one is not the
        expensive one.

        Measured on this machine: the first full-screen grab costs 250 ms
        against 57-62 ms for every one after it. That is `mss` opening
        device contexts and allocating a buffer for 5.2 megapixels, and it
        happens per THREAD, because the grabber is thread-local — so call
        this from whichever thread will do the grabbing, not just once at
        import.

        Returns seconds taken; never raises, because a failed warm-up is not
        a reason to stop, and the first real grab will report the problem.
        """
        t0 = time.time()
        try:
            self.grab(allow_dark=True)
        except Exception:
            return 0.0
        return time.time() - t0

    #: Neither axis is shrunk below this. See `min_size` in `grab`.
    FLOOR = (1920, 1080)

    def grab(self, region: tuple[int, int, int, int] | None = None,
             allow_dark: bool = False, scale: float = 1.0,
             fit_height: int | None = None,
             min_size: tuple[int, int] | None = None) -> Frame:
        """Capture the screen, or a region as (left, top, width, height).

        `scale` shrinks the captured image uniformly — both axes by the same
        factor, so the aspect ratio is exactly preserved and a round icon
        stays round. `fit_height` computes that factor for you from a target
        height, which is usually what you mean: on a 2880x1800 (16:10)
        screen, `fit_height=1080` gives scale 0.6 exactly and a 1728x1080
        image, where forcing a literal 1920x1080 would squash it to 16:9.

        Coordinates coming back out are unaffected: everything routes
        through `Frame.to_screen`, which divides the scale out again.

        Measured on this machine: OCR time stops improving below about
        1920 wide (fixed overhead dominates) while accuracy keeps falling,
        so shrinking far past 1080p costs words and buys nothing. Narrowing
        the REGION is the better lever — it keeps every pixel native.
        """
        from PIL import Image

        sct = self._sct()
        if region is None:
            m = sct.monitors[1]
            box = {"left": m["left"], "top": m["top"],
                   "width": m["width"], "height": m["height"]}
        else:
            left, top, width, height = region
            box = {"left": int(left), "top": int(top),
                   "width": max(1, int(width)), "height": max(1, int(height))}

        shot = sct.grab(box)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

        if fit_height and box["height"] > fit_height:
            scale = fit_height / box["height"]
        if min_size:
            # COVER, not fit: scale so NEITHER axis falls below the floor,
            # which means the other axis may end up larger. max() of the two
            # ratios does that; min(1.0, ...) refuses to ever upscale, since
            # inventing pixels cannot recover detail and only costs time.
            #
            # On a 16:10 2880x1800 screen with a 1920x1080 floor this gives
            # 1920x1200, not 1728x1080 — the height is allowed to overshoot
            # so the width is not starved. Aspect ratio is untouched either
            # way, because one factor is applied to both axes.
            fw, fh = min_size
            scale = min(1.0, max(fw / box["width"], fh / box["height"]))
        if scale != 1.0:
            # round(), not int(): truncating both axes independently can make
            # the realised ratio differ from the requested one by a pixel,
            # and that pixel is a permanent skew in every coordinate mapped
            # back out.
            img = img.resize((max(1, round(box["width"] * scale)),
                              max(1, round(box["height"] * scale))),
                             Image.LANCZOS)

        frame = Frame(img, box["left"], box["top"], box["width"],
                      box["height"], time.time(), scale)
        if not allow_dark and lit_fraction(img) < LIT_MIN:
            raise DisplayDark(
                f"region {frame.region} captured black — the display may be "
                f"asleep, or the window is minimised or occluded")
        return frame


def active_window_region(pad: int = 0) -> tuple[int, int, int, int] | None:
    """Where the foreground window is. The default answer to "where to look".

    Almost nothing a user cares about is spread over the whole desktop: it is
    in the window they are looking at. Measured on this machine, that alone
    is usually the difference between a 96ms full-screen grab and ~25ms.

    Returns None when there is no usable foreground window (the desktop, a
    zero-size window), so the caller can fall back to the whole screen rather
    than capture nothing.
    """
    import sys
    agent = Path(__file__).resolve().parents[2] / "agent"
    if str(agent) not in sys.path:
        sys.path.insert(0, str(agent))
    try:
        from core import locator
        hwnd = locator.foreground_window()
        if not hwnd:
            return None
        x1, y1, x2, y2 = locator.window_rect(hwnd)
    except Exception:
        return None
    x1, y1 = max(0, x1 - pad), max(0, y1 - pad)
    w, h = (x2 - x1) + 2 * pad, (y2 - y1) + 2 * pad
    if w < 40 or h < 40:
        return None
    return (x1, y1, w, h)


def changed_regions(before: Frame, after: Frame, min_area: int = 400,
                    pad: int = 12) -> list[tuple[int, int, int, int]]:
    """Where the screen changed between two frames, as regions to look at.

    The other answer to "where to look": whatever just moved. After a click,
    the part of the screen that responded is the part worth reading, and a
    frame difference finds it in under a millisecond — against 96ms to grab
    the whole screen and 251ms to read it.

    Both frames must share a region and scale; regions come back in absolute
    screen pixels, padded, so a box that clipped the edge of a control still
    contains it.
    """
    import cv2
    import numpy as np

    a = cv2.cvtColor(np.asarray(before.image), cv2.COLOR_RGB2GRAY)
    b = cv2.cvtColor(np.asarray(after.image), cv2.COLOR_RGB2GRAY)
    if a.shape != b.shape:
        return []
    diff = cv2.absdiff(a, b)
    _, mask = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
    # Join neighbouring specks: a changed word is dozens of disconnected
    # strokes, and one box around the phrase is worth more than sixty around
    # letters.
    mask = cv2.dilate(mask, np.ones((9, 9), np.uint8), iterations=2)
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in cnts:
        if cv2.contourArea(c) < min_area:
            continue
        x, y, w, h = cv2.boundingRect(c)
        left, top = after.to_screen(x - pad, y - pad)
        right, bottom = after.to_screen(x + w + pad, y + h + pad)
        out.append((max(0, left), max(0, top), right - left, bottom - top))
    return sorted(out, key=lambda r: -(r[2] * r[3]))


def lit_fraction(image) -> float:
    """How much of the image is above near-black.

    The mean, not the standard deviation: a capture composites the mouse
    cursor into itself, and those ~90 bright pixels push std over a naive
    threshold on a frame that is otherwise entirely black.
    """
    import numpy as np
    a = np.asarray(image.convert("L"), dtype="uint8")
    return float((a > 10).mean())


def fingerprint(image) -> str:
    """A cheap identity for a picture, for 'has anything changed?'.

    ~1 ms against 305 ms to OCR a full screen. Reduced to 64x64 and 16 grey
    levels so that antialiasing, a caret and a one-pixel scroll all hash the
    same, while a new dialog does not.
    """
    import numpy as np
    small = image.convert("L").resize((FINGERPRINT_SIDE, FINGERPRINT_SIDE))
    a = (np.asarray(small, dtype="uint8") // (256 // FINGERPRINT_LEVELS)).tobytes()
    return hashlib.blake2b(a, digest_size=8).hexdigest()


class ChangeWatch:
    """Remembers the last fingerprint per region, so a caller can skip work.

        watch = ChangeWatch()
        frame = screen.grab(region)
        if watch.changed("results", frame):
            rows = see(frame)           # only now pay for OCR

    Keyed by a caller-chosen name rather than by the region tuple: the same
    logical area often moves by a few pixels between grabs, and a key that
    changes with it would report every look as a change.
    """

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}

    def changed(self, key: str, frame: Frame) -> bool:
        fp = fingerprint(frame.image)
        was = self._seen.get(key)
        self._seen[key] = fp
        return was != fp

    def forget(self, key: str | None = None) -> None:
        if key is None:
            self._seen.clear()
        else:
            self._seen.pop(key, None)
