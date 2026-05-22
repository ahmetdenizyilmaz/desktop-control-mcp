import base64
import ctypes
import ctypes.wintypes
import io
import time
from pathlib import Path

import mss
import pyautogui
from PIL import Image, ImageDraw

import screenshot_manager

# Make process DPI-aware so ALL coordinates (pyautogui, Win32 cursor, mss)
# use physical pixels — eliminates scaling mismatch on high-DPI displays.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except (AttributeError, OSError):
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass

# Disable pyautogui failsafe so it works in all screen positions
pyautogui.FAILSAFE = False


def _get_cursor_info():
    """Get the current cursor position and icon using Win32 API."""
    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class CURSORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.wintypes.DWORD),
            ("flags", ctypes.wintypes.DWORD),
            ("hCursor", ctypes.wintypes.HANDLE),
            ("ptScreenPos", POINT),
        ]

    ci = CURSORINFO()
    ci.cbSize = ctypes.sizeof(CURSORINFO)
    if ctypes.windll.user32.GetCursorInfo(ctypes.byref(ci)) and ci.flags & 0x01:
        return ci.ptScreenPos.x, ci.ptScreenPos.y, ci.hCursor
    return None


def _draw_cursor(png: Image.Image, monitor_left: int, monitor_top: int) -> None:
    """Draw the mouse cursor onto the screenshot image."""
    info = _get_cursor_info()
    if info is None:
        return

    cx, cy, hCursor = info
    # Translate screen coords to image coords
    ix = cx - monitor_left
    iy = cy - monitor_top

    # Try to get the actual cursor bitmap via Win32
    hdc_screen = ctypes.windll.user32.GetDC(0)
    hdc_mem = ctypes.windll.gdi32.CreateCompatibleDC(hdc_screen)

    size = 32
    # Create a BITMAPINFO for a 32x32 BGRA bitmap
    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", ctypes.wintypes.DWORD),
            ("biWidth", ctypes.c_long),
            ("biHeight", ctypes.c_long),
            ("biPlanes", ctypes.wintypes.WORD),
            ("biBitCount", ctypes.wintypes.WORD),
            ("biCompression", ctypes.wintypes.DWORD),
            ("biSizeImage", ctypes.wintypes.DWORD),
            ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long),
            ("biClrUsed", ctypes.wintypes.DWORD),
            ("biClrImportant", ctypes.wintypes.DWORD),
        ]

    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth = size
    bmi.biHeight = -size  # top-down
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = 0

    bits = ctypes.c_void_p()
    hbmp = ctypes.windll.gdi32.CreateDIBSection(
        hdc_mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0
    )

    if hbmp and bits:
        old = ctypes.windll.gdi32.SelectObject(hdc_mem, hbmp)
        # Fill with transparent black
        ctypes.windll.gdi32.PatBlt(hdc_mem, 0, 0, size, size, 0x00000042)  # BLACKNESS
        # Draw the cursor icon
        ctypes.windll.user32.DrawIconEx(
            hdc_mem, 0, 0, hCursor, size, size, 0, 0, 0x0003  # DI_NORMAL
        )
        ctypes.windll.gdi32.GdiFlush()

        # Read pixel data
        buf = (ctypes.c_ubyte * (size * size * 4))()
        ctypes.memmove(buf, bits, size * size * 4)
        cursor_img = Image.frombuffer("RGBA", (size, size), bytes(buf), "raw", "BGRA", 0, 1)

        ctypes.windll.gdi32.SelectObject(hdc_mem, old)
        ctypes.windll.gdi32.DeleteObject(hbmp)
        ctypes.windll.gdi32.DeleteDC(hdc_mem)
        ctypes.windll.user32.ReleaseDC(0, hdc_screen)

        # Check if we got a valid cursor (not all black/transparent)
        if cursor_img.getbbox():
            png.paste(cursor_img, (ix, iy), cursor_img)
            return

    # Cleanup in case of failure
    if hbmp:
        ctypes.windll.gdi32.DeleteObject(hbmp)
    ctypes.windll.gdi32.DeleteDC(hdc_mem)
    ctypes.windll.user32.ReleaseDC(0, hdc_screen)

    # Fallback: draw a simple arrow
    draw = ImageDraw.Draw(png)
    arrow = [
        (ix, iy), (ix, iy + 18), (ix + 5, iy + 14),
        (ix + 9, iy + 20), (ix + 12, iy + 18), (ix + 8, iy + 12),
        (ix + 14, iy + 12), (ix, iy),
    ]
    draw.polygon(arrow, fill="white", outline="black")


