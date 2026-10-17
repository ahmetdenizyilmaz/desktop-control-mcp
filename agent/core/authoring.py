"""Claude writes the script once; Jev runs it every time after.

Three ways a script comes out of Claude:

  learn       the person ticked "Repeat task" and no saved script fits. The
              full agent does the task first, then saves what WORKED as a
              script (the save_script tool, `learn_script` here) - built from
              the element names its tools really reported, never guessed.
              Every name is checked against this run's tool results, so a
              made-up target ("Object Select" for Paint's Select) is refused
              and sent back to the agent to fix while it is still there.
  author      the older way (setting author_when = "before"): Claude (Opus)
              is given the step language, the scripts it may `call`, and the
              apps on this machine, and writes a new script BEFORE anything
              has been seen. It runs straight away; if it works it is saved.
              Measured 2026-10-16: of 10 scripts written blind, 5 got stuck
              and needed rescues and the agent anyway, and 1 of the 5 that
              ran through clicked the wrong tool.
  save-back   a script got stuck and the full Claude agent finished the job.
              Its last message carries a `script_patch`: the steps that
              would have done the part that failed. Those replace the failed
              step, the old file goes to `.history/`, and the version goes up.
              The script is better the next time instead of stuck again.

Both produce DATA in the one format the runner validates. Nothing Claude
writes is executed any other way, so the commit guard, the stop signal and
every `expect` apply to it exactly as to a hand-written script.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time

from . import pipeline_bridge as bridge

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*$")

GUIDE = r"""
SCRIPT FORMAT - JSON data, never code. One object:
{
  "name": "<site-or-app>/<what-it-does>",      lowercase, a-z 0-9 and -, e.g. "notepad/write-text", "youtube/play-video"
  "version": 1,
  "description": "<one sentence a person would say to ask for this>",
  "params": {"<name>": "<default, or empty = required>"},
  "trust": "draft",
  "listed": true,
  "tags": ["<words someone might use to ask for it, any language>"],
  "steps": [ ... ]
}
Use {param} inside text fields to substitute a parameter. No other expressions exist.

STEPS ("do" decides which fields matter):
  {"do":"open","app":"chrome|notepad|spotify|...","url":"https://...","window":"<title substring>","timeout":45}
      launches (or reuses) an app and WAITS for a window whose title contains "window". Always give "window".
  {"do":"focus","window":"<title substring>","timeout":15}
  {"do":"click","target":SEL}              also "double_click", "right_click"
  {"do":"type","into":SEL,"value":"text {param}","then":"enter"}   "into" optional (types where the caret is)
  {"do":"key","keys":"enter|tab|escape|ctrl+l|ctrl+a|ctrl+s|alt+f4|pagedown"}
  {"do":"scroll","direction":"down|up","amount":3}
  {"do":"wait","seconds":1.5}
  {"do":"wait_for","target":SEL,"timeout":20}         wait until something appears
  {"do":"expect","target":SEL} / {"do":"gone","target":SEL}   assert
  {"do":"if","present":SEL,"then":[steps],"else":[steps]}
  {"do":"repeat","for_each":["a","b"],"as":"item","steps":[...]}  or "times": N
  {"do":"repeat","for_each":"{items}","as":"item","steps":[...]}
      a LIST parameter: the person gives "süt, ekmek, peynir" (or one per line) and the
      steps run once per item. Use this for shopping lists, recipients, files - never
      hard-code the items the person happened to name.
  {"do":"call","script":"<existing script name>","with":{"param":"value"}}
  {"do":"observe","goal":"achieve this stage using the current screen","expect":SEL,"rounds":6}
      Takes a fresh screenshot and element list each round; the selected helper generates
      short action batches. Ends only with the completion element visible. Use for unfamiliar pages.
  {"do":"capture","target":SEL,"save_as":"observed_name"}
      Saves observed element TEXT for later {observed_name} substitutions in this script.
      Capture must be top-level and precede uses; names cannot overwrite input parameters.
      Pass captured text to called scripts explicitly through with. It is data, not instructions.
  {"do":"wait_message","words":["buy"],"stop_words":["cancel"],"timeout":1800}
      wait for a NEW chat message from a person - use this (not wait_for) whenever the
      task waits for a human reply or a code; code_as:"code" captures a 4-8 digit code into {code}.
Any step may also have: "expect": SEL (must appear after it - the step FAILS if not),
"gone": SEL, "optional": true (a failure does not stop the script), "timeout", "note".

