"""Vision click grounding — the fallback for a target nothing can name.

Every click in this engine resolves a NAME to a live UI Automation element
(locator.py) and acts on the element, never on a coordinate. That is the
right default — measured, UIA found 100% of 32 targets in 0.52 s while the
pixel pipeline found 44% in 84 s. But some targets publish nothing: a
canvas-drawn button, an icon with no accessible name, a game, a remote
desktop. Today those end in "no locator resolved" and the model has to find
another way.

This module is what happens next, when — and only when — it is switched on:

    1. the lookup missed            act.click_element, a plain miss only
    2. risky target?                "delete", "pay", "send" ... refused
                                    unless allow_risky
    3. capture the TARGET window    not the desktop: a smaller picture is
                                    faster, and the answer cannot land in
                                    another application
    4. ask the local grounder       POST /ground on 127.0.0.1 — a small
                                    vision model, ~4 s a call, that returns
                                    one point in the picture it was sent
                                    (and, when asked, how far its first
                                    look and its final answer disagree)
    5. gate the answer              inside the picture and the window; the
                                    window has not moved while the model was
                                    thinking; a large disagreement is high
                                    risk
    6. snap to a real element       the actionable UIA element containing the
                                    point, else the nearest within a radius;
                                    the detector when UIA has nothing there
    7. read it back                 the element's own label (UIA name, else
                                    OCR of its box) must match the requested
                                    target, or the answer is "not found" —
                                    the model ALWAYS returns a point, even
                                    for a target that is not on screen
    8. click through the actuator   the same path as every click: Invoke for
                                    a UIA element, else an injected click
                                    that foregrounds the window or refuses
    9. verify                       did anything observable change: focus,
                                    the element's state, the pixels around
                                    it? Reported, never retried blindly
   10. write it down                one JSON line per decision, with the
                                    exact picture sent, for later training

Measured on real apps through this path (field test): 56% of raw points land
inside the requested element — text targets well (links 100%, fields 75%),
icons poorly (18–27%); many misses land on a NEIGHBOURING control (what the
snap + read-back fixes); absent targets always get a point (what the
read-back refuses); the service's `confidence` is useless (median 0.18 hit
vs 0.15 miss) and is not relied on; when the 1-pass and 4-pass answers
disagree by > 5% of the diagonal the answer is right ~20% of the time vs
~52% when they agree.

Coordinates. The service answers in pixels of the picture it was SENT. The
picture is the window's rectangle on the virtual desktop in physical pixels:
actuator.py makes the process per-monitor DPI aware at import and the
capture pins its own thread the same way, so window_rect, the capture, UIA
rectangles and SendInput all share one space. Mapping back is an offset (the
capture's origin — negative on a monitor left of the primary) and a scale
(only when a very large capture was shrunk before sending). Both are pure
functions so they can be tested without a screen.

Screenshots never leave the machine: the service URL must be loopback.
"""

from __future__ import annotations

import io
import json
import base64
import difflib
import logging
import math
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable

from . import tasklog

_logger = logging.getLogger("grounding")

# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

#: The "vision_grounding" block of settings.json. Off until someone turns it
#: on, and even then a refusal at any gate costs one log line, never a click.
DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "url": "http://127.0.0.1:8765",
    # One grounding call is ~3.5–5 s on this PC; a cold model can take longer.
    "timeout_s": 60,
    # Refuse an answer below this when the service reports a confidence.
    # None = no gate; an answer with no confidence is never gated by this.
    # Measured useless as a hit/miss signal — leave it None.
    "min_confidence": None,
    # Refuse unless the click lands inside the captured window. Also means a
    # click needs a window to bound it — no whole-screen grounding.
    "require_inside_window": True,
    # Everything but the click: log where it WOULD have clicked.
    "dry_run": False,
    # -- snap: move the model's point onto a real element ------------------
    "snap": True,
    # Furthest a point is moved to the nearest element, in physical pixels
    # at 96 DPI (scaled by the window's DPI). A point inside an element
    # always snaps to it.
    "snap_radius_px": 40,
    # Time budget for the UI Automation walk that lists the elements.
    "snap_budget_s": 1.0,
    # When UIA has no element near the point (canvas, inaccessible web), run
    # the OmniParser-style detector (YOLO + OCR, ui_parser.parse_ui) on the
    # captured picture. Heavy: loads ~1 GB of models on first use.
    "detector_fallback": False,
    # -- read-back: the element's own label must match the target ---------
    "readback": True,
    # Fuzzy match threshold, 0.5–1.0 (case/space/punctuation-insensitive).
    "readback_min_ratio": 0.8,
    # An element with NO readable label at all (an unnamed icon) is clicked
    # anyway — unless the answer is high-risk. Off: unreadable = not found.
    "readback_allow_unreadable": False,
    # How much further than the radius OCR reads, left and right of the
    # point (96 DPI px, scaled by the window's DPI). Labels wider than the
    # radius were cut mid-word and never matched. Held-out desktop set, no
    # UIA (canvas): 14 -> 24 of 128 targets clicked, no wrong click.
    "readback_ocr_reach_px": 200,
    # -- second look: a target nothing can read -----------------------------
    # Nothing readable at the point (an unnamed icon, a canvas): instead of
    # "not found", ask the model again on a magnified crop around the chosen
    # spot and click only if it points there again. An answer of "none"
    # refuses. Never for a high-risk answer. readback_allow_unreadable
    # (click without asking) wins when both are on. OFF: measured on the
    # held-out set it adds ~16-19 right clicks per 128 targets but lets 6-8
    # wrong ones through (~85% precision vs ~99% with read-back) — the model
    # re-points at the same wrong icon, so agreeing with itself is weak proof.
    "second_look": False,
    # The crop, as a fraction of the picture's width and height.
    "second_look_fraction": 0.55,
    # -- agreement: the service's 1-pass vs final answer ------------------
    "agreement": True,              # ask the service for it
    # Normalised distance above which the answer is high-risk (read-back
    # must pass; with read-back off, the click is refused).
    "max_disagreement": 0.05,
    # -- risky targets ------------------------------------------------------
    # Vision clicks on "delete", "pay", "send", "sign out" ... are refused
    # unless this is true. Named (UIA) clicks are not affected.
    "allow_risky": False,
    # -- after-click verification -------------------------------------------
    "verify": True,
    "verify_timeout_s": 1.5,
    # -- learning log -------------------------------------------------------
    # One JSON line per decision in <data>/grounding/decisions.jsonl, plus
    # the exact picture sent under <data>/grounding/captures/. Local only.
    "learning_log": True,
    # Stop saving pictures past this many (the JSON lines keep coming).
    "learning_log_max_shots": 2000,
}

_BOOL_KEYS = ("enabled", "require_inside_window", "dry_run", "snap",
              "detector_fallback", "readback", "readback_allow_unreadable",
              "second_look", "agreement", "allow_risky", "verify",
              "learning_log")

#: key -> (min, max, as_int)
_NUM_KEYS = {
    "timeout_s": (1.0, 600.0, False),
    "snap_radius_px": (0.0, 400.0, False),
    "snap_budget_s": (0.1, 10.0, False),
    "readback_min_ratio": (0.5, 1.0, False),
    "readback_ocr_reach_px": (0.0, 1000.0, False),
    "second_look_fraction": (0.15, 0.9, False),
    "max_disagreement": (0.0, 1.0, False),
    "verify_timeout_s": (0.2, 10.0, False),
    "learning_log_max_shots": (0, 1_000_000, True),
}

#: Hosts a screenshot of the desktop may be sent to. Local only, by design.
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

#: A capture longer than this on its long edge is shrunk before sending. The
#: service resizes anyway; this keeps the request small and the latency flat.
MAX_SIDE = 2560

