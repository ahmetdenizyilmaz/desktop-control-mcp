"""Claude writes the script once; Jev runs it every time after.

Two ways a script comes out of Claude:

  author      a task no saved script fits. Claude (Opus) is given the step
              language, the scripts it may `call`, and the apps on this
              machine, and writes a new script. It runs straight away; if it
              works it is saved, verified, under its site's folder - and the
              next time anyone asks for that task, Jev finds it and no Claude
              turn is spent at all.
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
  {"do":"call","script":"<existing script name>","with":{"param":"value"}}
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
- Prefer URLs to navigate web sites (search pages by URL: https://site/search?q={query}).
- Give every action that changes the screen an "expect" for something that ONLY appears if it worked
  (not a permanent header). A step that did nothing must fail, not pass.
- Waiting for a person (a reply, "buy", an SMS code) is wait_message - a static screen while
  waiting is normal and must not be written as a wait_for that will time out.
- Keep it short and linear. Reuse existing scripts with "call" when one does part of the job.
- Clicks on buy/pay/order/send/delete words are asked for approval automatically - write them plainly.
"""


def _claude(prompt: str, model: str, timeout: float, env: dict | None) -> str:
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("Claude Code CLI not installed")
    p = subprocess.run(
        [exe, "-p", "--model", model, "--output-format", "json"],
        input=prompt, capture_output=True, text=True, encoding="utf-8",
        timeout=timeout, env={**os.environ, **(env or {})},
        creationflags=0x08000000)
    out = json.loads(p.stdout or "{}")
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


def author(task: str, lib, settings: dict, env: dict | None = None,
           say=lambda k, t: None) -> tuple[str | None, str]:
    """Have Claude write a script for `task`, validate it, save it as a
    draft. Returns (name, why). None = Claude declined or could not."""
    from script.schema import parse, ScriptError

    base = (
        "You write automation scripts for a Windows desktop agent. The agent runs "
        "your script itself; it can drive any app and Chrome.\n\n"
        f"TASK FROM THE PERSON: {task}\n\n"
        f"APPS ON THIS MACHINE (for open.app): {_apps()}\n\n"
        f"EXISTING SCRIPTS YOU MAY CALL:\n{catalogue(lib)}\n"
        f"{GUIDE}\n"
        "Write ONE script that does this task, generalised with params where the "
        "person named a specific thing (a song, a product, a message, a person). "
        "Reply with ONLY: {\"script\": <the script object>}. If this task is not "
        "something to automate on screen (a question you could answer, research, "
        "a one-off file job), reply {\"script\": null, \"why\": \"...\"}.")
    prompt, last_err = base, ""
    model = settings.get("author_model") or "opus"
    for attempt in range(2):
        try:
            text = _claude(prompt, model, 300, env)
        except Exception as e:
            return None, f"Claude could not write a script ({e})"
        got = _first_object(text)
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
            raw["authored_by"] = "claude"
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
