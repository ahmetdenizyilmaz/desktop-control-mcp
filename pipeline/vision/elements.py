"""One list of what is on screen, from three sources that each see differently.

None of the three is sufficient alone, and the ways they fail do not overlap:

  UIA        knows what is CLICKABLE, and its name and kind, exactly — but
             sees nothing on a canvas, in a game, in a remote desktop, or in
             an app that has stopped answering. Measured: 119 ms for a
             window, 100% recall where it works.
  OCR        reads any text at all, including text drawn into a picture —
             but cannot tell a button from a photo caption, and is blind to
             anything without text. Measured: ~214 ms full screen.
  templates  find icons that have no text, if you taught them first.
             Measured: 1.000 correlation across page states, ~27 ms each.

So: UIA is authoritative where it answers, OCR fills the silence, templates
cover what neither can name. The merge is mostly about NOT double-counting —
a button labelled "Sepete Ekle" is one element that UIA and OCR both saw,
not two.

Cost: UIA and OCR run CONCURRENTLY, which measured 209 ms against 384 ms
sequential — 46% saved, because OCR sits on a multi-threaded-apartment
worker while UIA needs the single-threaded apartment, so they never contend.
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .capture import Frame, Screen
from . import ocr as ocr_mod

_AGENT = Path(__file__).resolve().parents[2] / "agent"
if str(_AGENT) not in sys.path:
    sys.path.insert(0, str(_AGENT))

#: UIA control types worth listing, and the plain word used for each. Text,
#: images and groups are deliberately absent: they are most of a tree's nodes
#: and none of its choices.
KINDS = {
    "HyperlinkControl": "link", "ButtonControl": "button",
    "SplitButtonControl": "button", "EditControl": "edit",
    "ComboBoxControl": "combobox", "CheckBoxControl": "checkbox",
    "RadioButtonControl": "radio", "MenuItemControl": "menuitem",
    "TabItemControl": "tab", "ListItemControl": "listitem",
    "SliderControl": "slider", "TreeItemControl": "treeitem",
}

#: An OCR line whose centre falls inside a control this much bigger than the
#: text is that control's LABEL, not a separate thing. Generous, because a
#: button is always larger than the words on it; too generous and a whole
#: pane would swallow every line inside it, which is why containers are
#: excluded by size before this is applied.
LABEL_MAX_AREA_RATIO = 40.0


@dataclass
class Element:
    """One thing on screen, however it was found."""

    kind: str                      # button, link, edit, text, icon, …
    name: str
    left: int
    top: int
    width: int
    height: int
    source: str                    # uia | ocr | template | uia+ocr
    clickable: bool
    value: str = ""
    automation_id: str = ""
    confidence: float = 1.0
    #: The live UIA handle when there is one — the actuator can Invoke it
    #: without touching the cursor, which a coordinate click cannot.
    control: Any = field(default=None, repr=False)

    @property
    def rect(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.left + self.width,
                self.top + self.height)

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.width // 2, self.top + self.height // 2)

    def contains(self, x: float, y: float) -> bool:
        return (self.left <= x <= self.left + self.width
                and self.top <= y <= self.top + self.height)

    @property
    def area(self) -> int:
        return max(1, self.width * self.height)


@dataclass
class Scene:
    """Everything found in one look, and what it cost."""

    elements: list[Element]
    frame: Frame
    ms: float
    counts: dict[str, int] = field(default_factory=dict)
    #: The document rectangle, when the window has one — the page in a
    #: browser, the sheet in an editor. None when the app has no document.
    #:
    #: Worth carrying because a window's own frame is in the tree too, and
    #: it is full of traps. On a Migros page Chrome offered 270 elements,
    #: of which the first 44 were its own: tab strip, toolbar, bookmarks —
    #: including TWO buttons named "Kapat", one closing the tab and one
    #: closing the browser. A selector aimed at the page that matched
    #: either of those would end the run by doing exactly what it was told.
    content: tuple[int, int, int, int] | None = None

    def clickable(self) -> list[Element]:
        return [e for e in self.elements if e.clickable]

    def find(self, needle: str, clickable_only: bool = False) -> list[Element]:
        n = ocr_mod.fold(needle)
        return [e for e in self.elements
                if n in ocr_mod.fold(e.name)
                and (e.clickable or not clickable_only)]

    def at(self, x: float, y: float) -> Element | None:
        """The smallest element covering a point — the one a click would hit."""
        hits = [e for e in self.elements if e.contains(x, y)]
        return min(hits, key=lambda e: e.area) if hits else None

    def rows(self, cap: int = 60) -> list[dict]:
        """The row shape the decision layer eats, in reading order.

        Deliberately the same keys `fast.rows_to_state` expects, so a scene
        can be handed straight to a decision model without a translation
        step — and so `kind` carries what OCR alone could never say.
        """
        ordered = sorted(self.elements, key=lambda e: (e.top // 12, e.left))
        return [{"type": e.kind, "name": e.name[:80], "id": e.automation_id,
                 "value": e.value, "hint": "" if e.clickable else "text only"}
                for e in ordered[:cap]]


class Watcher:
    """Looks once, then reuses the answer until the screen says otherwise.

    A `look()` costs 200-590 ms and most of the time nothing has moved, so
    paying it per question is waste. But a cached coordinate is a liability:
    the window scrolled, the dialog closed, and the click lands on whatever
    took that pixel. So the cache is never trusted on age — it is trusted on
    evidence, and there are two grades of it:

      `scene()`        5 ms fingerprint of the whole screen. Unchanged means
                       every coordinate is still good; changed means look
                       again. This is the cheap, total check.

      `recall(name)`   ~10 ms. Re-reads ONLY the small rectangle where the
                       element was last seen and confirms the same text is
                       still there. Survives changes elsewhere on screen —
                       a clock ticking in the corner invalidates the whole
                       scene but not the button you care about.

    Measured: a verified recall is roughly 20x cheaper than a fresh look,
    and a fingerprint-clean scene() is roughly 100x cheaper.
    """

    def __init__(self, screen: Screen | None = None, icons=None,
                 hwnd: int | None = None) -> None:
        self.screen = screen or Screen()
        self.icons = icons
        self.hwnd = hwnd
        self._scene: Scene | None = None
        self._fp: str = ""
        self.looks = 0
        self.reuses = 0
        self.pixel_looks = 0
        #: Last known document rectangle. Kept on the Watcher rather than
        #: only on the Scene because the PIXEL-ONLY path cannot compute it
        #: - there is no tree to ask - and without it a text selector
        #: matches the browser's own tab title. "süt - Migros" in the tab
        #: strip would satisfy a wait for "süt" before the results had
        #: even begun to load.
        self.content: tuple[int, int, int, int] | None = None

    def scene(self, force: bool = False) -> Scene:
        """The current scene, looked up again only if the screen moved."""
        from .capture import fingerprint

        frame = self.screen.grab()
        fp = fingerprint(frame.image)
        if not force and self._scene is not None and fp == self._fp:
            self.reuses += 1
            return self._scene
        self._scene = look(hwnd=self.hwnd, screen=self.screen,
                           icons=self.icons, frame=frame)
        if self._scene.content:
            self.content = self._scene.content
        self._fp = fp
        self.looks += 1
        return self._scene

    def text_scene(self) -> Scene:
        """A scene from PIXELS ONLY - no accessibility tree.

        For a selector that names nothing but text there is nothing the
        tree can add that OCR does not already see, and on a live page the
        tree costs 879 ms against OCR's 381 ms. The document rectangle is
        carried over from the last full look, because a text match that
        can stray into the browser's tab strip is worse than useless.
        """
        sc = look(hwnd=self.hwnd, screen=self.screen, icons=self.icons,
                  uia=False)
        sc.content = self.content
        self.pixel_looks += 1
        return sc

    def recall(self, name: str, pad: int = 8) -> Element | None:
        """An element by name, verified in place rather than re-found.

        Checks the remembered rectangle first: if the same text is still
        there, the cached coordinates are returned without a full look. Only
        when that fails does it pay for one.

        Returns None when the element cannot be found even after looking —
        which is a real answer, not a failure: it usually means the thing
        is gone, and a caller that clicked the stale coordinates anyway
        would be clicking whatever replaced it.
        """
        cached = None
        if self._scene is not None:
            hits = self._scene.find(name)
            cached = hits[0] if hits else None

        if cached is not None and self._still_there(cached):
            self.reuses += 1
            return cached

        sc = self.scene(force=True)
        hits = sc.find(name)
        return hits[0] if hits else None

    def _still_there(self, e: Element, pad: int = 8) -> bool:
        """Is this element's own text still in its own rectangle?

        A UIA element is checked through UIA, which is both cheaper and
        stronger than pixels: the handle either still resolves to a control
        with that name, or the control is gone. Only a pixel-sourced element
        needs a crop and a re-read.
        """
        if e.control is not None:
            try:
                from core import locator
                cur = (e.control.Name or "").strip()
                r = locator._rect_of(e.control)
            except Exception:
                return False
            return bool(r) and cur == e.name and abs(r[0] - e.left) < 4
        try:
            region = (max(0, e.left - pad), max(0, e.top - pad),
                      e.width + 2 * pad, e.height + 2 * pad)
            frame = self.screen.grab(region, allow_dark=True)
            reading = ocr_mod.read(frame)
        except Exception:
            return False
        want = ocr_mod.fold(e.name)
        return any(want in ocr_mod.fold(l.text) for l in reading.lines)

    def stats(self) -> str:
        total = self.looks + self.reuses
        pct = (self.reuses / total * 100) if total else 0
        return (f"{self.looks} full looks, {self.reuses} reused "
                f"({pct:.0f}% avoided)")


def _uia_elements(hwnd: int, max_nodes: int = 1500,
                  content: list | None = None) -> list[Element]:
    """Every named, sized control in a window, depth-first (reading order).

    `content`, when a list is passed, receives the window's document
    rectangle if it has one. See `Scene.content` for why that matters.
    """
    from core import locator

    out: list[Element] = []
    try:
        locator.invalidate_cache(hwnd)
        root = locator.uia_root(hwnd)
    except Exception:
        return out

    stack, seen = [root], 0
    while stack and seen < max_nodes:
        c = stack.pop()
        seen += 1
        try:
            kids = c.GetChildren()
        except Exception:
            kids = []
        for k in reversed(kids):
            stack.append(k)
        # Order matters, and it is worth 68 ms on a 346-node tree. Every
        # property read is a COM round trip, and only ~15% of nodes survive
        # the kind filter — so ask the ONE question that rejects most of
        # them first, and read nothing else until it passes.
        try:
            type_name = c.ControlTypeName
            kind = KINDS.get(type_name)
        except Exception:
            continue
        if content is not None and type_name == "DocumentControl":
            # Free, because we are already standing on this node. A
            # document is where an app's CONTENT lives, so its rectangle is
            # the line between the page and the application's own frame.
            try:
                r = locator._rect_of(c)
                area = (r[2] - r[0]) * (r[3] - r[1]) if r else 0
                if area > 40000 and (not content or area > content[0][4]):
                    # Largest wins: Chrome exposes several documents, and
                    # the small ones are things like the omnibox dropdown.
                    content[:] = [(r[0], r[1], r[2], r[3], area)]
            except Exception:
                pass
        if not kind:
            continue
        try:
            name = (c.Name or "").strip()
            r = locator._rect_of(c)
        except Exception:
            continue
        if not r:
            continue
        value = ""
        if kind in ("edit", "combobox"):
            try:
                if getattr(c, "IsPassword", False):
                    value = ""
                else:
                    value = (c.GetValuePattern().Value or "")[:40]
            except Exception:
                value = ""
        try:
            aid = (c.AutomationId or "")[:40]
        except Exception:
            aid = ""
        out.append(Element(kind, name, r[0], r[1], r[2] - r[0], r[3] - r[1],
                           "uia", True, value, aid, 1.0, c))
    return out


def look(hwnd: int | None = None, screen: Screen | None = None,
         icons=None, frame: Frame | None = None,
         concurrent: bool = True, uia_floor: int = 5,
         sweep_icons: bool = False, uia: bool = True) -> Scene:
    """Look once, from every source, and return one merged list.

    `hwnd=None` uses the foreground window for the UIA half while OCR still
    reads the whole screen — which is usually right: the user's attention is
    in one window, but a dialog or a toast from another may be what matters.

    `uia_floor` is how many clickable controls the tree must produce before
    the icon library is considered unnecessary. Below it, the window counts
    as blind and the templates are swept. `sweep_icons=True` forces the
    sweep regardless, for when you want the enumeration anyway.
    """
    from core import locator

    t0 = time.perf_counter()
    screen = screen or Screen()
    if hwnd is None:
        hwnd = locator.foreground_window()

    got: dict[str, Any] = {}

    def read_pixels():
        f = frame if frame is not None else screen.grab()
        got["frame"] = f
        try:
            got["ocr"] = ocr_mod.read(f)
        except Exception:
            got["ocr"] = None

    # UIA on this thread (it needs the apartment it is already in), pixels on
    # another. Measured 209 ms against 384 ms sequential.
    content: list = []
    # `uia=False` answers from pixels alone. For a selector that names only
    # TEXT there is nothing the accessibility tree can add that OCR does
    # not already see, and the tree costs 879 ms against OCR's 381 ms on
    # the same page - so asking for it is pure loss.
    if not uia:
        read_pixels()
        uia = []
    elif concurrent:
        th = threading.Thread(target=read_pixels, name="vision-pixels")
        th.start()
        uia = _uia_elements(hwnd, content=content) if hwnd else []
        th.join()
    else:
        read_pixels()
        uia = _uia_elements(hwnd, content=content) if hwnd else []

    f = got.get("frame") or screen.grab()
    reading = got.get("ocr")

    elements = list(uia)
    counts = {"uia": len(uia), "ocr_label": 0, "ocr_text": 0, "icon": 0,
              "icons_swept": 0}

    # Controls small enough to be a label's owner. A full-window pane would
    # otherwise claim every line on screen as its own text.
    labelled = [e for e in uia if e.area < 400_000]

    if reading is not None:
        for line in reading.lines:
            cx, cy = line.center
            owner = None
            best = None
            for e in labelled:
                if not e.contains(cx, cy):
                    continue
                if e.area / max(1, (line.rect[2] - line.rect[0])
                                * (line.rect[3] - line.rect[1])) > LABEL_MAX_AREA_RATIO:
                    continue
                if best is None or e.area < best:
                    owner, best = e, e.area
            if owner is not None:
                # UIA already had it. Only enrich: an unnamed icon button that
                # OCR can read is worth naming, but a control's own name is
                # authoritative and is never overwritten.
                if not owner.name:
                    owner.name = line.text
                    owner.source = "uia+ocr"
                elif owner.source == "uia":
                    owner.source = "uia+ocr"
                counts["ocr_label"] += 1
            else:
                x1, y1, x2, y2 = line.rect
                elements.append(Element("text", line.text, x1, y1, x2 - x1,
                                        y2 - y1, "ocr", False))
                counts["ocr_text"] += 1

    # Sweep the icon library only when nothing else could name things.
    #
    # The sweep is the expensive branch — linear in library size, ~82 ms per
    # template, so a dozen cost a second and fifty would cost four. And on a
    # healthy window it is redundant by construction: the templates were
    # TAUGHT from UIA, so every hit describes a control UIA has already
    # listed, and the de-duplication below throws nearly all of them away.
    # Measured: 219 ms without icons, 1144 ms with, for four extra rows.
    #
    # Icons earn their cost exactly when the tree goes silent — a canvas, a
    # game, a remote desktop, an app that stopped answering. That is
    # detectable here for free: count what UIA produced.
    blind = len([e for e in elements if e.clickable]) < uia_floor
    if icons is not None and (blind or sweep_icons):
        try:
            for hit in icons.find_any(f):
                if any(e.contains(*hit.center) and e.clickable
                       for e in elements):
                    continue           # UIA already named this one
                elements.append(Element("icon", hit.name, hit.left, hit.top,
                                        hit.width, hit.height, "template",
                                        True, confidence=hit.score))
                counts["icon"] += 1
            counts["icons_swept"] = 1
        except Exception:
            pass

    return Scene(elements, f, (time.perf_counter() - t0) * 1000, counts,
                 content=tuple(content[0][:4]) if content else None)
