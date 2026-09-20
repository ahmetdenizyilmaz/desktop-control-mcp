"""The agent's pointer — one model, two views.

A cartoon arrow with a coloured halo that says what the agent is doing on the
one pixel that matters: blue thinking, red touching the machine, orange
waiting on a person — the same three states as the screen border. It squashes
while a button is held, sends a ripple out from where the button came up, and
trails a few fading ghosts while dragging, so a click reads as a click and a
drag as a drag even at fifteen frames a second.

This module holds the STATE (colour, button, recent positions) and RENDERS
the sprite; it opens no window. overlay.PointerOverlay paints it on screen in
place of the real arrow, and screencast draws the same sprite into the
recording — so what the person saw and what the video shows are one picture.

Any poller may feed it (`sample`); the overlay does at fifty a second while
it is up, the recorder at its frame rate whether or not the overlay is. A
button transition is detected by whichever arrives first, and the trail just
collects positions, so two feeders never disagree.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import math
import threading
import time
from collections import deque

#: Half the sprite canvas, in pixels at scale 1. Room for the halo, a full
#: ripple and a short trail; small enough that uploading it fifty times a
#: second costs nothing worth measuring.
R = 80
GLOW_R = 20
RIPPLE_S = 0.38
TRAIL_S = 0.16
DRAG_PX = 6
PULSE_S = 1.8
PHASES = 12

#: The arrow, tip at the origin. Its corners are rounded at render time
#: (the tip is kept sharp — it has to point at something), and its outline
#: carries the state colour as a breathing glow. The halo behind it is only
#: a hint; the outline is the signal.
ARROW = [(0, 0), (0, 19), (4.5, 15), (7.5, 22), (11, 20.5), (8, 13.5), (14, 13.5)]
ARROW_FILL = (250, 250, 252, 255)
ARROW_LINE_IDLE = (60, 66, 80)
#: Ring and trail colour when no state is set (an idle recording).
IDLE_RING = (200, 205, 215)

VK_LBUTTON = 0x01
_user32 = ctypes.windll.user32
_user32.GetAsyncKeyState.restype = ctypes.c_short


def cursor_pos() -> tuple[int, int] | None:
    """Pointer position in the caller's coordinate space — physical pixels
    on a DPI-aware thread, which every poller here is."""
    pt = ctypes.wintypes.POINT()
    if _user32.GetCursorPos(ctypes.byref(pt)):
        return pt.x, pt.y
    return None


def button_down() -> bool:
    return bool(_user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)


# -- state -------------------------------------------------------------------

_lock = threading.Lock()
_color: tuple[int, int, int] | None = None
_pressed = False
_press_pos = (0, 0)
_release_at = 0.0
_release_pos = (0, 0)
_trail: deque = deque(maxlen=24)
_dragging = False


def set_color(rgb) -> None:
    """The halo colour, or None for no halo."""
    global _color
    with _lock:
        _color = tuple(int(c) for c in rgb) if rgb else None


def sample(x: int, y: int, pressed: bool, now: float | None = None) -> None:
    """Feed one observation of the pointer."""
    global _pressed, _press_pos, _release_at, _release_pos, _dragging
    now = now or time.time()
    with _lock:
        if pressed and not _pressed:
            _press_pos = (x, y)
            _dragging = False
            _trail.clear()
        elif not pressed and _pressed:
            _release_at = now
            _release_pos = (x, y)
            _dragging = False
            _trail.clear()
        if pressed:
            if not _dragging and (abs(x - _press_pos[0]) >= DRAG_PX
                                  or abs(y - _press_pos[1]) >= DRAG_PX):
                _dragging = True
            if not _trail or _trail[-1][1:] != (x, y):
                _trail.append((now, x, y))
        _pressed = pressed


def snapshot(x: int, y: int, now: float | None = None,
             offset_scale: float = 1.0) -> dict:
    """Everything the renderer needs, with positions turned into offsets from
    the pointer — scaled, because a recording's pixels are not the screen's."""
    now = now or time.time()
    with _lock:
        ripple = now - _release_at
        trail = ([(now - t, (tx - x) * offset_scale, (ty - y) * offset_scale)
                  for (t, tx, ty) in _trail if now - t <= TRAIL_S]
                 if _dragging else [])
        return {
            "color": _color,
            "pressed": _pressed,
            "dragging": _dragging,
            "ripple": ripple if ripple <= RIPPLE_S else None,
            "ripple_off": ((_release_pos[0] - x) * offset_scale,
                           (_release_pos[1] - y) * offset_scale),
            "trail": trail,
            "phase": int((now % PULSE_S) / PULSE_S * PHASES),
        }


def key(snap: dict) -> tuple:
    """Everything that changes the picture, so a poller can skip a repaint."""
    rip = None if snap["ripple"] is None else (int(snap["ripple"] * 60),
                                                snap["ripple_off"])
    return (snap["color"], snap["pressed"], snap["dragging"], snap["phase"], rip,
            tuple((int(a * 60), int(dx), int(dy)) for a, dx, dy in snap["trail"]))


# -- rendering ---------------------------------------------------------------

def canvas_size(scale: float) -> int:
    return max(16, int(round(2 * R * scale)))


_glow_cache: dict = {}
_arrow_cache: dict = {}


def _glow_phases(color, scale: float) -> list:
    """Twelve halos, one per breath step, built once per colour and scale."""
    k = (color, round(scale, 3))
    got = _glow_cache.get(k)
    if got is not None:
        return got
    import numpy as np
    from PIL import Image
    S = canvas_size(scale)
    c = S // 2
    yy, xx = np.mgrid[0:S, 0:S]
    d = np.hypot(xx - c, yy - c) / (GLOW_R * scale)
    # A gentler-than-square falloff keeps a visible ring around the arrow,
    # which is where the eye actually looks — the centre is under it.
    falloff = np.clip(1.0 - d, 0.0, 1.0) ** 1.7
    out = []
    for p in range(PHASES):
        breath = 0.82 + 0.18 * (0.5 + 0.5 * math.sin(2 * math.pi * p / PHASES))
        img = np.zeros((S, S, 4), np.uint8)
        img[..., 0], img[..., 1], img[..., 2] = color
        img[..., 3] = (falloff * 70 * breath).astype(np.uint8)
        out.append(Image.fromarray(img))
    _glow_cache[k] = out
    return out


def _rounded(pts: list, passes: int = 2) -> list:
    """Chaikin corner-cutting on an open polyline: every corner softens, the
    two endpoints stay put. The outline runs tip -> around -> tip, so the
    tip is both endpoints and keeps its point."""
    for _ in range(passes):
        out = [pts[0]]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            out.append((0.75 * x0 + 0.25 * x1, 0.75 * y0 + 0.25 * y1))
            out.append((0.25 * x0 + 0.75 * x1, 0.25 * y0 + 0.75 * y1))
        out.append(pts[-1])
        pts = out
    return pts


def _arrow(scale: float, pressed: bool, color, phase: int):
    """The arrow sprite for one state and breath step, and where its tip
    sits inside it. Cached: a few dozen small images per scale."""
    k = (round(scale, 3), pressed, color, phase if color else 0)
    got = _arrow_cache.get(k)
    if got is not None:
        return got
    from PIL import Image, ImageDraw, ImageFilter
    SS = 3                                   # supersample, then shrink
    f = scale * SS * (0.88 if pressed else 1.0)
    pad = int(6 * scale * SS) + 2            # room for the outline glow
    pts = _rounded([(pad + x * f, pad + y * f) for x, y in ARROW + [ARROW[0]]])
    w = int(max(x for x, _ in pts) + pad + 2)
    h = int(max(y for _, y in pts) + pad + 2)
    line = tuple(color) if color else ARROW_LINE_IDLE

    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if color:
        # The glow: the outline again, thick and blurred, breathing with the
        # phase — solid while a button is held.
        breath = 1.0 if pressed else 0.5 + 0.5 * (0.5 + 0.5 * math.sin(
            2 * math.pi * phase / PHASES))
        glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(glow).line(pts, fill=line + (255,),
                                  width=max(2, int(5 * scale * SS)), joint="curve")
        glow = glow.filter(ImageFilter.GaussianBlur(radius=3 * scale * SS))
        glow.putalpha(glow.getchannel("A").point(lambda v: int(v * 0.9 * breath)))
        canvas.alpha_composite(glow)

    body = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(body)
    d.polygon(pts, fill=ARROW_FILL)
    d.line(pts, fill=line + (255,), width=max(1, int(2.2 * scale * SS)),
           joint="curve")
    canvas.alpha_composite(body)

    small = canvas.resize((max(1, w // SS), max(1, h // SS)), Image.LANCZOS)
    got = (small, pad // SS)
    _arrow_cache[k] = got
    return got


def render(snap: dict, scale: float = 1.0):
    """The sprite for one moment: an RGBA image with the tip at its centre."""
    from PIL import Image, ImageDraw
    S = canvas_size(scale)
    c = S // 2
    color = snap["color"]
    if color:
        canvas = _glow_phases(color, scale)[snap["phase"]].copy()
        if snap["pressed"]:
            # Brighter while held — the halo is the button.
            canvas.putalpha(canvas.getchannel("A").point(
                lambda v: min(255, int(v * 1.45))))
    else:
        canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    ring = tuple(min(255, v + 60) for v in color) if color else IDLE_RING
    fx = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(fx)
    # Drag: ghosts behind the pointer, fading with age.
    for age, dx, dy in snap["trail"]:
        f = 1.0 - age / TRAIL_S
        r = max(1.0, 4 * scale * f)
        cx, cy = c + dx, c + dy
        if -r <= cx <= S + r and -r <= cy <= S + r:
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=ring + (int(150 * f),))
    # Click: a ring growing out of where the button came up, and staying
    # there — the pointer may already have moved on.
    if snap["ripple"] is not None:
        f = snap["ripple"] / RIPPLE_S
        r = (5 + 26 * f) * scale
        ox, oy = snap["ripple_off"]
        cx, cy = c + ox, c + oy
        d.ellipse([cx - r, cy - r, cx + r, cy + r],
                  outline=ring + (int(220 * (1 - f)),),
                  width=max(1, int(2 * scale * (1 - 0.5 * f))))
    canvas.alpha_composite(fx)

    arrow, tip = _arrow(scale, snap["pressed"], color, snap["phase"])
    canvas.alpha_composite(arrow, (c - tip, c - tip))
    return canvas


def composite(frame, sx: int, sy: int, fx: int, fy: int, frame_scale: float,
              sprite_scale: float, now: float | None = None) -> None:
    """Draw the pointer onto a BGR numpy frame with its tip at (fx, fy).

    (sx, sy) is the pointer on screen — offsets for the ripple and trail are
    measured there and scaled into the frame by frame_scale. The sprite's own
    size follows sprite_scale, which the recorder floors so a half-size video
    still has a pointer you can see.
    """
    import numpy as np
    snap = snapshot(sx, sy, now, offset_scale=frame_scale)
    spr = render(snap, sprite_scale)
    S = spr.size[0]
    c = S // 2
    x0, y0 = fx - c, fy - c
    H, W = frame.shape[:2]
    X0, Y0 = max(0, x0), max(0, y0)
    X1, Y1 = min(W, x0 + S), min(H, y0 + S)
    if X1 <= X0 or Y1 <= Y0:
        return
    src = np.asarray(spr)[Y0 - y0:Y1 - y0, X0 - x0:X1 - x0].astype(np.uint16)
    a = src[..., 3:4]
    region = frame[Y0:Y1, X0:X1].astype(np.uint16)
    bgr = src[..., [2, 1, 0]]
    frame[Y0:Y1, X0:X1] = ((bgr * a + region * (255 - a)) // 255).astype(np.uint8)
