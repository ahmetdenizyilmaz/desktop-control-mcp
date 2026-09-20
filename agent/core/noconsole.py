"""Stop a windowed build from flashing a console on every subprocess.

A GUI application on Windows has no console. Windows' answer to a process with
no console that starts a console program is to **create one**, visibly. So a
packaged, `--windowed` build pops a black cmd window for every child it spawns:
the Claude CLI on each task, PowerShell for the app inventory, Chromium for the
browser. From source it goes unnoticed, because the child quietly inherits the
terminal the app was started from.

Our own call sites can pass the flag themselves, and do. The problem is the
ones we do not own — the Agent SDK spawns `claude.exe` through asyncio, and
Playwright spawns the browser through its own driver. Both bottom out in
`subprocess.Popen`, so the only place to fix all of them at once is there.

`install()` is therefore a deliberate, narrow monkey-patch: Windows only, and
only when there is genuinely no console to inherit. With a console present it
does nothing at all, so running from a terminal behaves exactly as before and
a developer never loses child output.
"""

from __future__ import annotations

import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000

_installed = False


def has_console() -> bool:
    """True when this process owns a console a child could inherit."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:
        return True          # unsure: assume yes and change nothing


def flags() -> int:
    """CREATE_NO_WINDOW when a child would otherwise open its own window."""
    if sys.platform == "win32" and not has_console():
        return CREATE_NO_WINDOW
    return 0


def install() -> bool:
    """Make every subprocess in this process start without a window.

    Returns False when it was not needed, which is the normal case from source.
    """
    global _installed
    if _installed or sys.platform != "win32" or has_console():
        return False

    original = subprocess.Popen.__init__

    def patched(self, *args, **kwargs):
        # Never override an explicit choice — a caller that asked for a visible
        # console had a reason, and silencing it would be the same class of bug
        # as the one this fixes.
        if "creationflags" not in kwargs or not kwargs["creationflags"]:
            kwargs["creationflags"] = CREATE_NO_WINDOW
        else:
            kwargs["creationflags"] |= CREATE_NO_WINDOW
        return original(self, *args, **kwargs)

    subprocess.Popen.__init__ = patched      # type: ignore[method-assign]
    _installed = True
    return True