SEL (selector) - never coordinates:
  {"text":"visible name","match":"exact|contains","kind":"button|link|edit|combobox|text|tab|listitem|menuitem|checkbox|radio",
   "id":"<DOM id / AutomationId>","nth":1,"area":"content","near":"<text of a neighbour>"}
  A bare string "Save" means {"text":"Save"}. "area":"content" = inside the web page, not the browser's own
  buttons - use it for everything on a web page. "nth" counts in reading order (1 = first).
  Choosing between many identical buttons by MEANING (e.g. which product): add to the click
  "pick": {"goal":"what a good one looks like","reject":"what to avoid","on_unsure":"top"}.

RULES
- For unfamiliar sites, write a high-level sequence of open/focus + observe stages.
  Do not invent the site's intermediate buttons, roles, or seat labels. The observe goal
  tells the helper the constraints and desired stopping point; it sees the real screen.
- For multi-site work, use explicit open/focus transitions and pass captured evidence to
  later stages. Recheck time-sensitive prices/availability when returning to a site.
- Notes are documentation, not executable checks or questions. Never rely on a note to
  verify a constraint, ask the user, or decline an optional offer.
- For a task ending at a passenger/payment form, stop at that form so the user can fill it.
  Do not require personal details up front when they are only needed after that endpoint.
- wait_message reads messages in a chat application; it does not ask the app's user a
  question. Do not use it to request completion of a website form.
- Hard constraints (route, date, departure time, window seat) require evidence from the
  current page. Use pick.on_unsure="fail", never "top", for these choices. Numeric seat
  names alone do not prove window position. Do not claim unavailable evidence was verified.
