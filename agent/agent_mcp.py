"""MCP server — gives Claude the hands.

This is the connector that turns a tested desktop-control engine into "an app
that can do any computer task": Claude supplies the general intelligence, this
supplies reliable eyes and hands, and the arbiter makes sure the user can
always take the machine back.

Deliberately different from the original desktop-control MCP:

  * no coordinates ever cross this boundary. Callers name what they want
    ("click Save in Excel"); resolution and clicking happen in one call, so a
    coordinate can never go stale between the model reading it and using it.
  * clicking prefers UIA Invoke, which does not move the physical cursor and
    works on background windows. Measured: injection into a covered window
    clicks whatever is on top instead — i.e. the wrong application.
  * every mutating call passes the arbiter first, so physical input from the
    user pauses the agent and ESC+ESC aborts it.
  * screen reading is a fallback, not the default. UIA resolves in 0.5s at
    100% accuracy where the 84s vision pipeline managed 44%.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from mcp.server.fastmcp import FastMCP

from core import act, actuator, display, locator
from core.arbiter import Arbiter, Control
from core.machine_profile import load_profile
from core.recorder import Recorder
from core.replay import Replayer
from core.store import Store
from core.watchdog import watchdog

mcp = FastMCP("desktop-agent", instructions="""
Drive this Windows PC to complete real tasks.

HOW TO WORK
1. look_at_screen() or list_windows() to see what is there
2. find_controls(window=...) to learn what can be clicked, BY NAME
3. click(name=...) / type_text(...) / press(...) to act
4. look_at_screen() again to confirm the result

NEVER pass pixel coordinates. Name the control. Resolution and the click
happen inside one call, so nothing goes stale in between, and clicking a
named control cannot land on the wrong window.

CONTROL AND SAFETY
- take_control() once before acting; release_control() when finished.
- If a call returns "user took control", stop. The person is using their
  machine. Do not fight them for the mouse.
- The user can abort anything with a physical double-tap of ESC.

WHEN A CONTROL HAS NO NAME
Some apps (Electron, Tk, some games) publish geometry but no names.
read_labels(window=...) runs OCR over that window and returns the text drawn
inside each control, so you can then click by that text. It costs ~15s, so
use it only when find_controls() comes back with unnamed entries.

