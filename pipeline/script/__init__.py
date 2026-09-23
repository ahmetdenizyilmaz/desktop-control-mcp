"""A script language for sequences of screen actions.

    from script import Library, Runner, DesktopActuator
    from vision.elements import Watcher

    lib = Library()
    run = lib.resolve("migros/weekly-shop")     # fetches, verifies, checks
    report = Runner(run, Watcher(), DesktopActuator()).run()
    print(report.summary())

Four modules, in the order they matter:

    schema.py    what a script may say, and what it may not
    library.py   where scripts come from, and how a remote one is pinned
    resolve.py   turning "the Ara button" into one element on screen
    runner.py    executing it, and admitting when it did not work
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

# `script` sits beside `vision` and imports from it. Putting the pipeline
# root on the path here means both `import script` and `import
# pipeline.script` work, rather than only whichever one the caller guessed.
_ROOT = str(_Path(__file__).resolve().parents[1])
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)

from .guard import is_commit                                   # noqa: E402
from .library import (CACHE, SCRIPTS, FetchError, Library,      # noqa: E402
                      Resolved, pin, to_json)
from .resolve import Resolution, find                          # noqa: E402
from .runner import (Actuator, DesktopActuator, Outcome,        # noqa: E402
                     Report, Runner, StepFailed, Stopped)
from .schema import (MAX_CALL_DEPTH, Script, ScriptError,       # noqa: E402
                     Selector, Step, Trust, parse)

__all__ = [
    "Library", "Resolved", "Runner", "Report", "Outcome", "Actuator",
    "DesktopActuator", "Script", "Step", "Selector", "Trust", "parse",
    "find", "Resolution", "is_commit", "pin", "to_json",
    "ScriptError", "FetchError", "StepFailed", "Stopped",
    "SCRIPTS", "CACHE", "MAX_CALL_DEPTH",
]
