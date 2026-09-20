"""In-process tools for the agent.

These run inside the app's own process — no MCP subprocess, no HTTP, no port.
That is what lets the whole thing ship as one standalone application.

The tool surface is deliberately task-shaped rather than API-shaped: the model
is given "click the control named X" rather than "enumerate, choose, send
input at (x,y)". Every call that touches the machine goes through the arbiter
first, so the user can always take the desktop back.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from core import act, actuator, display, locator, paths
from core import journal as jrnl        # module name would shadow journal() below
from core.arbiter import Arbiter, Control
from core.browser import browser
from core import noconsole
from core.machine_profile import load_profile

_arbiter: Arbiter | None = None
_log: list[str] = []


def set_arbiter(a: Arbiter | None) -> None:
    global _arbiter
    _arbiter = a


#: The task text of the run in progress, so save_lesson can record which task
#: taught the lesson without the model having to repeat it back.
_task: str = ""


def set_task(text: str) -> None:
    global _task
    _task = text or ""


def _current_task() -> str:
    return _task


# -- what the screenshots cost ------------------------------------------------
#
# The API charges an image by its pixels — ceil(w/28) * ceil(h/28) visual
# tokens, whatever the JPEG quality — so a screenshot's price is a size
# budget, and that is what the detail setting sets. The count and the tokens
# are kept per task so the window can show what the pictures cost, next to
# the words.
#
# Shrinking never moves a click. mouse_path scales coordinates by the
# "shown at" size the model passes back with them, so a picture shown at
# 1000 px maps onto the same screen pixels as one shown at 2500.

_shots = {"count": 0, "tokens": 0, "unchanged": 0}
#: region key -> (fingerprint, when, shown size) of the last picture sent, so
#: a re-look at an unchanged screen costs words instead of pixels.
_last_shot: dict[str, tuple[str, float, tuple[int, int]]] = {}
_shot_detail = "standard"
DETAIL_BUDGET = {"low": 1000, "standard": 1600, "high": 4784}
#: The long-edge cap of the high-resolution tier; anything past it is
#: downscaled by the API anyway.
MAX_SIDE = 2576


def reset_shots() -> None:
    _shots.update(count=0, tokens=0, unchanged=0)
    _last_shot.clear()


def shot_stats() -> dict:
    return dict(_shots)


def set_screenshot_detail(level: str) -> None:
    global _shot_detail
    _shot_detail = level if level in DETAIL_BUDGET else "standard"


def _visual_tokens(w: int, h: int) -> int:
    return -(-w // 28) * -(-h // 28)


def _fit_to_tokens(img, budget: int):
    """The image scaled so it costs at most `budget` visual tokens."""
    from PIL import Image
    w, h = img.size
    if _visual_tokens(w, h) <= budget and max(w, h) <= MAX_SIDE:
        return img
    s = min(1.0, (budget * 28 * 28 / (w * h)) ** 0.5, MAX_SIDE / max(w, h))
    nw, nh = max(28, int(w * s)), max(28, int(h * s))
    while _visual_tokens(nw, nh) > budget and nw > 56 and nh > 56:
        nw, nh = int(nw * 0.98), int(nh * 0.98)
    return img.resize((nw, nh), Image.LANCZOS)


def _fingerprint(img) -> str:
    """Coarse enough that a caret blink is the same picture and a menu
    opening is not: a 64x40 grey thumbnail at sixteen levels."""
    import hashlib
    small = img.convert("L").resize((64, 40))
    return hashlib.md5(bytes(v >> 4 for v in small.tobytes())).hexdigest()


def journal() -> list[str]:
    return list(_log)


def _note(msg: str) -> None:
    _log.append(msg)
    del _log[:-200]
    if _arbiter is not None:
        _arbiter.note(msg[:60])


#: Set while the user has the run paused. Held here rather than in the app so
#: the wait happens *before* an action, not between model turns — pausing a
#: task that is mid-way through filling a form should stop the next keystroke,
#: not wait for the model to come back around.
_paused = threading.Event()


#: Set when the user presses Stop. Long waits poll it, because they run on the
#: event loop the SDK reads messages on — a blocking wait there means Stop
#: cannot be noticed until the wait ends by itself.
_cancelled = threading.Event()


def set_cancelled(on: bool = True) -> None:
    _cancelled.set() if on else _cancelled.clear()


def should_stop() -> bool:
    """True if the user has asked to stop, by any route."""
    return _cancelled.is_set() or (_arbiter is not None and _arbiter.aborted)


def set_paused(on: bool) -> None:
    _paused.set() if on else _paused.clear()


def is_paused() -> bool:
    return _paused.is_set()


def _blocked() -> str | None:
    # Checked first, and unconditionally. The SDK hands a tool to the worker
    # at the same moment the app notices Stop, so without this the run could
    # "finish" and then a queued web_do or type_text would carry on driving
    # the machine with the arbiter already torn down.
    if should_stop():
        return ("STOP: the user asked to stop. Abandon the task and report "
                "what was done.")
    if _paused.is_set():
        # Block here rather than returning an error: an error would make the
        # model try something else, and "paused" must mean nothing happens.
        waited = 0.0
        while _paused.is_set() and waited < 3600:
            time.sleep(0.2)
            waited += 0.2
            if should_stop():
                return ("STOP: the user asked to stop. Abandon the task and "
                        "report what was done.")
        if waited >= 3600:
            return ("STOP: paused for over an hour with no resume. "
                    "Report what is done and what is left.")
    if _arbiter is None:
        return None
    st = _arbiter.check()
    if st.control is Control.DRIVING:
        return None
    if st.control is Control.ABORTED:
        return "STOP: the user pressed ESC+ESC. Abandon the task and report."
    if st.control is Control.YIELDED:
        return "STOP: the user is using their machine. Do not continue."
    return f"Cannot act right now: {st.control.value} — {st.detail}"


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


# ---------------------------------------------------------------------------
# desktop — seeing
# ---------------------------------------------------------------------------

@tool("list_windows", "List the windows currently on screen.", {})
async def list_windows(args: dict[str, Any]) -> dict:
    ws = locator.list_windows()
    if not ws:
        return _ok("No visible windows.")
    lines = [f"{len(ws)} windows open:"]
    for w in ws:
        x1, y1, x2, y2 = w["rect"]
        lines.append(f"  {w['title'][:60]:<60} {x2-x1}x{y2-y1}")
    return _ok("\n".join(lines))


@tool("find_controls",
      "List clickable controls in a window BY NAME, including web page content in "
      "Chrome/Edge. Use before clicking. Fast (~0.1-0.5s). filter= keeps only names "
      "containing that text — use it on busy pages.",
      {"window": str, "only_named": bool, "filter": str})
async def find_controls(args: dict[str, Any]) -> dict:
    window = args.get("window") or ""
    only_named = args.get("only_named", True)
    needle = (args.get("filter") or "").strip().lower()
    h = locator.find_window(title_contains=window) if window else locator.foreground_window()
    if not h:
        return _ok(f"No window matching {window!r}. Call list_windows() first.")
    locator.invalidate_cache(h)
    rows, unnamed = [], 0
    try:
        root = locator.uia_root(h)
    except locator.WindowGone as e:
        return _ok(f"{e}. Call list_windows to see what is open now.")
    # Depth 40, not 12: a web page's controls sit 20-30 levels below the
    # browser window, and the old cap listed the toolbar and called the page
    # empty. The node and time budgets still bound the cost.
    CAP = 100
    truncated = False
    scan = {"nodes": 0, "truncated": False, "reason": ""}
    hits: list[tuple[str, str, str]] = []
    for c in locator.walk(root, max_depth=40, max_nodes=2500,
                          budget_s=4.0, stats=scan):
        if locator._rect_of(c) is None:
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
        if needle and needle not in name.lower():
            continue
        if len(hits) >= CAP:
            truncated = True
            break
        hits.append((ctype, name, aid))
    # Same-named controls are numbered [k/N] in this order, so the model can
    # say "the 3rd" — or better, name a neighbour — instead of reaching for
    # a screenshot and pixel coordinates when a name alone is ambiguous.
    counts: dict[str, int] = {}
    for _, n, _ in hits:
        counts[n] = counts.get(n, 0) + 1
    seen: dict[str, int] = {}
    for ctype, name, aid in hits:
        tag = ""
        if name and counts[name] > 1:
            seen[name] = seen.get(name, 0) + 1
            tag = f" [{seen[name]}/{counts[name]}]"
        shown = name[:48 - len(tag)] + tag
        rows.append(f"  {ctype[:20]:<20} {shown:<48} {('id=' + aid[:18]) if aid else ''}")
    out = [f"{locator.window_title(h)} — {len(rows)} controls"
           + (f" matching {needle!r}" if needle else "")
           + (f" ({unnamed} unnamed)" if unnamed else "")]
    out += rows
    repeated = sum(1 for n, k in counts.items() if n and k > 1)
    if repeated:
        out.append(f"\n{repeated} name(s) repeat, marked [k/N]. Choose one with "
                   f"click(name=..., index=k) — k counts in this order — or "
                   f"near='<name of a control beside it>', which still works "
                   f"after the page changes. Never coordinates.")
    if truncated:
        out.append(f"\n(first {CAP} shown. Pass filter='word' to list only the "
                   f"controls whose name contains it.)")
    elif scan.get("truncated"):
        # Saying "not found" after running out of budget would be a lie the
        # model cannot see through, and it would go off inventing workarounds
        # for a control that is simply further down the tree.
        limit = "too many elements" if scan["reason"] == "nodes" else "too slowly"
        out.append(f"\n(this window has {limit} to scan fully — "
                   f"{scan['nodes']} elements were checked. What is listed is "
                   f"real, but the list is not complete: narrow it with "
                   f"filter='word'.)")
    if needle and not rows:
        out.append("\nNothing matched that filter."
                   + (" The scan was also cut short, so try again with a "
                      "different word." if scan.get("truncated")
                      else " Try a shorter word, or no filter."))
    # The OCR hint is for a desktop app that draws its own controls. Every web
    # page has hundreds of unnamed nodes, so on a browser this fired every
    # time and sent the model into a ~15s OCR pass that reads the browser
    # frame and nothing useful.
    browser_class = locator.window_class(h) in ("Chrome_WidgetWin_1",
                                                "MozillaWindowClass")
    if unnamed and only_named and not browser_class:
        out.append(f"\n{unnamed} controls have no name. If yours is missing, "
                   f"call read_labels.")

    # Include the window list. Every round trip costs a model call, and
    # "which window?" is the question that most often follows this one —
    # answering it here removes a whole turn from the common path.
    others = [w["title"] for w in locator.list_windows()
              if w["hwnd"] != h][:8]
    if others:
        out.append("\nOther windows open: " + " | ".join(t[:34] for t in others))
    return _ok("\n".join(out))


@tool("read_labels",
      "Read text drawn inside unnamed controls (OCR). Slow ~15s. Only if find_controls missed it.",
      {"window": str})
async def read_labels(args: dict[str, Any]) -> dict:
    window = args.get("window") or ""
    h = locator.find_window(title_contains=window) if window else locator.foreground_window()
    if not h:
        return _ok(f"No window matching {window!r}.")
    try:
        labels = locator.labels_for(h)
    except display.WorkstationLocked:
        return _ok("The workstation is locked. Ask the user to unlock it.")
    except display.DisplayAsleep:
        return _ok("The display is asleep and will not wake.")
    if not labels:
        return _ok("No readable text in unnamed controls.")
    return _ok("\n".join(f"  {lab}" for lab in labels.values()))


@tool("screenshot",
      "See the screen, or one window (window=...). Returns the picture itself, "
      "scaled to fit, and saves the full-size file. Not for finding targets: "
      "use find_controls.",
      {"window": str})
async def screenshot(args: dict[str, Any]) -> dict:
    """Return the image inline rather than a path.

    The path-then-Read version cost two turns per look, and the Read came
    back as a full-resolution image of up to 1.1 MB — over the SDK's
    per-message limit, which ended a task mid-run. The model then took to
    cropping screenshots with PIL in Bash and Reading those, which was worse.
    Here the picture arrives with the call, scaled to what the model would
    see anyway, and the full-size file is still saved for attaching/sending.
    """
    try:
        display.assert_usable()
    except Exception as e:
        return _ok(f"Cannot capture: {e}")

    import base64
    import io
    import time as _t
    import mss
    from PIL import Image

    window = (args.get("window") or "").strip()
    h = None
    region = None
    if window:
        h = locator.find_window(title_contains=window)
        if not h:
            return _ok(f"No window matching {window!r}. Call list_windows() first.")
    with mss.mss() as sct:
        # monitors[0] is the whole virtual desktop, monitors[1] the primary.
        # Clamping to the primary meant a window on a second screen produced
        # an empty region, silently fell back, and returned a picture of the
        # PRIMARY screen captioned as that window.
        desktop, primary = sct.monitors[0], sct.monitors[1]
        if h:
            l, t, r, b = locator.window_rect(h)
            L, T = max(l, desktop["left"]), max(t, desktop["top"])
            R = min(r, desktop["left"] + desktop["width"])
            B = min(b, desktop["top"] + desktop["height"])
            if R - L >= 8 and B - T >= 8:
                region = {"left": L, "top": T, "width": R - L, "height": B - T}
            else:
                return _ok(f"{locator.window_title(h)!r} is not visible on any "
                           f"screen — it is minimised or positioned off the "
                           f"desktop. Restore it first, or call screenshot() "
                           f"with no window for the whole screen.")
        shot = sct.grab(region or primary)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    # Under the data root, not next to the source: a one-file build unpacks
    # its code into a temp directory that is deleted on exit, so screenshots
    # written beside __file__ would vanish with it.
    out_dir = paths.data("workspace", "screenshots")
    out_dir.mkdir(parents=True, exist_ok=True)
    # Milliseconds in the name: a task can capture several times a second,
    # and second-resolution names silently overwrote each other — so the path
    # handed back to the model pointed at a LATER picture than the one it was
    # told about.
    path = out_dir / f"screen_{_t.strftime('%H%M%S')}_{int(_t.time() * 1000) % 1000:03d}.jpg"
    img.save(path, format="JPEG", quality=80)          # full size, for sending

    fg = locator.window_title(locator.foreground_window())
    what = f"window {locator.window_title(h)!r}" if h else "the primary screen"

    # A re-look at a screen that has not changed costs words, not pixels:
    # the model already holds that picture, and its frame size is repeated
    # so any coordinates it reads off it still map.
    key = f"win:{h}" if h else "screen"
    fp = _fingerprint(img)
    last = _last_shot.get(key)
    if last and last[0] == fp:
        _shots["unchanged"] += 1
        age = _t.time() - last[1]
        sw, sh = last[2]
        return _ok(f"Unchanged: {what} looks exactly as it did in your last "
                   f"screenshot of it ({age:.0f}s ago, shown at {sw}x{sh}). No "
                   f"new image sent — use that picture. Focused window: {fg!r}. "
                   f"If you expected a change, it has not happened yet.")

    # What the model sees, scaled to the visual-token budget of the detail
    # setting (see DETAIL_BUDGET), and compressed enough to stay far below
    # the per-message limit — typically 100-350 KB.
    preview = _fit_to_tokens(img, DETAIL_BUDGET.get(_shot_detail, 1600))
    buf = io.BytesIO()
    preview.save(buf, format="JPEG", quality=60, optimize=True)
    data = base64.b64encode(buf.getvalue()).decode("ascii")
    vt = _visual_tokens(preview.width, preview.height)
    _shots["count"] += 1
    _shots["tokens"] += vt
    _last_shot[key] = (fp, _t.time(), (preview.width, preview.height))

    note = (f"Screenshot of {what}, {img.width}x{img.height} px, "
            f"saved full-size to {path}. Shown at {preview.width}x{preview.height} "
            f"(~{vt} visual tokens; screenshot #{_shots['count']} this task). "
            f"Focused window: {fg!r}. To act on what you see, use find_controls "
            f"and click by name.")
    return {"content": [{"type": "image", "data": data, "mimeType": "image/jpeg"},
                        {"type": "text", "text": note}]}


# ---------------------------------------------------------------------------
# desktop — waiting for the screen to change
# ---------------------------------------------------------------------------

#: Cadence of wait_for_reply's UIA re-walks. The gaps are slept in 0.25s
#: slices with the stop flag checked in every slice, so Stop lands within
#: half a second even though the tree is only re-read every couple of seconds.
_WAIT_POLL_S = 2.0

#: Names kept per snapshot. The walk is breadth-first, so past this cap what
#: is dropped is the shallowest chrome — menus, toolbars — and what is kept
#: is the deep content, which is where a chat's messages live. The cap is
#: this high because the baseline must cover everything a later poll can
#: see: a name the baseline missed would be reported as "new" every time.
_WAIT_MAX_NAMES = 1500


def _reply_snapshot(h: int, needle: str) -> dict[str, str] | None:
    """Named controls in a window right now: norm_name -> name as rendered.

    Keyed on norm_name so whitespace and case churn between polls does not
    read as new content; the verbatim name is kept because it IS the payload
    — in WhatsApp Web each message is a control whose name is its text.
    None means the user stopped mid-walk. WindowGone propagates: only the
    caller knows whether the window can be re-found by title.
    """
    locator.invalidate_cache(h)
    root = locator.uia_root(h)
    out: dict[str, str] = {}
    seen = 0
    for c in locator.walk(root, max_depth=locator.WALK_DEPTH,
                          max_nodes=2500, budget_s=2.0):
        seen += 1
        if seen % 200 == 0 and should_stop():
            return None
        try:
            name = (c.Name or "").strip()
        except Exception:
            continue
        if not name:
            continue
        k = locator.norm_name(name)
        if needle and needle not in k:
            continue
        out.setdefault(k, name)
    if len(out) > _WAIT_MAX_NAMES:
        out = dict(list(out.items())[-_WAIT_MAX_NAMES:])
    return out


def _sift_new(current: dict[str, str], baseline: dict | set,
              pending: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """What is genuinely new: confirmed on a second sighting, or held pending.

    A name is reported only once it has been seen on two consecutive polls.
    Chat apps flash transient controls — a "typing…" indicator, an upload
    progress row — that appear and vanish within a poll; reported on first
    sight, one of those would come back as "the reply". Disappearances are
    ignored outright: a reply is new content, and old content scrolling out
    of the walk's budget proves nothing.
    """
    fresh = {k: v for k, v in current.items() if k not in baseline}
    confirmed = {k: v for k, v in fresh.items() if k in pending}
    return confirmed, {k: v for k, v in fresh.items() if k not in confirmed}


@tool("wait_for_reply",
      "Block until NEW content appears in a window, then return that new "
      "text. For a chat answer after you send someone a message (in WhatsApp "
      "Web each message is a control whose name is its text), or anything "
      "else that changes on screen — a build finishing, a page updating. "
      "window= title substring (empty = the window in front now); watch= "
      "keep only names containing this; timeout_s default 90, max 600. "
      "'No reply yet' is normal: call it again to keep waiting.",
      {"window": str, "watch": str, "timeout_s": int})
async def wait_for_reply(args: dict[str, Any]) -> dict:
    """A wait, not an act — so it deliberately does not run _blocked().

    _blocked() consults the arbiter, which treats recent keystrokes or a
    foreground change as the user taking the machine back. Right before a
    click; exactly wrong here: this tool sends no input, and the user working
    in another window WHILE the agent waits on a WhatsApp answer is the
    normal case, not a takeover. Stop and Pause are honoured directly
    instead, checked every quarter second — a wait the Stop button cannot
    reach reads as a hang, and this user has been through that once.
    """
    window = (args.get("window") or "").strip()
    needle = locator.norm_name(args.get("watch") or "")
    try:
        timeout_s = int(args.get("timeout_s") or 90)
    except (TypeError, ValueError):
        timeout_s = 90
    # 600 caps one CALL, not the wait — the model re-calls to keep waiting.
    # (The stack would allow far more: the bundled CLI's per-tool timeout
    # defaults to ~28 hours and its idle timeout exempts in-process servers.
    # A shorter call keeps the model in the loop and able to re-decide.)
    timeout_s = max(2, min(600, timeout_s))

    stop_msg = ("STOP: the user asked to stop. Abandon the task and report "
                "what was done.")
    if should_stop():
        return _ok(stop_msg)

    h = (locator.find_window(title_contains=window) if window
         else locator.foreground_window())
    if not h:
        return _ok(f"No window matching {window!r}. Call list_windows() first.")
    title = locator.window_title(h) or window or "the foreground window"
    gone_msg = (f"{title!r} closed while waiting — no reply was seen. "
                f"Call list_windows to see what is open now.")

    try:
        baseline = _reply_snapshot(h, needle)
    except locator.WindowGone as e:
        return _ok(f"{e}. Call list_windows to see what is open now.")
    if baseline is None:
        return _ok(stop_msg)

    _note(f"waiting for a reply in {title[:36]} (up to {timeout_s}s)")
    t0 = time.time()
    deadline = t0 + timeout_s
    noted = t0
    pending: dict[str, str] = {}

    def report(confirmed: dict[str, str]) -> dict:
        shown = list(confirmed.values())
        lines = [f"New in {title!r} after {int(time.time() - t0)}s:"]
        lines += [f"  {v[:400]}" for v in shown[:12]]
        if len(shown) > 12:
            lines.append(f"  … and {len(shown) - 12} more")
        lines.append("")
        lines.append("These control names appeared and stayed — in a chat, a "
                     "message's name is its text. If this is churn rather "
                     "than the reply (a status tick, a timestamp), call "
                     "wait_for_reply again; otherwise continue the task "
                     "with it.")
        _note(f"reply arrived: {shown[0][:38]}")
        return _ok(chr(10).join(lines))

    while True:
        wake = time.time() + _WAIT_POLL_S
        while time.time() < min(wake, deadline):
            if should_stop():
                return _ok(stop_msg)
            if _paused.is_set():
                # Pause freezes the countdown rather than eating it: a user
                # who pauses mid-wait and resumes ten minutes later should
                # not land on an instant "no reply" they never waited out.
                paused_at = time.time()
                while _paused.is_set():
                    if should_stop():
                        return _ok(stop_msg)
                    if time.time() - paused_at >= 3600:
                        return _ok("STOP: paused for over an hour with no "
                                   "resume. Report what is done and what "
                                   "is left.")
                    time.sleep(0.2)
                shift = time.time() - paused_at
                wake += shift
                deadline += shift
            time.sleep(0.25)

        try:
            current = _reply_snapshot(h, needle)
        except locator.WindowGone:
            h = locator.find_window(title_contains=window) if window else None
            if not h:
                return _ok(gone_msg)
            # Same title, fresh handle — Electron apps recreate their window.
            # Everything in it would count as new, so re-baseline and keep
            # waiting rather than reporting a whole window as the reply.
            title = locator.window_title(h) or title
            try:
                rebased = _reply_snapshot(h, needle)
            except locator.WindowGone:
                return _ok(gone_msg)
            if rebased is None:
                return _ok(stop_msg)
            baseline, pending = rebased, {}
            continue
        if current is None:
            return _ok(stop_msg)

        confirmed, pending = _sift_new(current, baseline, pending)
        if confirmed:
            return report(confirmed)

        if time.time() >= deadline:
            if pending:
                # First seen right on the deadline. Returning now would lose
                # it for good — the next call's baseline swallows whatever
                # is already on screen — so spend one short look confirming.
                for _ in range(5):
                    if should_stop():
                        return _ok(stop_msg)
                    time.sleep(0.25)
                try:
                    current = _reply_snapshot(h, needle)
                except locator.WindowGone:
                    return _ok(gone_msg)
                if current is None:
                    return _ok(stop_msg)
                confirmed, _ = _sift_new(current, baseline, pending)
                if confirmed:
                    return report(confirmed)
            break

        if time.time() - noted >= 30:
            noted = time.time()
            _note(f"still waiting in {title[:30]} — "
                  f"{int(time.time() - t0)}s, no reply yet")

    return _ok(f"No reply yet after {timeout_s}s watching {title!r}. Call "
               f"wait_for_reply again to keep waiting, or continue "
               f"differently.")



# ---------------------------------------------------------------------------
# desktop — acting
# ---------------------------------------------------------------------------

@tool("click",
      "Click a control by NAME (never coordinates). When several share the "
      "name (find_controls marks them [k/N]): index=k picks the k-th in that "
      "order; near='<name of a control beside it>' picks the one closest to "
      "that control — prefer near, it survives the page changing.",
      {"name": str, "window": str, "control_type": str, "by_label": bool,
       "index": int, "near": str})
async def click(args: dict[str, Any]) -> dict:
    b = _blocked()
    if b:
        return _ok(b)
    name = args.get("name", "")
    window = args.get("window") or None
    if args.get("by_label"):
        h = locator.find_window(title_contains=window) if window else locator.foreground_window()
        el = locator.find_labelled(h, name) if h else None
        if el is None:
            return _ok(f"No control labelled {name!r}.")
        r = actuator.click(el, prefer="invoke")
        _note(f"clicked {name}")
        return _ok(f"Clicked {name!r} ({r.method}).")
    try:
        index = int(args.get("index") or 0) or None
    except (TypeError, ValueError):
        index = None
    near = (args.get("near") or "").strip() or None
    res = act.click_element(name=name or None,
                            control=args.get("control_type") or None,
                            window=window, index=index, near=near)
    if not res.ok:
        return _ok(f"Could not click {name!r}: {res.error}. "
                   f"Call find_controls(window={window!r}) to see real names.")
    which = (f" near {near!r}" if near else f" #{index}" if index else "")
    _note(f"clicked {name}{which}")
    return _ok(f"Clicked {name!r}{which} via {res.locator_type} ({res.method}).")


@tool("type_text", "Type literal text into the focused field.", {"text": str})
async def type_text(args: dict[str, Any]) -> dict:
    b = _blocked()
    if b:
        return _ok(b)
    text = args.get("text", "")
    r = actuator.type_text(text)
    _note(f"typed {text[:40]}")
    return _ok(f"Typed {len(text)} characters." if r.ok else "Typing failed.")


@tool("press_key", "Press a key or shortcut: enter, tab, f5, ctrl+s, alt+tab.",
      {"keys": str, "times": int})
async def press_key(args: dict[str, Any]) -> dict:
    b = _blocked()
    if b:
        return _ok(b)
    keys = args.get("keys", "")
    try:
        times = max(1, int(args.get("times", 1) or 1))
    except (TypeError, ValueError):
        times = 1
    # "+" alone is the plus key, not a combo — only treat it as a combo when
    # there is something on both sides. `times` used to be silently dropped
    # for combos, so press_key("ctrl+z", times=5) undid exactly one thing.
    r = (actuator.send_keys(keys, times) if "+" in keys and len(keys.strip()) > 1
         else actuator.press_key(keys, times))
    _note(f"pressed {keys}")
    return _ok(f"Pressed {keys}." if r.ok else f"Failed: {r.detail}")


# ---------------------------------------------------------------------------
# desktop — several actions in one call
# ---------------------------------------------------------------------------

_STEP_KINDS = ("click", "double_click", "right_click", "scroll", "type", "key",
               "focus", "wait", "wait_for")
#: One call, not one task: a longer list is split by the model into more
#: calls, which also keeps each report readable.
_MAX_STEPS = 150
#: Pause between steps so the app can react to one before the next lands.
#: Replay uses 0.6; a model-composed batch usually targets an app it has
#: just looked at, so a little less is enough. Per-step settle= overrides.
_SETTLE_S = 0.35

_STOP_MSG = ("STOP: the user asked to stop. Abandon the task and report "
             "what was done.")


def _controls_snapshot(h: int, cap: int = 40) -> str:
    """The named controls in a window, briefly — for a failure report."""
    try:
        locator.invalidate_cache(h)
        root = locator.uia_root(h)
    except locator.WindowGone:
        return "  (the window has gone)"
    rows = []
    for c in locator.walk(root, max_depth=locator.WALK_DEPTH, max_nodes=1500,
                          budget_s=2.0):
        try:
            name = (c.Name or "").strip()
            ctype = c.ControlTypeName or ""
        except Exception:
            continue
        if not name or locator._rect_of(c) is None:
            continue
        rows.append(f"  {ctype[:18]:<18} {name[:50]}")
        if len(rows) >= cap:
            rows.append(f"  … first {cap} only; find_controls(filter=...) lists more")
            break
    return "\n".join(rows) if rows else "  (no named controls found)"


def _step_scope(s: dict, default_window: str) -> tuple[int | None, str | None]:
    """The window a step acts in: its own window=, else the batch's, else the front."""
    title = str(s.get("window") or default_window or "").strip()
    if title:
        h = locator.find_window(title_contains=title)
        if not h:
            return None, f"no open window matches {title!r}"
        return h, None
    return locator.foreground_window() or None, None


