"""Tiered element resolution.

Turns a locator stack into a concrete, live element. Tiers run cheapest and
most stable first; each is measured, so the cost of falling through is known:

    win32       0.002s   classic controls, window geometry
    uia         0.52s    default path — native, WPF, WinUI, Electron, webview
    ocr        13.8s     labels elements UIA located but did not name
    vision     84.2s     games, canvas, remote desktop only
    container   0.52s    empty space: canvas interiors, drag targets

Nothing here stores or returns a coordinate that outlives the call. Callers
get an Element with a live UIA handle, which the actuator prefers over pixels.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterator

import uiautomation as auto

from .schema import Locator, LocatorType

user32 = ctypes.windll.user32
DWMWA_CLOAKED = 14


# ---------------------------------------------------------------------------
# Element
# ---------------------------------------------------------------------------

@dataclass
class Element:
    """A resolved, live UI element.

    `control` is the UIA handle when we have one — the actuator needs it to
    Invoke without touching the cursor. `rect` is only a fallback for elements
    that expose no invocable pattern.
    """
    rect: tuple[int, int, int, int]          # x1, y1, x2, y2 (screen px)
    name: str = ""
    control_type: str = ""
    automation_id: str = ""
    hwnd: int = 0
    source: str = ""                          # which tier produced this
    control: Any = None                       # uiautomation control, if any
    confidence: float = 1.0

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.rect
        return ((x1 + x2) // 2, (y1 + y2) // 2)

    @property
    def size(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.rect
        return (x2 - x1, y2 - y1)

    def click_point(self) -> tuple[int, int]:
        """Prefer the control's own clickable point over the naive centre.

        A wide label, an L-shaped control, or a box overlapping a child can all
        put the geometric centre somewhere unclickable.
        """
        if self.control is not None:
            try:
                pt = self.control.GetClickablePoint()
                if pt and pt[2]:
                    return (int(pt[0]), int(pt[1]))
            except Exception:
                pass
        return self.center


class ResolveError(Exception):
    """No locator in the stack resolved."""


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

def is_cloaked(hwnd: int) -> bool:
    """DWM-cloaked windows report visible but are NOT rendered.

    Filtering on this moved measured UIA coverage from 36% to 100% — without
    it, suspended UWP apps and background shell surfaces look like failures.
    """
    val = ctypes.c_int(0)
    try:
        ctypes.windll.dwmapi.DwmGetWindowAttribute(
            ctypes.wintypes.HWND(hwnd), DWMWA_CLOAKED,
            ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        return False
    return val.value != 0


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    r = ctypes.wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def window_title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if not n:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def list_windows(include_cloaked: bool = False,
                 min_size: tuple[int, int] = (120, 80)) -> list[dict]:
    """Rendered top-level windows, newest-focus first."""
    out: list[dict] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = window_title(hwnd)
        if not title:
            return True
        if not include_cloaked and is_cloaked(hwnd):
            return True
        x1, y1, x2, y2 = window_rect(hwnd)
        if (x2 - x1) < min_size[0] or (y2 - y1) < min_size[1]:
            return True
        out.append({"hwnd": hwnd, "title": title, "rect": (x1, y1, x2, y2)})
        return True

    user32.EnumWindows(cb, None)
    return out


def foreground_window() -> int:
    return user32.GetForegroundWindow()


def find_window(title_contains: str | None = None,
                exe: str | None = None) -> int | None:
    for w in list_windows():
        if title_contains and title_contains.lower() in w["title"].lower():
            return w["hwnd"]
    if exe:
        import psutil
        for w in list_windows():
            pid = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(w["hwnd"], ctypes.byref(pid))
            try:
                if psutil.Process(pid.value).name().lower() == exe.lower():
                    return w["hwnd"]
            except Exception:
                continue
    return None


# ---------------------------------------------------------------------------
# UIA helpers
# ---------------------------------------------------------------------------

_root_cache: dict[int, tuple[Any, float]] = {}

#: How long a resolved window root stays usable. Rebuilding the root on every
#: lookup meant a fresh COM handle per call for no benefit — the handle stays
#: valid while the window lives. Short enough that a replaced window is picked
#: up quickly, long enough that a burst of lookups pays for it once.
ROOT_TTL_S = 2.0


class WindowGone(Exception):
    """The window stopped existing while we were looking at it."""


def uia_root(hwnd: int | None = None):
    """Cached UIA control for a window handle (or the desktop).

    A window that closes mid-inspection makes UI Automation raise a COM error
    out of the depths of comtypes — "an event was unable to invoke any of the
    subscribers", which says nothing useful and, unhandled, takes the whole
    process down. It happened for real: a test suite crashed outright because
    a fixture exited a moment before the tree was walked. An app closing while
    the agent reads it is a normal event, not a fatal one, so it is turned into
    something a caller can act on.
    """
    if hwnd is None:
        try:
            return auto.GetRootControl()
        except Exception as e:
            raise WindowGone(f"no desktop root available: {type(e).__name__}") from e
    hit = _root_cache.get(hwnd)
    if hit is not None and (time.time() - hit[1]) < ROOT_TTL_S:
        return hit[0]
    try:
        ctrl = auto.ControlFromHandle(hwnd)
    except Exception as e:
        _root_cache.pop(hwnd, None)
        raise WindowGone(
            f"window {hwnd} could not be inspected — it has probably closed "
            f"({type(e).__name__})") from e
    if ctrl is None:
        raise WindowGone(f"window {hwnd} no longer exists")
    _root_cache[hwnd] = (ctrl, time.time())
    return ctrl


def invalidate_cache(hwnd: int | None = None) -> None:
    # The OCR label cache goes too. A browser tab keeps the same hwnd across
    # navigations, so labels read from one page were being returned for every
    # page after it — and a click "by label" then hit-tested a rectangle from
    # a page that is no longer on screen.
    if hwnd is None:
        _root_cache.clear()
        _label_cache.clear()
    else:
        _root_cache.pop(hwnd, None)
        _label_cache.pop(hwnd, None)


def _rect_of(ctrl) -> tuple[int, int, int, int] | None:
    try:
        r = ctrl.BoundingRectangle
        if r and r.width() > 0 and r.height() > 0:
            return (r.left, r.top, r.right, r.bottom)
    except Exception:
        pass
    return None


def _to_element(ctrl, source: str, hwnd: int = 0) -> Element | None:
    rect = _rect_of(ctrl)
    if rect is None:
        return None
    try:
        name = ctrl.Name or ""
        ctype = ctrl.ControlTypeName or ""
        aid = ctrl.AutomationId or ""
    except Exception:
        name = ctype = aid = ""
    return Element(rect=rect, name=name, control_type=ctype, automation_id=aid,
                   hwnd=hwnd, source=source, control=ctrl)


# Web content in Chrome/Edge sits 20-30 levels below the window: a depth
# cap of 14 stopped at the browser's toolbar and reported a page as empty.
# The node and time budgets are what bound the walk, not the depth.
WALK_DEPTH = 40


def walk(ctrl, max_depth: int = WALK_DEPTH, max_nodes: int = 2500,
         budget_s: float = 4.0, stats: dict | None = None) -> Iterator[Any]:
    """Breadth-first walk of a UIA subtree under explicit budgets.

    Breadth-first, deliberately. Every caller is on a latency budget, so a
    busy window WILL be truncated — and what goes unvisited must then be the
    deepest nodes, not whole branches of shallow ones. Depth-first spent the
    entire node budget inside a web page's document and never came back for
    the browser's own toolbar, so raising the depth limit to reach page
    content had the side effect of hiding controls that used to be found.
    Breadth-first cannot do that: everything at depth N is visited before
    anything at depth N+1.

    Budget exhaustion ends the walk; depth only prunes THIS branch.

    Truncation is deliberate, but it must not be silent — a caller that says
    "not found" after running out of budget is telling the model something
    false. Pass `stats` to find out:
        {"nodes": int, "truncated": bool, "reason": "nodes"|"time"|""}
    It is filled when the walk finishes on its own; a caller that stops early
    knows it stopped, so it does not need telling.
    """
    t0 = time.time()
    seen = 0
    reason = ""
    queue = deque([(ctrl, 0)])
    while queue and not reason:
        node, depth = queue.popleft()
        if depth >= max_depth:
            continue
        try:
            children = node.GetChildren()
        except Exception:
            continue
        for ch in children:
            if seen >= max_nodes:
                reason = "nodes"
                break
            if time.time() - t0 > budget_s:
                reason = "time"
                break
            seen += 1
            yield ch
            queue.append((ch, depth + 1))
    if stats is not None:
        stats["nodes"] = seen
        stats["truncated"] = bool(reason)
        stats["reason"] = reason


# ---------------------------------------------------------------------------
# Tier: win32
# ---------------------------------------------------------------------------

def _resolve_win32(value: dict, scope_hwnd: int | None) -> Element | None:
    cls = value.get("class")
    idx = value.get("index", 0)
    parent = scope_hwnd or foreground_window()
    matches: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _l):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        if cls is None or buf.value == cls:
            matches.append(hwnd)
        return True

    user32.EnumChildWindows(parent, cb, None)
    if idx < len(matches):
        h = matches[idx]
        return Element(rect=window_rect(h), hwnd=h, source="win32",
                       control_type=cls or "")
    return None


# ---------------------------------------------------------------------------
# Tier: uia
# ---------------------------------------------------------------------------

#: Host windows that own a frame but not the content inside it. Scoping a
#: search to one of these finds a title bar and nothing else — the app's real
#: tree lives in another process and is not reachable as a child of the frame.
HOST_FRAME_CLASSES = {"ApplicationFrameWindow", "Windows.UI.Core.CoreWindow"}


def window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def is_host_frame(hwnd: int) -> bool:
    return window_class(hwnd) in HOST_FRAME_CLASSES


#: Shortest name accepted as a truncated prefix. find_controls shows names cut
#: for display, so a caller may hold only the start of a long one; a short
#: prefix is refused because "Search" must never resolve to "Search or start
#: a new chat" by accident.
PREFIX_MIN = 12
PREFIX_LOOSE = 24


def norm_name(s: str) -> str:
    """A name reduced to what a human would call the same name.

    Web pages pad accessible names with non-breaking spaces and stray runs of
    whitespace (" Sepete Ekle "), and the case a page renders is not always
    the case it exposes. find_controls prints the tidied name, so an exact,
    case-sensitive compare meant a control that had just been listed could not
    be clicked: "Could not click 'X': no locator resolved", with X visibly
    there in the previous tool result.
    """
    return " ".join((s or "").replace("\xa0", " ").split()).casefold()


def name_is_long_prefix(actual: str, wanted: str) -> bool:
    """Is `wanted` a truncated form of `actual`, long enough to be unambiguous?

    Two regimes. At PREFIX_LOOSE (24) and above a prefix is specific enough
    to accept as-is — that is what a name cut short by a display cap looks
    like, and the cut can land mid-word. Down to PREFIX_MIN (12) it is
    accepted only when it ends on a word boundary of the full name: a
    WhatsApp chat row is named 'Furkan Fizix 15:30 <message preview>', the
    preview changes under the model between listing and clicking, and the
    stable head anyone naturally asks for is the contact's name — far
    shorter than 24 ('Furkan Fizix' is 12 and could not be clicked, run of
    2026-09-08). 'furkan f' would be a guess and stays refused. Resolution
    still requires the prefix to hit exactly ONE control, so a short prefix
    that is genuinely ambiguous fails the uniqueness gate, not this one.
    """
    a, w = norm_name(actual), norm_name(wanted)
    if len(a) <= len(w) or not a.startswith(w):
        return False
    if len(w) >= PREFIX_LOOSE:
        return True
    return len(w) >= PREFIX_MIN and a[len(w)] == " "


def _is_long_prefix(ch, wanted: str) -> bool:
    try:
        return name_is_long_prefix(ch.Name or "", wanted)
    except Exception:
        return False


def _match(ch, want_id, want_name, want_type) -> bool:
    try:
        if want_id and (ch.AutomationId or "") != want_id:
            return False
        if want_name and norm_name(ch.Name) != norm_name(want_name):
            return False
        if want_type and (ch.ControlTypeName or "") != want_type:
            return False
    except Exception:
        return False
    return True


def _resolve_uia(ltype: LocatorType, value: dict,
                 scope_hwnd: int | None) -> Element | None:
    try:
        root = uia_root(scope_hwnd)
    except WindowGone:
        # The window went away between choosing it and looking in it. That is
        # a miss for this locator, not a failure of the whole resolution.
        return None
    if root is None:
        return None

    if ltype == LocatorType.UIA_PATH:
        return _resolve_uia_path(root, value.get("path", ""), scope_hwnd)

    want_id = value.get("id") or value.get("automation_id")
    want_name = value.get("name")
    want_type = value.get("control")

    # Several controls sharing a name is the normal case on a shop page —
    # one "add to cart" per product. Picking whichever the walk reached first
    # sent the model to pixel coordinates instead; an ordinal or a neighbour
    # keeps the choice in words.
    if want_name and (value.get("index") or value.get("near")):
        return _resolve_among(root, want_id, want_name, want_type,
                              value.get("index"), value.get("near"),
                              ltype, scope_hwnd)

    # An exact name wins. A long name that only matches as a prefix is kept
    # as a fallback for names find_controls had to truncate for display.
    prefix_hits = []
    for ch in walk(root):
        if _match(ch, want_id, want_name, want_type):
            el = _to_element(ch, f"uia:{ltype.value}", scope_hwnd or 0)
            if el:
                return el
        elif (want_name and len(prefix_hits) < 2
              and _match(ch, want_id, None, want_type)
              and _is_long_prefix(ch, want_name)):
            prefix_hits.append(ch)
    # Only when it is the single candidate. Two controls sharing a long prefix
    # is the normal case on a shop page — colour and size variants of one
    # product — and picking whichever the walk reached first would click the
    # wrong one silently. Failing lets the caller pass a longer name.
    if len(prefix_hits) == 1:
        el = _to_element(prefix_hits[0], f"uia:{ltype.value}", scope_hwnd or 0)
        if el:
            return el

    # Scoped search found nothing. If we were scoped to a host frame, its tree
    # is a title bar and the real content is elsewhere — widen to the desktop
    # rather than reporting a false negative.
    if scope_hwnd and is_host_frame(scope_hwnd):
        # Bounded tightly: this is a recovery path, not a search strategy.
        # An unbounded desktop walk can take tens of seconds on a busy machine.
        desk = auto.GetRootControl()
        for ch in walk(desk, max_depth=12, max_nodes=1200, budget_s=1.5):
            if _match(ch, want_id, want_name, want_type):
                el = _to_element(ch, f"uia:{ltype.value}:desktop", scope_hwnd)
                if el:
                    return el
    return None


def _resolve_among(root, want_id, want_name, want_type, index, near,
                   ltype: LocatorType, scope_hwnd: int | None) -> Element | None:
    """One of several same-named controls: the k-th in tree order, or the one
    nearest another named control.

    `near` beats `index` when both are given — an ordinal breaks the moment a
    page reflows, a neighbour ("the + beside 'Pınar Beyaz'") does not. Raises
    ResolveError with the reason when the choice cannot be made, so the model
    is told "only 5 of those" rather than "no locator resolved".
    """
    hits, anchors = [], []
    near_n = norm_name(near) if near else ""
    for ch in walk(root):
        if _match(ch, want_id, want_name, want_type):
            hits.append(ch)
        elif near_n:
            try:
                nm = norm_name(ch.Name)
            except Exception:
                continue
            if nm and (nm == near_n or near_n in nm):
                anchors.append(ch)
    if not hits:
        return None
    if near_n:
        pts = [r for r in (_rect_of(a) for a in anchors) if r]
        if not pts:
            raise ResolveError(f"nothing named like {near!r} to measure from — "
                               f"pick a name find_controls listed")
        best, best_d = None, None
        for ch in hits:
            r = _rect_of(ch)
            if not r:
                continue
            hx, hy = (r[0] + r[2]) // 2, (r[1] + r[3]) // 2
            for p in pts:
                px, py = (p[0] + p[2]) // 2, (p[1] + p[3]) // 2
                d = (hx - px) ** 2 + (hy - py) ** 2
                if best_d is None or d < best_d:
                    best, best_d = ch, d
        return _to_element(best, f"uia:{ltype.value}:near", scope_hwnd or 0) if best else None
    try:
        k = int(index)
    except (TypeError, ValueError):
        raise ResolveError(f"index must be a number, not {index!r}")
    if k < 1 or k > len(hits):
        raise ResolveError(f"only {len(hits)} control(s) named {want_name!r}; "
                           f"index {k} is out of range")
    return _to_element(hits[k - 1], f"uia:{ltype.value}#{k}", scope_hwnd or 0)


def _is_frame_only(root) -> bool:
    """True when a window exposes a title bar and nothing else.

    Store/UWP apps hosted by ApplicationFrameHost present this shape: the
    frame publishes TitleBar / Minimize / Maximize / Close and stops. Callers
    need to distinguish 'element absent' from 'this window hides its content'.
    """
    try:
        ids = set()
        for ch in walk(root, max_depth=4, max_nodes=40, budget_s=1.0):
            aid = ch.AutomationId or ""
            if aid:
                ids.add(aid)
        return bool(ids) and ids <= {"TitleBar", "SystemMenuBar", "Minimize",
                                     "Maximize", "Close", "Restore"}
    except Exception:
        return False


def _ocr_window(hwnd: int, scale: float = 1.0
                ) -> list[tuple[tuple[int, int, int, int], str, float]]:
    """Run OCR once over a window's pixels. Returns [(rect, text, conf), ...].

    Cropping to the window instead of the full desktop is what makes this
    affordable: the full-screen pass measured 13.8s, a window crop is a small
    fraction of that because OCR cost scales with area.

    `scale` upsamples before reading. Measured: at 1.0 every normal control is
    read but 20x20 buttons are missed entirely; upsampling recovers small text
    at roughly scale^2 the time, so it is a deliberate opt-in rather than the
    default.
    """
    import numpy as np
    from PIL import Image
    import mss

    from . import display

    # A slept display captures as black and OCR then reports "no text" with no
    # error — the same silent-wrong-answer that cost three benchmark runs.
    display.assert_usable()

    import sys as _sys
    from pathlib import Path as _Path
    _root = str(_Path(__file__).resolve().parents[2])
    if _root not in _sys.path:
        _sys.path.append(_root)   # append: repo root must not shadow agent/
    import ui_parser

    if ui_parser._models.get("easyocr") is None:
        ui_parser.load_models()

    x1, y1, x2, y2 = window_rect(hwnd)
    x1, y1 = max(x1, 0), max(y1, 0)
    with mss.mss() as sct:
        shot = sct.grab({"left": x1, "top": y1,
                         "width": max(x2 - x1, 1), "height": max(y2 - y1, 1)})
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    if display.lit_fraction(img) < 0.005:
        raise display.DisplayAsleep(
            f"window {hwnd} captured blank — it may be occluded, minimised, "
            "or the display is off")

    if scale != 1.0:
        img = img.resize((int(img.width * scale), int(img.height * scale)),
                         Image.LANCZOS)

    out = []
    for bbox, text, conf in ui_parser._models["easyocr"].readtext(np.array(img)):
        xs = [int(p[0] / scale) for p in bbox]
        ys = [int(p[1] / scale) for p in bbox]
        # back to screen coordinates
        out.append(((min(xs) + x1, min(ys) + y1, max(xs) + x1, max(ys) + y1),
                    (text or "").strip(), float(conf)))
    return out


def _overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """Fraction of `a` covered by `b`."""
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    area = max((a[2] - a[0]) * (a[3] - a[1]), 1)
    return inter / area


#: hwnd -> {rect: label}, so the OCR pass is paid once per window
_label_cache: dict[int, dict[tuple, str]] = {}


def label_elements(hwnd: int, force: bool = False,
                   min_conf: float = 0.35,
                   scale: float = 1.0) -> dict[tuple, str]:
    """Give names to UIA elements that have none.

    Measured need: a Tk window publishes 29 ButtonControls with 3 names, and
    Teams publishes 64 elements with 9. UIA locates them perfectly and cannot
    say what they are. One OCR pass over the window fills that in by matching
    text boxes to element rectangles.

    Returns {element_rect: label} and caches per window.
    """
    if not force and hwnd in _label_cache:
        return _label_cache[hwnd]

    from . import display

    prev = globals().get("ENABLE_OCR")
    try:
        globals()["ENABLE_OCR"] = True
        texts = _ocr_window(hwnd, scale=scale)
    except display.DisplayAsleep:
        raise               # a blank screen is an error, never "no labels"
    except Exception:
        return {}
    finally:
        globals()["ENABLE_OCR"] = prev

    try:
        root = uia_root(hwnd)
    except WindowGone:
        return {}
    if root is None:
        return {}

    # Collect candidate elements once.
    elements: list[tuple[tuple, int]] = []     # (rect, area)
    # WALK_DEPTH, not 14: web page elements sit far below a browser window,
    # so the shallow walk collected only chrome and every OCR'd word on the
    # page found nothing to attach to.
    for ch in walk(root, max_depth=WALK_DEPTH, max_nodes=1200, budget_s=4.0):
        rect = _rect_of(ch)
        if rect is None:
            continue
        try:
            if (ch.Name or "").strip():
                continue                       # already named; leave it alone
        except Exception:
            continue
        area = (rect[2] - rect[0]) * (rect[3] - rect[1])
        if area <= 0:
            continue
        elements.append((rect, area))

    # Assign per TEXT, not per element.
    #
    # The obvious loop — "for each element, take the text most inside it" —
    # is wrong: an element that contains no text of its own still picks up
    # whichever box scores highest, so a stray icon can claim a label that
    # belongs to a real button elsewhere. Driving from the text instead means
    # every label goes to the control that actually draws it, and the TIGHTEST
    # containing element wins, because text sits inside the button, which sits
    # inside a pane, which sits inside the window.
    labels: dict[tuple, tuple[str, float]] = {}
    for trect, text, conf in texts:
        if conf < min_conf or not text.strip():
            continue

        best_rect, best_area, best_cov = None, None, 0.0
        for rect, area in elements:
            cov = _overlap(trect, rect)        # how much of the text it holds
            if cov < 0.8:                      # must essentially contain it
                continue
            if best_rect is None or area < best_area:
                best_rect, best_area, best_cov = rect, area, cov

        if best_rect is None:
            continue

        # Text must be a plausible fraction of its control: a label filling
        # 0.2% of a huge pane is that pane's child's label, not the pane's.
        text_area = max((trect[2] - trect[0]) * (trect[3] - trect[1]), 1)
        if text_area / best_area < 0.02:
            continue

        prev = labels.get(best_rect)
        if prev is None or best_cov > prev[1]:
            labels[best_rect] = (text.strip(), best_cov)

    _label_cache[hwnd] = labels
    return labels


def find_labelled(hwnd: int, text: str, force: bool = False) -> Element | None:
    """Resolve an unnamed control by the text drawn inside it.

    The bridge for apps that publish geometry without identity.

    A label can match several elements — text sits inside a button, which sits
    inside a pane, and a stray OCR read can land on an unrelated glyph. So
    candidates are ranked rather than taking whichever the dict yields first,
    which previously returned a 26x26 icon for 'Save'.
    """
    want = text.strip().lower()
    labels = label_elements(hwnd, force=force)

    candidates = []
    for rect, entry in labels.items():
        label, cov = entry if isinstance(entry, tuple) else (entry, 1.0)
        lab = label.strip().lower()
        if lab == want:
            score = 2.0 + cov
        elif want in lab:
            score = 1.0 + cov
        else:
            continue
        area = max((rect[2] - rect[0]) * (rect[3] - rect[1]), 1)
        candidates.append((score, area, rect, label))

    if not candidates:
        return None

    # Exact match first, then coverage, then the larger control — a text box
    # sits inside the button that owns it, so the bigger rect is the target.
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    _score, _area, rect, label = candidates[0]

    el = hit_test((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
    if el is not None:
        el.name = label
        el.source = "uia+ocr"
        return el
    return Element(rect=rect, name=label, source="uia+ocr", hwnd=hwnd)


def labels_for(hwnd: int) -> dict[tuple, str]:
    """Flattened {rect: label} view, for callers that only want the names."""
    return {r: (e[0] if isinstance(e, tuple) else e)
            for r, e in label_elements(hwnd).items()}


def diagnose(hwnd: int) -> dict:
    """Explain what a window exposes — used when resolution fails.

    Turns 'no locator resolved' into an actionable reason.
    """
    try:
        root = uia_root(hwnd)
    except WindowGone as e:
        # A diagnostic that raises is worse than useless: it is
        # asked precisely when something has already gone wrong.
        return {"error": str(e), "class": "", "nodes": 0,
                "frame_only": False}
    if root is None:
        return {"reachable": False, "reason": "no UIA control for this handle"}
    nodes = sum(1 for _ in walk(root, max_depth=6, max_nodes=300, budget_s=2.0))
    frame_only = _is_frame_only(root)
    return {
        "reachable": True,
        "class": window_class(hwnd),
        "cloaked": is_cloaked(hwnd),
        "nodes": nodes,
        "frame_only": frame_only,
        "reason": ("window exposes only a title bar — content is hosted in "
                   "another process (Store/UWP app); search desktop-wide"
                   if frame_only else "ok"),
    }


def _resolve_uia_path(root, path: str, scope_hwnd: int | None) -> Element | None:
    """Structural path like Window/Pane[0]/ToolBar/Button[2]."""
    if not path:
        return None
    node = root
    for part in path.split("/"):
        if not part:
            continue
        idx = 0
        if "[" in part and part.endswith("]"):
            part, rest = part.split("[", 1)
            try:
                idx = int(rest[:-1])
            except ValueError:
                idx = 0
        try:
            kids = [c for c in node.GetChildren()
                    if not part or (c.ControlTypeName or "").startswith(part)]
        except Exception:
            return None
        if idx >= len(kids):
            return None
        node = kids[idx]
    return _to_element(node, "uia:path", scope_hwnd or 0)


# ---------------------------------------------------------------------------
# Tier: relative
# ---------------------------------------------------------------------------

def _resolve_relative(value: dict, scope_hwnd: int | None) -> Element | None:
    anchor_spec = value.get("anchor", {})
    anchor = _resolve_uia(LocatorType.UIA_NAME_TYPE, anchor_spec, scope_hwnd)
    if anchor is None:
        return None
    ax, ay = anchor.center
    x = ax + int(value.get("dx", 0))
    y = ay + int(value.get("dy", 0))
    return hit_test(x, y) or Element(rect=(x - 1, y - 1, x + 1, y + 1),
                                     source="relative")


# ---------------------------------------------------------------------------
# Tier: normalized coordinate (last resort)
# ---------------------------------------------------------------------------

def _resolve_norm(value: dict, scope_hwnd: int | None) -> Element | None:
    of = value.get("of", "window")
    if of == "window":
        hwnd = scope_hwnd or foreground_window()
        x1, y1, x2, y2 = window_rect(hwnd)
    else:
        x1, y1 = 0, 0
        x2 = user32.GetSystemMetrics(0)
        y2 = user32.GetSystemMetrics(1)
    x = int(x1 + (x2 - x1) * float(value.get("x", 0.5)))
    y = int(y1 + (y2 - y1) * float(value.get("y", 0.5)))
    return Element(rect=(x - 2, y - 2, x + 2, y + 2), source="norm_coord",
                   confidence=0.3)


# ---------------------------------------------------------------------------
# Tier: OCR  (labels what UIA located but did not name)
# ---------------------------------------------------------------------------

#: OCR and vision are opt-in. They cost 14s and 84s respectively and the OCR
#: path will download ~1.2GB of models on first use — an implicit fall-through
#: into that is never acceptable, so callers must ask for it explicitly.
ENABLE_OCR = False
ENABLE_VISION = False


def _resolve_ocr(value: dict, scope_hwnd: int | None) -> Element | None:
    if not ENABLE_OCR:
        return None
    want = (value.get("text") or "").strip().lower()
    if not want:
        return None
    try:
        import sys
        from pathlib import Path
        _root = str(Path(__file__).resolve().parents[2])
        if _root not in sys.path:
            sys.path.append(_root)   # append: repo root must not shadow agent/
        import controller
        import ui_parser
        if ui_parser._models.get("easyocr") is None:
            ui_parser.load_models()
        import numpy as np
        img, _ = controller._capture_screen()
        reader = ui_parser._models["easyocr"]
        best, best_score = None, 0.0
        for bbox, text, conf in reader.readtext(np.array(img)):
            t = (text or "").strip().lower()
            if not t:
                continue
            score = conf if t == want else (conf * 0.7 if want in t else 0.0)
            if score > best_score:
                xs = [int(p[0]) for p in bbox]
                ys = [int(p[1]) for p in bbox]
                best = Element(rect=(min(xs), min(ys), max(xs), max(ys)),
                               name=text, source="ocr", confidence=float(conf))
                best_score = score
        return best
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Hit testing — pixel to element (used by the recorder)
# ---------------------------------------------------------------------------

def hit_test(x: int, y: int) -> Element | None:
    """Resolve a screen point to the UIA element under it.

    This is what turns a recorded click into a locator rather than a
    coordinate. Costs ~10ms.
    """
    try:
        ctrl = auto.ControlFromPoint(x, y)
    except Exception:
        return None
    if ctrl is None:
        return None
    hwnd = user32.WindowFromPoint(ctypes.wintypes.POINT(x, y))
    return _to_element(ctrl, "hit_test", hwnd or 0)


def describe(el: Element) -> list[Locator]:
    """Build a locator stack for an element, best-first.

    Called at capture time so a recorded click becomes portable identity.
    """
    out: list[Locator] = []
    if el.automation_id:
        out.append(Locator(LocatorType.UIA_AUTOMATION_ID,
                           {"id": el.automation_id, "control": el.control_type}))
    if el.name:
        out.append(Locator(LocatorType.UIA_NAME_TYPE,
                           {"name": el.name, "control": el.control_type}))
        out.append(Locator(LocatorType.OCR_TEXT, {"text": el.name}))
    if el.hwnd:
        wx1, wy1, wx2, wy2 = window_rect(el.hwnd)
        w, h = max(wx2 - wx1, 1), max(wy2 - wy1, 1)
        cx, cy = el.center
        out.append(Locator(LocatorType.NORM_COORD,
                           {"x": round((cx - wx1) / w, 4),
                            "y": round((cy - wy1) / h, 4), "of": "window"}))
    return out


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------

_DISPATCH = {
    LocatorType.WIN32_CLASS: lambda v, s: _resolve_win32(v, s),
    LocatorType.UIA_AUTOMATION_ID: lambda v, s: _resolve_uia(LocatorType.UIA_AUTOMATION_ID, v, s),
    LocatorType.UIA_NAME_TYPE: lambda v, s: _resolve_uia(LocatorType.UIA_NAME_TYPE, v, s),
    LocatorType.UIA_PATH: lambda v, s: _resolve_uia(LocatorType.UIA_PATH, v, s),
    LocatorType.RELATIVE: lambda v, s: _resolve_relative(v, s),
    LocatorType.OCR_TEXT: lambda v, s: _resolve_ocr(v, s),
    LocatorType.NORM_COORD: lambda v, s: _resolve_norm(v, s),
}


@dataclass
class ResolveResult:
    element: Element | None
    locator_index: int = -1
    tried: list[tuple[int, str, bool, float]] = field(default_factory=list)
    elapsed_s: float = 0.0
    #: Why the last locator that had something to say could not choose —
    #: "only 5 named that", not the generic miss.
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.element is not None


def resolve(locators: list[Locator], scope_hwnd: int | None = None,
            order: list[int] | None = None,
            allow_slow: bool = False) -> ResolveResult:
    """Walk the locator stack until one resolves.

    `order` lets the caller supply a statistics-driven ordering (from the
    local sidecar) without the shareable macro carrying usage data.

    `allow_slow` opts into the OCR and vision tiers for this call only. Off by
    default so a routine resolve can never silently cost 84 seconds.
    """
    global ENABLE_OCR, ENABLE_VISION
    t0 = time.time()
    result = ResolveResult(element=None)
    indices = order if order is not None else list(range(len(locators)))
    prev = (ENABLE_OCR, ENABLE_VISION)
    if allow_slow:
        ENABLE_OCR = ENABLE_VISION = True

    try:
        for i in indices:
            if i >= len(locators):
                continue
            loc = locators[i]
            fn = _DISPATCH.get(loc.type)
            if fn is None:
                continue
            t1 = time.time()
            try:
                el = fn(loc.value, scope_hwnd)
            except ResolveError as e:
                el = None
                result.reason = str(e)
            except Exception:
                el = None
            dt = time.time() - t1
            result.tried.append((i, loc.type.value, el is not None, round(dt, 4)))
            if el is not None:
                result.element = el
                result.locator_index = i
                break
    finally:
        ENABLE_OCR, ENABLE_VISION = prev

    result.elapsed_s = time.time() - t0
    return result
