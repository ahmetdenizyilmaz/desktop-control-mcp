"""Reading text off the screen, with coordinates, using the OCR already in Windows.

Measured on this machine, 2880x1800, against the alternatives tried in the
other project:

    Windows.Media.Ocr        305 ms full screen, 22 ms for 800x400
    EasyOCR               13,800 ms                  (~100 MB + torch)
    Florence-2 <OCR>       1,500-17,000 ms, garbled  (1.1 GB)
    OmniParser            84,200 ms, 0.44 hit rate   (1.1 GB)

45x faster than EasyOCR, nothing to download, and no GPU — which matters
here, because torch on this machine is a CPU-only build and the Radeon 8060S
is invisible to it.

**Language is not cosmetic.** The same picture, read twice:

    en:  Arama alam   qé19öü      Bahqelievler   Magazadan
    tr:  Arama alanı  çğışöü      Bahçelievler   Mağazadan

Every Turkish letter is wrong under `en`, and anything matching on exact text
downstream would miss. Turkish is already available on this machine, so `tr`
is the default here and English is the fallback.

What this module does NOT do: decide what anything IS. OCR reads "Sepete
Ekle" and cannot tell you it is a button rather than a heading. That is
`elements.py`'s job, and the reason this package merges OCR with the
accessibility tree instead of choosing between them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .capture import Frame

#: Tried in order until one works. Turkish first: this machine's apps and
#: sites are Turkish, and reading them under `en` corrupts every accented
#: character. A tag with no installed model raises, so the list is a fallback
#: chain rather than a preference.
LANGUAGES = ("tr", "en")

_ENGINE_OK: dict[str, bool] = {}


@dataclass(frozen=True)
class Word:
    """One word, in absolute screen pixels."""

    text: str
    left: int
    top: int
    width: int
    height: int

    @property
    def rect(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.left + self.width,
                self.top + self.height)

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.width // 2, self.top + self.height // 2)


@dataclass(frozen=True)
class Line:
    """A run of words the engine considered one line, in screen pixels."""

    text: str
    words: tuple[Word, ...]

    @property
    def rect(self) -> tuple[int, int, int, int]:
        xs1 = min(w.left for w in self.words)
        ys1 = min(w.top for w in self.words)
        xs2 = max(w.left + w.width for w in self.words)
        ys2 = max(w.top + w.height for w in self.words)
        return (xs1, ys1, xs2, ys2)

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.rect
        return ((x1 + x2) // 2, (y1 + y2) // 2)


@dataclass
class Reading:
    """Everything one OCR pass found, plus what it cost."""

    lines: tuple[Line, ...]
    language: str
    ms: float
    region: tuple[int, int, int, int]

    @property
    def text(self) -> str:
        return "\n".join(l.text for l in self.lines)

    @property
    def words(self) -> tuple[Word, ...]:
        return tuple(w for l in self.lines for w in l.words)

    def find(self, needle: str, whole_line: bool = True) -> list[Line | Word]:
        """Lines (or words) whose text contains `needle`, case-insensitively.

        Turkish case is not Python's case: "İ".lower() is not "i", and
        "I".lower() is not "ı". `fold` handles the two pairs that actually
        differ so that searching for "ödeme" finds "ÖDEME".
        """
        n = fold(needle)
        if whole_line:
            return [l for l in self.lines if n in fold(l.text)]
        return [w for w in self.words if n in fold(w.text)]


def fold(s: str) -> str:
    """Casefold, with the two Turkish letters that do not round-trip."""
    return (s.replace("İ", "i").replace("I", "ı")
             .replace("Ş", "ş").replace("Ğ", "ğ").casefold())


#: One worker thread, never the caller's.
#:
#: WinRT's blocking `.get()` refuses to run in a COM single-threaded
#: apartment — "Cannot call blocking method from single-threaded apartment".
#: Anything that has touched UI Automation is in exactly that state, because
#: `uiautomation` initialises STA on import. Since this package exists to be
#: merged WITH the accessibility tree, the two would collide on their first
#: call together.
#:
#: The worker is created lazily, initialises COM as multi-threaded for
#: itself, and is reused, so the apartment cost is paid once rather than per
#: OCR pass.
_POOL = None
_POOL_LOCK = __import__("threading").Lock()


def _mta_init() -> None:
    """Put this worker thread in the multi-threaded apartment."""
    try:
        import ctypes
        COINIT_MULTITHREADED = 0x0
        ctypes.windll.ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
    except Exception:
        pass


def _recognise(image, tag: str):
    """Run Windows OCR off the caller's thread, and wait for the result."""
    global _POOL
    import winocr

    def work():
        op = winocr.recognize_pil(image, tag)
        return op.get() if hasattr(op, "get") else op

    with _POOL_LOCK:
        if _POOL is None:
            from concurrent.futures import ThreadPoolExecutor
            _POOL = ThreadPoolExecutor(max_workers=1, initializer=_mta_init,
                                       thread_name_prefix="winocr")
    return _POOL.submit(work).result(timeout=30)


