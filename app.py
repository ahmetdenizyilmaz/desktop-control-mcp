import threading

from PIL import Image, ImageDraw
from pystray import Icon, Menu, MenuItem

import server

# ---------------------------------------------------------------------------
# Tray icon (generated at runtime — no external file needed)
# ---------------------------------------------------------------------------

def _create_icon_image():
    """Create a simple 64x64 tray icon with a green circle."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((8, 8, 56, 56), fill=(46, 204, 113))
    draw.text((22, 20), "DC", fill="white")
    return img


# ---------------------------------------------------------------------------
# Server thread management
# ---------------------------------------------------------------------------

_server_thread: threading.Thread | None = None
_server_running = False


def _start_server():
    global _server_thread, _server_running
    if _server_running:
        return
    _server_running = True
    _server_thread = threading.Thread(target=server.run, daemon=True)
    _server_thread.start()
    print("Server started on http://localhost:7845")


def _stop_server():
    global _server_running
    # Flask dev server doesn't expose a clean shutdown from outside,
    # but since the thread is a daemon it will die with the process.
    _server_running = False
    print("Server flagged to stop (will stop on next restart or quit).")


# ---------------------------------------------------------------------------
# Tray menu callbacks
# ---------------------------------------------------------------------------

def _on_status(icon, item):
    state = "running" if _server_running else "stopped"
    print(f"Server is {state}")


def _on_toggle(icon, item):
    if _server_running:
        _stop_server()
    else:
        _start_server()


def _toggle_label(item):
    return "Stop Server" if _server_running else "Start Server"


def _on_quit(icon, item):
    icon.stop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    _start_server()

    icon = Icon(
        "DesktopControl",
        _create_icon_image(),
        "Desktop Control",
        menu=Menu(
            MenuItem("Status", _on_status),
            MenuItem(_toggle_label, _on_toggle),
            MenuItem("Quit", _on_quit),
        ),
    )
    icon.run()  # blocks until icon.stop()


if __name__ == "__main__":
    main()