def _verify_for(s: dict):
    """A Verify from a step's expect= / gone= — None when it declares neither."""
    from core.schema import Locator, LocatorType, Verify
    expect = str(s.get("expect") or "").strip()
    gone = str(s.get("gone") or "").strip()
    if not expect and not gone:
        return None
    try:
        timeout = float(s.get("timeout") or 8)
    except (TypeError, ValueError):
        timeout = 8.0
    return Verify(
        appears=[Locator(LocatorType.UIA_NAME_TYPE, {"name": expect})] if expect else [],
        disappears=[Locator(LocatorType.UIA_NAME_TYPE, {"name": gone})] if gone else [],
        timeout_s=max(0.5, min(60.0, timeout)))


def _describe_step(s: dict) -> str:
    k = s.get("do")
    if k in ("click", "double_click", "right_click", "scroll"):
        return f"{k} {str(s.get('name') or '')[:32]!r}"
    if k == "type":
        return f"type {str(s.get('text') or '')[:24]!r}"
    if k == "key":
        return f"key {s.get('keys')}"
    if k == "focus":
        return f"focus {str(s.get('title') or s.get('window') or '')[:32]!r}"
    if k == "wait_for":
        return f"wait for {str(s.get('expect') or s.get('gone') or '')[:32]!r}"
    return str(k)