#: A service found down is not knocked on again for this long.
RETRY_DOWN_S = 60.0
HEALTH_TIMEOUT_S = 3.0

#: How many prior actions to send as context.
HISTORY_N = 6

config: dict[str, Any] = dict(DEFAULTS)

#: Set by the app: returns the last few actions as short strings ("clicked
#: Save", "typed hello"), which the model sees as context. Optional.
history_provider: Callable[[], list[str]] | None = None

#: The last decisions, newest last, for the window and for diagnosis.
recent: deque = deque(maxlen=50)


def _as_bool(v) -> bool:
    return (v.strip().lower() in ("1", "true", "yes", "on")
            if isinstance(v, str) else bool(v))


def clean_settings(value) -> dict:
    """The block sanitised: every key present, every value the right type,
    anything unknown dropped. Takes a dict or a JSON string."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            value = {}
    if not isinstance(value, dict):
        value = {}
    out = dict(DEFAULTS)
    for k, v in value.items():
        if k not in DEFAULTS:
            continue
        if k in _BOOL_KEYS:
            out[k] = _as_bool(v)
        elif k == "url":
            s = str(v or "").strip().rstrip("/")
            out[k] = s if _is_loopback(s) else DEFAULTS["url"]
        elif k in _NUM_KEYS:
            lo, hi, as_int = _NUM_KEYS[k]
            try:
                n = min(max(float(v), lo), hi)
                out[k] = int(n) if as_int else n
            except (TypeError, ValueError):
                out[k] = DEFAULTS[k]
        elif k == "min_confidence":
            if v is None or v == "":
                out[k] = None
            else:
                try:
                    out[k] = min(max(float(v), 0.0), 1.0)
                except (TypeError, ValueError):
                    out[k] = None
    return out


def _is_loopback(url: str) -> bool:
    try:
        p = urllib.parse.urlsplit(url)
    except Exception:
        return False
    return p.scheme in ("http", "https") and (p.hostname or "") in LOOPBACK_HOSTS


def configure(settings: dict | None) -> dict:
    """Apply the "vision_grounding" block (or a whole settings dict holding
    one). A changed URL forgets what was known about the old service."""
    block = settings or {}
    if "vision_grounding" in block and isinstance(block["vision_grounding"], dict):
        block = block["vision_grounding"]
    new = clean_settings(block)
    if new["url"] != config.get("url"):
        _health.update(ok=None, at=0.0, detail="")
    config.clear()
    config.update(new)
    return dict(config)


def enabled() -> bool:
    return bool(config.get("enabled"))


# ---------------------------------------------------------------------------
# the service
# ---------------------------------------------------------------------------

class Unavailable(Exception):
    """The service could not be reached or did not answer properly."""


class CaptureError(Exception):
    """No usable picture of the target window."""


#: No proxy, ever: the service is on this machine.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http(url: str, payload: dict | None, timeout_s: float) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method="POST" if data is not None else "GET",
        headers={"Content-Type": "application/json"} if data is not None else {})
    try:
        with _opener.open(req, timeout=timeout_s) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        raise Unavailable(f"HTTP {e.code} from {url}") from e
    except urllib.error.URLError as e:
        raise Unavailable(f"{getattr(e, 'reason', e)}") from e
    except (OSError, TimeoutError) as e:
        raise Unavailable(f"{type(e).__name__}: {e}") from e
    try:
        out = json.loads(body)
    except Exception as e:
        raise Unavailable("non-JSON reply") from e
    if not isinstance(out, dict):
        raise Unavailable("reply is not an object")
    return out


_health: dict[str, Any] = {"ok": None, "at": 0.0, "detail": ""}


def health(force: bool = False) -> tuple[bool, str]:
    """Is the service up? Asked once; a 'down' is remembered for
    RETRY_DOWN_S so a task does not pay a connect timeout per click."""
    now = time.time()
    if not force:
        if _health["ok"] is True:
            return True, _health["detail"]
        if _health["ok"] is False and now - _health["at"] < RETRY_DOWN_S:
            return False, _health["detail"]
    try:
        info = _http(config["url"] + "/health", None, HEALTH_TIMEOUT_S)
        ok = bool(info.get("ok"))
        detail = (f"adapter={info.get('adapter')} "
                  f"model_loaded={info.get('model_loaded')} "
                  f"step={info.get('model_step')}" if ok
                  else f"service reports not ok: {info}")
    except Unavailable as e:
        ok, detail = False, str(e)
    _health.update(ok=ok, at=now, detail=detail)
    return ok, detail


def _mark_down(why: str) -> None:
    _health.update(ok=False, at=time.time(), detail=why)


# ---------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------

@dataclass
class Capture:
    png: bytes                         # exactly what is sent
    sent_size: tuple[int, int]         # (w, h) of the picture sent
    region: dict                       # left/top/width/height, physical px,
                                       # where the capture sits on the desktop
    window_rect: tuple | None          # the window at capture time, or None
    title: str = ""
    hwnd: int = 0
    img: Any = None                    # the sent picture, as a PIL image


def encode(img, region: dict, window_rect: tuple | None, title: str = "",
           hwnd: int = 0) -> Capture:
    """A PIL image as the request will carry it. Pure: tests feed a drawn
    picture through this instead of grabbing the screen."""
    from PIL import Image
    w, h = img.size
    if max(w, h) > MAX_SIDE:
        s = MAX_SIDE / max(w, h)
        img = img.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Capture(buf.getvalue(), img.size, dict(region), window_rect, title,
                   hwnd, img)


def _capture_window(hwnd: int | None) -> Capture:
    """The target window's pixels, clipped to the virtual desktop, in physical
    pixels. A slept or locked display is an error, never 'a dark picture'."""
    import mss
    from PIL import Image

    from . import display, locator
    from .actuator import make_thread_dpi_aware

    # Same space as UIA rects and SendInput, whatever the GUI stack did to
    # the process default (see actuator.make_thread_dpi_aware).
    make_thread_dpi_aware()
    display.assert_usable()
    with mss.mss() as sct:
        desktop, primary = sct.monitors[0], sct.monitors[1]
        if hwnd:
            l, t, r, b = locator.window_rect(hwnd)
            L, T = max(l, desktop["left"]), max(t, desktop["top"])
            R = min(r, desktop["left"] + desktop["width"])
            B = min(b, desktop["top"] + desktop["height"])
            if R - L < 8 or B - T < 8:
                raise CaptureError("the window is not visible on any screen "
                                   "(minimised or off the desktop)")
            region = {"left": L, "top": T, "width": R - L, "height": B - T}
            rect, title = (l, t, r, b), locator.window_title(hwnd)
        else:
            region = {k: primary[k] for k in ("left", "top", "width", "height")}
            rect, title = None, ""
        shot = sct.grab(region)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    if display.lit_fraction(img) < 0.005:
        raise CaptureError("the capture is blank — the window is occluded or "
                           "minimised, or the display is off")
    return encode(img, region, rect, title, hwnd or 0)


def _window_rect(hwnd: int) -> tuple:
    from . import locator
    return locator.window_rect(hwnd)


# ---------------------------------------------------------------------------
# geometry — pure
# ---------------------------------------------------------------------------

def map_point(x: float, y: float, region: dict,
              sent_size: tuple[int, int]) -> tuple[int, int]:
    """A point in the SENT picture -> physical screen pixels.

    The picture is `region` (left/top/width/height on the virtual desktop)
    shown at `sent_size`; the two differ only when a large capture was
    shrunk. Offset first, scale second, and the offset may be negative.
    """
    sw, sh = sent_size
    sx = region["width"] / max(sw, 1)
    sy = region["height"] / max(sh, 1)
    return (int(round(region["left"] + x * sx)),
            int(round(region["top"] + y * sy)))


def screen_to_image(x: float, y: float, region: dict,
                    sent_size: tuple[int, int]) -> tuple[float, float]:
    """The inverse of map_point (unrounded)."""
    sw, sh = sent_size
    return ((x - region["left"]) * sw / max(region["width"], 1),
            (y - region["top"]) * sh / max(region["height"], 1))


def rect_to_image(rect: tuple, region: dict, sent_size: tuple[int, int]) -> tuple:
    x1, y1 = screen_to_image(rect[0], rect[1], region, sent_size)
    x2, y2 = screen_to_image(rect[2], rect[3], region, sent_size)
    return (int(math.floor(x1)), int(math.floor(y1)),
            int(math.ceil(x2)), int(math.ceil(y2)))


def rect_to_screen(rect: tuple, region: dict, sent_size: tuple[int, int]) -> tuple:
    x1, y1 = map_point(rect[0], rect[1], region, sent_size)
    x2, y2 = map_point(rect[2], rect[3], region, sent_size)
    return (x1, y1, x2, y2)


def inside_image(x: float, y: float, sent_size: tuple[int, int]) -> bool:
    w, h = sent_size
    return 0 <= x < w and 0 <= y < h


def inside_rect(x: int, y: int, rect: tuple) -> bool:
    l, t, r, b = rect
    return l <= x < r and t <= y < b


def rect_distance(x: float, y: float, rect: tuple) -> float:
    """0 inside the rectangle, else the distance to its nearest edge."""
    l, t, r, b = rect
    dx = max(l - x, 0, x - (r - 1))
    dy = max(t - y, 0, y - (b - 1))
    return math.hypot(dx, dy)


def rect_center(rect: tuple) -> tuple[int, int]:
    return (int(rect[0] + rect[2]) // 2, int(rect[1] + rect[3]) // 2)


def rect_area(rect: tuple) -> int:
    return max(rect[2] - rect[0], 0) * max(rect[3] - rect[1], 0)


# ---------------------------------------------------------------------------
# labels — pure: normalising, aliases, fuzzy match, risky names
# ---------------------------------------------------------------------------

#: A token that is nothing but one of these symbols reads as the word.
#: Accessibility names spell operators out ('Plus', 'Divide by'); what is
#: drawn, and what a model or a person asks for, is often the symbol.
_SYMBOLS = {
    "+": "plus", "-": "minus", "−": "minus", "–": "minus", "*": "multiply",
    "×": "multiply", "/": "divide", "÷": "divide", "=": "equals",
    ".": "point", "%": "percent", "±": "plusminus", "+/-": "plusminus",
    "⌫": "backspace", "√": "sqrt", "&": "and", "@": "at",
}

#: Words that mean the same thing, folded to one spelling.
_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "add": "plus", "subtract": "minus", "times": "multiply",
    "multiplication": "multiply", "division": "divide", "equal": "equals",
    "decimal": "point", "dot": "point", "percentage": "percent",
    "okay": "ok",
}

#: Phrases folded before splitting into words (after lower-casing).
_PHRASES = (
    ("multiply by", "multiply"), ("divide by", "divide"),
    ("decimal separator", "point"), ("positive negative", "plusminus"),
    ("square root", "sqrt"),
)

#: Shortcut hints that ride along in names and tooltips: 'Bold (Ctrl+B)'.
_SHORTCUT = re.compile(r"\(?\b(?:ctrl|alt|shift|win|cmd)\s*\+\s*\S+\)?", re.I)


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").replace("\xa0", " ")
    s = _SHORTCUT.sub(" ", s)
    s = s.replace("…", " ")
    # Windows OCR reads Turkish first (pipeline/vision/ocr.py), and then
    # English 'View' comes back as 'Vıew'. Both sides are folded the same
    # way, so a Turkish target still matches its own text.
    s = s.replace("İ", "i").replace("I", "i").replace("ı", "i")
    return " ".join(s.split()).casefold()


def split_identifier(s: str) -> str:
    """'num7Button' -> 'num 7 Button', 'close_btn' -> 'close btn'."""
    s = re.sub(r"[_\-.]+", " ", s or "")
    s = re.sub(r"(?<=[a-z])(?=[A-Z0-9])|(?<=[0-9])(?=[A-Za-z])|"
               r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)
    return s


def canon_tokens(s: str) -> list[str]:
    """A label as comparable words: case, whitespace, punctuation and the
    spelling of digits and operators no longer matter."""
    s = _fold(s)
    if not s:
        return []
    whole = _SYMBOLS.get(s)
    if whole:
        return [whole]
    for a, b in _PHRASES:
        s = s.replace(a, b)
    out: list[str] = []
    for tok in s.split():
        sym = _SYMBOLS.get(tok)
        if sym:
            out.append(sym)
            continue
        for w in re.sub(r"[\W_]+", " ", tok).split():
            out.append(_WORDS.get(w, w))
    return out


def _tok_sim(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if a.isdigit() or b.isdigit():
        return 0.0          # '7' is not '1', however similar the strings
    return difflib.SequenceMatcher(None, a, b).ratio()


def match_score(target: str, label: str) -> float:
    """How well a label names the target, 0..1.

    The minimum of two views, so both must agree: the whole strings are
    alike (forgives OCR slips: 'Sve' for 'Save'), AND the words line up
    (refuses an extra or missing word: 'Save' is not 'Save as', 'Sign in'
    is not 'Sign out'). A target that is the leading words of a long label
    ('Furkan Fizix' of 'Furkan Fizix 15:30 hello') passes, as in locator.
    """
    t, l = canon_tokens(target), canon_tokens(label)
    if not t or not l:
        return 0.0
    if t == l:
        return 1.0
    tj, lj = " ".join(t), " ".join(l)
    if len(tj) >= 12 and lj.startswith(tj + " "):
        return 0.95
    whole = difflib.SequenceMatcher(None, tj, lj).ratio()

    def covered(a: list[str], b: list[str]) -> float:
        return sum(1 for x in a if max(_tok_sim(x, y) for y in b) >= 0.8) / len(a)

    recall, precision = covered(t, l), covered(l, t)
    words = (2 * recall * precision / (recall + precision)
             if recall + precision else 0.0)
    return min(whole, words)


def best_label(target: str, labels: list[str]) -> tuple[float, str]:
    """The best (score, label) among `labels`; (0.0, '') when none."""
    best = (0.0, "")
    for lab in labels:
        if not lab or not str(lab).strip():
            continue
        s = match_score(target, str(lab))
        if s > best[0]:
            best = (s, str(lab))
    return best


#: Names a vision click must not act on unless the user allows it. A wrong
#: point on one of these is not a miss, it is a loss. English only.
RISKY_TERMS = (
    "delete", "remove", "uninstall", "format", "erase", "wipe", "pay",
    "payment", "buy", "purchase", "checkout", "check out", "place order",
    "order now", "send", "submit", "confirm", "transfer", "withdraw",
    "sign out", "signout", "log out", "logout", "log off", "shut down",
    "shutdown", "restart", "reboot", "reset", "discard", "empty trash",
    "empty recycle bin", "deactivate", "close account", "unsubscribe",
    "overwrite", "trash", "donate",
)
_RISKY_RE = re.compile(r"(?<![\w])(" + "|".join(
    re.escape(t).replace(r"\ ", r"\s+") for t in RISKY_TERMS) + r")(?![\w])")


def risky(name: str) -> str:
    """The risky term in `name`, or '' if none."""
    m = _RISKY_RE.search(_fold(name))
    return m.group(1) if m else ""


def target_from_instruction(instruction: str) -> str:
    """'click Save (Button)' -> 'Save'."""
    s = (instruction or "").strip()
    s = re.sub(r"^(?:left[- ]?)?click\s+(?:on\s+)?", "", s, flags=re.I)
    s = re.sub(r"\s*\((?:[A-Za-z]+)\)\s*$", "", s)
    return s.strip().strip("'\"")


# ---------------------------------------------------------------------------
# candidate elements
# ---------------------------------------------------------------------------

@dataclass
class Cand:
    """One clickable thing near the point: a UIA element, a detector box,
    or a line of OCR text. Rectangles are physical screen pixels."""
    rect: tuple
    name: str = ""
    control_type: str = ""
    automation_id: str = ""
    help_text: str = ""
    source: str = "uia"                # uia | detector:<kind> | ocr
    control: Any = None                # live UIA handle, when there is one
    ocr_text: str = ""                 # what OCR read inside the box

    def uia_labels(self) -> list[str]:
        out = [self.name, self.help_text]
        if self.automation_id:
            out.append(split_identifier(self.automation_id))
        return [x for x in out if x and x.strip()]

    def describe(self, region: dict | None = None,
                 sent_size: tuple | None = None) -> dict:
        d = {"name": self.name, "type": self.control_type,
             "automation_id": self.automation_id, "source": self.source,
             "box": [int(v) for v in self.rect]}
        if self.ocr_text:
            d["ocr_text"] = self.ocr_text
        if region and sent_size:
            d["box_image"] = list(rect_to_image(self.rect, region, sent_size))
        return d


#: UIA control types a person clicks. Text, images, panes and groups are
#: most of a tree's nodes and none of its choices.
ACTIONABLE = {
    "ButtonControl", "SplitButtonControl", "HyperlinkControl", "EditControl",
    "ComboBoxControl", "CheckBoxControl", "RadioButtonControl",
    "MenuItemControl", "TabItemControl", "ListItemControl", "SliderControl",
    "TreeItemControl", "DataItemControl", "HeaderItemControl",
}


def _core_window_roots(hwnd: int) -> list:
    """A Store app's content lives in a CoreWindow child of the frame."""
    import ctypes
    import uiautomation as auto
    from . import locator

    kids: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(h, _l):
        if locator.window_class(h) == "Windows.UI.Core.CoreWindow":
            kids.append(h)
        return True

    ctypes.windll.user32.EnumChildWindows(hwnd, cb, None)
    out = []
    for h in kids:
        try:
            c = auto.ControlFromHandle(h)
            if c is not None:
                out.append(c)
        except Exception:
            pass
    return out


