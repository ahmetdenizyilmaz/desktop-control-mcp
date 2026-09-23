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
    e.execute = (lambda n, a, t="", **kw: None) if runner_none \
        else (lambda n, a, t="", **kw: FakeRunner(report))
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


def fake_claude(prompt, model, timeout, env, **kw):
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
ea.execute = lambda n, a, t="", **kw: FakeRunner(ok_rep2)
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
print("\nevery model's time and cost is on the bill")
# --------------------------------------------------------------------------
from core import ledger as ledger_mod                         # noqa: E402

L = ledger_mod.Ledger("weekly shop")
L.add("decide", 0.4, 0.000035)
L.add("details", 1.1, 0.00002)
L.add("script", 13.0, 0.0)
L.add("rescue", 14.0, 0.21)
L.add("rescue", 16.0, 0.19)
L.add("agent", 60.0, 0.67)
L.add("waiting", 30.0)
sm = L.summary()
check("the bill adds up every model, not just the agent",
      abs(L.cost() - (0.000035 + 0.00002 + 0.21 + 0.19 + 0.67)) < 1e-9, sm)
check("the time line names each part, with call counts",
      "deciding 0.4s" in sm and "Claude rescue 30.0s ×2" in sm
      and "Claude agent 1m 0s" in sm and "waiting for you 30.0s" in sm, sm)
check("tiny costs are shown as numbers, not as $0.00",
      ledger_mod.money(0.000035) == "$0.000035"
      and ledger_mod.money(0.004) == "$0.0040"
      and ledger_mod.money(0.67) == "$0.67")
L2 = ledger_mod.Ledger("x")
L2.add("agent", 5.0, None)
check("a part with no reported cost says so, never '$0'",
      "cost not reported" in L2.summary(), L2.summary())

ledger_mod.current = ledger_mod.Ledger("t")
from core import claude_api as _capi                        # noqa: E402
_capi.mode = "cli"            # this part is about the CLI path
import subprocess as _sp                                     # noqa: E402
_real_run = _sp.run


class _P:
    stdout = json.dumps({"result": "ok", "total_cost_usd": 0.12})


authoring.subprocess.run = lambda *a, **k: _P()
authoring._claude("hi", "opus", 10, None, kind="write")
authoring.subprocess.run = _real_run
tw = ledger_mod.current.totals().get("write", {})
check("a Claude CLI call records its reported cost under its kind",
      abs(tw.get("cost", 0) - 0.12) < 1e-9 and tw.get("calls") == 1, str(tw))

from core import quick as quick_mod                           # noqa: E402


class _R:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, *a):
        return json.dumps({"choices": [{"message": {"content": "{}"}}],
                           "usage": {"cost": 0.00003},
                           "model": "anthropic/claude-haiku-4.5"}).encode()


_real_key = quick_mod._openrouter_key
_real_open = quick_mod.urllib.request.urlopen
quick_mod._openrouter_key = lambda: "k"
quick_mod.urllib.request.urlopen = lambda *a, **k: _R()
quick_mod.complete("x", {})
quick_mod._openrouter_key = _real_key
quick_mod.urllib.request.urlopen = _real_open
td = ledger_mod.current.totals().get("details", {})
check("an OpenRouter call records the cost OpenRouter reported",
      abs(td.get("cost", 0) - 0.00003) < 1e-12, str(td))

ag._ledger = ledger_mod.current
uv = ag._usage_view()
check("the window's cost includes what the agent did NOT spend itself",
      abs(uv.get("cost", 0) - (0.12 + 0.00003)) < 1e-9, str(uv.get("cost")))
ag._ledger = None
ledger_mod.current = None

# --------------------------------------------------------------------------
print("\na run cut short can be resumed without redoing its items")
# --------------------------------------------------------------------------
from core import checkpoint as ck                             # noqa: E402

c1 = ck.Checkpoint("weekly shop", "migros/weekly-shop", {"x": "1"})
c1.item("süt", True)
c1.item("peynir", False)
c1.item("yumurta", True)
un = {d["id"]: d for d in ck.unfinished()}
check("a run in progress is on record with what it finished",
      c1.id in un and un[c1.id]["done"] == ["süt", "yumurta"]
      and un[c1.id]["failed"] == ["peynir"])
check("one the app did not close cleanly shows as interrupted",
      un[c1.id]["status"] == "interrupted")
