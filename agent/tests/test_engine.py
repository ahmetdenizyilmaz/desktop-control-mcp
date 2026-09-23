"""The script engine: Jev first, Claude second, the person third.

What would fail SILENTLY, and so what these check:

  * a script run when Jev was not sure - or when it had no key at all and
    the "decision" was the offline stub;
  * a stuck script handed to Claude without saying what was already done,
    so Claude redoes it (ten products bought twice);
  * a person moving the mouse ENDING a script instead of pausing it;
  * ESC+ESC or Stop treated as a failure to rescue, rather than a stop;
  * a Jev key written into settings.json instead of the vault;
  * an app update overwriting a script the user or Claude changed.

Nothing here touches the screen or the network. The data directory is a
throwaway, so no script, key or setting leaks into the real ones.

    python tests/test_engine.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cleanup import scratch_dir  # noqa: E402

_TMP = str(scratch_dir("engine_test_"))
os.environ["DESKTOP_AGENT_DATA"] = _TMP

from core import engine, keys, pipeline_bridge as bridge     # noqa: E402
from core import fast                                        # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""),
          flush=True)
    return bool(ok)


said: list[tuple[str, str]] = []


def say(kind, text):
    said.append((kind, text))


# --------------------------------------------------------------------------
print("\nscripts live in the data folder, seeded once, never overwritten")
# --------------------------------------------------------------------------

root = bridge.scripts_root()
check("with a data dir set, scripts live under it - not in the bundle",
      str(root).startswith(_TMP), str(root))
copied = bridge.seed_scripts()
check("first run seeds the shipped scripts",
      "migros/weekly-shop.json" in copied and (root / "migros").is_dir(),
      f"{len(copied)} copied")
mine = root / "migros" / "weekly-shop.json"
mine.write_text(mine.read_text(encoding="utf-8").replace(
    "Open Migros", "MY EDIT Open Migros"), encoding="utf-8")
again = bridge.seed_scripts()
check("seeding again copies nothing and keeps the user's edit",
      again == [] and "MY EDIT" in mine.read_text(encoding="utf-8"),
      str(again[:3]))
(root / "broken").mkdir(exist_ok=True)
(root / "broken" / "bad.json").write_text("{nope", encoding="utf-8")
lib = bridge.library(fresh=True)
check("one unreadable script does not hide the others",
      "migros/weekly-shop" in lib.local() and len(lib.errors) == 1,
      f"{len(lib.local())} scripts, errors={lib.errors}")

# --------------------------------------------------------------------------
print("\nwhich scripts a task is matched against")
# --------------------------------------------------------------------------

rows = {r["name"] for r in engine.listed_rows(lib)}
check("a script someone asks for by name is listed",
      "migros/weekly-shop" in rows, str(sorted(rows)))
check("a building block other scripts call is not",
      "browser/goto" not in rows and "migros/add-product" not in rows,
      str(sorted(rows)))
check("`listed: true` overrides the guess (spotify is also called by an "
      "errand)", "media/spotify-search-play" in rows)


# --------------------------------------------------------------------------
print("\nrecognise: Jev decides, and only when it is sure")
# --------------------------------------------------------------------------

class FakeClient:
    def __init__(self, offline=False):
        self.offline = offline
        self.backend = "fake"


real_rerank = fast.rerank


def rerank_says(idx, why="confidence 0.93"):
    def fake(task, cands, at=0.6, client=None):
        v = fast.Verdict(idx is not None, why)
        if isinstance(idx, str):
            names = [c["name"] for c in cands]
            return (names.index(idx) if idx in names else None), v
        return idx, v
    return fake


eng = engine.ScriptEngine({}, say, client=FakeClient(offline=True))
name, why = eng.recognise("do my weekly shop")
check("no Jev key: nothing is run, and the reason names Settings",
      name is None and "Settings" in why, why)

eng = engine.ScriptEngine({}, say, client=FakeClient())
fast.rerank = rerank_says("migros/weekly-shop")
name, why = eng.recognise("do my weekly shop")
check("a confident Jev choice names the script",
      name == "migros/weekly-shop" and "Jev chose" in why, f"{name} {why}")
fast.rerank = rerank_says(None, "no saved job matches")
name, why = eng.recognise("what is the weather")
check("'none of these' runs nothing", name is None and "no saved" in why, why)


def boom(*a, **k):
    raise RuntimeError("network down")


fast.rerank = boom
name, why = eng.recognise("anything")
check("a Jev failure falls through to Claude instead of crashing",
      name is None and "network down" in why, why)
fast.rerank = real_rerank

# --------------------------------------------------------------------------
print("\nhandle: done, stopped, or handed to Claude with the full story")
# --------------------------------------------------------------------------

from script.runner import Report, Outcome as StepOutcome   # noqa: E402


class FakeRunner:
    def __init__(self, report, helper=None):
        self.report = report
        self.helper = helper


def engine_with(report, runner_none=False, params=None):
    e = engine.ScriptEngine({}, say, client=FakeClient())
    e.recognise = lambda task: ("migros/weekly-shop", "Jev chose it")
    e.fill_params = lambda script, task: params if params is not None else {}
    e.execute = (lambda n, a, t="": None) if runner_none \
        else (lambda n, a, t="": FakeRunner(report))
    return e


ok_rep = Report(script="migros/weekly-shop")
ok_rep.outcomes.append(StepOutcome("x", "click", "clicked", True, 1))
out = engine_with(ok_rep).handle("weekly shop")
check("a clean run is DONE - no Claude turn at all", out.kind == "done",
      out.kind)

stop_rep = Report(script="migros/weekly-shop", stopped=True,
                  stopped_by="'cancel' in the chat")
out = engine_with(stop_rep).handle("weekly shop")
check("a stopped run stays stopped - Stop is not a failure to rescue",
      out.kind == "stopped", out.kind)

stuck = Report(script="migros/weekly-shop")
stuck.items = {"süt": True, "peynir": True, "yumurta": False}
stuck.bought = {"İçim Süt 1 L": 1, "Ekmek": 3}
stuck.outcomes.append(StepOutcome("migros/add-product.steps[1]", "click",
                                  "no element matched 'Sepete ekle'",
                                  False, 1))
out = engine_with(stuck).handle("weekly shop")
p = out.prompt
check("a stuck run is handed to Claude", out.kind == "claude", out.kind)
check("the handover says NOT to redo what was done",
      "do NOT redo" in p and "süt" in p and "peynir" in p, p[:200])
check("the handover lists what is still left",
      "NOT done" in p and "yumurta" in p)
check("the handover names what was already bought, with quantities",
      "3 x Ekmek" in p)
check("the handover says where and why it failed",
      "no element matched 'Sepete ekle'" in p)

out = engine_with(ok_rep, params=None).handle("weekly shop")
e = engine_with(ok_rep)
e.fill_params = lambda script, task: None
out = e.handle("play something")
check("missing required details hand the task to Claude, not a guess",
      out.kind == "claude" and "needs details" in out.why, out.why)

out = engine_with(ok_rep, runner_none=True).handle("weekly shop")
check("a run abandoned in a hung call reports stopped",
      out.kind == "stopped")

# --------------------------------------------------------------------------
print("\nthe actuator asks the arbiter first")
# --------------------------------------------------------------------------

import tools_sdk                                     # noqa: E402
from script.runner import Stopped                    # noqa: E402

ga = engine.GuardedActuator(say)
real_blocked = tools_sdk._blocked
tools_sdk._blocked = lambda: "STOP: the user pressed ESC+ESC. Abandon."
try:
    ga._gate()
    stopped = False
except Stopped:
    stopped = True
check("ESC+ESC raises Stopped - never a rescue", stopped)


class FakeActivity:
    def __init__(self):
        self.t0 = time.time()

    def idle_s(self):
        return time.time() - self.t0          # idle from now on


class FakeArbiter:
    def __init__(self):
        self._activity = FakeActivity()
        self.rebased = False
        self.aborted = False

    def rebaseline(self, settle_s=0.8):
        self.rebased = True


calls = {"n": 0}


def blocked_once():
    calls["n"] += 1
    return ("STOP: the user is using their machine. Do not continue."
            if calls["n"] == 1 else None)


tools_sdk._blocked = blocked_once
fa = FakeArbiter()
tools_sdk.set_arbiter(fa)
engine.IDLE_TO_RESUME_S = 0.5
t0 = time.time()
try:
    ga._gate()
    paused_ok = True
except Stopped:
    paused_ok = False
waited = time.time() - t0
check("a person using the machine PAUSES the script, then it continues",
      paused_ok and fa.rebased and 0.4 <= waited < 3,
      f"ok={paused_ok} rebased={fa.rebased} waited={waited:.2f}s")

tools_sdk._blocked = lambda: "STOP: the user is using their machine."
tools_sdk.set_cancelled(True)
try:
    ga._gate()
    stop_during_pause = False
except Stopped:
    stop_during_pause = True
check("Stop while paused for the person ends the run",
      stop_during_pause)
tools_sdk.set_cancelled(False)
tools_sdk.set_arbiter(None)
tools_sdk._blocked = real_blocked

# --------------------------------------------------------------------------
print("\nJev's key lives in the vault")
# --------------------------------------------------------------------------

settings = {"jev_backend": "typesafe"}
check("no key: the client is offline, honestly",
      keys.jev_client(settings).offline)
r = keys.save_jev_key("typesafe", "ts-secret-123")
check("saving a key succeeds", r.get("ok") and r.get("saved"), str(r))
check("the key is found afterwards", keys.has_jev_key(settings))
check("and the client is no longer offline",
      not keys.jev_client(settings).offline)
leaked = [p.name for p in Path(_TMP).rglob("*.json")
          if "ts-secret-123" in p.read_text(encoding="utf-8",
                                            errors="ignore")]
check("the key is in no plain JSON file", leaked == [], str(leaked))
r = keys.save_jev_key("typesafe", "")
check("an empty key clears it", r.get("ok") and not keys.has_jev_key(settings))
check("an unknown backend is refused",
      not keys.save_jev_key("nope", "x").get("ok"))
t = keys.test_jev({"jev_backend": "typesafe"})
check("testing with no key says so, without calling out",
      not t["ok"] and "no key" in t["error"], str(t))

# --------------------------------------------------------------------------
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print(f"\n{passed}/{total} passed")
for n, ok, d in results:
    if not ok:
        print(f"  FAILED: {n} — {d}")
sys.exit(0 if passed == total else 1)