def _uia_candidates(hwnd: int, budget_s: float, max_nodes: int = 1500) -> list[Cand]:
    """The window's actionable, on-screen UIA elements, under a time budget."""
    from . import locator
    out: list[Cand] = []
    if not hwnd:
        return out
    try:
        roots = [locator.uia_root(hwnd)]
        wl, wt, wr, wb = locator.window_rect(hwnd)
    except Exception:
        return out
    try:
        if locator.is_host_frame(hwnd):
            roots += _core_window_roots(hwnd)
    except Exception:
        pass
    t0, seen = time.time(), set()
    for root in roots:
        left = budget_s - (time.time() - t0)
        if root is None or left <= 0:
            break
        for ch in locator.walk(root, max_nodes=max_nodes, budget_s=left):
            try:
                tn = ch.ControlTypeName
            except Exception:
                continue
            if tn not in ACTIONABLE:
                continue
            r = locator._rect_of(ch)
            if not r or r[2] <= wl or r[0] >= wr or r[3] <= wt or r[1] >= wb:
                continue
            try:
                if ch.IsOffscreen:
                    continue
            except Exception:
                pass
            try:
                name = (ch.Name or "").strip()
                aid = ch.AutomationId or ""
            except Exception:
                name, aid = "", ""
            try:
                helptext = (ch.HelpText or "").strip()
            except Exception:
                helptext = ""
            key = (r, name, tn)
            if key in seen:
                continue
            seen.add(key)
            out.append(Cand(rect=r, name=name, control_type=tn,
                            automation_id=aid, help_text=helptext,
                            source="uia", control=ch))
    return out


