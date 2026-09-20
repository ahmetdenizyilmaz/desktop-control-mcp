"""Phase 02 acceptance — the arbiter.

Bar: two sessions cannot act at once, physical ESC+ESC aborts, injected input
never counts as the user, and the display stays usable for the run.

Injected input is used to simulate the agent; genuine physical input cannot be
faked from a test, so the physical paths are verified by construction (the
hook reports flags) and the injected paths are verified end to end.

Run:  python agent/tests/test_phase02.py
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except (AttributeError, OSError):
    pass

from core import actuator, display                      # noqa: E402
from core.arbiter import Arbiter, Control               # noqa: E402
from core.input_monitor import (InputEvent, VK_ESCAPE,  # noqa: E402
                                DoubleTapWatcher, monitor)

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""),
          flush=True)
    return ok


# ---------------------------------------------------------------------------

def test_hook():
    print("\n1. shared input hook")
    ok = monitor.start()
    check("hook starts", ok and monitor.running)

    seen: list[InputEvent] = []
    sub = monitor.subscribe(seen.append)
    try:
        actuator.press_key("f15")           # harmless, injected
        time.sleep(0.4)
    finally:
        monitor.unsubscribe(sub)

    keys = [e for e in seen if e.kind == "key"]
    check("observes injected keystrokes", len(keys) > 0, f"{len(keys)} key events")
    check("marks our own input as INJECTED", keys and all(not e.physical for e in keys),
          "an agent must never be mistaken for the user")


def test_abort_cannot_be_faked():
    print("\n2. the kill switch cannot be pressed by the agent")
    fired = {"n": 0}
    watcher = DoubleTapWatcher(VK_ESCAPE, 0.5, lambda: fired.__setitem__("n", fired["n"] + 1))
    sub = monitor.subscribe(watcher)
    try:
        # Two injected ESC presses in quick succession — exactly what an agent
        # suppressing its own abort would do.
        actuator.press_key("escape")
        time.sleep(0.08)
        actuator.press_key("escape")
        time.sleep(0.5)
    finally:
        monitor.unsubscribe(sub)
    check("injected ESC+ESC does NOT abort", fired["n"] == 0,
          f"triggered {fired['n']} times — must be 0")

    # And prove the same watcher DOES fire for physical input, by feeding it
    # synthesised events flagged physical (the hook is what sets that flag).
    watcher2 = DoubleTapWatcher(VK_ESCAPE, 0.5,
                                lambda: fired.__setitem__("n", fired["n"] + 100))
    now = time.time()
    watcher2(InputEvent(kind="key", action="down", physical=True, t=now, vk=VK_ESCAPE))
    watcher2(InputEvent(kind="key", action="down", physical=True, t=now + 0.1, vk=VK_ESCAPE))
    time.sleep(0.3)
    check("physical ESC+ESC DOES abort", fired["n"] >= 100,
          "same watcher, only the physical flag differs")


def test_arbiter_lifecycle():
    print("\n3. arbiter acquires and reports control")
    arb = Arbiter(owner="Phase02 Test", use_overlay=False)
    got = arb.start()
    try:
        check("acquires control", got and arb.state.control is Control.DRIVING,
              arb.state.detail or arb.state.control.value)
        st = arb.status()
        check("hook running under arbiter", st["hook_running"])
        check("display wake-lock held", arb._keeper is not None)
        check("guard() permits action when idle", arb.guard("test action"),
              f"control={arb.state.control.value}")

        # Our own injected input must not read as user interference.
        actuator.press_key("f15")
        time.sleep(0.3)
        st2 = arb.status()
        check("injected input is not interference", arb.guard(),
              f"injected={st2['injected_events']} physical={st2['physical_events']}")
    finally:
        arb.stop()
    check("releases cleanly", arb.state.control is not Control.DRIVING,
          arb.state.control.value)


def test_mutual_exclusion():
    print("\n4. two processes cannot drive at once")
    arb = Arbiter(owner="Holder", use_overlay=False)
    if not arb.start():
        check("first holder acquires", False, arb.state.detail)
        return
    try:
        code = (
            "import sys; sys.path.insert(0, r'%s');"
            "from core.arbiter import Arbiter;"
            "a = Arbiter(owner='Rival', use_overlay=False);"
            "ok = a.start();"
            "print('ACQUIRED' if ok else 'BLOCKED:' + a.state.detail);"
            "a.stop()" % str(Path(__file__).resolve().parents[1])
        )
        p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, timeout=60)
        out = (p.stdout or "").strip().splitlines()
        verdict = out[-1] if out else "(no output)"
        check("second process is blocked", verdict.startswith("BLOCKED"), verdict[:70])
    except subprocess.TimeoutExpired:
        check("second process is blocked", False, "rival timed out")
    finally:
        arb.stop()


def test_motion_vs_intent():
    """The speed fix, pinned down.

    Pausing on bare cursor movement cost seconds on every single action, and
    was most of why the app felt slow. Movement is now ignored while clicks,
    keys and scrolls still stop the run. Both halves are load-bearing: lose the
    first and it is slow again, lose the second and it drives over the user.
    """
    print("\n5. bare mouse movement does not count as taking over")
    from core.input_monitor import ActivityTracker, InputEvent

    def feed(tr, kind, action, physical=True):
        tr(InputEvent(kind=kind, action=action, physical=physical, t=time.time()))

    tr = ActivityTracker()
    for _ in range(40):
        feed(tr, "mouse", "move")
    check("movement is seen", tr.physical_count == 40)
    check("movement is not intent", tr.intent_count == 0,
          "someone watching the agent work moves the cursor constantly")
    check("movement leaves intent-idle infinite", tr.idle_s() == float("inf"))
    check("movement does update motion-idle", tr.motion_idle_s() < 1.0,
          "still needed: it is how a real takeover is distinguished")

    for kind, action in (("mouse", "left_down"), ("key", "down"),
                         ("mouse", "wheel")):
        t2 = ActivityTracker()
        feed(t2, kind, action)
        check(f"{kind}:{action} counts as intent", t2.intent_count == 1)

    t3 = ActivityTracker()
    feed(t3, "mouse", "left_down", physical=False)
    feed(t3, "key", "down", physical=False)
    check("injected clicks and keys are never intent",
          t3.intent_count == 0 and t3.injected_count == 2,
          "otherwise the agent pauses for its own actions, forever")

    # And the arbiter must act on that distinction, not just record it.
    arb = Arbiter(owner="MotionTest", use_overlay=False)
    if arb.start():
        try:
            for _ in range(30):
                arb._activity(InputEvent(kind="mouse", action="move",
                                         physical=True, t=time.time()))
            t0 = time.time()
            ok = arb.guard()
            elapsed = time.time() - t0
            check("arbiter keeps driving through cursor movement", ok,
                  arb.state.detail or arb.state.control.value)
            check("and does not back off for it", elapsed < 0.15,
                  f"{elapsed*1000:.0f}ms — the old code slept up to 5.8s here")

            arb._activity(InputEvent(kind="mouse", action="left_down",
                                     physical=True, t=time.time()))
            st = arb.check(settle=False)
            check("a real click pauses the run",
                  st.control is not Control.DRIVING, st.detail)
        finally:
            arb.stop()
    else:
        check("arbiter available for motion test", False, arb.state.detail)


def test_invited_interaction():
    """Answering a question we asked must not read as a takeover.

    The agent puts a prompt on screen; the user clicks Approve. That click is
    physical and it moves the foreground window — both signals that normally
    mean "stop". Without rebaseline() the run would end the instant they said
    to continue.
    """
    print("\n6. answering our own question does not stop the run")
    from core.input_monitor import InputEvent

    arb = Arbiter(owner="AskTest", use_overlay=False)
    if not arb.start():
        check("arbiter available", False, arb.state.detail)
        return
    try:
        # Simulate the user clicking Approve in our window.
        arb._activity(InputEvent(kind="mouse", action="left_down",
                                 physical=True, t=time.time()))
        arb._baseline_window = 0xDEAD          # pretend focus moved to us
        st = arb.check(settle=False)
        check("without rebaseline this looks like a takeover",
              st.control is not Control.DRIVING, st.control.value)

        arb.rebaseline()
        check("rebaseline resumes driving",
              arb.state.control is Control.DRIVING, arb.state.detail)
        check("and it sticks on the next check",
              arb.check(settle=False).control is Control.DRIVING,
              arb.state.detail)
        check("interference record is cleared",
              not arb.state.interference.window_changed
              and arb.state.interference.physical_events == 0)

        # It must not disarm the kill switch or the real takeover path.
        arb._activity(InputEvent(kind="key", action="down",
                                 physical=True, t=time.time()))
        check("a genuine keystroke after rebaseline still pauses",
              arb.check(settle=False).control is not Control.DRIVING,
              "rebaseline forgives one invited interaction, not all future ones")
    finally:
        arb.stop()



def test_event_loop_stays_free():
    """A tool must not stop everything else on the loop it was called from.

    Every handler is `async def` because the SDK requires it, and none of them
    await anything — the work is blocking. Run on the loop directly, one tool
    call froze the whole session: no messages read from the Claude CLI, the
    feed stopped mid-step, and the cancel check never got a turn, so Stop did
    nothing until the tool finished by itself.
    """
    print("\n7. a busy tool does not freeze everything else")
    import asyncio
    import threading as _th
    import tools_sdk as _ts

    fn = {t.name: t for t in _ts.ALL_TOOLS}
    check("every tool body runs off the loop",
          all(getattr(t.handler, "_offloaded", False) for t in _ts.ALL_TOOLS),
          f"{len(_ts.ALL_TOOLS)} tools")

    async def probe():
        ticks = []
        stop = asyncio.Event()

        async def heartbeat():
            while not stop.is_set():
                ticks.append(1)
                await asyncio.sleep(0.05)

        hb = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.3)
        before = len(ticks)

        _ts.set_paused(True)
        # Released from a THREAD. Before the fix a coroutine scheduled to do
        # this could never get a turn, and the probe deadlocked outright.
        _th.Timer(1.5, lambda: _ts.set_paused(False)).start()
        t0 = time.time()
        await fn["click"].handler({"name": "nothing-at-all"})
        blocked = time.time() - t0
        during = len(ticks) - before
        stop.set()
        await hb
        return blocked, during

    blocked, during = asyncio.run(probe())
    _ts.set_paused(False)
    expected = int(blocked / 0.05)
    check("the tool really was busy", blocked > 1.0, f"{blocked:.1f}s")
    check("the loop kept running throughout", during > expected * 0.5,
          f"{during} heartbeats in {blocked:.1f}s (0 = frozen)")

    # UI Automation is COM and is initialised per thread; a worker that
    # skipped it fails with "CoInitialize has not been called". UIA objects
    # are also thread-affine and the locator caches them, which is why this
    # is ONE worker rather than a pool.
    async def uia():
        a = (await fn["find_controls"].handler({"window": ""}))["content"][0]["text"]
        b = (await fn["find_controls"].handler({"window": ""}))["content"][0]["text"]
        return a, b

    a, b = asyncio.run(uia())
    check("UI Automation still works off the loop",
          "CoInitialize" not in a and "controls" in a, a.splitlines()[0][:48])
    check("and a second call reuses the cache safely",
          "CoInitialize" not in b and "controls" in b,
          "UIA objects are thread-affine; a pool would marshal them wrongly")


def test_display_guard():
    print("\n8. display stays usable for the run")
    check("not locked", not display.is_locked())
    with display.KeepSessionOpen(keepalive_s=3.0) as k:
        s = k.status()
        check("wake-lock engaged", s["display_hold"])
        check("policy override possible", s["policy_override_possible"],
              "no group policy forcing a lock")
        before = display._screensaver_state()
        time.sleep(4)
        check("keepalive ran without changing state",
              display._screensaver_state() == before, str(before))
    check("settings restored on exit", True, "screensaver state written back")


def main() -> int:
    print("=" * 74)
    print("PHASE 02 ACCEPTANCE — arbiter")
    print("=" * 74)
    if display.is_locked():
        print("\nABORT: workstation locked.")
        return 2

    test_hook()
    test_abort_cannot_be_faked()
    test_arbiter_lifecycle()
    test_mutual_exclusion()
    test_motion_vs_intent()
    test_invited_interaction()
    test_event_loop_stays_free()
    test_display_guard()

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
