"""Close what the tests opened, and nothing else.

The suites launch real applications on purpose — that is the point of them —
but a suite that crashes, or one that simply never tore down, leaves those
windows on screen. Six test runs left six File Explorers and a dozen Notepads.

The rule here is conservative: close only windows that are provably fixtures,
never anything that could hold work.

  * a title carrying the test's own marker            -> definitely ours
  * an UNMODIFIED Untitled Notepad                    -> nothing to lose
  * anything else at all, including File Explorer     -> LEFT ALONE
  * a modified Notepad (title starts with `*`)        -> LEFT ALONE
  * the app itself, or the editor running it          -> LEFT ALONE

The same rule covers the temp directories the suites create. Each one used
to call tempfile.mkdtemp() and walk away; %LOCALAPPDATA%\\Temp gained a few
directories per run, and after some hundreds of runs the machine-profile
scan that walks it was pushing a suite over its timeout. So:

  * a suite asks scratch_dir() for its directory, and it is removed when
    the suite's process exits — no teardown to forget
  * sweep_temp() removes what a crashed or killed suite left behind: only a
    name that is a registered prefix plus mkdtemp's eight-character tail,
    only directly under the system temp directory, and only when old enough
    that no run can still be using it

Run directly, or let tests/run_all.py call it after a suite finishes.
"""
from __future__ import annotations

import atexit
import ctypes
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

# core is imported lazily, inside the functions that need it: two suites
# import this module before they have pointed DESKTOP_AGENT_DATA at a
# throwaway directory, and core.paths must not be loaded before that.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

WM_CLOSE = 0x0010
u = ctypes.windll.user32

#: Windows the tests create. Matched against the title.
#: Only names no human would have on screen. "Calculator" was here
#: too, and that was wrong: the suite reuses an already-open one, so
#: the sweep closed the user's. A suite now closes what it opened.
FIXTURE_TITLES = ("BENCH TARGET", "E2E TARGET")

#: Files the end-to-end test writes are stamped with this, so its Notepad
#: windows are identifiable no matter how many runs pile up.
TEST_MARKER = re.compile(r"desktopagent\d{6,}", re.I)

#: Never close these, whatever else matches. The editor running this is how
#: the user is talking to us; closing it would end the session mid-sweep.
PROTECTED = ("Claude GUI", "AGI Agent", "Desktop Agent", "Claude Code",
             "Visual Studio Code", "Cursor", "Windsurf")


def _protected(title: str) -> bool:
    return any(p.lower() in title.lower() for p in PROTECTED)


def classify(title: str) -> str:
    """close / keep / skip — and the reason is the name of the bucket."""
    t = title.strip()
    if not t or _protected(t):
        return "skip"
    if TEST_MARKER.search(t):
        return "close"
    if any(f in t or t == f for f in FIXTURE_TITLES):
        return "close"
    if t == "Untitled - Notepad":
        # No leading `*` means Notepad has nothing unsaved in it.
        return "close"
    if t.startswith("*") and "Notepad" in t:
        return "keep"          # unsaved work that is not ours
    # NOT closed any more: "Home - File Explorer". That rule existed because
    # the packaged Notepad the end-to-end suite used to drive opened one on
    # every launch. That fixture is gone, so the only Explorer windows left
    # are the user's own — and closing someone's file manager because a test
    # ran is exactly the kind of thing that makes a tool untrustworthy.
    return "skip"


#: A fixture with unsaved scratch text pops a save prompt on WM_CLOSE and then
#: simply stays open. Answering "Don't save" is only ever correct for a window
#: classify() already proved is ours — never for the user's.
DISCARD_BUTTONS = ("Don't save", "Do&n't save", "Don't Save", "No")


def _dismiss_save_prompts(rounds: int = 3) -> int:
    """Answer the save prompts our own fixtures raise on close."""
    from core import actuator, locator

    dismissed = 0
    for _ in range(rounds):
        found = False
        for w in locator.list_windows():
            title = w["title"].strip()
            # The prompt is a small dialog owned by the app; it carries the
            # app's name, not the document's.
            if title not in ("Notepad", "Microsoft Notepad", ""):
                continue
            for label in DISCARD_BUTTONS:
                el = locator.find_labelled(w["hwnd"], label)
                if el is not None:
                    actuator.click(el, prefer="invoke")
                    dismissed += 1
                    found = True
                    break
        if not found:
            break
        time.sleep(0.6)
    return dismissed


def sweep(dry_run: bool = False) -> dict:
    from core import locator

    closed, kept = [], []
    for w in locator.list_windows():
        verdict = classify(w["title"])
        if verdict == "close":
            if not dry_run:
                u.PostMessageW(w["hwnd"], WM_CLOSE, 0, 0)
            closed.append(w["title"])
        elif verdict == "keep":
            kept.append(w["title"])

    dismissed = 0
    if closed and not dry_run:
        time.sleep(1.2)
        dismissed = _dismiss_save_prompts()
        time.sleep(1.0)

    killed = 0
    if not dry_run:
        try:
            import psutil
            for p in psutil.process_iter(["pid", "cmdline"]):
                try:
                    if "target_app" in " ".join(p.info["cmdline"] or []):
                        p.kill()
                        killed += 1
                except Exception:
                    pass
        except Exception:
            pass

    left = [w["title"] for w in locator.list_windows()
            if classify(w["title"]) == "close"]
    return {"closed": closed, "kept": kept, "killed": killed,
            "dismissed": dismissed, "left": left}


