"""MCP server — desktop control + 3-mode screenshot system for Windows."""

import json
import threading
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP, Image as MCPImage

import controller

# ------------------------------------------------------------------
# Lifespan — start model loading in background so server boots fast
# ------------------------------------------------------------------

_models_ready = threading.Event()

def _load_models_background():
    """Load OmniParser v2 models in a background thread."""
    import ui_parser
    ui_parser.load_models()
    _models_ready.set()

@asynccontextmanager
async def lifespan(app):
    """Start model loading in background — server is ready immediately."""
    thread = threading.Thread(target=_load_models_background, daemon=True)
    thread.start()
    yield {}


# ------------------------------------------------------------------
# Server
# ------------------------------------------------------------------

mcp = FastMCP("desktop-control", instructions="""
Desktop control tools for a Windows PC. All coordinates are absolute pixels on the PRIMARY monitor.

═══════════════════════════════════════════════════════
 MANDATORY CLICK WORKFLOW — follow this EVERY time
═══════════════════════════════════════════════════════

NEVER guess coordinates from a screenshot. ALWAYS use detect_ui_elements to get exact coordinates.

1. DETECT  — call detect_ui_elements to get the element table.
2. FIND    — locate your target in the table by matching the Label column.
3. CLICK   — call click_mouse with the EXACT (x, y) from the Center column.
4. VERIFY  — call take_screenshot to confirm the click landed correctly.
5. RECOVER — if the click missed, call detect_ui_elements again and retry.

═══════════════════════════════════════════════════════
 WHICH TOOL TO USE
═══════════════════════════════════════════════════════

| Situation                           | Tool                 |
|-------------------------------------|----------------------|
| See what's on screen                | take_screenshot      |
| Find a button/field/icon to click   | detect_ui_elements   |
| Click a detected element            | click_mouse          |
| Double-click (open file, select)    | double_click         |
| Type plain text into a field        | type_text            |
| Press Enter, Tab, Escape, F5, etc.  | press_key            |
| Keyboard shortcut (ctrl+c, alt+f4) | send_keys            |
| Scroll a page or list               | scroll               |
| Observe animation/loading           | take_screenshot_burst|
| Check which window is focused       | get_active_window    |

═══════════════════════════════════════════════════════
 READING THE ELEMENT TABLE
═══════════════════════════════════════════════════════

detect_ui_elements returns a table like this:

  ID  Type  Conf  Center         Label
   1  text  0.98  ( 499,   55)   File  Edit  View
   2  icon  0.91  ( 674,  200)   Search button
   3  icon  0.87  (1200,  400)   Close

To click element 2: call click_mouse(x=674, y=200)
To click element 3: call click_mouse(x=1200, y=400)

Always use the EXACT coordinates from the Center column. Never estimate or round.

═══════════════════════════════════════════════════════
 KEYBOARD INPUT
═══════════════════════════════════════════════════════

Three separate tools — pick the right one:

• type_text("hello world")     — types literal text, never interprets + as hotkey
• press_key("enter")           — presses a single key (enter, tab, escape, f5, etc.)
• press_key("down", presses=5) — presses a key multiple times
• send_keys("ctrl+c")          — keyboard shortcut (modifier+key combos only)
• send_keys("alt+f4")          — another shortcut example

DO NOT use send_keys for plain text — use type_text instead.
DO NOT use send_keys for single keys — use press_key instead.

═══════════════════════════════════════════════════════
 ERROR RECOVERY
═══════════════════════════════════════════════════════

• Click missed target   → detect_ui_elements again (screen may have changed), retry
• Element not found     → try lower confidence_threshold (e.g. 0.3), or scroll to reveal it
• Too many elements     → raise confidence_threshold (e.g. 0.7) to filter noise
• Wrong window focused  → call get_active_window to check, click correct window first

═══════════════════════════════════════════════════════
 IMPORTANT RULES
═══════════════════════════════════════════════════════

• ALWAYS verify with take_screenshot after clicking, typing, or pressing keys.
• When the screen changes (new dialog, menu opens, page scrolls), call detect_ui_elements again — old coordinates are stale.
• Call get_screen_size once at the start to learn the resolution.
""".strip(), lifespan=lifespan)


