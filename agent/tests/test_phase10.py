"""Turning a demonstration into a job, and editing one afterwards.

The expensive failure here is silent: a job induced from a misread recording
replays without error and does the wrong thing every time. So most of these
checks are about what the draft REFUSES to assume, and about the confirmation
step being impossible to skip.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cleanup import scratch_dir  # noqa: E402

from core import induce  # noqa: E402
from core.schema import Action, Trust  # noqa: E402
from core.store import Store  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""),
          flush=True)
    return ok


def ev(**kw):
    base = {"t": 0.0, "type": "click", "window_title": "Invoices - Excel"}
    base.update(kw)
    return base


RECORDING = {"duration_s": 12.4, "events": [
    # the user reaching for our own recorder — never part of the job
    ev(t=0.2, element_name="Start recording", element_type="ButtonControl",
       window_title="AGI Agent",
       locators=[{"type": "uia_name_type", "value": {"name": "Start"}}]),
    ev(t=1.0, element_name="Export", element_type="ButtonControl",
       locators=[{"type": "uia_name_type",
                  "value": {"name": "Export", "type": "Button"}}]),
    # a double-click registered as two
    ev(t=1.2, element_name="Export", element_type="ButtonControl",
       locators=[{"type": "uia_name_type",
                  "value": {"name": "Export", "type": "Button"}}]),
    ev(t=2.5, type="type", text="march-invoices.xlsx"),
    ev(t=3.0, type="scroll"),
    ev(t=3.6, element_name="", element_type="ButtonControl",
       locators=[{"type": "norm_coord", "value": {"x": 0.5, "y": 0.5}}]),
    ev(t=4.1, type="key", keys="ctrl+s"),
]}


def main() -> int:
    print("=" * 72)
    print("DEMONSTRATION TO JOB")
    print("=" * 72)

    print("\n1. the mechanical pass")
    d = induce.from_recording(RECORDING)
    kinds = [s.action for s in d.steps]
    check("keeps the real actions",
          kinds == [Action.CLICK, Action.TYPE, Action.CLICK, Action.KEY],
          str([k.value for k in kinds]))
    check("drops clicks on our own window",
          not any("AGI Agent" in s.window for s in d.steps),
          "the user reaching for the recorder is not part of the job")
    legacy = induce.from_recording({"duration_s": 1, "events": [
        ev(element_name="Start recording", window_title="Desktop Agent",
           locators=[{"type": "uia_name_type", "value": {"name": "Start"}}])]})
    check("and under the name the app had before", legacy.steps == [],
          "a demonstration recorded against the previous build reads the same")
    check("collapses a double click into one",
          sum(1 for s in d.steps
              if s.action is Action.CLICK and "Export" in s.intent) == 1)
    check("drops scrolling", not any("scroll" in s.action.value for s in d.steps),
          "replay resolves by identity and scrolls the element into view itself")
    check("says what it ignored", len(d.dropped) >= 3, str(d.dropped)[:70])

    print("\n2. it keeps identity, not position")
    export = d.steps[0]
    check("carries the locator stack recorded live", bool(export.locators))
    check("locator is identity-based",
          export.locators[0].type.value.startswith("uia"),
          export.locators[0].type.value)
    check("typed text is preserved exactly",
          d.steps[1].args["text"] == "march-invoices.xlsx")
    check("key combinations survive", d.steps[3].args["keys"] == "ctrl+s")

    print("\n3. uncertainty is surfaced, not smoothed over")
    unnamed = d.steps[2]
    check("a nameless control is low confidence", unnamed.confidence < 0.6,
          f"{unnamed.confidence}")
    check("and says why", "no name" in unnamed.note, unnamed.note[:50])
    check("a named control is full confidence", export.confidence == 1.0)
    check("the draft reports its weakest link", d.weakest < 0.6, str(d.weakest))

    bare = induce.from_recording({"duration_s": 1, "events": [
        ev(element_name="", element_type="", locators=[])]})
    check("a click on nothing identifiable is the least trusted",
          bare.steps[0].confidence <= 0.25, str(bare.steps[0].confidence))
    check("and warns that it will not survive a window move",
          "window move" in bare.steps[0].note, bare.steps[0].note[:44])

    print("\n4. the confirmation text is readable by a human")
    text = d.describe()
    check("describes each step as a sentence", "click 'Export' (button)" in text)
    check("shows the typed text", "march-invoices.xlsx" in text)
    check("flags the uncertain step inline", "not sure" in text)
    check("asks the question outright", "Is that right?" in text)
    check("explains the stake", "does the wrong thing" in text,
          "a confirmation prompt nobody reads is not a confirmation")
    check("no JSON in the user-facing text",
          "{" not in text and "locators" not in text,
          "a user can check English; they cannot check a locator array")

    print("\n5. a demonstrated password is caught before saving")
    login = induce.from_recording({"duration_s": 5, "events": [
        ev(element_name="Password", element_type="EditControl",
           locators=[{"type": "uia_name_type", "value": {"name": "Password"}}]),
        ev(t=1, type="type", text="Hunter2!swordfish"),
    ]})
    warn = induce.looks_sensitive(login)
    check("spotted", bool(warn), str(warn)[:60])
    check("names the step", warn and "step 2" in warn[0], warn[0][:40] if warn else "")
    check("points at the fix", warn and "secret://" in warn[0],
          "a recorded login writes the password into a file meant to be shared")
    plain = induce.from_recording({"duration_s": 2, "events": [
        ev(t=1, type="type", text="march invoices for review")]})
    check("ordinary prose is not flagged", not induce.looks_sensitive(plain),
          "crying wolf on every typed sentence would train the user to ignore it")

    print("\n6. freezing a confirmed draft")
    tmp = scratch_dir("agent_induce_")
    store = Store(tmp)
    macro, local = induce.to_macro(d, name="export-invoices",
                                   description="Export the month's invoices.")
    check("name and description are the user's words",
          macro.name == "export-invoices" and "invoices" in macro.description)
    check("steps carried over", len(macro.steps) == len(d.steps))
    check("starts untrusted", local.trust is Trust.DRAFT,
          "a demonstration is not a run; the first real run earns trust")
    check("marked as recorded", local.origin == "recorded", local.origin)

    store.save(macro, local)
    back, back_local = store.load(macro.id)
    check("round-trips through the store", back.name == macro.name
          and len(back.steps) == len(macro.steps))
    raw = (tmp / macro.id / "macro.json").read_text(encoding="utf-8")
    check("the saved job carries no machine path",
          Path.home().name.lower() not in raw.lower())
    check("and no trust", "trust" not in raw,
          "trust is per machine and must never ride along in a shared file")

    print("\n7. an empty recording produces nothing")
    empty = induce.from_recording({"duration_s": 3, "events": [
        ev(element_name="Stop", window_title="AGI Agent",
           locators=[{"type": "uia_name_type", "value": {"name": "Stop"}}])]})
    check("no steps when everything was ours", empty.steps == [])
    check("and it is not silently saved as a blank job", not empty.steps)

    print("\n8. editing a saved job")
    import asyncio
    import tools_sdk
    fn = {t.name: t for t in tools_sdk.ALL_TOOLS}

    async def call(name, args):
        r = await fn[name].handler(args)
        return r["content"][0]["text"]

    real = Store()
    m2, l2 = induce.to_macro(d, name="edit-me", description="a job to edit")
    real.save(m2, l2)
    try:
        out = asyncio.run(call("edit_job", {"id": m2.id, "action": "show"}))
        check("show lists the steps", "1. click" in out and "edit-me" in out,
              out.splitlines()[0][:50])

        asyncio.run(call("edit_job", {"id": m2.id, "action": "rename",
                                      "value": "renamed-job"}))
        check("rename sticks", real.load(m2.id)[0].name == "renamed-job")

        asyncio.run(call("edit_job", {"id": m2.id, "action": "describe",
                                      "value": "a better description"}))
        check("description sticks",
              real.load(m2.id)[0].description == "a better description")

        before = len(real.load(m2.id)[0].steps)
        # Give it locator statistics, so the reset below is observable.
        _, ls = real.load(m2.id)
        ls.record(0, 0, True)
        real.save_local(ls)
        out = asyncio.run(call("edit_job", {"id": m2.id, "action": "drop_step",
                                            "step": 2}))
        after_macro, after_local = real.load(m2.id)
        check("step removed", len(after_macro.steps) == before - 1,
              f"{before} -> {len(after_macro.steps)}")
        check("locator statistics were reset", after_local.locator_stats == {},
              "they are keyed by step index and now describe the wrong steps")
        check("and it says so", "reset" in out, out[-60:])

        out = asyncio.run(call("edit_job", {"id": m2.id, "action": "drop_step",
                                            "step": 99}))
        check("a step that does not exist is refused", "does not exist" in out,
              out[:50])
        out = asyncio.run(call("edit_job", {"id": "nope", "action": "show"}))
        check("an unknown job is refused", "No job with id" in out, out[:40])

        asyncio.run(call("edit_job", {"id": m2.id, "action": "delete"}))
        check("delete removes it", not real.path(m2.id).exists())
    finally:
        real.delete(m2.id)

    print("\n9. the tools are wired up")
    names = set(fn)
    check("record_job exposed", "record_job" in names)
    check("edit_job exposed", "edit_job" in names)
    check("saving without a draft is refused",
          "Record something first" in asyncio.run(
              call("record_job", {"action": "save"})))
    check("stopping when not recording is refused",
          "Nothing is being recorded" in asyncio.run(
              call("record_job", {"action": "stop"})))
    check("the prompt forbids saving without confirmation",
          "WAIT FOR THEM TO CONFIRM" in tools_sdk.SYSTEM_PROMPT
          and "Never save straight through" in tools_sdk.SYSTEM_PROMPT)

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
