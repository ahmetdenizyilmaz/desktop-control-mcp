"""End-to-end: can it actually do a job on this computer?

Not a unit test. This drives a real application the way the product will:
launch it, find controls by name, act on them, verify the result, then save
and replay the whole thing as a macro.

Target is a small WinForms fixture: it exposes a real UIA edit control so a
typed value can be read back, and it closes without a save prompt so a hundred
runs leave the desktop exactly as they found it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ctypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except (AttributeError, OSError):
    pass

from core import act, actuator, display, locator            # noqa: E402
from core.arbiter import Arbiter                            # noqa: E402
from core.machine_profile import load_profile               # noqa: E402
from core.schema import (Action, LocalState, Locator,       # noqa: E402
                         LocatorType, Macro, Step, Trust, Verify)
from core.store import Store                                # noqa: E402
from core.replay import Replayer                            # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cleanup                                              # noqa: E402

REPO = Path(__file__).resolve().parents[2]
TARGET = REPO / "bench" / "e2e_target.ps1"
TARGET_TITLE = "E2E TARGET"
from core.watchdog import Watchdog                          # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""),
          flush=True)
    return ok


def main() -> int:
    print("=" * 74)
    print("END TO END — drive a real application")
    print("=" * 74)
    if display.is_locked():
        print("\nABORT: workstation locked.")
        return 2

    prof = load_profile()
    arb = Arbiter(owner="E2E test", use_overlay=False)
    if not check("take control", arb.start(), arb.state.detail):
        return 1

    # Everything on screen before we start. Anything this suite opens gets
    # closed again at the end — a test that drives real applications and then
    # walks away leaves the user's desktop full of its debris, and after a few
    # runs that is indistinguishable from the app misbehaving.
    before = {w["hwnd"] for w in locator.list_windows()}

    try:
        # -- 1. launch a real app -------------------------------------
        print("\n1. launch a real application")
        # A purpose-built fixture rather than Notepad. Notepad pops a
        # "Save changes?" prompt on close so the window never actually goes
        # away, the packaged build drags a File Explorer window along with
        # every launch, and a real one may hold the user's own unsaved work
        # that a test which types has no business touching.
        check("fixture script present", TARGET.exists(), str(TARGET.name))
        subprocess.Popen(["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden",
                          "-ExecutionPolicy", "Bypass", "-File", str(TARGET)],
                         cwd=str(REPO))

        h = None
        for _ in range(40):
            time.sleep(0.5)
            h = locator.find_window(title_contains=TARGET_TITLE)
            if h:
                break
        if not check("target window appeared", h is not None):
            return 1
        actuator.focus_window(h)
        time.sleep(1.0)
        locator.invalidate_cache()

        # -- 2. perceive without pixels --------------------------------
        print("\n2. see what can be clicked, without a screenshot")
        root = locator.uia_root(h)
        named, total = [], 0
        t0 = time.time()
        for c in locator.walk(root, max_depth=12, max_nodes=600, budget_s=4.0):
            total += 1
            try:
                if (c.Name or "").strip():
                    named.append((c.ControlTypeName, c.Name))
            except Exception:
                pass
        dt = (time.time() - t0) * 1000
        check("enumerates controls fast", total > 0 and dt < 5000,
              f"{total} elements, {len(named)} named, {dt:.0f}ms")

        edit = None
        for c in locator.walk(locator.uia_root(h), max_depth=12,
                              max_nodes=600, budget_s=4.0):
            try:
                if c.ControlTypeName in ("EditControl", "DocumentControl"):
                    edit = c
                    break
            except Exception:
                pass
        check("found the text area", edit is not None,
              edit.ControlTypeName if edit is not None else "")

        # -- 3. act -----------------------------------------------------
        print("\n3. do the job")
        actuator.focus_window(h)
        time.sleep(0.5)
        locator.invalidate_cache()

        edit = None
        for c in locator.walk(locator.uia_root(h), max_depth=12,
                              max_nodes=600, budget_s=4.0):
            try:
                if c.ControlTypeName in ("EditControl", "DocumentControl"):
                    edit = c
                    break
            except Exception:
                pass

        stamp = f"desktopagent{int(time.time())}"
        if edit is not None:
            try:
                edit.SetFocus()
            except Exception:
                pass
        time.sleep(0.4)
        r = actuator.type_text(stamp)
        check("typed into the app", r.ok, f"{r.method} — {stamp}")
        time.sleep(0.8)

        # -- 4. verify from the app's own state ------------------------
        print("\n4. verify from the application, not from our own belief")
        locator.invalidate_cache()
        found_in = ""
        for c in locator.walk(locator.uia_root(h), max_depth=12,
                              max_nodes=600, budget_s=4.0):
            try:
                if c.ControlTypeName not in ("EditControl", "DocumentControl"):
                    continue
                vp = c.GetValuePattern()
                val = (vp.Value or "") if vp is not None else ""
                if stamp in val:
                    found_in = val
                    break
                if not found_in and val:
                    found_in = val
            except Exception:
                continue
        check("app contains what we typed", stamp in found_in,
              f"read back {found_in[:60]!r}")

        # -- 4b. the same job as ONE batched call --------------------------
        print("\n4b. a whole sequence of desktop actions in one call")
        import asyncio
        import tools_sdk
        batch = [
            {"do": "focus", "title": TARGET_TITLE},
            {"do": "click", "name": "Type here", "control": "EditControl"},
            # Select what section 3 typed so the batch REPLACES it. Not
            # ctrl+a: a single-line WinForms TextBox ignores it, and the
            # first run of this check appended instead of replacing.
            {"do": "key", "keys": "end"},
            {"do": "key", "keys": "shift+home"},
            {"do": "type", "text": "batched-by-do-steps"},
            # The fixture's status label reads "<n> characters": the batch
            # itself confirms the typing landed before it reports success.
            {"do": "wait_for", "expect": "19 characters", "timeout": 5},
        ]
        out = asyncio.run(tools_sdk.do_steps.handler({"steps": json.dumps(batch)}))
        text = out["content"][0]["text"]
        print("       " + text.replace("\n", "\n       "))
        check("all six steps ran in one call", text.startswith("6/6 steps"),
              text[:60])
        locator.invalidate_cache()
        val = ""
        for c in locator.walk(locator.uia_root(h), max_depth=12,
                              max_nodes=600, budget_s=4.0):
            try:
                if c.ControlTypeName in ("EditControl", "DocumentControl"):
                    vp = c.GetValuePattern()
                    val = (vp.Value or "") if vp is not None else ""
                    break
            except Exception:
                continue
        check("the app holds exactly the batched text",
              val == "batched-by-do-steps", repr(val[:40]))

        # -- 4c. precise pointer work: a move lands where the picture says,
        #        and a drag selects like a hand would --------------------
        print("\n4c. pointer paths land where the picture says")
        from ctypes import wintypes as _wt
        region = tools_sdk._window_region(h)
        if check("the window's on-screen region is known", region is not None):
            L, T, W, H = region
            out = asyncio.run(tools_sdk.mouse_path.handler({
                "window": TARGET_TITLE, "action": "move", "button": "left",
                "frame_w": 0, "frame_h": 0,
                "points": json.dumps([[10, 10], [W // 2, H // 2]])}))
            text = out["content"][0]["text"]
            pt = _wt.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            check("the pointer ended on the mapped point",
                  text.startswith("Moved") and abs(pt.x - (L + W // 2)) <= 2
                  and abs(pt.y - (T + H // 2)) <= 2,
                  f"cursor=({pt.x},{pt.y}) expected=({L + W // 2},{T + H // 2})")

            # Drag across the text box to select what 4b typed, then type
            # over it: the app's own state proves the button was held down
            # for the whole path, not just clicked at both ends.
            box = act.find(name="Type here", control="EditControl",
                           window=TARGET_TITLE)
            if check("the text box can be found for a drag", box is not None):
                x1, y1, x2, y2 = box.rect
                cy = (y1 + y2) // 2 - T
                # 8 px in from each edge, not 2: the fixture is DPI-unaware
                # and Windows scales it 2x, so 2 physical px is one logical
                # px — on the edit's 3D border, where a press sets no caret
                # and takes no capture. Probed: centre clicks landed, edge
                # clicks did nothing at all.
                out = asyncio.run(tools_sdk.mouse_path.handler({
                    "window": TARGET_TITLE, "action": "drag", "button": "left",
                    "frame_w": 0, "frame_h": 0,
                    "points": json.dumps([[x1 - L + 8, cy], [x2 - L - 8, cy]])}))
                print("       " + out["content"][0]["text"][:90])
                time.sleep(0.3)
                actuator.type_text("dragged")
                time.sleep(0.6)
                locator.invalidate_cache()
                val = ""
                for c in locator.walk(locator.uia_root(h), max_depth=12,
                                      max_nodes=600, budget_s=4.0):
                    try:
                        if c.ControlTypeName in ("EditControl", "DocumentControl"):
                            vp = c.GetValuePattern()
                            val = (vp.Value or "") if vp is not None else ""
                            break
                    except Exception:
                        continue
                check("a drag selected the text and typing replaced it",
                      val == "dragged", repr(val[:40]))

        # -- 5. save it as a portable macro ----------------------------
        print("\n5. save the job as a replayable macro")
        # A throwaway library. Writing into the real one left eighteen
        # 'e2e-*' entries behind after eighteen runs, which is noise in
        # a feature whose whole value is that you can find things in it.
        store = Store(cleanup.scratch_dir("agent_e2e_store_"))
        # Focus-then-type rather than launch-then-type: the fixture is already
        # open, and a macro with no launch step carries no path at all, which
        # is what the portability assertion below is really checking.
        macro = Macro(
            name="e2e-type-into-target",
            description="Focus the target window and type a marker string into it.",
            steps=[
                Step(action=Action.FOCUS_WINDOW, intent="Bring the target to the front",
                     args={"title": TARGET_TITLE},
                     verify=Verify(window_title_regex=TARGET_TITLE, timeout_s=10)),
                Step(action=Action.TYPE, intent="Type the marker text",
                     args={"text": "replayed-by-macro"}),
            ],
            permissions=["input"],
        )
        local = LocalState(macro_id=macro.id, trust=Trust.SUPERVISED)
        store.save(macro, local)
        check("macro saved", (store.path(macro.id) / "macro.json").exists(),
              macro.id)

        blob = (store.path(macro.id) / "macro.json").read_text(encoding="utf-8")
        check("shareable half has no machine paths",
              "C:/Users" not in blob and "C:\\\\Users" not in blob,
              "portable by construction")

        # -- 6. replay it ----------------------------------------------
        print("\n6. replay it")
        rp = Replayer(store)
        res = rp.run(macro.id, arbiter=arb)
        if not res.ok and "took control" in (res.stopped_reason or ""):
            # The user was using the machine — the arbiter correctly refused
            # to fight them for the mouse. Wait for them to be idle and retry
            # once, so the test measures replay rather than their typing.
            print("       user was active; waiting for idle and retrying once")
            for _ in range(20):
                time.sleep(1.0)
                if arb.check(settle=False).control.value == "driving":
                    break
            res = rp.run(macro.id, arbiter=arb)
        print("       " + res.report().replace("\n", "\n       "))
        check("replay completed", res.ok, res.stopped_reason or "")
        check("trust advanced on success", res.trust_after in ("supervised", "trusted"),
              f"{res.trust_before} -> {res.trust_after}")

        # -- 7. watchdog -----------------------------------------------
        print("\n7. progress watchdog nudges a stalled task")
        nudged = []
        wd = Watchdog(check_every_s=1.0, stall_after_s=1.0, max_nudges=2,
                      on_nudge=lambda p: nudged.append(p.task_id))
        p = wd.begin("t1", "a task that stops reporting")
        wd.beat("t1", "did something")
        time.sleep(1.4)
        wd.sweep()
        check("stall detected and nudged", nudged == ["t1"],
              f"nudges={p.nudges} state={p.state.value}")
        prompt = wd.continuation_prompt(p)
        check("continuation prompt carries context",
              "did something" in prompt and "Continue" in prompt,
              prompt[:70] + "…")
        wd.beat("t1", "resumed")
        check("heartbeat clears the stall", p.state.value == "running")

    finally:
        arb.stop()
        opened = [w for w in locator.list_windows() if w["hwnd"] not in before]
        closed = 0
        for w in opened:
            # Only what this run put there, and only if cleanup agrees it is
            # safe — a window the user opened mid-test is not ours to close.
            if cleanup.classify(w["title"]) == "close":
                ctypes.windll.user32.PostMessageW(w["hwnd"], 0x0010, 0, 0)
                closed += 1
        if opened:
            print(f"\n   cleaned up {closed}/{len(opened)} window(s) this run opened")

    passed = sum(1 for _, ok, _ in results if ok)
    print("\n" + "=" * 74)
    print(f"{passed}/{len(results)} checks passed")
    fails = [(n, d) for n, ok, d in results if not ok]
    if fails:
        print("\nfailures:")
        for n, d in fails:
            print(f"  - {n}  {d}")
    print("=" * 74)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
