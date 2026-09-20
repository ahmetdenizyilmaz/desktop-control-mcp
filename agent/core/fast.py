"""System-1 decisions about *this desktop*: the bindings from `jev` to the engine.

`jev.py` knows how to get a typed answer. This module knows which questions
are worth asking about a Windows UI, how to turn a control tree into a state
small enough to send, and — the part that matters most — when an answer is
good enough to act on.

Four places in the existing engine are doing a fuzzy job with crisp code, and
each is one question wide:

  `locator._resolve_among`  "several controls share this name; which one?"
                            Today: the k-th in tree order, or nearest anchor.
                            An ordinal dies the moment a page reflows.

  `replay._alternate`       rung 3 of the repair ladder. Today it has exactly
                            one idea — look the name up via OCR labels. A
                            Choice over everything in the window is a general
                            repair for ~200ms, tried *before* waking Claude.

  `act.check`               "did that step work?" Structural predicates miss
                            the interesting failure: a modal that is neither
                            the success state nor an error.

  `retrieval.search`        lexical TF-IDF, which by its own docstring cannot
                            match "stock market" to "borsa" and therefore
                            hands its candidates to Claude. A re-rank is
                            exactly the shape Jev is for.

And one that does not exist yet and should: a **commit guard**. The whole
point of a fast loop is to stop asking permission for every click. That is
only safe if something cheap is watching for the click that spends money or
cannot be undone. At ~$0.00005 per check it is affordable to ask before every
single action, which is the only frequency that actually protects anything.

**The rule this module enforces.** Nothing here acts on an answer that is not
both real and confident. `jev.Decision.real` is False for the offline stub, and
`Verdict.act` is False unless confidence clears the threshold for that call
site. A missing API key must surface as "escalate to Claude", never as a
confident random click — STATE.md §3, fail loudly rather than plausibly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import jev

#: Default gate. Chosen to match TypeSafe's own guidance (act above 0.8,
#: review 0.5-0.8, escalate below 0.5) and the project's habit of preferring a
#: slow correct step to a fast wrong one.
CONFIDENT = 0.80

#: A confident distribution can still be split across two near-identical
#: candidates — two "Buy" buttons, two "Continue". Margin catches that;
#: confidence does not. Both must clear for a click.
MARGIN = 0.25

#: Above this probability that an action spends money or cannot be undone, the
#: fast loop stops and hands the decision to Claude or the user. Deliberately
#: low: a false alarm costs one round trip, a miss costs a plane ticket.
COMMIT_STOP = 0.30

#: Words that make the *code* call something a commit, whatever the model says.
#:
#: This list exists because of a documented property of jev-1.13: "state is
#: data, and the model does not treat it as hostile by default. Content
#: written to adversarially steer the model can move the answer."
#: (docs.typesafe.ai/model-jaggedness/jev-1.13, failure mode 6.)
#:
#: Every control name we send comes off a web page or an application we do not
#: control. On a checkout page that text is written by the merchant. A page
#: that labels its pay button "Continue browsing" — by sloppiness or by
#: design — would talk the guard down, and the guard is the only thing
#: standing between a fast loop and a real purchase. So the model is allowed
#: to *add* caution and never to remove it.
#:
#: Turkish included deliberately: the ticketing and shopping sites this is
#: aimed at are Turkish, and an English-only list would miss every one of them.
COMMIT_WORDS = (
    # English
    "pay", "payment", "checkout", "buy", "purchase", "order now", "place order",
    "confirm", "subscribe", "submit", "send", "book now", "reserve", "donate",
    "delete", "remove", "cancel booking", "agree", "accept terms",
    # Turkish
    "öde", "odeme", "ödeme", "satın al", "satin al", "satınal", "sepeti onayla",
    "siparişi tamamla", "siparisi tamamla", "onayla", "rezervasyon",
    "devam et ve öde", "kaydet ve öde", "sil", "iptal et", "kabul ediyorum",
)

#: Window titles / URLs that put the whole screen in commit territory. A page
#: is judged by where it is, not only by what it calls its buttons.
COMMIT_CONTEXT = ("checkout", "payment", "ödeme", "odeme", "sepet", "basket",
                  "cart", "billing", "3d secure", "3dsecure", "secure.",
                  "bank", "banka", "kart", "card details")

#: Confidence an operation needs, by what it costs to be wrong about.
#:
#: The three that act on a control, and `submit` — which presses Enter and
#: can therefore send a form — are held to the full gate. The rest are
#: observations about where the task is, and being wrong about one costs a
#: wasted 344ms or an early hand-back to Claude, so a strict gate on them
#: only throws away usable answers.
OP_GATE: dict[str, float] = {
    "click": CONFIDENT, "type": CONFIDENT, "select": CONFIDENT,
    "submit": CONFIDENT,
    "scroll_down": 0.50, "scroll_up": 0.50, "wait": 0.50,
    "done": 0.60, "blocked": 0.50,
}

#: How many controls to put in the state. The full table is capped at 100 by
#: `find_controls` already; 60 keeps a decision near 2k tokens (~$0.00008).
MAX_ROWS = 60


@dataclass(frozen=True)
class Verdict:
    """An answer plus the single bit the caller actually needs: act or not."""

    act: bool
    why: str
    answer: jev.Answer | None = None
    decision: jev.Decision | None = None

    @property
    def confidence(self) -> float:
        return getattr(self.answer, "confidence", 0.0) if self.answer else 0.0

    def line(self) -> str:
        head = "ACT " if self.act else "HOLD"
        tail = self.decision.line() if self.decision else ""
        return f"{head} {self.why} | {tail}".strip()


def _gate(d: jev.Decision, key: str, at: float,
          margin: float | None = None) -> Verdict:
    """The one place that decides whether an answer may be acted on."""
    if not d.real:
        return Verdict(False, "offline stub — no API key; escalate", None, d)
    a = d.answers.get(key)
    if a is None:
        return Verdict(False, f"no answer for {key!r}", None, d)
    conf = a.confidence
    if conf < at:
        return Verdict(False, f"confidence {conf:.2f} < {at:.2f}", a, d)
    if margin is not None and isinstance(a, jev.ChoiceAnswer) and a.margin < margin:
        return Verdict(False, f"margin {a.margin:.2f} < {margin:.2f} "
                              f"({a.ranked[:2]})", a, d)
    return Verdict(True, f"confidence {conf:.2f}", a, d)


# --------------------------------------------------------------------------
# State: a window as text
# --------------------------------------------------------------------------

def rows_to_state(rows: Sequence[Mapping[str, Any]], *,
                  window: str = "", extra: Mapping[str, Any] | None = None,
                  cap: int = MAX_ROWS) -> dict:
    """Turn a control table into the JSON Jev reads.

    Keys are short on purpose — every character is an input token, and at 5Hz
    the difference between `"control_type"` and `"t"` is real money. Keeping
    the *values* verbatim matters far more than keeping the keys readable.

    The integer `i` is the contract: the model answers with `i`, and the
    caller maps it back to a real `locator.Element`. The model never sees a
    coordinate and never returns one — the project's first rule.
    """
    out = []
    for i, r in enumerate(rows[:cap]):
        row: dict[str, Any] = {"i": i, "t": r.get("type") or r.get("control") or "",
                               "n": r.get("name") or ""}
        if r.get("id"):
            row["id"] = r["id"]
        # What a field already contains. Without this the model cannot tell a
        # box it has just filled from an empty one, and a search loop types
        # the same word into the same field forever — which is exactly what
        # the first live run on migros.com.tr did, eight times.
        if r.get("value"):
            row["v"] = str(r["value"])[:40]
        if r.get("hint"):
            row["hint"] = r["hint"]
        out.append(row)
    state: dict[str, Any] = {"window": window, "controls": out}
    if len(rows) > cap:
        state["truncated"] = len(rows) - cap
    if extra:
        state.update(dict(extra))
    return state


def elements_to_rows(elements: Iterable[Any]) -> list[dict]:
    """`locator.Element` objects -> plain rows.

    Note `control_type`, not `control`: on an Element, `control` is the live
    UIA handle the actuator invokes, and `control_type` is the string we want
    to show the model. Getting those two confused sends a row whose type is
    the repr of a COM object.
    """
    rows = []
    for e in elements:
        rows.append({"type": getattr(e, "control_type", "") or "",
                     "name": getattr(e, "name", "") or "",
                     "id": getattr(e, "automation_id", "") or ""})
    return rows


def window_candidates(hwnd: int, cap: int = MAX_ROWS
                      ) -> tuple[list[dict], list[Any]]:
    """Everything nameable in a window, as rows and Elements in lock step.

    The pairing is the whole point: the model answers with a row index, and
    `elements[i]` is the live UIA handle that index means. The model never
    sees a coordinate and never returns one, so the project's first rule —
    a pixel position never crosses a turn boundary — still holds even though
    a remote model is now choosing the target.

    Imported lazily so `fast` stays importable, and its tests runnable, on a
    machine with no UI Automation.
    """
    from . import locator

    try:
        root = locator.uia_root(hwnd)
    except Exception:
        return [], []

    els: list[Any] = []
    for ch in locator.walk(root, max_depth=40, max_nodes=2500, budget_s=4.0):
        if locator._rect_of(ch) is None:
            continue
        el = locator._to_element(ch, "uia:jev", hwnd)
        if el is None or not (el.name or "").strip():
            continue
        els.append(el)
        if len(els) >= cap:
            break
    return elements_to_rows(els), els


def fingerprint(rows: Sequence[Mapping[str, Any]]) -> str:
    """Cheap identity for a screen state.

    A decision takes ~200ms and the UI does not wait. Before acting, compare
    the fingerprint taken when the question was asked with one taken now: if
    the window changed underneath, the answer refers to a screen that no
    longer exists and must be discarded, not applied. This is the failure the
    Doom demo tolerates because a missed shot is free; a click on a moved
    checkout button is not.
    """
    import hashlib
    blob = "\n".join(f"{r.get('type','')}|{r.get('name','')}|{r.get('id','')}"
                     for r in rows)
    return hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()[:16]


# --------------------------------------------------------------------------
# The questions
# --------------------------------------------------------------------------

def q_pick(intent: str, rows: Sequence[Mapping[str, Any]],
           cap: int = MAX_ROWS) -> jev.Choice:
    """"Which control satisfies this intent?" — the workhorse.

    Options are the row indices. Descriptions are `type: name` because the
    type is what separates the menu item "Save" from the button "Save" from
    the tab "Save", and name alone is exactly the ambiguity that
    `_resolve_among` currently resolves by counting.
    """
    criteria: dict[str, str | None] = {}
    for i, r in enumerate(rows[:cap]):
        name = (r.get("name") or "").strip()
        t = (r.get("type") or "").strip()
        criteria[str(i)] = f"{t}: {name}" if name else f"{t} (unnamed)"
    criteria["none"] = ("No control here does that — the screen is wrong, "
                        "the step is not possible yet, or it must be scrolled to")
    return jev.Choice(
        instructions={"goal": intent,
                      "task": "Choose the one control a careful person would "
                              "use next to achieve the goal. Answer with its "
                              "index, or 'none'."},
        criteria=criteria)


def q_worked(intent: str) -> jev.Noul:
    """"Did that step do what it was for?" — cheaper than a screenshot, and
    it notices the failure a structural predicate cannot: a screen that is
    neither the old one nor the intended one."""
    return jev.Noul(
        instructions={"step": intent,
                      "task": "Compare `before` and `after`. Did the step "
                              "achieve its purpose?"},
        criteria={"true": "The after state is what this step was meant to "
                          "produce — the dialog opened, the value is set, the "
                          "page advanced.",
                  "false": "Nothing changed, an error or warning appeared, a "
                           "different dialog appeared, or the app is asking "
                           "for something the step did not supply."})


def q_commit(intent: str) -> jev.Noul:
    """The guard. Asked before every click the fast loop makes.

    Written as "would a careful person want to be asked first", not "is this
    dangerous", because the latter reads as physical danger and under-fires on
    the case we care about: a perfectly ordinary-looking button that happens
    to be the last one before money moves.
    """
    return jev.Noul(
        instructions={"about_to_do": intent,
                      "task": "Would a careful person want to be asked before "
                              "this is done on their computer?"},
        criteria={"true": "It spends money, places or confirms an order, "
                          "sends a message to someone, submits a form that "
                          "cannot be taken back, deletes or overwrites data, "
                          "changes a password or a setting that locks them "
                          "out, or accepts terms on their behalf.",
                  "false": "It searches, filters, sorts, scrolls, opens, "
                           "expands, navigates, fills a field, or puts "
                           "something in a basket — anything they could undo "
                           "by clicking back."})


def q_blocker(rows_hint: str = "") -> jev.Choice:
    """What kind of thing is in the way — the single most common time sink in
    shopping and ticketing flows, and pure recognition."""
    return jev.Choice(
        instructions="The expected control is not here. What is on screen "
                     "instead?",
        criteria={"cookie_banner": "A cookie or privacy consent notice",
                  "login": "A sign-in or account wall",
                  "captcha": "A human-verification or bot check",
                  "newsletter": "A promotional, app-install or newsletter popup",
                  "error": "An error, outage or 'something went wrong' message",
                  "sold_out": "The item, seat or fare is no longer available",
                  "loading": "Still loading — spinner, skeleton or empty list",
                  "wrong_page": "A different page than expected",
                  "nothing": "Nothing is blocking; the control is simply "
                             "elsewhere or needs scrolling to"})


#: What the engine can actually carry out. Deliberately small and closed —
#: the model picks from what `actuator` can do, so an answer is always
#: executable and "unsupported action" cannot happen.
OPERATIONS: dict[str, str] = {
    "click": "Press the chosen control to make something happen — a button, "
             "a menu item, a link, a checkbox. Not for entering text.",
    # Measured: without the second sentence, a search box splits the operation
    # head between click and type at ~0.73 and the whole decision is held.
    # It is a fair reading of an ambiguous question — you do click a search
    # field before typing in it — so the question had to say which wins.
    "type": "Put text into the chosen field. Choose this for any text entry, "
            "including when the field must be focused first — focusing is "
            "part of typing, not a separate click. The text itself is "
            "written elsewhere; this only decides that typing is needed.",
    # Typing into a search box achieves nothing until something submits it.
    # Without this operation the loop fills the field and has no move left
    # that makes the page change, so it fills it again.
    "submit": "Confirm what is already typed — press Enter to run the search "
              "or send the form. Choose this when the field already holds "
              "the right text.",
    "select": "Choose a value from the chosen dropdown or list",
    "scroll_down": "The thing needed is below the visible area",
    "scroll_up": "The thing needed is above the visible area",
    "wait": "The screen is still loading or settling; do nothing yet",
    "done": "The goal has been reached; nothing more is needed",
    "blocked": "Something is in the way that this loop cannot clear",
}


def q_operation(intent: str) -> jev.Choice:
    """What *kind* of action comes next — asked in the same breath as which
    control.

    Browser Use's `jev-ultrafast` calls this "two decisions, one network round
    trip", and it is the single biggest structural win available: the
    operation head and the target head read the same state, so asking both
    costs one round trip instead of two. On their Google Flights run that
    contributed to a median task time of 7.09s against 9.45s, and browser
    protocol calls dropping from 1,092 to 101.
    """
    return jev.Choice(
        instructions={"goal": intent,
                      "task": "What kind of action moves this goal forward "
                              "right now?"},
        criteria=dict(OPERATIONS))


# --------------------------------------------------------------------------
# The calls
# --------------------------------------------------------------------------

def pick(intent: str, rows: Sequence[Mapping[str, Any]], *,
         window: str = "", at: float = CONFIDENT, margin: float = MARGIN,
         also: Mapping[str, jev.Question] | None = None,
         client: jev.Client | None = None) -> tuple[int | None, Verdict]:
    """Choose a control for an intent. Returns its row index, or None.

    `also` rides along free: independent questions are evaluated in parallel
    against the same state, so asking "and is this a commit?" and "and what is
    blocking?" in the same breath costs a few hundred tokens and no extra
    wall-clock. Always send the commit guard with the pick — a second round
    trip to ask it is 200ms the loop does not have, and a guard you skipped
    because it was slow is a guard you do not have.
    """
    c = client or jev.client()
    qs: dict[str, jev.Question] = {"pick": q_pick(intent, rows)}
    if also:
        qs.update(also)
    d = c.ask(rows_to_state(rows, window=window), qs)
    v = _gate(d, "pick", at, margin)
    if not v.act:
        return None, v
    choice = getattr(v.answer, "choice", "")
    if choice == "none":
        return None, Verdict(False, "model says no control here fits",
                             v.answer, d)
    try:
        i = int(choice)
    except ValueError:
        return None, Verdict(False, f"unparsable choice {choice!r}", v.answer, d)
    if not 0 <= i < len(rows):
        return None, Verdict(False, f"index {i} out of range", v.answer, d)
    return i, v


@dataclass(frozen=True)
class Move:
    """One decision: what to do, to what, and whether it may be done."""

    operation: str
    index: int | None
    verdict: Verdict
    blocker: str = ""

    @property
    def needs_target(self) -> bool:
        return self.operation in {"click", "type", "select"}

    def line(self) -> str:
        tgt = "" if self.index is None else f" #{self.index}"
        blk = f" blocker={self.blocker}" if self.blocker else ""
        return f"{self.operation}{tgt}{blk} | {self.verdict.line()}"


def decide(intent: str, rows: Sequence[Mapping[str, Any]], *,
           window: str = "", at: float = CONFIDENT, margin: float = MARGIN,
           guard: bool = True, client: jev.Client | None = None) -> Move:
    """Operation, target, guard and blocker — one state, one round trip.

    This is the shape Browser Use arrived at for `jev-ultrafast`, and it is
    worth copying exactly: the heads all read the same state, so four
    questions cost one network round trip and a few hundred extra tokens.
    Asking them separately would be four times the latency for the same
    information, and at 200ms a call that is the whole budget.

    A target is only required for the operations that act on one. `scroll`,
    `wait`, `done` and `blocked` resolve with `index is None` and are still
    perfectly good answers — "the thing is not on screen yet" is a decision,
    not a failure, and it is the one a control table alone cannot express.
    """
    c = client or jev.client()
    qs: dict[str, jev.Question] = {"op": q_operation(intent),
                                   "pick": q_pick(intent, rows),
                                   "blocker": q_blocker()}
    if guard:
        qs["commit"] = q_commit(intent)

    try:
        d = c.ask(rows_to_state(rows, window=window), qs)
    except jev.JevError as e:
        return Move("blocked", None, Verdict(False, f"no decision: {e}"))

    # The operation head is gated by what the operation *costs to get wrong*,
    # not by one flat number. A mistaken click acts on the world; a mistaken
    # `done` stops and asks Claude, which is exactly what blocking does — so
    # holding `done` to a click's standard buys nothing and loses the answer.
    #
    # Measured: the migros.com.tr run reached the results page, ranked `done`
    # top at 0.74, and a flat 0.80 gate turned a completed task into
    # "blocked". `submit` stays strict because pressing Enter can send a form.
    a_op = d.answers.get("op")
    if not d.real or not isinstance(a_op, jev.ChoiceAnswer):
        return Move("blocked", None,
                    _gate(d, "op", at))
    operation = a_op.choice
    need = OP_GATE.get(operation, at)
    if a_op.confidence < need:
        return Move("blocked", None,
                    Verdict(False, f"op confidence {a_op.confidence:.2f} "
                                   f"< {need:.2f} for {operation!r}", a_op, d))
    op_v = Verdict(True, f"op confidence {a_op.confidence:.2f}", a_op, d)

    blk = d.answers.get("blocker")
    blocker = getattr(blk, "choice", "") if blk else ""
    if blocker == "nothing":
        blocker = ""

    if operation not in {"click", "type", "select"}:
        return Move(operation, None, op_v, blocker)

    pick_v = _gate(d, "pick", at, margin)
    if not pick_v.act:
        return Move(operation, None, pick_v, blocker)
    choice = getattr(pick_v.answer, "choice", "")
    if choice == "none":
        return Move(operation, None,
                    Verdict(False, "no control here fits", pick_v.answer, d),
                    blocker)
    try:
        i = int(choice)
    except ValueError:
        return Move(operation, None,
                    Verdict(False, f"unparsable choice {choice!r}",
                            pick_v.answer, d), blocker)
    if not 0 <= i < len(rows):
        return Move(operation, None,
                    Verdict(False, f"index {i} out of range", pick_v.answer, d),
                    blocker)

    if guard:
        word = code_side_commit(intent, window=window, target=rows[i])
        if word:
            return Move(operation, None,
                        Verdict(False, f"code-side guard: {word!r}",
                                pick_v.answer, d), blocker)
        g = d.answers.get("commit")
        if isinstance(g, jev.NoulAnswer) and g.p >= COMMIT_STOP:
            return Move(operation, None,
                        Verdict(False, f"commit guard (p={g.p:.2f})", g, d),
                        blocker)

    return Move(operation, i, pick_v, blocker)


def available() -> bool:
    """Is there a usable key? Call sites check this before doing any work.

    Without it, a repair attempt on a machine with no key would build a
    control table, hit the offline stub, and be refused — correct, but a
    wasted UIA walk on every failed step.
    """
    return not jev.client().offline


def repair(intent: str, hwnd: int, *, window: str = "",
           at: float = CONFIDENT, client: jev.Client | None = None
           ) -> tuple[Any | None, Verdict]:
    """"The locator stack failed. Is the control still here under another name?"

    This is the rung `replay._alternate` was missing. The existing tier looks
    the *same name* up via OCR, which fixes a control that draws its label
    instead of publishing it — but not a button that was renamed, moved into
    a menu, or replaced by one that means the same thing. That is the failure
    a human fixes in a second by looking, and it is exactly a bounded choice
    over what is on screen.

    Returns the live Element, or None with a Verdict saying why not. The
    commit guard applies: a repair that lands on a pay button is not a
    repair, it is an accident, and it goes back to Claude.
    """
    rows, els = window_candidates(hwnd)
    if not rows:
        return None, Verdict(False, "nothing nameable in the window")

    i, v = pick(intent, rows, window=window, at=at,
                also={"commit": q_commit(intent)}, client=client)
    if i is None:
        return None, v

    if v.decision is not None:
        g = v.decision.answers.get("commit")
        if isinstance(g, jev.NoulAnswer) and g.p >= COMMIT_STOP:
            return None, Verdict(False, f"repair lands on something "
                                        f"irreversible (p={g.p:.2f})", g,
                                 v.decision)
    word = code_side_commit(intent, window=window, target=rows[i])
    if word:
        return None, Verdict(False, f"repair lands on {word!r} — not automatic",
                             v.answer, v.decision)

    return els[i], v


def code_side_commit(intent: str, *, window: str = "",
                     target: Mapping[str, Any] | None = None) -> str:
    """Does *code* think this is a commit, without asking anything?

    Returns the matched word, or "" for none. This runs before and alongside
    the model, and its verdict is not reviewable: `safe_to_act` holds if
    either this or the model fires. The model may add caution; it may never
    remove it.

    The reason is failure mode 6 of jev-1.13 — state is data, and the model
    does not treat it as hostile. Every string we put in the state came off a
    page we do not control, and on a checkout page the merchant wrote it.
    A cheap, deterministic, page-independent check is what stops a mislabelled
    button from talking the guard down.
    """
    hay = f" {intent.lower()} "
    if target:
        hay += f" {str(target.get('name', '')).lower()} "
    for w in COMMIT_WORDS:
        if w in hay:
            return w
    ctx = (window or "").lower()
    for w in COMMIT_CONTEXT:
        if w in ctx:
            return f"context:{w}"
    return ""


def safe_to_act(intent: str, rows: Sequence[Mapping[str, Any]], *,
                window: str = "", stop_at: float = COMMIT_STOP,
                target: Mapping[str, Any] | None = None,
                client: jev.Client | None = None) -> Verdict:
    """The guard on its own, for call sites that did not batch it.

    Two independent checks, and *either* one holds:

      1. `code_side_commit` — deterministic, cannot be argued with by page
         content, catches the obvious cases in two languages;
      2. the model — catches everything phrased in a way no word list
         anticipated.

    Note the inverted gate on the model's half: this is the one question where
    an *unreliable* answer must not mean "go ahead". An offline stub, a
    low-confidence reading, or an outright API failure all resolve to hold.
    """
    word = code_side_commit(intent, window=window, target=target)
    if word:
        return Verdict(False, f"code-side guard: {word!r} — confirm first")

    c = client or jev.client()
    try:
        d = c.ask(rows_to_state(rows, window=window),
                  {"commit": q_commit(intent)})
    except jev.JevError as e:
        return Verdict(False, f"guard could not be checked ({e}) — hold")
    if not d.real:
        return Verdict(False, "offline stub cannot clear a commit guard", None, d)
    a = d.answers.get("commit")
    if not isinstance(a, jev.NoulAnswer):
        return Verdict(False, "no commit answer", None, d)
    if a.p >= stop_at:
        return Verdict(False, f"looks irreversible (p={a.p:.2f}) — confirm first",
                       a, d)
    return Verdict(True, f"reversible (p={a.p:.2f})", a, d)


def rerank(query: str, candidates: Sequence[Mapping[str, Any]], *,
           at: float = 0.6, client: jev.Client | None = None
           ) -> tuple[int | None, Verdict]:
    """Pick the saved job that answers a description.

    This is the gap `retrieval.py` documents and leaves open: lexical matching
    cannot connect "stock market" to "borsa". It hands its candidates up to
    Claude for that. A Choice over the same candidates closes it for 200ms
    instead of a full turn, and still hands up whatever it is unsure about.
    """
    c = client or jev.client()
    crit: dict[str, str | None] = {}
    for i, row in enumerate(candidates):
        name = row.get("name", "")
        desc = (row.get("description") or "").strip()
        crit[str(i)] = f"{name} — {desc}" if desc else str(name)
    crit["none"] = "None of these is the job being described"
    d = c.ask({"asked_for": query},
              {"job": jev.Choice(instructions="Which saved job is the person "
                                              "describing?", criteria=crit)})
    v = _gate(d, "job", at)
    if not v.act:
        return None, v
    ch = getattr(v.answer, "choice", "")
    if ch == "none":
        return None, Verdict(False, "no saved job matches", v.answer, d)
    try:
        return int(ch), v
    except ValueError:
        return None, Verdict(False, f"unparsable choice {ch!r}", v.answer, d)


def worked(intent: str, before: Sequence[Mapping[str, Any]],
           after: Sequence[Mapping[str, Any]], *,
           at: float = 0.6, client: jev.Client | None = None) -> Verdict:
    """Verify a step from the control-table diff, with no screenshot."""
    c = client or jev.client()
    state = {"step": intent,
             "before": rows_to_state(before)["controls"],
             "after": rows_to_state(after)["controls"]}
    d = c.ask(state, {"worked": q_worked(intent), "blocker": q_blocker()})
    if not d.real:
        return Verdict(False, "offline stub — cannot verify", None, d)
    a = d.answers.get("worked")
    if not isinstance(a, jev.NoulAnswer):
        return Verdict(False, "no answer", None, d)
    if a.confidence < at:
        return Verdict(False, f"unsure ({a.p:.2f}) — escalate", a, d)
    blocker = d.answers.get("blocker")
    tag = getattr(blocker, "choice", "") if blocker else ""
    why = f"p(worked)={a.p:.2f}" + (f", blocker={tag}" if tag and tag != "nothing"
                                    else "")
    return Verdict(a.yes, why, a, d)


# --------------------------------------------------------------------------
# A loop that keeps up with the screen
# --------------------------------------------------------------------------

@dataclass
class Tick:
    ok: bool
    note: str
    index: int | None = None
    verdict: Verdict | None = None
    stale: bool = False


class Pilot:
    """Sense -> decide -> act, at a rate the UI notices.

    The hazard at this speed is not a wrong answer, it is a *late* one. The
    decision describes the screen as it was ~200ms ago. Before acting, the
    screen is re-sensed and fingerprinted; if it moved, the answer is dropped
    and the loop senses again rather than clicking where a button used to be.
    Dropping a tick costs 200ms. Not dropping it costs whatever was underneath.

    Deliberately *not* pipelined. Requesting decision N+1 while acting on N
    would hide the latency, but every action changes the state the next
    decision is about, so the pipelined answer is stale by construction. The
    Doom demo can live with that because a wasted shot is free. A checkout
    page cannot.
    """

    #: Measured, not guessed. From this machine via OpenRouter, 30 calls:
    #: warm p50 344ms, p95 453ms, range 281-469ms with no long tail. A 5Hz
    #: loop needs a 200ms budget and cannot have it; 2Hz leaves the p95 with
    #: room to spare, and ~2.9Hz is the hard ceiling. The connect time says
    #: this is not the gateway's fault — OpenRouter is 132ms from here
    #: against TypeSafe's own 546ms, so direct access would be slower, not
    #: faster. 344ms is simply what a decision costs at this distance, and it
    #: is still ~30x quicker than the Claude turn it replaces.
    DEFAULT_HZ = 2.0

    def __init__(self, sense: Callable[[], list[dict]],
                 act: Callable[[int], bool], *,
                 hz: float = DEFAULT_HZ, client: jev.Client | None = None,
                 guard: bool = True, window: str = "") -> None:
        self.sense = sense
        self.act = act
        self.period = 1.0 / max(hz, 0.2)
        self.client = client or jev.client()
        self.guard = guard
        self.window = window
        self.ticks: list[Tick] = []

    def step(self, intent: str, *, at: float = CONFIDENT) -> Tick:
        rows = self.sense()
        if not rows:
            return self._log(Tick(False, "nothing on screen"))
        before = fingerprint(rows)

        also = {"commit": q_commit(intent)} if self.guard else None
        i, v = pick(intent, rows, window=self.window, at=at, also=also,
                    client=self.client)

        if self.guard and v.decision is not None:
            g = v.decision.answers.get("commit")
            if isinstance(g, jev.NoulAnswer) and g.p >= COMMIT_STOP:
                return self._log(Tick(False,
                                      f"commit guard held (p={g.p:.2f}) — "
                                      f"hand to Claude or the user",
                                      None, v))
        if i is None:
            return self._log(Tick(False, v.why, None, v))

        # The model has now named a target, so the deterministic guard can see
        # what it is. This is the check the model cannot be talked out of.
        if self.guard:
            word = code_side_commit(intent, window=self.window, target=rows[i])
            if word:
                return self._log(Tick(False, f"code-side guard: {word!r} — "
                                             f"hand to Claude or the user", None, v))

        if fingerprint(self.sense()) != before:
            return self._log(Tick(False, "screen moved while deciding — "
                                         "answer discarded", i, v, stale=True))
        return self._log(Tick(bool(self.act(i)), v.why, i, v))

    def run(self, intent: str, *, until: Callable[[], bool],
            budget_s: float = 30.0, at: float = CONFIDENT) -> list[Tick]:
        end = time.monotonic() + budget_s
        while time.monotonic() < end and not until():
            t0 = time.monotonic()
            tick = self.step(intent, at=at)
            if not tick.ok and not tick.stale:
                break                       # hand up rather than thrash
            slack = self.period - (time.monotonic() - t0)
            if slack > 0:
                time.sleep(slack)
        return self.ticks

    def _log(self, t: Tick) -> Tick:
        self.ticks.append(t)
        return t

    def report(self) -> str:
        ok = sum(1 for t in self.ticks if t.ok)
        stale = sum(1 for t in self.ticks if t.stale)
        return (f"{len(self.ticks)} ticks, {ok} acted, {stale} stale | "
                f"{self.client.stats.report()}")
