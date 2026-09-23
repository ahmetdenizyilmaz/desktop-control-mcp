"""A cursor the agent draws for itself: two black wings, angled by speed.

**Two rounded-corner triangles, solid black, sharing one corner.** The
angle between them tracks how fast the pointer is moving: closed to a
hairline at rest, splayed into a wide V at speed. The shared corner is
both the pivot and the hot spot, so the point being indicated never moves
however far the wings open.

    with hidden_cursor():
        c = Cursor(); c.start()
        c.move_to(x, y)

**Why draw one at all.** Most of what the agent does is invisible: a UIA
`Invoke` activates a button without moving the mouse, typing goes to
whatever holds focus, a targeted query resolves an element with no pointer
involved. Watching the screen you see results appear with no cause. This
goes to whatever is about to be acted on, real mouse or not.

**Why an overlay rather than a system cursor.** `SetSystemCursor` takes a
fixed set of frames and applies them to every application until restored —
there is no way to express an angle that changes with speed. The overlay
is a layered click-through window we repaint ourselves, and it goes away
when the process does. (`hide_system_cursor` is still here, to blank the
real pointer while ours is up; it is global and MUST be paired with
`restore_system_cursor`, which `hidden_cursor` guarantees.)

**Measured before building it.** One frame renders in **1.4 ms** at 96px —
4% of a 30 fps budget. Actual rate is ~37 fps, limited by WM_TIMER
granularity rather than by drawing.

**It hides from our own eyes.** `SetWindowDisplayAffinity(WDA_EXCLUDEFROM-
CAPTURE)` keeps it out of screen captures, which matters because this
package READS the screen with OCR — a cursor drawn over a page would be
merged into the scene as page content. Checked at start-up, not assumed:
see `self.hidden_from_capture`.

**ctypes discipline.** WPARAM is `c_size_t`, LPARAM `c_ssize_t`, every
prototype declared, all on *private* `WinDLL` instances. Each rule comes
from a bug this project already paid for: an undeclared handle truncates
to 32 bits, a wrong LPARAM raises inside the window procedure where the
traceback is swallowed, and setting argtypes on the shared
`ctypes.windll.user32` imposes them on every other caller in the process.
"""

from __future__ import annotations

import ctypes
import math
import threading
import time
from collections import deque
from ctypes import wintypes

# Private instances. Setting argtypes on ctypes.windll.user32 would impose
# them on every other user32 caller in the process.
_u = ctypes.WinDLL("user32", use_last_error=True)
_g = ctypes.WinDLL("gdi32", use_last_error=True)
_k = ctypes.WinDLL("kernel32", use_last_error=True)

#: WPARAM is pointer-sized UNSIGNED, LPARAM pointer-sized SIGNED. Getting
#: this wrong raises "int too long to convert" from inside the window
#: procedure, where the traceback is swallowed and the window simply never
#: works.
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             ctypes.c_size_t, ctypes.c_ssize_t)

WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x00080000, 0x00000020
WS_EX_TOPMOST, WS_EX_TOOLWINDOW = 0x00000008, 0x00000080
WS_EX_NOACTIVATE = 0x08000000
SW_SHOWNA, SW_HIDE = 8, 0
HWND_TOPMOST = -1
SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = 0x0002, 0x0001, 0x0010
WM_DESTROY, WM_TIMER = 0x0002, 0x0113
MSG_SHOW, MSG_HIDE, MSG_STOP = 0x0400 + 1, 0x0400 + 2, 0x0400 + 3
ULW_ALPHA, AC_SRC_OVER, AC_SRC_ALPHA = 0x02, 0x00, 0x01
DIB_RGB_COLORS = 0
WDA_NONE, WDA_EXCLUDEFROMCAPTURE = 0x00, 0x11
TIMER_ID = 1


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte),
                ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


#: The system cursors worth blanking. Leaving any one of them alone means
#: the real pointer flashes back the moment it crosses a text field or a
#: link, which is worse than not hiding it at all.
OCR_IDS = (32512,  # NORMAL
           32513,  # IBEAM
           32649,  # HAND
           32650,  # APPSTARTING
           32514,  # WAIT
           32515,  # CROSS
           32646,  # SIZEALL
           32642, 32643, 32644, 32645)  # SIZE NWSE / NESW / WE / NS
SPI_SETCURSORS = 0x0057


def _declare() -> None:
    _u.DefWindowProcW.restype = ctypes.c_ssize_t
    _u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  ctypes.c_size_t, ctypes.c_ssize_t]
    _u.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
    _u.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    _u.CreateWindowExW.restype = wintypes.HWND
    _u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR,
                                   wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                   wintypes.HINSTANCE, ctypes.c_void_p]
    _u.DestroyWindow.argtypes = [wintypes.HWND]
    _u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                wintypes.UINT]
    _u.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT,
                            ctypes.c_void_p]
    _u.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
    _u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                ctypes.c_size_t, ctypes.c_ssize_t]
    _u.GetMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND,
                               wintypes.UINT, wintypes.UINT]
    _u.GetDC.restype = wintypes.HDC
    _u.GetDC.argtypes = [wintypes.HWND]
    _u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    _u.UpdateLayeredWindow.restype = wintypes.BOOL
    _u.UpdateLayeredWindow.argtypes = [
        wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
        ctypes.POINTER(wintypes.SIZE), wintypes.HDC,
        ctypes.POINTER(wintypes.POINT), wintypes.COLORREF,
        ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
    _u.SetWindowDisplayAffinity.restype = wintypes.BOOL
    _u.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    _u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    _g.CreateCompatibleDC.restype = wintypes.HDC
    _g.CreateCompatibleDC.argtypes = [wintypes.HDC]
    _g.CreateDIBSection.restype = wintypes.HBITMAP
    _g.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFO),
                                    wintypes.UINT,
                                    ctypes.POINTER(ctypes.c_void_p),
                                    wintypes.HANDLE, wintypes.DWORD]
    _g.SelectObject.restype = wintypes.HGDIOBJ
    _g.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    _g.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    _g.DeleteDC.argtypes = [wintypes.HDC]
    _u.CreateCursor.restype = wintypes.HANDLE
    _u.CreateCursor.argtypes = [wintypes.HINSTANCE, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                                ctypes.c_void_p]
    _u.SetSystemCursor.restype = wintypes.BOOL
    _u.SetSystemCursor.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _u.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT,
                                         ctypes.c_void_p, wintypes.UINT]
    _k.GetModuleHandleW.restype = wintypes.HMODULE
    _k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]


_declare()


_declare()


