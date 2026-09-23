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
    authored: bool = False    # Claude wrote this script for this very task
    where: str = ""           # the step that failed, for the save-back


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
                 set_status: Callable[[dict], None] | None = None,
                 set_current: Callable[[dict | None], None] | None = None,
                 provider_env: dict | None = None,
                 client=None, cancel_watch=None) -> None:
        self.settings = settings
        self.say = say
        self._set_status = set_status or (lambda s: None)
        self._set_current = set_current or (lambda c: None)
        self.provider_env = provider_env or {}
        self._client = client              # tests inject a fake Jev
        self.status = Status()
        self.cancel_watch = cancel_watch
        self.pulse = None
        self.wait = None
        self._last_event = self._last_acted = time.time()
        self._waiting = self._rescuing = False
        self._extended = 0.0
        #: This run's resume record (core/checkpoint.py).
        self.checkpoint = None

    # -- status ---------------------------------------------------------
    def _status(self, state: str, script: str = "", detail: str = "") -> None:
        self.status = Status(state, script or self.status.script, detail)
        self._set_status(self.status.as_dict())

    def _set_status_raw(self, d: dict) -> None:
        self._set_status(d)

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
        if self._decider() == "claude":
            return self._claude_recognise(task, rows)
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
        from . import ledger
        d = getattr(verdict, "decision", None)
        ledger.record("decide", ms / 1000,
                      d.cost_usd if d is not None and d.real else None,
                      "jev: which script")
        from . import tasklog
        ans = getattr(verdict, "answer", None)
        tasklog.model("jev", "decide: which script",
                      json.dumps({"task": task, "scripts": [
                          f"{i}: {c['name']} — {c['description'][:120]}"
                          for i, c in enumerate(cands)]}, ensure_ascii=False),
                      json.dumps({"chose": (cands[idx]["name"]
                                            if idx is not None else None),
                                  "why": verdict.why,
                                  "ranked": list(getattr(ans, "ranked", None)
                                                 or [])[:5]}),
                      ms / 1000, d.cost_usd if d is not None and d.real
                      else None)
        if idx is None:
            second = self._second_opinion(task, cands, verdict)
            if second:
                return second, (f"Jev leaned to {second} but was unsure "
                                f"({verdict.why}); a quick check confirmed it")
            return None, f"Jev: no saved script fits ({verdict.why}, {ms} ms)"
        name = cands[idx]["name"]
        return name, f"Jev chose {name} ({verdict.why}, {ms} ms)"

    def _second_opinion(self, task: str, cands: list[dict],
                        verdict) -> str | None:
        """Jev hesitated but did point at a script - ask a small model.

        Jev's confidence splits across near-duplicates and sags on a Turkish
        request against an English description: "not defterine … yaz" put
        notepad/write-text on top at 0.22, under the 0.6 bar, and the task
        went off to have a whole new script written ($0.12 and two rescues)
        for a job that already had one. A one-second check by a small model
        over Jev's top picks is far cheaper than that. An answer of "none"
        from Jev itself is trusted - only a hesitant yes is re-asked.
        """
        ans = getattr(verdict, "answer", None)
        ranked = list(getattr(ans, "ranked", None) or [])
        if not ranked or ranked[0][0] == "none":
            return None
        top = []
        for key, _p in ranked[:4]:
            if key == "none":
                continue
            try:
                top.append(cands[int(key)])
            except (ValueError, IndexError):
                continue
        if not top:
            return None
        opts = {str(i): f"{r['name']} — {r['description'][:140]}"
                for i, r in enumerate(top)}
        opts["none"] = "none of these does this task"
        key = self._claude_choose(
            f"Which saved automation script does THIS task: {task!r}? Only "
            f"a script that does this very task counts.", "", opts)
        if key in ("", "none"):
            return None
        try:
            return top[int(key)]["name"]
        except (ValueError, IndexError):
            return None

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
        # Asked even when every parameter has a default: a script Claude
        # wrote for "type 'merhaba dünya'" keeps that text as the default,
        # and "type 'test iki'" must not quietly type the old one.
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
            "query, so query is null. A parameter a script repeats over "
            "(items, products, names) takes ALL of them as one comma-separated "
            "string: 'süt, ekmek ve peynir al' -> \"süt, ekmek, peynir\".")
        try:
            from . import quick
            from .authoring import _first_object
            text, _how = quick.complete(
                prompt, self.settings,
                cli_model=self.settings.get("param_model") or "haiku",
                env=self.provider_env)
            got = _first_object(text)
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
        """A commitment the script wants to make - ask the person.

        Waiting for this answer is WAITING, not stuck: the wait policy
        reminds and eventually pauses, and after a Resume the question is
        asked again. Only a real answer (or Stop) ends it.
        """
        from . import channel as chan
        import tools_sdk
        q = (f"The script wants to click '{el.name}' — that looks like a "
             f"commitment ('{word}'). Allow it?")
        while True:
            reply = chan.channel.ask(q, approve_deny=True,
                                     timeout_s=self._limit_s(),
                                     cancelled=tools_sdk.should_stop)
            if tools_sdk.should_stop():
                return False
            if reply.answer in (chan.Answer.APPROVE, chan.Answer.DENY,
                                chan.Answer.TEXT):
                return reply.ok
            if reply.answer is chan.Answer.UNREACHABLE:
                self.say("system", "Nobody could be asked (no app window, "
                                   "no Telegram) — not clicking it.")
                return False
            # Timed out: pause, and ask again once resumed.
            if self.wait is not None and not tools_sdk.is_paused():
                self.wait.paused_by_us = True
                tools_sdk.set_paused(True)
                self.say("system", "No answer — paused. Press Resume and "
                                   "the question comes back.")
            self._hold()

    def _limit_s(self) -> float:
        return float(self.settings.get("wait_limit_minutes") or 60) * 60

    def _on_step(self, o) -> None:
        mark = "✓" if o.ok else ("·" if o.skipped else "✗")
        self.say("job", f"{mark} {o.where}  {o.detail[:140]}")

    # -- the hooks the runner calls (on the tool thread) -------------------
    def _hold(self) -> float:
        """Block while paused; return how long, so waits shift their
        deadlines instead of losing the time."""
        import tools_sdk
        if not tools_sdk.is_paused():
            return 0.0
        t0 = time.time()
        from script.runner import Stopped
        while tools_sdk.is_paused():
            if tools_sdk.should_stop():
                raise Stopped("stopped while paused")
            time.sleep(0.25)
        return time.time() - t0

    def _phase(self, kind: str, info: dict) -> None:
        self._last_event = time.time()
        where = info.get("where", "")
        if kind == "acted":
            if self.pulse is not None:
                self.pulse.touch()
            self._last_acted = time.time()
            self._extended = 0.0
        elif kind == "step_start":
            self._status("acting", detail=where)
        elif kind in ("wait_begin", "confirm_begin"):
            self._wait_t0 = time.time()
            reason = (info.get("reason") if kind == "wait_begin"
                      else f"your OK to click '{info.get('name', '')}'")
            self._waiting = True
            if self.wait is not None:
                self.wait.begin(reason)
            self._status("waiting" if kind == "wait_begin" else "asking",
                         detail=f"waiting for {reason}")
            self._set_current({"tool": "wait_for_reply" if kind == "wait_begin"
                               else "ask_user", "detail": reason,
                               "since": time.time()})
        elif kind in ("wait_end", "confirm_end"):
            self._waiting = False
            from . import ledger
            ledger.record("waiting", time.time() - getattr(self, "_wait_t0",
                                                           time.time()))
            if self.wait is not None:
                self.wait.end()
            self._status("acting", detail=where)
            self._set_current({"tool": "script", "detail": self.status.script,
                               "since": time.time()})
            self._last_acted = time.time()
        elif kind == "rescue_begin":
            self._rescuing = True
            self._status("rescuing", detail=f"stuck at {where} — Claude "
                         f"rescue {info.get('try')}/{info.get('of')}")
        elif kind == "rescue_end":
            self._rescuing = False
        elif kind == "assist_round":
            self._rescuing = True
            self._status("rescuing", detail=f"Claude working it out "
                         f"(round {info.get('round')}/{info.get('of')})")
        elif kind == "item_end":
            if self.checkpoint is not None:
                self.checkpoint.item(str(info.get("item", "")),
                                     bool(info.get("ok")))
        elif kind == "pick_unsure":
            self.say("system", f"Jev could not choose at {where}: "
                               f"{info.get('why', '')}")

    def _patience(self, elapsed: float, timeout: float) -> str:
        """Stuck, or still loading? Asked on every poll of a wait.

        stuck    the driven window has not moved at all for
                 `no_change_seconds` since the last action, and nothing is
                 busy -> give up now and let the rescue start
        loading  something is still moving (a spinner, the busy cursor) ->
                 at the timeout, extend, up to `busy_grace_seconds`
        """
        if self.pulse is None or self._waiting:
            return "keep"
        quiet = float(self.settings.get("no_change_seconds") or 10)
        since_act = time.time() - self._last_acted
        if (elapsed >= quiet and since_act >= quiet
                and self.pulse.still_for() >= quiet and not self.pulse.busy()):
            self.say("system", f"Nothing has moved on screen for "
                               f"{self.pulse.still_for():.0f}s — treating the "
                               f"step as stuck.")
            return "fail"
        if elapsed >= timeout and self.pulse.busy():
            grace = float(self.settings.get("busy_grace_seconds") or 30)
            if elapsed - timeout < grace:
                return "extend"
        return "keep"

    def _hooks(self):
        from script.runner import RunHooks
        return RunHooks(phase=self._phase, hold=self._hold,
                        patience=self._patience,
                        wait_expired=lambda waited: "keep")

    def _chooser(self):
        """Jev, then Claude, then the person - for `pick` steps Jev cannot
        answer (no key, a failed call, an answer with no options)."""
        from script.choose import NoChoice, jev_chooser
        jev = jev_chooser(client=self.jev())
        use_claude = self._decider() == "claude"

        def choose(goal, reject, options):
            if use_claude:
                k = self._claude_choose(goal, reject, options,
                                        model=self._decider_model())
                if k:
                    return k, 1.0
                why = "Claude could not choose"
            else:
                client = self.jev()
                before = client.stats.cost_usd
                t0 = time.time()
                try:
                    got = jev(goal, reject, options)
                    from . import ledger, tasklog
                    ledger.record("decide", time.time() - t0,
                                  client.stats.cost_usd - before,
                                  "jev: which item")
                    tasklog.model(
                        "jev", "decide: which item",
                        json.dumps({"goal": goal, "reject": reject,
                                    "options": options}, ensure_ascii=False),
                        json.dumps({"choice": got[0],
                                    "label": options.get(got[0], ""),
                                    "margin": got[1]}, ensure_ascii=False),
                        time.time() - t0, client.stats.cost_usd - before)
                    return got
                except NoChoice as e:
                    why = str(e)
            key = self._claude_choose(goal, reject, options)
            if key:
                self.say("system", f"Jev could not choose ({why}); Claude "
                                   f"chose: {options[key][:60]}")
                return key, 1.0
            key = self._ask_choose(goal, options)
            if key:
                return key, 1.0
            raise NoChoice(f"{why}; Claude and you could not decide either")

        return choose

    def _decider(self) -> str:
        """Who makes the quick decisions - which saved script, which
        product. "jev" (default: ~0.4 s, a fraction of a cent) or "claude"
        (a Claude model: seconds per decision, far dearer, no Jev key)."""
        return (self.settings.get("decider") or "jev").lower()

    def _decider_model(self) -> str:
        return self.settings.get("decider_model") or "claude-opus-5-5"

    def _claude_recognise(self, task: str, rows: list[dict]
                          ) -> tuple[str | None, str]:
        from .authoring import _claude, _first_object
        opts = "\n".join(f"{i}: {r['name']} — {r['description'][:140]}"
                         for i, r in enumerate(rows))
        t0 = time.time()
        try:
            text = _claude(
                "Which saved automation script does this task ask for? Only a "
                "script that does THIS task counts; a similar-looking one does "
                "not.\nTASK: " + task + "\nSCRIPTS:\n" + opts
                + '\nReply with only {"choice": <number>} or '
                  '{"choice": "none"}.',
                self._decider_model(), 120, self.provider_env,
                kind="decide")
        except Exception as e:
            return None, f"Claude could not decide ({e})"
        ms = int((time.time() - t0) * 1000)
        ch = _first_object(text).get("choice")
        try:
            i = int(ch)
            name = rows[i]["name"]
        except (TypeError, ValueError, IndexError):
            return None, f"Claude: no saved script fits ({ms} ms)"
        return name, f"Claude chose {name} ({self._decider_model()}, {ms} ms)"

    def _claude_choose(self, goal, reject, options, model: str = "") -> str:
        try:
            from .authoring import _claude, _first_object
            ask = ("Pick the ONE option that fits. Goal: " + goal
                   + (f" Reject: {reject}." if reject else "")
                   + "\nOptions:\n" + "\n".join(f"{k}: {v}" for k, v in
                                                options.items())
                   + '\nReply with only {"choice": "<key>"}.')
            if model:                     # the decider: a model you chose
                text = _claude(ask, model, 120, self.provider_env,
                               kind="decide")
            else:                         # a tie-break: the fast path
                from . import quick
                text, _how = quick.complete(ask, self.settings,
                                            env=self.provider_env,
                                            kind="decide")
            k = str(_first_object(text).get("choice", ""))
            return k if k in options else ""
        except Exception:
            return ""

    def _ask_choose(self, goal, options) -> str:
        from . import channel as chan
        import tools_sdk
        keys_ = list(options)
        q = (f"Which one? {goal}\n" + "\n".join(
            f"{i + 1}) {options[k][:70]}" for i, k in enumerate(keys_))
             + "\nReply with the number.")
        r = chan.channel.ask(q, approve_deny=False, timeout_s=self._limit_s(),
                             cancelled=tools_sdk.should_stop)
        try:
            n = int((r.text or "").strip().split()[0].rstrip(").")) - 1
            return keys_[n] if 0 <= n < len(keys_) else ""
        except (ValueError, IndexError):
            return ""

    def execute(self, name: str, args: dict, task: str = "",
                skip: list[str] | None = None, run_id: str = ""):
        """Run a script on the tool worker thread; return the Runner.

        The task thread meanwhile watches: it ticks the wait policy
        (reminder, pause), and notices a HANG - no event from the runner for
        `hang_s` while it is acting, which only a UI Automation call that
        never returns produces. That run is abandoned and handed over.
        """
        import tools_sdk
        from . import channel as chan
        from .progress import ScreenPulse
        from .waiting import WaitPolicy
        lib = bridge.library()
        resolved = lib.resolve(name)
        level = self.settings.get("automation_level") or "supervised"
        script = lib.local().get(name)
        cancel = (script.extra.get("cancel") or {}) if script else {}
        if cancel.get("window") and self.cancel_watch is not None:
            self.cancel_watch.watch_chat(str(cancel["window"]))

        from .checkpoint import Checkpoint
        try:
            self.checkpoint = Checkpoint(task, name, args, skip, run_id)
        except Exception:
            self.checkpoint = None      # resuming is a convenience, never a blocker
        self.pulse = ScreenPulse().start()
        self.wait = WaitPolicy(
            float(self.settings.get("remind_after_minutes") or 5) * 60,
            self._limit_s(), notify=chan.channel.notify, say=self.say,
            set_paused=tools_sdk.set_paused, is_paused=tools_sdk.is_paused,
            task=task)
        self._last_event = self._last_acted = time.time()
        self._waiting = self._rescuing = False
        holder: dict = {}

        def job():
            from script.runner import Runner
            from script.helper import OpusHelper
            from vision.elements import Watcher
            helper = None
            tries = self.settings.get("rescue_tries", 2)
            tries = 2 if tries is None else int(tries)
            if self.settings.get("claude_rescue", True) and tries > 0:
                helper = OpusHelper(
                    model=self.settings.get("rescue_model") or "opus",
                    budget=int(self.settings.get("rescue_budget") or 12),
                    log=lambda m: self.say("system", m.strip()),
                    stuck_dir=paths.data("workspace", "stuck",
                                         ".keep").parent,
                    env=self.provider_env)
            if helper is not None:
                from . import ledger as _ledger
                helper.on_cost = lambda s, c: _ledger.record(
                    "rescue", s, c, "claude: stuck step")
                from . import tasklog as _tasklog
                helper.on_log = lambda prompt, reply, s, c, shot: \
                    _tasklog.model(f"claude {helper.model}", "rescue",
                                   prompt, reply, s, c, shot=shot)
            runner = Runner(
                resolved, Watcher(hwnd=None), GuardedActuator(self.say),
                confirm=self._confirm,
                should_stop=tools_sdk.should_stop,
                on_step=self._on_step,
                chooser=self._chooser(),
                cursor=_CursorProxy(), helper=helper,
                honour_trust=(level == "auto"),
                rescue_tries=max(1, tries),
                skip_items=set(skip or ()),
                hooks=self._hooks())
            holder["runner"] = runner
            runner.run(args)
            return runner

        self._set_current({"tool": "script", "detail": name,
                           "since": time.time()})
        self._status("acting", script=name)
        hang_s = float(self.settings.get("hang_seconds") or 120)
        from . import ledger
        led = ledger.current
        t_exec = time.time()
        other_before = (sum(t["seconds"] for t in led.totals().values())
                        if led else 0.0)
        box, done = tools_sdk._worker.call(job)
        stop_seen = 0.0
        try:
            while not done.wait(0.25):
                self.wait.tick()
                self._publish_wait()
                if tools_sdk.should_stop():
                    stop_seen = stop_seen or time.time()
                    if time.time() - stop_seen > 20:
                        # The runner checks stop before every step; twenty
                        # seconds without returning means a call that will
                        # not come back. Leave it.
                        tools_sdk._worker.abandon()
                        return None
                    continue
                idle = time.time() - self._last_event
                if (idle > hang_s and not self._waiting and not self._rescuing
                        and not tools_sdk.is_paused()):
                    tools_sdk._worker.abandon()
                    self.say("system", f"The script has not moved for "
                                       f"{idle:.0f}s — the window stopped "
                                       f"answering. Handing over to Claude.")
                    r = holder.get("runner")
                    if r is not None:
                        from script.runner import Outcome as StepOutcome
                        r.report.outcomes.append(StepOutcome(
                            self.status.detail or name, "hang",
                            f"no progress for {idle:.0f}s", False, 0))
                        r.hung = True
                    return r
        finally:
            if led is not None:
                # The script's own time: the run, minus the model calls and
                # the waiting for a person that happened inside it (already
                # on the ledger under their own names).
                other = (sum(t["seconds"] for t in led.totals().values())
                         - other_before)
                led.add("script", max(0.0, time.time() - t_exec - other),
                        0.0, name)
            self.pulse.stop()
            if self.wait.paused_by_us and tools_sdk.is_paused():
                tools_sdk.set_paused(False)
            self._set_current(None)
        kind, value = box[0]
        if kind == "err":
            raise value
        return value

    def _publish_wait(self) -> None:
        if self.wait is not None and self.wait.active:
            d = self.status.as_dict()
            d.update(self.wait.view())
            self._set_status_raw(d)

    # -- 4. the whole route -----------------------------------------------
    def handle(self, task: str, forced: tuple | None = None) -> Outcome:
        """Route a task. `forced` = (script, args[, skip, run_id]): run that
        script (the Scripts tab's Run, or a Resume), skipping recognition."""
        lib = bridge.library()
        authored = False
        skip, run_id = [], ""
        if forced is not None:
            name, args = forced[0], forced[1]
            if len(forced) > 2:
                skip, run_id = list(forced[2] or []), str(forced[3] or "")
                if skip:
                    self.say("system", f"Resuming — skipping {len(skip)} "
                                       f"item(s) already done: "
                                       f"{', '.join(skip[:8])}"
                                       + ("…" if len(skip) > 8 else ""))
            if name not in lib.local():
                return Outcome("claude", why=f"no script {name!r}")
        else:
            self._status("routing", script="", detail="Jev is checking "
                                                      "saved scripts")
            try:
                name, why = self.recognise(task)
            except Exception as e:
                name, why = None, f"script library unavailable ({e})"
            self.say("system", why)
            if not name and self.settings.get("author_scripts", True):
                self._status("routing", detail="Claude is writing a script "
                                               "for this task")
                self.say("system", "Asking Claude to write a script for "
                                   "this task (so next time needs no Claude)…")
                from . import authoring
                name, why2 = authoring.author(task, lib, self.settings,
                                              self.provider_env, self.say)
                self.say("system", why2)
                authored = bool(name) and not authoring.reused
            if not name:
                return Outcome("claude", why=why)
            script = lib.local()[name]
            args = self.fill_params(script, task)
            if args is None:
                why = (f"{name} needs details the task does not give "
                       f"({', '.join(k for k, v in script.params.items() if v == '')})")
                self.say("system", why + " — handing to Claude.")
                if authored:
                    self._unlist(lib, name, "its parameters could not be "
                                            "filled")
                return Outcome("claude", why=why)
        if args:
            self.say("system", f"Parameters: {args}")

        t0 = time.time()
        runner = self.execute(name, args, task, skip=skip, run_id=run_id)
        if runner is None:
            self._finish_checkpoint("stopped")
            self._status("done", detail="stopped")
            return Outcome("stopped", why="stopped during a hung step",
                           script=name)
        rep = runner.report
        secs = time.time() - t0
        self._record_stats(name, rep)
        if rep.ok and not getattr(runner, "hung", False):
            self._finish_checkpoint("done")
            self._status("done", detail=f"{secs:.0f}s")
            if authored:
                from . import authoring
                authoring.mark(lib, name, verified=True)
                self.say("system", f"Saved {name} — next time Jev runs it "
                                   f"without Claude. See the Scripts tab.")
            self.say("result", f"Done with script {name} in {secs:.0f}s.\n"
                               f"{rep.summary()}" + _bought_lines(rep))
            return Outcome("done", script=name, report=rep)
        if rep.stopped:
            self._finish_checkpoint("stopped")
            self._status("done", detail="stopped")
            self.say("system", f"Script stopped: {rep.stopped_by or 'by you'}")
            return Outcome("stopped", script=name, report=rep)
        self._finish_checkpoint("handed over to Claude")
        self._status("rescuing", detail="Claude takes over")
        self.say("system", f"Script {name} got stuck — Claude takes over "
                           f"from where it stopped.")
        shot = getattr(runner.helper, "last_shot", None) if runner.helper \
            else None
        from .authoring import failed_where
        return Outcome("claude", why="script stuck", script=name, report=rep,
                       authored=authored, where=failed_where(rep),
                       prompt=takeover_prompt(task, lib, name, args, rep,
                                              shot))

    def _finish_checkpoint(self, status: str) -> None:
        if self.checkpoint is not None:
            try:
                self.checkpoint.finish(status)
            except Exception:
                pass

    def _unlist(self, lib, name: str, why: str) -> None:
        from . import authoring
        authoring.mark(lib, name, listed=False, note=f"hidden: {why}")

    def save_back(self, outcome: Outcome, result_text: str,
                  ok: bool) -> None:
        """After a takeover: fold Claude's fix into the script."""
        if not outcome.script:
            return
        from . import authoring
        lib = bridge.library()
        patch = authoring.extract_patch(result_text) if ok else None
        if patch:
            done, msg = authoring.apply_patch(lib, patch, outcome.where)
            if done:
                self.say("system", f"Saved Claude's fix into {msg}. Review it "
                                   f"in the Scripts tab.")
                if outcome.authored:
                    authoring.mark(lib, outcome.script, verified=True)
                return
            self.say("system", f"Claude's fix could not be saved ({msg}).")
        if outcome.authored:
            self._unlist(lib, outcome.script, "it got stuck on its first run "
                                              "and no fix was saved")
            self.say("system", f"{outcome.script} is kept but hidden from "
                               f"Jev until it is fixed (Scripts tab).")

    def _record_stats(self, name: str, rep) -> None:
        try:
            p = paths.data("script_stats.json")
            data = json.loads(p.read_text(encoding="utf-8")) if p.exists() \
                else {}
            st = data.setdefault(name, {"runs": 0, "ok": 0})
            st["runs"] += 1
            st["ok"] += 1 if rep.ok else 0
            from . import ledger
            led = ledger.current
            if led is not None:
                v = led.view()
                st["seconds_total"] = (st.get("seconds_total", 0.0)
                                       + v["elapsed"])
                st["cost_total"] = st.get("cost_total", 0.0) + v["cost"]
                st["last_seconds"] = round(v["elapsed"], 1)
                st["last_cost"] = round(v["cost"], 6)
            st["last"] = time.time()
            st["last_ok"] = bool(rep.ok)
            p.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except Exception:
            pass


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
    from .authoring import PATCH_REQUEST, failed_where
    where = failed_where(rep)
    parts += ["", "THE SCRIPT (JSON), for reference:", script_json, "",
              "Take a fresh look at the screen first — it may have moved on."]
    if where:
        parts += ["", f"The failed step's where is: {where}", PATCH_REQUEST]
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