# ------------------------------------------------------------------
# Screen info
# ------------------------------------------------------------------

@mcp.tool()
def get_screen_size() -> str:
    """Get the primary monitor resolution in pixels. Call this first to know the coordinate space."""
    size = controller.get_screen_size()
    return f"Screen size: {size['width']}x{size['height']}"


# ------------------------------------------------------------------
# Screenshot tools
# ------------------------------------------------------------------

@mcp.tool()
def take_screenshot(quality: int = 30) -> list:
    """Capture the primary monitor as a compressed JPEG for general observation.

    DO NOT use this to find coordinates for clicking. Use detect_ui_elements instead.

    Args:
        quality: JPEG quality 1-100 (default 30). Lower = smaller file, faster.
    """
    file_path, _ = controller.take_screenshot_to_file(quality=quality)
    return [
        MCPImage(path=str(file_path)),
        f"Saved: {file_path}",
    ]


@mcp.tool()
def detect_ui_elements(confidence_threshold: float = 0.5, full_response: bool = False) -> list:
    """Detect all UI elements on screen using AI vision models.

    Returns an annotated image with numbered colored rectangles plus a compact table
    with ID, type, confidence, center coordinates, and label for each element.

    Green = icons/buttons. Blue = text.

    USAGE: Find your target element in the table, then call click_mouse with its Center coordinates.
    Example: if the table shows element 5 at center (674, 200), call click_mouse(x=674, y=200).

    Args:
        confidence_threshold: Minimum confidence 0.0-1.0 (default 0.5). Raise to reduce noise, lower to find more elements.
        full_response: If True, include full JSON with bboxes. Default False returns compact table only.
    """
    if not _models_ready.is_set():
        _models_ready.wait(timeout=60)
        if not _models_ready.is_set():
            return "Error: AI models still loading. Try again in a moment."

    annotated_path, json_path, elements = controller.detect_ui_elements(
        confidence_threshold=confidence_threshold
    )

    if full_response:
        return [
            MCPImage(path=str(annotated_path)),
            f"Detected {len(elements['elements'])} elements | Full JSON: {json_path}",
            json.dumps(elements, separators=(",", ":")),
        ]

    # Sort elements by screen position: top→bottom, left→right
    sorted_els = sorted(
        elements["elements"],
        key=lambda el: (el["center"]["y"], el["center"]["x"]),
    )

    # Compact table: ID, type, confidence, center, label
    lines = [
        f"Detected {len(sorted_els)} elements | Full JSON: {json_path}",
        "",
        f"{'ID':>4}  {'Type':<5}  {'Conf':<5}  {'Center':>13}  Label",
        f"{'─'*4}  {'─'*5}  {'─'*5}  {'─'*13}  {'─'*30}",
    ]
    for el in sorted_els:
        cx, cy = el["center"]["x"], el["center"]["y"]
        conf = el.get("confidence", 0.0)
        label = el.get("label") or el.get("content") or ""
        if len(label) > 50:
            label = label[:47] + "..."
        lines.append(f"{el['id']:>4}  {el['type']:<5}  {conf:.2f}   ({cx:>4},{cy:>4})  {label}")

    return [
        MCPImage(path=str(annotated_path)),
        "\n".join(lines),
    ]


@mcp.tool()
def take_screenshot_burst(frame_count: int = 10, duration_seconds: float = 1.0) -> str:
    """Capture multiple frames spread evenly over a time span.

    Use this to observe animations, loading states, or timing-sensitive UI changes.
    Returns file paths only (no inline images).

    Args:
        frame_count: Number of frames (default 10, max 30).
        duration_seconds: Total capture duration in seconds (default 1.0).
    """
    frame_count = min(max(1, frame_count), 30)
    folder_path, frame_paths = controller.take_screenshot_burst(
        frame_count=frame_count, duration_seconds=duration_seconds
    )

    frame_list = "\n".join(f"  {p.name}" for p in frame_paths)
    return f"Burst: {len(frame_paths)} frames over {duration_seconds}s\nFolder: {folder_path}\n{frame_list}"


# ------------------------------------------------------------------
# Mouse tools
# ------------------------------------------------------------------

