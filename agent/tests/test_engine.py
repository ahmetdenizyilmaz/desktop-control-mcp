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
name, why = eng.recognise("do the migros weekly shop")
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
print("\nwaiting for a person is not being stuck")
# --------------------------------------------------------------------------
from core.waiting import WaitPolicy                          # noqa: E402

notes, paused = [], {"on": False}
wp = WaitPolicy(0.3, 0.8, notify=notes.append, say=say,
                set_paused=lambda on: paused.__setitem__("on", on),
                is_paused=lambda: paused["on"], task="weekly shop")
wp.begin("'buy' in the chat")
wp.tick()
check("no reminder before its time", notes == [] and not paused["on"])
time.sleep(0.35)
wp.tick()
check("a reminder after remind_after", len(notes) == 1
      and "Still waiting" in notes[0] and "'buy'" in notes[0], str(notes))
wp.tick()
check("…only once", len(notes) == 1)
time.sleep(0.5)
wp.tick()
check("the task PAUSES at the limit - it does not fail",
      paused["on"] and wp.paused_by_us and len(notes) == 2, str(notes))
paused["on"] = False              # the person presses Resume
wp.tick()
check("Resume restarts the wait with a fresh clock",
      wp.active and not wp.paused_by_us and not wp.reminded
      and wp.waited() < 0.2)
wp.end()
check("an answer ends the wait", not wp.active and wp.view() == {})

# --------------------------------------------------------------------------
print("\nstill, loading, or stuck - from pixels")
# --------------------------------------------------------------------------
import numpy as np                                           # noqa: E402
from core.progress import diff_frames                        # noqa: E402

a = np.zeros((100, 240), dtype=np.uint8)
b = a.copy()
check("identical frames: no change, no motion",
      diff_frames(a, b) == (False, False))
b[50, 10:13] = 200                 # a spinner's few pixels
check("a few pixels (a spinner) = motion, not a change",
      diff_frames(a, b) == (False, True))
c = a.copy()
c[:30, :] = 200                    # a repaint
check("a repaint = a change", diff_frames(a, c)[0] is True)


class FakePulse:
    def __init__(self, still, busy):
        self._still, self._busy = still, busy

    def still_for(self):
        return self._still

    def busy(self):
        return self._busy

    def touch(self):
        pass


pe = engine.ScriptEngine({"no_change_seconds": 10, "busy_grace_seconds": 30},
                         say, client=FakeClient())
pe._last_acted = time.time() - 60
pe.pulse = FakePulse(still=15, busy=False)
check("dead still for 15s after the last action = stuck, fail now",
      pe._patience(12, 45) == "fail")
pe.pulse = FakePulse(still=3, busy=False)
check("something moved 3s ago = not stuck yet", pe._patience(12, 45) == "keep")
pe.pulse = FakePulse(still=0.5, busy=True)
check("still loading at the timeout = extend",
      pe._patience(46, 45) == "extend")
check("…but not forever (past the busy grace)",
      pe._patience(80, 45) == "keep")
pe._waiting = True
pe.pulse = FakePulse(still=600, busy=False)
check("while WAITING for a person, a still screen is never stuck",
      pe._patience(600, 45) == "keep")
pe._waiting = False
pe._last_acted = time.time()
pe.pulse = FakePulse(still=15, busy=False)
check("just acted: too early to call it stuck",
      pe._patience(12, 45) == "keep")

pe2 = engine.ScriptEngine({}, say, client=FakeClient())
pe2._phase("wait_begin", {"reason": "'buy' in the chat", "where": "x"})
check("a wait marks the engine WAITING and the tool as a wait",
      pe2._waiting and pe2.status.state == "waiting")
pe2._phase("wait_end", {"where": "x"})
check("…and the end of it clears that", not pe2._waiting)

# --------------------------------------------------------------------------
print("\ncancel from anywhere")
# --------------------------------------------------------------------------
from core import cancel_watch as cw                          # noqa: E402

check("'cancel', 'İptal', '/stop' and 'cancel 01:16' count",
      all(cw.is_cancel(t) for t in ("cancel", "iptal", "/stop",
                                    "Cancel.", "cancel 01:16")))
check("a sentence or 'c-a-n-c-e-l' does not",
      not any(cw.is_cancel(t) for t in (
          "Durdurmak icin c-a-n-c-e-l", "please cancel my order", "cancelled",
          "stop the music at 5")))

