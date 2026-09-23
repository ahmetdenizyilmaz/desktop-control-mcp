"""Where the pipeline lives, and where its scripts are kept.

`pipeline/` is a sibling of `agent/` in the source tree and is bundled into
the packaged build (see build_exe.py). Its script library cannot stay where
the code is in a packaged build: that is `_MEIPASS`, a temporary directory
deleted on exit, so every script Claude wrote or repaired would vanish with
the process. So:

    from source        pipeline/script/scripts        (edit them in place)
    packaged / tests   <data>/scripts                 seeded from the build

Seeding copies only files that are MISSING, never overwrites: a script the
user or Claude changed is theirs, and an app update must not revert it.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from . import paths

REPO = Path(__file__).resolve().parents[2]


def pipeline_dir() -> Path:
    if paths.FROZEN:
        return paths.resource("pipeline")
    return REPO / "pipeline"


def ensure_importable() -> bool:
    """Put `pipeline/` on sys.path. False if it is not there at all."""
    d = pipeline_dir()
    if not (d / "script").is_dir():
        return False
    if str(d) not in sys.path:
        # Appended, like the repo root in arbiter.py: `pipeline/` has no
        # module named like anything in agent/, but appending can never
        # shadow the app's own imports.
        sys.path.append(str(d))
    return True


def _shipped_scripts() -> Path:
    return pipeline_dir() / "script" / "scripts"


def _uses_data_dir() -> bool:
    return paths.FROZEN or bool(os.environ.get("DESKTOP_AGENT_DATA"))


def scripts_root() -> Path:
    if _uses_data_dir():
        root = paths.data("scripts", ".keep").parent
        return root
    return _shipped_scripts()


def seed_scripts() -> list[str]:
    """Copy shipped scripts the data folder does not have yet."""
    if not _uses_data_dir():
        return []
    src, dst = _shipped_scripts(), scripts_root()
    if not src.is_dir():
        return []
    copied = []
    for f in src.rglob("*.json"):
        rel = f.relative_to(src)
        if rel.parts and rel.parts[0] in (".trash", ".history", ".cache"):
            continue
        out = dst / rel
        if out.exists():
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, out)
        copied.append(rel.as_posix())
    return copied


_lib = None


def library(fresh: bool = False):
    """The shared script library. Tolerant: a bad file is listed in
    `.errors`, not fatal."""
    global _lib
    if _lib is None or fresh:
        if not ensure_importable():
            raise RuntimeError(f"pipeline not found at {pipeline_dir()}")
        seed_scripts()
        from script.library import Library
        _lib = Library(roots=[scripts_root()],
                       cache=paths.data("scripts-cache", ".keep").parent,
                       strict=False)
    return _lib
