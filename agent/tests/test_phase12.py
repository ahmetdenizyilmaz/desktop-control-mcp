"""Recording what the user does in the browser, and replaying it.

The desktop input hook sees a click on the browser window and nothing more —
it cannot know which DOM element was under the cursor, so a demonstrated web
flow used to record nothing usable at all. This listens where that information
actually exists: inside the page.

Three things must hold, and each has a specific way of going wrong:

  identity    a recorded step must carry something durable, and say so when it
              could not — a positional selector kept silently is a job that
              breaks at the next redesign with no warning;
  secrecy     a password must never reach the file. A job is meant to be
              shareable, and a recorded one would sit there in plain text;
  replay      the recording must actually run again. Capturing beautifully and
              replaying not at all is the failure that looks like success.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cleanup import scratch_dir  # noqa: E402

import core.browser as bmod  # noqa: E402
from core import induce  # noqa: E402
from core.browser import Browser  # noqa: E402
from core.replay import Replayer  # noqa: E402
from core.schema import Action, Trust  # noqa: E402
from core.store import Store  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""),
          flush=True)
    return ok


PAGE = """<!doctype html><title>Signup</title>
<label for=who>Name</label><input id=who name=who placeholder=Name>
<label for=pw>Password</label><input id=pw name=pw type=password>
<select id=plan name=plan><option>Basic</option><option>Pro</option></select>
<input type=checkbox id=terms name=terms><label for=terms>Accept terms</label>
<button data-testid=go>Register</button>
<div><div><div><button id="">Nameless</button></div></div></div>
<div id=out></div>
<script>document.querySelector('[data-testid=go]').onclick=()=>
 out.textContent='DONE:'+who.value+'/'+plan.value+'/'+terms.checked;</script>"""


def main() -> int:
    print("=" * 72)
    print("RECORDING THE BROWSER, AND REPLAYING IT")
    print("=" * 72)

    tmpdir = Path(tempfile.gettempdir())
    page = tmpdir / "agent_p12_signup.html"
    page.write_text(PAGE, encoding="utf-8")

    b = Browser(headless=True, profile_dir=scratch_dir("p12_"))
    real_singleton = bmod.browser
    bmod.browser = b            # replay reaches for the shared instance
    try:
        print("\n1. it records by DOM identity, not by pixel")
        b.goto(page.as_uri())
        check("recording starts", b.start_recording().get("ok"))
        pg = b._page
        pg.fill("#who", "Ada Lovelace")
        pg.fill("#pw", "hunter2-secret")
        pg.select_option("#plan", label="Pro")
        pg.check("#terms")
        pg.click("[data-testid=go]")
        time.sleep(0.6)
        events = b.stop_recording()

        kinds = [e["kind"] for e in events]
        check("captured every interaction", len(events) == 5, f"{len(events)}: {kinds}")
        check("a click is a click", "click" in kinds)
        check("a checkbox is a tick, not a click", "check" in kinds,
              "replaying a tick as a click would toggle it the wrong way "
              "when it starts checked")
        check("a dropdown is a select", "select" in kinds)

        by_sel = {e["selector"]: e for e in events}
        check("prefers a test id", "[data-testid=\"go\"]" in by_sel, str(list(by_sel))[:70])
        check("falls back to an id", "#who" in by_sel)

        print("\n2. a password never reaches the recording")
        pw = next(e for e in events if e["selector"] == "#pw")
        check("the field is marked secret", pw.get("secret") is True)
        check("its value is empty", pw.get("value") == "")
        check("the password is nowhere in the whole recording",
              "hunter2" not in json.dumps(events),
              "a recorded password would sit in plain text in a shareable file")

        print("\n3. order is when typing began, not when focus left")
        # `change` fires on blur, so a field filled early but left late would
        # otherwise land after everything that happened in between.
        order = [e["selector"] for e in events]
        check("name comes before password",
              order.index("#who") < order.index("#pw"), str(order))
        check("both come before the submit",
              order.index("#pw") < order.index('[data-testid="go"]'), str(order))

        print("\n4. the draft reads as English")
        draft = induce.from_web_recording(events)
        text = draft.describe()
        check("names the field, not the selector", "into Name" in text,
              [l for l in text.splitlines() if "Name" in l][:1])
        check("shows a typed value", '"Ada Lovelace"' in text)
        check("says a stored password will be used",
              "stored password" in text, "not the password itself")
        check("describes a tick as a tick", "tick" in text)
        check("no raw selectors in the prose",
              "data-testid" not in text and "#who" not in text,
              "a user can check English; nobody can check a selector")

        print("\n5. it admits what will not travel")
        check("a local file path is flagged",
              any(s.confidence < 0.6 and s.action is Action.NAVIGATE
                  for s in draft.steps),
              "a file:// url names this machine and cannot work on another")
        check("and says so in the warnings",
              any("local file" in d for d in draft.dropped), str(draft.dropped)[:60])
        check("the password step is flagged too",
              any(s.confidence < 0.6 and s.action is Action.WEB_FILL
                  for s in draft.steps))

        b.goto(page.as_uri())
        b.start_recording()
        b._page.click("#\\30 " if False else "button:not([data-testid])")
        time.sleep(0.4)
        nameless = b.stop_recording()
        if nameless:
            d2 = induce.from_web_recording(nameless)
            weak = [s for s in d2.steps if s.action is Action.WEB_CLICK]
            check("a nameless element is flagged as fragile",
                  bool(weak) and weak[0].confidence < 0.6,
                  weak[0].note[:52] if weak else "no step produced")
        else:
            check("a nameless element is flagged as fragile", False,
                  "nothing recorded")

        print("\n6. record -> save -> replay")
        b.goto(page.as_uri())
        b.start_recording()
        pg = b._page
        pg.fill("#who", "Grace Hopper")
        pg.select_option("#plan", label="Pro")
        pg.check("#terms")
        pg.click("[data-testid=go]")
        time.sleep(0.6)
        evs2 = b.stop_recording()

        store = Store(scratch_dir("p12store_"))
        d3 = induce.from_web_recording(evs2)
        macro, local = induce.to_macro(d3, name="web-signup",
                                       description="Fill in the signup form.")
        store.save(macro, local)
        check("saved as a job", len(macro.steps) == len(d3.steps),
              f"{len(macro.steps)} steps")
        check("starts untrusted", local.trust is Trust.DRAFT,
              "a demonstration is not a run")

        pg.goto("about:blank")
        time.sleep(0.3)
        res = Replayer(store).run(macro.id)
        check("replays end to end", res.ok, res.stopped_reason or "")
        check("every step ran", all(s.ok for s in res.outcomes),
              f"{sum(1 for s in res.outcomes if s.ok)}/{len(res.outcomes)}")
        check("the page agrees it happened",
              "DONE:Grace Hopper/Pro/true" in b.text(),
              b.text()[-34:].strip().replace("\n", " "))

        print("\n7. a web step is not sent through desktop resolution")
        # It carries a DOM selector; there was never a UIA element to find.
        # Routed the wrong way it failed with "no locator resolved", which is
        # true and useless.
        errs = [s.error for s in res.outcomes if s.error]
        check("no phantom locator errors",
              not any("no locator resolved" in (e or "") for e in errs),
              str(errs)[:60])

    finally:
        bmod.browser = real_singleton
        b.stop()

    print("\n8. the tools know about it")
    import tools_sdk
    fn = {t.name: t for t in tools_sdk.ALL_TOOLS}
    check("record_job exists", "record_job" in fn)
    src = tools_sdk.record_job.handler.__doc__ or ""
    check("the prompt still demands confirmation",
          "WAIT FOR THEM TO CONFIRM" in tools_sdk.SYSTEM_PROMPT)
    import inspect
    body = inspect.getsource(tools_sdk.record_job.handler)
    check("recording starts the browser side too",
          "start_recording" in body)
    check("stopping collects the browser side too",
          "stop_recording" in body and "from_web_recording" in body)

    print("\n" + "=" * 72)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"{passed}/{len(results)} passed")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name}  {detail}")
    print("=" * 72)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