# ---------------------------------------------------------------------------
# Temp directories
# ---------------------------------------------------------------------------

#: Every prefix a suite hands to scratch_dir(). Leftovers with these names
#: under the system temp directory — and nowhere else — are what sweep_temp()
#: removes. scratch_dir() refuses a prefix that is not listed here, so a new
#: suite cannot quietly start leaking again.
TEMP_PREFIXES = (
    "agent_store_", "agent_vault_", "agent_ret_", "agent_sched_",
    "agent_sched2_", "agent_sched3_", "agent_induce_", "agent_p11_prof_",
    "agent_e2e_store_", "p12_", "p12store_", "lessons_test_", "clip_test_",
    "clip_bad_", "clip_seq_", "stop_test_", "agi_migrate_", "dl_test_",
    "engine_test_", "grounding_test_",
)

#: mkdtemp appends exactly eight characters from [a-z0-9_]. Insisting on that
#: shape means a directory somebody made by hand that happens to start with
#: "agent_" is never touched.
_TEMP_DIR_RE = re.compile(
    "^(?:" + "|".join(re.escape(p) for p in TEMP_PREFIXES) + r")[a-z0-9_]{8}$")

#: run_all.py's per-suite log: NamedTemporaryFile(delete=False) with a
#: `_test_<suite>.log` suffix. It is unlinked after the run unless a fixture
#: window inherited the handle, in which case it lingers here.
_TEMP_LOG_RE = re.compile(r"^tmp[a-z0-9_]{8}_test_[a-z0-9_]+\.log$")

#: A leftover younger than this may belong to a run still in progress — in
#: another terminal, or one of the interactive suites, which take a while —
#: so the sweep leaves it for next time. The suite's own exit hook removes it
#: anyway; the sweep is only the backstop for a crash or a kill.
TEMP_MIN_AGE_S = 3600


def scratch_dir(prefix: str) -> Path:
    """A throwaway directory that is removed when the suite's process exits."""
    if prefix not in TEMP_PREFIXES:
        raise ValueError(
            f"{prefix!r} is not in cleanup.TEMP_PREFIXES; add it so the sweep "
            "can find what a crashed run leaves behind")
    p = Path(tempfile.mkdtemp(prefix=prefix))
    atexit.register(remove_tree, p)
    return p


def remove_tree(p: Path) -> bool:
    """rmtree that copes with read-only files (git objects, browser profiles).

    Returns True when the directory is gone. A file some process still holds
    open cannot be removed on Windows; then this returns False and the next
    sweep_temp() gets it.
    """
    def _make_writable_and_retry(func, path, _exc):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except OSError:
            pass

    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(p, onexc=_make_writable_and_retry)
        else:                                       # onerror passes exc_info
            shutil.rmtree(p, onerror=lambda f, path, ei:
                          _make_writable_and_retry(f, path, ei[1]))
    except OSError:
        pass
    return not p.exists()


def _tree_size(path: str) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.stat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


def sweep_temp(dry_run: bool = False,
               min_age_s: float = TEMP_MIN_AGE_S) -> dict:
    """Remove what earlier test runs left in the system temp directory.

    Three conditions, all required: the name is a registered prefix plus
    mkdtemp's tail (or the runner's log-file shape); the entry sits directly
    under the temp directory; and it is older than min_age_s by both its
    creation and its modification time. Nothing else is looked at.
    """
    root = Path(tempfile.gettempdir()).resolve()
    out = {"root": str(root), "removed": [], "skipped": [], "freed_bytes": 0}
    try:
        entries = list(os.scandir(root))
    except OSError:
        return out
    now = time.time()
    for entry in entries:
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
            ours = (_TEMP_DIR_RE.match(entry.name) if is_dir
                    else _TEMP_LOG_RE.match(entry.name))
            if not ours:
                continue
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        born = getattr(st, "st_birthtime", st.st_ctime)
        if now - max(born, st.st_mtime) < min_age_s:
            out["skipped"].append(entry.name)       # may still be in use
            continue
        size = _tree_size(entry.path) if is_dir else st.st_size
        if dry_run:
            gone = True
        elif is_dir:
            gone = remove_tree(Path(entry.path))
        else:
            try:
                os.unlink(entry.path)
                gone = True
            except OSError:
                gone = False
        if gone:
            out["removed"].append(entry.name)
            out["freed_bytes"] += size
        else:
            out["skipped"].append(entry.name)
    return out


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    r = sweep(dry_run=dry)
    tag = "would close" if dry else "closed windows"
    print(f"{tag:<14}: {len(r['closed'])}")
    for t in r["closed"]:
        print(f"    - {t[:66]}")
    if r["kept"]:
        print(f"{'left alone':<14}: {len(r['kept'])}  (unsaved work, not ours)")
        for t in r["kept"]:
            print(f"    - {t[:66]}")
    print(f"{'save prompts':<14}: {r['dismissed']} answered (our fixtures only)")
    print(f"{'strays killed':<14}: {r['killed']}")
    print(f"{'still open':<14}: {', '.join(r['left']) if r['left'] else 'none'}")
    t = sweep_temp(dry_run=dry)
    verb = "would remove" if dry else "removed"
    print(f"{'temp dirs':<14}: {verb} {len(t['removed'])} "
          f"({t['freed_bytes'] / 1e6:.1f} MB), skipped {len(t['skipped'])} "
          f"under {t['root']}")
