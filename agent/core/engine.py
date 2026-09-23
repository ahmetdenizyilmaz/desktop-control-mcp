"""Jev first, Claude second, the person third.

A task arrives. Before a single Claude turn is spent, the engine asks the
cheap question: is this a job there is already a script for?

    recognise    Jev ranks the saved scripts against the task      ~0.4 s
    fill         missing parameters, from the task (one short call) ~5 s
    execute      the script runs; Jev picks, the runner checks each step
    rescue       a stuck step goes to Claude with a screenshot, twice
    take over    still stuck: the full Claude agent finishes the task,
                 told exactly what the script already did

Nothing matched, or Jev has no key, and the task goes to Claude as before.
The engine can only ever make a task cheaper - never block it.

Three things about HOW it runs, each learned the hard way:

  * On the tool worker thread. UI Automation is COM with thread affinity,
    and the app's tools already own a thread initialised for it. A script
    run on the task thread would build a second, uninitialised UIA world.
  * Behind the arbiter, before EVERY action. The runner only knows
    `should_stop`; the app also has Pause, ESC+ESC and "the person is using
    the machine". `GuardedActuator` asks all of those first.
  * A person touching the machine PAUSES a script, it does not end it:
    it waits until they have been idle for a few seconds, then takes the
    machine back. Ending it would throw away a half-done shop because
    someone moved the mouse.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import pipeline_bridge as bridge
from . import paths

#: A script is only run when Jev is at least this sure it is the right one.
DEFAULT_MIN_CONFIDENCE = 0.6
#: More candidates than this and the probability spreads too thin to trust.
MAX_CANDIDATES = 8
#: How long a person must leave the machine alone before a paused script
#: takes it back.
IDLE_TO_RESUME_S = 5.0


@dataclass
class Outcome:
    """What the engine did with a task, and what the app should do next."""

    kind: str                 # done | stopped | claude
    why: str = ""
    prompt: str = ""          # for kind=claude: what the full agent is told
    script: str = ""
    report: Any = None


@dataclass
class Status:
    """What the window shows about the engine, polled with get_state()."""

    state: str = "idle"       # idle|routing|acting|asking|rescuing|paused|done
    script: str = ""
    detail: str = ""
    since: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {"state": self.state, "script": self.script,
                "detail": self.detail, "since": self.since}


class _CursorProxy:
    """Hands the runner's cursor calls to the app's drawn cursor, if it is
    up. The runner knows WHERE each action lands - including UIA invokes
    and keystrokes that never move the real pointer - so it drives it."""

    def _c(self):
        try:
            from . import agent_cursor
            return agent_cursor._cursor if agent_cursor._shown else None
        except Exception:
            return None

    def state(self, s):
        c = self._c()
        if c is not None:
            c.state(s)

    def move_to(self, x, y):
        c = self._c()
        if c is not None:
            c.move_to(x, y)

    def burst(self, x, y):
        c = self._c()
        if c is not None:
            c.burst(x, y)


def listed_rows(lib) -> list[dict]:
    """The scripts a task can be matched against.

    A script someone would ask for by name ("do the weekly shop") is
    listed. A building block that other scripts `call` (browser/goto,
    migros/add-this) is not - offered to Jev as a candidate it only dilutes
    the choice. A script's own `"listed": true/false` overrides the guess.
    """
    scripts = lib.local()
    called: set[str] = set()

    def walk(steps):
        for st in steps:
            if st.do == "call" and st.script:
                called.add(st.script)
            walk(st.then)
            walk(st.otherwise)
            walk(st.steps)

    for s in scripts.values():
        walk(s.steps)
    rows = []
    for name, s in sorted(scripts.items()):
        listed = s.extra.get("listed")
        if listed is None:
            listed = name not in called
        if not listed:
            continue
        rows.append({"id": name, "name": name,
                     "description": s.description or "",
                     "tags": list(s.extra.get("tags") or [])
                     + [name.split("/")[0]],
                     "params": dict(s.params), "trust": s.trust.value})
    return rows


class ScriptEngine:
    def __init__(self, settings: dict, say: Callable[[str, str], None],
                 set_status: Callable[[Status], None] | None = None,
                 set_current: Callable[[dict | None], None] | None = None,
                 provider_env: dict | None = None,
                 client=None) -> None:
        self.settings = settings
        self.say = say
        self._set_status = set_status or (lambda s: None)
        self._set_current = set_current or (lambda c: None)
        self.provider_env = provider_env or {}
        self._client = client              # tests inject a fake Jev
        self.status = Status()

    # -- status ---------------------------------------------------------
    def _status(self, state: str, script: str = "", detail: str = "") -> None:
        self.status = Status(state, script or self.status.script, detail)
        self._set_status(self.status)

    def jev(self):
        if self._client is None:
            from . import keys
            self._client = keys.jev_client(self.settings)
        return self._client

    # -- 1. recognise -----------------------------------------------------
    def recognise(self, task: str) -> tuple[str | None, str]:
        """(script name, why) or (None, why)."""
        lib = bridge.library()
        rows = listed_rows(lib)
        if lib.errors:
            self.say("system", f"{len(lib.errors)} script file(s) could not "
                               f"be read and were skipped — see Scripts.")
        if not rows:
            return None, "no saved scripts"
        client = self.jev()
        if client.offline:
            return None, ("Jev has no key (Settings → Engines) — going "
                          "straight to Claude")
        cands = rows
        if len(rows) > MAX_CANDIDATES:
            from .retrieval import Index
            hits = Index(rows).search(task, limit=MAX_CANDIDATES,
                                      min_score=0.2)
            by = {r["name"]: r for r in rows}
            cands = [by[h.name] for h in hits if h.name in by]
            if not cands:
                return None, "no saved script shares a word with the task"
        from . import fast
        at = float(self.settings.get("jev_min_confidence")
                   or DEFAULT_MIN_CONFIDENCE)
        t0 = time.time()
        try:
            idx, verdict = fast.rerank(task, cands, at=at, client=client)
        except Exception as e:
            return None, f"Jev failed ({type(e).__name__}: {e})"
        ms = int((time.time() - t0) * 1000)
        if idx is None:
            return None, f"Jev: no saved script fits ({verdict.why}, {ms} ms)"
        name = cands[idx]["name"]
        return name, f"Jev chose {name} ({verdict.why}, {ms} ms)"

    # -- 2. parameters ----------------------------------------------------
    def fill_params(self, script, task: str) -> dict | None:
        """Values for the script's parameters, taken from the task.

        Jev chooses; it cannot write. Pulling "la serenissima" out of "play
        la serenissima on spotify" is generation, so it is one short Claude
        call - and only when the script has a parameter without a default.
        """
        needed = {k: v for k, v in script.params.items()}
        if not needed:
            return {}
        if all(v != "" for v in needed.values()):
            return {}                       # every one has a default
        exe = shutil.which("claude")
        if not exe:
            return None
        prompt = (
            "Fill the parameters of a saved automation script from a task.\n"
            f"TASK: {task}\n"
            f"SCRIPT: {script.name} — {script.description}\n"
            f"PARAMETERS (name: default, empty = required): "
            f"{json.dumps(needed, ensure_ascii=False)}\n"
            "Reply with ONE JSON object mapping each parameter name to its "
            "value from the task, and nothing else. Use the default when the "
            "task does not say. Use null for a required one the task does not "
            "give. A value is the THING the parameter names (a song, a "
            "product, a message) - never a command word of the task itself "
            "(open, play, start, aç, çal, başlat): 'spotify aç' gives no "
            "query, so query is null.")
        try:
            import os
            p = subprocess.run(
                [exe, "-p", "--model",
                 self.settings.get("param_model") or "haiku",
                 "--output-format", "json"],
                input=prompt, capture_output=True, text=True,
                encoding="utf-8", timeout=90,
                env={**os.environ, **self.provider_env},
                creationflags=0x08000000)
            text = json.loads(p.stdout or "{}").get("result", "")
            from script.helper import _first_json
            got = _first_json(text)
        except Exception:
            return None
        out = {}
        for k, default in needed.items():
            v = got.get(k)
            if v is None or str(v).strip() == "":
                if default == "":
                    return None
                v = default
            out[k] = str(v)
        return out

    # -- 3. execute -------------------------------------------------------
    def _confirm(self, word: str, el) -> bool:
        """A commitment the script wants to make - ask the person."""
        from . import channel as chan
        import tools_sdk
        self._status("asking", detail=f"approve '{el.name}'?")
        reply = chan.channel.ask(
            f"The script wants to click '{el.name}' — that looks like a "
            f"commitment ('{word}'). Allow it?",
            approve_deny=True, timeout_s=900,
            cancelled=tools_sdk.should_stop)
        self._status("acting")
        return reply.ok

    def _on_step(self, o) -> None:
        mark = "✓" if o.ok else ("·" if o.skipped else "✗")
        self.say("job", f"{mark} {o.where}  {o.detail[:140]}")
        self._status("acting", detail=o.where)

    def execute(self, name: str, args: dict, task: str = ""):
        """Run a script on the tool worker thread; return its Report."""
        import tools_sdk
        lib = bridge.library()
        resolved = lib.resolve(name)
        level = self.settings.get("automation_level") or "supervised"

        def job():
            from script.runner import Runner
            from script.helper import OpusHelper
            from script.choose import jev_chooser
            from vision.elements import Watcher
            helper = None
            if self.settings.get("claude_rescue", True):
                helper = OpusHelper(
                    model=self.settings.get("rescue_model") or "opus",
                    budget=int(self.settings.get("rescue_budget") or 12),
                    log=lambda m: self.say("system", m.strip()),
                    stuck_dir=paths.data("workspace", "stuck", ".keep").parent,
                    env=self.provider_env)
            runner = Runner(
                resolved, Watcher(hwnd=None), GuardedActuator(self.say),
                confirm=self._confirm,
                should_stop=tools_sdk.should_stop,
                on_step=self._on_step,
                chooser=jev_chooser(client=self.jev()),
                cursor=_CursorProxy(), helper=helper,
                honour_trust=(level == "auto"),
                rescue_tries=int(self.settings.get("rescue_tries") or 2))
            runner.run(args)
            return runner

        self._set_current({"tool": "script", "detail": name,
                           "since": time.time()})
        self._status("acting", script=name)
        box, done = tools_sdk._worker.call(job)
        stop_seen = 0.0
        while not done.wait(0.25):
            if tools_sdk.should_stop():
                stop_seen = stop_seen or time.time()
                if time.time() - stop_seen > 20:
                    # The runner checks stop before every step; twenty
                    # seconds without returning means it is inside a call
                    # that will not come back (a UIA hang). Leave it.
                    tools_sdk._worker.abandon()
                    self._set_current(None)
                    return None
        self._set_current(None)
        kind, value = box[0]
        if kind == "err":
            raise value
        return value

    # -- 4. the whole route -----------------------------------------------
    def handle(self, task: str) -> Outcome:
        self._status("routing", script="", detail="Jev is checking saved "
                                                  "scripts")
        try:
            name, why = self.recognise(task)
        except Exception as e:
            name, why = None, f"script library unavailable ({e})"
        self.say("system", why)
        if not name:
            return Outcome("claude", why=why)

        lib = bridge.library()
        script = lib.local()[name]
        args = self.fill_params(script, task)
        if args is None:
            why = (f"{name} needs details the task does not give "
                   f"({', '.join(k for k, v in script.params.items() if v == '')})")
            self.say("system", why + " — handing to Claude.")
            return Outcome("claude", why=why)
        if args:
            self.say("system", f"Parameters: {args}")

        t0 = time.time()
        runner = self.execute(name, args, task)
        if runner is None:
            self._status("done", detail="stopped")
            return Outcome("stopped", why="stopped during a hung step",
                           script=name)
        rep = runner.report
        secs = time.time() - t0
        if rep.ok:
            self._status("done", detail=f"{secs:.0f}s")
            self.say("result", f"Done with saved script {name} in "
                               f"{secs:.0f}s.\n{rep.summary()}"
                     + _bought_lines(rep))
            return Outcome("done", script=name, report=rep)
        if rep.stopped:
            self._status("done", detail="stopped")
            self.say("system", f"Script stopped: {rep.stopped_by or 'by you'}")
            return Outcome("stopped", script=name, report=rep)
        self._status("rescuing", detail="Claude takes over")
        self.say("system", f"Script {name} got stuck — Claude takes over "
                           f"from where it stopped.")
        shot = getattr(runner.helper, "last_shot", None) if runner.helper \
            else None
        return Outcome("claude", why="script stuck", script=name, report=rep,
                       prompt=takeover_prompt(task, lib, name, args, rep,
                                              shot))


def _bought_lines(rep) -> str:
    if not getattr(rep, "bought", None):
        return ""
    return "\n" + "\n".join(f"  {q} × {label}" for label, q in
                            rep.bought.items())


def takeover_prompt(task: str, lib, name: str, args: dict, rep,
                    shot=None) -> str:
    """Everything the full agent needs to finish what the script started.

    The part that matters most is "do not redo": a script that added nine
    products and stuck on the tenth must not have Claude add all ten again.
    """
    from script.library import to_json
    try:
        script_json = json.dumps(to_json(lib.local()[name]),
                                 ensure_ascii=False)[:6000]
    except Exception:
        script_json = "(unavailable)"
    done = [k for k, v in rep.items.items() if v]
    missed = [k for k, v in rep.items.items() if not v]
    fails = [f"{o.where}: {o.detail}" for o in rep.failures()][-6:]
    parts = [
        f"TASK: {task}",
        "",
        f"A saved automation script ({name}) was already run for this task "
        f"with parameters {args}, and it got STUCK part-way. Finish the task "
        "from where it stopped, using your tools.",
        "",
        "WHAT THE SCRIPT ALREADY DID — do NOT redo any of it:",
        rep.summary(),
    ]
    if done:
        parts.append(f"Completed items: {', '.join(done)}")
    if missed:
        parts.append(f"NOT done (these still need doing): {', '.join(missed)}")
    if getattr(rep, "bought", None):
        parts.append("Already added: " + "; ".join(
            f"{q} x {label}" for label, q in rep.bought.items()))
    if fails:
        parts.append("Where it failed:\n" + "\n".join(fails))
    if shot:
        parts.append(f"Screenshot at the moment it was stuck: {shot}")
    parts += ["", "THE SCRIPT (JSON), for reference:", script_json, "",
              "Take a fresh look at the screen first — it may have moved on."]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# the actuator, behind the arbiter
# ---------------------------------------------------------------------------

def _guarded_actuator_class():
    bridge.ensure_importable()
    from script.runner import DesktopActuator, Stopped

    class _Guarded(DesktopActuator):
        """DesktopActuator that asks the app's arbiter before every action.

        Stop and ESC+ESC end the run (`Stopped`, which the runner never
        treats as a failure to rescue). Pause waits. A person using the
        machine pauses the script until they have been idle a moment.
        """

        def __init__(self, say=None) -> None:
            super().__init__()
            self._say = say or (lambda k, t: None)

        def _gate(self) -> None:
            import tools_sdk
            msg = tools_sdk._blocked()        # waits out Pause itself
            if msg is None:
                return
            if "using their machine" not in msg:
                raise Stopped(msg)
            # The person took over. Wait them out, then take it back.
            arb = tools_sdk._arbiter
            self._say("system", "You're using the machine — the script is "
                                "paused and continues after "
                                f"{IDLE_TO_RESUME_S:.0f}s without input.")
            while True:
                if tools_sdk.should_stop():
                    raise Stopped("stopped while paused for the user")
                idle = arb._activity.idle_s() if arb is not None else 99.0
                if idle >= IDLE_TO_RESUME_S:
                    break
                time.sleep(0.3)
            if arb is not None:
                arb.rebaseline(settle_s=0.0)
            if self.target_hwnd:
                try:
                    self.a.focus_window(self.target_hwnd)
                except Exception:
                    pass
            self._say("system", "Continuing the script.")

        def click(self, el, button="left"):
            self._gate()
            return super().click(el, button)

        def double_click(self, el):
            self._gate()
            return super().double_click(el)

        def type_text(self, text, el):
            self._gate()
            return super().type_text(text, el)

        def press_key(self, key):
            self._gate()
            return super().press_key(key)

        def drag(self, a, b):
            self._gate()
            return super().drag(a, b)

        def scroll(self, el, direction, amount):
            self._gate()
            return super().scroll(el, direction, amount)

        def move(self, el):
            self._gate()
            return super().move(el)

        def launch(self, app, url):
            self._gate()
            return super().launch(app, url)

        def focus_window(self, hwnd, maximize=True):
            self._gate()
            return super().focus_window(hwnd, maximize)

    return _Guarded


def GuardedActuator(say=None):
    return _guarded_actuator_class()(say)
