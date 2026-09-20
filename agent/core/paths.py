"""Where things live — which differs once the app is a packaged .exe.

Running from source, everything sits next to the code and that is convenient.
Frozen with PyInstaller it is actively dangerous: a one-file build extracts
itself into a temporary directory and **deletes it on exit**. Every module that
wrote `Path(__file__).parents[1] / "vault.json"` would appear to work, then
lose the vault, the macro library, the schedules and the browser logins the
moment the app closed.

So the two kinds of path are separated and never confused:

    resource(...)   read-only things shipped inside the build (ui/app.html)
    data(...)       things the user accumulates and must keep

Running from source both resolve under `agent/`, so nothing changes for
development. Frozen, `data()` moves to %LOCALAPPDATA%\\AGIAgent, which
survives not only exit but reinstalling over the top.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: PyInstaller sets this to the extraction directory. Its presence is the
#: only reliable signal that we are running from a bundle.
FROZEN = getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")

#: agent/ when running from source.
_SOURCE_ROOT = Path(__file__).resolve().parents[1]

APP_NAME = "AGIAgent"

#: What the data directory was called before the app was renamed. A packaged
#: build that finds one of these and no APP_NAME directory adopts it — see
#: _adopt_legacy_data. The DESKTOP_AGENT_DATA override below keeps its old
#: name on purpose: it is a developer knob, not something a user sees.
LEGACY_APP_NAMES = ("DesktopAgent",)


def resource_root() -> Path:
    """Read-only files shipped with the build."""
    if FROZEN:
        return Path(sys._MEIPASS)           # type: ignore[attr-defined]
    return _SOURCE_ROOT


def _adopt_legacy_data(new: Path, legacy: list[Path]) -> Path:
    """Carry the data directory across the rename, once — or keep the old one.

    The app was Desktop Agent, and packaged builds kept their vault, macros,
    schedules and browser logins under %LOCALAPPDATA%\\DesktopAgent. A rename
    that quietly started a fresh directory would look, to the user, like every
    login and every saved job had vanished overnight.

    So the first run of a renamed build moves the old directory to the new
    name. It is a rename on the same volume — one atomic MoveFileEx, nothing
    copied file by file, so there is no half-moved state to recover from. If
    Windows refuses it (the old build still open and holding a file is the
    likely reason) the old directory is used as-is for this run and the move
    is tried again next time. Nothing here ever deletes anything.
    """
    if new.exists():
        return new
    for old in legacy:
        if not old.is_dir():
            continue
        try:
            os.rename(old, new)
            return new
        except OSError:
            return old
    return new


def data_root() -> Path:
    """Everything the user would be upset to lose.

    Overridable with DESKTOP_AGENT_DATA, which is what the tests use so a run
    never touches the real vault or macro library.
    """
    override = os.environ.get("DESKTOP_AGENT_DATA")
    if override:
        p = Path(override)
    elif FROZEN:
        base = Path(os.environ.get("LOCALAPPDATA")
                    or str(Path.home() / "AppData" / "Local"))
        p = _adopt_legacy_data(base / APP_NAME,
                               [base / name for name in LEGACY_APP_NAMES])
    else:
        p = _SOURCE_ROOT
    p.mkdir(parents=True, exist_ok=True)
    return p


def resource(*parts: str) -> Path:
    return resource_root().joinpath(*parts)


def data(*parts: str) -> Path:
    """A path under the data root, with its parent directory created."""
    p = data_root().joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def describe() -> dict:
    return {"frozen": FROZEN,
            "resources": str(resource_root()),
            "data": str(data_root())}