def _run_one_step(s: dict, default_window: str) -> tuple[bool, str]:
    """Execute one step. Returns (ok, what happened) — never raises.

    key, type, focus and wait mirror the single-action tools exactly, so a
    batch and a sequence of single calls do the same thing; the element
    actions go through act.do_step, the same resolve-act-verify path replay
    uses, so expect= gets its post-condition check and the invoke-to-input
    fallback for free.
    """
    from core.schema import Action, Locator, LocatorType, Step
    kind = s.get("do")
    if kind == "wait":
        try:
            secs = max(0.0, min(60.0, float(s.get("seconds") or 1)))
        except (TypeError, ValueError):
            secs = 1.0
        end = time.time() + secs
        while time.time() < end:
            if should_stop():
                return False, "stopped"
            time.sleep(max(0.0, min(0.25, end - time.time())))
        return True, f"waited {secs:g}s"
    if kind == "key":
        keys = str(s.get("keys") or "").strip()
        if not keys:
            return False, "key step needs keys="
        try:
            times = max(1, int(s.get("times") or 1))
        except (TypeError, ValueError):
            times = 1
        r = (actuator.send_keys(keys, times) if "+" in keys and len(keys) > 1
             else actuator.press_key(keys, times))
        if not r.ok:
            return False, f"key {keys}: {r.detail}"
        return True, f"pressed {keys}" + (f" x{times}" if times > 1 else "")
    if kind == "type":
        text = s.get("text")
        if not isinstance(text, str) or not text:
            return False, "type step needs text="
        r = actuator.type_text(text)
        return r.ok, (f"typed {len(text)} chars" if r.ok else "typing failed")
    if kind == "focus":
        title = str(s.get("title") or s.get("window") or default_window or "").strip()
        if not title:
            return False, "focus step needs title="
        h = locator.find_window(title_contains=title)
        if not h:
            return False, f"no open window matches {title!r}"
        ok = actuator.focus_window(h)
        if ok and s.get("maximize"):
            actuator.maximize_window(h)
        return ok, (f"focused {locator.window_title(h)!r}" if ok
                    else f"could not bring {title!r} to the front")

    # -- everything below acts on a named element in a window ---------------
    scope, err = _step_scope(s, default_window)
    if err:
        return False, err
    verify = _verify_for(s)
    if kind == "wait_for":
        if verify is None:
            return False, "wait_for needs expect= or gone="
        ok = act.check(verify, scope)
        return bool(ok), ("it is there" if ok else "it did not appear in time")
    name = str(s.get("name") or "").strip()
    if not name:
        return False, f"{kind} step needs name="
    control = s.get("control") or s.get("control_type") or None
    value: dict[str, Any] = {"name": name, "control": control}
    if s.get("index"):
        value["index"] = s.get("index")
    if s.get("near"):
        value["near"] = s.get("near")
    locs = [Locator(LocatorType.UIA_NAME_TYPE, value)]
    action = {"click": Action.CLICK, "double_click": Action.DOUBLE_CLICK,
              "right_click": Action.RIGHT_CLICK, "scroll": Action.SCROLL}[kind]
    args: dict[str, Any] = {}
    if kind == "scroll":
        try:
            amount = int(s.get("amount") or 3)
        except (TypeError, ValueError):
            amount = 3
        args = {"direction": s.get("direction") or "down", "amount": amount}
    step = Step(action=action, intent=f"{kind} {name!r}", locators=locs,
                args=args, verify=verify, optional=bool(s.get("optional")))
    r = act.do_step(step, scope, prefer="invoke")
    if not r.ok:
        return False, f"{kind} {name!r}: {r.error}"
    tail = f" via {r.locator_type} ({r.method})"
    if r.verified is True:
        tail += ", verified"
    return True, f"{kind} {name!r}{tail}"


@tool("do_steps",
      'Do SEVERAL desktop actions in one call — clicks by name, typing, keys, '
      'waits — far faster than one call each. steps is JSON, in order: '
      '[{"do":"focus","title":"WhatsApp"},{"do":"click","name":"Yeni sohbet"},'
      '{"do":"type","text":"Furkan"},{"do":"key","keys":"enter"}]. Kinds: click, '
      'double_click, right_click, scroll, type, key, focus, wait (seconds=), '
      'wait_for (expect= or gone= a control name). Per step: window=, control=, '
      'index=k or near=<neighbour name> when names repeat (as in click), '
      'expect= (a name that must appear afterwards), gone=, optional=true, '
      'settle=seconds. window= on the call is the default window for every step.',
      {"steps": str, "window": str, "stop_on_error": bool})
async def do_steps(args: dict[str, Any]) -> dict:
    """Batch desktop work. Same idea as web_do, same shape of report.

    Every single-action tool costs a model round trip, and a real run of 79
    clicks, 56 key presses and 41 typings took 31 minutes largely for that
    reason. Here the steps run back to back on the worker, with the arbiter
    and Stop consulted BETWEEN every step — a batch must never become a way
    to keep driving after the user has taken the mouse or pressed Stop. It
    stops at the first failure (unless that step is optional) and returns
    what is on screen, so recovery is one turn rather than a restart.
    """
    b = _blocked()
    if b:
        return _ok(b)
    raw = args.get("steps") or ""
    if isinstance(raw, str):
        try:
            steps = json.loads(raw)
        except Exception as e:
            return _ok(f"steps must be a JSON list: {e}")
    else:
        steps = raw
    if not isinstance(steps, list) or not steps:
        return _ok("Give a non-empty JSON list of steps.")
    bad = [i for i, s in enumerate(steps, 1) if not isinstance(s, dict)]
    if bad:
        return _ok(f"Step(s) {', '.join(map(str, bad))} are not objects. Each "
                   f'step looks like {{"do":"click","name":"Kaydet"}}.')
    unknown = [f"{i} ({s.get('do')!r})" for i, s in enumerate(steps, 1)
               if s.get("do") not in _STEP_KINDS]
    if unknown:
        return _ok(f"Unknown step kind at {', '.join(unknown)}. Kinds: "
                   + ", ".join(_STEP_KINDS) + ".")
    if len(steps) > _MAX_STEPS:
        return _ok(f"{len(steps)} steps is too many for one call; send at most "
                   f"{_MAX_STEPS}, then continue with another call.")
    default_window = str(args.get("window") or "").strip()
    stop_on_error = args.get("stop_on_error")
    stop_on_error = True if stop_on_error is None else bool(stop_on_error)

    t0 = time.time()
    lines: list[str] = []
    done, failed_at, halted = 0, 0, ""
    for i, s in enumerate(steps, 1):
        if i > 1:
            b = _blocked()
            if b:
                halted = b
                break
        _note(f"step {i}/{len(steps)}: {_describe_step(s)}"[:80])
        try:
            ok, what = _run_one_step(s, default_window)
        except Exception as e:
            ok, what = False, f"{type(e).__name__}: {e}"
        if ok:
            done += 1
        lines.append(f"  {'ok  ' if ok else 'FAIL'} {i}. {what}")
        if not ok:
            if what == "stopped":
                halted = _STOP_MSG
                break
            if not s.get("optional") and stop_on_error:
                failed_at = i
                break
        try:
            settle = (float(s.get("settle")) if s.get("settle") is not None
                      else _SETTLE_S)
        except (TypeError, ValueError):
            settle = _SETTLE_S
        if settle > 0 and i < len(steps):
            time.sleep(max(0.0, min(10.0, settle)))

    out = [f"{done}/{len(steps)} steps in {time.time() - t0:.1f}s"] + lines
    if halted:
        out.append("")
        out.append(f"{halted} ({done} of {len(steps)} steps were done.)")
        return _ok("\n".join(out))
    if failed_at:
        left = len(steps) - failed_at
        out.append("")
        out.append(f"Stopped at step {failed_at}."
                   + (f" Step {len(steps)} was NOT done." if left == 1 else
                      f" Steps {failed_at + 1}–{len(steps)} were NOT done." if left
                      else ""))
        scope, _ = _step_scope(steps[failed_at - 1], default_window)
        if scope:
            out.append(f"Named controls in {locator.window_title(scope)!r} now:")
            out.append(_controls_snapshot(scope))
    else:
        fg = locator.foreground_window()
        if fg:
            out.append(f"Window in front now: {locator.window_title(fg)!r}.")
    return _ok("\n".join(out))


# ---------------------------------------------------------------------------
# desktop — precise pointer work
# ---------------------------------------------------------------------------

_PATH_MAX_POINTS = 5000


def _parse_strokes(raw) -> tuple[list[list[tuple[float, float]]], str | None]:
    """points= as one stroke [[x,y],...] or several [[[x,y],...],...]."""
    if isinstance(raw, str):
        try:
            data = json.loads(raw or "")
        except Exception as e:
            return [], f"points must be JSON: {e}"
    else:
        data = raw
    if not isinstance(data, list) or not data:
        return [], "points must be a non-empty JSON list of [x, y] pairs."

    def pair(p) -> bool:
        return (isinstance(p, (list, tuple)) and len(p) == 2
                and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                        for v in p))

    if all(pair(p) for p in data):
        strokes = [data]
    elif all(isinstance(s, list) and s and all(pair(p) for p in s) for s in data):
        strokes = data
    else:
        return [], ("points must be [[x,y],[x,y],...] for one stroke, or "
                    "[[[x,y],...],[[x,y],...]] for several.")
    n = sum(len(s) for s in strokes)
    if n > _PATH_MAX_POINTS:
        return [], f"{n} points is too many for one call (max {_PATH_MAX_POINTS})."
    return [[(float(x), float(y)) for x, y in s] for s in strokes], None


def _window_region(h: int) -> tuple[int, int, int, int] | None:
    """The window's on-screen rectangle (left, top, width, height) — the same
    clamp screenshot(window=...) applies, so a coordinate read off that
    picture maps back onto the same pixels."""
    l, t, r, b = locator.window_rect(h)
    vx, vy, vw, vh = actuator.virtual_desktop()
    L, T = max(l, vx), max(t, vy)
    R, B = min(r, vx + vw), min(b, vy + vh)
    if R - L < 8 or B - T < 8:
        return None
    return (L, T, R - L, B - T)


def _map_strokes(strokes, frame: tuple[int, int],
                 region: tuple[int, int, int, int]):
    """Picture coordinates -> physical screen pixels. A frame of (0, 0) means
    the points are already in the window's own pixels."""
    L, T, W, H = region
    fw, fh = frame
    fw, fh = (fw or W), (fh or H)
    sx, sy = W / fw, H / fh
    out = []
    for i, s in enumerate(strokes, 1):
        m = []
        for x, y in s:
            if not (-1 <= x <= fw + 1 and -1 <= y <= fh + 1):
                return [], (f"point ({x:g}, {y:g}) in stroke {i} is outside the "
                            f"{fw}x{fh} frame. Read coordinates off "
                            f"screenshot(window=...) and pass frame_w/frame_h = "
                            f"that picture's shown size.")
            m.append((int(round(L + min(max(x, 0), fw) * sx)),
                      int(round(T + min(max(y, 0), fh) * sy))))
        out.append(m)
    return out, None


# ---------------------------------------------------------------------------
# stroke sets — a picture turned into something the pointer can replay
# ---------------------------------------------------------------------------
# The model must never carry coordinates by hand: a 2000-cell mosaic pasted
# back into a call is slow, expensive and gets mangled. So what the screen
# yields is saved as a set of normalised strokes, and mouse_path replays a
# set by id, fitted into whatever rectangle the model points at.

_STROKES_MAX_STROKES = 800
_STROKES_MAX_POINTS = 12000


def _strokes_dir():
    d = paths.data("workspace", "strokes")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_strokes(data: dict) -> str:
    import uuid
    sid = "s-" + uuid.uuid4().hex[:6]
    body = {"id": sid, "created": time.time(), **data}
    (_strokes_dir() / f"{sid}.json").write_text(json.dumps(body), encoding="utf-8")
    return sid


def _load_strokes(sid: str) -> dict | None:
    if not sid or "/" in sid or "\\" in sid:
        return None
    p = _strokes_dir() / f"{sid}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _rect_in_frame(raw, fw, fh):
    """A [x1,y1,x2,y2] rectangle in the picture's frame; empty means all of it."""
    try:
        rect = json.loads(raw) if isinstance(raw, str) and raw.strip() else raw
    except Exception as e:
        return None, f"rect must be JSON [x1,y1,x2,y2]: {e}"
    if not rect:
        rect = [0, 0, fw, fh]
    if (not isinstance(rect, list) or len(rect) != 4
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                       for v in rect)):
        return None, "rect must be [x1,y1,x2,y2] in the same frame as your screenshot."
    x1, y1, x2, y2 = [float(v) for v in rect]
    if not (0 <= x1 < x2 <= fw + 1 and 0 <= y1 < y2 <= fh + 1):
        return None, (f"rect ({x1:g},{y1:g})-({x2:g},{y2:g}) must lie inside the "
                      f"{fw}x{fh} frame with x2 > x1 and y2 > y1.")
    return (x1, y1, x2, y2), None


def _fit_rect(aspect: float, target):
    """The largest rectangle of the given width/height ratio inside target,
    centred — so a copied picture keeps its shape wherever it is placed."""
    x1, y1, x2, y2 = target
    tw, th = x2 - x1, y2 - y1
    if tw / th > aspect:
        w, h = th * aspect, th
    else:
        w, h = tw, tw / aspect
    ox, oy = x1 + (tw - w) / 2, y1 + (th - h) / 2
    return (ox, oy, ox + w, oy + h)


def _runs_from_rows(rows: list[str], gw: int, gh: int) -> dict[str, list]:
    """Horizontal runs of one letter -> one stroke each, normalised 0..1.

    A run is drawn as a single drag from its first cell centre to its last,
    so a flat area costs one stroke instead of one dab per cell."""
    layers: dict[str, list] = {}
    for r, row in enumerate(rows):
        y = (r + 0.5) / gh
        c = 0
        while c < gw:
            letter, c2 = row[c], c
            while c2 + 1 < gw and row[c2 + 1] == letter:
                c2 += 1
            layers.setdefault(letter, []).append(
                [[(c + 0.5) / gw, y], [(c2 + 0.5) / gw, y]])
            c = c2 + 1
    return layers


def _rdp(points: list, eps: float) -> list:
    """Ramer-Douglas-Peucker: the same line with far fewer points."""
    if len(points) < 3:
        return points
    (x1, y1), (x2, y2) = points[0], points[-1]
    dx, dy = x2 - x1, y2 - y1
    norm = (dx * dx + dy * dy) ** 0.5 or 1e-9
    best, idx = 0.0, 0
    for i in range(1, len(points) - 1):
        px, py = points[i]
        d = abs(dy * px - dx * py + x2 * y1 - y2 * x1) / norm
        if d > best:
            best, idx = d, i
    if best > eps:
        return _rdp(points[:idx + 1], eps)[:-1] + _rdp(points[idx:], eps)
    return [points[0], points[-1]]


def _trace(img, max_side: int = 160, keep_frac: float = 0.06) -> list:
    """The picture's edges, chained into polylines normalised 0..1.

    Pure PIL: blur a little, FIND_EDGES, keep the strongest few percent of
    pixels, then follow neighbours into chains (endpoints first, preferring
    the current direction) and simplify each chain. Enough for line art of
    a face or an object; not a contour engine, and it does not need to be.
    """
    from PIL import Image, ImageFilter
    g = img.convert("L")
    scale = min(1.0, max_side / max(g.size))
    if scale < 1.0:
        g = g.resize((max(2, round(g.width * scale)), max(2, round(g.height * scale))),
                     Image.LANCZOS)
    g = g.filter(ImageFilter.GaussianBlur(0.8)).filter(ImageFilter.FIND_EDGES)
    w, h = g.size
    px = list(g.getdata())
    # FIND_EDGES lights up the whole 1px image frame — the convolution has no
    # neighbours at the boundary. Left in, every trace is wrapped in a box, a
    # flat picture becomes a rectangle, and the bright frame steals the
    # threshold from real interior edges. Zero the ring before anything reads it.
    for x in range(w):
        px[x] = 0
        px[(h - 1) * w + x] = 0
    for y in range(h):
        px[y * w] = 0
        px[y * w + w - 1] = 0
    n_keep = max(1, int(len(px) * keep_frac))
    thr = max(sorted(px, reverse=True)[n_keep - 1], 24)
    edge = {(i % w, i // w) for i, v in enumerate(px) if v >= thr}
    nbrs = ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))

    def around(p, pool):
        return [(p[0] + dx, p[1] + dy) for dx, dy in nbrs if (p[0] + dx, p[1] + dy) in pool]

    remaining = set(edge)
    chains = []
    for s in sorted(edge, key=lambda p: len(around(p, edge))):
        if s not in remaining:
            continue
        chain, cur = [s], s
        remaining.discard(s)
        while True:
            nxt = around(cur, remaining)
            if not nxt:
                break
            if len(chain) >= 2:
                ddx, ddy = cur[0] - chain[-2][0], cur[1] - chain[-2][1]
                nxt.sort(key=lambda q: -((q[0] - cur[0]) * ddx + (q[1] - cur[1]) * ddy))
            cur = nxt[0]
            chain.append(cur)
            remaining.discard(cur)
        if len(chain) >= 6:
            chains.append(chain)
    chains.sort(key=len, reverse=True)
    out, total = [], 0
    for chain in chains[:_STROKES_MAX_STROKES]:
        simple = _rdp(chain, 1.0)
        if total + len(simple) > _STROKES_MAX_POINTS:
            break
        total += len(simple)
        out.append([[(x + 0.5) / w, (y + 0.5) / h] for x, y in simple])
    return out