def _repo_on_path() -> None:
    root = str(Path(__file__).resolve().parents[2])
    if root not in sys.path:
        sys.path.append(root)        # append: repo root must not shadow agent/


def _detector_candidates(cap: Capture) -> list[Cand]:
    """The OmniParser-style detector (YOLO icon boxes + OCR text) on the
    picture that was sent. Heavy, so imported only here."""
    _repo_on_path()
    import ui_parser
    if ui_parser._models.get("yolo") is None:
        ui_parser.load_models()
    res = ui_parser.parse_ui(cap.img)
    out = []
    for e in res.get("elements", []):
        b = e.get("bbox") or {}
        try:
            box = (int(b["x1"]), int(b["y1"]), int(b["x2"]), int(b["y2"]))
        except (KeyError, TypeError, ValueError):
            continue
        rect = rect_to_screen(box, cap.region, cap.sent_size)
        kind = e.get("type", "")
        # An icon's caption describes it ('a floppy disk'); it is not a
        # name and is never used as one. Only text read off the box is.
        out.append(Cand(rect=rect, name=e.get("content", "") if kind == "text" else "",
                        control_type=kind, source=f"detector:{kind}"))
    return out


def _segments(words) -> list[list]:
    """Split one OCR line where the gap between words is wider than the
    text is tall: Windows OCR reads a toolbar ('File  Edit  View') as ONE
    line, and each button's label must be read on its own."""
    segs: list[list] = []
    for w in words:
        if segs:
            prev = segs[-1][-1]
            gap = w.left - (prev.left + prev.width)
            if gap > max(prev.height, w.height) * 0.9:
                segs.append([w])
                continue
            segs[-1].append(w)
        else:
            segs.append([w])
    return segs


#: The Windows OCR worker, once found broken, is not asked again.
_ocr_state: dict[str, Any] = {"winocr": None}


def _ocr_lines(img, box: tuple) -> list[tuple[tuple, str]] | None:
    """Text lines inside `box` of `img` (picture coordinates both ways).

    Windows' own OCR first (tens of ms, no download), EasyOCR only when it
    is already loaded. None = no OCR engine available; [] = nothing read.
    """
    from PIL import Image
    x1, y1, x2, y2 = [int(v) for v in box]
    x1, y1 = max(x1, 0), max(y1, 0)
    x2, y2 = min(x2, img.width), min(y2, img.height)
    if x2 - x1 < 4 or y2 - y1 < 4:
        return []
    crop = img.crop((x1, y1, x2, y2)).convert("RGB")
    # Small UI text reads far better at twice the size.
    f = 2 if max(crop.size) <= 2000 else 1
    if f != 1:
        crop = crop.resize((crop.width * f, crop.height * f), Image.LANCZOS)

    if _ocr_state["winocr"] is not False:
        try:
            from . import pipeline_bridge
            pipeline_bridge.ensure_importable()
            from vision.capture import Frame
            from vision.ocr import read
            rd = read(Frame(crop, 0, 0, crop.width, crop.height, time.time()))
            _ocr_state["winocr"] = True
            out = []
            for ln in rd.lines:
                for seg in _segments(ln.words):
                    l = min(w.left for w in seg)
                    t = min(w.top for w in seg)
                    r = max(w.left + w.width for w in seg)
                    b = max(w.top + w.height for w in seg)
                    out.append(((x1 + l / f, y1 + t / f, x1 + r / f, y1 + b / f),
                                " ".join(w.text for w in seg)))
            return out
        except Exception as e:
            _logger.info("windows OCR unavailable: %s", e)
            _ocr_state["winocr"] = False
    try:
        _repo_on_path()
        import ui_parser
        reader = ui_parser._models.get("easyocr")
        if reader is None:
            return None
        import numpy as np
        out = []
        for bb, text, conf in reader.readtext(np.array(crop)):
            xs = [p[0] / f for p in bb]
            ys = [p[1] / f for p in bb]
            out.append(((x1 + min(xs), y1 + min(ys), x1 + max(xs), y1 + max(ys)),
                        (text or "").strip()))
        return out
    except Exception:
        return None