c1.finish("stopped")
check("a stopped run is kept, marked stopped",
      ck.load(c1.id)["status"] == "stopped")
c2 = ck.Checkpoint("t", "s", {})
c2.finish("done")
check("a run that finished is forgotten", ck.load(c2.id) is None)

seen_exec = {}
er = engine.ScriptEngine({}, say, client=FakeClient())
er.execute = lambda n, a, t="", **kw: (seen_exec.update(kw),
                                       FakeRunner(ok_rep))[1]
out = er.handle("Resume", forced=("migros/weekly-shop", {"x": "1"},
                                  ["süt", "yumurta"], c1.id))
check("resume runs the same script, skipping the items already done",
      out.kind == "done" and seen_exec.get("skip") == ["süt", "yumurta"]
      and seen_exec.get("run_id") == c1.id, str(seen_exec))

started = {}
real_run_task = ag.run_task
ag.run_task = lambda text: (started.update(text=text), {"ok": True})[1]
r = ag.resume_run(c1.id)
check("Resume in the window routes to that script with the done items",
      r["ok"] and ag._forced[0] == "migros/weekly-shop"
      and ag._forced[2] == ["süt", "yumurta"] and ag._forced[3] == c1.id
      and started["text"].startswith("Resume:"), str(ag._forced))
ag._forced = None
ag.run_task = real_run_task
check("the window lists it until dismissed",
      any(u["id"] == c1.id for u in ag._unfinished()))
check("Dismiss forgets it", ag.dismiss_run(c1.id)["ok"]
      and not any(u["id"] == c1.id for u in ag._unfinished()))


# --------------------------------------------------------------------------
print("\nJev hesitates: a quick second opinion, not a new script")
# --------------------------------------------------------------------------
class _Ans:
    def __init__(self, ranked):
        self.ranked = ranked


eh = engine.ScriptEngine({}, say, client=FakeClient())
fast.rerank = lambda task, cands, at=0.6, client=None: (
    None, fast.Verdict(False, "confidence 0.22 < 0.60",
                       _Ans([("1", 0.22), ("0", 0.2), ("none", 0.1)])))
asked_opts = {}
eh._claude_choose = lambda goal, reject, opts, model="": (
    asked_opts.update(opts), "0")[1]
nm, why = eh.recognise("not defterine 'x' yaz")
rows_now = engine.listed_rows(bridge.library())
check("a hesitant Jev pick is confirmed by the quick check, not dropped",
      nm is not None and "quick check" in why and "none" in asked_opts, why)
fast.rerank = lambda task, cands, at=0.6, client=None: (
    None, fast.Verdict(False, "no saved job matches",
                       _Ans([("none", 0.9), ("0", 0.1)])))
asked_opts.clear()
nm2, _ = eh.recognise("what is the weather")
check("Jev saying 'none of these' is trusted - no second opinion",
      nm2 is None and not asked_opts)
fast.rerank = real_rerank

authoring._claude = fake_claude
replies[:] = ['{"use": "migros/weekly-shop"}']
before_n = len(bridge.library(fresh=True).local())
nm3, why3 = authoring.author("haftalık alışveriş", bridge.library(), {})
check("Claude may point at an existing script instead of writing a copy",
      nm3 == "migros/weekly-shop" and authoring.reused
      and len(bridge.library(fresh=True).local()) == before_n, why3)
authoring._claude = real_claude


# --------------------------------------------------------------------------
print("\nlog everything - when asked")
# --------------------------------------------------------------------------
from core import tasklog                                     # noqa: E402

check("logging off: nothing is started, nothing written",
      tasklog.start("x", enabled=False) is None and tasklog.current is None)
lg = tasklog.start("haftalık migros alışverişimi yap", enabled=True)
check("logging on: one folder per task, named for it",
      lg is not None and lg.dir.exists()
      and "haftalik-migros-alisverisimi-yap" in lg.dir.name, str(lg.dir))
ag._say("system", "Jev chose migros/weekly-shop")
tasklog.model("jev", "decide: which script", "prompt text", "reply text",
              0.4, 0.00002)
from core.secrets import vault as _v                           # noqa: E402
_v.put("log-test-secret", "hunter2-SECRET", note="t")
tasklog.model("claude opus", "rescue", "type hunter2-SECRET please", "ok",
              12.0, 0.2, shot=str(bridge.scripts_root().parent / "nope.png"))