def take_screenshot() -> str:
    """Capture the entire screen and return as a base64-encoded PNG string."""
    with mss.mss() as sct:
        monitor = sct.monitors[1]  # primary monitor
        img = sct.grab(monitor)
        png = Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")

    png = png.convert("RGBA")
    _draw_cursor(png, monitor.get("left", 0), monitor.get("top", 0))
    png = png.convert("RGB")

    buffer = io.BytesIO()
    png.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def move_mouse(x: int, y: int) -> None:
    """Move the mouse cursor to (x, y)."""
    pyautogui.moveTo(x, y)


def click_mouse(x: int, y: int, button: str = "left") -> None:
    """Click the mouse at (x, y) with the given button."""
    pyautogui.click(x, y, button=button)


def drag_mouse(x1: int, y1: int, x2: int, y2: int, button: str = "left") -> None:
    """Click at (x1, y1) and drag to (x2, y2)."""
    pyautogui.moveTo(x1, y1)
    pyautogui.mouseDown(button=button)
    pyautogui.moveTo(x2, y2, duration=0.5)
    pyautogui.mouseUp(button=button)


def scroll(x: int, y: int, direction: str, amount: int = 3) -> None:
    """Scroll the mouse wheel at position (x, y).

    Args:
        x: X coordinate on screen
        y: Y coordinate on screen
        direction: "up", "down", "left", or "right"
        amount: Number of scroll clicks (default 3)
    """
    pyautogui.moveTo(x, y)
    if direction == "up":
        pyautogui.scroll(amount)
    elif direction == "down":
        pyautogui.scroll(-amount)
    elif direction == "left":
        pyautogui.hscroll(-amount)
    elif direction == "right":
        pyautogui.hscroll(amount)


def double_click(x: int, y: int) -> None:
    """Double-click the mouse at (x, y)."""
    pyautogui.doubleClick(x, y)


def get_screen_size() -> dict:
    """Return the primary screen resolution as {"width": int, "height": int}."""
    with mss.mss() as sct:
        monitor = sct.monitors[1]  # primary monitor
        return {"width": monitor["width"], "height": monitor["height"]}


# Keys recognized by pyautogui.press() — used to distinguish key presses from text
_SINGLE_KEYS = {
    'enter', 'return', 'tab', 'escape', 'esc', 'backspace', 'delete', 'del',
    'space', 'insert', 'home', 'end', 'pageup', 'pagedown',
    'up', 'down', 'left', 'right',
    'f1', 'f2', 'f3', 'f4', 'f5', 'f6', 'f7', 'f8', 'f9', 'f10', 'f11', 'f12',
    'capslock', 'numlock', 'scrolllock', 'printscreen', 'pause',
    'volumeup', 'volumedown', 'volumemute',
    'playpause', 'nexttrack', 'prevtrack', 'stop',
}
_MODIFIER_KEYS = {'ctrl', 'alt', 'shift', 'win', 'command', 'option', 'fn'}
_ALL_KNOWN_KEYS = _SINGLE_KEYS | _MODIFIER_KEYS


def send_keys(text: str) -> None:
    """Send keyboard input — smart dispatch between key press, hotkey, and text.

    1. Single known key (e.g. "enter", "tab", "f5") → pyautogui.press()
    2. Hotkey combo where all parts are known keys (e.g. "ctrl+c") → pyautogui.hotkey()
    3. Everything else (e.g. "hello", "2+2") → pyautogui.write() as literal text
    """
    lower = text.strip().lower()

    # Case 1: single key name
    if lower in _SINGLE_KEYS:
        pyautogui.press(lower)
        return

    # Case 2: hotkey combo — needs at least one modifier and all parts must be
    # known keys, modifiers, or single characters (a-z, 0-9)
    if "+" in text:
        parts = [k.strip().lower() for k in text.split("+")]
        has_modifier = any(p in _MODIFIER_KEYS for p in parts)
        all_valid = all(
            p in _ALL_KNOWN_KEYS or (len(p) == 1 and p.isalnum())
            for p in parts
        )
        if len(parts) > 1 and has_modifier and all_valid:
            pyautogui.hotkey(*parts)
            return

    # Case 3: plain text
    pyautogui.write(text, interval=0.02)


