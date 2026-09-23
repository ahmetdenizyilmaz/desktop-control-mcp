"""Choosing between candidates that look identical but mean different things.

A search results grid offers twenty buttons all named "Ürünü sepete ekle".
Every selector signal this package has — `kind`, `near`, `nth`, `area` —
describes where a thing IS, and on that grid they are all equally right.
What distinguishes them is what each button BELONGS to, which is written a
few pixels above it.

Measured on three real Migros searches, taking the first result bought:

    süt     ->  Sütaş LATTE 1 L                     scored 0.000
    peynir  ->  İçim LABNE 180 G                    scored 0.000
    ekmek   ->  Ekşi Mayalı ve Tahıllı BAGET 290 G  scored 0.000

Three for three, the thing position picked was the thing meaning rejected.

So the candidates are labelled with their nearest title and handed to a
decision model, which is what a System-1 model is actually good at: a
bounded set, one question, no generation. ~400 ms warm, $0.000035 a call.

**The gate is MARGIN, not confidence**, and that is not a detail. The
correct "peynir" answer came back at confidence 0.61 — under the 0.80
floor used elsewhere in this project — with a margin of 0.44 over the
runner-up. When options are near-duplicates the probability mass spreads
across all the plausible ones and confidence falls; the GAP is what says
"this one and not that one". Gating on confidence would have rejected a
correct answer and fallen back to buying labne.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from vision.elements import Element, Scene

#: What a chooser must return: the winning key, and the margin over the
#: runner-up so the caller can decide whether to believe it.
Chooser = Callable[[str, str, dict[str, str]], tuple[str, float]]


class NoChoice(Exception):
    """The chooser could not decide, or is not available."""


@dataclass(frozen=True)
class Labelled:
    key: str
    element: Element
    label: str


def label_candidates(scene: Scene, candidates: list[Element],
                     radius: int = 280) -> list[Labelled]:
    """Give each candidate the text of the thing it belongs to.

    Looks for the nearest element that carries real text, preferring one
    ABOVE the candidate, because that is where a card puts its title. A
    candidate whose label cannot be found still gets a slot — dropping it
    would quietly remove an option the model should have seen.
    """
    texts = [e for e in scene.elements
             if len(e.name.strip()) > 8 and e is not None]
    out: list[Labelled] = []
    for i, c in enumerate(candidates):
        cx, cy = c.center
        best, best_d = "", float("inf")
        for t in texts:
            if t is c:
                continue
            tx, ty = t.center
            if abs(tx - cx) > radius:
                continue
            dy = cy - ty
            if not -radius <= dy <= radius:
                continue
            # Above is worth more than below: a product grid puts the
            # title over the button and the NEXT card's title under it.
            d = abs(dy) * (1.0 if dy > 0 else 1.8) + abs(tx - cx) * 0.5
            if t.source == "ocr":
                # Pixels render what the card had room for - "Sofra E..." -
                # while the tree carries the full title. Same card, so let
                # the full one win unless the OCR line is much closer.
                d *= 1.6
            if d < best_d:
                best, best_d = t.name.strip(), d
        best = _untruncate(best, texts)
        out.append(Labelled(f"c{i}", c, best or f"(option {i + 1})"))
    return out


def _untruncate(label: str, texts: list[Element]) -> str:
    """"Sofra E..." -> "Sofra Ekmek Adet", when the page holds the full one.

    A truncated label is what gets written into the WhatsApp message and
    the report, and "Sofra E..." tells nobody what was bought.
    """
    stem = label.rstrip(".… ").strip()
    if not stem or stem == label.strip():
        return label
    fuller = [t.name.strip() for t in texts
              if t.name.strip().startswith(stem) and len(t.name) > len(stem)]
    return max(fuller, key=len) if fuller else label


def jev_chooser(backend: str = "openrouter") -> Chooser:
    """A chooser backed by the System-1 model. Built lazily, shared.

    Refuses rather than guessing when there is no key: the offline stub
    answers with a near-uniform distribution, which would look like a real
    decision with a tiny margin and silently fall back — a wrong answer
    dressed as a considered one.
    """
    state: dict = {}

    def choose(goal: str, reject: str, options: dict[str, str]
               ) -> tuple[str, float]:
        client = state.get("client")
        if client is None:
            import sys
            from pathlib import Path
            agent = Path(__file__).resolve().parents[2] / "agent"
            if str(agent) not in sys.path:
                sys.path.insert(0, str(agent))
            from core import jev
            client = jev.Client(backend=backend)
            if client.offline:
                raise NoChoice(
                    f"no {backend} key, so the decision model would answer "
                    f"from its offline stub — refusing rather than returning "
                    f"a made-up ranking")
            state["client"], state["jev"] = client, jev
        jev = state["jev"]

        want = goal
        if reject:
            want += f" Reject: {reject}."
        question = {"best": jev.Choice(
            instructions=(f"Which ONE of these is the right one to act on? "
                          f"{want}"),
            criteria=options)}
        got = client.ask({"goal": goal, "reject": reject,
                          "options": options}, question)["best"]
        return got.choice, got.margin

    return choose