@tool("trace_edges",
      'Turn a picture on screen into LINES the pointer can draw. rect= '
      '[x1,y1,x2,y2] around the picture in the frame of screenshot(window=...) '
      '(frame_w/frame_h = its shown size, 0 = window pixels; empty rect = the '
      'whole window). detail 1-3. Saves a stroke set and returns its id; draw it '
      "with mouse_path(strokes_from=id, layer='lines', target=[...]) and a thin "
      'brush. For copying an image as line art, never from memory.',
      {"window": str, "rect": str, "frame_w": int, "frame_h": int, "detail": int})
async def trace_edges(args: dict[str, Any]) -> dict:
    try:
        display.assert_usable()
    except Exception as e:
        return _ok(f"Cannot read the screen: {e}")
    window = str(args.get("window") or "").strip()
    h = (locator.find_window(title_contains=window) if window
         else locator.foreground_window())
    if not h:
        return _ok(f"No window matching {window!r}. Call list_windows() first."
                   if window else "No window is in front. Name one with window=.")
    title = locator.window_title(h)
    region = _window_region(h)
    if region is None:
        return _ok(f"{title!r} is not visible on any screen. Restore it first.")
    L, T, W, H = region
    try:
        frame = (int(args.get("frame_w") or 0), int(args.get("frame_h") or 0))
    except (TypeError, ValueError):
        frame = (0, 0)
    fw, fh = (frame[0] or W), (frame[1] or H)
    rect, err = _rect_in_frame(args.get("rect"), fw, fh)
    if err:
        return _ok(err)
    rx1, ry1, rx2, ry2 = rect
    try:
        detail = max(1, min(3, int(args.get("detail") or 2)))
    except (TypeError, ValueError):
        detail = 2
    import mss
    from PIL import Image
    with mss.mss() as sct:
        shot = sct.grab({"left": L, "top": T, "width": W, "height": H})
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    sx, sy = W / fw, H / fh
    box = (min(int(rx1 * sx), W - 1), min(int(ry1 * sy), H - 1),
           min(max(int(rx2 * sx), int(rx1 * sx) + 1), W),
           min(max(int(ry2 * sy), int(ry1 * sy) + 1), H))
    strokes = _trace(img.crop(box), max_side={1: 120, 2: 180, 3: 240}[detail],
                     keep_frac={1: 0.05, 2: 0.06, 3: 0.08}[detail])
    if not strokes:
        return _ok(f"No edges found in that part of {title!r} — it may be flat, or "
                   f"the rect is off the picture. Check with screenshot(window=...).")
    n = sum(len(s) for s in strokes)
    sid = _save_strokes({"kind": "trace", "source": title,
                         "rect": [rx1, ry1, rx2, ry2],
                         "aspect": (rx2 - rx1) / (ry2 - ry1),
                         "layers": {"lines": strokes}})
    _note(f"traced {len(strokes)} lines from {title[:30]}")
    return _ok(f"Traced {title!r} rect ({rx1:g},{ry1:g})-({rx2:g},{ry2:g}) into "
               f"{len(strokes)} line strokes ({n} points, longest first, detail "
               f"{detail}) — stroke set {sid}. Draw it: mouse_path(window=<target>, "
               f"strokes_from='{sid}', layer='lines', target='[x1,y1,x2,y2]' in the "
               f"target's frame, action='drag') with a thin brush. The picture is "
               f"fitted into that rectangle keeping its shape.")


@tool("mouse_path",
      'Precise pointer work where nothing has a name: draw a stroke (drag), '
      'click a list of points, or move. Take screenshot(window=...) first and '
      'read coordinates off that picture; pass frame_w/frame_h = its "shown at" '
      'size so they map exactly. points is JSON: one stroke [[x,y],[x,y],...] '
      'or several [[[x,y],...],[[x,y],...]]. OR replay a saved stroke set: '
      'strokes_from=<id from sample_colors mosaic / trace_edges>, layer=<letter '
      "or 'lines'>, target=[x1,y1,x2,y2] in this window's frame (empty = whole "
      'frame; the picture is fitted in, keeping its shape). action: drag | '
      'click | move. button: left | right.',
      {"window": str, "points": str, "action": str, "frame_w": int,
       "frame_h": int, "button": str, "strokes_from": str, "layer": str,
       "target": str})
async def mouse_path(args: dict[str, Any]) -> dict:
    """The one place a coordinate crosses the tool boundary — and it is a
    coordinate in a picture the model was just shown, of one named window,
    never a bare screen position. A paint canvas, a game board, a chart:
    nothing there has a name to click, and this is how they are driven.
    """
    b = _blocked()
    if b:
        return _ok(b)
    window = str(args.get("window") or "").strip()
    h = (locator.find_window(title_contains=window) if window
         else locator.foreground_window())
    if not h:
        return _ok(f"No window matching {window!r}. Call list_windows() first."
                   if window else "No window is in front. Name one with window=.")
    action = str(args.get("action") or "drag").strip().lower()
    if action not in ("drag", "click", "move"):
        return _ok(f"action must be drag, click or move — not {action!r}.")
    button = str(args.get("button") or "left").strip().lower()
    if button not in ("left", "right", "middle"):
        button = "left"
    title = locator.window_title(h)
    region = _window_region(h)
    if region is None:
        return _ok(f"{title!r} is not visible on any screen. Restore it first.")
    try:
        frame = (int(args.get("frame_w") or 0), int(args.get("frame_h") or 0))
    except (TypeError, ValueError):
        frame = (0, 0)
    L, T, W, H = region
    fw, fh = (frame[0] or W), (frame[1] or H)
    set_note = ""
    sid = str(args.get("strokes_from") or "").strip()
    if sid:
        data = _load_strokes(sid)
        if data is None:
            return _ok(f"No stroke set {sid!r}. sample_colors (mosaic mode) and "
                       f"trace_edges create them.")
        layers = data.get("layers") or {}
        layer = (str(args.get("layer") or "").strip()
                 or ("lines" if "lines" in layers else next(iter(layers), "")))
        if layer not in layers:
            return _ok(f"Stroke set {sid} has no layer {layer!r}. Layers: "
                       f"{', '.join(layers) or 'none'}.")
        target, err = _rect_in_frame(args.get("target"), fw, fh)
        if err:
            return _ok("target: " + err)
        fx1, fy1, fx2, fy2 = _fit_rect(float(data.get("aspect") or 1.0), target)
        strokes = [[(fx1 + x * (fx2 - fx1), fy1 + y * (fy2 - fy1)) for x, y in s]
                   for s in layers[layer]]
        if not strokes:
            return _ok(f"Layer {layer!r} of {sid} is empty.")
        set_note = (f" from stroke set {sid} layer {layer!r}, fitted into "
                    f"({fx1:.0f},{fy1:.0f})-({fx2:.0f},{fy2:.0f})")
        grid = data.get("grid")
        if grid:
            cw, chh = (fx2 - fx1) / grid[0], (fy2 - fy1) / grid[1]
            set_note += (f"; each cell is {cw:.0f}x{chh:.0f} px in this frame — "
                         f"the brush must be at least that wide")
        colour = (data.get("palette") or {}).get(layer)
        if colour:
            set_note += f"; this layer's colour is {colour}"
    else:
        strokes, err = _parse_strokes(args.get("points"))
        if err:
            return _ok(err)
    mapped, err = _map_strokes(strokes, frame, region)
    if err:
        return _ok(err)
    # Coordinate input lands on whatever is on top, so the window must be in
    # front — the rule the name-based click applies before it injects.
    top = actuator._root_owner(h)
    if locator.foreground_window() != top:
        if not actuator.focus_window(top):
            return _ok(f"Could not bring {title!r} to the front; refused to "
                       f"inject — the pointer would land on the covering window.")
        time.sleep(0.25)
    r = actuator.pointer_path(mapped, action, button)
    n = sum(len(s) for s in strokes)
    verb = {"drag": "drew", "click": "clicked", "move": "moved through"}[action]
    _note(f"{verb} {n} point(s) in {title[:36]}")
    if not r.ok:
        return _ok(f"Failed: {r.detail}")
    return _ok(f"{verb.capitalize()} {len(strokes)} stroke(s), {n} point(s) in "
               f"{title!r}{set_note} ({r.detail}, {r.latency_ms:.0f}ms). Take "
               f"screenshot(window={title[:30]!r}) to check the result.")


_BASIC_COLORS = {
    "black": (0, 0, 0), "white": (255, 255, 255), "gray": (128, 128, 128),
    "red": (220, 30, 30), "orange": (255, 140, 0), "yellow": (250, 220, 40),
    "green": (40, 170, 60), "cyan": (40, 200, 220), "blue": (40, 80, 220),
    "purple": (130, 50, 180), "pink": (240, 120, 180), "brown": (130, 80, 40),
}


def _color_name(r: int, g: int, b: int) -> str:
    best, dist = "", None
    for name, (cr, cg, cb) in _BASIC_COLORS.items():
        d = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
        if dist is None or d < dist:
            best, dist = name, d
    return best


def _mosaic(img, rect, gw: int, gh: int, k: int):
    """Average a picture into gw x gh cells and reduce them to k colours.

    Returns (palette, rows): palette as [(letter, (r, g, b), share)] most
    common first, rows as strings of letters top to bottom. PIL does the
    real work — a BOX resize is an exact area average, and median-cut is
    what image editors mean by "reduce to N colours". The letters are what
    makes 2000 cells fit in one tool result the model can actually read.
    """
    from collections import Counter
    from PIL import Image
    x1, y1, x2, y2 = rect
    small = img.crop((x1, y1, x2, y2)).convert("RGB").resize((gw, gh), Image.BOX)
    method = getattr(getattr(Image, "Quantize", Image), "MEDIANCUT", 0)
    dither = getattr(getattr(Image, "Dither", Image), "NONE", 0)
    q = small.quantize(colors=k, method=method, dither=dither)
    pal = q.getpalette() or []
    idx = list(q.getdata())
    counts = Counter(idx)
    order = [i for i, _ in counts.most_common()]
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    letter_of = {i: letters[n] for n, i in enumerate(order)}
    palette = [(letters[n], tuple(pal[3 * i:3 * i + 3]), counts[i] / len(idx))
               for n, i in enumerate(order)]
    rows = ["".join(letter_of[idx[r * gw + c]] for c in range(gw)) for r in range(gh)]
    return palette, rows


@tool("sample_colors",
      'Exact colours read from the screen, two ways. POINTS: points= as in '
      'mouse_path -> "#rrggbb" per point. MOSAIC: rect=[x1,y1,x2,y2] around a '
      'picture plus grid_w/grid_h (e.g. 32x48) and palette= (2-24, default 8) '
      '-> the picture as rows of letters, one per cell, with the exact colour '
      'of each letter and where each cell sits — the way to COPY an image '
      'rather than draw it from memory. Coordinates are read off '
      'screenshot(window=...); frame_w/frame_h = its shown size (0 = window '
      'pixels).',
      {"window": str, "points": str, "frame_w": int, "frame_h": int,
       "rect": str, "grid_w": int, "grid_h": int, "palette": int})
async def sample_colors(args: dict[str, Any]) -> dict:
    """A screenshot is a compressed, downscaled picture; a swatch two shades
    apart from its neighbour cannot be told from it. The screen can. And a
    picture the model could only ever paint from memory becomes, as a
    letter mosaic, something it can reproduce cell by cell."""
    try:
        display.assert_usable()
    except Exception as e:
        return _ok(f"Cannot read the screen: {e}")
    window = str(args.get("window") or "").strip()
    h = (locator.find_window(title_contains=window) if window
         else locator.foreground_window())
    if not h:
        return _ok(f"No window matching {window!r}. Call list_windows() first."
                   if window else "No window is in front. Name one with window=.")
    title = locator.window_title(h)
    region = _window_region(h)
    if region is None:
        return _ok(f"{title!r} is not visible on any screen. Restore it first.")
    try:
        frame = (int(args.get("frame_w") or 0), int(args.get("frame_h") or 0))
    except (TypeError, ValueError):
        frame = (0, 0)
    try:
        gw, gh = int(args.get("grid_w") or 0), int(args.get("grid_h") or 0)
    except (TypeError, ValueError):
        gw, gh = 0, 0
    L, T, W, H = region
    fw, fh = (frame[0] or W), (frame[1] or H)

    if gw or gh:
        # -- mosaic mode -----------------------------------------------
        if not (2 <= gw <= 64 and 2 <= gh <= 64):
            return _ok("grid_w and grid_h must both be between 2 and 64.")
        rect, err = _rect_in_frame(args.get("rect"), fw, fh)
        if err:
            return _ok(err)
        rx1, ry1, rx2, ry2 = rect
        try:
            k = int(args.get("palette") or 8)
        except (TypeError, ValueError):
            k = 8
        k = max(2, min(24, k))
        import mss
        from PIL import Image
        with mss.mss() as sct:
            shot = sct.grab({"left": L, "top": T, "width": W, "height": H})
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        sx, sy = W / fw, H / fh
        px = (int(rx1 * sx), int(ry1 * sy),
              max(int(rx1 * sx) + 1, int(rx2 * sx)), max(int(ry1 * sy) + 1, int(ry2 * sy)))
        px = (min(px[0], W - 1), min(px[1], H - 1), min(px[2], W), min(px[3], H))
        palette, rows = _mosaic(img, px, gw, gh, k)
        cw, ch = (rx2 - rx1) / gw, (ry2 - ry1) / gh
        lines = [f"Mosaic of {title!r}: rect ({rx1:g},{ry1:g})-({rx2:g},{ry2:g}) in your "
                 f"{fw}x{fh} frame as {gw}x{gh} cells of {cw:.1f}x{ch:.1f} frame px, "
                 f"reduced to {len(palette)} colours:"]
        for letter, (r, g, b), share in palette:
            lines.append(f"  {letter}  #{r:02x}{g:02x}{b:02x}  ~{_color_name(r, g, b)}"
                         f"  {share * 100:.0f}% of cells")
        lines.append("Rows top to bottom, one letter per cell, column 0 at the left:")
        lines += ["  " + row for row in rows]
        layers = _runs_from_rows(rows, gw, gh)
        sid = _save_strokes({"kind": "mosaic", "source": title,
                             "rect": [rx1, ry1, rx2, ry2],
                             "aspect": (rx2 - rx1) / (ry2 - ry1),
                             "grid": [gw, gh],
                             "palette": {letter: f"#{r:02x}{g:02x}{b:02x}"
                                         for letter, (r, g, b), _ in palette},
                             "layers": layers})
        lines.append(f"Stroke set {sid}: "
                     + ", ".join(f"{letter} {len(layers.get(letter, []))} runs"
                                 for letter, _, _ in palette)
                     + ". Paint it ONE colour at a time, most common first: set "
                       f"that letter's colour, a brush at least one cell wide, then "
                       f"mouse_path(window=<target>, strokes_from='{sid}', "
                       f"layer='<letter>', target='[x1,y1,x2,y2]' in the target's "
                       f"frame, action='drag'). You never copy coordinates yourself.")
        _note(f"mosaic {gw}x{gh}/{len(palette)} colours of {title[:30]}")
        return _ok("\n".join(lines))

    # -- points mode -----------------------------------------------------
    strokes, err = _parse_strokes(args.get("points"))
    if err:
        return _ok(err)
    pts = [p for s in strokes for p in s]
    if len(pts) > 200:
        return _ok(f"{len(pts)} points is too many for one call (max 200).")
    mapped, err = _map_strokes([pts], frame, region)
    if err:
        return _ok(err)
    import mss
    from PIL import Image
    with mss.mss() as sct:
        shot = sct.grab({"left": L, "top": T, "width": W, "height": H})
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    lines = [f"Colours in {title!r} ({len(pts)} point(s)):"]
    for (fx, fy), (x, y) in zip(pts, mapped[0]):
        px = min(max(x - L, 0), img.width - 1)
        py = min(max(y - T, 0), img.height - 1)
        r, g, b = img.getpixel((px, py))[:3]
        lines.append(f"  ({fx:g}, {fy:g}): #{r:02x}{g:02x}{b:02x}  ~{_color_name(r, g, b)}")
    return _ok("\n".join(lines))