def _dpi_scale(hwnd: int) -> float:
    try:
        import ctypes
        dpi = ctypes.windll.user32.GetDpiForWindow(hwnd) if hwnd else 0
        return dpi / 96.0 if dpi else 1.0
    except Exception:
        return 1.0


# ---------------------------------------------------------------------------
# snap + read-back — the choice, given the candidates
# ---------------------------------------------------------------------------

@dataclass
class Choice:
    cand: Cand | None                  # what to click; None = the raw point
    readback: dict                     # {"done", "ok", "score", "label", "source", ...}
    snapped: bool = False
    distance: float = 0.0
    refuse: str = ""                   # non-empty: not found, and why


def _geometric(near: list[tuple[float, int, Cand]]) -> tuple | None:
    """Containing -> the tightest; else the nearest (then the smaller)."""
    if not near:
        return None
    inside = [n for n in near if n[0] == 0]
    if inside:
        return min(inside, key=lambda n: n[1])
    return min(near, key=lambda n: (n[0], n[1]))


def choose(px: int, py: int, cands: list[Cand], target: str, *,
           radius: float, readback: bool, min_ratio: float,
           allow_unreadable: bool, high_risk: bool,
           read_text: Callable[[tuple], list[tuple[tuple, str]] | None],
           ocr_reach: float = 0.0,
           ) -> Choice:
    """Which element the point means, and whether its label says so.

    Pure apart from `read_text(screen_box) -> [(screen_rect, text)] | None`,
    which OCRs a screen rectangle (None = no OCR available). `ocr_reach`
    widens that rectangle left and right of the point, so a label longer
    than the radius is read whole.

    Without read-back: the element containing the point (the tightest),
    else the nearest within `radius`, else the raw point.

    With read-back: a candidate within the radius whose own label matches
    the target wins over a nearer one that does not — the measured miss is
    a point on the NEIGHBOUR of the right control. A matching line of text
    with no element around it (a canvas) is a target too. Nothing matching
    is "not found": the model returns a point for anything, even a target
    that is not on screen.
    """
    near = [(rect_distance(px, py, c.rect), rect_area(c.rect), c) for c in cands]
    near = [n for n in near if n[0] <= radius]
    geo = _geometric(near)

    if not readback:
        if geo is None:
            return Choice(None, {"done": False})
        return Choice(geo[2], {"done": False}, snapped=True, distance=geo[0])

    rb: dict[str, Any] = {"done": True, "ok": False, "score": 0.0, "label": "",
                          "source": "", "min_ratio": min_ratio}
    scored = []
    for d, a, c in near:
        s, lab = best_label(target, c.uia_labels())
        scored.append((s, lab, d, a, c, "uia"))
    hits = [x for x in scored if x[0] >= min_ratio]

    lines: list[tuple[tuple, str]] | None = []
    if not hits:
        # One OCR pass over the neighbourhood: the point's surroundings plus
        # every candidate box in reach. Each box is read from the lines whose
        # centre falls inside it.
        pad = max(radius, 24)
        reach = pad + max(0.0, ocr_reach)
        boxes = [(px - reach, py - pad / 2, px + reach, py + pad / 2)]
        boxes += [c.rect for _d, _a, c in near]
        area = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes))
        lines = read_text(area)
        if lines is None:
            rb["ocr"] = "unavailable"
        else:
            rescored = []
            for s, lab, d, a, c, src in scored:
                inner = [t for r, t in lines
                         if inside_rect(*rect_center(r), c.rect) and t.strip()]
                if inner:
                    c.ocr_text = " ".join(inner)
                    s2, lab2 = best_label(target, inner + [c.ocr_text])
                    if s2 > s:
                        s, lab, src = s2, lab2, "ocr"
                rescored.append((s, lab, d, a, c, src))
            scored = rescored
            hits = [x for x in scored if x[0] >= min_ratio]

    if hits:
        s, lab, d, a, c, src = min(hits, key=lambda x: (x[2], x[3], -x[0]))
        rb.update(ok=True, score=round(s, 3), label=lab, source=src)
        return Choice(c, rb, snapped=True, distance=d)

    # A line of text that IS the target, near the point, with no element.
    if lines:
        tl = []
        for r, t in lines:
            d = rect_distance(px, py, r)
            if d > radius:
                continue
            s, _ = best_label(target, [t])
            if s >= min_ratio:
                tl.append((d, s, r, t))
        if tl:
            d, s, r, t = min(tl, key=lambda x: (x[0], -x[1]))
            r = tuple(int(round(v)) for v in r)
            c = Cand(rect=r, name=t, control_type="text", source="ocr",
                     ocr_text=t)
            rb.update(ok=True, score=round(s, 3), label=t, source="ocr")
            return Choice(c, rb, snapped=True, distance=d)

    # Nothing names the target. Say what WAS there.
    best = max(scored, key=lambda x: x[0], default=None)
    if best is not None:
        rb.update(score=round(best[0], 3), label=best[1] or best[4].ocr_text)
    if geo is not None:
        gc = geo[2]
        readable = bool(gc.uia_labels() or gc.ocr_text)
        seen = (gc.name or gc.ocr_text or gc.help_text or "")[:60]
    else:
        gc = None
        readable = bool(lines and any(t.strip() for r, t in lines
                                      if rect_distance(px, py, r) <= radius))
        seen = ""
    if not readable and allow_unreadable and not high_risk:
        rb.update(ok=None, reason="unreadable: nothing to read; allowed")
        return Choice(gc, rb, snapped=gc is not None,
                      distance=geo[0] if geo else 0.0)
    if seen:
        why = (f"not found — the control at the model's point reads "
               f"{seen!r}, not {target!r}")
    elif lines is None:
        why = (f"not found — nothing at the model's point has a name, and no "
               f"OCR is available to read it")
    elif not readable:
        why = (f"not found — nothing readable at the model's point names "
               f"{target!r}")
    else:
        why = f"not found — nothing near the model's point reads {target!r}"
    if high_risk:
        why += " (and the model's two looks disagreed)"
    rb["reason"] = why
    return Choice(None, rb, refuse=why)


# ---------------------------------------------------------------------------
# after-click verification
# ---------------------------------------------------------------------------

THUMB = 64
THUMB_LEVEL = 24        # grey levels a cell must move to count
THUMB_CELLS = 4         # cells that must move: a blinking caret moves 1–2


def thumb(img, mask: tuple | None = None) -> bytes:
    """A 64x64 grey thumbnail with `mask` (picture coordinates) blanked —
    the clicked element itself, whose hover highlight is not an effect."""
    from PIL import ImageDraw
    g = img.convert("L")
    if mask is not None:
        ImageDraw.Draw(g).rectangle([mask[0], mask[1], mask[2], mask[3]], fill=0)
    from PIL import Image
    # BOX: each cell is the plain average of the pixels it covers.
    return g.resize((THUMB, THUMB), Image.BOX).tobytes()


def thumb_diff(a: bytes, b: bytes, level: int = THUMB_LEVEL) -> int:
    if not a or not b or len(a) != len(b):
        return 0
    return sum(1 for x, y in zip(a, b) if abs(x - y) > level)