import base64 as _b64                                         # noqa: E402
png = _b64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


class _TRB:
    tool_use_id = "tu1"
    is_error = False
    content = [{"type": "text", "text": "Screenshot saved"},
               {"type": "image", "source": {"type": "base64",
                                            "media_type": "image/png",
                                            "data": png}}]


ag._log_tool_result(_TRB())
from core import channel as _ch                               # noqa: E402
_real_ask = _ch.channel._ask
_ch.channel._ask = lambda q, ad, t, c: _ch.Reply(_ch.Answer.APPROVE,
                                                 via="app")
_ch.channel.ask("Allow the script to click 'Sepeti Onayla'?")
_ch.channel._ask = _real_ask
tasklog.stop("finished", "Time: script 3s\nCost: $0.0004")
events = [json.loads(l) for l in (lg.dir / "events.jsonl").read_text(
    encoding="utf-8").splitlines()]
kinds = [e["kind"] for e in events]
check("feed lines, model calls, tool results, questions and answers "
      "are all in the log",
      {"start", "feed", "model", "agent", "question", "answer", "end"}
      <= set(kinds), str(kinds))
check("a model call keeps its prompt, reply, time and cost",
      any(e["kind"] == "model" and e["who"] == "jev" and
          e["prompt"] == "prompt text" and e["cost"] == 0.00002
          for e in events))
shots = list((lg.dir / "shots").glob("*.png"))
check("a screenshot the agent was shown is saved as a file and referenced",
      shots and any(e.get("role") == "tool_result" and
                    any("image" in p for p in e.get("parts", []))
                    for e in events), str(shots))
check("a vault secret never reaches the log file",
      "hunter2-SECRET" not in (lg.dir / "events.jsonl").read_text(
          encoding="utf-8"))
check("the question and its answer are both recorded",
      any(e["kind"] == "question" and "Sepeti Onayla" in e["text"]
          for e in events)
      and any(e["kind"] == "answer" and e["answer"] == "approve"
              for e in events))
check("a summary is written at the end",
      "Cost: $0.0004" in (lg.dir / "summary.txt").read_text(encoding="utf-8"))
_v.delete("log-test-secret")

# --------------------------------------------------------------------------
print("\nClaude straight over the API, the CLI only as the fallback")
# --------------------------------------------------------------------------
from core import claude_api                                  # noqa: E402
import types as _types                                       # noqa: E402

check("'opus' is Opus 5.5 on the Anthropic API",
      claude_api.model_id("opus", "anthropic") == "claude-opus-5-5")
check("... and the same model under OpenRouter's name",
      claude_api.model_id("opus", "openrouter") == "anthropic/claude-opus-5.5"
      and claude_api.model_id("claude-haiku-4-5", "openrouter")
      == "anthropic/claude-haiku-4.5"
      and claude_api.model_id("claude-sonnet-5", "openrouter")
      == "anthropic/claude-sonnet-5")
check("an OpenRouter id given with an Anthropic key is translated back",
      claude_api.model_id("anthropic/claude-opus-5.5", "anthropic")
      == "claude-opus-5-5")
_u = _types.SimpleNamespace(input_tokens=1000, output_tokens=500,
                            cache_read_input_tokens=10000,
                            cache_creation_input_tokens=0)
check("cost from tokens when the reply does not say: cache reads at 0.1x",
      abs(claude_api.price("claude-opus-5-5", _u)
          - (1000 * 4 + 10000 * 0.4 + 500 * 20) / 1e6) < 1e-12,
      str(claude_api.price("claude-opus-5-5", _u)))

claude_api.configure({"claude_transport": "cli"})
check("'Claude login' in Settings turns direct calls off",
      not claude_api.enabled())
claude_api.configure({})
_real_route = claude_api.route
claude_api.route = lambda: None
check("no key anywhere: direct calls are off even when chosen",
      not claude_api.enabled())

sent: list[dict] = []


class _Msgs:
    fail = False

    def create(self, **req):
        sent.append(req)
        if _Msgs.fail:
            raise RuntimeError("503 overloaded")
        return _types.SimpleNamespace(
            content=[_types.SimpleNamespace(type="text", text='{"choice": "1"}')],
            usage=_types.SimpleNamespace(input_tokens=100, output_tokens=10,
                                         cache_read_input_tokens=0,
                                         cache_creation_input_tokens=0,
                                         cost=0.0007))