@mcp.tool()
def click_mouse(x: int, y: int, button: str = "left") -> str:
    """Click at exact screen coordinates. ALWAYS get coordinates from detect_ui_elements — never guess.

    Args:
        x: X pixel coordinate (from detect_ui_elements Center column)
        y: Y pixel coordinate (from detect_ui_elements Center column)
        button: "left", "right", or "middle"
    """
    controller.click_mouse(x, y, button)
    return f"Clicked {button} at ({x}, {y})"


@mcp.tool()
def double_click(x: int, y: int) -> str:
    """Double-click at exact screen coordinates. ALWAYS get coordinates from detect_ui_elements.

    Args:
        x: X pixel coordinate (from detect_ui_elements Center column)
        y: Y pixel coordinate (from detect_ui_elements Center column)
    """
    controller.double_click(x, y)
    return f"Double-clicked at ({x}, {y})"


@mcp.tool()
def move_mouse(x: int, y: int) -> str:
    """Move the mouse cursor without clicking.

    Args:
        x: X pixel coordinate
        y: Y pixel coordinate
    """
    controller.move_mouse(x, y)
    return f"Moved to ({x}, {y})"


@mcp.tool()
def drag_mouse(x1: int, y1: int, x2: int, y2: int, button: str = "left") -> str:
    """Click and drag from one position to another.

    Args:
        x1: Start X
        y1: Start Y
        x2: End X
        y2: End Y
        button: "left", "right", or "middle"
    """
    controller.drag_mouse(x1, y1, x2, y2, button)
    return f"Dragged ({x1},{y1}) → ({x2},{y2})"


@mcp.tool()
def scroll(x: int, y: int, direction: str, amount: int = 3) -> str:
    """Scroll the mouse wheel at a screen position.

    Args:
        x: X pixel coordinate
        y: Y pixel coordinate
        direction: "up", "down", "left", or "right"
        amount: Scroll clicks (default 3)
    """
    controller.scroll(x, y, direction, amount)
    return f"Scrolled {direction} ×{amount} at ({x}, {y})"


# ------------------------------------------------------------------
# Keyboard tools
# ------------------------------------------------------------------

@mcp.tool()
def send_keys(text: str) -> str:
    """Send a keyboard shortcut using "+" notation.

    ONLY use this for modifier key combos. For plain text use type_text. For single keys use press_key.

    Examples: "ctrl+c", "alt+tab", "ctrl+shift+s", "win+d"

    Args:
        text: Keyboard shortcut in "modifier+key" notation.
    """
    controller.send_keys(text)
    return f"Sent: {text}"


@mcp.tool()
def type_text(text: str) -> str:
    """Type text literally into the focused field. Never interprets characters as shortcuts.

    Use this for all text input: filenames, search queries, code, messages, URLs, etc.

    Examples: type_text("hello world"), type_text("C++ is great"), type_text("user@example.com")

    Args:
        text: The text to type.
    """
    controller.type_text(text)
    return f"Typed: {text}"


@mcp.tool()
def press_key(key: str, presses: int = 1) -> str:
    """Press a single key one or more times.

    Use this for Enter, Tab, Escape, arrow keys, F-keys, Delete, Backspace, etc.

    Examples: press_key("enter"), press_key("tab"), press_key("f5"), press_key("down", presses=5)

    Args:
        key: Key name — enter, tab, escape, backspace, delete, space, up, down, left, right, f1-f12, home, end, pageup, pagedown, etc.
        presses: Number of times to press (default 1).
    """
    controller.press_key(key, presses=presses)
    return f"Pressed {key}" + (f" ×{presses}" if presses > 1 else "")


# ------------------------------------------------------------------
# Window info
# ------------------------------------------------------------------

@mcp.tool()
def get_active_window() -> str:
    """Get the title, position, and size of the currently focused window.

    Use this to verify the correct window is focused before typing or pressing keys.
    """
    info = controller.get_active_window()
    if not info["title"]:
        return "No active window detected."
    pos = info["position"]
    size = info["size"]
    return f"Active window: \"{info['title']}\" | Position: ({pos['x']}, {pos['y']}) | Size: {size['width']}x{size['height']}"


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