from core import channel as chan_mod                          # noqa: E402

fired = []


class FakeTG:
    configured = True

    def __init__(self, texts):
        self.texts = list(texts)

    def _updates(self, timeout=0):
        return [{"message": {"text": t}} for t in self.texts]


real_tg, real_pending = chan_mod.channel.telegram, chan_mod.channel.pending
chan_mod.channel.telegram = FakeTG(["hello", "iptal"])
w = cw.CancelWatch(fired.append)
w._telegram()
check("'iptal' on Telegram fires the cancel", fired == ["'cancel' on Telegram"],
      str(fired))
fired.clear()
w2 = cw.CancelWatch(fired.append)
chan_mod.channel.pending = {"question": "Allow?"}
w2._telegram()
check("…but not while a Telegram question is waiting for its own answer",
      fired == [])
chan_mod.channel.telegram, chan_mod.channel.pending = real_tg, real_pending

# --------------------------------------------------------------------------
print("\nClaude writes a script; a fix is saved back into the right step")
# --------------------------------------------------------------------------
from core import authoring                                   # noqa: E402

check("a step path through a repeat and a call parses",
      authoring.parse_where("site/x.steps[2][süt].steps[0]")
      == ("site/x", [("steps", 2), ("steps", 0)]))
check("if/else paths parse",
      authoring.parse_where("a/b.steps[1].else[3]")
      == ("a/b", [("steps", 1), ("else", 3)]))

patch_text = ('Done, everything is in the basket.\n```json\n'
              '{"script_patch": {"script": "t/fixme", "at": '
              '"t/fixme.steps[1][b].steps[0]", "steps": [{"do": "click", '
              '"target": {"text": "Add", "match": "exact"}}, {"do": "wait", '
              '"seconds": 1}]}}\n```')
pt = authoring.extract_patch(patch_text)
check("the patch block is found in Claude's final message",
      pt and pt["at"].startswith("t/fixme") and len(pt["steps"]) == 2)

tlib = bridge.library(fresh=True)
tlib.save(__import__("script.schema", fromlist=["parse"]).parse({
    "name": "t/fixme", "steps": [
        {"do": "wait", "seconds": 0},
        {"do": "repeat", "for_each": ["a", "b"], "as": "x", "steps": [
            {"do": "click", "target": "Old {x}"}, {"do": "wait", "seconds": 0}]}]}))
ok, msg = authoring.apply_patch(tlib, pt)
fixed = tlib.local()["t/fixme"]
inner = fixed.steps[1].steps
check("the fix replaces the failed step inside the repeat",
      ok and inner[0].target.text == "Add" and inner[1].do == "wait"
      and len(inner) == 3, f"{msg} {[s.do for s in inner]}")
check("the version goes up and the script is back to draft",
      fixed.version == 2 and fixed.trust.value == "draft"
      and fixed.extra.get("patched_by") == "claude")
hist = list((bridge.scripts_root() / ".history").glob("t__fixme.v1.json"))
check("the old version is kept in .history", len(hist) == 1)
ok2, msg2 = authoring.apply_patch(tlib, {"at": "t/fixme.steps[9]",
                                         "steps": [{"do": "wait",
                                                    "seconds": 0}]})
check("a patch for a step that does not exist changes nothing",
      not ok2 and tlib.local()["t/fixme"].version == 2, msg2)
ok3, msg3 = authoring.apply_patch(tlib, {"at": "t/fixme.steps[0]",
                                         "steps": [{"do": "teleport"}]})
check("an invalid step in a patch is refused", not ok3 and "rejected" in msg3,
      msg3)

replies = []
real_claude = authoring._claude


def fake_claude(prompt, model, timeout, env):
    return replies.pop(0)


authoring._claude = fake_claude
replies[:] = ['{"script": {"name": "testpad/write-text", "description": '
              '"Open Notepad and type text", "params": {"text": ""}, '
              '"trust": "trusted", "steps": [{"do": "open", "app": "notepad", '
              '"window": "Notepad"}, {"do": "type", "value": "{text}"}]}}']
name, why = authoring.author("notepad'a merhaba yaz", tlib, {})
got = tlib.local().get(name or "")
check("Claude's script is validated and saved under its site folder",
      name == "testpad/write-text" and got is not None
      and (bridge.scripts_root() / "testpad" / "write-text.json").exists(),
      why)