def hide_system_cursor() -> bool:
    """Blank every standard pointer, system-wide.

    `SetSystemCursor` replaces the shape for EVERY application, not just
    ours, and it CONSUMES the handle it is given — so each id needs its
    own freshly created cursor. A 32x32 cursor whose AND mask is all ones
    and XOR mask all zeros leaves the screen untouched wherever it is
    drawn, which is how you get nothing rather than a black square.

    This is a change to the user's machine and it does not undo itself.
    Every caller must pair it with `restore_system_cursor()` in a
    `finally`, and `hidden_cursor()` below does exactly that.
    """
    hinst = _k.GetModuleHandleW(None)
    ok = True
    for which in OCR_IDS:
        and_mask = (ctypes.c_ubyte * 128)(*([0xFF] * 128))
        xor_mask = (ctypes.c_ubyte * 128)()
        h = _u.CreateCursor(hinst, 0, 0, 32, 32,
                            ctypes.cast(and_mask, ctypes.c_void_p),
                            ctypes.cast(xor_mask, ctypes.c_void_p))
        ok = bool(h and _u.SetSystemCursor(h, which)) and ok
    return ok


def restore_system_cursor() -> None:
    """The user's own pointer scheme, back from the registry."""
    try:
        _u.SystemParametersInfoW(SPI_SETCURSORS, 0, None, 0)
    except Exception:
        pass


class hidden_cursor:
    """`with hidden_cursor():` — restores even if the block raises.

    Also registered with atexit, because a process that dies between the
    hide and the restore leaves the machine with no pointer at all.
    """

    def __enter__(self):
        import atexit
        atexit.register(restore_system_cursor)
        self.ok = hide_system_cursor()
        return self

    def __exit__(self, *exc):
        restore_system_cursor()
        return False


# --------------------------------------------------------------------------
# The shape: two rounded triangles that share a corner
# --------------------------------------------------------------------------

#: One triangle, apex at the origin. The apex is the HOT SPOT — the pixel
#: the cursor is pointing at — and it is also the pivot both triangles
#: rotate about, which is what keeps them joined at a single point however
#: far they splay.
#:
#: The inner edge runs straight down from the apex, so at zero spread the
#: two mirrored triangles meet edge-to-edge and read as one arrowhead.
#: Everything else is the outer edge bulging away.
TRI_W = 13.0
TRI_H = 34.0

#: How far each wing's cross corner reaches past the centre line.
#:
#: Two limits, and the answer sits exactly on the tighter one. Too little
#: and the wings stop meeting, leaving a notch or a gap. Too MUCH and the
#: cross corner pokes out past the OTHER wing's outer edge: at 10 it sat
#: at x=-10 where that edge only reaches -8.9, and the rounding turned the
#: 1.1-unit overhang into a visible bump halfway down each side.
#:
#: Staying inside means ov <= W * n / H (the other wing's edge at the
#: notch height). Substituting the isosceles notch depth below and solving
#: for the largest ov that still satisfies it:
#:
#:     ov = W (W^2 + H^2) / (2 (H^2 - W^2))
#:
#: A hair under that, so the rounded corner lands inside the silhouette
#: rather than on its edge.
OVERLAP = 0.98 * TRI_W * (TRI_W ** 2 + TRI_H ** 2) / (2 * (TRI_H ** 2 - TRI_W ** 2))

#: Depth of the cross corner, where the two wings meet. Pulling it UP
#: toward the tip is what turns a plain triangle into an arrowhead: the
#: union of the two wings then has a notch in its base instead of a flat
#: edge.
#:
#: Solved rather than chosen, so each wing is a genuine ISOSCELES
#: triangle — |tip->cross| == |cross->outer|:
#:
#:     ov^2 + n^2 == (W+ov)^2 + (H-n)^2
#:     n == ((W+ov)^2 + H^2 - ov^2) / (2H)
#:
#: It also RELAXES the gapless constraint, which is the happy part: the
#: cross corner now traces a smaller circle about the apex, so it stays
#: over the centre line to a wider splay than a full-depth corner could.
NOTCH_H = ((TRI_W + OVERLAP) ** 2 + TRI_H ** 2 - OVERLAP ** 2) / (2 * TRI_H)

TRIANGLE = [(0.0, 0.0), (-OVERLAP, NOTCH_H), (TRI_W, TRI_H)]

#: Corner radius PER VERTEX, in the same units, in the order the triangle
#: lists them: apex, inner base, outer base.
#:
#: A fillet cannot exceed half its shortest adjoining edge, so these are
#: capped by the geometry: raising a value past that does nothing at all,
#: silently. With the isosceles wing the two short sides are ~25 units,
#: so ~12 is the ceiling for the corners between them.
#:
#: All three equal, as asked. Rounding the apex this much pulls the
#: visible point back from the hot spot, which TIP_INSET below corrects.
CORNER_R = (11.0, 11.0, 11.0)

#: How far apart the two halves lean, in degrees, at rest and at full
#: speed. Rest is ZERO: the two triangles meet edge-to-edge and read as one
#: solid arrowhead, with no sliver of background between them.
SPREAD_REST = 0.0

#: The widest the wings may open WITHOUT a wedge of background appearing
#: between them, derived rather than chosen.
#:
#: Each wing pivots on the apex, so its inner-bottom corner traces a
#: circle. That corner starts at x = -OVERLAP and reaches the centre line
#: when OVERLAP*cos(s/2) == TRI_H*sin(s/2) — i.e. at s = 2*atan(OVERLAP /
#: TRI_H). Past that the two inner edges stop crossing and the gap opens.
#: Hard-coding 58 degrees was exactly this mistake: it looked right in a
#: still and split apart the moment the cursor moved quickly.
SPREAD_MAX_GAPLESS = 2.0 * math.degrees(math.atan(OVERLAP / NOTCH_H))
SPREAD_FAST = SPREAD_MAX_GAPLESS - 2.0

#: Pointer speed, px/s, that counts as "full speed". Chosen for a 2880px
#: screen: a deliberate drag across a window lands around 1200-1800.
SPEED_FULL = 2200.0

#: Below this the wings stay shut. Without a dead zone the shape is never
#: actually closed — measured on a slow wander, mean spread was 16 degrees
#: with a square-root curve, so it sat permanently half-open and the
#: response to speed could not be seen at all. The closed state has to be
#: the DEFAULT for the open one to mean anything.
SPEED_DEAD = 130.0

#: Heading is only believed above this. Direction computed from a 5px
#: jitter is noise, and a shape that spins while standing still is worse
#: than one that does not turn at all.
SPEED_HEADING = 90.0

