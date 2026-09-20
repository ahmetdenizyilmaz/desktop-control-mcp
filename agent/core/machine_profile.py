"""Machine profile — resolves portable symbols to this PC's concrete values.

A macro says {DOWNLOADS} or {APP:excel}. This module turns that into a real
path on whatever machine it is running on, so the same macro file works on a
different PC, with a different username, install location, and screen size.

The profile is generated per machine and NEVER shared or committed.
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import shutil
import subprocess
import winreg
from collections import deque
from pathlib import Path

from . import noconsole
from . import paths

PROFILE_PATH = paths.data("machine_profile.json")

# Known apps: symbol -> (registry App Paths key, exe name, friendly capability)
KNOWN_APPS = {
    "excel":    ("EXCEL.EXE",    "excel"),
    "word":     ("WINWORD.EXE",  "word"),
    "powerpoint": ("POWERPNT.EXE", "powerpoint"),
    "outlook":  ("OUTLOOK.EXE",  "outlook"),
    "chrome":   ("chrome.exe",   "chrome"),
    "edge":     ("msedge.exe",   "edge"),
    "firefox":  ("firefox.exe",  "firefox"),
    "notepad":  ("notepad.exe",  "notepad"),
    "explorer": ("explorer.exe", "explorer"),
    "terminal": ("wt.exe",       "terminal"),
    "code":     ("Code.exe",     "vscode"),
    "teams":    ("ms-teams.exe", "teams"),
    "spotify":  ("Spotify.exe",  "spotify"),
    # Store apps reachable through their WindowsApps alias on PATH. Paint is
    # the canvas the pointer-path tool was built for; without an entry here
    # open_app("paint") said "not installed" on a machine that has it.
    "paint":    ("mspaint.exe",  "paint"),
    "calculator": ("calc.exe",   "calculator"),
}

# Windows known-folder GUIDs for user directories (locale-independent —
# do NOT hardcode "Downloads", it is localized on non-English installs)
KNOWN_FOLDERS = {
    "DOWNLOADS": "{374DE290-123F-4565-9164-39C4925E467B}",
    "DOCUMENTS": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "DESKTOP":   "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "PICTURES":  "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
    "VIDEOS":    "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
    "MUSIC":     "{4BD8D571-6D19-48D3-BE97-422220080E43}",
}


def _known_folder(guid: str) -> str | None:
    """Resolve a Windows known folder by GUID (locale-independent)."""
    try:
        from ctypes import windll, wintypes
        import comtypes  # noqa: F401  (only to ensure COM is available)
    except Exception:
        pass
    try:
        SHGetKnownFolderPath = ctypes.windll.shell32.SHGetKnownFolderPath
        CoTaskMemFree = ctypes.windll.ole32.CoTaskMemFree

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                        ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

        def _parse(g: str) -> GUID:
            g = g.strip("{}")
            p = g.split("-")
            d4 = bytes.fromhex(p[3] + p[4])
            return GUID(int(p[0], 16), int(p[1], 16), int(p[2], 16),
                        (ctypes.c_ubyte * 8)(*d4))

        ptr = ctypes.c_wchar_p()
        if SHGetKnownFolderPath(ctypes.byref(_parse(guid)), 0, None,
                                ctypes.byref(ptr)) == 0:
            val = ptr.value
            CoTaskMemFree(ptr)
            return val
    except Exception:
        return None
    return None


#: Places a registry App Paths lookup will not find things.
#: Store/MSIX apps live under WindowsApps, and several installers put their
#: exe in per-user AppData with no registry entry at all.
_EXTRA_ROOTS = [
    (os.environ.get("LOCALAPPDATA", ""), 4),
    (os.environ.get("APPDATA", ""), 3),
    (os.path.join(os.environ.get("ProgramFiles", ""), "WindowsApps"), 3),
    (os.environ.get("ProgramFiles", ""), 4),
    (os.environ.get("ProgramFiles(x86)", ""), 4),
]


#: Directory names never descended into, at any depth. No installer puts an
#: application in a cache or a temp directory, and they are the bulk of the
#: tree: %LOCALAPPDATA%\Temp alone was ~7,000 entries on one machine, a
#: fifth of the whole walk, and it grew with every test run that did not
#: clean up after itself. Compared case-insensitively.
_SKIP_ANYWHERE = frozenset({
    "temp", "tmp", "cache", "caches", "code cache", "gpucache", "cachestorage",
    "inetcache", "inetcookies", "webcache", "crashdumps", "crashpad", "logs",
    "node_modules", "__pycache__", "site-packages", ".git",
})

#: Skipped only as a direct child of a root, where the name is unambiguous:
#: %LOCALAPPDATA%\pip is pip's download cache, %LOCALAPPDATA%\Packages holds
#: Store apps' settings (their exes are under Program Files\WindowsApps), and
#: ms-playwright is a test harness's private browser build — reporting that
#: as the user's Chrome would be wrong. Deeper down the same names could be
#: anything, so there they are left alone. Vendor directories are NOT listed:
#: Chrome, Discord, Teams, VS Code and Spotify all install under
#: %LOCALAPPDATA%\<Vendor>, and that is exactly what this walk exists to find.
_SKIP_ROOT_CHILD = frozenset({
    "packages", "package cache", "pip", "npm-cache", "uv", "ccache",
    "ms-playwright", "d3dscache", "squirreltemp", "temporary internet files",
    "history", "virtualstore", "connecteddevicesplatform", "comms",
})

#: (root, depth) -> {exe name, lowercased: path}. Each root is walked once
#: per build, however many apps miss the registry. The per-app search this
#: replaces re-walked every root for every miss, and three misses (Outlook,
#: Firefox and VS Code absent) cost three full walks of %LOCALAPPDATA%.
_ROOT_INDEX: dict[tuple[str, int], dict[str, str]] = {}


def _index_root(root: str, max_depth: int) -> dict[str, str]:
    """Every .exe under root, at most max_depth levels down.

    Breadth-first, first sighting wins — so a copy at Programs\\X\\x.exe beats
    one buried four levels deep, the same order the per-app search used.
    os.scandir reports file-or-directory from the listing itself; the pathlib
    walk this replaces paid two stat() calls per entry to learn the same.
    """
    index: dict[str, str] = {}
    frontier = deque([(root, 0)])
    while frontier:
        d, depth = frontier.popleft()
        try:
            with os.scandir(d) as it:
                for entry in it:
                    name = entry.name.lower()
                    try:
                        is_dir = entry.is_dir()
                    except OSError:
                        continue
                    if is_dir:
                        if (depth < max_depth and name not in _SKIP_ANYWHERE
                                and not (depth == 0 and name in _SKIP_ROOT_CHILD)):
                            frontier.append((entry.path, depth + 1))
                    elif name.endswith(".exe"):
                        index.setdefault(name, entry.path)
        except OSError:
            continue
    return index


def _bounded_find(root: str, exe: str, max_depth: int) -> str | None:
    """Depth-bounded search for an .exe. rglob over Program Files can take minutes."""
    if not os.path.isdir(root):
        return None
    key = (root, max_depth)
    if key not in _ROOT_INDEX:
        _ROOT_INDEX[key] = _index_root(root, max_depth)
    return _ROOT_INDEX[key].get(exe.lower())


def _app_path(exe: str) -> str | None:
    """Find an executable: App Paths, PATH, Store/MSIX, then per-user installs."""
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            key = winreg.OpenKey(
                hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}")
            val, _ = winreg.QueryValueEx(key, "")
            winreg.CloseKey(key)
            if val and Path(val).exists():
                return str(Path(val))
        except OSError:
            continue

    found = shutil.which(exe)
    if found:
        return str(Path(found))

    for root, depth in _EXTRA_ROOTS:
        if not root:
            continue
        hit = _bounded_find(root, exe, depth)
        if hit:
            return hit
    return None


def _appx_packages() -> dict[str, str]:
    """Store apps addressable by AppUserModelID rather than a path.

    Some Store apps cannot be launched by exe at all (the exe under
    WindowsApps refuses direct execution); the shell protocol is the reliable
    route, so record it instead of pretending the app is absent.
    """
    out: dict[str, str] = {}
    try:
        ps = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-StartApps | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=45,
            creationflags=noconsole.flags())
        if ps.returncode == 0 and ps.stdout.strip():
            import json as _json
            data = _json.loads(ps.stdout)
            if isinstance(data, dict):
                data = [data]
            for item in data:
                name = (item.get("Name") or "").strip()
                aid = (item.get("AppID") or "").strip()
                if name and aid:
                    out[name.lower()] = aid
    except Exception:
        pass
    return out


def _screen() -> dict:
    """Physical resolution + DPI scale. Requires a DPI-aware process."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass
    u = ctypes.windll.user32
    w, h = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    try:
        hdc = u.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
        u.ReleaseDC(0, hdc)
    except Exception:
        dpi = 96
    return {
        "w": w, "h": h,
        "dpi": dpi,
        "dpi_scale": round(dpi / 96.0, 2),
        "monitors": u.GetSystemMetrics(80),  # SM_CMONITORS
    }