check("…as a DRAFT, whatever it claimed, and marked unverified",
      got.trust.value == "draft" and got.extra.get("verified") is False
      and got.extra.get("source_task") == "notepad'a merhaba yaz")
replies[:] = ['{"script": {"name": "testpad/write-text", "steps": '
              '[{"do": "wait", "seconds": 0}]}}']
name2, _ = authoring.author("again", tlib, {})
check("a name already taken gets -2, never overwrites",
      name2 == "testpad/write-text-2", str(name2))
replies[:] = ['{"script": {"name": "Bad Name", "steps": []}}',
              '{"script": {"name": "calc/add", "steps": [{"do": "call", '
              '"script": "does/not-exist"}]}}']
name3, why3 = authoring.author("add numbers", tlib, {})
check("invalid twice = no script, and nothing half-saved",
      name3 is None and "invalid twice" in why3
      and "calc/add" not in tlib.local(), why3)
replies[:] = ['{"script": null, "why": "a question, not a screen task"}']
name4, why4 = authoring.author("what is 2+2", tlib, {})
check("Claude may decline: a question is not a script",
      name4 is None and "not a screen task" in why4, why4)
authoring._claude = real_claude

# the engine uses it: no match -> author -> run -> verified
ea = engine.ScriptEngine({"author_scripts": True}, say, client=FakeClient())
ea.recognise = lambda task: (None, "Jev: no saved script fits")
authoring._claude = fake_claude
replies[:] = ['{"script": {"name": "paint/open", "description": "Open Paint",'
              ' "steps": [{"do": "open", "app": "mspaint", "window": '
              '"Paint"}]}}']
ok_rep2 = Report(script="paint/open")
ok_rep2.outcomes.append(StepOutcome("x", "open", "opened", True, 1))
ea.execute = lambda n, a, t="": FakeRunner(ok_rep2)
out = ea.handle("open paint")
authoring._claude = real_claude
lib_now = bridge.library()
check("no saved script: Claude writes one, it runs, it is DONE",
      out.kind == "done" and out.script == "paint/open", f"{out.kind} {out.why}")
check("…and once it worked it is marked verified for Jev next time",
      lib_now.local()["paint/open"].extra.get("verified") is True)

# a stuck authored script with no fix is hidden from Jev
bad = Report(script="paint/open")
bad.outcomes.append(StepOutcome("paint/open.steps[0]", "open", "no window",
                                False, 1))
eb = engine.ScriptEngine({}, say, client=FakeClient())
eb.save_back(engine.Outcome("claude", script="paint/open", authored=True,
                            where="paint/open.steps[0]"),
             "I could not do it.", ok=False)
check("a script Claude wrote that stuck and got no fix is hidden from Jev",
      bridge.library().local()["paint/open"].extra.get("listed") is False)
eb.save_back(engine.Outcome("claude", script="t/fixme",
                            where="t/fixme.steps[0]"),
             '```json\n{"script_patch": {"at": "t/fixme.steps[0]", "steps": '
             '[{"do": "wait", "seconds": 2}]}}\n```', ok=True)
check("after a takeover, Claude's fix is folded into the script",
      bridge.library().local()["t/fixme"].version == 3
      and bridge.library().local()["t/fixme"].steps[0].seconds == 2)

# --------------------------------------------------------------------------
print("\nthe Scripts tab")
# --------------------------------------------------------------------------
import app as appmod                                         # noqa: E402

ag = appmod.Agent()
ls = ag.list_scripts()
sites = {g["site"] for g in ls["groups"]}
check("scripts are grouped by site/app",
      {"migros", "media", "testpad", "t"} <= sites, str(sorted(sites)))
card = next(s for g in ls["groups"] for s in g["scripts"]
            if s["name"] == "testpad/write-text")
check("a card says who wrote it, trust, and whether Jev can pick it",
      card["authored"] and card["trust"] == "draft" and card["jev"]
      and card["verified"] is False, str(card))
gs = ag.get_script("migros/weekly-shop")
check("Steps shows one readable line per step",
      gs["ok"] and any("migros/add-product" in l for l in gs["lines"]),
      str(gs.get("lines", [])[:3]))
check("trust can be raised", ag.set_script_trust("testpad/write-text",
                                                 "trusted")["ok"]
      and bridge.library().local()["testpad/write-text"].trust.value
      == "trusted")