#: Seconds of movement history used to measure speed. Short enough to feel
#: immediate, long enough not to jitter on a single stray sample.
SPEED_WINDOW = 0.12

#: Idle orbit. After IDLE_AFTER seconds without movement the two wings
#: slide apart and circle each other about their shared centre, easing up
#: to IDLE_DEG_PER_S over IDLE_RAMP_S so they do not lurch into motion.
#: Negative is COUNTER-clockwise: screen y points down, so a positive angle
#: turns clockwise on screen.
IDLE_AFTER = 0.5
IDLE_RAMP_S = 0.8
IDLE_DEG_PER_S = -75.0

#: Direction and speed of each idle shape's turn about the cursor's
#: centre. Four entries: each wing and then a COPY of it that turns the
#: other way. The two originals turn against each other too, and no two
#: share a speed, so the four keep meeting at new angles instead of
#: repeating one figure. With two entries (one per wing) there are no
#: copies. (Tried and dropped along the way: four
#: counter-rotating copies — a black lump; a same-direction orbit with a
#: gap; the pair merged into a spinning disc; wings flipping in turns.)
IDLE_COPIES = ((+1, 1.25), (-1, 0.95),    # wing 0, then its copy
               (-1, 0.80), (+1, 1.10))    # wing 1, then its copy

#: When movement resumes, the spin unwinds toward zero by this fraction per
#: frame, always the SHORT way round. It must not unwind the whole
#: accumulated angle: ten seconds idle is 500 degrees, and spinning back
#: through all of it would be a second, faster, backwards animation on top
#: of the movement.
IDLE_RETURN = 0.2

#: Idle centre circle: black with the same red OUTLINE_PX border, at the
#: point the wings revolve about, in the SAME layers as the wings (ring at
#: the back, disc at the front). Radius in shape units; it breathes
#: smoothly between BREATH_MIN and BREATH_MAX of it every BREATH_S seconds, and
#: grows in / shrinks away with the idle amount so a moving cursor is the
#: plain arrow.
CIRCLE_R = 13.65
BREATH_MIN = 0.85
BREATH_MAX = 1.00
BREATH_S = 2.4

#: The idle animation in use: BREATHING as if the cursor were speeding up
#: and slowing down. The wings SPLAY — exactly the motion speed gives them —
#: opening to BREATH_SPREAD degrees and closing again once every BREATH_S
#: seconds, eased like an in-and-out breath. No spin, no copies, no circle,
#: no sliding apart (all tried and dropped; `spun`'s push/spin/merge are
#: still there but no longer driven by the tick).
BREATH_SPREAD = 26.0

#: Unused alternative idle: the wings take turns flipping. One full flip
#: (over and back) lasts IDLE_FLIP_S, then IDLE_FLIP_GAP of stillness
#: before the other wing goes. The orbit / merge machinery below (spin,
#: push, merge in `spun`) is kept but not driven by the tick any more.
IDLE_FLIP_S = 0.9
IDLE_FLIP_GAP = 0.35

#: While idle the wings flip into point symmetry and slide together until
#: their long flat sides meet, making a near-round rounded parallelogram
#: that spins. IDLE_PUSH is then a small extra press INTO each other
#: (negative = inward), which closes the little notches the rounded
#: corners leave at the two ends of the seam. IDLE_PUSH_IN / _OUT are how
#: quickly the "idle amount" driving all of this eases in (slowly, as a
#: settle) and out (faster, so movement is never kept waiting on a
#: decoration).
IDLE_PUSH = -1.0
IDLE_PUSH_IN = 0.06
IDLE_PUSH_OUT = 0.22

#: Red, as asked. Kept as one constant so the whole thing is one edit.
#: The wings themselves.
FILL = (0, 0, 0, 255)

#: A second layer UNDER the wings: each wing grown by exactly OUTLINE_PX
#: screen pixels, in red. Because both outlines sit below both fills, the
#: black of one wing covers the other wing's outline wherever they overlap
#: — so the red shows only around the outside of the combined shape, never
#: as a seam down the middle of it.
OUTLINE = (228, 26, 36, 255)

#: The outline colour says WHO is driving; the black wings never change.
#:
#:   control   red    the app is acting: clicking, typing, moving to a target
#:   thinking  blue   the app is looking, waiting or choosing
#:   manual    green  a person moved the mouse while the app was in charge
MODE_COLOURS = {
    "control": (228, 26, 36),
    "thinking": (40, 110, 235),
    "manual": (30, 175, 75),
}

#: Runner states (see `script.runner._show`) -> mode.
STATE_MODES = {
    "click": "control", "type": "control", "moving": "control",
    "blocked": "control",
    "looking": "thinking", "thinking": "thinking", "waiting": "thinking",
    "idle": "thinking",
}

#: How long green lingers after the last manual movement, and how long
#: after an app action a cursor movement still counts as the APP's own.
#: The second matters because injected clicks move the real pointer too:
#: without it every click the app made would flash green.
MANUAL_HOLD_S = 2.0
INJECT_GRACE_S = 0.6

#: Fraction of the way to the new colour covered per frame. ~0.2 settles
#: in about a quarter of a second at 37 fps: a blend, not a snap.
COLOUR_EASE = 0.2
OUTLINE_PX = 2.0


def _round_corners(pts: list[tuple[float, float]], r, seg: int = 18
                   ) -> list[tuple[float, float]]:
    """Replace each sharp vertex with a quadratic curve through it.

    A true fillet is an arc tangent to both edges; a quadratic Bezier with
    the vertex as its control point is within a pixel of that at this size
    and needs no trigonometry, no sign handling and no special case for a
    reflex corner. The cut-back distance is clamped to half of each
    adjoining edge so a short edge cannot be consumed by its two corners.
    """
    n = len(pts)
    radii = list(r) if isinstance(r, (list, tuple)) else [r] * n
    out: list[tuple[float, float]] = []
    for i in range(n):
        a, b, c = pts[(i - 1) % n], pts[i], pts[(i + 1) % n]
        r = radii[i % len(radii)]
        v1 = (a[0] - b[0], a[1] - b[1])
        v2 = (c[0] - b[0], c[1] - b[1])
        l1 = math.hypot(*v1) or 1.0
        l2 = math.hypot(*v2) or 1.0
        d = min(r, l1 / 2.0, l2 / 2.0)
        p1 = (b[0] + v1[0] / l1 * d, b[1] + v1[1] / l1 * d)
        p2 = (b[0] + v2[0] / l2 * d, b[1] + v2[1] / l2 * d)
        for k in range(seg + 1):
            t = k / seg
            u = 1.0 - t
            out.append((u * u * p1[0] + 2 * u * t * b[0] + t * t * p2[0],
                        u * u * p1[1] + 2 * u * t * b[1] + t * t * p2[1]))
    return out