def _changed(before: dict, after: dict) -> str:
    """Why `after` differs observably from `before`, or ''."""
    if after.get("fg") and before.get("fg") != after.get("fg"):
        return "the foreground window changed"
    if after.get("focus") is not None and before.get("focus") != after.get("focus"):
        return "keyboard focus moved"
    if before.get("state") is not None and before.get("state") != after.get("state"):
        return "the element's state changed"
    n = thumb_diff(before.get("thumb") or b"", after.get("thumb") or b"")
    if n >= THUMB_CELLS:
        return f"the window changed around the element ({n} cells)"
    return ""


def _state_of(ctrl) -> tuple | None:
    if ctrl is None:
        return None
    out = []
    for get, prop in (("GetTogglePattern", "ToggleState"),
                      ("GetSelectionItemPattern", "IsSelected"),
                      ("GetExpandCollapsePattern", "ExpandCollapseState"),
                      ("GetValuePattern", "Value")):
        try:
            p = getattr(ctrl, get)()
            out.append(None if p is None else getattr(p, prop))
        except Exception:
            out.append(None)
    return tuple(out)


def _observe(cand: Cand | None, rect: tuple, hwnd: int) -> dict:
    """What can be seen of the window right now: foreground, focus, the
    element's pattern state, and a masked thumbnail of the window."""
    import mss
    from PIL import Image
    import uiautomation as auto

    from . import locator
    from .actuator import make_thread_dpi_aware

    make_thread_dpi_aware()
    st: dict[str, Any] = {"fg": locator.foreground_window()}
    try:
        f = auto.GetFocusedControl()
        st["focus"] = (locator._rect_of(f), f.Name or "") if f is not None else None
    except Exception:
        st["focus"] = None
    st["state"] = _state_of(cand.control if cand is not None else None)
    if hwnd:
        l, t, r, b = locator.window_rect(hwnd)
        with mss.mss() as sct:
            dk = sct.monitors[0]
            L, T = max(l, dk["left"]), max(t, dk["top"])
            R = min(r, dk["left"] + dk["width"])
            B = min(b, dk["top"] + dk["height"])
            if R - L >= 8 and B - T >= 8:
                shot = sct.grab({"left": L, "top": T, "width": R - L,
                                 "height": B - T})
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                m = (rect[0] - L - 6, rect[1] - T - 6, rect[2] - L + 6,
                     rect[3] - T + 6)
                st["thumb"] = thumb(img, m)
    return st


def _verify(cand: Cand | None, rect: tuple, hwnd: int, before: dict | None,
            timeout_s: float) -> tuple[bool | None, str]:
    """Poll for an observable change after the click. Never clicks again."""
    if before is None:
        return None, "no baseline before the click"
    deadline = time.time() + timeout_s
    while True:
        try:
            after = _observe(cand, rect, hwnd)
        except Exception as e:
            return None, f"could not observe ({type(e).__name__}: {e})"
        why = _changed(before, after)
        if why:
            return True, why
        if time.time() >= deadline:
            return False, f"no observable change within {timeout_s:g} s"
        time.sleep(0.15)


# ---------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------

@dataclass
class Decision:
    """One grounding attempt, start to finish — what was asked, what came
    back, and what was done about it. Every one is logged."""
    instruction: str
    hwnd: int = 0
    title: str = ""
    #: disabled | unavailable | refused | not_found | dry_run | clicked |
    #: not_clicked
    outcome: str = "disabled"
    reason: str = ""
    accepted: bool = False             # the answer passed every gate
    clicked: bool = False              # and the actuator reported a click
    image_point: tuple | None = None   # in the sent picture
    point: tuple | None = None         # the model's point, physical screen px
    confidence: float | None = None
    latency_ms: int | None = None      # the service's own
    seconds: float = 0.0               # ours, end to end
    method: str = ""
    raw: str = ""
    model_step: int | None = None
    region: dict = field(default_factory=dict)
    sent_size: tuple | None = None
    history: list = field(default_factory=list)
    target: str = ""                   # the name the read-back compares to
    agreement_distance: float | None = None
    pass1_point: tuple | None = None   # in the sent picture
    high_risk: bool = False            # disagreement > max_disagreement
    snapped: dict | None = None        # the element chosen (name/type/box)
    snap_distance: float | None = None
    readback: dict | None = None
    click_point: tuple | None = None   # where the click went (screen px)
    click_rect: tuple | None = None    # what was clicked (screen px)
    verified: bool | None = None       # None = not checked
    verify_reason: str = ""
    second_look: dict | None = None    # the zoomed re-ask of an unreadable target
    capture_path: str = ""             # learning log copy of the picture

    def as_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        bits = [self.outcome]
        if self.click_point and self.click_point != self.point:
            bits.append(f"at {self.click_point} (model {self.point})")
        elif self.point:
            bits.append(f"at {self.point}")
        if self.readback and self.readback.get("done"):
            bits.append(f"read-back {self.readback.get('ok')}")
        if self.verified is not None:
            bits.append("verified" if self.verified else "UNVERIFIED")
        if self.latency_ms is not None:
            bits.append(f"{self.latency_ms}ms")
        if self.reason:
            bits.append(f"— {self.reason}")
        return " ".join(bits)


#: The second look's crop is magnified towards the service's default pixel
#: budget (a small crop shown at its own size made the model WORSE), at most
#: this many times.
SECOND_LOOK_PIXELS = 1_003_520
SECOND_LOOK_MAX_ZOOM = 2.5


def says_absent(resp: dict) -> bool:
    """The service's answer is the model saying the target is not there: no
    point, and the text is just 'none'."""
    return (not resp.get("ok") and resp.get("error") == "no_point"
            and re.fullmatch(r"\s*none\s*\.?\s*", str(resp.get("raw") or ""),
                             re.I) is not None)


def look_box(x: float, y: float, size: tuple[int, int],
             fraction: float) -> tuple[int, int, int, int]:
    """The second look's crop in picture pixels: `fraction` of the picture's
    width and height (at least 28 px), centred on (x, y), shifted to stay
    inside the picture. Pure."""
    W, H = size
    cw = min(W, max(28, round(W * fraction)))
    ch = min(H, max(28, round(H * fraction)))
    left = min(W - cw, max(0, round(x - cw / 2)))
    top = min(H - ch, max(0, round(y - ch / 2)))
    return left, top, left + cw, top + ch