check("a script can be hidden from Jev",
      ag.set_script_listed("testpad/write-text", False)["ok"]
      and not any(s["jev"] for g in ag.list_scripts()["groups"]
                  for s in g["scripts"] if s["name"] == "testpad/write-text"))
check("params save as defaults; an unknown one is refused",
      ag.save_script_params("testpad/write-text", {"text": "hi"})["ok"]
      and not ag.save_script_params("testpad/write-text", {"x": 1})["ok"])
check("running a script with a missing required value is refused up front",
      not ag.run_script("migros/add-product", {})["ok"])
d = ag.delete_script("testpad/write-text-2")
check("delete moves it to the trash",
      d["ok"] and ".trash" in d["moved_to"]
      and "testpad/write-text-2" not in bridge.library(fresh=True).local())


# --------------------------------------------------------------------------
print("\nsettings save instantly, and only what makes sense")
# --------------------------------------------------------------------------
import re as _re                                              # noqa: E402

r = ag.update_settings({"no_change_seconds": -5})
check("a value out of range is clamped, not stored as-is",
      ag.settings["no_change_seconds"] == 3)
r = ag.update_settings({"provider": "bogus"})
check("a bad choice is refused and the old value kept",
      ag.settings["provider"] != "bogus")
r = ag.update_settings({"not_a_setting": 1, "decider": "claude"})
check("an unknown key is refused; the valid one in the same patch is saved",
      "not_a_setting" not in ag.settings and ag.settings["decider"] == "claude")
ag.update_settings({"decider": "jev"})
saved = json.loads(appmod.SETTINGS_FILE.read_text(encoding="utf-8"))
check("each change is on disk immediately (no Save button)",
      saved["decider"] == "jev" and saved["no_change_seconds"] == 3)

old = {"automation_level": "auto", "stall_minutes": -1, "provider": "custom",
       "max_output_tokens": "2000"}
appmod.SETTINGS_FILE.write_text(json.dumps(old), encoding="utf-8")
mig = appmod.load_settings()
check("an old settings file is migrated: version set, values cleaned",
      mig["settings_version"] == 2 and mig["stall_minutes"] == 1
      and mig["max_output_tokens"] == 2000 and mig["provider"] == "custom"
      and mig["automation_level"] == "auto")
check("…and new settings get their defaults",
      mig["decider"] == "jev" and mig["scripts_first"] is True)
appmod.SETTINGS_FILE.write_text("{broken", encoding="utf-8")
bad = appmod.load_settings()
check("a corrupt settings file is kept aside, not silently lost",
      appmod.SETTINGS_FILE.with_suffix(".broken.json").exists()
      and bad["decider"] == "jev")

keys.save_jev_key("typesafe", "ts-secret-XYZ")
state_text = json.dumps(ag.get_state(), default=str)
check("the window state never carries a key value",
      "ts-secret-XYZ" not in state_text)
keys.save_jev_key("typesafe", "")

re0 = engine.ScriptEngine({"rescue_tries": 0}, say, client=FakeClient())
check("rescue tries 0 means no rescue (not 'or 2')",
      (lambda t: 2 if t is None else int(t))(re0.settings.get("rescue_tries", 2))
      == 0)

# --------------------------------------------------------------------------
print("\nthe window's code only calls what exists")
# --------------------------------------------------------------------------
html = (Path(appmod.__file__).parent / "ui" / "app.html").read_text(
    encoding="utf-8")
ids = set(_re.findall(r'id="([^"$]+)"', html))
used = set(_re.findall(r'getElementById\("([^"]+)"\)', html))
check("every getElementById names an element that exists",
      used <= ids, str(sorted(used - ids)))
methods = {n for n in dir(appmod.Agent) if not n.startswith("__")}
calls = set(_re.findall(r'api\(\)\.(\w+)\(', html))
check("every api() call is a method of the app", calls <= methods,
      str(sorted(calls - methods)))
data_keys = set(_re.findall(r'data-key="([a-z_]+)"', html))
check("every instant-save control maps to a known setting",
      data_keys <= set(appmod.SETTINGS_SCHEMA),
      str(sorted(data_keys - set(appmod.SETTINGS_SCHEMA))))
dup = [i for i in ids if html.count(f'id="{i}"') > 1]
check("no element id is used twice", not dup, str(dup))

# --------------------------------------------------------------------------
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print(f"\n{passed}/{total} passed")
for n, ok, d in results:
    if not ok:
        print(f"  FAILED: {n} — {d}")
sys.exit(0 if passed == total else 1)