_TRI_ROUND = _round_corners(TRIANGLE, CORNER_R)

#: How far the rounding pulled the visible tip back from the sharp apex.
#: The hot spot — where a click is aimed and what the wings pivot on — is
#: the sharp apex at the origin, but an 11-unit apex radius leaves the
#: drawn point several pixels short of it, so the arrow visibly points a
#: little BEFORE the thing it is pointing at. Shifting the rounded outline
#: up by exactly this much puts the visible tip back on the hot spot while
#: keeping the rounded look.
TIP_INSET = min(y for _, y in _TRI_ROUND)
_TRI_ROUND = [(x, y - TIP_INSET) for x, y in _TRI_ROUND]


def _rotate(pts, deg: float):
    """About the origin — which is the shared apex, by construction."""
    a = math.radians(deg)
    ca, sa = math.cos(a), math.sin(a)
    return [(x * ca - y * sa, x * sa + y * ca) for x, y in pts]


def wings(spread: float, heading: float = 0.0) -> tuple[list, list]:
    """The two halves, splayed and then turned to face `heading`.

    The splay uses a NEGATIVE half-angle, and the sign is not arbitrary.
    Screen y points DOWN, so a positive angle rotates clockwise, which
    swings the right-hand wing inward — the shape narrows as it speeds up,
    exactly backwards. Rotating the other way opens the V.

    `heading` then turns the whole pair about the same apex, so the point
    stays put and the body swings round behind it. 0 is the shape's
    resting direction, which is straight up.
    """
    half = -spread / 2.0
    right = _rotate(_TRI_ROUND, half)
    left = [(-x, y) for x, y in right]
    if heading:
        left = _rotate(left, heading)
        right = _rotate(right, heading)
    return left, right


def _centroid(pts: list[tuple[float, float]]) -> tuple[float, float]:
    """Area centroid of a closed polygon (shoelace), not the vertex mean.

    The rounded outline has eighteen points on every corner and three
    straight edges; averaging the vertices would drag the "centre" toward
    whichever corner happens to be most densely sampled.
    """
    a = cx = cy = 0.0
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(a) < 1e-9:
        return (sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n)
    return (cx / (3.0 * a), cy / (3.0 * a))


def spun(halves, angle: float, push: float = 0.0, flip: float = 0.0,
         heading: float = 0.0, merge: float = 0.0):
    """Let the two wings circle each other about the cursor's centre.

    Each wing is turned by its own angle about the same point — the middle
    of the combined shape (the mean of the wings' centroids), not the tip —
    and then slid OUTWARD by `push`, along the line from that centre
    through the wing's own centroid. With equal angles the wings stay on
    opposite sides of the centre and revolve as a pair; the push is what
    makes that read as two bodies circling each other rather than the whole
    arrow merely rotating.

    At angle 0 and push 0 this is the plain arrow, joint on the hot spot,
    so the return to movement is continuous: the wings close back in and
    unwind rather than jumping.

    `flip` (0..1) turns the SECOND wing upside down about its own centroid,
    along the arrow's own axis — a card-flip, squashing through a line at
    0.5. At 1 the pair is point-symmetric: each wing is exactly the other
    rotated 180 degrees about the centre. That is the balance a spinning
    pair needs. Left as mirror images, both wings point the same way and
    the orbiting pair looks lopsided. The proof is short: the right wing is
    the left mirrored across the axis, flipping it along the axis mirrors
    it again the other way, and two perpendicular mirrors make a half turn.

    `angle` is one number (applied to both wings) or a per-wing list.
    Outlines are drawn from these same points, so each red border moves
    with its own wing, and `render` keeps all borders under all fills.
    """
    if isinstance(angle, (int, float)):
        angles = [float(angle)] * len(halves)
    else:
        angles = list(angle)
        if len(angles) == 2 * len(halves) and not any(angles):
            # All copies sit exactly on their originals: draw each once.
            angles = [0.0] * len(halves)
        elif len(angles) == 2 * len(halves):
            # Two angles per wing: draw each wing twice, original then copy.
            # At all-zero both copies lie exactly on their originals, so the
            # moving cursor is still the plain arrow and the copies never
            # pop in or out.
            halves = [h for h in halves for _ in (0, 1)]
    # `flip` is one number (the SECOND wing only, the orbit's half-turn into
    # point symmetry) or a per-wing list. Each value is a card-flip phase:
    # 1 is upside down, 2 is a full turn back to normal.
    if isinstance(flip, (int, float)):
        flips = [0.0] * len(halves)
        if len(halves) > 1:
            flips[1] = float(flip)
    else:
        flips = list(flip) + [0.0] * (len(halves) - len(flip))
    if not any(angles) and not push and not any(flips) and not merge:
        return halves
    if any(flips):
        # The arrow's axis, tip toward body, in the current heading.
        hr = math.radians(heading)
        ux, uy = -math.sin(hr), math.cos(hr)
        new = []
        for h, f in zip(halves, flips):
            if not f:
                new.append(h)
                continue
            s = math.cos(math.pi * f)
            fx, fy = _centroid(h)
            flipped = []
            for x, y in h:
                along = (x - fx) * ux + (y - fy) * uy
                k = (s - 1.0) * along
                flipped.append((x + k * ux, y + k * uy))
            new.append(flipped)
        halves = new
    cs = [_centroid(h) for h in halves]
    ox = sum(c[0] for c in cs) / len(cs)
    oy = sum(c[1] for c in cs) / len(cs)
    if merge and len(halves) > 1:
        # Slide the wings together until their LONG flat sides coincide.
        #
        # Once flipped, wing 2 is wing 1 turned 180 degrees about the centre
        # C. Turning wing 1 180 degrees about M, the midpoint of its long
        # edge, instead would lay the copy's long edge exactly on wing 1's —
        # and the two half-turns differ only by a translation of 2(M - C).
        # Splitting that between the wings (each moves M - C toward the
        # other) joins them flat side to flat side, keeps the pair centred
        # on C, and makes the whole a rounded parallelogram: the nearest a
        # pair of triangles comes to a circle.
        #
        # The long edge's straight run is the longest segment of the
        # rounded outline; with equal radii at both its corners, its
        # midpoint is the midpoint of the true edge.
        w0 = halves[0]
        i = max(range(len(w0)),
                key=lambda j: math.dist(w0[j], w0[(j + 1) % len(w0)]))
        mx = (w0[i][0] + w0[(i + 1) % len(w0)][0]) / 2.0
        my = (w0[i][1] + w0[(i + 1) % len(w0)][1]) / 2.0
        tx, ty = (ox - mx) * merge, (oy - my) * merge
        halves = ([[(x + tx, y + ty) for x, y in halves[0]],
                   [(x - tx, y - ty) for x, y in halves[1]]]
                  + list(halves[2:]))
    out = []
    for h, deg in zip(halves, angles):
        a = math.radians(deg)
        ca, sa = math.cos(a), math.sin(a)
        pts = [(ox + (x - ox) * ca - (y - oy) * sa,
                oy + (x - ox) * sa + (y - oy) * ca) for x, y in h]
        if push:
            cx, cy = _centroid(pts)
            dx, dy = cx - ox, cy - oy
            d = math.hypot(dx, dy)
            if d > 1e-6:
                ux, uy = dx / d * push, dy / d * push
                pts = [(x + ux, y + uy) for x, y in pts]
        out.append(pts)
    return out


