"""Atomic resolve-and-act.

The failure this exists to remove: a coordinate that round-trips through the
model across a turn boundary. Detect returns (412,388); the model reads it,
composes a reply, calls click(412,388) — and by then a menu opened, a window
snapped, or another agent moved something. The coordinate was true when it was
produced and false when it was used.

Here the coordinate never leaves the process. Callers name *what* they want;
resolution, action, and verification happen inside one call.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import actuator, locator
from .locator import Element, ResolveResult
from .schema import Locator, Step, Verify


@dataclass
class StepResult:
    ok: bool
    step_intent: str
    action: str
    locator_index: int = -1
    locator_type: str = ""
    method: str = ""
    verified: bool | None = None
    elapsed_s: float = 0.0
    tried: list = field(default_factory=list)
    error: str = ""
    element: Element | None = None
    #: True when a pattern invoke reported success but the post-condition
    #: failed, and real input was used instead. Worth recording — an app that
    #: fakes invoke should be remembered so later steps skip straight to input.
    fell_back: bool = False

    def summary(self) -> str:
        bits = [f"{'ok ' if self.ok else 'FAIL'} {self.action}"]
        if self.locator_type:
            bits.append(f"via {self.locator_type}[{self.locator_index}]")
        if self.method:
            bits.append(f"using {self.method}")
        if self.fell_back:
            bits.append("(invoke no-op, fell back to input)")
        if self.verified is not None:
            bits.append("verified" if self.verified else "UNVERIFIED")
        bits.append(f"{self.elapsed_s*1000:.0f}ms")
        if self.error:
            bits.append(f"— {self.error}")
        return "  ".join(bits)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def check(verify: Verify | None, scope_hwnd: int | None = None) -> bool | None:
    """Poll until the post-condition holds or the timeout expires.

    Returns None when a step declares no verification — distinct from False,
    which means it declared one and the condition never became true.
    """
    if verify is None:
        return None
    deadline = time.time() + verify.timeout_s

    while time.time() < deadline:
        ok = True

        if verify.appears:
            locator.invalidate_cache()
            r = locator.resolve(verify.appears, scope_hwnd)
            ok &= r.ok

        if ok and verify.disappears:
            locator.invalidate_cache()
            r = locator.resolve(verify.disappears, scope_hwnd)
            ok &= not r.ok

        if ok and verify.window_title_regex:
            import re
            titles = [w["title"] for w in locator.list_windows()]
            ok &= any(re.search(verify.window_title_regex, t) for t in titles)

        if ok:
            return True
        time.sleep(0.25)

    return False


# ---------------------------------------------------------------------------
# Single step
# ---------------------------------------------------------------------------

def do_step(step: Step, scope_hwnd: int | None = None,
            order: list[int] | None = None,
            params: dict | None = None,
            prefer: str = "invoke") -> StepResult:
    """Resolve, act, verify — without the coordinate ever escaping."""
    t0 = time.time()
    res = StepResult(ok=False, step_intent=step.intent, action=step.action.value)
    params = params or {}

    # --- actions that need no element ------------------------------------
    # Web steps carry a DOM selector, which the browser resolves itself. Sent
    # through desktop element resolution they fail with "no locator resolved",
    # which is true and useless — there was never a UIA element to find.
    if step.action.value in ("key", "type", "focus_window", "wait_for", "launch",
                             "navigate", "web_click", "web_fill"):
        try:
            r = _do_elementless(step, scope_hwnd, params)
            res.ok, res.method = r.ok, r.method
            # Without this a failed web step surfaced as the generic "no
            # locator resolved", which is both untrue and unactionable — the
            # browser knew exactly what went wrong and it was thrown away.
            if not r.ok:
                res.error = getattr(r, "detail", "") or res.error
        except Exception as e:
            res.error = f"{type(e).__name__}: {e}"
            res.elapsed_s = time.time() - t0
            return res
    else:
        # --- resolve ------------------------------------------------------
        locator.invalidate_cache(scope_hwnd)
        rr: ResolveResult = locator.resolve(step.locators, scope_hwnd, order)
        res.tried = rr.tried
        if not rr.ok:
            # A locator that could say why it could not choose ("only 5
            # named that") beats the generic miss.
            res.error = rr.reason or "no locator resolved"
            res.elapsed_s = time.time() - t0
            return res

        res.element = rr.element
        res.locator_index = rr.locator_index
        res.locator_type = rr.tried[-1][1] if rr.tried else ""

        # --- act ----------------------------------------------------------
        el = rr.element
        a = step.action.value
        try:
            if a == "click":
                out = actuator.click(el, "left", prefer=prefer)
            elif a == "right_click":
                out = actuator.click(el, "right", prefer="inject")
            elif a == "double_click":
                out = actuator.double_click(el)
            elif a == "scroll":
                out = actuator.scroll(el, step.args.get("direction", "down"),
                                      int(step.args.get("amount", 3)))
            else:
                out = actuator.click(el, "left", prefer=prefer)
            res.ok, res.method = out.ok, out.method
            if not out.ok:
                res.error = out.detail or "actuation failed"
        except Exception as e:
            res.error = f"{type(e).__name__}: {e}"
            res.elapsed_s = time.time() - t0
            return res

    # --- verify -----------------------------------------------------------
    if res.ok:
        res.verified = check(step.verify, scope_hwnd)

        # An invoke that returns cleanly is not proof it did anything. Tk
        # publishes a real InvokePattern whose Invoke() silently no-ops, so
        # the actuator reports success and nothing happened. When a step
        # declares a post-condition and it did not hold, retry the same
        # target through real input before calling it a failure.
        if (res.verified is False and res.element is not None
                and res.method.startswith("uia:")):
            retry = actuator.click(res.element, "left", prefer="inject")
            if retry.ok:
                res.method = f"{res.method}->{retry.method}"
                res.verified = check(step.verify, scope_hwnd)
                res.fell_back = True

        if res.verified is False and not step.optional:
            res.ok = False
            res.error = "post-condition not met"

    res.elapsed_s = time.time() - t0
    return res


def _do_elementless(step: Step, scope_hwnd, params) -> actuator.ActResult:
    from .machine_profile import resolve as resolve_symbols
    a = step.action.value
    args = step.args

    if a == "type":
        text = resolve_symbols(str(args.get("text", "")), params=params)
        el = None
        if step.locators:
            rr = locator.resolve(step.locators, scope_hwnd)
            el = rr.element
        return actuator.type_text(text, el)

    if a == "key":
        keys = str(args.get("keys", ""))
        if "+" in keys:
            return actuator.send_keys(keys)
        return actuator.press_key(keys, int(args.get("presses", 1)))

    if a == "focus_window":
        title = resolve_symbols(str(args.get("title", "")), params=params)
        h = locator.find_window(title_contains=title)
        if h is None:
            return actuator.ActResult(False, "focus_window", 0,
                                      detail=f"no window matching {title!r}")
        return actuator.ActResult(actuator.focus_window(h), "focus_window", 0)

    # -- web ---------------------------------------------------------
    # Recorded browser steps carry a DOM selector, which is exact. They go
    # straight to the browser rather than through the desktop actuator: a
    # coordinate click into a scrolled page is how a replay clicks the wrong
    # row of a table.
    if a in ("navigate", "web_click", "web_fill"):
        from .browser import browser
        if a == "navigate":
            url = resolve_symbols(str(args.get("url", "")), params=params)
            browser.goto(url)
            return actuator.ActResult(True, "web:goto", 0, detail=url)

        step_kind = args.get("how") or ("click" if a == "web_click" else "fill")
        if args.get("state") == "checked":
            step_kind = "check"
        elif args.get("state") == "unchecked":
            step_kind = "uncheck"

        value = resolve_symbols(str(args.get("value", "")), params=params)
        if value.startswith("secret://"):
            from .secrets import resolve as resolve_secret
            value = resolve_secret(value)

        r = browser.run([{ "do": step_kind,
                           "target": args.get("selector") or args.get("label", ""),
                           "value": value,
                           "label": args.get("label", "")}])
        ok = bool(r.get("ok"))
        detail = (r.get("steps") or [{}])[0].get("did") or                  (r.get("steps") or [{}])[0].get("error", "")
        return actuator.ActResult(ok, f"web:{step_kind}", 0, detail=detail)

    if a == "launch":
        import subprocess
        from . import noconsole
        target = resolve_symbols(str(args.get("app", "")), params=params)
        subprocess.Popen([target], shell=False, creationflags=noconsole.flags())
        return actuator.ActResult(True, "launch", 0, detail=target)

    if a == "wait_for":
        ok = check(step.verify, scope_hwnd)
        return actuator.ActResult(bool(ok), "wait_for", 0)

    return actuator.ActResult(False, a, 0, detail="unhandled action")


# ---------------------------------------------------------------------------
# Convenience — the primitive Claude actually calls
# ---------------------------------------------------------------------------

def click_element(name: str | None = None, control: str | None = None,
                  automation_id: str | None = None, text: str | None = None,
                  window: str | None = None, prefer: str = "invoke",
                  index: int | None = None, near: str | None = None) -> StepResult:
    """Find and click in one call. No coordinate is ever returned to the caller.

    Builds a locator stack from whatever identity the caller supplied, so a
    single named target automatically gets fallbacks. `index` / `near` choose
    among same-named controls — and switch the OCR fallback off, since text
    matching would land on the first occurrence, the very thing being avoided.
    """
    from .schema import Action, LocatorType

    # Scope matters more than it looks. With no window the search used to
    # start at the DESKTOP root, so a name as ordinary as "Send" or "Search"
    # could resolve inside some other application — including this app's own
    # window — and clicking it reported success. Default to the window in
    # front, and if a title was named but is not open, say so instead of
    # quietly searching everything.
    if window:
        scope = locator.find_window(title_contains=window)
        if not scope:
            return StepResult(False, "click_element", "click",
                              error=f"no open window matches {window!r}")
    else:
        scope = locator.foreground_window() or None

    locs: list[Locator] = []
    if automation_id:
        locs.append(Locator(LocatorType.UIA_AUTOMATION_ID,
                            {"id": automation_id, "control": control}))
    picky = bool(index or near)
    if name:
        value = {"name": name, "control": control}
        if index:
            value["index"] = index
        if near:
            value["near"] = near
        locs.append(Locator(LocatorType.UIA_NAME_TYPE, value))
    if (text or name) and not picky:
        locs.append(Locator(LocatorType.OCR_TEXT, {"text": text or name}))
    if not locs:
        return StepResult(False, "click_element", "click",
                          error="no identity supplied")

    step = Step(action=Action.CLICK,
                intent=f"Click {name or automation_id or text!r}",
                locators=locs)
    return do_step(step, scope, prefer=prefer)


def find(name: str | None = None, control: str | None = None,
         automation_id: str | None = None,
         window: str | None = None) -> Element | None:
    """Resolve without acting — for inspection and verification."""
    from .schema import LocatorType
    # Same reasoning as click_element: never fall back to a desktop-wide
    # search, or verification can "confirm" a control in the wrong window.
    if window:
        scope = locator.find_window(title_contains=window)
        if not scope:
            return None
    else:
        scope = locator.foreground_window() or None
    locs: list[Locator] = []
    if automation_id:
        locs.append(Locator(LocatorType.UIA_AUTOMATION_ID, {"id": automation_id}))
    if name:
        locs.append(Locator(LocatorType.UIA_NAME_TYPE,
                            {"name": name, "control": control}))
    if not locs:
        return None
    return locator.resolve(locs, scope).element
