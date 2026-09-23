"""Finding things by their pixels, for when nothing will name them.

Template matching needs a reference image, and there is no universal library
of them: a close button's pixels depend on the app, the theme, the DPI and
the OS version. So the library is yours, and this module is about filling it
cheaply.

**The trick that makes it cheap.** You do not have to crop icons by hand. UI
Automation already knows where every control is and what it is called — so
while a window is readable, it can be asked to hand over each control's
rectangle, and the pixels inside that rectangle ARE the template. The
accessibility tree teaches the vision layer, once, and afterwards the vision
layer can find the same button on a screen where the tree has gone silent:
a canvas, a remote desktop, a game, or an app that stopped answering.

Measured tolerance, cart icon across three different page states:

    exact, same screenshot       1.000
    different page, same icon    1.000   (27 ms on a 1440x900 screen)
    resized to 95% / 105%        0.948 / 0.940
    resized to 90% / 110%        0.831 / 0.915
    rotated 5 degrees            0.912

So 0.85 is a defensible default threshold: it survives ±10% scaling and a
few degrees of rotation, and still rejects unrelated icons. What it does NOT
survive is a theme change or a redesign — those are pixel changes, and this
is pixel correlation. Re-teach after either.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .capture import Frame

#: Default library location. One PNG per template plus a JSON index, so the
#: library is inspectable with an image viewer and diffable in git.
LIBRARY = Path(__file__).resolve().parent / "icons"

#: Correlation below which a hit is not believed. See the table above.
THRESHOLD = 0.85

#: Greyscale standard deviation below which a crop is refused as a template.
#: See `Icons.add`. An icon has edges; a panel does not.
MIN_DETAIL = 12.0

#: Container types are never icons. Learning them is what produced the
#: featureless crops that matched everywhere.
SKIP_TYPES = ("Pane", "Group", "Document", "Window", "Custom", "Text")


def _detail(image) -> float:
    """Greyscale spread of a crop — how much there is to recognise."""
    import numpy as np
    return float(np.asarray(image.convert("L"), dtype="float32").std())


#: Two matches closer than this are the same control found twice, because
#: `matchTemplate` peaks over a few adjacent pixels. Suppressed by distance
#: rather than by non-maximum suppression over boxes: templates are small and
#: uniform in size, so a radius is enough and is far cheaper.
MIN_SEPARATION = 8


@dataclass(frozen=True)
class Hit:
    """One place a template was found, in absolute screen pixels."""

    name: str
    score: float
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


class Icons:
    """A named library of reference images, on disk."""

    def __init__(self, path: Path | str = LIBRARY) -> None:
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self._index = self.path / "index.json"
        self._meta: dict[str, dict] = {}
        if self._index.exists():
            try:
                self._meta = json.loads(self._index.read_text(encoding="utf-8"))
            except Exception:
                self._meta = {}
        self._cache: dict[str, Any] = {}

    # -- teaching ----------------------------------------------------------

    def add(self, name: str, frame: Frame,
            rect: tuple[int, int, int, int], note: str = "") -> Path:
        """Store the pixels inside `rect` (absolute screen coords) as `name`.

        Rejects anything smaller than 6x6: a template that small matches
        everywhere, and a library full of them is worse than an empty one.
        """
        x1, y1, x2, y2 = rect
        if (x2 - x1) < 6 or (y2 - y1) < 6:
            raise ValueError(f"{name!r}: {x2 - x1}x{y2 - y1} is too small to "
                             f"identify anything")
        crop = frame.image.crop((x1 - frame.left, y1 - frame.top,
                                 x2 - frame.left, y2 - frame.top))
        # A featureless crop matches everywhere. Measured: a plain pane
        # captured by mistake scored 0.99 at twenty-one unrelated places on
        # one screen, and every one of them entered the scene as a clickable
        # "icon". Size was not the problem — the crop was 90x60 — so the
        # guard has to be on CONTENT, not dimensions.
        #
        # Standard deviation of the greyscale is the cheapest sufficient
        # test: an icon has edges and therefore spread, a flat panel has
        # almost none. 12 sits well below any real icon measured here and
        # well above the gradients that caused the false matches.
        spread = _detail(crop)
        if spread < MIN_DETAIL:
            raise ValueError(f"{name!r}: too featureless to identify anything "
                             f"(detail {spread:.1f} < {MIN_DETAIL}) — a flat "
                             f"crop matches every flat region on screen")
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:60]
        out = self.path / f"{safe}.png"
        crop.save(out)
        self._meta[name] = {"file": out.name, "w": crop.width, "h": crop.height,
                            "note": note, "taught": time.time()}
        self._save()
        self._cache.pop(name, None)
        return out

    def learn_window(self, hwnd: int, frame: Frame | None = None,
                     max_icons: int = 40,
                     only_unnamed_text: bool = False) -> list[str]:
        """Capture every control in a window as a template, named by UIA.

        This is the point of the module. While the tree answers, each
        control's rectangle is a free, correctly-cropped, correctly-named
        reference image. Run it once on a healthy window; afterwards the same
        buttons are findable by pixels alone.

        `only_unnamed_text=True` keeps just the controls OCR could never help
        with — the ones whose name is empty or which hold no text — because
        those are exactly the icons worth having templates for.
        """
        import sys
        agent = Path(__file__).resolve().parents[2] / "agent"
        if str(agent) not in sys.path:
            sys.path.insert(0, str(agent))
        from core import locator

        from .capture import Screen
        if frame is None:
            frame = Screen().grab()

        taught = []
        locator.invalidate_cache(hwnd)
        try:
            root = locator.uia_root(hwnd)
        except Exception:
            return taught
        for ch in locator.walk(root, max_depth=30, max_nodes=1500, budget_s=5.0):
            if len(taught) >= max_icons:
                break
            try:
                name = (ch.Name or "").strip()
                ctype = ch.ControlTypeName or ""
                r = locator._rect_of(ch)
            except Exception:
                continue
            if not r:
                continue
            w, h = r[2] - r[0], r[3] - r[1]
            # Icons are small and roughly square; a 900px-wide pane is a
            # container, and storing it would match the whole window.
            if not (8 <= w <= 120 and 8 <= h <= 120):
                continue
            if only_unnamed_text and name:
                continue
            if any(t in ctype for t in SKIP_TYPES):
                continue
            key = f"{ctype}:{name}" if name else f"{ctype}:{r[0]},{r[1]}"
            try:
                self.add(key, frame, r, note=f"auto from hwnd {hwnd}")
                taught.append(key)
            except Exception:
                continue
        return taught

    # -- finding -----------------------------------------------------------

    def _needle(self, name: str):
        import cv2
        import numpy as np
        from PIL import Image

        got = self._cache.get(name)
        if got is None:
            meta = self._meta.get(name)
            if not meta:
                raise KeyError(f"no template named {name!r}")
            img = Image.open(self.path / meta["file"]).convert("RGB")
            got = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
            self._cache[name] = got
        return got

    def find(self, name: str, frame: Frame,
             threshold: float = THRESHOLD) -> Hit | None:
        """Best match for one template, or None."""
        hits = self.find_all(name, frame, threshold, limit=1)
        return hits[0] if hits else None

    def find_all(self, name: str, frame: Frame,
                 threshold: float = THRESHOLD, limit: int = 20) -> list[Hit]:
        """Every place this template appears, best first.

        A toolbar has many identical icons and a results page many identical
        add buttons, so "all" is the common case, not the exception.
        """
        import cv2
        import numpy as np

        needle = self._needle(name)
        hay = cv2.cvtColor(np.array(frame.image), cv2.COLOR_RGB2GRAY)
        if needle.shape[0] > hay.shape[0] or needle.shape[1] > hay.shape[1]:
            return []
        res = cv2.matchTemplate(hay, needle, cv2.TM_CCOEFF_NORMED)
        h, w = needle.shape
        out: list[Hit] = []
        ys, xs = np.where(res >= threshold)
        for score, x, y in sorted(((res[y, x], x, y) for x, y in zip(xs, ys)),
                                  reverse=True):
            if any(abs(x - hit.left + frame.left) < MIN_SEPARATION
                   and abs(y - hit.top + frame.top) < MIN_SEPARATION
                   for hit in out):
                continue
            out.append(Hit(name, float(score), int(x) + frame.left,
                           int(y) + frame.top, w, h))
            if len(out) >= limit:
                break
        return out

    def find_any(self, frame: Frame,
                 threshold: float = THRESHOLD) -> list[Hit]:
        """Sweep the whole library over one frame.

        Linear in library size — about 27 ms per template on a full screen —
        so this is for a dozen templates, not a thousand. Name what you are
        looking for when you know it.
        """
        out = []
        for name in self._meta:
            out.extend(self.find_all(name, frame, threshold, limit=8))
        return sorted(out, key=lambda h: -h.score)

    # -- housekeeping ------------------------------------------------------

    def names(self) -> list[str]:
        return sorted(self._meta)

    def forget(self, name: str) -> bool:
        meta = self._meta.pop(name, None)
        if not meta:
            return False
        try:
            (self.path / meta["file"]).unlink()
        except Exception:
            pass
        self._cache.pop(name, None)
        self._save()
        return True

    def _save(self) -> None:
        self._index.write_text(json.dumps(self._meta, indent=2,
                                          ensure_ascii=False), encoding="utf-8")
