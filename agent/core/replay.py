"""Replay — run a macro, verify each step, improvise when a locator dies.

The improvisation ladder from the plan, bounded at every rung:

  1. next locator in the stack           (all locators)
  2. re-resolve after a wait             (5 attempts — UI may still be settling)
  3. same window, same intent, alt route (5 attempts)
  4. hand to the caller with the intent  (a model can solve from intent)
  5. stop, summarise, ask

Two properties matter more than the ladder itself:

  * every step's outcome updates locator statistics, so the stack reorders
    itself as an application drifts — that is the learning
  * the arbiter is consulted before EVERY action, so a user touching the
    machine pauses the run rather than fighting it
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from . import act, locator
from .arbiter import Arbiter, Control
from .machine_profile import MissingSymbol, check_requirements, resolve
from .schema import LocalState, Macro, Step, Trust
from .store import Store, order_for, promote


@dataclass
class StepOutcome:
    index: int
    intent: str
    ok: bool
    locator_index: int = -1
    attempts: int = 1
    improvised: bool = False
    fell_back: bool = False
    verified: bool | None = None
    elapsed_s: float = 0.0
    error: str = ""

    def line(self) -> str:
        mark = "ok  " if self.ok else "FAIL"
        bits = [f"{mark} {self.index + 1:>2}. {self.intent[:56]}"]
        if self.locator_index >= 0:
            bits.append(f"loc[{self.locator_index}]")
        if self.attempts > 1:
            bits.append(f"x{self.attempts}")
        if self.improvised:
            bits.append("improvised")
        if self.fell_back:
            bits.append("invoke->input")
        bits.append(f"{self.elapsed_s * 1000:.0f}ms")
        if self.error:
            bits.append(f"— {self.error}")
        return "  ".join(bits)


@dataclass
class RunResult:
    macro_id: str
    ok: bool
    outcomes: list[StepOutcome] = field(default_factory=list)
    elapsed_s: float = 0.0
    stopped_reason: str = ""
    trust_before: str = ""
    trust_after: str = ""
    needs_user: str = ""

    def report(self) -> str:
        lines = [o.line() for o in self.outcomes]
        head = f"{'COMPLETED' if self.ok else 'STOPPED'} — {len(self.outcomes)} steps, {self.elapsed_s:.1f}s"
        if self.stopped_reason:
            head += f"  ({self.stopped_reason})"
        return head + "\n" + "\n".join("  " + l for l in lines)


class Replayer:
    RESOLVE_ATTEMPTS = 5
    ALT_ATTEMPTS = 5
    SETTLE_S = 0.6

    def __init__(self, store: Store | None = None,
                 on_event: Callable[[str, dict], None] | None = None):
        self.store = store or Store()
        self.on_event = on_event or (lambda kind, data: None)

    def _emit(self, kind: str, **data) -> None:
        try:
            self.on_event(kind, data)
        except Exception:
            pass

    # -- pre-flight ------------------------------------------------------
    def preflight(self, macro: Macro, params: dict | None = None) -> list[str]:
        """Everything that can be known before touching the desktop."""
        problems: list[str] = []
        ok, missing = check_requirements(macro.requires_symbols,
                                         macro.requires_capabilities)
        if not ok:
            problems += [f"missing: {m}" for m in missing]

        supplied = set((params or {}).keys())
        for p in macro.params:
            if p.default is None and p.name not in supplied:
                problems.append(f"parameter required: {p.name}")
        return problems

    # -- run -------------------------------------------------------------
    def run(self, macro_id: str, params: dict | None = None,
            arbiter: Arbiter | None = None,
            supervised: bool | None = None) -> RunResult:
        macro, local = self.store.load(macro_id)
        params = params or {}
        t0 = time.time()
        res = RunResult(macro_id=macro_id, ok=False,
                        trust_before=local.trust.value)

        problems = self.preflight(macro, params)
        if problems:
            res.stopped_reason = "; ".join(problems)
            res.trust_after = local.trust.value
            self._emit("preflight_failed", problems=problems)
            return res

        if supervised is None:
            supervised = local.trust is not Trust.TRUSTED

        own_arbiter = arbiter is None
        arb = arbiter or Arbiter()
        if own_arbiter and not arb.start():
            res.stopped_reason = f"cannot take control: {arb.state.detail}"
            res.trust_after = local.trust.value
            return res

        try:
            self._emit("run_start", macro=macro.name, steps=len(macro.steps),
                       supervised=supervised, trust=local.trust.value)

            for i, step in enumerate(macro.steps):
                st = arb.check()
                if st.control is Control.ABORTED:
                    res.stopped_reason = "aborted by user (ESC+ESC)"
                    break
                if st.control is Control.YIELDED:
                    res.stopped_reason = "user took control"
                    break
                if st.control is Control.BLOCKED:
                    res.stopped_reason = f"blocked: {st.detail}"
                    break

                arb.note(step.intent[:60])
                out = self._do(step, i, local, params, supervised)
                res.outcomes.append(out)
                self._emit("step", index=i, intent=step.intent, ok=out.ok,
                           detail=out.line())

                if not out.ok and not step.optional:
                    res.stopped_reason = out.error or "step failed"
                    res.needs_user = (
                        f"Step {i + 1} of {len(macro.steps)} could not be completed: "
                        f"{step.intent}. Tried {out.attempts} approaches. "
                        f"Last error: {out.error}")
                    break
            else:
                res.ok = True

            # -- statistics and trust ------------------------------------
            local.stats.runs += 1
            local.stats.last_run = time.time()
            if res.ok:
                local.stats.successes += 1
                local.stats.last_ok = time.time()
            else:
                local.stats.failures += 1
            if any(o.improvised for o in res.outcomes):
                local.stats.improvisations += 1

            dur = time.time() - t0
            prev = local.stats.mean_duration_s
            local.stats.mean_duration_s = (
                dur if prev is None else round((prev * (local.stats.runs - 1) + dur)
                                               / local.stats.runs, 2))
            promote(local)
            self.store.save_local(local)

            res.trust_after = local.trust.value
            res.elapsed_s = dur
            self._emit("run_end", ok=res.ok, reason=res.stopped_reason,
                       trust=local.trust.value)
            return res
        finally:
            if own_arbiter:
                arb.stop()

    # -- a single step, with the ladder ----------------------------------
    def _do(self, step: Step, index: int, local: LocalState,
            params: dict, supervised: bool) -> StepOutcome:
        t0 = time.time()
        out = StepOutcome(index=index, intent=step.intent, ok=False)

        scope = None
        title = step.args.get("window")
        if title:
            try:
                scope = locator.find_window(title_contains=resolve(str(title),
                                                                  params=params))
            except MissingSymbol:
                scope = None

        order = order_for(local, index, len(step.locators))

        # rung 1 + 2: the stack, then re-resolve after settling
        for attempt in range(1, self.RESOLVE_ATTEMPTS + 1):
            out.attempts = attempt
            r = act.do_step(step, scope, order=order, params=params)

            for (li, _ltype, hit, _dt) in r.tried:
                local.record(index, li, hit)

            if r.ok:
                out.ok = True
                out.locator_index = r.locator_index
                out.verified = r.verified
                out.fell_back = getattr(r, "fell_back", False)
                out.elapsed_s = time.time() - t0
                return out

            out.error = r.error
            if attempt < self.RESOLVE_ATTEMPTS:
                # The UI may simply not have settled yet — a dialog animating
                # in, a page still painting. Waiting is cheaper than improvising.
                time.sleep(self.SETTLE_S * attempt)
                locator.invalidate_cache()

        # rung 3: same intent, alternative route within the same window
        alt = self._alternate(step, scope, params)
        if alt is not None:
            out.improvised = True
            out.attempts += 1
            if alt.ok:
                out.ok = True
                out.verified = alt.verified
                out.elapsed_s = time.time() - t0
                # A route that worked is worth keeping — it becomes a locator
                # for next time, and starts accruing its own statistics.
                if alt.element is not None:
                    new_locs = locator.describe(alt.element)
                    if new_locs:
                        step.locators.extend(new_locs)
                return out
            out.error = alt.error or out.error

        # rung 4/5: hand back with the intent so a caller can solve it
        out.elapsed_s = time.time() - t0
        out.error = out.error or "no locator resolved and no alternative found"
        return out

    def _ask_system_one(self, step: Step, scope: int):
        """Last rung before waking Claude: ask a decision model which control
        satisfies the step's intent.

        The OCR tier above fixes a control that *draws* its label instead of
        publishing it. It cannot fix a button that was renamed, moved into a
        menu, or replaced by one that means the same thing — and that is the
        common way an application drifts. A human repairs it in a second by
        looking at the screen; this is that, as a bounded choice over the
        control table, at ~344ms against the several seconds a Claude turn
        costs.

        Silent and free when no key is configured: `fast.available()` is
        checked before any UIA walk happens, so a machine without one pays
        nothing on every failed step. Any failure here returns None and the
        ladder carries on to rung 4/5 exactly as before — this tier can only
        add recoveries, never remove one.
        """
        try:
            from . import fast
        except Exception:
            return None
        if not fast.available():
            return None
        try:
            el, verdict = fast.repair(step.intent, scope,
                                      window=str(step.args.get("window") or ""))
        except Exception as e:
            self._emit("improvise", rung="system-one", ok=False, error=str(e))
            return None
        self._emit("improvise", rung="system-one", ok=el is not None,
                   why=verdict.why)
        return el

    def _alternate(self, step: Step, scope, params):
        """Try to satisfy the step's INTENT by another means.

        Two tiers, cheapest first: look the target's name up by drawn label
        via OCR, which addresses controls that publish no name at all; then,
        if a key is configured, ask a System-1 model which control on screen
        satisfies the intent, which addresses the control that was renamed or
        moved. This is the rung that turns 'the button moved' into a recovery
        rather than a failure.
        """
        if scope is None:
            scope = locator.foreground_window()
        if not scope:
            return None

        wanted = ""
        for loc in step.locators:
            v = loc.value or {}
            wanted = v.get("name") or v.get("text") or v.get("id") or ""
            if wanted:
                break
        # The OCR tier needs a name to look up. The System-1 tier works from
        # the step's intent and does not, so a step whose locators carry no
        # name still gets a shot at the rung below instead of going straight
        # to Claude.
        el = None
        if wanted:
            try:
                el = locator.find_labelled(scope, wanted)
            except Exception:
                el = None

        if el is None:
            el = self._ask_system_one(step, scope)
        if el is None:
            return None

        from . import actuator
        r = actuator.click(el, prefer="invoke")
        from .act import StepResult, check
        sr = StepResult(ok=r.ok, step_intent=step.intent, action=step.action.value,
                        method=r.method, element=el)
        if r.ok:
            sr.verified = check(step.verify, scope)
            if sr.verified is False:
                r2 = actuator.click(el, prefer="inject")
                sr.verified = check(step.verify, scope) if r2.ok else False
                sr.ok = bool(sr.verified is not False)
        return sr