@tool("focus_window",
      "Bring a window to the front. maximize=true also makes it fill the "
      "screen — do that for a browser window before working in it, so the "
      "whole page is reachable.",
      {"title": str, "maximize": bool})
async def focus_window(args: dict[str, Any]) -> dict:
    """Maximising is a flag here rather than a tool of its own.

    A window is focused just before it is worked in, and that is exactly the
    moment to make it fill the screen — one call instead of two round trips,
    and one tool fewer in every request's schema.
    """
    b = _blocked()
    if b:
        return _ok(b)
    h = locator.find_window(title_contains=args.get("title", ""))
    if not h:
        return _ok(f"No window matching {args.get('title')!r}.")
    title = locator.window_title(h)
    ok = actuator.focus_window(h)
    if not args.get("maximize"):
        _note(f"focused {title[:40]}")
        return _ok(f"Focused {title!r}." if ok else "Could not focus it.")
    already = actuator.fills_screen(h)
    big = actuator.maximize_window(h)
    _note(f"{'maximised' if big else 'focused'} {title[:40]}")
    head = f"Focused {title!r}" if ok else f"Could not bring {title!r} to the front"
    if big and already:
        return _ok(f"{head}; it already fills the screen.")
    if big:
        return _ok(f"{head}; it is now maximised.")
    return _ok(f"{head}; it would not maximise — probably a fixed-size window, "
               f"so work in it as it is.")


@tool("open_app", "Launch an installed application by short name (excel, chrome, notepad).",
      {"name": str})
async def open_app(args: dict[str, Any]) -> dict:
    b = _blocked()
    if b:
        return _ok(b)
    import subprocess
    prof = load_profile()
    name = args.get("name", "").lower()
    target = prof["symbols"].get(f"APP:{name}")
    if not target:
        return _ok(f"{name!r} is not installed. Available: {', '.join(prof['capabilities'])}")
    if target.startswith("shell:"):
        subprocess.Popen(["explorer.exe", target], shell=False,
                         creationflags=noconsole.flags())
    else:
        subprocess.Popen([target], shell=False,
                         creationflags=noconsole.flags())
    _note(f"opened {name}")
    return _ok(f"Launched {name}. Wait a moment, then list_windows() to confirm.")


# ---------------------------------------------------------------------------
# browser
# ---------------------------------------------------------------------------

@tool("web_open", "Open a URL in the agent's browser (headless unless the site blocks it).",
      {"url": str})
async def web_open(args: dict[str, Any]) -> dict:
    """Also reports what is already known about this site.

    If a target was resolved here before, its selector is on file and can be
    used straight away — which lets a repeat visit skip the web_page call
    entirely. Saying so here is the difference between remembering and
    remembering usefully.
    """
    try:
        r = browser.goto(args.get("url", ""))
    except Exception as e:
        return _ok(f"Could not open it: {type(e).__name__}: {e}")
    _note(f"web open {r.get('url', '')}")

    lines = [f"Opened {r.get('url', '')}", f"Title: {r.get('title', '')}"]
    if r.get("note"):
        lines.append(r["note"])
    try:
        known = sorted(browser._flows.get(browser._host(), {}))
    except Exception:
        known = []
    if known:
        lines.append("")
        lines.append("Targets already known on this site: " + ", ".join(known))
        lines.append("You can pass these as `label` in web_do and act on them "
                     "immediately — no web_page needed unless something fails.")
    return _ok(chr(10).join(lines))


@tool("web_elements", "List clickable things on the page by role and name. "
      "Prefer web_page, which numbers them.", {})
async def web_elements(args: dict[str, Any]) -> dict:
    els = browser.elements()
    if not els:
        return _ok("No interactive elements found. The page may still be loading.")
    lines = [f"{len(els)} elements:"]
    for e in els:
        tid = f"  testid={e.testid}" if e.testid else ""
        lines.append(f"  {e.role:<10} {e.name[:44]:<44}{tid}")
    return _ok(chr(10).join(lines))


@tool("web_click", "Click one page element by its visible name or role. "
      "For more than one action, use web_do.",
      {"name": str, "role": str, "testid": str})
async def web_click(args: dict[str, Any]) -> dict:
    r = browser.click(name=args.get("name", ""), role=args.get("role", ""),
                      testid=args.get("testid", ""))
    _note(f"web click {args.get('name') or args.get('testid')}")
    if r.get("ok"):
        return _ok(f"Clicked via {r['via']}. Now at {r.get('url', '')}")
    return _ok(f"{r.get('error')}. {r.get('hint', '')}".strip())


@tool("web_fill", "Type into one form field identified by its label or "
      "placeholder. For more than one, use web_do.",
      {"name": str, "value": str})
async def web_fill(args: dict[str, Any]) -> dict:
    name = (args.get("name") or "").strip()
    if not name:
        # An empty label matches the FIRST labelled input on the page, so this
        # used to report "Filled via label." after typing into an arbitrary
        # field — the worst kind of success.
        return _ok("Name the field to fill (its label or placeholder). "
                   "Call web_page to see what is on the page.")
    r = browser.fill(name, args.get("value", ""))
    _note(f"web fill {args.get('name')}")
    return _ok(f"Filled via {r['via']}." if r.get("ok")
               else r.get("error", "failed"))


@tool("web_read", "Read the visible text of the current page.", {})
async def web_read(args: dict[str, Any]) -> dict:
    t = browser.text()
    return _ok(t or "Page has no readable text yet.")


@tool("web_status", "Where the browser is and whether it is visible.", {})
async def web_status(args: dict[str, Any]) -> dict:
    return _ok(json.dumps(browser.status(), indent=2))


@tool("web_page",
      "See the page as a numbered list of things you can act on. Call this "
      "first, then act by number with web_do.",
      {"only_visible": bool})
async def web_page(args: dict[str, Any]) -> dict:
    """One line per element, numbered.

    A number resolves to exactly one node; a name has to be searched for and
    can match several. That is why every action below takes a number.
    """
    snap = browser.snapshot()
    if snap.get("error"):
        return _ok(f"Could not read the page: {snap['error']}")
    if not snap.get("elements"):
        return _ok("Nothing interactive on this page. It may still be loading "
                   "— try web_read to see the text.")
    return _ok(browser.render(snap, only_inview=bool(args.get("only_visible"))))


@tool("web_do",
      "Do SEVERAL browser actions in one call. Far faster than one call each. "
      "steps is JSON: [{\"do\":\"fill\",\"target\":3,\"value\":\"Ada\","
      "\"label\":\"name\"},{\"do\":\"click\",\"target\":7,\"label\":\"submit\"}]",
      {"steps": str, "stop_on_error": bool})
async def web_do(args: dict[str, Any]) -> dict:
    """Batch the whole sequence. This is the main speed lever.

    The browser work is milliseconds; the cost that dominates is the model
    round trip around each call. Six fields is six round trips done one at a
    time and one done as a batch.

    `do` is one of: goto, click, fill, select, check, uncheck, hover, press,
    scroll, wait, read. `target` is a number from web_page, or a name.
    Always pass `label` — a short stable name for what you are acting on. It
    is what lets the second run of this task skip the search entirely.

    On failure it stops, says which step, and returns a fresh page listing, so
    recovering costs one more turn rather than starting over.
    """
    b = _blocked()
    if b:
        return _ok(b)

    raw = args.get("steps") or ""
    if isinstance(raw, str):
        try:
            steps = json.loads(raw)
        except Exception as e:
            return _ok(f"steps must be a JSON list: {e}")
    else:
        steps = raw
    if not isinstance(steps, list) or not steps:
        return _ok("Give a non-empty JSON list of steps.")
    # Each step must be an object. A list of bare values ("[1,2]") reached the
    # browser and died on `step.get(...)` with an AttributeError, which tells
    # the model nothing about what it got wrong.
    bad = [i for i, s in enumerate(steps, 1) if not isinstance(s, dict)]
    if bad:
        return _ok(f"Step(s) {', '.join(map(str, bad))} are not objects. Each "
                   f"step looks like {{\"do\":\"click\",\"target\":3,"
                   f"\"label\":\"submit\"}}.")

    r = browser.run(steps, stop_on_error=args.get("stop_on_error", True))
    _note(f"web_do {len(steps)} steps")

    lines = [f"{r['done']}/{r['of']} steps in {r['elapsed_s']}s"]
    for s in r["steps"]:
        mark = "ok  " if s.get("ok") else "FAIL"
        lines.append(f"  {mark} {s['step']}. {s.get('did') or s.get('error', '')}")
        if s.get("text"):
            lines.append("       " + s["text"][:600].replace("\n", " "))
    if not r["ok"]:
        lines.append("")
        lines.append(f"Stopped at step {r['failed_at']}. The page now looks like:")
        lines.append(r.get("page", ""))
    else:
        lines.append(f"Now at {r.get('url', '')}"
                     + (f"  ({r['title']})" if r.get("title") else ""))
    return _ok("\n".join(lines))


@tool("web_learned",
      "What this browser has already learned about a site: which selectors "
      "worked, and whether it needs a visible window.",
      {"host": str})
async def web_learned(args: dict[str, Any]) -> dict:
    host = (args.get("host") or "").strip()
    flows = browser._flows
    if host:
        flows = {host: flows.get(host, {})}
    if not any(flows.values()):
        return _ok("Nothing learned yet. Pass a `label` with each web_do step "
                   "and the second run of this task will skip the searching.")
    lines = ["Remembered selectors (used before searching):"]
    for h, entries in flows.items():
        if not entries:
            continue
        lines.append(f"  {h}")
        for label, e in sorted(entries.items()):
            lines.append(f"    {label:<24} {e['selector']:<28} "
                         f"{e.get('hits', 0)} hits")
    return _ok("\n".join(lines))


@tool("web_download",
      "Click something that produces a file, and keep the file. Use this "
      "instead of web_click for any download — a plain click loses it.",
      {"name": str, "role": str, "testid": str, "url": str})
async def web_download(args: dict[str, Any]) -> dict:
    url = (args.get("url") or "").strip()
    if not url and not any((args.get(k) or "").strip()
                           for k in ("name", "role", "testid")):
        # Without a target this waits the full 60s download timeout and then
        # reports a timeout, which reads as "the site is broken".
        return _ok("Say what to download: a url=, or the name= of the link or "
                   "button that produces the file.")
    r = (browser.download_url(url) if url else
         browser.download(name=args.get("name", ""), role=args.get("role", ""),
                          testid=args.get("testid", "")))
    _note(f"web download {args.get('name') or url}")
    if not r.get("ok"):
        return _ok(f"{r.get('error')}. {r.get('hint', '')}".strip())
    return _ok(f"Saved {r['name']} ({r['bytes']:,} bytes) to {r['path']}. "
               f"Read it with the Read tool or process it with Bash.")


@tool("web_upload",
      "Attach a file to a page's file input or upload button.",
      {"paths": str, "name": str, "testid": str})
async def web_upload(args: dict[str, Any]) -> dict:
    raw = (args.get("paths") or "").strip()
    files = [p.strip() for p in raw.split("|") if p.strip()] or [raw]
    r = browser.upload(files, name=args.get("name", ""),
                       testid=args.get("testid", ""))
    _note(f"web upload {len(files)} file(s)")
    return _ok(f"Attached {len(files)} file(s) via {r['via']}." if r.get("ok")
               else f"{r.get('error')}. {r.get('hint', '')}".strip())


@tool("web_tabs",
      "List, open, switch or close browser tabs. action: list | new | switch | close.",
      {"action": str, "index": int, "url": str})
async def web_tabs(args: dict[str, Any]) -> dict:
    action = (args.get("action") or "list").lower()
    if action == "new":
        browser.new_tab(args.get("url", ""))
    elif action == "switch":
        r = browser.switch_tab(int(args.get("index", 0)))
        if not r.get("ok"):
            return _ok(r.get("error", "failed"))
    elif action == "close":
        idx = args.get("index")
        browser.close_tab(None if idx in (None, "") else int(idx))
    tabs = browser.tabs()
    if not tabs:
        return _ok("No browser tabs open.")
    lines = [f"{'*' if t['active'] else ' '} [{t['index']}] {t['title'][:44]}"
             f"  {t['url'][:70]}" for t in tabs]
    return _ok("\n".join(["Tabs (* = the one you are driving):"] + lines))


@tool("web_session",
      "Check whether the browser already has a login for a site, before "
      "assuming you need to sign in.",
      {"host": str})
async def web_session(args: dict[str, Any]) -> dict:
    r = browser.signed_in(args.get("host", ""))
    return _ok(json.dumps(r, indent=2))


# ---------------------------------------------------------------------------
# machine
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# credentials and asking the user
# ---------------------------------------------------------------------------

@tool("list_credentials",
      "See which logins are stored (names only — values are never readable by you).",
      {})
async def list_credentials(args: dict[str, Any]) -> dict:
    from core.secrets import vault
    items = vault.list()
    if not items:
        return _ok("No credentials stored. If a task needs a login, call "
                   "use_credential and the user will be asked to provide it.")
    lines = ["Stored credentials (you can use these, not read them):"]
    for i in items:
        lines.append(f"  {i.name:<28} {i.note[:44]}"
                     + (f"  used {i.uses}x" if i.uses else "  never used"))
    return _ok("\n".join(lines))


@tool("use_credential",
      "Type a stored secret into the focused field WITHOUT ever seeing its value. "
      "If it is not stored yet, the user is asked for it first.",
      {"name": str, "where": str})
async def use_credential(args: dict[str, Any]) -> dict:
    """The value goes straight from the vault into the keyboard.

    It is never returned to the model, never logged, and never enters the
    transcript — so a task can log in without the credential existing anywhere
    a conversation could leak it.
    """
    b = _blocked()
    if b:
        return _ok(b)

    from core.channel import Answer, channel
    from core.secrets import vault, VaultError

    name = (args.get("name") or "").strip()
    where = args.get("where") or "the focused field"
    if not name:
        return _ok("Give the credential a name, e.g. 'gmail-password'.")

    if not vault.has(name):
        r = channel.ask(
            f"A task needs a credential that isn't stored: '{name}'.\n"
            f"It will be typed into {where}.\n"
            f"Add it in the app (Settings → Credentials), then approve here.",
            approve_deny=True, timeout_s=600, cancelled=should_stop)
        # A typed value is accepted only from the app. Over Telegram it would
        # leave the password sitting in a chat history forever, which is worse
        # than the inconvenience of walking back to the machine.
        if r.answer is Answer.TEXT and r.text and r.via == "app":
            vault.put(name, r.text, note=f"provided when asked (for {where})")
        if not vault.has(name):
            return _ok(f"Cannot proceed: '{name}' is not available "
                       f"({r.summary()}). Stop, and tell the user exactly which "
                       f"credential to add and what it is for.")

    try:
        value = vault.get(name)
    except VaultError as e:
        return _ok(f"Vault error: {e}")

    r = actuator.type_text(value)
    _note(f"entered credential «{name}»")
    return _ok(f"Typed the stored value for '{name}' into {where}. "
               f"({len(value)} characters — the value itself is not shown to you.)"
               if r.ok else f"Could not type the credential for '{name}'.")