def heading_for(dx: float, dy: float) -> float:
    """Direction of travel -> rotation, in degrees.

    The shape rests pointing UP, which is atan2(-1, 0) = -90 degrees, so
    the rotation needed is the travel angle plus 90.
    """
    return math.degrees(math.atan2(dy, dx)) + 90.0


def spring(pos: float, target: float, vel: float, omega: float,
           dt: float) -> tuple[float, float]:
    """One step of a critically damped spring, solved EXACTLY.

    Stepping the differential equation numerically (v += a*dt; x += v*dt)
    is only stable while omega*dt stays small, and it fails loudly rather
    than gracefully: at 7.5 Hz and 37 fps, omega*dt = 1.27 and a 1200 px
    step diverged to 3e12 px within twenty frames. Raising stiffness is a
    thing anyone will reasonably want to do, so the integrator has to
    survive it.

    This is the closed-form solution for the critically damped case
    (Game Programming Gems 4), which is unconditionally stable: whatever
    dt and omega are, the result is the exact position and velocity of a
    real spring at that moment. Velocity peaks at t = 1/omega and decays,
    which is the "fast away, gentle arrival" shape wanted here.
    """
    e = math.exp(-omega * dt)
    change = pos - target
    temp = (vel + omega * change) * dt
    return target + (change + temp) * e, (vel - omega * temp) * e


def turn_toward(current: float, want: float, rate: float) -> float:
    """Ease an angle the SHORT way round.

    Interpolating 350 -> 10 numerically sweeps 340 degrees backwards; the
    cursor spins a full turn to move ten degrees. Wrapping the difference
    into (-180, 180] first is the whole fix.
    """
    diff = ((want - current + 180.0) % 360.0) - 180.0
    return current + diff * rate


def spread_for(speed: float) -> float:
    """Speed in px/s -> splay angle.

    Dead zone first, then a slightly convex curve. The first version used a
    square root, which is the obvious choice for "most movement is slow" —
    and it was wrong here for exactly that reason: it lifted every small
    movement to 15-25 degrees, so the wings were always part-open and
    opening further said nothing.
    """
    if speed <= SPEED_DEAD:
        return SPREAD_REST
    f = min(1.0, (speed - SPEED_DEAD) / (SPEED_FULL - SPEED_DEAD)) ** 1.25
    return SPREAD_REST + (SPREAD_FAST - SPREAD_REST) * f


#: Where the apex sits in the canvas, as a fraction of it. DEAD CENTRE,
#: because the body now swings all the way round the apex as the heading
#: changes — anchoring near the top would clip the shape whenever it
#: pointed upward.
ANCHOR = (0.5, 0.5)

CANVAS = 177
SUPERSAMPLE = 4
FPS = 60
#: How stiff the follow is, in Hz, for a CRITICALLY DAMPED spring.
#:
#: The old exponential ease (pos += (target-pos) * k) is smooth in the
#: middle and abrupt at both ends: it starts at full speed and creeps in
#: asymptotically, so a fast target snaps and a slow one drags. A
#: critically damped spring accelerates out of rest and decelerates into
#: the target without overshoot, which is what "smooth whether fast or
#: slow" actually means.
#:
#: A step response peaks in velocity at t = 1/omega and decays from there,
#: so stiffness alone buys "fast out, slow in": at 7.5 Hz the peak lands
#: about 21 ms after a jump — immediate to the eye — and the last third of
#: the travel is all deceleration. 4 Hz was smooth but left it ambling out
#: of the gate.
FOLLOW_HZ = 7.5


#: Click burst: short lines thrown out from the tip, fanned around the way
#: it points, that travel outward, lengthen a little, and fade. Angles are
#: degrees either side of the pointing direction; radii are SCREEN pixels
#: from the click point at the start and end of the burst.
BURST_S = 0.40
BURST_ANGLES = (-56.0, -19.0, 19.0, 56.0)
BURST_R_START = (9.0, 20.0)       # inner, outer
BURST_R_END = (21.0, 35.0)
BURST_W = 2.6                     # line width, screen px, before outline


def _burst_segments(bursts, px: float, ox: float, oy: float):
    """(start, end, width_px, alpha) for every live burst line, in canvas
    pixels. `bursts` holds (dx, dy, phase 0..1, heading) with dx/dy the
    click point's offset from the hot spot in screen pixels."""
    out = []
    for dx, dy, ph, hd in bursts:
        e = 1.0 - (1.0 - ph) ** 2                    # ease out: fast, then slow
        alpha = 1.0 if ph < 0.55 else max(0.0, 1.0 - (ph - 0.55) / 0.45)
        r0 = BURST_R_START[0] + (BURST_R_END[0] - BURST_R_START[0]) * e
        r1 = BURST_R_START[1] + (BURST_R_END[1] - BURST_R_START[1]) * e
        # The shape rests pointing UP (heading 0); its pointing direction is
        # (sin h, -cos h), the opposite of the tip-to-body axis.
        base = math.atan2(-math.cos(math.radians(hd)), math.sin(math.radians(hd)))
        cx, cy = ox + dx * px, oy + dy * px
        for off in BURST_ANGLES:
            a = base + math.radians(off)
            ux, uy = math.cos(a), math.sin(a)
            out.append(((cx + ux * r0 * px, cy + uy * r0 * px),
                        (cx + ux * r1 * px, cy + uy * r1 * px), alpha))
    return out