class _FakeAnthropic:
    def __init__(self, **kw):
        _FakeAnthropic.kw = kw
        self.messages = _Msgs()


_fake_mod = _types.SimpleNamespace(Anthropic=_FakeAnthropic,
                                   BadRequestError=type("BRE", (Exception,), {}))
_real_mod = sys.modules.get("anthropic")
sys.modules["anthropic"] = _fake_mod
claude_api.route = lambda: ("openrouter", {"base_url": claude_api.OPENROUTER_BASE,
                                           "auth_token": "k", "api_key": None})
ledger_mod.current = ledger_mod.Ledger("t")
authoring.subprocess.run = boom            # the CLI must not be touched
try:
    txt = authoring._claude("which one?", "opus", 30, None, kind="decide",
                            system="THE GUIDE")
    ok_direct = txt == '{"choice": "1"}'
except Exception as e:
    ok_direct = False
    txt = repr(e)
authoring.subprocess.run = _real_run
check("with a key, a Claude call goes straight to the API - no CLI",
      ok_direct, txt)
req = sent[-1] if sent else {}
check("the direct call asks the model the user chose, by OpenRouter's name",
      req.get("model") == "anthropic/claude-opus-5.5", str(req.get("model")))
check("the fixed guide is sent once as a cached system block",
      req.get("system", [{}])[0].get("text") == "THE GUIDE"
      and req["system"][0].get("cache_control") == {"type": "ephemeral"}
      and "THE GUIDE" not in req["messages"][0]["content"][-1]["text"])
check("a decision is asked at low effort (a lookup, not a puzzle)",
      req.get("output_config") == {"effort": "low"})
check("its reported cost lands on the task's bill",
      abs(ledger_mod.current.totals().get("decide", {}).get("cost", 0)
          - 0.0007) < 1e-12, str(ledger_mod.current.totals()))

_shot = Path(_TMP) / "stuck-test.png"
_shot.write_bytes(b"\x89PNG\r\n\x1a\nfake")
got = claude_api.call("rescue me", "opus", images=[_shot], kind="rescue",
                      record=False)
blocks = sent[-1]["messages"][0]["content"]
check("a rescue sends the screenshot inline as an image block",
      blocks[0].get("type") == "image"
      and blocks[0]["source"]["media_type"] == "image/png"
      and blocks[-1]["text"] == "rescue me"
      and sent[-1]["output_config"] == {"effort": "medium"}, str(blocks[0])[:120])

_Msgs.fail = True
authoring.subprocess.run = lambda *a, **k: _P()
fell = authoring._claude("hi", "opus", 10, None, kind="write")
authoring.subprocess.run = _real_run
check("a failed direct call falls back to the CLI, and the task goes on",
      fell == "ok" and "overloaded" in claude_api.last_error,
      claude_api.last_error)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pipeline"))
from script.helper import OpusHelper                         # noqa: E402
h = OpusHelper(stuck_dir=Path(_TMP) / "stuck", log=lambda m: None,
               claude_path="")
h.claude = None                          # no CLI on this "machine"
h._screenshot = lambda: _shot
billed = []
h.on_cost = lambda s, c: billed.append(c)
asked = []
h.caller = lambda prompt, shot: (asked.append((prompt, shot))
                                 or ('{"done": true, "why": "ok", "steps": []}',
                                     0.02))
plan = h.rescue("add milk", "button gone", ["button, Add, (1,1), "])
check("the rescue helper uses the direct caller, needing no CLI",
      plan.get("done") is True and asked and asked[0][1] == _shot
      and billed == [0.02], str(plan))
check("... and does not tell the model to go read a file",
      "read the file" not in asked[0][0] and "attached" in asked[0][0])
h.caller = lambda prompt, shot: None
plan = h.rescue("add milk", "button gone", [])
check("a direct call that fails with no CLI to fall back to: no steps, no crash",
      plan.get("steps") == [] and not plan.get("done"), str(plan))

_Msgs.fail = False
if _real_mod is not None:
    sys.modules["anthropic"] = _real_mod
else:
    sys.modules.pop("anthropic", None)
claude_api.route = _real_route

# --------------------------------------------------------------------------
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print(f"\n{passed}/{total} passed")
for n, ok, d in results:
    if not ok:
        print(f"  FAILED: {n} — {d}")
sys.exit(0 if passed == total else 1)