@tool("ask_user",
      "Ask the user a question and wait. Reaches them in the app or on Telegram.",
      {"question": str, "approve_deny": bool, "timeout_minutes": int})
async def ask_user(args: dict[str, Any]) -> dict:
    """Use when you genuinely cannot proceed without a human decision.

    Never guess a password, never invent data, never pick for the user on
    something irreversible. Asking costs time; guessing costs trust.
    """
    from core.channel import channel
    q = (args.get("question") or "").strip()
    if not q:
        return _ok("Nothing to ask.")
    mins = int(args.get("timeout_minutes") or 15)
    prev = locator.foreground_window()
    r = channel.ask(q, approve_deny=bool(args.get("approve_deny", True)),
                    timeout_s=mins * 60, cancelled=should_stop)
    _note(f"asked user: {q[:44]}")
    # Answering happens in the agent's own window, which the question raised
    # — and the next type_text then went into the agent itself, silently
    # swallowing a WhatsApp message (run of 2026-09-08). If the front window
    # is now OURS and the one the work was in still exists, give it back.
    restored = ""
    try:
        import ctypes
        from ctypes import wintypes
        cur = locator.foreground_window()
        if prev and cur and cur != prev and ctypes.windll.user32.IsWindow(prev):
            pid = wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(cur, ctypes.byref(pid))
            if pid.value == os.getpid():
                if actuator.focus_window(prev):
                    restored = (f" Focus is back on "
                                f"{locator.window_title(prev)!r}.")
                    _note("gave focus back to the task window")
    except Exception:
        pass
    return _ok(f"User replied: {r.summary()}.{restored}"
               if restored else f"User replied: {r.summary()}")


@tool("notify_user",
      "Tell the user something without waiting — progress on a long task, or a result.",
      {"message": str})
async def notify_user(args: dict[str, Any]) -> dict:
    from core.channel import channel
    sent = channel.notify(args.get("message", ""))
    return _ok(f"Sent via {', '.join(sent)}." if sent
               else "No remote channel configured; the message is in the app feed only.")


@tool("find_job",
      "Check whether a saved job already does what was asked, BEFORE doing it "
      "by hand. Searches by description, not by name.",
      {"description": str})
async def find_job(args: dict[str, Any]) -> dict:
    """Ranked candidates and why each matched — the decision stays with you.

    Matching is lexical, so it will not connect "stock market" to "borsa" on
    its own. That is why scores and match reasons are returned rather than a
    single answer: you can see that a 0.6 hit on the right concept beats a
    0.9 hit on the wrong one.
    """
    from core.retrieval import suggest
    from core.store import Store

    q = (args.get("description") or "").strip()
    if not q:
        return _ok("Describe the job to search for.")
    hits = suggest(Store(), q, limit=5)
    if not hits:
        return _ok(f"No saved job resembles {q!r}. Do it directly; if it is "
                   f"worth repeating, it can be saved afterwards.")
    lines = [f"Saved jobs that may already do this ({len(hits)}):"]
    for c in hits:
        lines.append(
            f"  {c.name}  [{c.id}]  score {c.score}\n"
            f"      {c.description[:88]}\n"
            f"      {c.why()} · {c.trust}"
            + (f" · {c.runs} runs, {c.success_rate:.0%} ok" if c.runs else " · never run"))
    lines.append("\nA high score is not a decision. Read the description: "
                 "if it is not actually the same job, ignore it and do the "
                 "task directly.")
    return _ok("\n".join(lines))


@tool("schedule_task",
      "Set work to run on a cadence — \"every morning at 9\", \"weekdays at 8am\", "
      "\"every 3 hours\". Use action=list or action=remove to manage them.",
      {"task": str, "when": str, "action": str, "id": str})
async def schedule_task(args: dict[str, Any]) -> dict:
    """Schedules only fire while the app is running.

    That is a deliberate limit, not an oversight: a Windows scheduled task can
    be launched into a session with no desktop at all, and this agent exists to
    drive a desktop. It would fire, find no windows, and report success on
    having done nothing. Say so plainly if the user expects it to run overnight
    with the app closed.
    """
    from core.schedule import parse, scheduler

    action = (args.get("action") or "").lower()
    if action in ("list", "show"):
        rows = scheduler.status()
        if not rows:
            return _ok("Nothing is scheduled.")
        lines = ["Scheduled work:"]
        for r in rows:
            state = "" if r["enabled"] else " · disabled"
            history = f" · {r['runs']} runs" if r["runs"] else ""
            missed = f" · {r['misses']} missed while closed" if r["misses"] else ""
            lines.append(f"  [{r['id']}] {r['task'][:52]}")
            lines.append(f"      {r['when']} · next {r['next_human']}"
                         f"{state}{history}{missed}")
        return _ok(chr(10).join(lines))

    if action in ("remove", "delete", "cancel"):
        sid = (args.get("id") or "").strip()
        return _ok("Removed." if scheduler.remove(sid)
                   else f"No schedule with id {sid!r}. Use action=list first.")

    task = (args.get("task") or "").strip()
    when = (args.get("when") or "").strip()
    if not task or not when:
        return _ok("Give both the task and when it should run.")
    spec = parse(when)
    if not spec.get("understood"):
        return _ok(f"Could not read {when!r} as a time. Understood forms: "
                   f"'every morning at 9', 'weekdays at 8am', 'every 3 hours', "
                   f"'monday and thursday at 7pm'.")
    s = scheduler.add(task, when)
    _note(f"scheduled: {task[:40]}")
    return _ok(f"Scheduled — {s.describe()}. It runs only while this app is "
               f"open; anything due while it is closed is reported, not run.")


# ---------------------------------------------------------------------------
# recording a demonstration into a job
# ---------------------------------------------------------------------------

#: The live recorder, while one is running. One at a time by construction —
#: two recorders sharing the input hook would each see the other's events.
_recorder = None


@tool("record_job",
      "Record the user demonstrating a task, then propose it as a job. "
      "action: start | stop | save | discard.",
      {"action": str, "name": str, "description": str})
async def record_job(args: dict[str, Any]) -> dict:
    """Three steps, and the middle one is not optional.

    start -> the user demonstrates -> stop, which returns a DRAFT in plain
    English. Show that draft to the user and get their confirmation before
    calling save. A job built from a misread demonstration replays cleanly and
    does the wrong thing every single time it runs, which is far worse than
    asking one question.
    """
    global _recorder
    from core import induce
    from core.paths import data
    from core.recorder import Recorder
    from core.store import Store

    action = (args.get("action") or "").lower()

    if action == "start":
        if _recorder is not None:
            return _ok("Already recording. Stop the current recording first.")
        out = data("recordings", f"rec_{int(time.time())}")
        _recorder = Recorder(out, capture_frames=True)
        if not _recorder.start():
            _recorder = None
            return _ok("Could not start recording — the input hook is not running.")
        # The desktop hook sees a click on the browser window and nothing more
        # — it cannot know which DOM element was under the cursor. Recording
        # inside the page is the only place that information exists.
        web = {"ok": False}
        if browser.running:
            web = browser.start_recording()
        _note("recording started")
        return _ok("Recording. Tell the user to perform the task now, and to "
                   "say when they are done. Do not drive the machine yourself "
                   "while recording — your own clicks would be captured as "
                   "theirs. "
                   + ("Browser actions are being recorded too."
                      if web.get("ok") else
                      "NOTE: the agent's browser is not open, so anything the "
                      "user does in their OWN browser will not be captured — "
                      "only desktop applications. Open it with web_open first "
                      "if the task is a web task."))

    # Only stop and discard need a live recorder. save operates on the draft
    # that stop produced — by which time the recorder is already gone, so
    # gating save on it would reject every legitimate save.
    if action in ("stop", "discard") and _recorder is None:
        return _ok("Nothing is being recorded.")

    if action == "discard":
        _recorder.stop()
        _recorder = None
        return _ok("Recording discarded. Nothing was saved.")

    if action == "stop":
        _recorder.stop()
        summary = _recorder.summary()
        _recorder.save()
        _recorder = None
        web_events = []
        try:
            web_events = browser.stop_recording()
        except Exception:
            pass
        draft = (induce.from_web_recording(web_events, summary) if web_events
                 else induce.from_recording(summary))
        if not draft.steps:
            return _ok("Nothing usable was recorded — no clicks or typing "
                       "outside this app's own window, and nothing in the "
                       "agent's browser.")
        globals()["_draft"] = draft
        warn = induce.looks_sensitive(draft)
        parts = [draft.describe()]
        if warn:
            parts.append("")
            parts.append("Before saving:")
            parts.extend(f"  - {w}" for w in warn)
        parts.append("")
        parts.append("SHOW THIS TO THE USER AND WAIT. Only call "
                     "record_job(action=save) once they have confirmed it, "
                     "with a name and description in their own words.")
        return _ok(chr(10).join(parts))

    if action == "save":
        draft = globals().get("_draft")
        if draft is None:
            return _ok("No draft to save. Record something first.")
        macro, local = induce.to_macro(
            draft, name=args.get("name", ""),
            description=args.get("description", ""))
        Store().save(macro, local)
        globals()["_draft"] = None
        _note(f"saved recorded job: {macro.name}")
        return _ok(f"Saved '{macro.name}' [{macro.id}] with {len(macro.steps)} "
                   f"steps, trust=draft. The first real run is observed — a "
                   f"demonstration is not a run.")

    return _ok("action must be start, stop, save or discard.")


@tool("edit_job",
      "Change a saved job: rename, re-describe, remove a step, or delete it. "
      "action: show | rename | describe | drop_step | delete.",
      {"id": str, "action": str, "value": str, "step": int})
async def edit_job(args: dict[str, Any]) -> dict:
    """Editing is deliberately coarse.

    Rewriting a step's locators by hand is how a working job becomes a broken
    one that still looks right. Removing a step and re-recording is safer, and
    the replay engine repairs drifted locators on its own.
    """
    from core.store import Store

    store = Store()
    jid = (args.get("id") or "").strip()
    action = (args.get("action") or "show").lower()
    if not jid:
        return _ok("Which job? Use find_job to get its id.")
    try:
        macro, local = store.load(jid)
    except Exception:
        return _ok(f"No job with id {jid!r}.")

    if action == "show":
        lines = [f"{macro.name} [{macro.id}] — trust={local.trust.value}",
                 f"  {macro.description}", ""]
        for i, s in enumerate(macro.steps, 1):
            lines.append(f"  {i}. {s.action.value}: {s.intent}"
                         + (f"  args={s.args}" if s.args else ""))
        if macro.requires_secrets:
            lines.append("")
            lines.append("Needs credentials: " + ", ".join(macro.requires_secrets))
        return _ok(chr(10).join(lines))

    if action == "delete":
        return _ok("Deleted." if store.delete(jid) else "Could not delete it.")

    if action == "rename":
        macro.name = (args.get("value") or macro.name).strip()
    elif action == "describe":
        macro.description = (args.get("value") or macro.description).strip()
    elif action == "drop_step":
        n = int(args.get("step") or 0)
        if not (1 <= n <= len(macro.steps)):
            return _ok(f"Step {n} does not exist; the job has "
                       f"{len(macro.steps)}.")
        removed = macro.steps.pop(n - 1)
        # Per-step statistics are keyed by index, so they now describe the
        # wrong steps. Clearing them costs this machine its learned ordering
        # and is far better than confidently preferring a locator that
        # belonged to a step that no longer exists.
        local.locator_stats = {}
        store.save(macro, local)
        return _ok(f"Removed step {n} ({removed.intent}). Locator statistics "
                   f"were reset — they were keyed to the old numbering.")
    else:
        return _ok("action must be show, rename, describe, drop_step or delete.")

    macro.version += 1
    store.save(macro, local)
    return _ok(f"Updated. {macro.name} — {macro.description}")


# ---------------------------------------------------------------------------
# replay — record the agent's own actions, then redo them without the model
# ---------------------------------------------------------------------------

#: Acting tools worth journaling. Read-only looks (screenshot, find_controls),
#: waits, and the meta tools (ask_user, record_job, replay itself) are left
#: out — a replay redoes what CHANGED the machine, not what merely observed it.
_JOURNALLED = {
    "click", "type_text", "press_key", "focus_window", "open_app",
    "do_steps", "mouse_path", "copy_to_clipboard", "use_credential",
    "web_open", "web_click", "web_fill", "web_do", "web_download",
    "web_upload", "web_session", "web_page", "web_tabs",
}

#: The live screen recording, while one is running. One at a time.
_screencast = None

#: Breathing room between replayed actions, mirroring do_steps' settle.
_REPLAY_SETTLE = 0.35


def _raw_handlers() -> dict:
    """name -> the UNWRAPPED tool body.

    A replay runs on the worker thread already; calling the _offload-wrapped
    handler would submit a second job to the same single worker and then wait
    for it — a deadlock, because the worker is busy running the replay. The
    raw body (kept by functools.wraps as __wrapped__) runs inline instead.
    """
    m = {}
    for t in ALL_TOOLS:
        h = getattr(t, "handler", None)
        if h is not None:
            m[t.name] = getattr(h, "__wrapped__", h)
    return m


def _first_line(result: dict | None) -> str:
    for b in (result or {}).get("content", []):
        if b.get("type") == "text":
            txt = str(b.get("text", "")).strip()
            return txt.splitlines()[0] if txt else ""
    return ""


def _looks_blocked(result: dict | None) -> bool:
    """True if a tool result is an arbiter refusal rather than a real action —
    the wording _blocked() produces. Such a call did nothing and must not be
    journaled as if it had."""
    line = _first_line(result if isinstance(result, dict) else None)
    return line.startswith("STOP:") or line.startswith("Cannot act right now")


@tool("replay",
      "Record the actions you take so the whole task can be redone later "
      "without you, and optionally capture the screen as a small video. "
      "action: start | stop | run | list | show | delete | discard. "
      "On start: actions=false to record ONLY the screen; screen=true to also "
      "film it (fps default 15, scale default 0.5 = half size). "
      "run needs id=, from replay(action=list).",
      {"action": str, "id": str, "name": str, "description": str,
       "screen": bool, "fps": int, "scale": float, "actions": bool})