def second_look(cap: Capture, instruction: str, history: list[str],
                click_pt: tuple, click_rect: tuple | None, radius: float,
                ) -> tuple[bool, str, dict]:
    """Ask again on a magnified crop around where the click would go.

    Agrees when the new point is on the same element (its box grown by half
    the radius) — or, with no element, within the radius of the click point.
    Returns (agrees, why, details for the log)."""
    from PIL import Image
    cfg = config
    ix, iy = screen_to_image(click_pt[0], click_pt[1], cap.region, cap.sent_size)
    box = look_box(ix, iy, cap.sent_size, float(cfg.get("second_look_fraction", 0.55)))
    crop = cap.img.crop(box)
    cw, ch = crop.size
    s = min(SECOND_LOOK_MAX_ZOOM, math.sqrt(SECOND_LOOK_PIXELS / max(1, cw * ch)))
    if s > 1:
        crop = crop.resize((max(1, round(cw * s)), max(1, round(ch * s))), Image.LANCZOS)
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    info: dict[str, Any] = {"box_image": list(box), "sent_size": list(crop.size)}
    payload = {"image_b64": base64.b64encode(buf.getvalue()).decode("ascii"),
               "instruction": instruction}
    if history:
        payload["history"] = history
    try:
        resp = _http(cfg["url"] + "/ground", payload, float(cfg["timeout_s"]))
    except Unavailable as e:
        return False, f"the second look failed ({e})", info
    info.update(raw=str(resp.get("raw") or "")[:200], latency_ms=resp.get("latency_ms"))
    if says_absent(resp):
        return False, "on a closer look the model says it is not there", info
    if not resp.get("ok"):
        return False, f"the second look failed ({resp.get('error') or 'unknown'})", info
    try:
        zx, zy = float(resp["x"]), float(resp["y"])
    except (KeyError, TypeError, ValueError):
        return False, "the second look returned no usable point", info
    sw, sh = crop.size
    sx, sy = map_point(box[0] + zx * cw / max(sw, 1), box[1] + zy * ch / max(sh, 1),
                       cap.region, cap.sent_size)
    info["point"] = [sx, sy]
    if click_rect is not None:
        t = radius / 2
        grown = (click_rect[0] - t, click_rect[1] - t, click_rect[2] + t, click_rect[3] + t)
        dist, agrees = rect_distance(sx, sy, click_rect), inside_rect(sx, sy, grown)
        where = "that element"
    else:
        dist = math.hypot(sx - click_pt[0], sy - click_pt[1])
        agrees, where = dist <= radius, "that spot"
    info.update(distance=round(dist, 1), agrees=agrees)
    if agrees:
        return True, f"a second, closer look points at {where} again", info
    return False, (f"on a closer look the model points elsewhere ({sx},{sy}), "
                   f"{dist:.0f} px from {where}"), info


def _act(el, prefer: str = "inject"):
    """The one place a grounded point becomes input: the ordinary actuator,
    which Invokes a UIA element, or foregrounds the window and injects, or
    refuses. Replaced by tests."""
    from . import actuator
    return actuator.click(el, "left", prefer=prefer)


# ---------------------------------------------------------------------------
# logging — the task log, and the learning log
# ---------------------------------------------------------------------------

_shots: dict[str, Any] = {"n": None, "dir": None}


def learning_dir() -> Path:
    from . import paths
    return paths.data_root() / "grounding"


