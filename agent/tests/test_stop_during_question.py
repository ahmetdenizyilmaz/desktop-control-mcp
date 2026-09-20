"""Stop and close must work while a task is asking a question.

The report was blunt: "when app asks for user answers it actually prevent
clicking stop and close". Three things were true at once, and this suite pins
all three so none of them comes back:

  * the window: the question block had no height cap, so a long question — a
    numbered list of options is the common case — pushed the composer, Stop
    included, below the bottom edge of a window that cannot scroll;
  * the app: Stop only ended the run when the next message arrived from the
    CLI, which after a "deny" given on the user's behalf meant several seconds
    of apparent nothing; and the close handler joined the task thread on the
    window's own thread, freezing it for up to ten seconds;
  * the SDK: abandoning the message stream mid-iteration left three nested
    generators for asyncio.run() to close concurrently, and they raced —
    "aclose(): asynchronous generator is already running" on every stop.

The Python side is driven directly. The window is rendered in headless
Chromium with a stand-in for the pywebview bridge, because the bug there is
layout and only a layout engine can vouch for a fix to it. Nothing here moves
the mouse or opens a visible window.

Also covered, because they were done at the same time and are cheap to keep
honest: the rename to AGI Agent, the data directory migration that goes with
it, and the icon.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from cleanup import scratch_dir  # noqa: E402  (stdlib only; core untouched)

# Before anything imports core.paths: a run must never touch the real
# settings, vault or macro library.
os.environ["DESKTOP_AGENT_DATA"] = str(scratch_dir("stop_test_"))

import app as appmod                      # noqa: E402
import tools_sdk                          # noqa: E402
from core import channel as chan          # noqa: E402
from core import induce, paths            # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""),
          flush=True)
    return ok


def wait_for(pred, timeout_s=3.0, step=0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()


class _AsyncioErrors(logging.Handler):
    """What asyncio.run() complains about on the way out."""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.lines: list[str] = []

    def emit(self, record):
        text = record.getMessage()
        if record.exc_info and record.exc_info[1] is not None:
            text += f" :: {record.exc_info[1]!r}"
        self.lines.append(text)


def sdk_like_stream(events: list, agent=None):
    """Three async generators nested exactly as the SDK nests them.

    query() -> process_query() -> _process_query_inner(): the middle one
    acloses the innermost in a `finally`, and the innermost *takes time* in
    its own cleanup — the real one awaits transport.close(), which waits up
    to five seconds for the CLI to exit. That shape and that delay are the
    whole bug, so the stand-in keeps them and nothing else.

    With an agent, the innermost generator also does what the SDK does after
    yielding a tool call: runs the tool on another thread and waits for the
    CLI's next message — which, with the tool blocked on a question, never
    comes on its own.
    """
    async def inner():
        try:
            yield "init"
            if agent is not None:
                box = {}

                def tool():
                    box["reply"] = agent._ask_in_app("Continue?", True, 30)

                th = threading.Thread(target=tool, daemon=True)
                th.start()
                events.append(("tool", th, box))
            await asyncio.Event().wait()
        finally:
            events.append("inner: closing transport")
            await asyncio.sleep(0.2)
            events.append("inner: transport closed")

    async def middle():
        gen = inner()
        try:
            async for m in gen:
                yield m
        finally:
            events.append("middle: aclose(inner)")
            await gen.aclose()

    async def outer():
        async for m in middle():
            yield m

    return outer()


def main() -> int:
    print("=" * 72)
    print("STOP WHILE ASKING")
    print("=" * 72)

    print("\n1. Stop releases a question the task is blocked on")
    a = appmod.Agent()
    out = {}

    def ask():
        out["reply"] = a._ask_in_app("Transfer 500 TL to this account?", True, 30)

    th = threading.Thread(target=ask, daemon=True)
    th.start()
    check("the question is pending", wait_for(lambda: a.get_state()["question"]))
    t0 = time.time()
    a.stop_task()
    th.join(timeout=3)
    took = time.time() - t0
    check("Stop releases the wait", not th.is_alive(), f"{took:.2f}s")
    check("within a second", took < 1.5, f"{took:.2f}s")
    r = out.get("reply")
    check("the task hears 'deny' from the app, not silence",
          r is not None and r.answer is chan.Answer.DENY and r.via == "app",
          r.summary() if r else "no reply")
    check("the prompt is cleared", a.get_state()["question"] is None)
    check("the stop reaches the tools", tools_sdk.should_stop())
    tools_sdk.set_cancelled(False)
    a._cancel.clear()

    # The other route in: ESC+ESC and the kill switch set the tools' flag
    # without going through stop_task, and the wait has to notice that too.
    th = threading.Thread(target=ask, daemon=True)
    th.start()
    wait_for(lambda: a.get_state()["question"])
    tools_sdk.set_cancelled(True)
    th.join(timeout=3)
    check("the kill switch releases it as well", not th.is_alive())
    check("and abandons the question", a.get_state()["question"] is None)
    tools_sdk.set_cancelled(False)

    print("\n2. Stop reaches the SDK's stream and unwinds it in order")
    errors = _AsyncioErrors()
    logging.getLogger("asyncio").addHandler(errors)
    a = appmod.Agent()
    events: list = []
    a.running = True

    def run_consume():
        asyncio.run(a._consume(sdk_like_stream(events, a)))

    th = threading.Thread(target=run_consume, daemon=True)
    th.start()
    check("the stand-in tool is asking", wait_for(lambda: a.get_state()["question"]))
    check("the loop is reachable while it asks",
          a._loop is not None and a._main_task is not None and a._streaming)
    t0 = time.time()
    a.stop_task()
    th.join(timeout=5)
    took = time.time() - t0
    check("the run ends", not th.is_alive(), f"{took:.2f}s")
    check("promptly — no waiting for a next message", took < 2.0, f"{took:.2f}s")
    tool = next((e for e in events if isinstance(e, tuple) and e[0] == "tool"), None)
    if tool is not None:
        tool[1].join(timeout=2)
        reply = tool[2].get("reply")
        check("the blocked tool was released with a deny",
              reply is not None and reply.answer is chan.Answer.DENY)
    order = [e for e in events if isinstance(e, str)]
    check("the innermost generator ran its cleanup",
          "inner: closing transport" in order and "inner: transport closed" in order,
          str(order))
    check("in order, inside out",
          order == ["inner: closing transport", "inner: transport closed",
                    "middle: aclose(inner)"],
          str(order))
    feed = [m["text"] for m in a.feed]
    check("and the feed says why", any("Stopped by user" in t for t in feed),
          str(feed[-2:]))
    check("no 'already running' from the shutdown sweep",
          not any("already running" in line for line in errors.lines),
          str(errors.lines)[:120])
    check("the loop handle is released afterwards",
          a._loop is None and a._main_task is None)
    a.running = False
    tools_sdk.set_cancelled(False)

    print("\n3. ...because abandoning it mid-iteration is what raced")
    # Returning out of the loop leaves the chain at its yields. The generator
    # finalizers then close it from the outside in, the innermost cleanup
    # starts awaiting the transport — and asyncio.run() moves on to its
    # shutdown sweep, which acloses the same generators a second time while
    # the first close is still running. That is the logged error; worse, the
    # loop then closes with the first close, and the CLI's termination,
    # still pending.
    errors.lines.clear()
    events2: list = []

    async def abandon(agen):
        async for _ in agen:
            return                        # what the app used to do on Stop

    asyncio.run(abandon(sdk_like_stream(events2)))
    check("the old shape reproduces the SDK error",
          any("already running" in line for line in errors.lines),
          (errors.lines or ["nothing logged"])[0][-80:])
    check("and abandoned the transport close half-way",
          "inner: closing transport" in events2
          and "inner: transport closed" not in events2, str(events2))
    errors.lines.clear()

    # A second Stop must not be a second cancel — that one would land inside
    # the SDK's own cleanup and skip the terminate-then-kill escalation.
    a = appmod.Agent()
    a._streaming = True

    class _Task:
        cancels = 0

        def cancel(self):
            self.cancels += 1

    a._main_task = _Task()
    a._cancel_in_band()
    a._cancel_in_band()
    check("the cancellation is sent once", a._main_task.cancels == 1)
    a._streaming = False
    a._cancel_sent = False
    a._cancel_in_band()
    check("and never before the CLI has spoken", a._main_task.cancels == 1,
          "a stop that early is picked up at the first message instead")
    a._main_task = None
    logging.getLogger("asyncio").removeHandler(errors)

    print("\n4. closing the window during a question does not hang it")
    a = appmod.Agent()
    th = threading.Thread(target=lambda: a._ask_in_app("Really?", True, 30),
                          daemon=True)
    a.running, a._thread = True, th
    th.start()
    wait_for(lambda: a.get_state()["question"])
    t0 = time.time()
    a.begin_shutdown()
    dt = time.time() - t0
    check("the closing handler returns at once", dt < 0.2, f"{dt * 1000:.0f}ms")
    check("having signalled the stop", a._cancel.is_set() and tools_sdk.should_stop())
    ok = a.finish_shutdown(timeout_s=5)
    check("the task thread then ends", ok)
    check("and the question with it", a.get_state()["question"] is None)
    a.running = False
    tools_sdk.set_cancelled(False)
    src = inspect.getsource(appmod.main)
    check("the window hooks it that way round",
          "begin_shutdown" in src and "finish_shutdown" in src
          and ".join(" not in src,
          "signal in the closing handler; wait after the window is gone")
    check("finish_shutdown with nothing running is immediate",
          appmod.Agent().finish_shutdown(timeout_s=1) is True)

    print("\n5. the window keeps Stop reachable while a question is showing")
    try:
        from playwright.sync_api import sync_playwright
        page_url = (ROOT / "ui" / "app.html").resolve().as_uri()
        long_q = "\n".join(
            f"{i}. option {i} — a line of explanation about this choice"
            for i in range(1, 61))
        import json
        state = {
            "running": True, "task": "x", "feed": [], "paused": False,
            "elapsed": 12, "current": None, "missed": [], "schedules": [],
            "settings": {"automation_level": "auto", "browser_headless": True,
                         "watchdog_minutes": 60, "stall_minutes": 10,
                         "max_turns": 0},
            "machine": {"screen": "1920x1080", "dpi": 1, "apps": []},
            "arbiter": None, "browser": {"running": False}, "jobs": [],
            "watchdog": {}, "channel": {}, "question": {
                "text": long_q, "approve_deny": True, "since": time.time()},
        }
        bridge = """
            window.__calls = [];
            window.pywebview = { api: {
              get_state: async () => (%s),
              stop_task: async () => { window.__calls.push('stop_task'); return {ok:true}; },
              list_credentials: async () => [],
            }};
        """ % json.dumps(state)

        def measure(page):
            return page.evaluate("""() => {
                const b = document.getElementById('stop');
                const r = b.getBoundingClientRect();
                const q = document.getElementById('q-stop');
                return {top: r.top, bottom: r.bottom, disabled: b.disabled,
                        height: innerHeight, asking: !document.getElementById('asking').hidden,
                        qstop: q ? !q.hidden && q.getBoundingClientRect().bottom <= innerHeight : false};
            }""")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            for w, h in ((1180, 812), (880, 560)):
                page = browser.new_page(viewport={"width": w, "height": h})
                page.add_init_script(bridge)
                page.goto(page_url)
                page.evaluate("window.dispatchEvent(new Event('pywebviewready'))")
                page.wait_for_function("!document.getElementById('asking').hidden",
                                       timeout=5000)
                m = measure(page)
                check(f"the question is on screen at {w}x{h}", m["asking"])
                check(f"Stop is inside the window at {w}x{h}",
                      m["top"] >= 0 and m["bottom"] <= m["height"],
                      f"button bottom {m['bottom']:.0f} of {m['height']}")
                check("and enabled", not m["disabled"])
                check("and a Stop sits in the question itself", m["qstop"])
                page.click("#stop")
                page.click("#q-stop")
                calls = page.evaluate("window.__calls")
                check("both reach stop_task", calls.count("stop_task") == 2, str(calls))
                if (w, h) == (880, 560):
                    # Take the cap away and the composer leaves the window:
                    # proof the cap is what holds it, not the viewport size.
                    page.add_style_tag(content=".asking .q{max-height:none !important}")
                    m2 = measure(page)
                    check("without the height cap Stop falls off the bottom",
                          m2["bottom"] > m2["height"],
                          f"button bottom {m2['bottom']:.0f} of {m2['height']}")
                page.close()
            browser.close()
    except Exception as e:                 # noqa: BLE001 — reported as a failure
        check("headless layout check ran", False, f"{type(e).__name__}: {e}"[:100])

    print("\n6. the rename is complete where the user can see it")
    check("the window title", appmod.APP_TITLE == "AGI Agent")
    for rel in ("app.py", "ui/app.html", "ui/index.html", "core/channel.py",
                "build_exe.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        check(f"no 'Desktop Agent' left in {rel}", "DesktopAgent" not in text
              and "Desktop Agent" not in text)
    check("the recorder ignores the app under its new name",
          induce._is_our_chrome("AGI Agent"))
    check("and its old one", induce._is_our_chrome("Desktop Agent"))
    check("the packaged build is renamed", paths.APP_NAME == "AGIAgent")
    import build_exe
    check("and so is the executable", build_exe.NAME == "AGIAgent")
    check("the window is found by the title constant",
          "APP_TITLE" in inspect.getsource(appmod.Agent._come_to_front))

    print("\n7. a packaged build keeps its data across the rename")
    tmp = scratch_dir("agi_migrate_")
    old = tmp / "DesktopAgent"
    (old / "macros").mkdir(parents=True)
    (old / "vault.json").write_text("precious", encoding="utf-8")
    new = tmp / "AGIAgent"
    got = paths._adopt_legacy_data(new, [old])
    check("the old directory is moved to the new name",
          got == new and new.is_dir() and not old.exists())
    check("with everything in it",
          (new / "vault.json").read_text(encoding="utf-8") == "precious"
          and (new / "macros").is_dir())
    check("a second start finds it and does nothing",
          paths._adopt_legacy_data(new, [old]) == new and new.is_dir())
    fresh = tmp / "fresh"
    check("a fresh machine simply gets the new name",
          paths._adopt_legacy_data(fresh / "AGIAgent", [fresh / "DesktopAgent"])
          == fresh / "AGIAgent")
    held_dir = tmp / "held" / "DesktopAgent"
    held_dir.mkdir(parents=True)
    fh = open(held_dir / "open.json", "w", encoding="utf-8")
    try:
        got = paths._adopt_legacy_data(tmp / "held" / "AGIAgent", [held_dir])
    finally:
        fh.close()
    if got == held_dir:
        check("a refused move keeps using the old directory, nothing lost",
              held_dir.is_dir() and (held_dir / "open.json").exists())
    else:
        check("the move went through despite the open file (fine — nothing lost)",
              got.is_dir() and (got / "open.json").exists())

    # The real entry point, as a frozen build would hit it.
    la = tmp / "la"
    (la / "DesktopAgent").mkdir(parents=True)
    (la / "DesktopAgent" / "settings.json").write_text("{}", encoding="utf-8")
    saved_env = dict(os.environ)
    saved_frozen = paths.FROZEN
    try:
        os.environ.pop("DESKTOP_AGENT_DATA", None)
        os.environ["LOCALAPPDATA"] = str(la)
        paths.FROZEN = True
        root = paths.data_root()
    finally:
        paths.FROZEN = saved_frozen
        os.environ.clear()
        os.environ.update(saved_env)
    check("a packaged build adopts the old directory on first start",
          root == la / "AGIAgent" and (root / "settings.json").exists()
          and not (la / "DesktopAgent").exists(), str(root))
    check("the developer override still wins over all of that",
          paths.data_root() == Path(os.environ["DESKTOP_AGENT_DATA"]))

    print("\n8. the icon")
    ico = ROOT / "assets" / "icon.ico"
    check("exists", ico.is_file(), str(ico))
    try:
        from PIL import Image
        with Image.open(ico) as im:
            sizes = {s[0] for s in im.ico.sizes()}
            im.size = (16, 16)
            im.load()
            small = im.convert("RGBA")
        check("carries every size the shell asks for",
              {16, 32, 48, 64, 128, 256} <= sizes, str(sorted(sizes)))
        opaque = sum(1 for px in small.getdata() if px[3] > 200)
        whites = sum(1 for px in small.getdata() if px[3] > 200 and min(px[:3]) > 200)
        check("the 16px frame is a drawn mark, not a blur",
              160 < opaque < 256 and 12 <= whites <= 80,
              f"{opaque} opaque px, {whites} white px")
    except Exception as e:                 # noqa: BLE001
        check("icon readable", False, f"{type(e).__name__}: {e}")
    import webview
    check("pywebview's start() takes an icon",
          "icon" in inspect.signature(webview.start).parameters)
    check("and is given this one", appmod.ICON.resolve() == ico.resolve()
          and "icon=" in inspect.getsource(appmod.main))
    check("the title bar and taskbar get the frames drawn for their size",
          callable(getattr(appmod, "_apply_window_icon", None))
          and "_apply_window_icon" in inspect.getsource(appmod.main),
          "WM_SETICON at the system's small and large icon metrics, once shown")
    html = (ROOT / "ui" / "app.html").read_text(encoding="utf-8")
    check("the page has a favicon",
          '<link rel="icon"' in html and 'href="data:image/png;base64,' in html)
    check("the exe is built with it", build_exe.ICON == ico
          and '"--icon"' in inspect.getsource(build_exe.build))
    check("and ships it for the window", any(src == ico for src, _ in build_exe.DATA))

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