async def replay(args: dict[str, Any]) -> dict:
    """Two streams, both optional, started together and stopped together.

    The action journal is the signal: every acting tool you call is written
    down as called (tool name + arguments), and a saved journal is replayed by
    calling those same tools again in order — each still resolving its target
    by name and verifying itself, so the redo is as robust as the first run
    was. The screen video is context for the user to watch back; the model
    never sees it.

    Record from the FIRST action of the task, not the middle: call
    replay(action=start) before you begin, do the task normally, then
    replay(action=stop). Do not drive the machine between start and the task —
    every acting call in between lands in the recording.
    """
    global _screencast
    action = (args.get("action") or "").lower()

    if action == "start":
        if jrnl.armed() or _screencast is not None:
            return _ok("Already recording. Stop it first with "
                       "replay(action=stop).")
        rec_actions = args.get("actions")
        rec_actions = True if rec_actions is None else bool(rec_actions)
        want_screen = bool(args.get("screen"))
        if not rec_actions and not want_screen:
            return _ok("Nothing to record: set actions=true (default) to log "
                       "your actions, screen=true to film the screen, or both.")
        started = []
        if rec_actions:
            jrnl.replaying = False
            jrnl.live = jrnl.Recorder(
                name=str(args.get("name") or ""),
                description=str(args.get("description") or ""))
            started.append("actions")
        if want_screen:
            from core.screencast import ScreenCast
            try:
                fps = float(args.get("fps") or 15)
            except (TypeError, ValueError):
                fps = 15.0
            try:
                scale = float(args.get("scale") or 0.5)
            except (TypeError, ValueError):
                scale = 0.5
            out = paths.data("workspace", "screencasts", f"cast_{int(time.time())}")
            sc = ScreenCast(out, fps=fps, scale=scale)
            if sc.start():
                _screencast = sc
                started.append(f"screen ~{sc.fps:.0f}fps at {sc.scale:.0%} size")
            else:
                started.append(f"(screen recording could NOT start: "
                               f"{sc.error or 'unknown'})")
        _note("recording started: " + ", ".join(started))
        return _ok("Recording " + " + ".join(started) + ". Do the task now, "
                   "then call replay(action=stop). Everything you do until "
                   "then is captured.")

    if action in ("stop", "discard"):
        cast_report = ""
        vid = ""
        if _screencast is not None:
            info = _screencast.stop()
            _screencast = None
            if action == "discard":
                try:
                    from pathlib import Path as _P
                    _P(info["path"]).unlink()
                except Exception:
                    pass
            elif info.get("error"):
                cast_report = f"\nScreen video failed: {info['error']}"
            elif info.get("frames"):
                vid = info["path"]
                cast_report = (
                    f"\nScreen video: {info['path']} — {info['seconds']}s, "
                    f"{info['frames']} frames at {info['actual_fps']}fps, "
                    f"{info['dims'][0]}x{info['dims'][1]}, "
                    f"{info['bytes'] / 1e6:.1f} MB.")
        rec = jrnl.live
        jrnl.live = None
        if rec is None:
            if action == "discard":
                return _ok("Discarded." + (" Screen video deleted." if _screencast
                                           is None and cast_report == "" else ""))
            return _ok(("Stopped." + cast_report) if cast_report
                       else "Nothing was being recorded.")
        if action == "discard":
            return _ok("Recording discarded. No session saved."
                       + (" Screen video deleted." if vid == "" else ""))
        rec.video_path = vid
        data = jrnl.save(rec, name=str(args.get("name") or ""),
                            description=str(args.get("description") or ""))
        _note(f"saved replay session: {data['name']} [{data['id']}]")
        if not data["entries"]:
            return _ok("Stopped — but no actions were recorded (only looking "
                       "does not count)." + cast_report)
        return _ok(
            f"Saved '{data['name']}' [{data['id']}] — {len(data['entries'])} "
            f"actions, {data['duration_s']}s.\n{rec.describe()}{cast_report}\n\n"
            f"Redo it any time with replay(action=run, id={data['id']}).")

    if action == "list":
        items = jrnl.list_all()
        if not items:
            return _ok("No saved sessions yet. Record one with "
                       "replay(action=start).")
        lines = [f"Saved sessions ({len(items)}):"]
        for d in items:
            when = time.strftime("%Y-%m-%d %H:%M",
                                 time.localtime(d.get("created", 0)))
            vid = " · has video" if d.get("video") else ""
            lines.append(f"  {d.get('name', '?')}  [{d.get('id')}]  "
                         f"{len(d.get('entries', []))} actions · {when}{vid}")
            if d.get("description"):
                lines.append(f"      {d['description'][:88]}")
        return _ok("\n".join(lines))

    if action == "show":
        data = jrnl.load((args.get("id") or "").strip())
        if data is None:
            return _ok("No such session. Use replay(action=list) for the ids.")
        head = (f"{data.get('name', '?')} [{data.get('id')}] — "
                f"{len(data.get('entries', []))} actions, "
                f"{data.get('duration_s', 0)}s"
                + (" · has video" if data.get("video") else ""))
        rows = [f"  {e.get('i'):>2}. {jrnl.summarise(e.get('tool'), e.get('args') or {})}"
                for e in data.get("entries", [])]
        return _ok(head + "\n" + "\n".join(rows))

    if action == "delete":
        jid = (args.get("id") or "").strip()
        return _ok("Deleted." if jrnl.delete(jid)
                   else f"No session with id {jid!r}.")

    if action == "run":
        jid = (args.get("id") or "").strip()
        data = jrnl.load(jid)
        if data is None:
            return _ok(f"No saved session {jid!r}. Use replay(action=list).")
        entries = data.get("entries") or []
        if not entries:
            return _ok(f"{jid} recorded no actions, so there is nothing to redo.")
        b = _blocked()
        if b:
            return _ok(b)
        handlers = _raw_handlers()
        jrnl.replaying = True
        lines: list[str] = []
        done, halted = 0, ""
        t0 = time.time()
        try:
            for e in entries:
                if _blocked():
                    halted = _blocked() or _STOP_MSG
                    break
                tool_name = e.get("tool")
                targs = e.get("args") or {}
                fn = handlers.get(tool_name)
                label = jrnl.summarise(tool_name, targs)
                if fn is None:
                    lines.append(f"  SKIP {e.get('i')}. {label} — tool gone")
                    continue
                _note(f"replay {e.get('i')}/{len(entries)}: {label}"[:80])
                try:
                    res = await fn(targs)
                    txt = _first_line(res)
                    if txt.startswith("STOP:"):
                        lines.append(f"  STOP {e.get('i')}. {label} — {txt[:70]}")
                        halted = txt
                        break
                    lines.append(f"  ok   {e.get('i')}. {label}"
                                 + (f" — {txt[:60]}" if txt else ""))
                    done += 1
                except Exception as ex:
                    lines.append(f"  FAIL {e.get('i')}. {label} — "
                                 f"{type(ex).__name__}: {ex}")
                time.sleep(_REPLAY_SETTLE)
        finally:
            jrnl.replaying = False
        head = (f"Replayed {done}/{len(entries)} actions from "
                f"'{data.get('name', jid)}' in {time.time() - t0:.1f}s.")
        if halted:
            head += f"  Halted: {halted}"
        elif done < len(entries):
            head += ("  Some actions did not confirm — take a screenshot to see "
                     "where it diverged; the UI may have changed since recording.")
        return _ok(head + "\n" + "\n".join(lines))

    return _ok("action must be start, stop, run, list, show, delete or discard.")


@tool("machine_info", "What this PC has: screen, installed apps, capabilities.", {})
async def machine_info(args: dict[str, Any]) -> dict:
    p = load_profile()
    return _ok(f"Screen {p['screen']['w']}x{p['screen']['h']} @{p['screen']['dpi_scale']}x\n"
               f"Apps: {', '.join(p['capabilities'])}\n"
               f"Folders: {', '.join(k for k in p['symbols'] if not k.startswith('APP:'))}")


@tool("copy_to_clipboard",
      "Put text or an image FILE on the clipboard, ready to paste with "
      "press_key('ctrl+v'). This is how you put a screenshot into a web app "
      "like WhatsApp Web or Gmail — far more reliable than driving the native "
      "file dialog. Pass image_path= for a picture, or text= for text.",
      {"text": str, "image_path": str})
async def copy_to_clipboard(args: dict[str, Any]) -> dict:
    """Pasting beats attaching.

    Sending a picture through a web app otherwise means clicking an attach
    button, then driving a native Windows file-open dialog — a separate
    window, in the user's language, that swallows keystrokes aimed elsewhere.
    Putting the image on the clipboard and pressing Ctrl+V skips all of it.
    """
    b = _blocked()
    if b:
        return _ok(b)

    from pathlib import Path as _P
    from core import clipboard

    path = (args.get("image_path") or "").strip().strip('"')
    if path:
        p = _P(path)
        if not p.exists():
            return _ok(f"No file at {path}. Take a screenshot first, then pass "
                       f"the path it reports.")
        if not clipboard.set_image(str(p)):
            why = clipboard.last_error() or "unknown reason"
            return _ok(f"Could not put {p.name} on the clipboard ({why}). If "
                       f"another program was holding the clipboard, try once "
                       f"more; if it is not an image, attach it through the "
                       f"app's file dialog instead.")
        _note(f"clipboard image {p.name}")
        return _ok(f"{p.name} is on the clipboard. Click the message box, then "
                   f"press_key ctrl+v to paste it, then press_key enter to send.")

    text = args.get("text")
    if text:
        if not clipboard.set_text(str(text)):
            return _ok(f"Could not set the clipboard "
                       f"({clipboard.last_error() or 'unknown reason'}). "
                       f"Another program may be holding it — try again.")
        _note(f"clipboard text {str(text)[:30]}")
        return _ok(f"{len(str(text))} characters on the clipboard. "
                   f"Click the field, then press_key ctrl+v.")

    return _ok("Pass image_path= for a picture, or text= for text.")


@tool("recall_lessons",
      "Look up what was learned from earlier tasks about this kind of work — how "
      "an app or site actually behaves — BEFORE struggling with it. The relevant "
      "lessons are already shown to you at the start of a task; call this when a "
      "new obstacle comes up mid-task.",
      {"situation": str})
async def recall_lessons(args: dict[str, Any]) -> dict:
    from core import lessons

    q = (args.get("situation") or "").strip()
    if not q:
        return _ok("Describe the situation to look up lessons for.")
    hits = lessons.recall(q, limit=5)
    if not hits:
        return _ok(f"No lesson recorded yet for {q!r}. If you work this out, "
                   f"call save_lesson so the next attempt starts knowing it.")
    return _ok(lessons.as_prompt_block(hits))


@tool("save_lesson",
      "Write down something you learned the hard way, AFTER you struggled with it "
      "and it finally worked — so the next task like this starts knowing it. Give "
      "a short title, the problem (name the app/site and the symptom), and the "
      "solution that worked. apps and keywords are optional, comma-separated, and "
      "make it easier to find later. Do NOT save things that worked first try or "
      "facts specific to one run.",
      {"title": str, "problem": str, "solution": str, "apps": str, "keywords": str})
async def save_lesson(args: dict[str, Any]) -> dict:
    from core import lessons
    from core.secrets import redact

    title = redact((args.get("title") or "").strip())
    problem = redact((args.get("problem") or "").strip())
    solution = redact((args.get("solution") or "").strip())
    if not title or not solution:
        return _ok("A lesson needs at least a title and the solution that worked.")
    lesson, created = lessons.add(
        title=title, problem=problem, solution=solution,
        apps=redact(args.get("apps") or ""), keywords=redact(args.get("keywords") or ""),
        source_task=redact(_current_task()))
    verb = "Saved" if created else "Updated"
    return _ok(f"{verb} lesson: {lesson['title']!r}. It will be shown "
               f"automatically the next time a task looks like this.")


ALL_TOOLS = [list_windows, find_controls, read_labels, screenshot,
             wait_for_reply,
             do_steps,
             mouse_path,
             sample_colors,
             trace_edges,
             click, type_text, press_key, focus_window, open_app,
             web_open, web_page, web_do, web_learned,
             web_elements, web_click, web_fill, web_read, web_status,
             web_download, web_upload, web_tabs, web_session,
             list_credentials, use_credential, ask_user, notify_user,
             find_job, schedule_task, record_job, edit_job, replay,
             machine_info, copy_to_clipboard, recall_lessons, save_lesson]


# ---------------------------------------------------------------------------
# keeping the event loop free
# ---------------------------------------------------------------------------

class _ToolWorker:
    """One thread that runs every tool body, off the event loop.

    Every handler here is `async def` because the SDK requires it, and not one
    of them actually awaits anything — the work is UI Automation calls, sleeps
    and waits, all of it blocking. Run directly on the loop, a single tool call
    stops everything else on that loop dead. Measured: **zero heartbeats in
    6.2 seconds** while one tool was busy.

    What that cost was not abstract:

      * no messages read from the Claude CLI, so the task appeared frozen and
        the live feed stopped updating mid-step;
      * the cancel check lives in the message loop, so **Stop did nothing**
        until the tool finished on its own;
      * a coroutine scheduled to release a wait could never run. A probe that
        paused a tool and scheduled the unpause as a coroutine deadlocked
        outright.

    ONE thread, not a pool, for two reasons that both bite:

      * UI Automation is COM. COM is initialised per thread, and a thread that
        skipped it fails with "CoInitialize has not been called — can not load
        UIAutomationCore.dll". A pool would have to initialise every worker.
      * UIA objects have thread affinity, and `locator` caches them between
        calls. Handing a cached element to a different thread than the one
        that created it is a marshalling bug that would surface as rare,
        inexplicable failures rather than an honest error.

    Serialising tool calls is not a loss: the arbiter serialises actuation
    anyway, and the SDK issues one call at a time.
    """

    def __init__(self) -> None:
        self._gen = 0
        self._start()

    def _start(self) -> None:
        # The queue is handed to the thread rather than read off `self`, so
        # an abandoned thread keeps ITS queue: when it finally unblocks it
        # finds nothing more to do and idles, instead of grabbing the next
        # job meant for its replacement.
        self._q: "queue.Queue" = queue.Queue()
        self._gen += 1
        self._thread = threading.Thread(target=self._serve, args=(self._q,),
                                        name=f"tools-{self._gen}", daemon=True)
        self._thread.start()

    def abandon(self) -> None:
        """Give up on a thread stuck inside a call that will not return, and
        serve from a fresh one.

        Nothing can interrupt a COM call: with Paint's "Edit colors" flyout
        open, UI Automation simply did not answer until the flyout closed,
        and every tool queued behind it — including the screenshot that
        would have shown the way out — hung with it. The stuck thread is
        left to unblock whenever the app lets it; its result is discarded.
        """
        self._start()

    def _serve(self, q: "queue.Queue") -> None:
        try:
            import ctypes
            # COINIT_APARTMENTTHREADED — what UI Automation expects.
            ctypes.windll.ole32.CoInitializeEx(None, 0x2)
            # Same reasoning as COM: this is the thread every tool runs on,
            # so it is the thread whose coordinate space must match UIA's.
            actuator.make_thread_dpi_aware()
        except Exception:
            pass
        # UIA objects have thread affinity. A replacement thread must not
        # touch elements the abandoned one created.
        locator.invalidate_cache()
        while True:
            job = q.get()
            if job is None:
                return
            fn, args, box, done = job
            try:
                import asyncio as _aio
                box.append(("ok", _aio.run(fn(args))))
            except BaseException as e:          # noqa: BLE001 — re-raised below
                box.append(("err", e))
            finally:
                done.set()

    def submit(self, fn, args):
        box, done = [], threading.Event()
        self._q.put((fn, args, box, done))
        return box, done


_worker = _ToolWorker()


#: How long a tool may run before it is presumed stuck. Most finish in well
#: under a second; the exceptions are the ones that wait on purpose — for a
#: person, a page, a download, or the model's own long batch.
_TOOL_BUDGET_DEFAULT = 90.0
_TOOL_BUDGETS = {
    "ask_user": 4000.0, "wait_for_reply": 660.0, "record_job": 7200.0,
    "replay": 3600.0,
    "do_steps": 1200.0, "web_do": 600.0, "mouse_path": 300.0,
    "read_labels": 300.0, "web_download": 240.0, "web_upload": 240.0,
    "web_open": 120.0, "web_session": 120.0, "web_tabs": 120.0,
}