def _learn(d: Decision, cap: Capture | None) -> None:
    """One JSON line per decision, with the picture sent, for training."""
    if not config.get("learning_log"):
        return
    try:
        root = learning_dir()
        shots = root / "captures"
        shots.mkdir(parents=True, exist_ok=True)
        rel = ""
        if cap is not None and cap.png:
            if _shots["n"] is None or _shots["dir"] != str(shots):
                _shots["n"] = sum(1 for _ in shots.glob("*.png"))
                _shots["dir"] = str(shots)
            if _shots["n"] < int(config.get("learning_log_max_shots") or 0):
                name = (time.strftime("%Y%m%d-%H%M%S")
                        + f"-{int(time.time() * 1000) % 1000:03d}"
                        + f"-{_shots['n']:05d}.png")
                (shots / name).write_bytes(cap.png)
                _shots["n"] += 1
                rel = f"captures/{name}"
        d.capture_path = rel
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "instruction": d.instruction, "target": d.target,
            "window_title": d.title, "image": rel or None,
            "image_size": list(d.sent_size) if d.sent_size else None,
            "region": d.region or None,
            "model_point": list(d.image_point) if d.image_point else None,
            "model_point_screen": list(d.point) if d.point else None,
            "snapped": d.snapped, "snap_distance": d.snap_distance,
            "readback": d.readback,
            "agreement": {"distance": d.agreement_distance,
                          "pass1_point": list(d.pass1_point) if d.pass1_point else None,
                          "high_risk": d.high_risk},
            "second_look": d.second_look,
            "click_point": list(d.click_point) if d.click_point else None,
            "outcome": d.outcome, "reason": d.reason,
            "verified": d.verified, "verify_reason": d.verify_reason,
            "latency_ms": d.latency_ms, "seconds": d.seconds,
            "model_step": d.model_step,
        }
        with open(root / "decisions.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception as e:                      # a log must never break a click
        _logger.info("learning log failed: %s", e)


def _log(d: Decision, cap: Capture | None) -> None:
    _learn(d, cap)
    rec = d.as_dict()
    recent.append(rec)
    _logger.info("grounding %s", json.dumps(rec, ensure_ascii=False, default=str))
    log = tasklog.current
    if log is not None:
        shot = None
        if cap is not None:
            try:
                shot = log.shot_bytes(cap.png, "png", f"ground {d.instruction[:20]}")
            except Exception:
                shot = None
        tasklog.event("grounding", screenshot=shot, **rec)


# ---------------------------------------------------------------------------
# the fallback
# ---------------------------------------------------------------------------

def _float_or_none(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _point_or_none(v) -> tuple | None:
    try:
        return (int(round(float(v[0]))), int(round(float(v[1]))))
    except (TypeError, ValueError, IndexError, KeyError):
        return None


def click_by_vision(instruction: str, hwnd: int | None,
                    history: list[str] | None = None,
                    target: str | None = None,
                    prefer: str = "invoke") -> Decision:
    """Ground `instruction` in the window and click it — or say why not.

    `target` is the bare name the read-back compares against (derived from
    the instruction when omitted). `prefer` is how a snapped UIA element is
    clicked ("invoke" = pattern first, as for any named click); a point with
    no UIA element behind it is always an injected click.

    Never raises: a decision comes back whatever happened, with `outcome`
    saying which gate stopped it. The caller reports a non-click as the
    miss it is; only `clicked` means input went out.
    """
    t0 = time.time()
    cfg = config
    d = Decision(instruction=instruction, hwnd=int(hwnd or 0))
    d.target = (target or target_from_instruction(instruction)).strip()
    cap: Capture | None = None

    def done(outcome: str, reason: str = "") -> Decision:
        d.outcome, d.reason = outcome, reason
        d.seconds = round(time.time() - t0, 3)
        if outcome != "disabled":
            _log(d, cap)
        return d

    if not cfg.get("enabled"):
        return done("disabled", "vision_grounding.enabled is false")

    if not cfg.get("allow_risky"):
        term = risky(d.target)
        if term:
            return done("refused", f"{d.target!r} looks risky ({term!r}); a "
                                   f"vision click is not trusted with it "
                                   f"(vision_grounding.allow_risky is false)")

    ok, why = health()
    if not ok:
        return done("unavailable", f"service down ({why})")

    if not hwnd and cfg.get("require_inside_window"):
        return done("refused", "no target window to bound the click "
                               "(require_inside_window)")

    try:
        cap = _capture_window(hwnd)
    except Exception as e:
        return done("unavailable", f"capture failed: {e}")
    d.title, d.region, d.sent_size = cap.title, dict(cap.region), cap.sent_size

    if history is None and history_provider is not None:
        try:
            history = list(history_provider() or [])
        except Exception:
            history = []
    d.history = [str(h) for h in (history or [])][-HISTORY_N:]

    payload = {"image_b64": base64.b64encode(cap.png).decode("ascii"),
               "instruction": instruction}
    if d.history:
        payload["history"] = d.history
    if cfg.get("agreement"):
        payload["agreement"] = True
    try:
        resp = _http(cfg["url"] + "/ground", payload, float(cfg["timeout_s"]))
    except Unavailable as e:
        _mark_down(str(e))
        return done("unavailable", f"service failed ({e})")

    d.raw = str(resp.get("raw") or "")[:400]
    d.latency_ms = resp.get("latency_ms")
    d.model_step = resp.get("model_step")
    d.confidence = _float_or_none(resp.get("confidence"))
    d.agreement_distance = _float_or_none(resp.get("agreement_distance"))
    d.pass1_point = _point_or_none(resp.get("pass1_point"))

    if says_absent(resp):
        return done("not_found", f"the model says {d.target!r} is not in "
                                 f"{cap.title or 'the window'!r}")
    if not resp.get("ok"):
        return done("refused", f"service error: {resp.get('error') or 'unknown'}")
    try:
        x, y = int(resp["x"]), int(resp["y"])
    except (KeyError, TypeError, ValueError):
        return done("refused", "service returned no usable point")
    d.image_point = (x, y)

    # -- gates ---------------------------------------------------------------
    size = resp.get("image_size")
    if size is not None:
        try:
            got = (int(size[0]), int(size[1]))
        except (TypeError, ValueError, IndexError):
            got = None
        if got is not None and got != tuple(cap.sent_size):
            return done("refused", f"the service saw a {got[0]}x{got[1]} picture, "
                                   f"we sent {cap.sent_size[0]}x{cap.sent_size[1]}")
    if not inside_image(x, y, cap.sent_size):
        return done("refused", f"point ({x},{y}) is outside the "
                               f"{cap.sent_size[0]}x{cap.sent_size[1]} picture")

    mc = cfg.get("min_confidence")
    if mc is not None and d.confidence is not None and d.confidence < float(mc):
        return done("refused", f"confidence {d.confidence:.2f} < {float(mc):.2f}")

    sx, sy = map_point(x, y, cap.region, cap.sent_size)
    d.point = (sx, sy)

    now_rect = None
    if cap.hwnd:
        # The model thought for seconds; a window that moved meanwhile makes
        # every pixel of the answer describe the wrong place.
        try:
            now_rect = tuple(_window_rect(cap.hwnd))
        except Exception as e:
            return done("refused", f"the window is gone ({e})")
        if cap.window_rect is not None and now_rect != tuple(cap.window_rect):
            return done("refused", f"the window moved during grounding "
                                   f"({tuple(cap.window_rect)} -> {now_rect})")
        if cfg.get("require_inside_window") and not inside_rect(sx, sy, now_rect):
            return done("refused", f"({sx},{sy}) is outside the window {now_rect}")

    # -- agreement -----------------------------------------------------------
    md = float(cfg.get("max_disagreement", 0.05))
    if d.agreement_distance is not None and d.agreement_distance > md:
        d.high_risk = True
        if not cfg.get("readback"):
            return done("refused", f"high risk: the model's first look and its "
                                   f"answer disagree by {d.agreement_distance:.3f} "
                                   f"(> {md:g}) and read-back is off")

    # -- snap + read-back ----------------------------------------------------
    choice = Choice(None, {"done": False})
    scale = 1.0
    if cfg.get("snap") or cfg.get("readback"):
        scale = _dpi_scale(cap.hwnd)
        radius = (float(cfg.get("snap_radius_px", 40)) * scale
                  if cfg.get("snap") else 0.0)
        try:
            cands = _uia_candidates(cap.hwnd, float(cfg.get("snap_budget_s", 1.0)))
        except Exception as e:
            _logger.info("uia candidates failed: %s", e)
            cands = []
        if cfg.get("detector_fallback") and not any(
                rect_distance(sx, sy, c.rect) <= radius for c in cands):
            try:
                cands = cands + _detector_candidates(cap)
            except Exception as e:
                _logger.info("detector failed: %s", e)

        def read_text(screen_box):
            box = rect_to_image(screen_box, cap.region, cap.sent_size)
            got = _ocr_lines(cap.img, box)
            if got is None:
                return None
            return [(rect_to_screen(r, cap.region, cap.sent_size), t)
                    for r, t in got]

        choice = choose(sx, sy, cands, d.target, radius=radius,
                        readback=bool(cfg.get("readback")),
                        min_ratio=float(cfg.get("readback_min_ratio", 0.8)),
                        allow_unreadable=bool(cfg.get("readback_allow_unreadable")
                                              or cfg.get("second_look")),
                        high_risk=d.high_risk, read_text=read_text,
                        ocr_reach=float(cfg.get("readback_ocr_reach_px", 0)) * scale)
        d.readback = choice.readback
        if choice.cand is not None:
            d.snapped = choice.cand.describe(cap.region, cap.sent_size)
            d.snap_distance = round(choice.distance, 1)
        if choice.refuse:
            return done("not_found", choice.refuse)

    cand = choice.cand
    use_el = cand is not None and bool(cfg.get("snap"))
    if use_el:
        crect = tuple(int(v) for v in cand.rect)
        cx, cy = rect_center(crect)
    else:
        crect = (sx - 1, sy - 1, sx + 1, sy + 1)
        cx, cy = sx, sy
    d.click_point, d.click_rect = (cx, cy), crect

    if cand is not None and not cfg.get("allow_risky"):
        label = cand.name or cand.ocr_text
        term = risky(label)
        if term:
            return done("refused", f"the element there is {label!r}, which "
                                   f"looks risky ({term!r})")
    if now_rect is not None and cfg.get("require_inside_window") \
            and not inside_rect(cx, cy, now_rect):
        return done("refused", f"the snapped point ({cx},{cy}) is outside the "
                               f"window {now_rect}")

    # Nothing readable was there and choose() let it through only so that it
    # can be checked here: unless the user allows unreadable clicks outright,
    # the model must point at the same place again on a closer look.
    rb = d.readback or {}
    if (rb.get("done") and rb.get("ok") is None and cfg.get("second_look")
            and not cfg.get("readback_allow_unreadable")):
        agrees, why, d.second_look = second_look(
            cap, instruction, d.history, (cx, cy), crect if use_el else None,
            max(radius, 40.0 * scale))
        rb.update(ok=bool(agrees), source="second_look", reason=why)
        if not agrees:
            return done("not_found", f"nothing readable at the model's point; {why}")

    label = (cand.name or cand.ocr_text) if use_el else ""
    d.accepted = True
    if cfg.get("dry_run"):
        d.method = "dry_run"
        on = f" on {label!r}" if label else ""
        return done("dry_run", f"dry run: would click ({cx},{cy}){on} in "
                               f"{cap.title or 'the screen'!r}")

    from .locator import Element
    conf = d.confidence if d.confidence is not None else 0.0
    if use_el:
        el = Element(rect=crect, name=cand.name or d.target,
                     control_type=cand.control_type,
                     automation_id=cand.automation_id, hwnd=cap.hwnd,
                     source="vision:ground", control=cand.control,
                     confidence=conf)
        how = prefer if cand.control is not None else "inject"
    else:
        el = Element(rect=crect, name=instruction, source="vision:ground",
                     hwnd=cap.hwnd, confidence=conf)
        how = "inject"

    before = None
    if cfg.get("verify"):
        try:
            before = _observe(cand if use_el else None, crect, cap.hwnd)
        except Exception as e:
            d.verify_reason = f"no baseline ({type(e).__name__}: {e})"
    try:
        r = _act(el, how)
    except Exception as e:
        return done("not_clicked", f"actuator raised {type(e).__name__}: {e}")
    d.method = r.method
    if not r.ok:
        return done("not_clicked", f"actuator refused: {r.detail or r.method}")
    d.clicked = True
    if cfg.get("verify") and before is not None:
        d.verified, d.verify_reason = _verify(
            cand if use_el else None, crect, cap.hwnd, before,
            float(cfg.get("verify_timeout_s", 1.5)))
    on = f" on {label!r}" if label else ""
    tail = ""
    if d.verified is True:
        tail = f"; verified: {d.verify_reason}"
    elif d.verified is False:
        tail = f"; UNVERIFIED: {d.verify_reason}"
    return done("clicked", f"clicked ({cx},{cy}){on} in {cap.title!r} via "
                           f"{r.method}{tail}")
