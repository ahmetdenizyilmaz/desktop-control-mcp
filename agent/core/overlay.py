"""A screen-wide coloured border that says what the agent is doing right now.

A top-most, click-through window that draws a soft frame around the whole
virtual desktop and nothing in the middle. It exists so the signal is visible
even when the app itself is behind whatever the agent is working in. The
COLOUR is the message, set from the app as its state changes:

  BLUE    thinking, or working internally — the browser, files, MCP, memory
  RED     touching the machine — mouse, keyboard, screenshots, the UI tree
  ORANGE  waiting for a person to answer

Two properties make it safe to leave on top of everything:

  * click-through — WS_EX_TRANSPARENT, so the frame never catches a click
    meant for the window under it, nor one of the agent's own injected clicks;
  * a window REGION shaped like a frame — the middle of the screen is a true
    hole, not a transparent pixel, so there is nothing there to hit or repaint.

It runs on its own thread with its own message pump. show()/hide()/stop()/
set_color() post a message to that thread rather than touching the window
cross-thread. A gentle alpha pulse (SetLayeredWindowAttributes only, no
repaint) reads as "live". A colour change repaints just the frame ring.
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes

from . import pointer as ptr

# PRIVATE handles, deliberately not ctypes.windll: the prototypes set below
# mutate the function objects ctypes.windll caches process-wide, which would
# impose this module's argtypes on every other user32/gdi32 caller in the app
# (actuator's ShowWindow, the DPI calls in app.py, the input hook...). Its own
# WinDLL instance keeps them here.
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# Explicit prototypes: handles are 64-bit here, and a call left at ctypes'
# default int conversion overflows on them (FillRect's HDC did, measured).
user32.DefWindowProcW.restype = ctypes.c_longlong
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                   wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.FillRect.restype = ctypes.c_int
user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH]
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.InvalidateRect.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.BOOL]
user32.UpdateWindow.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]
user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF,
                                              wintypes.BYTE, wintypes.DWORD]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.CreateRectRgn.restype = wintypes.HRGN
gdi32.CreateRectRgn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
gdi32.CombineRgn.argtypes = [wintypes.HRGN, wintypes.HRGN, wintypes.HRGN, ctypes.c_int]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
SW_HIDE = 0
SW_SHOWNA = 8               # show without activating
LWA_ALPHA = 0x2
RGN_DIFF = 4
WM_DESTROY = 0x0002
WM_ERASEBKGND = 0x0014
WM_TIMER = 0x0113
WM_APP = 0x8000
MSG_SHOW = WM_APP + 1
MSG_HIDE = WM_APP + 2
MSG_STOP = WM_APP + 3
MSG_COLOR = WM_APP + 4
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
HWND_TOPMOST = wintypes.HWND(-1)
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010

#: The three states, as (r, g, b).
BLUE = (91, 141, 255)
RED = (245, 80, 80)
ORANGE = (255, 150, 20)


def _colorref(rgb) -> int:
    r, g, b = rgb
    return (int(r) & 0xFF) | ((int(g) & 0xFF) << 8) | ((int(b) & 0xFF) << 16)


# -- the pointer -------------------------------------------------------------
#
# While the agent has the machine, the real arrow is hidden and a cartoon one
# (core/pointer.py) is painted in its place: a small per-pixel-alpha layered
# window that follows the pointer at fifty frames a second, click-through like
# the border so it never catches a click, its own or the user's. The halo is
# the border's colour on the one pixel that matters; a press squashes it, a
# release ripples, a drag trails.
#
# Hiding the arrow: SetSystemCursor swaps a shape for the whole session and
# takes ownership of the handle, so every call makes a fresh blank cursor.
# Restoring is one call that reloads the user's scheme from the registry,
# which is why nothing here needs to remember what the arrow looked like —
# and why it is also called, belt and braces, when the app closes.

OCR_NORMAL, OCR_IBEAM, OCR_HAND = 32512, 32513, 32649
SPI_SETCURSORS = 0x0057
AC_SRC_OVER, AC_SRC_ALPHA, ULW_ALPHA = 0, 1, 2
DIB_RGB_COLORS = 0
POINTER_TIMER = 2
TICK_MS = 20


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", wintypes.BYTE), ("BlendFlags", wintypes.BYTE),
                ("SourceConstantAlpha", wintypes.BYTE), ("AlphaFormat", wintypes.BYTE)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


user32.UpdateLayeredWindow.restype = wintypes.BOOL
user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
    ctypes.POINTER(wintypes.SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT),
    wintypes.COLORREF, ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
user32.CreateCursor.restype = wintypes.HANDLE
user32.CreateCursor.argtypes = [wintypes.HINSTANCE, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                                ctypes.c_void_p]
user32.SetSystemCursor.restype = wintypes.BOOL
user32.SetSystemCursor.argtypes = [wintypes.HANDLE, wintypes.DWORD]
user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT,
                                         ctypes.c_void_p, wintypes.UINT]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFO),
                                   wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   wintypes.HANDLE, wintypes.DWORD]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]


def restore_system_cursor() -> None:
    """The user's own pointer scheme, back from the registry."""
    try:
        user32.SystemParametersInfoW(SPI_SETCURSORS, 0, None, 0)
    except Exception:
        pass