def _offload(fn):
    """Move a tool body onto the worker thread, leaving the loop free.

    Two things a tool must never do to the model: hang, and vanish. A real
    run stalled on three find_controls calls in a row that came back as
    nothing at all — the worker was stuck inside UI Automation behind an
    open flyout, and the exception that eventually surfaced was rendered as
    empty output. Now a call past its budget is abandoned with a message
    that names the cause and the way out, and an exception comes back as
    text the model can act on.
    """
    import asyncio
    import functools
    name = getattr(fn, "__name__", "tool")

    @functools.wraps(fn)
    async def wrapper(args):
        budget = _TOOL_BUDGETS.get(name, _TOOL_BUDGET_DEFAULT)
        box, done = _worker.submit(fn, args)
        deadline = time.time() + budget
        # Poll rather than block: awaiting sleep is what keeps the loop
        # servicing everything else while the tool works.
        while not done.is_set():
            if time.time() > deadline:
                _worker.abandon()
                _note(f"{name} stalled after {budget:.0f}s — worker replaced")
                return _ok(f"{name} did not finish within {budget:.0f}s and was "
                           f"abandoned. The window's UI tree has stopped "
                           f"answering — that is what an open flyout, menu or "
                           f"dialog that blocks accessibility looks like "
                           f"(Paint's 'Edit colors' flyout does this). Press "
                           f"escape to close it (press_key escape) and try again, "
                           f"or work from a screenshot with mouse_path while it "
                           f"is open.")
            await asyncio.sleep(0.02)
        kind, value = box[0]
        if kind == "err":
            if isinstance(value, (KeyboardInterrupt, SystemExit)):
                raise value
            return _ok(f"{name} failed inside the tool: {type(value).__name__}: "
                       f"{value}. Take a screenshot or call find_controls to see "
                       f"the current state, then continue another way.")
        # A recording, if one is armed, gets the acting calls only — the ones
        # that changed the machine and are worth redoing. record_call itself
        # skips this while a replay is running, so a redo is not re-recorded.
        # An action the arbiter blocked (Stop, ESC+ESC, user at the keyboard)
        # returns its refusal WITHOUT acting, so it must not enter the journal
        # as though it happened.
        if name in _JOURNALLED and not _looks_blocked(value):
            jrnl.record_call(name, args)
        return value

    return wrapper


def _offload_all(tools: list) -> list:
    """Wrap every tool's handler. Idempotent."""
    for t in tools:
        h = getattr(t, "handler", None)
        if h is None or getattr(h, "_offloaded", False):
            continue
        wrapped = _offload(h)
        wrapped._offloaded = True
        t.handler = wrapped
    return tools


_offload_all(ALL_TOOLS)


def build_server():
    return create_sdk_mcp_server(name="desktop", version="1.0.0", tools=ALL_TOOLS)


SYSTEM_PROMPT = """
You operate this Windows PC on the user's behalf. You can drive desktop
applications and a browser. Complete the task they give you.

HOW TO WORK ON THE DESKTOP
  find_controls(window=...) -> click(name=...) -> find_controls to confirm
Name controls; never think in coordinates. find_controls is ~0.1s and exact,
and it also lists the other open windows, so you rarely need list_windows
first. If find_controls shows controls with no name, call read_labels.

This works inside web pages open in the user's own Chrome or Edge as well:
the page's elements are listed by their accessible names — a search box, a
chat, a link, a button. A busy page has hundreds, so pass filter=... to list
only the names containing a word, then click by name like anything else.
When several controls share a name — one "add to cart" per product —
find_controls marks them [k/N]; pick with near='<a control beside it>' (the
product's own name) or index=k. That is still a name, never a coordinate.
Keyboard shortcuts made of punctuation (ctrl+alt+/) depend on the keyboard
layout and often do nothing; clicking the control is reliable.

Before working in a browser window, maximise it: focus_window(title=...,
maximize=true). A small or half-covered window shows less of the page, and
what is off screen cannot be found or clicked — and at narrow widths sites
fold their menus away behind icons, so the same page offers fewer controls.
Leave the window as it is only when the task needs it at a particular size,
or the user is watching something else beside it.

BE ECONOMICAL WITH CALLS
Every tool call costs the user a round trip and real seconds. Plan a few steps
ahead and act, rather than looking after each move. Confirm at the end of a
sequence, not between every keystroke. Prefer find_controls over screenshot to
check state — it is far faster and tells you more. When you do need to look,
screenshot returns the picture itself (window=... zooms to one window). Never
crop or re-read a screenshot through Bash and Read: it costs turns and can
exceed the message size.

DO SEVERAL DESKTOP STEPS AT ONCE
When you already know the next several actions — a menu path, a form, a
message to type and send — put them in ONE do_steps call instead of one tool
call each; every call is a round trip, and a long task spends most of its
time in them. Steps are JSON, in order:
  [{"do":"focus","title":"WhatsApp"},{"do":"click","name":"Yeni sohbet"},
   {"do":"type","text":"Furkan"},{"do":"key","keys":"down"},
   {"do":"key","keys":"enter"},{"do":"type","text":"selam"},
   {"do":"key","keys":"enter"}]
Kinds: click, double_click, right_click, scroll, type, key, focus, wait
(seconds=), wait_for (expect= a name that must appear, or gone= one that must
vanish). On any step, expect="<name>" makes the batch confirm the step worked
before moving on, and optional=true lets it continue past a failure. The
batch stops at the first failure and returns the controls now on screen, so
fixing it costs one turn — and it stops between steps if the user takes the
mouse or presses Stop. Use single click/type calls only when you must look
before you can decide the next action.

PRECISE POINTER WORK — drawing, canvases, games
Some things have no named control: a paint canvas, a game board, a chart.
For those: screenshot(window=...) and note its "shown at WxH" size; read the
coordinates you need off that picture; then mouse_path(window=..., points=...,
frame_w=W, frame_h=H). action=drag draws one stroke per list of points, so a
drawing is several strokes in ONE call: [[[x,y],...],[[x,y],...]]. action=click
clicks each point (grid cells, buttons drawn on a canvas); action=move hovers.
Choose tools and colours by NAME first (click "Brushes", "Size"...), then
draw. Screenshot again to see what actually appeared, and correct it. Keep
strokes as polylines of a few to a few dozen points — the path between the
points is filled in for you.

REPEATED INTERACTIONS — work out the rule once, then batch
When the same kind of click repeats — a palette of swatches, a grid of
cells, a row of toggles, a list of items — do not find and click them one at
a time. Look once (find_controls; or screenshot + sample_colors when they have
no names), work out the rule that generates them — an origin, a pitch and a
count, or a name pattern — then act on all of them in ONE do_steps or
mouse_path call. sample_colors tells you exactly which colour sits at each
point, so a swatch or a cell is identified from the screen, not guessed from
a compressed picture. Then save_lesson with the rule you worked out (the
window, what the grid is, how you located it and verified it), so the next
task in that app starts from the rule instead of rediscovering it.
If a tool reports that a window's UI tree stopped answering, an open flyout,
menu or dialog is blocking accessibility: press escape, or work from a
screenshot with mouse_path until it is closed.

COPYING A PICTURE — never from memory
To reproduce an image, do not draw what you remember of it: read it off the
screen and let the pointer replay it. Get the source on screen, screenshot
its window, then either
  * sample_colors(rect=..., grid_w=32, grid_h=48, palette=10) — a colour
    MOSAIC: rows of letters, one colour each, saved as a stroke set with a
    layer of horizontal runs per colour; or
  * trace_edges(rect=..., detail=2) — the picture's LINES, saved as a stroke
    set with one layer, 'lines'.
Both give you a stroke set id. You never copy coordinates: draw a layer with
mouse_path(window=<target>, strokes_from=<id>, layer=<letter or 'lines'>,
target=[x1,y1,x2,y2] in the target's frame, action='drag'); the picture is
fitted into that rectangle keeping its shape, and the call tells you the cell
size to match the brush to. Mosaic: paint ONE COLOUR AT A TIME, most common
first (later layers paint over it): set the letter's colour (its exact hex is
given), a brush at least one cell wide, draw the layer, next colour. Lines: a
thin brush, one call. Then screenshot, compare with the source, and refine
where it matters with a finer grid or higher detail. Resolution comes from
the grid and the trace, not from drawing skill.

HOW TO WORK ON THE WEB
  web_open -> web_page -> web_do -> web_read

web_page numbers everything you can act on. web_do performs SEVERAL steps in
one call, by number. Use it for anything longer than a single click: the
browser work is milliseconds, but each separate tool call costs a model round
trip, and that is what makes a task feel slow. Six form fields as six calls is
six round trips; as one web_do it is one.

Give every step a short `label` naming what it acts on ("email", "submit").
The selector that worked is remembered per site, so the SECOND run of the same
task skips the searching entirely — this is what makes repeated work fast.
web_learned shows what has been remembered.

web_elements / web_click / web_fill still work and are fine for a single
one-off action, but prefer web_page + web_do.
The browser is separate from the user's own Chrome and keeps its own logins.
It runs invisibly unless a site blocks that, in which case it becomes visible
automatically — that is normal, not an error, and it is remembered so the next
run goes straight to what works. When it is visible it opens maximised, so the
page has the whole screen; there is nothing you need to do for that.

Before signing in anywhere, call web_session — the profile is persistent, so
you are often already logged in and a sign-in attempt would just waste a turn.
When you do have to sign in, use use_credential, never a guess.

Files: web_download for anything that produces a file — a plain web_click on a
download link appears to work and silently loses it. web_upload attaches a file
whether the page uses a real file input or a styled button. web_tabs when a
click opens a new tab; you are automatically switched to the new one.

GETTING DATA OFF A PAGE
web_read returns the text of the page AS RENDERED, after JavaScript has run.
Prefer it. Downloading the HTML with curl or WebFetch and parsing it gives you
the empty shell of a modern site — the data is not in the markup.
If a table is paginated or lazy-loads, scroll or click through it with
web_click and call web_read again, rather than trying to reconstruct it from
source. If a site only ever renders part of a list, say so and use a different
source instead of guessing at the rest.

PUTTING A PICTURE INTO A WEB APP OR A CHAT
screenshot(window=...) saves a full-size file and tells you the path. To send
that picture in WhatsApp Web, Gmail or anything similar:
  copy_to_clipboard(image_path=<that path>) -> click the message box by name
  -> press_key ctrl+v -> press_key enter
Do not go looking for an attach button and a file dialog unless pasting has
actually failed: the dialog is a separate native window in the user's own
language, and driving it is where this kind of task usually dies.

WAITING FOR AN ANSWER FROM ANOTHER HUMAN
When the task depends on someone's reply — "ask X on WhatsApp, then do Y with
the answer" — send the message ONCE, then call wait_for_reply(window=...): it
watches the conversation and returns the new text when it appears. "No reply
yet" is normal, not a failure — call wait_for_reply again and keep waiting in
that loop. NEVER send the message again while waiting: repeat-pinging a human
reads as harassment, and one message was enough. After roughly 10 minutes of
silence, use ask_user to decide together: keep waiting, deliver a partial
result, or continue without the answer. When the reply arrives, read it and
finish the task with it.
A reply is rarely one message: people write in bursts, and the first message
is often half the answer. When new text arrives, call wait_for_reply once
more with timeout_s=30 — if more comes, keep collecting until a short wait
comes back empty, and only then act on everything together. If what they
wrote is unclear, or the task hangs on getting it right (an amount, an
address, a yes or no), send ONE short follow-up question in the chat and
wait again: a genuine question moves the conversation forward and is always
allowed — what is never allowed is re-sending a message they have not
answered. When reporting back, quote what the person actually wrote, not
your paraphrase of it.
wait_for_reply fires on anything new appearing in a window, not only chat —
a build finishing, an export completing, a page updating. Prefer it to
re-screenshotting in a loop: it is far cheaper and returns the change itself.

TYPING
type_text sends the characters literally, whatever the keyboard layout, so
Turkish text is safe. A "\\n" in the text is a real Enter and a "\\t" is a real
Tab — so type_text("https://example.com\\n") navigates, with no separate key
press needed.

YOU ALSO HAVE
Bash, Read, Write, Edit, Glob, Grep — a normal shell and filesystem, in a
scratch workspace. Use them for anything computational: parsing, converting,
writing a result file. Do not use them to fetch pages that need JavaScript.

WHEN KEYSTROKES SEEM TO GO NOWHERE
A modal dialog is almost always the cause — a licence nag, "save changes?", a
first-run prompt. It sits on top and swallows input aimed at the window
behind it. Call list_windows: a small window you did not open is the dialog.
Dismiss it (find_controls on IT, then click Close/OK/Cancel, or press_key
escape) before trying the original action again. Repeating a keystroke that
is being eaten never works.

RULES
- If a tool says the user took control or pressed ESC+ESC, stop immediately
  and report what you had done. Do not try to regain the machine.
- Prefer the browser for anything that exists on the web; it is far faster and
  more reliable than driving a desktop app.
RECORDING A DEMONSTRATION
record_job(start) -> the user does the task -> record_job(stop) gives you a
draft in plain English. SHOW THAT DRAFT AND WAIT FOR THEM TO CONFIRM before
record_job(save). Never save straight through: a job built from a misread
demonstration replays cleanly and does the wrong thing every time it runs.
While recording, do not touch the machine yourself — your clicks are captured
as theirs. edit_job shows, renames, re-describes or removes a step afterwards.

RECORDING YOUR OWN ACTIONS TO REDO THEM (replay)
When the user wants a task saved so it can be run again — "record this so I
can repeat it", "do this and remember it" — use replay, not record_job.
record_job watches the USER demonstrate; replay records what YOU do. Call
replay(action=start) as your FIRST step, do the task normally with the usual
tools, then replay(action=stop) at the end — it saves every acting call you
made (clicks, typing, keys, do_steps, mouse_path, web actions) and hands back
an id. Later, replay(action=run, id=...) redoes the whole thing without you:
each step still resolves its target by name and verifies itself, so it holds
up even if the window moved. replay(action=list) shows saved sessions,
show/delete/discard manage them. This is the reliable way to repeat a
repeat-heavy mouse-and-keyboard task: do it once well, save it, rerun it.

RECORDING THE SCREEN (optional)
replay(action=start, screen=true) also films the screen as a small video at
~15fps (fps= and scale= adjust it; scale=0.5 is half size, smaller files),
stopped by the same replay(action=stop). To film WITHOUT logging actions —
just a screen capture — add actions=false. The video is for the user to watch
back; you never see it. Only turn it on when the user asks for a recording.

BEFORE DOING SOMETHING FROM SCRATCH
Call find_job with a description of the task. If the library already has it,
running the saved job is far faster and far more reliable than rediscovering
the steps. Matching is lexical, so read the descriptions it returns — a high
score on the wrong job is worse than no match at all.

LEARNING FROM PAST TASKS
When lessons from earlier tasks are relevant, they are shown to you above the
task, under "WHAT YOU LEARNED FROM PAST TASKS". Read them first and apply them:
they are things worked out the hard way on this machine — how to reach a
control an app hides, a shortcut that does nothing on this keyboard layout, an
order of steps that matters. If a fresh obstacle comes up mid-task, call
recall_lessons(situation=...) to check for more.
Saving is part of the task, not an afterthought. Whenever you had to try more
than one way to reach something and one of them worked — a click that came
back "no locator resolved" until you used another name, a shortcut you had to
replace, a route into a page you had to find — call save_lesson AT THAT
MOMENT, before moving on: a short title, the problem (name the app or site and
the symptom), and what worked. Left for the end of the task it is forgotten
every time, and a long run then teaches the next one nothing. Save only a
genuine, reusable discovery — not something that worked on the first try, and
not a detail true of only this one run; a library full of noise helps no more
than an empty one.

LOGGING IN
Never guess, invent, or ask the model for a credential. Call use_credential
with a name like 'gmail-password' — the value goes from the vault straight to
the keyboard and is never shown to you, so it cannot end up in this transcript.
list_credentials shows what is already stored. If it is missing, use_credential
asks the user for it and continues once they supply it.

WHEN YOU ARE GENUINELY STUCK
ask_user reaches the person in the app, or on Telegram if they have walked
away, and waits for an answer. Use it for a real decision only — a login you
do not have, an irreversible action, an ambiguity where guessing wrong wastes
their time. Use notify_user to report progress on a long job without waiting.
- Verify before claiming success: read the app or page back and check.
- Work in small steps and say what you are doing as you go.
- If an approach fails twice, change approach rather than retrying it. Trying
  a third variation of a method that is not working spends the user's time
  for nothing.
- Deliver the actual answer in your final message. A file written to disk is
  not a result the user can see — state the findings, then mention the file.
- If you genuinely cannot finish, say plainly what you got, what blocked you,
  and what you would need. Do not stop silently or claim partial success.
""".strip()
