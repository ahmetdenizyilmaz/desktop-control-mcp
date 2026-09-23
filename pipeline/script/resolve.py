"""Turning a selector into one element on screen.

This is where the format earns its keep or doesn't. A script says
`{"text": "Ara", "kind": "button"}` and something has to decide which of
the forty things on screen that is — and, more importantly, when to admit
it does not know.

**Why a bare word is not enough**, in the exact case that prompted this.
On a Turkish shopping page, `"ara"` as a substring matches:

    Ara                  the search button          ← wanted
    Arama alanı          the search field's label
    Kategori arama       a filter
    Karar ver            no, but `fold()` makes it
    Sepete Ekle · Ara…   a truncated tooltip

Five candidates, one intent. Three signals fix it, and none of them need
the script to be clever:

  `kind`   the merged scene knows a button from a label, because UIA says
           so. OCR alone never could, and this is the single cheapest
           disambiguator there is — free, and it removes four of the five.
  `near`   a neighbour's text. Survives reflow, which an ordinal does not.
  `nth`    reading order, 1-based. Last resort, and it says so.

**Multi-word selectors work by construction.** `elements.py` binds OCR
lines to the controls that contain them, so "Sepete Ekle" is one element's
name rather than two loose words that happen to sit near each other. The
only care needed here is whitespace: OCR returns "Sepete  Ekle" often
enough that comparing raw strings would fail roughly one time in ten.

**Ambiguity is reported, not guessed.** `find()` returns every candidate,
ranked, and the runner decides whether a second candidate close behind the
first is worth stopping for. A resolver that silently picks the first of
five is how you click the wrong button and never learn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from vision import ocr as ocr_mod
from vision.elements import Element, Scene
from .schema import Selector

#: Runs of whitespace collapse before comparison. OCR splits on layout, not
#: on words, so "Sepete  Ekle" and "Sepete Ekle" are the same button.
_SPACE = re.compile(r"\s+")

#: Two candidates whose scores are within this of each other are genuinely
#: ambiguous. Chosen by hand and worth revisiting with data: the failure it
#: prevents (clicking the wrong one of two near-identical buttons) is much
#: more expensive than the one it causes (stopping to ask).
AMBIGUOUS = 0.08

#: Elements whose name is this much longer than the needle are matching it
#: only incidentally — "ara" inside "Karar verme aracı". They stay as
#: candidates but rank below anything tighter.
LOOSE_RATIO = 4.0


#: Turkish circumflex, which is optional orthography. "kâğıt" and "kağıt"
#: are the same word, and a shopping site picks whichever it likes — a live
#: run lost `kâğıt havlu` to exactly this while the other nine went in.
#:
#: Deliberately ONLY these three. The tempting general fix is to strip all
#: combining marks, which would also fold ç→c, ş→s, ğ→g, ö→o, ü→u — and
#: those are distinct letters in Turkish, not decorations, so that would
#: turn a precise match into a sloppy one across the whole language.
_HAT = str.maketrans("âîûÂÎÛ", "aiuAIU")


def norm(s: str) -> str:
    return _SPACE.sub(" ", ocr_mod.fold(s.translate(_HAT))).strip()


@dataclass
class Match:
    """One candidate, with why it scored what it did."""

    element: Element
    score: float
    why: str


@dataclass
class Resolution:
    """The answer, including the near-misses that make it doubtful."""

    matches: list[Match]
    selector: Selector

    @property
    def best(self) -> Element | None:
        return self.matches[0].element if self.matches else None

    @property
    def ambiguous(self) -> bool:
        """Is the runner-up close enough that picking is a coin toss?"""
        return (len(self.matches) > 1
                and self.matches[0].score - self.matches[1].score < AMBIGUOUS)

    def explain(self) -> str:
        if not self.matches:
            return f"nothing matched {self.selector.describe()}"
        lines = [f"{m.element.kind} {m.element.name!r} at {m.element.center} "
                 f"({m.score:.2f}, {m.why})" for m in self.matches[:4]]
        return "; ".join(lines)


def _text_score(needle: str, name: str, match: str) -> tuple[float, str]:
    """How well one element's name answers one piece of selector text.

    Returns 0.0 for no match at all. Everything above that is a preference
    ordering, not a probability — it only has to be consistent.
    """
    n, h = norm(needle), norm(name)
    if not n:
        return 0.0, ""
    if not h:
        return 0.0, ""
    if n == h:
        return 1.0, "exact"
    if match == "exact":
        return 0.0, ""
    if h.startswith(n) or h.endswith(n):
        base, why = 0.85, "prefix/suffix"
    elif f" {n} " in f" {h} ":
        # Whole words inside a longer name: "Ara" in "Hızlı Ara Butonu".
        base, why = 0.80, "whole word"
    elif n in h:
        base, why = 0.60, "substring"
    else:
        return 0.0, ""
    # A name four times the length of the needle is probably a different
    # thing that happens to contain it. Taper rather than reject, so a
    # long-but-correct label is still reachable when nothing tighter exists.
    ratio = len(h) / max(1, len(n))
    if ratio > LOOSE_RATIO:
        base *= LOOSE_RATIO / ratio
        why += ", loose"
    return base, why


def _distance(a: Element, b: Element) -> float:
    ax, ay = a.center
    bx, by = b.center
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


def find(scene: Scene, sel: Selector,
         want_clickable: bool = False) -> Resolution:
    """Every element this selector could mean, best first."""
    pool = scene.elements

    if sel.area == "content":
        # Everything whose CENTRE is inside the document. Centre rather
        # than full containment, because a wide banner inside the page can
        # extend past the document's own edges by a pixel or two.
        box = scene.content
        if box:
            # NO lower bound on y, and that is the whole subtlety. The
            # document rectangle is the VIEWPORT, but UIA reports content
            # that is scrolled out of sight at its virtual coordinate -
            # measured on one results page, 5 of 7 quantity steppers and
            # 16 of 26 add buttons sat below the fold. Bounding the bottom
            # silently discarded them, so a post-condition that was true
            # read as false and a product that WAS added reported failure.
            #
            # What the filter is actually for is excluding the browser's
            # own frame, and that sits ABOVE the document. `look()` walks
            # a single window, so nothing from another application can
            # arrive here to be admitted by the missing bound.
            pool = [e for e in pool
                    if box[0] <= e.center[0] <= box[2]
                    and e.center[1] >= box[1]]
        # No document means the window has no content area to speak of —
        # a dialog, a plain Win32 app. Restricting to nothing would turn a
        # working selector into a silent miss, so the whole window stands.

    if sel.kind:
        want = sel.kind.lower()
        pool = [e for e in pool if e.kind == want]
        if not pool:
            return Resolution([], sel)

    if sel.icon:
        needle = norm(sel.icon)
        pool = [e for e in pool
                if e.source == "template" and needle in norm(e.name)]

    if sel.id:
        # An automation id is a HANDLE, not a description: on a web page it
        # is the DOM `id`, which the page author chose and the page author
        # keeps. So it filters rather than scores, and an exact hit wins
        # outright — there is nothing a name could add to it.
        want = norm(sel.id)
        exact = [e for e in pool if norm(e.automation_id) == want]
        pool = exact or [e for e in pool
                         if want and want in norm(e.automation_id)]
        if not pool:
            return Resolution([], sel)
        if exact and not (sel.text or sel.any):
            return Resolution(
                [Match(e, 1.0, f"id={e.automation_id!r}") for e in exact], sel)

    matches: list[Match] = []
    needles = [t for t in (sel.text, *sel.any) if t]
    if not needles and sel.id:
        return Resolution(
            [Match(e, 0.9, f"id~{e.automation_id!r}") for e in pool], sel)
    for e in pool:
        if sel.icon and not needles:
            # An icon selector with no text: the template name already
            # identified it, and the score is the match confidence.
            matches.append(Match(e, min(1.0, e.confidence), "icon"))
            continue
        if not needles:
            # kind alone — legitimate for "the only edit box on screen",
            # and scored low so anything with text outranks it.
            matches.append(Match(e, 0.4, f"kind={sel.kind}"))
            continue
        best, why = 0.0, ""
        for t in needles:
            s, w = _text_score(t, e.name, sel.match)
            # `value` matters for a filled field: a script looking for the
            # text it just typed should find the box holding it.
            if s == 0.0 and e.value:
                s, w = _text_score(t, e.value, sel.match)
                if s:
                    s *= 0.9
                    w += ", by value"
            if s > best:
                best, why = s, w
        if best > 0.0:
            matches.append(Match(e, best, why))

    if not matches:
        return Resolution([], sel)

    if want_clickable:
        # A prompt to click plain text usually means the control behind it,
        # so clickables are preferred rather than required — the merge
        # already folds a label into its button when it can.
        for m in matches:
            if not m.element.clickable:
                m.score *= 0.7
                m.why += ", not clickable"

    if sel.near:
        anchors = [e for e in scene.elements if norm(sel.near) in norm(e.name)]
        if anchors:
            # Nearest anchor, and a hard cut at 400px: "near" that matches
            # something on the far side of the screen is not near.
            for m in matches:
                d = min(_distance(m.element, a) for a in anchors)
                m.score *= max(0.1, 1.0 - min(d, 400.0) / 400.0)
                m.why += f", {int(d)}px from {sel.near!r}"
        else:
            for m in matches:
                m.score *= 0.5
                m.why += f", no {sel.near!r} on screen"

    # Reading order, then score — so `nth` means what a person reading the
    # screen would mean, and ties break top-left first.
    matches.sort(key=lambda m: (m.element.top // 12, m.element.left))
    matches.sort(key=lambda m: -m.score)

    if sel.nth:
        # `nth` selects rather than ranks: among everything that matched,
        # take the Nth in READING order, not the Nth best.
        ordered = sorted(matches,
                         key=lambda m: (m.element.top // 12, m.element.left))
        if sel.nth > len(ordered):
            return Resolution([], sel)
        chosen = ordered[sel.nth - 1]
        chosen.why += f", #{sel.nth} of {len(ordered)}"
        return Resolution([chosen], sel)

    return Resolution(matches, sel)
