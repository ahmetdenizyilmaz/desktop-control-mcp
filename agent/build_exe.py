"""Package the app as a single Windows executable.

    python agent/build_exe.py            build
    python agent/build_exe.py --clean    rebuild from scratch

Produces  dist/AGIAgent.exe  — one file, no Python install needed.

**What the .exe still needs on the target machine.** The Agent SDK does not
embed a model client; it drives the Claude Code CLI as a subprocess. So the
executable is self-contained as far as Python goes, and *not* self-contained as
far as Claude goes: `claude.exe` must be installed and signed in. Bundling it
is not an option — it is a separate product with its own auth and update path,
and shipping a stale copy inside this exe would be worse than requiring the
real one. The app checks at startup and says so plainly rather than failing
with a subprocess error nobody can read.

**What is deliberately left out.** The OCR labelling tier pulls torch, easyocr
and ultralytics — together well over 2GB, for a tier that is off by default and
used only when a window publishes no names. Bundling it would multiply the
download by roughly fifty to serve a fallback most tasks never reach. It stays
a pip install for anyone who wants it; `locator.ENABLE_OCR` is the switch.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DIST = REPO / "dist"
WORK = REPO / "build"

NAME = "AGIAgent"

#: Embedded in the .exe (what Explorer and a shortcut show) AND shipped as a
#: data file (what the window sets on itself at runtime — see app.py). Two
#: copies of one 29KB file is cheaper than two ways for them to differ.
ICON = HERE / "assets" / "icon.ico"

#: Pulled in only by the opt-in OCR tier. Excluding them is the difference
#: between a ~60MB download and a ~2.5GB one.
EXCLUDE = [
    "torch", "torchvision", "torchaudio", "easyocr", "ultralytics",
    "transformers", "timm", "scipy", "matplotlib", "pandas", "sympy",
    "IPython", "notebook", "tkinter", "test", "unittest",
]

#: Imported lazily or by string, so PyInstaller's static analysis misses them.
HIDDEN = [
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "clr_loader",
    "pythonnet",
    "playwright",
    "playwright.sync_api",
    "psutil",
    "mss",
    "PIL.Image",
    "comtypes",
    "comtypes.stream",
    "uiautomation",
    # The script engine (pipeline/, found through --paths below). The app
    # imports it lazily - only when a task is routed - so static analysis
    # sees none of it.
    "script", "script.runner", "script.library", "script.schema",
    "script.resolve", "script.choose", "script.helper", "script.guard",
    "vision", "vision.elements", "vision.capture", "vision.ocr",
    "vision.query", "vision.templates", "cursor", "winocr", "numpy",
    "core.engine", "core.keys", "core.pipeline_bridge", "core.progress",
    "core.waiting", "core.cancel_watch", "core.authoring", "core.ledger",
    "core.checkpoint", "core.quick", "core.tasklog", "core.claude_api",
    # Direct Claude calls (core/claude_api) - imported inside a function.
    "anthropic",
]

PIPELINE = REPO / "pipeline"

DATA = [
    (HERE / "ui" / "app.html", "ui"),
    (ICON, "assets"),
    # Shipped scripts. Copied into the data folder on first use and edited
    # there (core/pipeline_bridge.py) - the bundle itself is a temp dir.
    (PIPELINE / "script" / "scripts", "pipeline/script/scripts"),
]


def check_prereqs() -> list[str]:
    problems = []
    if sys.platform != "win32":
        problems.append("this builds a Windows executable; run it on Windows")
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        problems.append("pip install pyinstaller")
    for src, _ in DATA:
        if not src.exists():
            problems.append(f"missing data file: {src}")
    return problems


def build(clean: bool = False) -> int:
    problems = check_prereqs()
    if problems:
        print("Cannot build:")
        for p in problems:
            print(f"  - {p}")
        return 1

    if clean:
        for d in (DIST, WORK):
            shutil.rmtree(d, ignore_errors=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--windowed",                   # no console window behind the app
        "--name", NAME,
        "--icon", str(ICON),
        "--distpath", str(DIST),
        "--workpath", str(WORK),
        "--specpath", str(WORK),
        "--paths", str(HERE),
        "--paths", str(PIPELINE),
    ]
    for mod in EXCLUDE:
        cmd += ["--exclude-module", mod]
    for mod in HIDDEN:
        cmd += ["--hidden-import", mod]
    for src, dest in DATA:
        cmd += ["--add-data", f"{src}{';' if sys.platform == 'win32' else ':'}{dest}"]
    cmd.append(str(HERE / "app.py"))

    print("Building. This takes a few minutes the first time.\n")
    r = subprocess.run(cmd, cwd=str(REPO))
    if r.returncode != 0:
        return r.returncode

    exe = DIST / f"{NAME}.exe"
    if not exe.exists():
        print("\nBuild reported success but produced no executable.")
        return 1

    size_mb = exe.stat().st_size / (1024 * 1024)
    print(f"\n{exe}  ({size_mb:.0f} MB)")
    print("\nOn any machine this runs on, Claude Code must be installed and "
          "signed in — the SDK drives `claude.exe` as a subprocess.")
    print("The OCR labelling tier is not bundled; `pip install easyocr torch` "
          "if a window turns out to publish no control names.")
    return 0


if __name__ == "__main__":
    raise SystemExit(build(clean="--clean" in sys.argv))