def _round_line(d, a, b, width: float, fill) -> None:
    """A line with round caps — PIL's own ends are square and read as
    ticks rather than the soft strokes in the reference."""
    w = max(1, int(round(width)))
    d.line([a, b], fill=fill, width=w)
    r = width / 2.0
    for x, y in (a, b):
        d.ellipse([x - r, y - r, x + r, y + r], fill=fill)


def render(spread: float, heading: float = 0.0, scale: float = 1.0,
           colour=FILL, outline=OUTLINE, outline_px: float = OUTLINE_PX,
           spin: float = 0.0, push: float = 0.0, flip: float = 0.0,
           merge: float = 0.0, circle: float = 0.0, bursts=()):
    """One RGBA frame. The shared apex sits at `ANCHOR` of the canvas.

    Two layers, in this order and no other:

      1. both wings grown by `outline_px`, in the outline colour
      2. both wings at true size, in the fill colour

    Drawing each wing's outline-then-fill in turn would be wrong: the
    second wing's red border would land ON TOP of the first wing's black,
    drawing a red seam down the middle of the arrowhead. With every outline
    underneath every fill, the only red left visible is the ring around
    the outside of the combined shape.

    The growth is a stroke centred on the edge, `2 * outline_px` wide: half
    of it falls inside the wing and is covered by the fill, half lands
    outside and is the visible border — exactly `outline_px` of it.
    """
    from PIL import Image, ImageDraw

    size = int(CANVAS * scale)
    big = size * SUPERSAMPLE
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # The body reaches hypot(TRI_W, TRI_H) from the apex and swings all the
    # way round it, so the canvas has to hold that radius in every
    # direction with room for the corner rounding.
    # 88 units across rather than 80: spinning about the centre instead of
    # the tip moves the body up to ~42 units from the anchor, and the
    # canvas grew to match so the drawn size did not change.
    # 104 units across: the petals slide IDLE_PUSH further out than the
    # ~42 units a spin alone reaches. The canvas grew with it (CANVAS) so
    # the drawn size is unchanged.
    k = big / 104.0
    ox, oy = big * ANCHOR[0], big * ANCHOR[1]
    halves = [[(ox + x * k, oy + y * k) for x, y in half]
              for half in spun(list(wings(spread, heading)), spin, push,
                               flip, heading, merge)]

    # The idle circle, centred on the point the wings revolve about: the
    # mean of the two resting wings' centroids, which `spun` uses as its
    # pivot. It joins the SAME two layers as the wings — its red ring in
    # the back layer with theirs, its black disc in the front layer with
    # theirs — so where circle and wings overlap they read as one black
    # shape, and red shows only around the outside of the whole.
    disc = None
    if circle > 0:
        rest = list(wings(spread, heading))
        cs = [_centroid(h) for h in rest]
        cx = ox + sum(p[0] for p in cs) / len(cs) * k
        cy = oy + sum(p[1] for p in cs) / len(cs) * k
        disc = (cx, cy, circle * k)

    # Burst lines join the same two layers: outline strokes at the back,
    # black strokes at the front.
    px = SUPERSAMPLE * scale                    # canvas px per screen px
    segs = _burst_segments(bursts, px, ox, oy) if bursts else []

    if outline is not None and outline_px > 0:
        # Pixels are measured AFTER the downsample, so the stroke is sized
        # in supersampled units: 2 * px * SUPERSAMPLE * scale.
        stroke = max(1, round(2 * outline_px * SUPERSAMPLE * scale))
        for pts in halves:
            d.polygon(pts, fill=outline)
            d.line(pts + [pts[0]], fill=outline, width=stroke, joint="curve")
        if disc:
            cx, cy, r = disc
            g = outline_px * SUPERSAMPLE * scale
            d.ellipse([cx - r - g, cy - r - g, cx + r + g, cy + r + g],
                      fill=outline)
        for a, b, al in segs:
            _round_line(d, a, b, (BURST_W + 2 * outline_px) * px,
                        tuple(outline[:3]) + (int(255 * al),))
    for pts in halves:
        d.polygon(pts, fill=colour)
    if disc:
        cx, cy, r = disc
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=colour)
    for a, b, al in segs:
        _round_line(d, a, b, BURST_W * px,
                    tuple(colour[:3]) + (int(255 * al),))
    return img.resize((size, size), Image.LANCZOS)


