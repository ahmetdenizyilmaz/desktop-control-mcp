"""Seeing and typing: the layer a real task actually fails on.

Every check here is READ-ONLY — it reads the UI tree, maps characters to keys,
and captures the screen. Nothing is clicked and nothing is typed, so this runs
safely while the machine is in use, which is exactly when these regressions
need catching. The exceptions are the last two sections, each confined to a
small window the suite launches itself: one maximises and restores it, the
other watches it while its text changes.

These exist because a real run died here. On a Turkish keyboard `press_key("/")`
typed a 7, so WhatsApp's ctrl+alt+/ shortcut did nothing; page content sat
below the walk's depth limit so a web page looked empty; and a screenshot read
back through a file was too big for one message and killed the task.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cleanup import scratch_dir  # noqa: E402

if "DESKTOP_AGENT_DATA" not in os.environ:
    os.environ["DESKTOP_AGENT_DATA"] = str(scratch_dir("dl_test_"))

from core import actuator, locator      # noqa: E402
import tools_sdk                        # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""),
          flush=True)
    return bool(ok)


# ---------------------------------------------------------------- keyboard

def test_layout():
    print("\n1. characters are mapped through the CURRENT keyboard layout")
    # True on every Latin layout: a capital is the shifted form of its letter.
    lower, upper = actuator.char_key("a"), actuator.char_key("A")
    check("a lowercase letter needs no modifier", lower is not None and lower[1] == [])
    check("its capital needs shift",
          upper is not None and actuator.VK["shift"] in upper[1],
          str(upper))
    check("both are the same physical key",
          lower and upper and lower[0] == upper[0])

    # The bug: the shift state used to be masked off, so a character that
    # lives on a shifted key was sent as the UNSHIFTED one.
    shifted = [c for c in "/?:_+@" if (actuator.char_key(c) or (0, []))[1]]
    check("at least one punctuation key reports its modifiers",
          bool(shifted), f"shifted on this layout: {''.join(shifted) or 'none'}")
    for c in shifted:
        vk, mods = actuator.char_key(c)
        bare = chr(vk) if 32 < vk < 127 else str(vk)
        if bare != c:
            check(f"{c!r} is not silently sent as {bare!r}", True,
                  f"needs {len(mods)} modifier(s)")
            break

    check("a character the layout cannot produce is refused, not faked",
          actuator.char_key("漢") is None)


def test_combos():
    print("\n2. key combinations parse the way a person would write them")
    check("a normal combo splits", actuator._split_combo("ctrl+shift+s")
          == ["ctrl", "shift", "s"])
    check("a trailing + is the plus KEY, not a separator",
          actuator._split_combo("ctrl++") == ["ctrl", "+"],
          str(actuator._split_combo("ctrl++")))
    check("punctuation shortcuts survive",
          actuator._split_combo("ctrl+alt+/") == ["ctrl", "alt", "/"])

    r = actuator.send_keys("ctrl+notakey")
    check("an unknown key fails loudly rather than pressing something else",
          r.ok is False and "notakey" in r.detail, r.detail)
    r = actuator.press_key("notakey")
    check("so does an unknown single key", r.ok is False, r.detail)
    check("an empty batch is never reported as success", actuator._send([]) is False)

    # A real run spent a turn on `Failed: unknown key 'page_up'` — the key
    # was in the table as "pageup". A spelling the model reasonably guesses
    # must land on the same key as the canonical one, not on a dead end.
    for alias, canon in (("page_up", "pageup"), ("pgup", "pageup"),
                         ("page_down", "pagedown"), ("pagedn", "pagedown"),
                         ("pgdn", "pagedown"), ("ins", "insert"),
                         ("arrowup", "up"), ("arrow_down", "down"),
                         ("windows", "win")):
        check(f"{alias!r} is the same key as {canon!r}",
              actuator.VK.get(alias) is not None
              and actuator.VK.get(alias) == actuator.VK[canon])
    check("a combo spelt with an alias resolves too — ctrl+page_down switches tabs",
          all(p in actuator.VK for p in actuator._split_combo("ctrl+page_down")))
    r = actuator.press_key("page_sideways")
    check("a key that genuinely does not exist still fails loudly",
          r.ok is False and "unknown key" in r.detail, r.detail)


def test_typing_shapes():
    print("\n3. text is broken into characters and real control keys")
    check("a trailing newline becomes its own piece — a real Enter",
          actuator._segment("https://example.com/\n") == ["https://example.com/", "\n"])
    check("tabs separate fields", actuator._segment("a\tb") == ["a", "\t"] + ["b"])
    check("windows line endings do not double up",
          actuator._segment("a\r\nb") == ["a", "\n", "b"])
    check("ordinary text is one piece",
          actuator._segment("bu saat nasıl sizce beyler")
          == ["bu saat nasıl sizce beyler"])
    check("empty text types nothing", actuator._segment("") == [])

    # wScan is 16 bits, so an astral character has to go out as a surrogate
    # PAIR or it arrives as a different character entirely.
    check("an emoji is sent as a surrogate pair (4 inputs)",
          len(actuator._unicode_char("\U0001F60A")) == 4)
    check("a Turkish letter is a single unit (2 inputs)",
          len(actuator._unicode_char("ı")) == 2)


# ---------------------------------------------------------------- naming

def test_names():
    print("\n4. a name that was listed can be clicked")
    check("padding and case do not matter",
          locator.norm_name("  Sepete  Ekle ") == locator.norm_name("sepete ekle"))
    check("inner runs of space collapse",
          locator.norm_name("Add   to  cart") == "add to cart")

    long_name = "Xiaomi Redmi Watch 5 Active Gümüş Akıllı Saat"
    check("a name truncated for display still matches",
          locator.name_is_long_prefix(long_name, long_name[:36]))
    check("a short prefix does not — 'Search' must not match 'Search or start…'",
          not locator.name_is_long_prefix("Search or start a new chat", "Search"))
    check("an exact name is not treated as a prefix",
          not locator.name_is_long_prefix("abc", "abc"))


# ---------------------------------------------------------------- walking

def test_walk():
    print("\n5. the UI walk is breadth-first and admits when it is cut short")
    h = locator.foreground_window()
    if not h:
        check("a window to walk", False, "no foreground window")
        return
    root = locator.uia_root(h)
    if root is None:
        check("a window to walk", False, "no UIA root")
        return

    deep, shallow = {}, {}
    names_deep = set()
    for c in locator.walk(root, max_depth=locator.WALK_DEPTH, max_nodes=2500,
                          budget_s=4.0, stats=deep):
        try:
            n = (c.Name or "").strip()
        except Exception:
            continue
        if n:
            names_deep.add(n)
    names_shallow = set()
    locator.invalidate_cache(h)
    for c in locator.walk(locator.uia_root(h), max_depth=6, max_nodes=2500,
                          budget_s=4.0, stats=shallow):
        try:
            n = (c.Name or "").strip()
        except Exception:
            continue
        if n:
            names_shallow.add(n)

    check("the default depth reaches at least as much as a shallow one",
          names_shallow <= names_deep or len(names_deep) >= len(names_shallow),
          f"{len(names_shallow)} shallow vs {len(names_deep)} deep")
    check("stats report a completed walk honestly",
          deep.get("truncated") in (True, False) and deep.get("nodes", 0) > 0,
          str(deep))

    # Force truncation and confirm it is reported rather than looking empty.
    cut = {}
    seen = 0
    locator.invalidate_cache(h)
    for _ in locator.walk(locator.uia_root(h), max_nodes=5, budget_s=4.0, stats=cut):
        seen += 1
    check("a walk cut by the node budget says so",
          cut.get("truncated") is True and cut.get("reason") == "nodes", str(cut))
    check("and it stopped at the budget", seen <= 5, f"{seen} nodes yielded")

    # Breadth-first is the whole point: when the budget cuts the walk, what
    # survives must be the shallowest nodes. Depth-first would spend the same
    # budget inside the first branch — which is how raising the depth limit
    # to reach web content made browser toolbar buttons unfindable.
    #
    # Identity by (type, name, rect): UIA hands back a fresh wrapper object
    # each call, so `is` and `id()` compare nothing useful.
    def ident(c):
        try:
            return (c.ControlTypeName, (c.Name or "").strip(), locator._rect_of(c))
        except Exception:
            return None

    locator.invalidate_cache(h)
    direct = {ident(c) for c in locator.uia_root(h).GetChildren()}
    direct.discard(None)
    locator.invalidate_cache(h)
    kids = [ident(c) for c in
            locator.walk(locator.uia_root(h), max_nodes=3, budget_s=4.0)]
    kids = [k for k in kids if k is not None]
    check("a truncated walk returns only TOP-LEVEL nodes, not one deep branch",
          bool(kids) and all(k in direct for k in kids),
          f"{len(kids)} yielded, {sum(1 for k in kids if k in direct)} of them "
          f"direct children of the root")


# ---------------------------------------------------------------- tools

def test_seeing_tools():
    print("\n6. the seeing tools return usable answers, never an exception")
    h = locator.foreground_window()
    title = locator.window_title(h) or ""
    key = title[:18]

    out = asyncio.run(tools_sdk.find_controls.handler({"window": key}))
    text = out["content"][0]["text"]
    check("find_controls answers for the focused window", "controls" in text,
          text.splitlines()[0][:70])

    out = asyncio.run(tools_sdk.find_controls.handler(
        {"window": key, "filter": "zzqx-nothing-is-called-this"}))
    text = out["content"][0]["text"]
    check("a filter that matches nothing says so plainly",
          "0 controls" in text and "Nothing matched" in text,
          text.splitlines()[0][:70])

    out = asyncio.run(tools_sdk.find_controls.handler({"window": "no-such-window-xyz"}))
    check("an unknown window is a message, not a crash",
          "No window matching" in out["content"][0]["text"])

    r = asyncio.run(tools_sdk.screenshot.handler({}))["content"]
    img = next((c for c in r if c.get("type") == "image"), None)
    note = next((c["text"] for c in r if c.get("type") == "text"), "")
    check("screenshot returns the picture itself", img is not None,
          str([c.get("type") for c in r]))
    check("small enough for one message (the 1 MB limit that killed a task)",
          img is not None and len(img["data"]) < 800_000,
          f"{len(img['data']) if img else 0} base64 chars")
    check("the full-size file is saved under the data root, not beside the code",
          "saved full-size to" in note
          and os.environ["DESKTOP_AGENT_DATA"] in note, note[:90])

    r = asyncio.run(tools_sdk.screenshot.handler({"window": "no-such-window-xyz"}))
    check("screenshot of a window that is not there says so",
          "No window matching" in r["content"][0]["text"])


# ---------------------------------------------------------------- window size

def test_maximize():
    print("\n7. a window can be made to fill the screen, and left alone when it does")
    # A browser in a small window shows less of the page, and what is off
    # screen cannot be found or clicked. This is the only section that acts:
    # on a window of its own, restored and closed before it finishes, and
    # titled so tests/cleanup.py sweeps it if the suite dies half-way.
    u = ctypes.windll.user32
    title = f"maximise probe desktopagent{os.getpid():06d}"
    fixture = ("import tkinter as tk\n"
               f"r = tk.Tk(); r.title({title!r}); r.geometry('320x140-8-60')\n"
               "tk.Label(r, text='maximise probe').pack()\n"
               "r.after(20000, r.destroy)\n"          # never outlives the test
               "r.mainloop()\n")
    proc = subprocess.Popen([sys.executable, "-c", fixture],
                            creationflags=0x08000000)     # CREATE_NO_WINDOW
    h = None
    try:
        for _ in range(100):
            h = locator.find_window(title_contains=title)
            if h:
                break
            time.sleep(0.05)
        if not check("a fixture window to work on", bool(h)):
            return
        before = locator.window_rect(h)
        check("a fresh small window is not reported as filling the screen",
              not actuator.is_maximized(h) and not actuator.fills_screen(h))

        check("maximize_window reports that it worked", actuator.maximize_window(h))
        check("and Windows itself agrees", bool(u.IsZoomed(h)),
              "IsZoomed is the independent witness, not our own bookkeeping")
        after = locator.window_rect(h)
        grew = ((after[2] - after[0]) * (after[3] - after[1])
                > 4 * (before[2] - before[0]) * (before[3] - before[1]))
        check("the window actually got bigger", grew, f"{before} -> {after}")
        check("a maximised window counts as filling the screen",
              actuator.fills_screen(h))
        check("asking again is a harmless yes, not a toggle back down",
              actuator.maximize_window(h) and bool(u.IsZoomed(h)))

        u.ShowWindow(h, actuator.SW_RESTORE)
        for _ in range(50):
            if not u.IsZoomed(h):
                break
            time.sleep(0.03)
        check("restored, so the suite leaves nothing maximised",
              not actuator.is_maximized(h))

        # The tool the model calls. focus_window's foreground escalation
        # presses a synthetic ALT when Windows refuses the first attempt —
        # with a person typing in another window, that is an ALT into their
        # application. It has its own suite; here it is stood in for, so that
        # only the new maximise path runs.
        real_focus = actuator.focus_window
        actuator.focus_window = lambda hwnd, timeout_s=1.5: True
        try:
            out = asyncio.run(tools_sdk.focus_window.handler(
                {"title": title, "maximize": True}))
            text = out["content"][0]["text"]
            check("focus_window(maximize=true) maximises through the tool",
                  "maximised" in text and bool(u.IsZoomed(h)), text)
            out = asyncio.run(tools_sdk.focus_window.handler(
                {"title": title, "maximize": True}))
            text = out["content"][0]["text"]
            check("a second call says it already fills the screen",
                  "already fills the screen" in text and bool(u.IsZoomed(h)), text)
        finally:
            actuator.focus_window = real_focus
        u.ShowWindow(h, actuator.SW_RESTORE)
        time.sleep(0.1)
        check("restored again afterwards", not actuator.is_maximized(h))
    finally:
        proc.kill()

    check("a dead handle is a plain False, not an exception",
          actuator.maximize_window(0) is False
          and actuator.is_maximized(0) is False
          and actuator.fills_screen(0) is False)
    out = asyncio.run(tools_sdk.focus_window.handler(
        {"title": "no-such-window-xyz", "maximize": True}))
    check("the tool with a window that is not there says so",
          "No window matching" in out["content"][0]["text"])
    check("the tool declares the argument, so the model can see it",
          "maximize" in tools_sdk.focus_window.input_schema)


# ---------------------------------------------------------------- waiting

def test_wait_for_reply():
    print("\n8. wait_for_reply notices new content, times out honestly, and stops")

    # The sift is where the edge cases live, so it is proved directly first:
    # the two-sighting debounce, and the ignore-disappearances rule.
    base = {"a": "A", "b": "B"}
    conf, pend = tools_sdk._sift_new({"a": "A", "b": "B"}, base, {})
    check("an unchanged window reports nothing", not conf and not pend)
    conf, pend = tools_sdk._sift_new({"a": "A"}, base, {})
    check("a disappearance alone is never a reply", not conf and not pend)
    conf, pend = tools_sdk._sift_new({"a": "A", "t": "typing…"}, base, {})
    check("a first sighting is held back, not reported",
          not conf and pend == {"t": "typing…"})
    conf, pend2 = tools_sdk._sift_new({"a": "A"}, base, pend)
    check("flicker gone by the next poll is dropped for good",
          not conf and not pend2)
    conf, _ = tools_sdk._sift_new({"a": "A", "t": "typing…"}, base, pend)
    check("still there on the second poll IS the reply",
          conf == {"t": "typing…"})

    # Now against a live window. tkinter is no use as a fixture here: its
    # widgets draw on one hwnd and the UIA walk sees none of them (probed —
    # only the System menu and caption buttons come back). Native STATIC
    # children expose their text as their UIA name, which is exactly how a
    # chat message carries its text, so the fixture is built from those.
    u = ctypes.windll.user32
    u.FindWindowExW.restype = ctypes.c_void_p
    u.FindWindowExW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                ctypes.c_wchar_p, ctypes.c_wchar_p]
    u.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                               ctypes.c_void_p, ctypes.c_wchar_p]
    title = f"reply probe desktopagent{os.getpid():06d}"
    nonce = "arrived zebra quill 314159"
    fixture = r'''
import ctypes, sys
from ctypes import wintypes
u = ctypes.windll.user32
u.CreateWindowExW.restype = wintypes.HWND
u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                              wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, wintypes.HWND,
                              ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
h = u.CreateWindowExW(0x08000000, "STATIC", sys.argv[1],
                      0x00CF0000 | 0x10000000, sw - 268, sh - 220, 260, 140,
                      None, None, None, None)
u.CreateWindowExW(0, "STATIC", "waiting for the answer", 0x50000000,
                  8, 8, 220, 20, h, None, None, None)
TIMERPROC = ctypes.WINFUNCTYPE(None, wintypes.HWND, ctypes.c_uint,
                               ctypes.POINTER(ctypes.c_uint), wintypes.DWORD)
end = TIMERPROC(lambda *a: u.PostQuitMessage(0))
u.SetTimer(h, 2, 40000, end)                       # never outlives the test
msg = wintypes.MSG()
while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
    u.TranslateMessage(ctypes.byref(msg))
    u.DispatchMessageW(ctypes.byref(msg))
'''
    proc = subprocess.Popen([sys.executable, "-c", fixture, title],
                            creationflags=0x08000000)     # CREATE_NO_WINDOW
    try:
        h = None
        for _ in range(100):
            h = locator.find_window(title_contains=title)
            if h:
                break
            time.sleep(0.05)
        if not check("a fixture window to watch", bool(h)):
            return

        # (a) the reply lands mid-wait, from another process, the way a real
        # message does — and comes back verbatim, well inside the timeout.
        # WM_SETTEXT also makes the OLD name vanish, so this only passes
        # because disappearances are ignored.
        def deliver():
            child = u.FindWindowExW(h, None, "Static", "waiting for the answer")
            u.SendMessageW(child, 0x000C, None, nonce)        # WM_SETTEXT
        threading.Timer(2.0, deliver).start()
        t0 = time.time()
        out = asyncio.run(tools_sdk.wait_for_reply.handler(
            {"window": title, "timeout_s": 30}))
        took = time.time() - t0
        text = out["content"][0]["text"]
        check("the new text is returned verbatim", nonce in text,
              text.splitlines()[0][:70])
        check("and well before the timeout", took < 25, f"{took:.1f}s")

        snap = tools_sdk._reply_snapshot(h, "zebra")
        check("watch= narrows the snapshot to matching names",
              snap is not None and list(snap.values()) == [nonce], str(snap))

        # (b) a quiet window: the timeout message must invite the re-call
        # loop, and must arrive on time — not after a hidden grace period.
        t0 = time.time()
        out = asyncio.run(tools_sdk.wait_for_reply.handler(
            {"window": title, "timeout_s": 3}))
        took = time.time() - t0
        text = out["content"][0]["text"]
        check("a quiet window returns the call-again message",
              "Call wait_for_reply again" in text, text[:72])
        check("respecting a small timeout_s", took < 12, f"{took:.1f}s")

        # (c) Stop mid-wait has to win within seconds. The user has watched
        # this app sit unstoppable inside a long wait once already.
        threading.Timer(1.5, tools_sdk.set_cancelled).start()
        t0 = time.time()
        try:
            out = asyncio.run(tools_sdk.wait_for_reply.handler(
                {"window": title, "timeout_s": 60}))
        finally:
            tools_sdk.set_cancelled(False)
        took = time.time() - t0
        text = out["content"][0]["text"]
        check("Stop wins mid-wait", text.startswith("STOP"), text[:60])
        check("and wins within seconds, not at the timeout", took < 10,
              f"{took:.1f}s")
    finally:
        proc.kill()

    out = asyncio.run(tools_sdk.wait_for_reply.handler(
        {"window": "no-such-window-xyz", "timeout_s": 5}))
    check("an unknown window is an immediate message, not a wait",
          "No window matching" in out["content"][0]["text"])
    check("registered, offloaded, and its arguments declared",
          any(tl.name == "wait_for_reply" for tl in tools_sdk.ALL_TOOLS)
          and getattr(tools_sdk.wait_for_reply.handler, "_offloaded", False)
          and all(k in tools_sdk.wait_for_reply.input_schema
                  for k in ("window", "watch", "timeout_s")))
    check("the system prompt teaches the loop, not repeat-pinging",
          "wait_for_reply" in tools_sdk.SYSTEM_PROMPT
          and "NEVER send the message again" in tools_sdk.SYSTEM_PROMPT)


def test_prefix_and_dpi():
    print("\n9. short-prefix clicking and the worker's coordinate space")
    row = "Furkan Fizix 15:30 Alo duyam\u0131yorum seni"
    check("a contact name resolves a chat row whose preview changed",
          locator.name_is_long_prefix(row, "Furkan Fizix"))
    check("case and whitespace still fold", locator.name_is_long_prefix(row, "furkan  fizix"))
    check("but half a name is a guess and is refused",
          not locator.name_is_long_prefix(row, "Furkan F"))
    check("and a short prefix cut mid-token is refused",
          not locator.name_is_long_prefix(row, "Furkan Fizix 15"))
    check("equal names are not a prefix", not locator.name_is_long_prefix("abc def ghi jkl", "abc def ghi jkl"))
    check("24+ characters keep the old rule even mid-word",
          locator.name_is_long_prefix("a" * 30 + " tail", "a" * 24))

    # The thread every tool runs on must see physical pixels: UIA rects are
    # physical, and _norm() divides by GetSystemMetrics on this thread.
    import ctypes
    async def probe(_):
        u = ctypes.windll.user32
        u.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        ctx = u.GetThreadDpiAwarenessContext()
        return bool(u.AreDpiAwarenessContextsEqual(
            ctypes.c_void_p(ctx), ctypes.c_void_p(-4)))
    box, done = tools_sdk._worker.submit(probe, None)
    done.wait(5)
    kind, value = box[0]
    check("the tools worker thread is per-monitor-v2 DPI aware",
          kind == "ok" and value is True, f"{kind}:{value}")


def test_do_steps():
    print("\n10. do_steps runs a whole sequence in one call, and stops when it should")
    import json

    def run(a):
        return asyncio.run(tools_sdk.do_steps.handler(a))["content"][0]["text"]

    # (a) shape errors are named, not swallowed
    check("garbage is refused", "JSON list" in run({"steps": "not json"}))
    check("bare values are refused", "not objects" in run({"steps": "[1, 2]"}))
    check("an unknown kind is refused, by number",
          "Unknown step kind at 1" in run({"steps": '[{"do":"dance"}]'}))
    check("an absurd batch is refused", "too many" in run(
        {"steps": json.dumps([{"do": "wait", "seconds": 0}] * 151)}))

    # (b) keys and typing go out in order, through the same actuator calls
    # the single tools use — stubbed, so nothing reaches the keyboard.
    calls = []
    real = (actuator.press_key, actuator.send_keys, actuator.type_text)
    ok_r = actuator.ActResult(True, "stub", 0)
    actuator.press_key = lambda k, n=1: (calls.append(("press", k, n)), ok_r)[1]
    actuator.send_keys = lambda k, n=1: (calls.append(("combo", k, n)), ok_r)[1]
    actuator.type_text = lambda t, el=None: (calls.append(("type", t)), ok_r)[1]
    try:
        text = run({"steps": json.dumps([
            {"do": "type", "text": "abc"},
            {"do": "key", "keys": "ctrl+s", "times": 2},
            {"do": "key", "keys": "enter"},
            {"do": "wait", "seconds": 0.1}])})
    finally:
        actuator.press_key, actuator.send_keys, actuator.type_text = real
    check("four steps, four ok lines",
          text.startswith("4/4 steps") and text.count("ok  ") == 4, text[:60])
    check("in order, with times honoured for combos",
          calls == [("type", "abc"), ("combo", "ctrl+s", 2), ("press", "enter", 1)],
          str(calls))

    # (c) a failure stops the batch, says where, and shows what IS there.
    # A native window with one captioned child, as in section 8: its caption
    # is a real UIA name, which is what the failure report must list.
    u = ctypes.windll.user32
    title = f"steps probe desktopagent{os.getpid():06d}"
    fixture = r'''
import ctypes, sys
from ctypes import wintypes
u = ctypes.windll.user32
u.CreateWindowExW.restype = wintypes.HWND
u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                              wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, wintypes.HWND,
                              wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
h = u.CreateWindowExW(0x08000000, "STATIC", sys.argv[1], 0x10CF0000,
                      40, 40, 360, 200, None, None, None, None)
u.CreateWindowExW(0, "STATIC", "probe caption quill", 0x50000000,
                  10, 10, 300, 30, h, None, None, None)
u.ShowWindow(h, 8)
TIMERPROC = ctypes.WINFUNCTYPE(None, wintypes.HWND, ctypes.c_uint,
                               ctypes.POINTER(ctypes.c_uint), wintypes.DWORD)
end = TIMERPROC(lambda *a: u.PostQuitMessage(0))
u.SetTimer(h, 2, 30000, end)
msg = wintypes.MSG()
while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
    u.TranslateMessage(ctypes.byref(msg))
    u.DispatchMessageW(ctypes.byref(msg))
'''
    proc = subprocess.Popen([sys.executable, "-c", fixture, title],
                            creationflags=0x08000000)     # CREATE_NO_WINDOW
    try:
        h = None
        for _ in range(100):
            h = locator.find_window(title_contains=title)
            if h:
                break
            time.sleep(0.05)
        if not check("a fixture window to act in", bool(h)):
            return
        typed = []
        real_type = actuator.type_text
        actuator.type_text = lambda t, el=None: (typed.append(t), ok_r)[1]
        try:
            missing = "no such control zzz 314159"
            text = run({"window": title, "steps": json.dumps([
                {"do": "wait", "seconds": 0.05},
                {"do": "click", "name": missing},
                {"do": "type", "text": "never typed"}])})
            check("stopped at the failing step",
                  text.startswith("1/3 steps") and "Stopped at step 2" in text,
                  text[:80])
            check("later steps were not done", not typed and "NOT done" in text)
            check("and what IS on screen is listed", "probe caption quill" in text,
                  text[-160:])
            text = run({"window": title, "stop_on_error": False,
                        "steps": json.dumps([
                            {"do": "click", "name": missing},
                            {"do": "type", "text": "typed anyway"}])})
            check("stop_on_error=false carries on",
                  text.startswith("1/2 steps") and typed == ["typed anyway"],
                  text[:60])
            typed.clear()
            text = run({"window": title, "steps": json.dumps([
                {"do": "click", "name": missing, "optional": True},
                {"do": "type", "text": "after optional"}])})
            check("an optional failure carries on too",
                  text.startswith("1/2 steps") and typed == ["after optional"],
                  text[:60])
            text = run({"window": "no-such-window-xyz-314159",
                        "steps": '[{"do":"click","name":"anything"}]'})
            check("a missing window is named, not searched around",
                  "no open window matches" in text, text[:80])
        finally:
            actuator.type_text = real_type

        # (d) Stop between steps — and inside a wait — wins within seconds.
        threading.Timer(0.8, tools_sdk.set_cancelled).start()
        t0 = time.time()
        try:
            text = run({"steps": json.dumps([{"do": "wait", "seconds": 0.5}] * 20)})
        finally:
            tools_sdk.set_cancelled(False)
        took = time.time() - t0
        check("Stop halts the batch", "STOP" in text and not text.startswith("20/20"),
              text[:70])
        check("within seconds, not at the end", took < 4, f"{took:.1f}s")
    finally:
        proc.terminate()

    check("do_steps is registered and offloaded",
          any(t.name == "do_steps" for t in tools_sdk.ALL_TOOLS)
          and getattr(tools_sdk.do_steps.handler, "_offloaded", False))
    check("the system prompt teaches batching", "do_steps" in tools_sdk.SYSTEM_PROMPT)


def test_mouse_path_mapping():
    print("\n11. mouse_path maps picture coordinates onto the window exactly")
    parse, mapper = tools_sdk._parse_strokes, tools_sdk._map_strokes
    s, err = parse("[[1,2],[3,4]]")
    check("one stroke parses", err is None and s == [[(1.0, 2.0), (3.0, 4.0)]], str(err))
    s, err = parse("[[[1,2],[3,4]],[[5,6]]]")
    check("several strokes parse", err is None and len(s) == 2 and s[1] == [(5.0, 6.0)],
          str(err))
    check("garbage is refused", parse("nope")[1] is not None)
    check("a bare number list is refused", parse("[1,2,3]")[1] is not None)
    check("booleans are not coordinates", parse("[[true,false]]")[1] is not None)
    check("an empty stroke is refused", parse("[[[1,2]],[]]")[1] is not None)

    region = (1000, 500, 800, 400)          # a window at (1000,500), 800x400
    m, err = mapper([[(0, 0), (400, 200), (800, 400)]], (0, 0), region)
    check("window pixels map 1:1", err is None
          and m == [[(1000, 500), (1400, 700), (1800, 900)]], str(m))
    m, err = mapper([[(200, 100)]], (400, 200), region)
    check("a half-size picture is scaled back up", err is None and m == [[(1400, 700)]],
          str(m))
    m, err = mapper([[(10, 10)], [(450, 100)]], (400, 200), region)
    check("a point off the picture is refused, by stroke",
          err is not None and "stroke 2" in err, str(err))

    check("interpolation fills the gaps at the asked spacing",
          len(actuator._interpolate([(0, 0), (30, 0)], 3)) == 11)
    check("and keeps the endpoints exact",
          actuator._interpolate([(0, 0), (30, 40)], 3)[-1] == (30, 40))

    def run(a):
        return asyncio.run(tools_sdk.mouse_path.handler(a))["content"][0]["text"]

    base = {"points": "[[1,1]]", "action": "move", "frame_w": 0, "frame_h": 0,
            "button": "left"}
    text = run({**base, "window": "no-such-window-xyz-314159"})
    check("a missing window is named, nothing is injected",
          "No window matching" in text, text[:60])
    text = run({**base, "window": "", "action": "fly"})
    check("an unknown action is refused before anything moves",
          "action must be" in text, text[:60])
    check("mouse_path is registered and offloaded",
          any(t.name == "mouse_path" for t in tools_sdk.ALL_TOOLS)
          and getattr(tools_sdk.mouse_path.handler, "_offloaded", False))
    check("the system prompt teaches drawing", "mouse_path" in tools_sdk.SYSTEM_PROMPT
          and "frame_w" in tools_sdk.SYSTEM_PROMPT)


def test_sample_colors():
    print("\n12. sample_colors reads exact colours off the screen")

    def run(a):
        return asyncio.run(tools_sdk.sample_colors.handler(a))["content"][0]["text"]

    check("black is black", tools_sdk._color_name(0, 0, 0) == "black")
    check("a near-white is white", tools_sdk._color_name(250, 250, 248) == "white")
    check("a strong red is red", tools_sdk._color_name(200, 20, 30) == "red")
    text = run({"window": "no-such-window-xyz-314159", "points": "[[1,1]]",
                "frame_w": 0, "frame_h": 0})
    check("a missing window is named", "No window matching" in text, text[:60])
    text = run({"window": "", "points": "nope", "frame_w": 0, "frame_h": 0})
    check("bad points are refused", "points must be" in text, text[:60])
    text = run({"window": "", "points": "[[1,1]]", "frame_w": 0, "frame_h": 0})
    check("a real point comes back as #rrggbb with a name",
          "#" in text and "~" in text, text[:80])
    # The mosaic is what turns "paint the Mona Lisa" from freehand into a
    # copy. Proved on a synthetic picture: two flat halves must come back as
    # two colours in the right places.
    from PIL import Image
    pic = Image.new("RGB", (100, 100), (220, 30, 30))
    for x in range(50, 100):
        for y in range(100):
            pic.putpixel((x, y), (40, 80, 220))
    palette, rows = tools_sdk._mosaic(pic, (0, 0, 100, 100), 4, 2, 8)
    names = sorted(tools_sdk._color_name(*rgb) for _, rgb, _ in palette)
    check("two flat halves reduce to exactly two colours", names == ["blue", "red"],
          str(names))
    check("the rows place them left and right", len(rows) == 2 and rows[0] == rows[1]
          and rows[0][0] == rows[0][1] and rows[0][2] == rows[0][3]
          and rows[0][0] != rows[0][3], str(rows))
    check("shares add up", abs(sum(s for _, _, s in palette) - 1.0) < 1e-6)
    text = run({"window": "", "points": "", "frame_w": 0, "frame_h": 0,
                "rect": "[0,0,10,10]", "grid_w": 1, "grid_h": 5, "palette": 4})
    check("an absurd grid is refused", "between 2 and 64" in text, text[:60])
    text = run({"window": "", "points": "", "frame_w": 100, "frame_h": 100,
                "rect": "[50,50,20,20]", "grid_w": 4, "grid_h": 4, "palette": 4})
    check("an inside-out rect is refused", "x2 > x1" in text, text[:60])
    text = run({"window": "", "points": "", "frame_w": 0, "frame_h": 0,
                "rect": "", "grid_w": 4, "grid_h": 4, "palette": 3})
    check("a mosaic of the front window comes back as rows of letters",
          "Mosaic of" in text and "Rows top to bottom" in text
          and any(len(l.strip()) == 4 and l.strip().isalpha() for l in text.splitlines()),
          text[:100])
    check("sample_colors is registered and offloaded",
          any(t.name == "sample_colors" for t in tools_sdk.ALL_TOOLS)
          and getattr(tools_sdk.sample_colors.handler, "_offloaded", False))
    check("the system prompt teaches the rule-then-batch strategy",
          "sample_colors" in tools_sdk.SYSTEM_PROMPT
          and "work out the rule" in tools_sdk.SYSTEM_PROMPT
          and "ONE COLOUR AT A TIME" in tools_sdk.SYSTEM_PROMPT)


def test_worker_resilience():
    print("\n13. a tool that stalls or crashes says so, and the next call still works")

    async def _slow_probe(_):
        time.sleep(4.0)
        return tools_sdk._ok("late")

    async def _crash_probe(_):
        raise ValueError("boom")

    tools_sdk._TOOL_BUDGETS["_slow_probe"] = 1.0
    slow = tools_sdk._offload(_slow_probe)
    crash = tools_sdk._offload(_crash_probe)

    t0 = time.time()
    text = asyncio.run(slow(None))["content"][0]["text"]
    took = time.time() - t0
    check("a stalled tool is abandoned at its budget, with the way out",
          "did not finish" in text and "escape" in text and took < 2.5,
          f"{took:.1f}s {text[:50]}")
    t0 = time.time()
    text2 = asyncio.run(tools_sdk.list_windows.handler({}))["content"][0]["text"]
    took2 = time.time() - t0
    check("the next tool runs at once on a fresh worker",
          "window" in text2.lower() and took2 < 2.0, f"{took2:.1f}s")
    text3 = asyncio.run(crash(None))["content"][0]["text"]
    check("a crashing tool reports the error as text, not silence",
          "failed" in text3 and "ValueError" in text3 and "boom" in text3, text3[:80])
    time.sleep(3.2)        # the abandoned thread finishes its sleep meanwhile
    text4 = asyncio.run(tools_sdk.list_windows.handler({}))["content"][0]["text"]
    check("and the abandoned worker finishing later changes nothing",
          "window" in text4.lower())
    del tools_sdk._TOOL_BUDGETS["_slow_probe"]


def test_stroke_sets():
    print("\n14. a picture becomes stroke sets the pointer replays — no coordinates by hand")
    from PIL import Image, ImageDraw
    layers = tools_sdk._runs_from_rows(["AAB", "ABB"], 3, 2)
    check("runs of one colour become one stroke each",
          sorted(layers) == ["A", "B"] and len(layers["A"]) == 2 and len(layers["B"]) == 2,
          str(layers))
    check("a run spans its first cell centre to its last",
          layers["A"][0] == [[0.5 / 3, 0.25], [1.5 / 3, 0.25]], str(layers["A"][0]))
    fitted = tools_sdk._fit_rect(0.5, (0, 0, 100, 100))
    check("a tall picture is fitted inside and centred",
          fitted == (25.0, 0.0, 75.0, 100.0), str(fitted))
    fitted = tools_sdk._fit_rect(2.0, (10, 10, 110, 60))
    check("a wide one fills a wide target", fitted == (10.0, 10.0, 110.0, 60.0),
          str(fitted))

    pic = Image.new("RGB", (120, 120), (255, 255, 255))
    ImageDraw.Draw(pic).rectangle((30, 30, 90, 90), outline=(0, 0, 0), width=3)
    strokes = tools_sdk._trace(pic, max_side=120)
    pts = [p for s in strokes for p in s]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    check("a square outline traces into lines",
          len(strokes) >= 1 and len(pts) >= 8, f"{len(strokes)} strokes, {len(pts)} points")
    check("that lie on the square, not elsewhere",
          bool(pts) and abs(min(xs) - 0.25) < 0.08 and abs(max(xs) - 0.75) < 0.08
          and abs(min(ys) - 0.25) < 0.08 and abs(max(ys) - 0.75) < 0.08,
          (f"x {min(xs):.2f}-{max(xs):.2f} y {min(ys):.2f}-{max(ys):.2f}" if pts
           else "no points"))
    blank = Image.new("RGB", (80, 80), (200, 200, 200))
    check("a flat picture has no lines", tools_sdk._trace(blank, max_side=80) == [])

    sid = tools_sdk._save_strokes({"kind": "trace", "aspect": 1.0,
                                   "layers": {"lines": strokes}})
    back = tools_sdk._load_strokes(sid)
    check("a stroke set round-trips through its file",
          back is not None and back["layers"]["lines"] == strokes and back["id"] == sid)
    check("an unknown set is refused by name", tools_sdk._load_strokes("s-nope") is None)
    check("and a path is not a name", tools_sdk._load_strokes("../s-nope") is None)

    def run(a):
        return asyncio.run(tools_sdk.mouse_path.handler(a))["content"][0]["text"]

    base = {"window": "", "points": "", "action": "move", "frame_w": 0,
            "frame_h": 0, "button": "left", "layer": "", "target": ""}
    text = run({**base, "strokes_from": "s-nope"})
    check("mouse_path names a missing stroke set", "No stroke set" in text, text[:60])
    text = run({**base, "strokes_from": sid, "layer": "colours"})
    check("and a missing layer, listing the real ones",
          "no layer" in text and "lines" in text, text[:80])
    text = run({**base, "strokes_from": sid, "target": "[50,50,10,10]"})
    check("and a bad target rectangle", "target:" in text and "x2 > x1" in text,
          text[:80])

    text = asyncio.run(tools_sdk.trace_edges.handler(
        {"window": "", "rect": "", "frame_w": 0, "frame_h": 0, "detail": 1}
    ))["content"][0]["text"]
    check("trace_edges answers with a stroke set for whatever is in front",
          "stroke set s-" in text or "No edges found" in text, text[:80])
    check("trace_edges is registered and offloaded",
          any(t.name == "trace_edges" for t in tools_sdk.ALL_TOOLS)
          and getattr(tools_sdk.trace_edges.handler, "_offloaded", False))
    check("the prompt says never from memory, and how",
          "strokes_from" in tools_sdk.SYSTEM_PROMPT
          and "never from memory" in tools_sdk.SYSTEM_PROMPT.lower())


# --------------------------------------------------- replay + screen record

def test_replay_and_screencast():
    print("\n15. recording the agent's own actions, and the screen")
    from core import journal

    check("replay is registered and offloaded",
          any(t.name == "replay" for t in tools_sdk.ALL_TOOLS)
          and getattr(tools_sdk.replay.handler, "_offloaded", False))
    check("acting tools are journaled, looks and waits are not",
          "click" in tools_sdk._JOURNALLED
          and "do_steps" in tools_sdk._JOURNALLED
          and "mouse_path" in tools_sdk._JOURNALLED
          and "find_controls" not in tools_sdk._JOURNALLED
          and "screenshot" not in tools_sdk._JOURNALLED)
    check("replay never journals itself", "replay" not in tools_sdk._JOURNALLED)
    check("the prompt says to record actions and redo them",
          "replay(action=start)" in tools_sdk.SYSTEM_PROMPT
          and "replay(action=run" in tools_sdk.SYSTEM_PROMPT)

    def run(a):
        base = {"action": "", "id": "", "name": "", "description": "",
                "screen": False, "fps": 15, "scale": 0.5, "actions": True}
        return asyncio.run(tools_sdk.replay.handler({**base, **a}))["content"][0]["text"]

    # An id names a file in ONE directory — never a path out of it.
    check("a journal id with a path is rejected on load",
          journal.load("../secrets") is None)
    check("a journal id with a path is rejected on delete",
          journal.delete("j-../x") is False)
    check("running a missing session says so",
          "No saved session" in run({"action": "run", "id": "j-deadbeef"}))

    # start arms it; the dispatch hook (exercised directly) appends acting
    # calls; stop saves a session that round-trips through disk.
    check("start arms the journal",
          "Recording" in run({"action": "start", "name": "t"}) and journal.armed())
    journal.record_call("click", {"name": "Save"})
    journal.record_call("do_steps", {"steps": '[{"do":"key","keys":"enter"}]'})
    n = len(journal.live.entries)
    stopped = run({"action": "stop", "name": "Round Trip", "description": "d"})
    check("two acting calls were recorded", n == 2, str(n))
    check("stop reports the saved session",
          "Round Trip" in stopped and "2 actions" in stopped, stopped[:60])
    import re
    m = re.search(r"\[(j-[0-9a-f]+)\]", stopped)
    jid = m.group(1) if m else ""
    data = journal.load(jid)
    check("the journal round-trips through disk",
          bool(data) and len(data.get("entries", [])) == 2
          and data["entries"][0]["tool"] == "click", jid)
    check("list shows the saved session", jid and jid in run({"action": "list"}))
    check("show lists its steps",
          "click 'Save'" in run({"action": "show", "id": jid}))
    check("delete removes it and its file",
          journal.delete(jid) and journal.load(jid) is None)

    # An action the arbiter refuses returns "STOP:" WITHOUT driving anything,
    # so it must not land in the journal as if it had happened. Stop is safe
    # to exercise here precisely because a refused click never touches the UI.
    run({"action": "start", "name": "blocked"})
    tools_sdk.set_cancelled(True)
    try:
        refused = asyncio.run(tools_sdk.click.handler(
            {"name": "Nowhere", "window": "", "control_type": "", "by_label": False}
        ))["content"][0]["text"]
        check("a click while stopped is refused, not performed",
              refused.startswith("STOP:"), refused[:40])
        check("and the refused action is NOT journaled",
              len(journal.live.entries) == 0)
    finally:
        tools_sdk.set_cancelled(False)
        run({"action": "discard"})

    # The run path calls the tools AGAIN on the same single worker thread.
    # A wrapped handler would submit to that busy worker and deadlock; the raw
    # body runs inline. Bogus window titles match nothing, so nothing is driven.
    rec = journal.Recorder(name="run path")
    rec.record("focus_window", {"title": "NoSuchWindow_ReplayTest_A"})
    rec.record("focus_window", {"title": "NoSuchWindow_ReplayTest_B"})
    rid = journal.save(rec, name="run path")["id"]
    t0 = time.time()
    ran = run({"action": "run", "id": rid})
    check("run replays every action inline (no worker deadlock)",
          "Replayed 2/2" in ran and time.time() - t0 < 20,
          ran.splitlines()[0] if ran else "")
    check("a replay does not itself get recorded",
          not journal.armed() and journal.replaying is False)
    journal.delete(rid)

    # A short, real screen capture. It only READS the screen, so it is safe
    # while the machine is in use; skipped only where there is no display.
    try:
        from core.display import assert_usable
        assert_usable()
        usable = True
    except Exception:
        usable = False
    if not usable:
        check("screen recording skipped — no usable display", True, "headless")
        return
    started = run({"action": "start", "actions": False, "screen": True,
                   "fps": 15, "scale": 0.35})
    ok_start = "Recording screen" in started
    check("a screen-only recording starts (no action journal)",
          ok_start and not journal.armed(), started[:60])
    time.sleep(1.0)
    done = run({"action": "stop"})
    check("stop produces a video file with frames",
          "Screen video:" in done and "frames at" in done and "MB" in done,
          done.replace(chr(10), " ")[:100])
    check("no journal was left armed by a screen-only recording",
          not journal.armed())


def main() -> int:
    print("=" * 72)
    print("DESKTOP LAYER — seeing and typing (read-only)")
    print("=" * 72)
    test_layout()
    test_combos()
    test_typing_shapes()
    test_names()
    test_walk()
    test_seeing_tools()
    test_maximize()
    test_wait_for_reply()
    test_prefix_and_dpi()
    test_do_steps()
    test_mouse_path_mapping()
    test_sample_colors()
    test_worker_resilience()
    test_stroke_sets()
    test_replay_and_screencast()

    passed = sum(1 for _, ok, _ in results if ok)
    print("\n" + "=" * 72)
    print(f"{passed}/{len(results)} passed")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name}  {detail}")
    print("=" * 72)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