def _system_dpi_scale() -> float:
    try:
        user32.GetDpiForSystem.restype = wintypes.UINT
        return max(1.0, user32.GetDpiForSystem() / 96.0)
    except Exception:
        return 1.0


class PointerOverlay:
    """The cartoon pointer on screen. Same thread-and-message-pump shape as
    the border; show()/hide()/stop() post to it from anywhere."""

    def __init__(self) -> None:
        self.hwnd = 0
        self.scale = 1.0
        self.size = 0
        self._thread: "threading.Thread | None" = None
        self._ready = threading.Event()
        self._wndproc = None
        self._memdc = None
        self._hbm = None
        self._bits = ctypes.c_void_p()
        self._cursor_hidden = False
        self._last = None

    # -- public, callable from any thread --------------------------------
    def start(self) -> bool:
        if self._thread is not None:
            return self.hwnd != 0
        self._thread = threading.Thread(target=self._run, name="pointer",
                                        daemon=True)
        self._thread.start()
        self._ready.wait(timeout=3.0)
        return self.hwnd != 0

    def show(self, rgb) -> None:
        ptr.set_color(rgb)
        if self.hwnd:
            user32.PostMessageW(self.hwnd, MSG_SHOW, 0, 0)

    def set_color(self, rgb) -> None:
        ptr.set_color(rgb)                  # the next tick repaints

    def hide(self) -> None:
        if self.hwnd:
            user32.PostMessageW(self.hwnd, MSG_HIDE, 0, 0)

    def stop(self) -> None:
        if self.hwnd:
            user32.PostMessageW(self.hwnd, MSG_STOP, 0, 0)

    # -- the window's own thread -----------------------------------------
    def _hide_system_cursor(self) -> None:
        if self._cursor_hidden:
            return
        hInst = kernel32.GetModuleHandleW(None)
        # A 32x32 cursor whose AND mask is all ones and XOR mask all zeros
        # draws nothing at all.
        and_mask = (ctypes.c_ubyte * 128)(*([0xFF] * 128))
        xor_mask = (ctypes.c_ubyte * 128)()
        # Arrow, text beam and link hand — the three shapes a task passes
        # through. Leaving one alone would flash the real one back in over
        # every text field.
        for which in (OCR_NORMAL, OCR_IBEAM, OCR_HAND):
            h = user32.CreateCursor(hInst, 0, 0, 32, 32,
                                    ctypes.cast(and_mask, ctypes.c_void_p),
                                    ctypes.cast(xor_mask, ctypes.c_void_p))
            if h:
                user32.SetSystemCursor(h, which)      # takes ownership of h
        self._cursor_hidden = True

    def _restore_system_cursor(self) -> None:
        if self._cursor_hidden:
            restore_system_cursor()
            self._cursor_hidden = False

    def _upload(self, img) -> None:
        """The sprite into the DIB, premultiplied — what ULW_ALPHA expects."""
        import numpy as np
        arr = np.asarray(img, dtype=np.uint16)          # H, W, RGBA
        a = arr[..., 3]
        bgra = np.empty(arr.shape, np.uint8)
        bgra[..., 0] = (arr[..., 2] * a) // 255
        bgra[..., 1] = (arr[..., 1] * a) // 255
        bgra[..., 2] = (arr[..., 0] * a) // 255
        bgra[..., 3] = a
        ctypes.memmove(self._bits, bgra.ctypes.data, bgra.nbytes)

    def _tick(self, hwnd) -> None:
        try:
            pos = ptr.cursor_pos()
            if pos is None:
                return
            now = time.time()
            x, y = pos
            ptr.sample(x, y, ptr.button_down(), now)
            snap = ptr.snapshot(x, y, now)
            k = (x, y, ptr.key(snap))
            if k == self._last:
                return
            self._last = k
            self._upload(ptr.render(snap, self.scale))
            c = self.size // 2
            dst = wintypes.POINT(x - c, y - c)
            sz = wintypes.SIZE(self.size, self.size)
            src = wintypes.POINT(0, 0)
            bf = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            user32.UpdateLayeredWindow(hwnd, None, ctypes.byref(dst),
                                       ctypes.byref(sz), self._memdc,
                                       ctypes.byref(src), 0, ctypes.byref(bf),
                                       ULW_ALPHA)
        except Exception:
            pass

    def _on_show(self, hwnd) -> None:
        self._hide_system_cursor()
        self._last = None
        self._tick(hwnd)                    # placed and painted before it appears
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        user32.ShowWindow(hwnd, SW_SHOWNA)
        user32.SetTimer(hwnd, POINTER_TIMER, TICK_MS, None)

    def _on_hide(self, hwnd) -> None:
        user32.KillTimer(hwnd, POINTER_TIMER)
        user32.ShowWindow(hwnd, SW_HIDE)
        self._restore_system_cursor()

    def _run(self) -> None:
        try:
            from . import actuator
            actuator.make_thread_dpi_aware()
        except Exception:
            pass
        self.scale = _system_dpi_scale()
        self.size = ptr.canvas_size(self.scale)
        hInst = kernel32.GetModuleHandleW(None)

        def wndproc(hwnd, msg, wp, lp):
            if msg == MSG_SHOW:
                self._on_show(hwnd)
                return 0
            if msg == MSG_HIDE:
                self._on_hide(hwnd)
                return 0
            if msg == WM_TIMER:
                self._tick(hwnd)
                return 0
            if msg == MSG_STOP:
                self._on_hide(hwnd)
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wp, lp)

        self._wndproc = WNDPROC(wndproc)
        cls = _WNDCLASS()
        cls.style = 0
        cls.lpfnWndProc = self._wndproc
        cls.cbClsExtra = cls.cbWndExtra = 0
        cls.hInstance = hInst
        cls.hIcon = 0
        cls.hCursor = 0
        cls.hbrBackground = 0
        cls.lpszMenuName = None
        cls.lpszClassName = "AGIAgentPointer"
        if not user32.RegisterClassW(ctypes.byref(cls)):
            self._ready.set()
            return

        ex = (WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST
              | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        hwnd = user32.CreateWindowExW(ex, cls.lpszClassName, "", WS_POPUP,
                                      0, 0, self.size, self.size, 0, 0, hInst, None)
        if not hwnd:
            self._ready.set()
            return

        # One top-down 32-bit DIB the sprite is copied into every frame.
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = self.size
        bmi.bmiHeader.biHeight = -self.size
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        screen = user32.GetDC(0)
        self._memdc = gdi32.CreateCompatibleDC(screen)
        self._hbm = gdi32.CreateDIBSection(screen, ctypes.byref(bmi), DIB_RGB_COLORS,
                                           ctypes.byref(self._bits), None, 0)
        user32.ReleaseDC(0, screen)
        if not self._hbm or not self._bits:
            user32.DestroyWindow(hwnd)
            self._ready.set()
            return
        gdi32.SelectObject(self._memdc, self._hbm)
        self.hwnd = hwnd
        self._ready.set()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        self._restore_system_cursor()
        if self._hbm:
            gdi32.DeleteObject(self._hbm)
            self._hbm = None
        if self._memdc:
            gdi32.DeleteDC(self._memdc)
            self._memdc = None
        try:
            user32.UnregisterClassW(cls.lpszClassName, hInst)
        except Exception:
            pass
        self.hwnd = 0


class _WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class ScreenBorder:
    def __init__(self, thickness: int = 9, alpha: int = 200, pulse: bool = True,
                 color=BLUE):
        self.thickness = thickness
        self.alpha = alpha
        self.pulse = pulse
        self.color = _colorref(color)
        self.hwnd = 0
        self._thread: "threading.Thread | None" = None
        self._ready = threading.Event()
        self._wndproc = None            # keep the callback alive
        self._brush = None
        self._pulse_t = 0.0

    # -- public, callable from any thread --------------------------------
    def start(self) -> bool:
        if self._thread is not None:
            return self.hwnd != 0
        self._thread = threading.Thread(target=self._run, name="screen-border",
                                        daemon=True)
        self._thread.start()
        self._ready.wait(timeout=3.0)
        return self.hwnd != 0

    def set_color(self, rgb) -> None:
        """Change the frame's colour; repaints only the ring."""
        self.color = _colorref(rgb)
        if self.hwnd:
            user32.PostMessageW(self.hwnd, MSG_COLOR, self.color, 0)

    def show(self, rgb=None) -> None:
        if rgb is not None:
            self.set_color(rgb)
        if self.hwnd:
            user32.PostMessageW(self.hwnd, MSG_SHOW, 0, 0)

    def hide(self) -> None:
        if self.hwnd:
            user32.PostMessageW(self.hwnd, MSG_HIDE, 0, 0)

    def stop(self) -> None:
        if self.hwnd:
            user32.PostMessageW(self.hwnd, MSG_STOP, 0, 0)

    # -- the window's own thread -----------------------------------------
    def _set_brush(self, colorref: int) -> None:
        old = self._brush
        self._brush = gdi32.CreateSolidBrush(colorref)
        if old:
            gdi32.DeleteObject(old)

    def _run(self) -> None:
        hInst = kernel32.GetModuleHandleW(None)
        self._set_brush(self.color)

        def wndproc(hwnd, msg, wp, lp):
            if msg == WM_ERASEBKGND:
                # Only the frame ring exists (the region), so this fills
                # exactly the border and nothing else.
                rc = wintypes.RECT()
                user32.GetClientRect(hwnd, ctypes.byref(rc))
                user32.FillRect(wp, ctypes.byref(rc), self._brush)
                return 1
            if msg == MSG_COLOR:
                self._set_brush(int(wp))
                user32.InvalidateRect(hwnd, None, True)
                user32.UpdateWindow(hwnd)
                return 0
            if msg == MSG_SHOW:
                user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                user32.ShowWindow(hwnd, SW_SHOWNA)
                return 0
            if msg == MSG_HIDE:
                user32.ShowWindow(hwnd, SW_HIDE)
                return 0
            if msg == MSG_STOP:
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_TIMER:
                self._on_pulse()
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wp, lp)

        self._wndproc = WNDPROC(wndproc)
        cls = _WNDCLASS()
        cls.style = 0
        cls.lpfnWndProc = self._wndproc
        cls.cbClsExtra = cls.cbWndExtra = 0
        cls.hInstance = hInst
        cls.hIcon = 0
        cls.hCursor = 0
        cls.hbrBackground = 0           # painted in WM_ERASEBKGND instead
        cls.lpszMenuName = None
        cls.lpszClassName = "AGIAgentScreenBorder"
        atom = user32.RegisterClassW(ctypes.byref(cls))
        if not atom:
            self._ready.set()
            return

        x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)

        ex = (WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST
              | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        hwnd = user32.CreateWindowExW(ex, cls.lpszClassName, "", WS_POPUP,
                                      x, y, w, h, 0, 0, hInst, None)
        if not hwnd:
            self._ready.set()
            return

        # frame-shaped region: a hole in the middle, so only the border exists
        t = self.thickness
        outer = gdi32.CreateRectRgn(0, 0, w, h)
        inner = gdi32.CreateRectRgn(t, t, w - t, h - t)
        gdi32.CombineRgn(outer, outer, inner, RGN_DIFF)
        user32.SetWindowRgn(hwnd, outer, True)
        gdi32.DeleteObject(inner)

        user32.SetLayeredWindowAttributes(hwnd, 0, self.alpha, LWA_ALPHA)
        self.hwnd = hwnd
        if self.pulse:
            user32.SetTimer(hwnd, 1, 90, None)     # ~11 Hz, alpha only
        self._ready.set()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        if self._brush:
            gdi32.DeleteObject(self._brush)
            self._brush = None
        try:
            user32.UnregisterClassW(cls.lpszClassName, hInst)
        except Exception:
            pass
        self.hwnd = 0

    def _on_pulse(self) -> None:
        import math
        self._pulse_t += 0.09
        # breathe between ~0.62 and 1.0 of the base alpha
        f = 0.81 + 0.19 * math.sin(self._pulse_t * 1.6)
        a = max(0, min(255, int(self.alpha * f)))
        user32.SetLayeredWindowAttributes(self.hwnd, 0, a, LWA_ALPHA)


#: One process-wide border.
_border: "ScreenBorder | None" = None


def border() -> ScreenBorder:
    global _border
    if _border is None:
        _border = ScreenBorder()
        _border.start()
    return _border


#: One process-wide pointer.
_pointer: "PointerOverlay | None" = None


def pointer_overlay() -> PointerOverlay:
    global _pointer
    if _pointer is None:
        _pointer = PointerOverlay()
        _pointer.start()
    return _pointer


def pointer_hide() -> None:
    """Hide without creating one just to hide it."""
    if _pointer is not None:
        _pointer.hide()
    else:
        ptr.set_color(None)


def pointer_stop() -> None:
    if _pointer is not None:
        _pointer.stop()