def _is_missing_model(e: Exception) -> bool:
    """Tell 'no model for this language' from a real fault.

    Without this a transport or apartment failure would be recorded as a
    missing language, permanently disabling a tag that works.
    """
    text = f"{type(e).__name__}: {e}".lower()
    return ("no matching" in text or "not supported" in text
            or "0x80070032" in text or "language" in text
            or isinstance(e, (ValueError, OSError)))


def read(frame: Frame, language: str | None = None) -> Reading:
    """OCR one captured frame. Coordinates come back ABSOLUTE.

    The engine reports boxes relative to the image it was handed, so a caller
    that cropped to a region would otherwise have to add the offset back
    itself — and the one that forgets clicks somewhere else entirely.
    """
    tags = (language,) if language else LANGUAGES
    last: Exception | None = None
    for tag in tags:
        if _ENGINE_OK.get(tag) is False:
            continue
        t0 = time.perf_counter()
        try:
            result = _recognise(frame.image, tag)
        except Exception as e:                    # no model for this language
            if not _is_missing_model(e):
                raise
            _ENGINE_OK[tag] = False
            last = e
            continue
        _ENGINE_OK[tag] = True
        ms = (time.perf_counter() - t0) * 1000

        lines = []
        for l in result.lines:
            words = []
            for w in l.words:
                b = w.bounding_rect
                x, y = frame.to_screen(b.x, b.y)
                words.append(Word(w.text, x, y, int(b.width), int(b.height)))
            if words:
                lines.append(Line(l.text, tuple(words)))
        return Reading(tuple(lines), tag, ms, frame.region)

    raise RuntimeError(f"no Windows OCR model for {tags}: {last}")


def prewarm(size: tuple[int, int] = (2880, 1800),
            language: str | None = None) -> float:
    """Pay the engine's first-call cost now, not inside the first real look.

    Measured cold vs warm, full screen: ocr 208 ms then 154 ms. The ~54 ms
    is the MTA worker starting and the WinRT engine loading its language
    model.

    `size` matters, and a first attempt at this got it wrong: a 64x64 probe
    returned in 64 ms and absorbed almost none of the penalty, because the
    engine sizes its working buffers to the image it is handed. Warm with a
    frame the size of the ones that will follow — the screen, by default.

    Returns seconds taken, or 0.0 if the engine could not be reached; a
    failure here is not worth raising for, since the next real read will
    report it properly.
    """
    from PIL import Image

    w, h = size
    probe = Frame(Image.new("RGB", (max(64, w), max(64, h)), (255, 255, 255)),
                  0, 0, max(64, w), max(64, h), time.time())
    t0 = time.perf_counter()
    try:
        read(probe, language=language)
    except Exception:
        return 0.0
    return time.perf_counter() - t0


def languages() -> list[str]:
    """Which of LANGUAGES this machine can actually read."""
    from PIL import Image

    from .capture import Frame as F
    probe = F(Image.new("RGB", (32, 32), (255, 255, 255)), 0, 0, 32, 32, 0.0)
    out = []
    for tag in LANGUAGES:
        try:
            read(probe, language=tag)
            out.append(tag)
        except Exception:
            pass
    return out