def type_text(text: str) -> None:
    """Type text literally — never interprets as hotkey or key press."""
    pyautogui.write(text, interval=0.02)


def press_key(key: str, presses: int = 1) -> None:
    """Press a single key one or more times."""
    pyautogui.press(key.strip().lower(), presses=presses)


def get_active_window() -> dict:
    """Return info about the currently focused window using Win32 API."""
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    if not hwnd:
        return {"title": "", "position": None, "size": None, "hwnd": 0}

    # Get window title
    length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
    title = buf.value

    # Get window rect
    rect = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))

    return {
        "title": title,
        "position": {"x": rect.left, "y": rect.top},
        "size": {"width": rect.right - rect.left, "height": rect.bottom - rect.top},
        "hwnd": hwnd,
    }


def _capture_screen() -> tuple[Image.Image, dict]:
    """Capture screen and draw cursor. Returns (pil_image, monitor_info)."""
    with mss.mss() as sct:
        monitor = sct.monitors[1]  # primary monitor
        img = sct.grab(monitor)
        png = Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")

    png = png.convert("RGBA")
    _draw_cursor(png, monitor.get("left", 0), monitor.get("top", 0))
    png = png.convert("RGB")
    return png, monitor


def take_screenshot_to_file(quality: int = 30) -> tuple[Path, Image.Image]:
    """Capture screen, save as JPEG, return (file_path, pil_image)."""
    screenshot_manager.cleanup()
    png, _ = _capture_screen()
    path = screenshot_manager.save_jpeg(png, quality=quality)
    return path, png


def take_screenshot_burst(frame_count: int = 10, duration_seconds: float = 1.0, quality: int = 30) -> tuple[Path, list[Path]]:
    """Capture multiple frames spread over a duration. Returns (folder_path, [frame_paths])."""
    screenshot_manager.cleanup()
    burst_dir = screenshot_manager.create_burst_dir()
    frame_paths = []
    interval = duration_seconds / max(frame_count - 1, 1)

    with mss.mss() as sct:
        monitor = sct.monitors[1]  # primary monitor
        for i in range(frame_count):
            capture_start = time.perf_counter()

            img = sct.grab(monitor)
            png = Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")
            png = png.convert("RGBA")
            _draw_cursor(png, monitor.get("left", 0), monitor.get("top", 0))
            png = png.convert("RGB")

            ts = f"{time.perf_counter():.4f}".replace(".", "_")
            filename = f"frame_{i:03d}_{ts}.jpg"
            frame_path = burst_dir / filename
            png.save(frame_path, format="JPEG", quality=quality)
            frame_paths.append(frame_path)

            # Wait for next frame (skip wait after last frame)
            if i < frame_count - 1:
                elapsed = time.perf_counter() - capture_start
                wait = interval - elapsed
                if wait > 0:
                    time.sleep(wait)

    return burst_dir, frame_paths


def detect_ui_elements(confidence_threshold: float = 0.5) -> tuple[Path, Path, dict]:
    """Capture screen, run UI detection, save annotated image + JSON.
    Returns (annotated_image_path, json_path, elements_dict)."""
    import json as json_mod
    import ui_parser

    screenshot_manager.cleanup()
    png, _ = _capture_screen()

    elements = ui_parser.parse_ui(png, confidence_threshold=confidence_threshold)
    annotated = ui_parser.annotate_image(png, elements["elements"])

    # Save annotated image
    annotated_path = screenshot_manager.save_jpeg(annotated, quality=60, prefix="ui_detected")

    # Save JSON
    json_path = annotated_path.with_suffix(".json")
    with open(json_path, "w") as f:
        json_mod.dump(elements, f, indent=2)

    return annotated_path, json_path, elements