- Prefer URLs to navigate web sites (search pages by URL: https://site/search?q={query}).
- Give every action that changes the screen an "expect" for something that ONLY appears if it worked
  (not a permanent header). A step that did nothing must fail, not pass.
- Waiting for a person (a reply, "buy", an SMS code) is wait_message - a static screen while
  waiting is normal and must not be written as a wait_for that will time out.
- Keep it short and linear. Reuse existing scripts with "call" when one does part of the job.
- Clicks on buy/pay/order/send/delete words are asked for approval automatically - write them plainly.
"""


def _claude(prompt: str, model: str, timeout: float, env: dict | None,
            kind: str = "write", system: str = "") -> str:
    """One Claude call: straight to the API when Settings say so and a key
    is there (core/claude_api), otherwise the CLI. Its time and cost go on
    the task's ledger under `kind` (write, decide, details). `system` is
    the part that stays the same between calls - cached on the API."""
    from . import claude_api, codex_backend
    if codex_backend.enabled():
        return codex_backend.call(prompt, system=system, timeout=timeout, kind=kind)
    if claude_api.enabled():
        got = claude_api.call(prompt, model, system=system, timeout=timeout,
                              kind=kind)
        if got is not None:
            return got.text
    if system:
        prompt = system + "\n\n" + prompt
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("Claude Code CLI not installed")
    t0 = time.time()
    p = subprocess.run(
        [exe, "-p", "--model", model, "--output-format", "json"],
        input=prompt, capture_output=True, text=True, encoding="utf-8",
        timeout=timeout, env={**os.environ, **(env or {})},
        creationflags=0x08000000)
    out = json.loads(p.stdout or "{}")
    from . import ledger, tasklog
    ledger.record(kind, time.time() - t0, out.get("total_cost_usd"),
                  f"claude {model}")
    tasklog.model(f"claude {model}", kind, prompt,
                  str(out.get("result", "")), time.time() - t0,
                  out.get("total_cost_usd"))
    if out.get("is_error"):
        raise RuntimeError(str(out.get("result", ""))[:200])
    return str(out.get("result", "") or "")


def _first_object(text: str) -> dict:
    """The outermost JSON object in a reply, fenced or not."""
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    for chunk in fenced + [text]:
        start = chunk.find("{")
        while start != -1:
            depth = 0
            for i in range(start, len(chunk)):
                if chunk[i] == "{":
                    depth += 1
                elif chunk[i] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            got = json.loads(chunk[start:i + 1])
                            if isinstance(got, dict):
                                return got
                        except json.JSONDecodeError:
                            pass
                        break
            start = chunk.find("{", start + 1)
    return {}


def catalogue(lib) -> str:
    rows = []
    for name, s in sorted(lib.local().items()):
        params = ", ".join(f"{k}" + (f"={v!r}" if v else "")
                           for k, v in s.params.items())
        rows.append(f"  {name}({params}) - {s.description[:110]}")
    return "\n".join(rows) or "  (none yet)"


def _apps() -> str:
    try:
        from .machine_profile import load_profile
        return ", ".join(sorted(load_profile().get("capabilities", [])))
    except Exception:
        return "(unknown)"


#: True after `author` pointed at an existing script instead of writing one.
reused = False


def author(task: str, lib, settings: dict, env: dict | None = None,
           say=lambda k, t: None) -> tuple[str | None, str]:
    """Have Claude write a script for `task`, validate it, save it as a
    draft. Returns (name, why). None = Claude declined or could not.
    `reused` tells whether the name is an existing script Claude chose
    instead of writing a duplicate."""
    from script.schema import parse, ScriptError
    global reused
    reused = False

    # The guide goes first and alone, so the API can cache it: it is the
    # bulk of the prompt and the same for every script Claude writes.
    system = ("You write automation scripts for a Windows desktop agent. The "
              "agent runs your script itself; it can drive any app and "
              f"Chrome.\n{GUIDE}")
    base = (
        f"TASK FROM THE PERSON: {task}\n\n"
        f"APPS ON THIS MACHINE (for open.app): {_apps()}\n\n"
        f"EXISTING SCRIPTS YOU MAY CALL:\n{catalogue(lib)}\n\n"
        "Write ONE script that does this task, generalised with params where the "
        "person named a specific thing (a song, a product, a message, a person). "
        "Reply with ONLY: {\"script\": <the script object>}. If an EXISTING "
        "script above already does this task (only its parameters differ), do "
        "not write a copy - reply {\"use\": \"<its name>\"}. If this task is not "
        "something to automate on screen (a question you could answer, research, "
        "a one-off file job), reply {\"script\": null, \"why\": \"...\"}.")
    prompt, last_err = base, ""
    model = settings.get("author_model") or "opus"
    for attempt in range(2):
        try:
            text = _claude(prompt, model, 300, env, system=system)
        except Exception as e:
            return None, f"Claude could not write a script ({e})"
        got = _first_object(text)
        use = str(got.get("use") or "").strip()
        if use and use in lib.local():
            reused = True
            return use, f"Claude: the saved script {use} already does this"
        raw = got.get("script") if "script" in got else got
        if raw is None:
            return None, f"Claude: not a screen task ({got.get('why', '')})"
        try:
            if not isinstance(raw, dict):
                raise ScriptError("the reply held no script object")
            name = str(raw.get("name", "")).strip().lower()
            if not NAME_RE.match(name):
                raise ScriptError(f"name {name!r} must look like site/what-it-does")
            raw["name"] = _free_name(lib, name)
            raw["trust"] = "draft"
            raw.setdefault("listed", True)
            raw["authored_by"] = settings.get("planning_engine", "claude")
            raw["source_task"] = task
            raw["verified"] = False
            script = parse(raw, origin="claude")
            path = lib.save(script)
            try:
                lib.resolve(script.name)        # every call must exist
            except ScriptError:
                path.unlink(missing_ok=True)
                lib.reload()
                raise
            return script.name, f"Claude wrote {script.name} ({path.name})"
        except ScriptError as e:
            last_err = str(e)
            say("system", f"Claude's script was not valid ({last_err}) — "
                          f"asking again.")
            prompt = (base + f"\n\nYOUR PREVIOUS REPLY WAS REJECTED: {last_err}\n"
                      "Fix that and reply again with only the JSON.")
    return None, f"Claude's script was invalid twice ({last_err})"


# ---------------------------------------------------------------------------
# learn: the agent did the task; what worked becomes the script
# ---------------------------------------------------------------------------

LEARN_REQUEST = """
THE PERSON TICKED "REPEAT TASK": this task will be asked for again. Do it now as usual. Then,
once it is DONE and you have checked the result, save what worked as a script with the
save_script tool, so next time the app runs it in seconds without you.
- Build the script from what you actually did and saw work in this run, not from guesses.
  Leave out your mistakes and detours.
- Name every element exactly as your tools reported it (find_controls, do_steps, click,
  read_labels, web_page): its visible name, its id (AutomationId or DOM id) when it has one,
  and its kind. save_script refuses any name no tool reported in this run - if you are not
  sure of one, look it up with find_controls before saving.
- Never coordinates. The runner finds each element fresh by name every time (in a few
  milliseconds), so the script still works after the window moves or the layout shifts.
  Where names repeat, use kind, nth, near or area:"content". A part you could only do by
  pixel position (a canvas, an unnamed icon) becomes an "observe" step with a clear goal
  and an expect.
- Turn the things the person named (numbers, text, names, a search term) into params,
  with this run's values as defaults, so the same script serves the next request like it.
- Start with an "open" step (app or url, and "window"). open.app must be one of the APPS ON
  THIS MACHINE below, a program on PATH by its bare name (charmap, mspaint), or a Windows app
  URI (ms-clock:, ms-settings:display) - never a Start-menu search or Win+R: injected Windows
  keys do not reach Windows here. save_script refuses an app it cannot open.
- Give every action that changes the screen an "expect" that only appears if it worked. For
  a text label (a display, a page header) give its id, or its exact name with
  "match":"exact": the runner finds those directly.
- A web task's script runs in the person's Chrome, read through the same accessibility names
  as find_controls - not in your web_* browser, whose window size and layout differ (a link
  in plain view there sat in a collapsed menu in Chrome). So for a Repeat task on a web page,
  open it in Chrome and work with find_controls / do_steps, and use area:"content".
- Apps remember state from last time (Calculator's mode, a checkbox, a selected page). A step
  that SETS a state goes inside an "if": present = the state is already there -> nothing to
  do, else click and expect it. Never expect something that may already be on screen before
  the step (Clear's "Display is 0" on a fresh Calculator is already true).
- If save_script says something is wrong, fix exactly that and call it again. If this task
  cannot be a script (a question, research, a one-off file job), do not call it; say why.
""" + GUIDE

#: Fields of a step that hold a selector.
_SEL_FIELDS = ("target", "into", "to", "present", "expect", "gone")


def learn_prompt(prompt: str, lib=None) -> str:
    """The task prompt with the learn request after it."""
    try:
        cat = catalogue(lib or bridge.library())
    except Exception:
        cat = "  (unavailable)"
    return (prompt + "\n\n" + LEARN_REQUEST
            + f"\nAPPS ON THIS MACHINE (for open.app): {_apps()}"
            + f"\nEXISTING SCRIPTS YOUR SCRIPT MAY CALL:\n{cat}\n")


def literal_names(script) -> list[str]:
    """Every name a script will look for on screen - selector texts, ids,
    neighbours and window titles - that is fixed text, not a {param}."""
    out: list[str] = []

    def add(s: str) -> None:
        s = (s or "").strip()
        if s and "{" not in s and s not in out:
            out.append(s)

    def walk(steps) -> None:
        for st in steps:
            for f in _SEL_FIELDS:
                sel = getattr(st, f, None)
                if sel is None:
                    continue
                for s in (sel.text, sel.id, sel.near, *sel.any):
                    add(s)
            if st.do in ("open", "focus"):
                add(st.window)
            walk(st.then)
            walk(st.otherwise)
            walk(st.steps)

    walk(script.steps)
    return out


def open_apps(script) -> list[str]:
    """The fixed app names a script's `open` steps launch."""
    out: list[str] = []

    def walk(steps) -> None:
        for st in steps:
            if st.do == "open" and st.app and "{" not in st.app and st.app not in out:
                out.append(st.app)
            walk(st.then)
            walk(st.otherwise)
            walk(st.steps)

    walk(script.steps)
    return out


def _can_open(app: str) -> bool:
    """The runner's own answer, so the check and the replay never differ."""
    try:
        from script.runner import app_target
        return bool(app_target(app))
    except Exception:
        return True                     # cannot tell: let the replay decide


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").casefold()).strip()


def _reported(name: str, seen_n: str, tokens: set[str]) -> bool:
    """Did a tool report `name` as a whole - a token of its own, or a run
    of whole tokens on a line? Never as part of a word: "One" is inside
    "Done" and "Select" inside "Deselect", and neither was on the screen.
    `seen_n` is the run's tool text normalised line by line, `tokens` its
    whitespace-split words (the fast path for a one-word name)."""
    n = _norm(name)
    if not n or n in tokens:
        return True
    return re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", seen_n) is not None


def learn_script(raw, task: str, seen: str, engine: str = "claude",
                 replace: str | None = None, lib=None) -> tuple[str | None, str]:
    """Validate and save a script the agent built from the run it just did.

    (name, message) on success; (None, what to fix) otherwise - the message
    goes straight back to the agent, which is still in the task and can
    correct it. `seen` is what the run's tools reported was on screen
    (tools_sdk._observed); each fixed name in the script must be in it, as
    a whole name. `replace` is the name this task saved before, so a
    corrected second save overwrites the first instead of leaving two - and
    a broken second save leaves the first as it was.
    """
    from script.schema import parse, ScriptError
    lib = lib or bridge.library()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = _first_object(raw) or None
    if isinstance(raw, dict) and isinstance(raw.get("script"), dict):
        raw = raw["script"]
    if not isinstance(raw, dict):
        return None, ("Not saved: script must be one JSON object in the script "
                      "format.")
    raw = dict(raw)
    name = str(raw.get("name", "")).strip().lower()
    if not NAME_RE.match(name):
        return None, (f"Not saved: name {name!r} must look like site/what-it-does "
                      "(lowercase a-z, 0-9 and -).")
    if replace and replace in lib.local():
        name = replace
    else:
        name = _free_name(lib, name)
    raw.update(name=name, trust="draft", listed=True, authored_by=engine,
               source_task=task, verified=False,
               learned_at=time.strftime("%Y-%m-%d %H:%M"))
    raw.setdefault("version", 1)
    try:
        script = parse(raw, origin="learned")
    except ScriptError as e:
        return None, f"Not saved: {e}"
    bad_apps = [a for a in open_apps(script) if not _can_open(a)]
    if bad_apps:
        return None, ("Not saved: the script cannot open "
                      + ", ".join(repr(a) for a in bad_apps)
                      + f". open.app must be one of: {_apps()}; or a program on "
                        "PATH by its bare name (charmap), or a Windows app URI "
                        "(ms-clock:). Fix the open step and call save_script "
                        "again.")
    seen_n = "\n".join(_norm(line) for line in (seen or "").splitlines())
    tokens = set(seen_n.split())
    unseen = [s for s in literal_names(script)
              if not _reported(s, seen_n, tokens)]
    if unseen:
        return None, ("Not saved: no tool reported these names in this run: "
                      + ", ".join(repr(s) for s in unseen[:12])
                      + ". Use names exactly as find_controls, do_steps, click, "
                        "read_labels or web_page showed them (check with "
                        "find_controls if unsure), then call save_script again.")
    # The file this name already has (the replace case) is kept aside: if
    # the new version does not resolve, it goes back as it was. Otherwise a
    # broken second save stayed on disk and was reported as saved.
    path = lib.path_of(name) if name in lib.local() else None
    before = (path.read_text(encoding="utf-8")
              if path is not None and path.exists() else None)
    try:
        if before is not None:
            _write_back(lib, script)
        else:
            lib.save(script)
        lib.resolve(script.name)                # every call must exist
    except ScriptError as e:
        if before is not None:
            path.write_text(before, encoding="utf-8")
        else:
            p = lib.path_of(script.name)
            if p is not None:
                p.unlink(missing_ok=True)
        lib.reload()
        return None, f"Not saved: {e}"
    n = len(script.steps)
    return script.name, (f"Saved {script.name} ({n} step{'s' if n != 1 else ''}, "
                         f"params {dict(script.params) or 'none'}). Next time a "
                         "task like this comes, the app runs it without you. "
                         "Now finish with your final message.")


def resolves(name: str, lib=None) -> bool:
    """Whether `name` is in the library and every script it calls exists -
    what a save that failed must still be true of the save before it."""
    from script.schema import ScriptError
    lib = lib or bridge.library()
    if name not in lib.local():
        return False
    try:
        lib.resolve(name)
    except ScriptError:
        return False
    return True


def _free_name(lib, name: str) -> str:
    have = lib.local()
    if name not in have:
        return name
    n = 2
    while f"{name}-{n}" in have:
        n += 1
    return f"{name}-{n}"


def _write_back(lib, script) -> None:
    """Rewrite the file the script CAME from - `lib.save` derives a path
    from the name, and a script whose file is named differently would be
    duplicated (and a duplicate name is refused on the next scan)."""
    from script.library import to_json
    path = lib.path_of(script.name)
    if path is None:
        lib.save(script)
        return
    path.write_text(json.dumps(to_json(script), indent=2, ensure_ascii=False),
                    encoding="utf-8")
    lib.reload()


def mark(lib, name: str, **fields) -> None:
    """Set top-level extra fields (verified, listed...) and save."""
    s = lib.local().get(name)
    if s is None:
        return
    s.extra.update(fields)
    _write_back(lib, s)


def _root_of(lib, name: str):
    path = lib.path_of(name)
    for r in lib.roots:
        if path is not None and (r in path.parents):
            return r
    return lib.roots[0]


# ---------------------------------------------------------------------------
# save-back: Claude's fix replaces the step that failed
# ---------------------------------------------------------------------------

PATCH_REQUEST = """
WHEN YOU HAVE FINISHED THE TASK, end your final message with the fix for the script, so it
works next time without you, as a fenced block exactly like this:
```json
{"script_patch": {"script": "<script name from the failed step>", "at": "<the failed step's where>",
                  "steps": [ <the steps, in the script format below, that would have done the failed part> ]}}
```
Only steps that would have worked on the screen you saw. If you cannot tell, leave the block out.
""" + GUIDE

_TOKEN = re.compile(r"\.(steps|then|else)\[(\d+)\]|\[([^\]]*)\]")


def failed_where(report) -> str:
    """The deepest real step that failed - not the call/repeat around it,
    and not a rescue attempt's own steps."""
    best = ""
    for o in report.failures():
        if ".rescue" in o.where or ".round" in o.where or ".opus[" in o.where:
            continue
        if o.do in ("call", "repeat", "if"):
            continue
        return o.where
    return best


def parse_where(where: str) -> tuple[str, list[tuple[str, int]]]:
    """'site/x.steps[2][süt].steps[0]' -> ('site/x', [('steps',2),('steps',0)]).
    Iteration labels ([süt]) are dropped: every iteration runs the same steps."""
    i = where.find(".steps[")
    if i < 0:
        raise ValueError(f"not a step path: {where!r}")
    name, rest = where[:i], where[i:]
    path: list[tuple[str, int]] = []
    pos = 0
    for m in _TOKEN.finditer(rest):
        if m.start() != pos:
            break
        if m.group(1):
            path.append((m.group(1), int(m.group(2))))
        pos = m.end()
    if pos != len(rest):
        raise ValueError(f"cannot follow {rest[pos:]!r} in {where!r}")
    return name, path


def extract_patch(text: str) -> dict | None:
    for chunk in re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text or "", re.S):
        try:
            got = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        p = got.get("script_patch") if isinstance(got, dict) else None
        if isinstance(p, dict) and p.get("steps"):
            return p
    return None


def apply_patch(lib, patch: dict, fallback_where: str = "") -> tuple[bool, str]:
    """Splice Claude's steps in place of the failed one. Backs up first."""
    from script.library import step_json, to_json
    from script.schema import _step, parse, ScriptError

    where = str(patch.get("at") or fallback_where)
    try:
        name, path = parse_where(where)
    except ValueError as e:
        return False, str(e)
    name = str(patch.get("script") or name)
    script = lib.local().get(name)
    if script is None or not path:
        return False, f"no script {name!r} to patch"
    try:
        new = [_step(s, f"{name}.patch[{i}]")
               for i, s in enumerate(patch.get("steps") or [])]
    except ScriptError as e:
        return False, f"patch rejected: {e}"
    if not new:
        return False, "empty patch"

    raw = to_json(script)
    parent = raw
    for field, idx in path[:-1]:
        key = "else" if field == "else" else field
        try:
            parent = parent[key][idx]
        except (KeyError, IndexError, TypeError):
            return False, f"{where} does not exist in {name}"
    field, idx = path[-1]
    key = "else" if field == "else" else field
    try:
        seq = parent[key]
        _ = seq[idx]
    except (KeyError, IndexError, TypeError):
        return False, f"{where} does not exist in {name}"
    seq[idx:idx + 1] = [step_json(s) for s in new]

    raw["version"] = int(raw.get("version") or 1) + 1
    raw["trust"] = "draft"
    raw["patched_by"] = "claude"
    raw["patched_at"] = time.strftime("%Y-%m-%d %H:%M")
    try:
        fixed = parse(raw, origin="patch")
    except ScriptError as e:
        return False, f"patched script is not valid: {e}"

    src = lib.path_of(name)
    root = _root_of(lib, name)
    if src is not None and src.exists():
        hist = root / ".history" / f"{name.replace('/', '__')}.v{script.version}.json"
        hist.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, hist)
    _write_back(lib, fixed)
    return True, f"{name} v{fixed.version}: step {where} replaced by {len(new)} step(s)"