class Cursor:
    """The overlay. Every public method is safe to call from any thread."""

    def __init__(self, fps: int = FPS, scale: float = 1.0,
                 hide_from_capture: bool = True,
                 follow_mouse: bool = False) -> None:
        self.hwnd = 0
        self.fps = max(10, min(120, fps))
        self.scale = scale
        self.size = int(CANVAS * scale)
        self.hide_from_capture = hide_from_capture
        #: Track the real pointer instead of being driven by `move_to`.
        #: The spring still applies, so it trails the hardware slightly —
        #: that lag IS the smoothing, and it is why the drawn position is
        #: not where a click would land. Fine for looking at, not a
        #: substitute for the real coordinate.
        self.follow_mouse = follow_mouse
        #: Detect a person moving the mouse ourselves, by watching the real
        #: pointer. A host that has a proper input hook (one that can tell
        #: injected events from physical ones) should turn this off and call
        #: `manual_at` instead: the heuristic here can only guess from
        #: timing.
        self.auto_manual = True
        #: Set at start-up from the actual return value, never assumed.
        self.hidden_from_capture: bool | None = None
        #: Kept because the runner reports it, and because it costs
        #: nothing to record. It does NOT change the drawing: the shape is
        #: black and speed-driven, as specified.
        self._state = "idle"
        #: Colour mode (see MODE_COLOURS) and the colour actually drawn,
        #: which eases toward it.
        self._mode = "thinking"
        self._colour = [float(v) for v in MODE_COLOURS["thinking"]]
        self._manual_until = 0.0
        self._injected_at = 0.0
        self._bursts: list[dict] = []
        self._last_real = None
        self._spread = SPREAD_REST
        self._heading = 0.0
        #: Idle revolution about the cursor's own centre, and how long it
        #: has been still. See IDLE_*.
        #: One angle PER COPY, not a shared one. With different speeds a
        #: shared angle wrapped at 360 would jump every turn (1.3 x 359 is
        #: 467, 1.3 x 0 is 0), and unwinding a shared unbounded angle would
        #: spin every copy back through all the turns it had made.
        self._spins = [0.0] * len(IDLE_COPIES)
        self._still_for = 0.0
        self._idle = 0.0          # 0 moving .. 1 fully idle; scales IDLE_PUSH
        self._flip_t = 0.0        # time into the alternating idle flips
        self._flips = [0.0, 0.0]  # per-wing card-flip phase, 0..2
        self._vel = (0.0, 0.0)
        self._last_t: float | None = None
        self._hist: "deque[tuple[float, float, float]]" = deque(maxlen=64)
        self._target = None      # where we are going
        self._pos = None         # where we are
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._proc = None
        self._memdc = None
        self._hbm = None
        self._bits = ctypes.c_void_p()
        self._t0 = time.time()
        self.frames = 0

    # -- public ------------------------------------------------------------

    def start(self) -> bool:
        if self._thread is not None:
            return self.hwnd != 0
        self._thread = threading.Thread(target=self._run, name="cursor",
                                        daemon=True)
        self._thread.start()
        self._ready.wait(timeout=4.0)
        if self.hwnd:
            _u.PostMessageW(self.hwnd, MSG_SHOW, 0, 0)
        return self.hwnd != 0

    def state(self, name: str) -> None:
        """Record what the agent is doing, and colour the outline by it.

        Runner states map onto modes (STATE_MODES): acting is red, looking
        or waiting is blue. An acting state also opens a short window in
        which real-mouse movement counts as the app's OWN — injected clicks
        move the real pointer, and must not read as a person taking over.
        """
        mode = STATE_MODES.get(name)
        with self._lock:
            self._state = name
            if mode:
                self._mode = mode
            if mode == "control":
                self._injected_at = time.time()

    def mode(self, name: str) -> None:
        """Set the colour mode directly: 'control', 'thinking' or 'manual'.

        'manual' holds for MANUAL_HOLD_S and then returns to whatever the
        app was doing, so a caller that already knows a person took over
        (a Stop key, an input hook) can say so outright.
        """
        if name not in MODE_COLOURS:
            raise ValueError(f"mode must be one of {sorted(MODE_COLOURS)}")
        with self._lock:
            if name == "manual":
                self._manual_until = time.time() + MANUAL_HOLD_S
            else:
                self._mode = name

    def pulse(self) -> None:
        """No-op, kept so callers written against the old shape still run."""

    def move_to(self, x: int, y: int) -> None:
        """Glide to a point — used even when no real mouse moves.

        This is the method that makes an `Invoke` visible: the element was
        activated without a pointer, and the overlay goes there anyway so
        the action has somewhere to have happened.
        """
        with self._lock:
            self._target = (float(x), float(y))
            if self._pos is None:
                self._pos = (float(x), float(y))

    def jump_to(self, x: int, y: int) -> None:
        with self._lock:
            self._target = self._pos = (float(x), float(y))

    def follow_mouse(self) -> None:
        p = wintypes.POINT()
        _u.GetCursorPos(ctypes.byref(p))
        self.move_to(p.x, p.y)

    def show(self) -> None:
        """Show again after `hide`. Starts at the real pointer if it has
        nowhere better to be, rather than wherever it was last left."""
        if self._pos is None:
            p = wintypes.POINT()
            _u.GetCursorPos(ctypes.byref(p))
            self.jump_to(p.x, p.y)
        if self.hwnd:
            _u.PostMessageW(self.hwnd, MSG_SHOW, 0, 0)

    def burst(self, x: int, y: int) -> None:
        """Throw the click lines at (x, y). They fire once the cursor has
        reached that point, so call this as the click is announced."""
        with self._lock:
            self._bursts.append({"x": float(x), "y": float(y), "t0": None,
                                 "armed": time.time(), "ph": 0.0, "hd": 0.0})
            del self._bursts[:-4]          # a click storm must not pile up

    def manual_at(self, x: int | None = None, y: int | None = None) -> None:
        """A PERSON just touched the mouse or keyboard.

        Turns the outline green for MANUAL_HOLD_S and makes the drawn cursor
        follow the real pointer for that long — while the system cursor is
        hidden this is the only pointer the person can see, so it must not
        stay parked on the app's last target while they move.
        """
        with self._lock:
            self._manual_until = time.time() + MANUAL_HOLD_S
            if x is not None and y is not None:
                self._target = (float(x), float(y))
                if self._pos is None:
                    self._pos = self._target

    def hide(self) -> None:
        if self.hwnd:
            _u.PostMessageW(self.hwnd, MSG_HIDE, 0, 0)

    def stop(self) -> None:
        if self.hwnd:
            _u.PostMessageW(self.hwnd, MSG_STOP, 0, 0)
        if self._thread:
            self._thread.join(timeout=2.0)

    # -- the window's own thread -------------------------------------------

    def _tick(self, hwnd) -> None:
        if self.follow_mouse:
            p = wintypes.POINT()
            _u.GetCursorPos(ctypes.byref(p))
            with self._lock:
                self._target = (float(p.x), float(p.y))
                if self._pos is None:
                    self._pos = self._target
        with self._lock:
            target, pos = self._target, self._pos
        if pos is None or target is None:
            return

        now = time.time()
        dt = min(0.05, max(0.004, now - (self._last_t or now - 0.027)))
        self._last_t = now

        # Closed-form critically damped spring, per axis. dt is still
        # clamped, not for stability (this form has none to lose) but so a
        # stalled frame does not teleport the cursor a screen's width.
        w = 2.0 * math.pi * FOLLOW_HZ
        vx, vy = self._vel
        nx, vx = spring(pos[0], target[0], vx, w, dt)
        ny, vy = spring(pos[1], target[1], vy, w, dt)
        if (abs(target[0] - nx) < 0.3 and abs(target[1] - ny) < 0.3
                and math.hypot(vx, vy) < 12.0):
            nx, ny = target
            vx = vy = 0.0
        self._vel = (vx, vy)

        speed = math.hypot(vx, vy)
        want = spread_for(speed)
        self._spread += (want - self._spread) * (0.30 if want > self._spread
                                                 else 0.12)
        if speed > SPEED_HEADING:
            self._heading = turn_toward(self._heading,
                                        heading_for(vx, vy), 0.28)

        # Idle: the wings BREATHE — ease apart and back together — scaled
        # by an idle amount that grows in while still and drains quickly on
        # movement, so a moving cursor is always the joined arrow.
        if speed < SPEED_DEAD * 0.5:
            self._still_for += dt
        else:
            self._still_for = 0.0
        want_idle = 1.0 if self._still_for > IDLE_AFTER else 0.0
        self._idle += (want_idle - self._idle) * (
            IDLE_PUSH_IN if want_idle > self._idle else IDLE_PUSH_OUT)
        if self._idle < 0.002:
            self._idle = 0.0
        breath = 0.5 - 0.5 * math.cos(2.0 * math.pi * now / BREATH_S)

        # Click bursts start when the cursor has ARRIVED at the click point
        # (or after a short wait), not when the click was announced: the
        # announcement comes before the move, and a burst thrown from where
        # the cursor still is would mark the wrong place.
        live = []
        with self._lock:
            for b in self._bursts:
                if b["t0"] is None and (math.dist((nx, ny), (b["x"], b["y"]))
                                        < 8.0 or now - b["armed"] > 0.45):
                    b["t0"], b["hd"] = now, self._heading
                if b["t0"] is None:
                    live.append(b)
                    continue
                ph = (now - b["t0"]) / BURST_S
                if ph < 1.0:
                    b["ph"] = ph
                    live.append(b)
            self._bursts = live
        bursts = [(b["x"] - nx, b["y"] - ny, b["ph"], b["hd"])
                  for b in live if b["t0"] is not None
                  and abs(b["x"] - nx) < 45 and abs(b["y"] - ny) < 45]
        with self._lock:
            self._pos = (nx, ny)

        # Manual interference: the REAL pointer moved while the app drives
        # the drawn one, and not just after the app acted itself. Skipped
        # in follow_mouse mode, where the person is driving on purpose.
        if not self.follow_mouse:
            rp = wintypes.POINT()
            _u.GetCursorPos(ctypes.byref(rp))
            real = (rp.x, rp.y)
            if (self.auto_manual and self._last_real is not None
                    and math.dist(real, self._last_real) > 3.0
                    and now - self._injected_at > INJECT_GRACE_S):
                self._manual_until = now + MANUAL_HOLD_S
            self._last_real = real
            if now < self._manual_until:
                # A person is driving: follow the real pointer until they
                # stop, so they are never left without a visible cursor.
                with self._lock:
                    self._target = (float(real[0]), float(real[1]))
        mode = "manual" if now < self._manual_until else self._mode
        want = MODE_COLOURS[mode]
        self._colour = [c + (w - c) * COLOUR_EASE
                        for c, w in zip(self._colour, want)]
        ring = tuple(int(round(v)) for v in self._colour) + (255,)

        # Idle breath rides on top of the speed splay: whichever is wider
        # wins, so moving off from idle never snaps the wings shut first.
        spread = max(self._spread, BREATH_SPREAD * self._idle * breath)
        img = render(spread, self._heading, self.scale, outline=ring,
                     bursts=bursts)
        self._upload(img)
        self.frames += 1

        dst = wintypes.POINT(int(nx - self.size * ANCHOR[0]),
                             int(ny - self.size * ANCHOR[1]))
        sz = wintypes.SIZE(self.size, self.size)
        src = wintypes.POINT(0, 0)
        bf = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        _u.UpdateLayeredWindow(hwnd, None, ctypes.byref(dst), ctypes.byref(sz),
                               self._memdc, ctypes.byref(src), 0,
                               ctypes.byref(bf), ULW_ALPHA)

    def _upload(self, img) -> None:
        """PIL RGBA -> the premultiplied BGRA the layered window wants."""
        b = img.tobytes("raw", "BGRa")      # 'a' = premultiplied
        ctypes.memmove(self._bits, b, len(b))

    def _run(self) -> None:
        try:
            _u.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            _u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass
        hinst = _k.GetModuleHandleW(None)

        def proc(hwnd, msg, wp, lp):
            if msg == MSG_SHOW:
                _u.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                _u.ShowWindow(hwnd, SW_SHOWNA)
                _u.SetTimer(hwnd, TIMER_ID, int(1000 / self.fps), None)
                return 0
            if msg == MSG_HIDE:
                _u.KillTimer(hwnd, TIMER_ID)
                _u.ShowWindow(hwnd, SW_HIDE)
                return 0
            if msg == WM_TIMER:
                try:
                    self._tick(hwnd)
                except Exception:
                    pass
                return 0
            if msg == MSG_STOP:
                _u.KillTimer(hwnd, TIMER_ID)
                _u.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                _u.PostQuitMessage(0)
                return 0
            return _u.DefWindowProcW(hwnd, msg, wp, lp)

        self._proc = WNDPROC(proc)
        cls = WNDCLASS()
        cls.lpfnWndProc = self._proc
        cls.hInstance = hinst
        cls.lpszClassName = f"PipelineCursor{id(self)}"
        if not _u.RegisterClassW(ctypes.byref(cls)):
            self._ready.set()
            return

        ex = (WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST
              | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        hwnd = _u.CreateWindowExW(ex, cls.lpszClassName, "", WS_POPUP,
                                  0, 0, self.size, self.size,
                                  None, None, hinst, None)
        if not hwnd:
            self._ready.set()
            return

        if self.hide_from_capture:
            # Checked, not assumed: this needs Windows 10 2004+, and on an
            # older build it fails quietly and the cursor WOULD show up in
            # our own OCR.
            self.hidden_from_capture = bool(
                _u.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE))

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = self.size
        bmi.bmiHeader.biHeight = -self.size       # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        screen = _u.GetDC(None)
        self._memdc = _g.CreateCompatibleDC(screen)
        self._hbm = _g.CreateDIBSection(screen, ctypes.byref(bmi),
                                        DIB_RGB_COLORS,
                                        ctypes.byref(self._bits), None, 0)
        _u.ReleaseDC(None, screen)
        if not self._hbm or not self._bits:
            _u.DestroyWindow(hwnd)
            self._ready.set()
            return
        _g.SelectObject(self._memdc, self._hbm)
        self.hwnd = hwnd
        self._ready.set()

        msg = wintypes.MSG()
        while _u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _u.TranslateMessage(ctypes.byref(msg))
            _u.DispatchMessageW(ctypes.byref(msg))

        if self._hbm:
            _g.DeleteObject(self._hbm)
        if self._memdc:
            _g.DeleteDC(self._memdc)
        try:
            _u.UnregisterClassW(cls.lpszClassName, hinst)
        except Exception:
            pass
        self.hwnd = 0


if __name__ == "__main__":
    import random

    c = Cursor()
    print("started:", c.start())
    print("hidden from capture:", c.hidden_from_capture)
    c.jump_to(900, 600)
    time.sleep(0.6)
    # Slow drift, then fast flicks: the wings should barely part, then
    # snap wide open and settle.
    for _ in range(3):
        for _ in range(14):
            c.move_to(900 + random.randint(-40, 40),
                      600 + random.randint(-40, 40))
            time.sleep(0.09)
        for _ in range(4):
            c.move_to(random.randint(300, 2400), random.randint(300, 1300))
            time.sleep(0.42)
    print("frames drawn:", c.frames)
    c.stop()
    print("stopped")