def build_profile() -> dict:
    """Detect everything a portable macro might need on this machine."""
    symbols: dict[str, str] = {}
    capabilities: list[str] = []
    _ROOT_INDEX.clear()          # a rebuild must see what was installed since

    for sym, guid in KNOWN_FOLDERS.items():
        p = _known_folder(guid)
        if p:
            symbols[sym] = p.replace("\\", "/")

    symbols["USER_HOME"] = str(Path.home()).replace("\\", "/")
    symbols["TEMP"] = str(Path(os.environ.get("TEMP", "/tmp"))).replace("\\", "/")

    start_apps = _appx_packages()
    for sym, (exe, cap) in KNOWN_APPS.items():
        p = _app_path(exe)
        if p:
            symbols[f"APP:{sym}"] = p.replace("\\", "/")
            capabilities.append(cap)
            continue
        # Not on disk where we can reach it — fall back to the Start menu's
        # AppUserModelID, launchable via  explorer shell:AppsFolder\<id>
        match = next((aid for name, aid in start_apps.items()
                      if sym in name or name.startswith(sym)), None)
        if match:
            symbols[f"APP:{sym}"] = f"shell:AppsFolder\\{match}"
            capabilities.append(cap)

    if os.environ.get("SystemRoot"):
        capabilities.append("uia")

    return {
        "schema_version": 1,
        "machine": {
            "os": platform.platform(),
            "release": platform.release(),
            "user": os.environ.get("USERNAME", "?"),
            # fingerprint is informational; never used for auth
            "fingerprint": f"{platform.node()}-{platform.machine()}",
        },
        "screen": _screen(),
        "symbols": symbols,
        "capabilities": sorted(set(capabilities)),
    }


