"""Running a script against a real screen.

Three decisions shape this file, and two of them are about failure.

**1. "Skip and carry on" means skip the ITEM, not the step.**

The obvious reading — a step fails, ignore it, run the next step — is
actively dangerous, and the Migros run showed exactly how. The script is:

    search for "{product}"  →  click the first result  →  add to basket

If the search fails and the runner carries on to step two, it clicks the
first result *of the previous product's search*, which is still on screen,
and adds the wrong thing to the basket. Carrying on past a failed step
does not do nothing; it does something WRONG, confidently, and reports
success.

So a failed step abandons the rest of its iteration. The next iteration of
a `repeat` starts fresh, which is safe because it begins by re-establishing
state (a search, a navigation) rather than assuming it. That is the
behaviour the requirement asks for — ten products attempted, the failures
listed at the end — and it is the only version of it that cannot corrupt
the basket.

`optional: true` opts a single step out, for the cookie banner that may or
may not be there. That is the narrow case the naive reading was reaching
for, and it should be written on the step that needs it.

**2. Verification is part of the step, not a later surprise.**

`did I actually do that` was the single most expensive thing this project
got wrong: a click reported success, the basket stayed empty, and the run
claimed to have added milk. So `expect:` and `gone:` run inside the step
that names them, against a fresh look, and a step whose post-condition
fails IS a failed step.

**3. Stop means stop.**

Any loop that can click has to honour a stop signal on every iteration —
checked before each step and before each retry, not once at the top. A
fast loop that ignores it is a way to keep driving after the human has
already said no.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from vision.elements import Element, Watcher
from vision import query
from . import choose as choose_mod
from . import guard, resolve
from .library import Resolved
from .schema import (Pick, Script, ScriptError, Selector, Step, Trust,
                     _check_url,
                     normalise_url, substitute)

def _text_only(sel: Selector) -> bool:
    """Can pixels alone answer this? Then do not walk the tree for it."""
    return bool((sel.text or sel.any) and not sel.kind and not sel.id
                and not sel.icon)


#: How long to keep looking for something that should appear.
POLL_S = 0.35

#: After an action, how long to let the screen settle before verifying.
#: Below this a `expect:` reads the page as it was; much above it and every
#: step pays for the slowest case.
SETTLE_S = 0.4


class Stopped(Exception):
    """The run was told to stop. Never caught by the failure policy."""


class StepFailed(Exception):
    """This step did not happen. Abandons the iteration, not the run."""


class Actuator(Protocol):
    """What the runner needs from the world. Mocked in tests."""

    def click(self, el: Element, button: str = "left") -> bool: ...
    def double_click(self, el: Element) -> bool: ...
    def type_text(self, text: str, el: Element | None) -> bool: ...
    def press_key(self, key: str) -> bool: ...
    def drag(self, a: Element, b: Element) -> bool: ...
    def scroll(self, el: Element | None, direction: str, amount: int) -> bool: ...
    def move(self, el: Element) -> bool: ...
    # Reaching the thing to act on, rather than acting on it.
    def launch(self, app: str, url: str) -> str: ...
    def find_window(self, title: str) -> int: ...
    def foreground_window(self) -> int: ...
    def focus_window(self, hwnd: int, maximize: bool) -> bool: ...


@dataclass
class Outcome:
    """What happened at one step."""

    where: str            # migros/add-product.steps[2]
    do: str
    detail: str
    ok: bool
    ms: float
    skipped: bool = False


@dataclass
class Report:
    """The whole run, including everything that did not work.

    Written to be read at the END of a ten-product shop, which is the shape
    the requirement asked for: what went in, what did not, and why.
    """

    script: str
    outcomes: list[Outcome] = field(default_factory=list)
    #: for_each value → whether that iteration completed
    items: dict[str, bool] = field(default_factory=dict)
    #: for_each value → the label `pick` chose in that iteration (e.g.
    #: "süt" → "İçim Süt 1 L"). The language has no variables, so this is
    #: how what a script DECIDED gets out of it: a caller reads it and can
    #: generate the next script from it.
    picks: dict[str, str] = field(default_factory=dict)
    #: label -> how many went in: 1 when a `pick` click verified, +1 for
    #: every verified click on a `nearest: last` control after it (the
    #: quantity stepper). Ordered by first addition - it is the shopping
    #: list as it actually happened, which is what gets sent to the person.
    bought: dict[str, int] = field(default_factory=dict)
    #: steps a stronger model rescued, for the report
    rescued: list[str] = field(default_factory=list)
    stopped: bool = False
    stopped_by: str = ""
    ms: float = 0.0

    @property
    def ok(self) -> bool:
        return (not self.stopped
                and all(o.ok or o.skipped for o in self.outcomes)
                and all(self.items.values()))

    def failures(self) -> list[Outcome]:
        return [o for o in self.outcomes if not o.ok and not o.skipped]

    def summary(self) -> str:
        lines = [f"{self.script}: {len(self.outcomes)} steps in "
                 f"{self.ms / 1000:.1f}s"]
        if self.items:
            done = [k for k, v in self.items.items() if v]
            missed = [k for k, v in self.items.items() if not v]
            lines.append(f"  {len(done)}/{len(self.items)} items completed")
            if missed:
                lines.append(f"  missed: {', '.join(missed)}")
        for o in self.failures():
            lines.append(f"  FAILED {o.where} ({o.do}): {o.detail}")
        if self.stopped:
            lines.append("  STOPPED by request")
        return "\n".join(lines)


class Runner:
    """Executes a resolved script.

    `confirm` is called before anything the commit guard flags. Returning
    False refuses the action, which fails the step — that is deliberate: a
    refused checkout should not be followed by the steps that assume it
    happened.

    `should_stop` is polled before every step. The default never stops; a
    real caller wires it to whatever its ESC handler sets.
    """

    def __init__(self, resolved: Resolved, watcher: Watcher,
                 actuator: Actuator,
                 confirm: Callable[[str, Element], bool] | None = None,
                 should_stop: Callable[[], bool] | None = None,
                 on_step: Callable[[Outcome], None] | None = None,
                 chooser: "choose_mod.Chooser | None" = None,
                 cursor: "Any | None" = None,
                 helper: "Any | None" = None,
                 dry_run: bool = False,
                 honour_trust: bool = True,
                 rescue_tries: int = 1,
                 skip_items: "set[str] | None" = None) -> None:
        self.r = resolved
        self.watcher = watcher
        self.act = actuator
        self.confirm = confirm
        self.should_stop = should_stop or (lambda: False)
        self.on_step = on_step
        #: Resolves `pick:` steps. Injected in tests; built lazily in real
        #: use so a script with no `pick` never needs a key or a network.
        self._chooser = chooser
        self.chose = 0
        self._last_pick: str | None = None
        #: Optional visual pointer. Driven for EVERY action, including the
        #: ones with no real mouse - a UIA Invoke and a keystroke both
        #: happen somewhere, and without this they happen invisibly.
        self.cursor = cursor
        self.dry_run = dry_run
        #: A stronger model asked, with a screenshot, when a step is stuck
        #: and by `assist` steps. None = a stuck step simply fails.
        self.helper = helper
        self._rescuing = False
        #: False = even a TRUSTED script asks before a commitment. The
        #: app's "Supervised" level: trust records how well a script has
        #: behaved, the person decides whether that is enough to spend money.
        self.honour_trust = honour_trust
        #: How many times the helper may try one stuck step, each try told
        #: what the earlier ones did.
        self.rescue_tries = max(1, int(rescue_tries))
        #: `for_each` items already done by an earlier, interrupted run.
        self.skip_items = set(skip_items or ())
        #: Set by a `wait_message` with `authorizes: true`: the person wrote
        #: the go-ahead in their own chat. Unlocks the commit guard.
        self.authorized = ""
        #: Where the last click landed, for `nearest: "last"`.
        self._last_point: tuple[int, int] | None = None
        self._last_label: str | None = None
        self.report = Report(script=resolved.entry.name)
        #: (step, selector) -> the element it resolved to last time.
        self._seen: dict[tuple, Element] = {}
        self.recalled = 0
        #: How often the provider-side query answered instead of a walk.
        self.asked = 0

    # -- entry -------------------------------------------------------------

    def run(self, args: dict[str, str] | None = None) -> Report:
        t0 = time.time()
        values = dict(self.r.entry.params)
        values.update(args or {})
        missing = self.r.entry.declared_params() - set(values)
        if missing:
            raise ScriptError(f"{self.r.entry.name} needs {sorted(missing)}")
        try:
            self._run_script(self.r.entry, values, depth=0)
        except Stopped as exc:
            self.report.stopped = True
            self.report.stopped_by = str(exc)
        except StepFailed:
            # A failure at the top level ends the run. Already recorded.
            pass
        self.report.ms = (time.time() - t0) * 1000
        return self.report

    def _run_script(self, script: Script, values: dict[str, str],
                    depth: int) -> None:
        for i, step in enumerate(script.steps):
            self._exec(step, values, f"{script.name}.steps[{i}]", depth)

    # -- one step ----------------------------------------------------------

    def _exec(self, step: Step, values: dict[str, str], where: str,
              depth: int) -> None:
        if self.should_stop():
            raise Stopped()

        t0 = time.time()
        try:
            detail = self._dispatch(step, values, where, depth)
        except Stopped:
            raise
        except (StepFailed, ScriptError) as exc:
            if self._can_rescue(step):
                fixed = self._rescue(step, values, where, depth, str(exc))
                if fixed:
                    ms = (time.time() - t0) * 1000
                    self._tally(step)
                    self._record(Outcome(where, step.do, fixed, True, ms))
                    return
            ms = (time.time() - t0) * 1000
            self._record(Outcome(where, step.do, str(exc), False, ms,
                                 skipped=step.optional))
            if step.optional:
                # The cookie banner that was not there. Carrying on within
                # the iteration is safe ONLY because the step said so.
                return
            raise StepFailed(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - a bug here is a failed step
            ms = (time.time() - t0) * 1000
            self._record(Outcome(where, step.do,
                                 f"{type(exc).__name__}: {exc}", False, ms))
            if step.optional:
                return
            raise StepFailed(str(exc)) from exc

        ms = (time.time() - t0) * 1000
        self._tally(step)
        self._record(Outcome(where, step.do, detail, True, ms))

    def _tally(self, step: Step) -> None:
        """Count what verifiably went in. Only after the step's own check."""
        if step.do != "click" or self.dry_run:
            return
        if step.pick is not None and self._last_pick:
            self._last_label = self._last_pick
            self.report.bought[self._last_pick] = 1
        elif (step.target is not None and step.target.nearest == "last"
              and self._last_label):
            self.report.bought[self._last_label] = (
                self.report.bought.get(self._last_label, 0) + 1)

    # -- being rescued -----------------------------------------------------

    #: Steps worth a stronger model's time when stuck. Not control flow:
    #: a failed `repeat` or `call` already reported the step inside it.
    RESCUABLE = ("click", "double_click", "right_click", "type", "key",
                 "wait_for", "expect", "scroll", "move", "focus")

    def _can_rescue(self, step: Step) -> bool:
        return (self.helper is not None and not self._rescuing
                and not self.dry_run and not step.optional
                and step.do in self.RESCUABLE
                and self.helper.budget > 0)

    def _rescue(self, step: Step, values: dict[str, str], where: str,
                depth: int, error: str) -> str:
        """Ask the helper how to finish a stuck step; run its answer.

        Its steps are parsed by the same validator as any script and run
        through the same `_exec` - so the commit guard, the stop signal and
        `expect` all still apply. It cannot click past the guard; it can
        only do what a script could have done. Success is judged the same
        way as the original: the step's own `expect`, when it has one.
        """
        history: list[str] = []
        for attempt in range(self.rescue_tries):
            if self.helper.budget <= 0:
                break
            got = self._rescue_once(step, values, where, depth, error,
                                    history, attempt)
            if got:
                return got
        return ""

    def _rescue_once(self, step: Step, values: dict[str, str], where: str,
                     depth: int, error: str, history: list[str],
                     attempt: int) -> str:
        """One try. `history` carries what earlier tries did, so a second
        try is not the first one again."""
        from .library import step_json
        self._rescuing = True
        self._show("thinking")
        n_before = len(self.report.outcomes)
        try:
            plan = self.helper.rescue(
                goal=(f"Finish this step, which failed: {step_json(step)} "
                      f"(params {values})"),
                error=error, scene=self._scene_rows(),
                history=history or None)
            if plan.get("done"):
                # The helper looked and says the step's purpose is already
                # met. Believe it only as far as the step's own check does.
                if step.expect is not None:
                    self._verify(step, values, where)
                note = str(plan.get("why", ""))[:160]
                self.report.rescued.append(f"{where}: {note}")
                return f"{self.helper.name}: already done - {note}"
            steps = self.helper.parse(plan, where)
            if not steps:
                history.append(f"try {attempt + 1}: no usable steps "
                               f"({str(plan.get('why', ''))[:120]})")
                return ""
            for i, child in enumerate(steps):
                self._exec(child, values,
                           f"{where}.rescue{attempt + 1}[{i}]", depth)
            if step.expect is not None:
                self._verify(step, values, where)
            note = str(plan.get("why", ""))[:160]
            self.report.rescued.append(f"{where}: {note}")
            return f"rescued by {self.helper.name}: {note}"
        except (StepFailed, ScriptError) as exc:
            done = [o.detail[:100] for o in self.report.outcomes[n_before:]
                    if o.ok]
            history.append(f"try {attempt + 1} FAILED: {str(exc)[:160]}"
                           + (f" (after: {'; '.join(done)})" if done else ""))
            return ""
        finally:
            self._rescuing = False

    def _scene_rows(self, cap: int = 160) -> list[str]:
        try:
            scene = self.watcher.scene(force=True)
        except Exception:
            return []
        rows = []
        for e in scene.elements:
            if not e.name.strip():
                continue
            rows.append(f"{e.kind} {e.name.strip()[:90]!r} at {e.center}"
                        + (f" id={e.automation_id}" if e.automation_id
                           else ""))
        return rows[:cap]

    def _show(self, state: str, el: Element | None = None) -> None:
        """Tell the visual cursor what is about to happen, and where.

        Deliberately best-effort: a decoration must never be able to fail
        a step, so every call is swallowed. It is also the only place the
        runner knows both the intent and the target at the same moment.
        """
        if self.cursor is None:
            return
        try:
            self.cursor.state(state)
            point = el.center if el is not None else None
            if point is None and state == "type":
                # A keystroke with no element goes wherever focus is — the
                # caret, or the focused control — so show it THERE rather
                # than leaving the cursor on the last thing it touched.
                finder = getattr(self.act, "focus_point", None)
                point = finder() if finder else None
            if point is not None:
                self.cursor.move_to(*point)
                if state == "click" and hasattr(self.cursor, "burst"):
                    self.cursor.burst(*point)
        except Exception:
            pass

    def _record(self, outcome: Outcome) -> None:
        self.report.outcomes.append(outcome)
        if self.on_step:
            self.on_step(outcome)

    def _dispatch(self, step: Step, values: dict[str, str], where: str,
                  depth: int) -> str:
        handler = getattr(self, f"_do_{step.do}", None)
        if handler is None:
            raise ScriptError(f"{where}: no handler for {step.do!r}")
        detail = handler(step, values, where, depth)
        self._verify(step, values, where)
        return detail

    def _verify(self, step: Step, values: dict[str, str], where: str) -> None:
        """Post-conditions, against a fresh look.

        The wait before looking is not politeness: a page that has begun to
        change has not finished, and reading it mid-change is how a click
        that worked gets recorded as a failure.
        """
        if step.expect is None and step.gone is None:
            return
        time.sleep(SETTLE_S)
        if step.expect is not None:
            got = self._await(self._sel(step.expect, values, where),
                              step.timeout, present=True)
            if got is None:
                raise StepFailed(
                    f"acted, but {self._sel(step.expect, values, where).describe()} "
                    f"never appeared — so the action did not take effect")
        if step.gone is not None:
            if self._await(self._sel(step.gone, values, where),
                           step.timeout, present=False) is False:
                raise StepFailed(
                    f"{self._sel(step.gone, values, where).describe()} is "
                    f"still on screen")

    # -- selectors ---------------------------------------------------------

    def _sel(self, sel: Selector, values: dict[str, str],
             where: str) -> Selector:
        """Substitute `{param}` into every text field of a selector."""
        from dataclasses import replace
        return replace(
            sel,
            text=substitute(sel.text, values, where),
            near=substitute(sel.near, values, where),
            icon=substitute(sel.icon, values, where),
            id=substitute(sel.id, values, where),
            any=tuple(substitute(a, values, where) for a in sel.any))

    def _remember(self, key: tuple, el: Element) -> None:
        if el.control is not None:      # only UIA elements can be re-verified
            self._seen[key] = el

    def _recall(self, key: tuple) -> Element | None:
        """An element found before, if it is still exactly where it was.

        The single biggest saving available, and it comes from the shape of
        the work rather than any cleverness: a `repeat` runs the same steps
        ten times against a page whose furniture does not move. The search
        box sat at (1715, 452) on every one of ten iterations, and each
        iteration paid **1118 ms** to walk 1814 UIA nodes and rediscover
        it.

        Verification is two COM reads - the control's name and its
        rectangle - against the ~1100 ms of a full look. Anything that
        moved, was renamed, or went away fails the check and the caller
        pays for a real look. So this can be stale in only the one way
        that does not matter: it cannot hand back something that is no
        longer there.
        """
        el = self._seen.get(key)
        if el is None:
            return None
        try:
            from core import locator
            if el.automation_id:
                # Verify by id, never by name. This page's search box is
                # named after a ROTATING ADVERT - the very reason `id`
                # exists as a selector - so a name check would reject the
                # same control every single iteration, which is exactly
                # what it did: 0 recalls out of 30 opportunities.
                if (el.control.AutomationId or "") != el.automation_id:
                    self._seen.pop(key, None)
                    return None
            elif (el.control.Name or "").strip() != el.name:
                self._seen.pop(key, None)
                return None
            r = locator._rect_of(el.control)
        except Exception:
            self._seen.pop(key, None)
            return None
        if not r or abs(r[0] - el.left) > 2 or abs(r[1] - el.top) > 2:
            self._seen.pop(key, None)
            return None
        self.recalled += 1
        return el

    def _ask(self, sel: Selector) -> tuple[bool, Element | None]:
        """Answer a selector with a provider-side query, or admit defeat.

        Only for selectors a UIA property condition can express EXACTLY -
        an `id`, or a `kind` plus a full name. That is not a limitation so
        much as the definition: a condition is an equality test, and
        anything fuzzier genuinely needs the tree.

        Worth the narrowness because the exact cases are the common ones
        and the expensive ones: measured on a live page, **19 ms against
        879 ms** for the same answer. Returning None here is always safe -
        the caller falls back to a full look.
        """
        hwnd = getattr(self.watcher, "hwnd", None)
        if not hwnd or sel.icon or sel.near:
            return False, None
        if sel.area == "content" and not getattr(self.watcher, "content", None):
            # No document rectangle learned yet. A full look will find one.
            return False, None
        if not query.available():
            return False, None

        kinds = (sel.kind,) if sel.kind else ()
        expressible = bool(sel.id) or (sel.match == "exact"
                                       and (sel.text or sel.any))
        hits: list[Element] = []
        if not expressible:
            if not (sel.kind and (sel.text or sel.any)):
                return False, None
            # A substring test is not a UIA condition, but "every control of
            # this kind" is - and for a rare kind that list is short, so the
            # substring can be applied here. MEASURED: Chrome's tab strip
            # sits past the full walk's 1500-node budget on a Migros basket
            # page, so the walk said "no tab named Migros" while two were
            # on screen. A list that hits the limit may be incomplete, and
            # then only the walk can answer.
            cap = 300
            every = query.find(hwnd, kinds=kinds, limit=cap)
            if len(every) >= cap:
                return False, None
            wants = [resolve.norm(w) for w in (sel.text, *sel.any) if w]
            hits = [e for e in every
                    if any(w in resolve.norm(e.name) for w in wants)]
        elif sel.id:
            hits = query.find(hwnd, aid=sel.id, kinds=kinds)
        else:
            for want in (sel.text, *sel.any):
                if not want:
                    continue
                hits = query.find(hwnd, name=want, kinds=kinds)
                if hits:
                    break
        if sel.area == "content":
            box = self.watcher.content
            hits = [e for e in hits
                    if box[0] <= e.center[0] <= box[2]
                    and e.center[1] >= box[1]]
        if sel.nth:
            # `nth` is reading order, the same rule `resolve` applies.
            hits.sort(key=lambda e: (e.top // 12, e.left))
            self.asked += 1
            return True, (hits[sel.nth - 1] if sel.nth <= len(hits) else None)
        if len(hits) > 1:
            # Genuine ambiguity. It must go through the scoring and the
            # ambiguity check in `resolve`, not be settled silently here.
            return False, None
        # Zero hits is a real ANSWER, not a failure to answer: a property
        # condition is exact, so "no element has this id" is conclusive.
        # Saying so is what makes a polling `wait_for` cheap - otherwise
        # every miss would fall back to the full walk it is avoiding.
        self.asked += 1
        return True, (hits[0] if hits else None)

    def _locate(self, sel: Selector, values: dict[str, str], where: str,
                clickable: bool = True, pick: Pick | None = None) -> Element:
        """Find one element, or fail the step explaining what was on screen."""
        live = self._sel(sel, values, where)
        key = (where, live, clickable)
        if live.nearest == "last":
            return self._nearest_last(live)
        if pick is None:
            # A `pick` step must see ALL the candidates, so neither the
            # remembered element nor the single-answer fast path applies.
            got = self._recall(key)
            if got is not None:
                return got
            handled, got = self._ask(live)
            if got is not None:
                self._remember(key, got)
                return got
        self._show("thinking" if pick is not None else "looking")
        scene = self.watcher.scene()
        res = resolve.find(scene, live, want_clickable=clickable)
        if not res.matches:
            near = ", ".join(sorted({e.name for e in scene.clickable()
                                     if e.name})[:8])
            raise StepFailed(
                f"no element matched {live.describe()}. "
                f"Visible: {near or '(nothing named)'}")
        if pick is not None:
            return self._pick(scene, res, pick, values, where)
        if res.ambiguous:
            raise StepFailed(
                f"{live.describe()} is ambiguous — {res.explain()}. "
                f"Add kind, near or nth.")
        self._remember(key, res.best)
        return res.best

    def _nearest_last(self, live: Selector) -> Element:
        """Of all matches, the one where the previous click landed.

        Deliberately NOT ambiguous: twenty "+" buttons on a page is the
        normal case, and the question is only which one replaced the button
        just clicked. Refuses beyond 300 px - past that it is some other
        card's control, and adding the wrong product is worse than failing.
        """
        if self._last_point is None:
            raise StepFailed(f"{live.describe()}: nothing has been clicked "
                             f"yet, so there is no 'last'")
        self._show("looking")
        from dataclasses import replace
        scene = self.watcher.scene(force=True)
        res = resolve.find(scene, replace(live, nearest=""),
                           want_clickable=True)
        lx, ly = self._last_point
        best, best_d = None, 1e9
        for m in res.matches:
            cx, cy = m.element.center
            d = ((cx - lx) ** 2 + (cy - ly) ** 2) ** 0.5
            if d < best_d:
                best, best_d = m.element, d
        if best is None or best_d > 300:
            raise StepFailed(f"no {live.describe()} within 300 px of the last "
                             f"click at {self._last_point}")
        return best

    def _pick(self, scene, res, pick: Pick, values: dict[str, str],
              where: str) -> Element:
        """Choose among equal candidates by what each one is FOR."""
        cands = [m.element for m in res.matches][:max(2, pick.max_options)]
        if len(cands) == 1:
            return cands[0]
        labelled = choose_mod.label_candidates(scene, cands, pick.radius)
        options = {x.key: x.label for x in labelled}
        goal = substitute(pick.goal, values, where)
        reject = substitute(pick.reject, values, where)

        if self._chooser is None:
            self._chooser = choose_mod.jev_chooser()
        try:
            key, margin = self._chooser(goal, reject, options)
        except choose_mod.NoChoice as exc:
            raise StepFailed(f"could not choose between {len(cands)} "
                             f"candidates: {exc}") from exc
        self.chose += 1
        if margin < pick.min_margin and pick.on_unsure == "fail":
            # Deliberately a failure, not a quiet fallback to the page's
            # first result. "I cannot tell these apart" and "the first is
            # right" are different answers, and only one is honest.
            ranked = ", ".join(f"{x.label[:34]!r}" for x in labelled[:3])
            raise StepFailed(
                f"no clear choice for {goal!r}: margin {margin:.2f} < "
                f"{pick.min_margin} over {len(cands)} candidates ({ranked}…)")
        chosen = next((x for x in labelled if x.key == key), None)
        if chosen is None:
            raise StepFailed(f"chooser returned unknown option {key!r}")
        self._last_pick = chosen.label
        return chosen.element

    def _await(self, sel: Selector, timeout: float,
               present: bool) -> Element | None | bool:
        """Poll until a selector matches (or stops matching), or time out.

        Forces a fresh look each round: the Watcher's fingerprint cache is
        exactly wrong here, because waiting IS waiting for a change.
        """
        deadline = time.time() + timeout
        while True:
            if self.should_stop():
                raise Stopped()
            # The fast path matters MORE here than anywhere else, because
            # this loop pays the cost once per poll rather than once per
            # step. A `wait_for` that missed twice used to cost two full
            # walks - over two seconds of the seven a product took.
            handled, quick = self._ask(sel)
            if handled:
                if present and quick is not None:
                    return quick
                if not present and quick is None:
                    return True
            else:
                scene = (self.watcher.text_scene() if _text_only(sel)
                         else self.watcher.scene(force=True))
                res = resolve.find(scene, sel)
                if present and res.matches:
                    return res.best
                if not present and not res.matches:
                    return True
            if time.time() >= deadline:
                return None if present else False
            time.sleep(POLL_S)

    # -- the guard ---------------------------------------------------------

    def _gate(self, el: Element, where: str) -> None:
        """Stop before anything irreversible, unless the run is trusted.

        Two layers, in this order and no other: the word list decides
        WHETHER this is a commitment, and only then is a human asked. A
        model is nowhere in this path, on purpose.
        """
        word = guard.is_commit(el.name, el.value, el.automation_id)
        if not word:
            return
        trusted = (self.honour_trust and self.r.trust == Trust.TRUSTED
                   and not self.r.imported)
        if trusted:
            return
        if self.authorized:
            # The person wrote the go-ahead in their own chat, after being
            # shown the basket. Recorded on the step, never silent.
            return
        if self.confirm is None:
            raise StepFailed(
                f"{el.name!r} looks like a commitment ({word!r}) and this run "
                f"is {self.r.trust.value}"
                + (" with imported scripts" if self.r.imported else "")
                + " — no confirmation handler is wired up, so it was not "
                  "clicked")
        self._show("blocked", el)
        if not self.confirm(word, el):
            raise StepFailed(f"{el.name!r} ({word!r}) was refused")

    # -- actions -----------------------------------------------------------

    def _do_click(self, step, values, where, depth) -> str:
        el = self._locate(step.target, values, where, pick=step.pick)
        self._show("moving", el)
        self._gate(el, where)
        self._show("click", el)
        if self.dry_run:
            return f"[dry] click {el.name!r} at {el.center}"
        if not self.act.click(el):
            raise StepFailed(f"click on {el.name!r} did not go through")
        self._last_point = el.center
        word = guard.is_commit(el.name, el.value, el.automation_id)
        if word and self.authorized:
            return (f"clicked {el.name!r} at {el.center} (commitment "
                    f"{word!r}, authorized by {self.authorized!r} in chat)")
        return f"clicked {el.name!r} at {el.center}"

    def _do_double_click(self, step, values, where, depth) -> str:
        el = self._locate(step.target, values, where, pick=step.pick)
        self._show("moving", el)
        self._gate(el, where)
        self._show("click", el)
        if self.dry_run:
            return f"[dry] double-click {el.name!r}"
        if not self.act.double_click(el):
            raise StepFailed(f"double-click on {el.name!r} did not go through")
        return f"double-clicked {el.name!r}"

    def _do_right_click(self, step, values, where, depth) -> str:
        el = self._locate(step.target, values, where, pick=step.pick)
        self._show("moving", el)
        self._gate(el, where)
        self._show("click", el)
        if self.dry_run:
            return f"[dry] right-click {el.name!r}"
        if not self.act.click(el, button="right"):
            raise StepFailed(f"right-click on {el.name!r} did not go through")
        return f"right-clicked {el.name!r}"

    def _do_type(self, step, values, where, depth) -> str:
        # No `into` means "wherever the caret already is" — the normal case
        # straight after ctrl+l or a Tab, where naming the field would be
        # both noise and one more thing that can fail to resolve.
        el = (self._locate(step.into, values, where)
              if step.into is not None else None)
        self._show("type", el)
        text = substitute(step.value, values, where)
        target = repr(el.name) if el else "the focused control"
        if step.lines_from == "picks":
            return self._type_lines(text, el, target, step)
        if self.dry_run:
            return f"[dry] type {text!r} into {target}"
        if not self.act.type_text(text, el):
            raise StepFailed(f"typing into {target} did not go through")
        out = f"typed {text!r} into {target}"
        if step.then_key:
            # Saves a whole step-and-look on the commonest pair there is.
            if not self.act.press_key(step.then_key):
                raise StepFailed(f"{step.then_key} after typing failed")
            out += f" then {step.then_key}"
        return out

    def _type_lines(self, header: str, el, target: str, step) -> str:
        """Header, then one line per thing bought, as ONE chat message.

        shift+enter between lines: a chat box sends on a bare enter, which
        would turn a 25-line list into 25 messages.
        """
        lines = [header] if header else []
        for n, (label, qty) in enumerate(self.report.bought.items(), 1):
            # "n)" not "n.": a chat app renders "13. x" as its own numbered
            # list, and the message came out as "13. 13. x".
            lines.append(f"{n}) {label}" + (f"  x{qty}" if qty > 1 else ""))
        if len(lines) == (1 if header else 0):
            lines.append("(nothing was added)")
        if self.dry_run:
            return f"[dry] type {len(lines)} lines into {target}"
        for i, line in enumerate(lines):
            if not self.act.type_text(line, el if i == 0 else None):
                raise StepFailed(f"typing line {i + 1} into {target} failed")
            if i < len(lines) - 1 and not self.act.press_key("shift+enter"):
                raise StepFailed("shift+enter between lines failed")
        if step.then_key and not self.act.press_key(step.then_key):
            raise StepFailed(f"{step.then_key} after the list failed")
        return f"typed {len(lines)} lines into {target}"

    def _do_key(self, step, values, where, depth) -> str:
        self._show("type")
        keys = substitute(step.keys, values, where)
        if self.dry_run:
            return f"[dry] press {keys}"
        if not self.act.press_key(keys):
            raise StepFailed(f"{keys} did not go through")
        return f"pressed {keys}"

    def _do_drag(self, step, values, where, depth) -> str:
        a = self._locate(step.target, values, where)
        b = self._locate(step.to, values, where, clickable=False)
        if self.dry_run:
            return f"[dry] drag {a.name!r} to {b.name!r}"
        if not self.act.drag(a, b):
            raise StepFailed(f"drag {a.name!r} → {b.name!r} did not go through")
        return f"dragged {a.name!r} to {b.name!r}"

    def _do_scroll(self, step, values, where, depth) -> str:
        el = None
        if step.target is not None:
            try:
                el = self._locate(step.target, values, where, clickable=False)
            except StepFailed:
                el = None  # scrolling the focused pane is a fine fallback
        if self.dry_run:
            return f"[dry] scroll {step.direction} {step.amount}"
        if not self.act.scroll(el, step.direction, step.amount):
            raise StepFailed("scroll did not go through")
        return f"scrolled {step.direction} {step.amount}"

    def _do_move(self, step, values, where, depth) -> str:
        el = self._locate(step.target, values, where, clickable=False)
        if self.dry_run:
            return f"[dry] move to {el.name!r}"
        if not self.act.move(el):
            raise StepFailed(f"moving to {el.name!r} did not go through")
        return f"moved to {el.name!r}"

    # -- reaching the thing to act on --------------------------------------

    def _settle_on(self, hwnd: int, step: Step, where: str) -> str:
        """Focus a window and point the Watcher at it.

        Re-pointing is the step people forget. `look()` reads ONE window's
        accessibility tree, so a script that opened Chrome and then searched
        the scene would still be reading the window it started in — finding
        nothing, or worse, finding a stale match and clicking it.
        """
        if not self.act.focus_window(hwnd, step.maximize):
            raise StepFailed(f"could not bring window {hwnd} to the front")
        # Tell the actuator which window keystrokes are meant for.
        if hasattr(self.act, "target_hwnd"):
            self.act.target_hwnd = hwnd
        self.watcher.hwnd = hwnd
        # The cached scene belongs to the old window. Keeping it would hand
        # the next step a fingerprint that happens to match.
        self.watcher._scene = None
        self.watcher._fp = ""
        return f"focused window {hwnd}"

    def _await_window(self, title: str, timeout: float) -> int:
        """Wait for a window whose title contains `title`.

        An EMPTY title means "whatever is in front", not "the first window
        on the desktop" — a substring test against "" matches everything,
        so the obvious implementation would silently pick some unrelated
        window and drive it. `{title}` substituting to nothing is the
        normal case for a shortcut whose caller did not care, so this has
        to be right rather than merely guarded.
        """
        if not title:
            return self.act.foreground_window()
        deadline = time.time() + timeout
        while True:
            if self.should_stop():
                raise Stopped()
            got = self.act.find_window(title)
            if got:
                return got
            if time.time() >= deadline:
                return 0
            time.sleep(POLL_S)

    def _do_open(self, step, values, where, depth) -> str:
        app = substitute(step.app, values, where)
        url = substitute(step.url, values, where)
        if url:
            _check_url(url, where)        # a substituted url is checked now
            url = normalise_url(url)
        window = substitute(step.window, values, where)
        if step.reuse and window:
            already = self.act.find_window(window)
            if already:
                # The window you already have is the one holding your
                # session and your cookies; a second one starts from
                # nothing and leaves the first behind.
                if self.dry_run:
                    return f"[dry] reuse {window!r} (hwnd {already})"
                return (f"reused the open {window!r}; "
                        f"{self._settle_on(already, step, where)}")

        if self.dry_run:
            return f"[dry] open {app or 'default browser'} {url}".strip()

        detail = self.act.launch(app, url)

        if not window:
            # Nothing to wait for, so wait a little anyway: a launch returns
            # the moment the process starts and the next step would act on
            # the old screen. Then re-point at whatever came up, or the
            # scene would still be the window we just left.
            time.sleep(1.5)
            front = self.act.foreground_window()
            if front:
                return f"{detail}; {self._settle_on(front, step, where)}"
            return detail
        hwnd = self._await_window(window, step.timeout)
        if not hwnd:
            raise StepFailed(
                f"{detail}, but no window titled like {window!r} appeared "
                f"within {step.timeout}s")
        return f"{detail}; {self._settle_on(hwnd, step, where)} ({window!r})"

    def _do_focus(self, step, values, where, depth) -> str:
        window = substitute(step.window, values, where)
        hwnd = self._await_window(window, step.timeout)
        if not hwnd:
            raise StepFailed(f"no window titled like {window!r} within "
                             f"{step.timeout}s")
        if self.dry_run:
            return f"[dry] focus {window!r} (hwnd {hwnd})"
        return f"{self._settle_on(hwnd, step, where)} ({window!r})"

    # -- waiting and checking ----------------------------------------------

    def _do_wait(self, step, values, where, depth) -> str:
        self._show("waiting")
        # Slept in slices so a stop request is honoured within a poll, not
        # after the full sleep.
        deadline = time.time() + step.seconds
        while time.time() < deadline:
            if self.should_stop():
                raise Stopped()
            time.sleep(min(POLL_S, max(0.0, deadline - time.time())))
        return f"waited {step.seconds}s"

    def _do_wait_for(self, step, values, where, depth) -> str:
        sel = self._sel(step.target, values, where)
        got = self._await(sel, step.timeout, present=True)
        if got is None:
            raise StepFailed(f"{sel.describe()} did not appear within "
                             f"{step.timeout}s")
        return f"{sel.describe()} appeared"

    def _chat_text(self) -> list[str] | None:
        """Every text line in the document area, read from pixels - or None
        when the chat is not what is on screen.

        A chat's messages carry no accessible names on WhatsApp Web - only
        the bubbles' media buttons do - so OCR is the only reader there is.
        And OCR reads the SCREEN, not the window: the first live wait fired
        a false "cancel" because a terminal came to the front and its text
        said 'or "cancel" to stop'. So a frame counts only while the chat's
        own window is in front; anything else is skipped, not counted.
        """
        hwnd = getattr(self.watcher, "hwnd", 0)
        if hwnd and self.act.foreground_window() != hwnd:
            return None
        sc = self.watcher.text_scene()
        box = self.watcher.content
        out = []
        for e in sc.elements:
            if box and not (box[0] <= e.center[0] <= box[2]
                            and box[1] <= e.center[1] <= box[3]):
                continue
            if e.name.strip():
                out.append(e.name.strip())
        return out

    def _tally_chat(self, lines: list[str], words: tuple[str, ...]
                    ) -> tuple[dict[str, int], dict[str, int]]:
        import re
        counts = {w: 0 for w in words}
        codes: dict[str, int] = {}
        for line in lines:
            low = resolve.norm(line)
            for w in words:
                pat = (r"(?<![a-z0-9])" + re.escape(resolve.norm(w))
                       + r"(?![a-z0-9])")
                if re.search(pat, low):
                    counts[w] += 1
            for c in re.findall(r"(?<![\d:/.,])\d{4,8}(?![\d:/.,])",
                                line.replace(" ", "")):
                codes[c] = codes.get(c, 0) + 1
        return counts, codes

    def _do_wait_message(self, step, values, where, depth) -> str:
        """Wait for a NEW message in the chat on screen.

        "New" is the whole difficulty: the chat already holds this run's
        own messages and yesterday's, and a wait that matched any "buy" on
        screen would fire on history. So the chat is counted when the wait
        starts, and only an INCREASE counts - the word appearing once more
        than it did. A code is any 4-8 digit run that was not there before.
        """
        words = tuple(substitute(w, values, where) for w in step.words)
        stops = tuple(substitute(w, values, where) for w in step.stop_words)
        watch = words + tuple(w for w in stops if w not in words)
        self._show("waiting")
        first = self._chat_text()
        if first is None:
            raise StepFailed("the chat is not in front, so it cannot be read")
        base_w, base_c = self._tally_chat(first, watch)
        deadline = time.time() + step.timeout
        while True:
            if self.should_stop():
                raise Stopped()
            time.sleep(1.0)
            if time.time() >= deadline:
                raise StepFailed(f"no {list(words) or 'code'} within "
                                 f"{step.timeout:g}s")
            lines = self._chat_text()
            if lines is None:
                continue              # someone else's window is in front
            now_w, now_c = self._tally_chat(lines, watch)
            for w in stops:
                if now_w[w] > base_w[w]:
                    self._record(Outcome(where, step.do,
                                         f"{w!r} arrived - stopping in "
                                         f"{step.stop_after:g}s", True, 0))
                    time.sleep(step.stop_after)
                    raise Stopped(f"{w!r} in the chat")
            for w in words:
                if now_w[w] > base_w[w]:
                    if step.authorizes:
                        self.authorized = w
                    return (f"{w!r} arrived"
                            + (" - commitments now authorized"
                               if step.authorizes else ""))
            if step.code_as:
                fresh = [c for c, n in now_c.items() if n > base_c.get(c, 0)]
                if fresh:
                    values[step.code_as] = fresh[-1]
                    return f"code arrived ({len(fresh[-1])} digits)"
            # A new message can push old ones out of view and LOWER a count;
            # re-base on the lower number so the next arrival still counts.
            base_w = {w: min(base_w[w], now_w[w]) for w in watch}
            base_c = {c: min(n, now_c.get(c, 0)) for c, n in base_c.items()}
            if time.time() >= deadline:
                raise StepFailed(f"no {list(words) or 'code'} within "
                                 f"{step.timeout:g}s")

    def _do_assist(self, step, values, where, depth) -> str:
        """Reach a goal on a page nobody scripted, a few steps at a time.

        Each round: screenshot and element list to the helper, a few steps
        back, run through `_exec` (guard, stop and checks all apply), and
        what happened goes into the next round. Ends when the helper says
        the goal is met, or fails after `rounds`.
        """
        if self.helper is None:
            raise StepFailed("assist needs a helper model and none is wired")
        goal = substitute(step.goal, values, where)
        history: list[str] = []
        for r in range(step.rounds):
            if self.should_stop():
                raise Stopped()
            self._show("thinking")
            plan = self.helper.rescue(goal=goal, error="",
                                      scene=self._scene_rows(),
                                      history=history)
            if plan.get("done"):
                return (f"goal met after {r} round(s): "
                        f"{str(plan.get('why', ''))[:160]}")
            steps = self.helper.parse(plan, where)
            if not steps:
                history.append(f"round {r + 1}: no usable steps "
                               f"({str(plan.get('why', ''))[:120]})")
                continue
            self._rescuing = True
            try:
                for i, child in enumerate(steps):
                    try:
                        self._exec(child, values,
                                   f"{where}.round{r + 1}[{i}]", depth)
                        history.append(
                            f"round {r + 1} ok: "
                            f"{self.report.outcomes[-1].detail[:140]}")
                    except StepFailed as exc:
                        history.append(f"round {r + 1} FAILED {child.do}: "
                                       f"{str(exc)[:160]}")
                        break
            finally:
                self._rescuing = False
        raise StepFailed(f"goal not reached in {step.rounds} rounds: {goal}")

    def _do_expect(self, step, values, where, depth) -> str:
        sel = self._sel(step.target, values, where)
        got = self._await(sel, step.timeout, present=True)
        if got is None:
            raise StepFailed(f"expected {sel.describe()}, not on screen")
        return f"confirmed {sel.describe()}"

    def _do_gone(self, step, values, where, depth) -> str:
        sel = self._sel(step.target, values, where)
        if self._await(sel, step.timeout, present=False) is False:
            raise StepFailed(f"{sel.describe()} is still on screen")
        return f"{sel.describe()} is gone"

    # -- control flow ------------------------------------------------------

    def _do_if(self, step, values, where, depth) -> str:
        sel = self._sel(step.present, values, where)
        scene = self.watcher.scene()
        there = bool(resolve.find(scene, sel).matches)
        branch = step.then if there else step.otherwise
        label = "then" if there else "else"
        for i, child in enumerate(branch):
            self._exec(child, values, f"{where}.{label}[{i}]", depth)
        return (f"{sel.describe()} {'is' if there else 'is not'} present "
                f"→ {label} ({len(branch)} steps)")

    def _do_repeat(self, step, values, where, depth) -> str:
        items = ([substitute(v, values, where) for v in step.for_each]
                 if step.for_each else [str(i + 1) for i in range(step.times)])
        name = step.as_name or "item"
        done = 0
        for n, item in enumerate(items):
            if self.should_stop():
                raise Stopped()
            scope = dict(values)
            scope[name] = item
            self._last_pick = None
            try:
                for i, child in enumerate(step.steps):
                    self._exec(child, scope, f"{where}[{item}].steps[{i}]",
                               depth)
            except StepFailed:
                # THE failure policy. One product's search failed; the next
                # product starts from its own search, so the basket cannot
                # be corrupted by carrying on.
                self.report.items[item] = False
                continue
            self.report.items[item] = True
            if self._last_pick:
                self.report.picks[item] = self._last_pick
            done += 1
        return f"{done}/{len(items)} iterations completed"

    def _do_call(self, step, values, where, depth) -> str:
        if depth + 1 > 8:
            raise StepFailed("call nested too deep")
        target = self.r.get(step.script)
        # No scope inheritance: a called script sees its `with:` and nothing
        # else. Checked at resolve time too, so this is belt and braces.
        scope = {k: substitute(v, values, where)
                 for k, v in step.with_args.items()}
        self._run_script(target, scope, depth + 1)
        return f"called {step.script} with {scope}"


# --------------------------------------------------------------------------
# The real actuator
# --------------------------------------------------------------------------

class DesktopActuator:
    """Binds the runner to the agent's input layer.

    Prefers a UIA pattern invoke over a coordinate click wherever the
    element carries a live control: it works on an occluded window, does
    not move the cursor, and cannot land on whatever happens to be on top.
    """

    def __init__(self) -> None:
        import sys
        from pathlib import Path
        agent = Path(__file__).resolve().parents[2] / "agent"
        if str(agent) not in sys.path:
            sys.path.insert(0, str(agent))
        from core import actuator, locator
        self.a = actuator
        self.loc = locator
        #: The window the runner is driving. Injected keystrokes go to
        #: whatever has focus, NOT to a window you name - so without this
        #: a focus change mid-run types into whatever the person is using.
        #: That happened here, twice, during testing.
        self.target_hwnd = 0

    # -- reaching ----------------------------------------------------------

    def launch(self, app: str, url: str) -> str:
        """Start an app, open a URL, or open a URL *in* a named app.

        App names come from `machine_profile`'s resolved symbol table —
        the same one the agent's `open_app` tool uses — so `chrome` works
        whether Chrome is on PATH, in App Paths, or only reachable through
        its Start-menu AppUserModelID.
        """
        import subprocess

        from core import noconsole
        from core.machine_profile import load_profile

        flags = noconsole.flags()
        target = ""
        if app:
            prof = load_profile()
            target = prof["symbols"].get(f"APP:{app.lower()}", "")
            if not target:
                raise StepFailed(
                    f"{app!r} is not installed, or not found. Available: "
                    f"{', '.join(sorted(prof['capabilities']))}")

        if target.startswith("shell:"):
            # A Store app reached through its AppUserModelID cannot be
            # handed a URL on the command line; open the app, then the URL.
            subprocess.Popen(["explorer.exe", target], creationflags=flags)
            if url:
                time.sleep(2.0)
                subprocess.Popen(["explorer.exe", url], creationflags=flags)
            return f"launched {app}" + (f" and opened {url}" if url else "")
        if target:
            cmd = [target] + ([url] if url else [])
            subprocess.Popen(cmd, creationflags=flags)
            return f"launched {app}" + (f" with {url}" if url else "")
        # No app named: hand the URL to whatever the user has set as
        # default, through explorer rather than os.startfile so the child
        # is not tied to this process's lifetime.
        subprocess.Popen(["explorer.exe", url], creationflags=flags)
        return f"opened {url} in the default browser"

    def find_window(self, title: str) -> int:
        # An empty substring matches every window, so refuse it here too
        # rather than relying on the one caller that currently guards it.
        if not title:
            return 0
        return self.loc.find_window(title_contains=title) or 0

    def foreground_window(self) -> int:
        return int(self.loc.foreground_window() or 0)

    def focus_point(self) -> tuple[int, int] | None:
        """Where a keystroke will land: caret, else focused control."""
        try:
            return self.a.focus_point()
        except Exception:
            return None

    def focus_window(self, hwnd: int, maximize: bool = True) -> bool:
        ok = bool(self.a.focus_window(hwnd))
        if ok and maximize:
            try:
                self.a.focus_window(hwnd)
                self.loc.user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
            except Exception:
                pass
        return ok

    # -- acting ------------------------------------------------------------

    def _invoke(self, el: Element) -> bool:
        if el.control is None:
            return False
        try:
            shim = type("E", (), {"control": el.control})()
            ok, _ = self.a._try_invoke(shim)
            return bool(ok)
        except Exception:
            return False

    def _virtual_screen(self) -> tuple[int, int, int, int]:
        g = self.loc.user32.GetSystemMetrics
        x, y = g(76), g(77)                      # SM_X/YVIRTUALSCREEN
        return (x, y, x + g(78), y + g(79))      # + SM_CX/CYVIRTUALSCREEN

    def _reach(self, el: Element) -> tuple[int, int] | None:
        """Where to click, scrolling into view first if it is off screen.

        UIA reports elements that are scrolled out of sight — measured on a
        live Migros page, 130 of 319 were below the fold — and it reports
        their VIRTUAL coordinates, which can be past the bottom of the
        monitor. Invoking such an element works fine; injecting a click at
        y=1896 on an 1800px screen does not. It lands on nothing, or on
        another monitor, and reports success either way.

        So a coordinate click on an off-screen element scrolls first and
        re-reads the rectangle, and gives up rather than guessing.
        """
        x, y = el.center
        if el.control is not None:
            # Re-read the rectangle. The look that produced this element
            # took ~1.1 s, during which a page lazy-loads images and
            # reflows - so the cached centre can point at whatever moved
            # into that space. A UIA pattern call does not care, because it
            # holds a live handle; a COORDINATE does, and this is the cost
            # of preferring coordinates. One COM read, about 1 ms.
            try:
                r = self.loc._rect_of(el.control)
                if r:
                    x, y = (r[0] + r[2]) // 2, (r[1] + r[3]) // 2
            except Exception:
                pass
        left, top, right, bottom = self._virtual_screen()
        if left <= x < right and top <= y < bottom:
            return (x, y)
        if el.control is not None:
            try:
                el.control.GetScrollItemPattern().ScrollIntoView()
                time.sleep(0.25)
                r = self.loc._rect_of(el.control)
                if r:
                    x, y = (r[0] + r[2]) // 2, (r[1] + r[3]) // 2
                    if left <= x < right and top <= y < bottom:
                        return (x, y)
            except Exception:
                pass
        return None

    def _uncovered(self, point: tuple[int, int],
                   el: Element | None = None) -> bool:
        """Would a click at this point actually reach `el`?

        Checking the WINDOW is not enough, and that cost three products in
        a ten-product run. Migros floats a support-chat bubble over the
        results grid; a click injected at the first add button's centre
        hit the overlay instead. Same window, so a window-level check
        passed happily - and the click reported success while adding
        nothing. Measured, twice, on two different products:

            injected click at (1164, 1522)   steppers 0 -> 0
            UIA Invoke on the same element   steppers 0 -> 1

        So ask UIA what is actually at the point. A hit on `el` itself or
        on something inside it is fine - the centre of a button often
        belongs to the icon drawn in it. Anything else means covered, and
        the caller should invoke instead of clicking blind.
        """
        try:
            at = self.a._root_owner(self.a._window_at(*point))
            if not at or not self.a.is_foreground(at):
                return False
        except Exception:
            return False
        if el is None:
            return True
        if el.control is None:
            # Found by OCR: the pixels ARE what is on top, so there is
            # nothing to be covered by. And the containment test below
            # cannot apply - the control under a label (a tab, a button) is
            # always bigger than the label's text box, so it failed every
            # pixel-only element: clicking the AGI Agent window's own
            # "Settings" tab silently did nothing.
            return True
        try:
            import uiautomation as auto
            hit = auto.ControlFromPoint(*point)
            if hit is None:
                return False
            r = hit.BoundingRectangle
            # Ours, or nested inside ours.
            return (r.left >= el.left - 2 and r.top >= el.top - 2
                    and r.right <= el.left + el.width + 2
                    and r.bottom <= el.top + el.height + 2)
        except Exception:
            # Cannot tell. The safe answer is the slow one.
            return False

    def click(self, el: Element, button: str = "left") -> bool:
        """Click, preferring real input over a UIA pattern.

        **Speed.** Invoking is not free. On this page an `Expand()` on the
        search box measured **507 ms** against **7 ms** for an injected
        click: a pattern call runs inside the application and blocks until
        the app has finished reacting.

        **Correctness.** A combobox has no Invoke pattern, so the pattern
        ladder fell through to ExpandCollapse and called `Expand()`. That
        opens the suggestion list. It is not what "click" means, and it
        left the caret somewhere the script had not asked for. It worked
        by luck.

        The safety the pattern path bought - cannot land on the wrong
        window, works occluded - is bought here instead by checking the
        point is on screen (`_reach`) and that our window is the one under
        it (`_uncovered`). When either is false the pattern path is still
        there, and still right.
        """
        point = self._reach(el)
        if (point is not None and self.target_hwnd
                and not self.a.is_foreground(self.target_hwnd)):
            # Something else took the foreground - the person's terminal, a
            # notification. The fallback below is a UIA Invoke, which on
            # WhatsApp's chat rows reports success and does NOTHING; that
            # is exactly how "Message yourself" failed. Take the window
            # back first, then decide how to click.
            self.a.focus_window(self.target_hwnd)
            time.sleep(0.3)
            point = self._reach(el)
        if point is not None and self._uncovered(point, el):
            if self.a.click_injected(*point, button):
                return True
        if button == "left" and self._invoke(el):
            return True
        return False

    def double_click(self, el: Element) -> bool:
        point = self._reach(el)
        if point is None:
            return False
        first = self.a.click_injected(*point)
        time.sleep(0.06)
        return bool(first and self.a.click_injected(*point))

    def type_text(self, text: str, el: Element | None) -> bool:
        if not self._ours():
            return False
        if el is not None and not self._focus(el):
            return False
        return bool(self.a.type_text(text).ok)

    def _ours(self) -> bool:
        """Is the window we are driving the one that will receive input?

        A click can be aimed at a coordinate; a KEYSTROKE cannot. It goes
        to whatever holds focus, so the only safe check is to ask, right
        before typing, whether that is still us. One re-focus attempt,
        then refuse - a step that fails is recoverable, a paragraph typed
        into somebody's terminal is not.
        """
        if not self.target_hwnd:
            # Nothing claimed means nobody knows where these keys go. This
            # used to answer "caller's risk" - and a scouting call whose
            # window lookup returned None typed a URL into the person's
            # chat box, twice. An `open` or `focus` claims a window first.
            return False
        if self._same_app_in_front():
            return True
        self.a.focus_window(self.target_hwnd)
        time.sleep(0.25)
        return self._same_app_in_front()

    def _same_app_in_front(self) -> bool:
        """Our window, or a popup of the same program (Chrome's tab search
        is its own top-level window). Another program - the person's
        terminal - is never ours."""
        if self.a.is_foreground(self.target_hwnd):
            return True
        import ctypes
        u = self.loc.user32
        fg = u.GetForegroundWindow()
        if not fg:
            return False
        a, b = ctypes.c_ulong(), ctypes.c_ulong()
        u.GetWindowThreadProcessId(fg, ctypes.byref(a))
        u.GetWindowThreadProcessId(self.target_hwnd, ctypes.byref(b))
        return bool(a.value) and a.value == b.value

    def _focus(self, el: Element) -> bool:
        try:
            if el.control is not None:
                el.control.SetFocus()
                return True
        except Exception:
            pass
        point = self._reach(el)
        return bool(point and self.a.click_injected(*point))

    def press_key(self, key: str) -> bool:
        if not self._ours():
            return False
        fn = self.a.send_keys if "+" in key else self.a.press_key
        return bool(fn(key).ok)

    def drag(self, a: Element, b: Element) -> bool:
        return bool(self.a.drag(_Shim(a), _Shim(b)).ok)

    def scroll(self, el: Element | None, direction: str, amount: int) -> bool:
        return bool(self.a.scroll(_Shim(el) if el else None,
                                  direction, amount).ok)

    def move(self, el: Element) -> bool:
        # Built from the SendInput primitives rather than reported as a
        # success: `core.actuator` has no move_to, and a `move` that always
        # returns True would make a hover step silently do nothing.
        point = self._reach(el)
        if point is None:
            return False
        nx, ny = self.a._norm(*point)
        flags = (self.a.MOUSEEVENTF_MOVE | self.a.MOUSEEVENTF_ABSOLUTE
                 | self.a.MOUSEEVENTF_VIRTUALDESK)
        return bool(self.a._send([self.a._mouse(nx, ny, flags)]))


class _Shim:
    """Adapts a pipeline Element to what `core.actuator` expects."""

    def __init__(self, el: Element) -> None:
        self.control = el.control
        self.hwnd = 0
        self._c = el.center
        self.name = el.name

    def click_point(self) -> tuple[int, int]:
        return self._c