REMEMBERING WORK
When you finish a task worth repeating, call save_macro() with a short
description. It stores the steps as element identity, not coordinates, so it
replays after windows move, themes change, or on a different PC.
""".strip())

store = Store()
_arbiter: Arbiter | None = None
_recorder: Recorder | None = None
_journal: list[dict] = []


def _need_control() -> str | None:
    """Returns an error string if we must not act right now."""
    global _arbiter
    if _arbiter is None:
        return "No control. Call take_control() first."
    st = _arbiter.check()
    if st.control is Control.DRIVING:
        return None
    if st.control is Control.ABORTED:
        return "ABORTED: the user pressed ESC+ESC. Stop and report."
    if st.control is Control.YIELDED:
        return "The user took control of the machine. Stop acting."
    if st.control is Control.BLOCKED:
        return f"Another session holds control: {st.detail}"
    return f"Cannot act: {st.control.value} ({st.detail})"


# ---------------------------------------------------------------------------
# control
# ---------------------------------------------------------------------------

@mcp.tool()
def take_control() -> str:
    """Acquire exclusive control of the desktop. Call once before acting."""
    global _arbiter
    if _arbiter is None:
        _arbiter = Arbiter(owner="Claude")
    if not _arbiter.start():
        return f"DENIED — {_arbiter.state.detail}"
    watchdog.start()
    p = load_profile()
    return (f"Control acquired. Screen {p['screen']['w']}x{p['screen']['h']} "
            f"@{p['screen']['dpi_scale']}x. Apps available: "
            f"{', '.join(p['capabilities'])}. "
            f"The user can abort at any time with physical ESC+ESC.")


@mcp.tool()
def release_control() -> str:
    """Release control. Call when the task is finished."""
    global _arbiter
    if _arbiter is not None:
        _arbiter.stop()
        _arbiter = None
    return "Control released."


@mcp.tool()
def control_status() -> str:
    """Check whether it is still safe to act."""
    if _arbiter is None:
        return "No control held."
    return json.dumps(_arbiter.status(), indent=2, default=str)


# ---------------------------------------------------------------------------
# perceiving
# ---------------------------------------------------------------------------

@mcp.tool()
def list_windows() -> str:
    """List the windows actually on screen, with their sizes."""
    ws = locator.list_windows()
    if not ws:
        return "No visible windows."
    lines = [f"{len(ws)} windows:"]
    for w in ws:
        x1, y1, x2, y2 = w["rect"]
        lines.append(f"  {w['title'][:58]:<58} {x2-x1}x{y2-y1}")
    return "\n".join(lines)


@mcp.tool()
def find_controls(window: str = "", limit: int = 60,
                  only_named: bool = True) -> str:
    """List the controls in a window that can be clicked, by name.

    This is the cheap, accurate path — use it before any screenshot.

    Args:
        window: window title (substring). Empty = the focused window.
        limit: max controls to return.
        only_named: hide unnamed controls. Set False to see how many exist
            without names — if most are unnamed, use read_labels().
    """
    h = locator.find_window(title_contains=window) if window else locator.foreground_window()
    if not h:
        return f"No window matching {window!r}."
    locator.invalidate_cache(h)
    root = locator.uia_root(h)

    rows, unnamed = [], 0
    for c in locator.walk(root, max_depth=12, max_nodes=900, budget_s=4.0):
        r = locator._rect_of(c)
        if not r:
            continue
        try:
            name = (c.Name or "").strip()
            ctype = c.ControlTypeName or ""
            aid = (c.AutomationId or "").strip()
        except Exception:
            continue
        if not name:
            unnamed += 1
            if only_named:
                continue
        rows.append(f"  {ctype[:20]:<20} {name[:34]:<34} "
                    f"{'id=' + aid[:18] if aid else ''}")
        if len(rows) >= limit:
            break

    title = locator.window_title(h)
    out = [f"{title} — {len(rows)} controls" + (f", {unnamed} unnamed" if unnamed else "")]
    out += rows
    if unnamed and only_named:
        out.append(f"\n{unnamed} controls publish no name. If the one you need "
                   f"is missing, call read_labels(window={title[:30]!r}).")
    return "\n".join(out)


@mcp.tool()
def read_labels(window: str = "") -> str:
    """Read the text drawn inside unnamed controls (OCR). Slow — ~15s.

    Use only when find_controls() did not show what you need. Some toolkits
    publish exact geometry with no names; this fills in the names so you can
    click by text.
    """
    h = locator.find_window(title_contains=window) if window else locator.foreground_window()
    if not h:
        return f"No window matching {window!r}."
    try:
        labels = locator.labels_for(h)
    except display.WorkstationLocked:
        return "The workstation is locked — pixels show the lock screen. Ask the user to unlock."
    except display.DisplayAsleep:
        return "The display is asleep; nothing can be read from pixels."
    if not labels:
        return "No text found in unnamed controls."
    lines = [f"{len(labels)} labelled controls:"]
    for rect, lab in labels.items():
        lines.append(f"  {lab[:40]:<40} {rect[2]-rect[0]}x{rect[3]-rect[1]}")
    return "\n".join(lines)


@mcp.tool()
def look_at_screen(quality: int = 35) -> str:
    """Take a screenshot. Use to CONFIRM a result, not to find click targets.

    find_controls() is 160x faster and exact; a screenshot cannot tell you
    where to click reliably.
    """
    try:
        display.assert_usable()
    except display.WorkstationLocked:
        return "Workstation locked — ask the user to unlock."
    except display.DisplayAsleep:
        return "Display asleep and will not wake."
    import controller
    path, _ = controller.take_screenshot_to_file(quality=quality)
    fg = locator.foreground_window()
    return (f"Screenshot saved: {path}\nFocused window: {locator.window_title(fg)!r}")


# ---------------------------------------------------------------------------
# acting
# ---------------------------------------------------------------------------

@mcp.tool()
def click(name: str = "", window: str = "", control_type: str = "",
          automation_id: str = "", by_label: bool = False) -> str:
    """Click a control BY NAME. Never pass coordinates.

    Args:
        name: the control's name or, with by_label, its drawn text.
        window: window title substring to search in.
        control_type: e.g. ButtonControl, to disambiguate.
        automation_id: developer-assigned id, the most stable identifier.
        by_label: resolve via OCR text instead of the accessibility name.
    """
    err = _need_control()
    if err:
        return err

    if by_label:
        h = locator.find_window(title_contains=window) if window else locator.foreground_window()
        el = locator.find_labelled(h, name) if h else None
        if el is None:
            return f"No control labelled {name!r}."
        r = actuator.click(el, prefer="invoke")
        _journal.append({"action": "click", "name": name, "via": "label"})
        return f"Clicked {name!r} via {r.method} ({r.latency_ms:.0f}ms)."

    res = act.click_element(name=name or None, control=control_type or None,
                            automation_id=automation_id or None,
                            window=window or None)
    if not res.ok:
        return (f"Could not click {name or automation_id!r}: {res.error}. "
                f"Try find_controls(window={window!r}) to see the real names, "
                f"or read_labels() if the control has no name.")
    _journal.append({"action": "click", "name": name,
                     "locator": res.locator_type})
    _arbiter.note(f"click {name}")
    return f"Clicked {name or automation_id!r} via {res.locator_type} using {res.method}."


@mcp.tool()
def type_text(text: str) -> str:
    """Type literal text into whatever has focus. Layout-independent."""
    err = _need_control()
    if err:
        return err
    r = actuator.type_text(text)
    _journal.append({"action": "type", "text": text[:40]})
    _arbiter.note(f"type {text[:30]}")
    return f"Typed {len(text)} characters." if r.ok else "Typing failed."


@mcp.tool()
def press(keys: str, times: int = 1) -> str:
    """Press a key or shortcut. 'enter', 'tab', 'f5', 'ctrl+s', 'alt+tab'."""
    err = _need_control()
    if err:
        return err
    r = actuator.send_keys(keys) if "+" in keys else actuator.press_key(keys, times)
    _journal.append({"action": "key", "keys": keys})
    _arbiter.note(f"press {keys}")
    return f"Pressed {keys}." if r.ok else f"Failed to press {keys}: {r.detail}"


@mcp.tool()
def focus_window(title: str) -> str:
    """Bring a window to the front before interacting with it."""
    err = _need_control()
    if err:
        return err
    h = locator.find_window(title_contains=title)
    if not h:
        return f"No window matching {title!r}."
    ok = actuator.focus_window(h)
    return (f"Focused {locator.window_title(h)!r}." if ok
            else f"Could not bring {title!r} to the front.")


@mcp.tool()
def launch_app(name: str) -> str:
    """Start an application by short name, e.g. excel, chrome, notepad."""
    err = _need_control()
    if err:
        return err
    import subprocess
    prof = load_profile()
    target = prof["symbols"].get(f"APP:{name.lower()}")
    if not target:
        return (f"{name!r} not found on this machine. Available: "
                f"{', '.join(prof['capabilities'])}")
    if target.startswith("shell:"):
        subprocess.Popen(["explorer.exe", target], shell=False)
    else:
        subprocess.Popen([target], shell=False)
    return f"Launched {name}. Wait a moment, then list_windows() to confirm."


# ---------------------------------------------------------------------------
# remembering
# ---------------------------------------------------------------------------

@mcp.tool()
def list_macros() -> str:
    """List saved, replayable tasks."""
    ms = store.list()
    if not ms:
        return "No saved macros yet."
    lines = [f"{len(ms)} macros:"]
    for m in ms:
        lines.append(f"  [{m['trust']:<10}] {m['name'][:30]:<30} "
                     f"{m['steps']} steps, {m['runs']} runs, "
                     f"{m['success_rate']*100:.0f}% ok — {m['description'][:50]}")
    return "\n".join(lines)


@mcp.tool()
def run_macro(macro_id: str, params_json: str = "{}") -> str:
    """Replay a saved task."""
    err = _need_control()
    if err:
        return err
    try:
        params = json.loads(params_json or "{}")
    except json.JSONDecodeError:
        return "params_json is not valid JSON."
    rp = Replayer(store)
    res = rp.run(macro_id, params=params, arbiter=_arbiter)
    out = res.report()
    if res.needs_user:
        out += f"\n\nNEEDS YOU: {res.needs_user}"
    return out


@mcp.tool()
def start_recording() -> str:
    """Record what the USER does, to learn a task by demonstration."""
    global _recorder
    if _recorder is not None:
        return "Already recording."
    import time as _t
    d = HERE / "recordings" / _t.strftime("rec_%Y%m%d_%H%M%S")
    _recorder = Recorder(d)
    if not _recorder.start():
        _recorder = None
        return "Could not start the input hook."
    return (f"Recording to {d.name}. Perform the task normally. "
            f"Each click is resolved to an element as you make it. "
            f"Call stop_recording() when finished.")


@mcp.tool()
def stop_recording() -> str:
    """Stop recording and return what was captured, for you to interpret."""
    global _recorder
    if _recorder is None:
        return "Not recording."
    meta = _recorder.stop()
    summary = _recorder.summary()
    _recorder = None
    return (f"Captured {meta['events']} events over {meta['duration_s']}s "
            f"({meta['frames_kept']} frames kept).\n\n"
            f"{json.dumps(summary, indent=2)[:4000]}\n\n"
            f"Describe in one sentence what the user was doing, identify which "
            f"values would change between runs, then call save_macro().")


@mcp.tool()
def save_macro(name: str, description: str, steps_json: str) -> str:
    """Save a repeatable task.

    Args:
        name: short name.
        description: what it accomplishes, in one sentence. This is what
            retrieval matches on AND what recovery falls back to when a
            control moves, so write it as intent, not as mechanics.
        steps_json: JSON list of steps, each
            {"action": "click|type|key|launch|focus_window",
             "intent": "why this step exists",
             "locators": [{"type": "uia_name_type", "value": {...}}],
             "args": {...}}
    """
    from core.schema import LocalState, Macro, Trust
    from core.store import _step_from
    try:
        raw = json.loads(steps_json)
    except json.JSONDecodeError as e:
        return f"steps_json is not valid JSON: {e}"
    try:
        steps = [_step_from(s) for s in raw]
    except Exception as e:
        return f"Could not parse steps: {e}"

    m = Macro(name=name, description=description, steps=steps)
    local = LocalState(macro_id=m.id, trust=Trust.DRAFT)
    store.save(m, local)
    return (f"Saved {name!r} as {m.id} with {len(steps)} steps, trust=draft. "
            f"It will run supervised until two clean runs promote it.")


@mcp.tool()
def machine_info() -> str:
    """What this PC has: screen, installed apps, capabilities."""
    return json.dumps(load_profile(), indent=2)


if __name__ == "__main__":
    mcp.run(transport="stdio")