def load_profile(refresh: bool = False) -> dict:
    if PROFILE_PATH.exists() and not refresh:
        try:
            return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    prof = build_profile()
    save_profile(prof)
    return prof


def save_profile(profile: dict) -> None:
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(json.dumps(profile, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Symbol resolution
# ---------------------------------------------------------------------------

class MissingSymbol(Exception):
    """A macro referenced a symbol this machine cannot provide."""


def resolve(text: str, profile: dict | None = None,
            params: dict | None = None) -> str:
    """Expand {SYMBOL} and {param} references in a string.

    Raises MissingSymbol rather than silently leaving a broken path — a macro
    that clicks into the void is worse than one that refuses to start.
    """
    import re
    prof = profile or load_profile()
    syms = prof.get("symbols", {})
    params = params or {}

    def sub(m):
        key = m.group(1)
        if key in params:
            return str(params[key])
        if key in syms:
            return syms[key]
        raise MissingSymbol(key)

    return re.sub(r"\{([A-Za-z0-9_:]+)\}", sub, text)


def check_requirements(macro_symbols: list[str], macro_caps: list[str],
                       profile: dict | None = None) -> tuple[bool, list[str]]:
    """Pre-flight a macro against this machine. Returns (ok, missing)."""
    prof = profile or load_profile()
    syms, caps = prof.get("symbols", {}), set(prof.get("capabilities", []))
    missing = [s for s in macro_symbols if s not in syms]
    missing += [f"capability:{c}" for c in macro_caps if c not in caps]
    return (not missing), missing


if __name__ == "__main__":
    prof = load_profile(refresh=True)
    print(json.dumps(prof, indent=2))
