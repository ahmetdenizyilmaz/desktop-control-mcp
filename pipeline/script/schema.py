"""The script format: what a sequence of actions is allowed to say.

A script is DATA, never code. That is the one decision everything else
follows from, and it comes from a requirement — scripts are loaded from a
server or a git repo. A JSON step list can be validated before anything
touches the mouse; a Python file fetched from a URL is a shell.

The format sits at roughly the level of GitHub Actions or Ansible:
parameters, a step list, conditionals, repetition, and calls to other
scripts. Deliberately short of a programming language, and the line is
EXPRESSIONS. `{product}` substituted into a string is fine. `{count + 1}`
is not, and never will be — the moment arithmetic is needed, JSON is the
wrong host and the work belongs in a model turn.

    allowed                              refused
    {param} into text/value fields       any expression, arithmetic, indexing
    for_each over a literal list         for_each over a query result
    call with explicit `with:` args      implicit scope, return values

**A function is just a script.** There is no separate concept: `call` runs
another script by name with explicit arguments, so the library of scripts
IS the library of functions. That is also what makes the remote-fetch story
coherent — you publish scripts, and scripts compose.

Three guards exist because this format can drive a mouse:

  depth and cycles   `a` calls `b` calls `a` is an infinite loop that
                     clicks. Refused at VALIDATION time, not discovered at
                     run time.
  no implicit scope  a called script sees only its `with:`. The alternative
                     is a scope chain, and then shadowing rules, and then
                     you have a language.
  trust is the MIN   of the whole call chain. Otherwise trust laundering:
                     import an unreviewed script, have a trusted one call
                     it, and it runs unattended.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

#: How deep `call` may nest. Eight is far past any real script and well
#: short of a stack that takes minutes to unwind.
MAX_CALL_DEPTH = 8

#: Steps that act on the world. Everything here needs a resolved target and
#: is subject to the commit guard before it runs.
ACT_KINDS = ("click", "double_click", "right_click", "type", "key", "drag",
             "scroll", "move")

#: Steps that put something in front of the runner to act ON. Separate from
#: ACT_KINDS because they do not resolve a target in the scene — they
#: change WHICH scene there is, which is why the runner re-points the
#: Watcher after each one. Without these a script could only ever drive
#: whatever happened to be on screen when it started.
REACH_KINDS = ("open", "focus")

#: Steps that decide or wait. These change control flow, never the screen.
FLOW_KINDS = ("wait", "wait_for", "if", "repeat", "call", "wait_message",
              "assist", "observe", "capture")

#: Steps that assert. A script without these fails silently three steps
#: later — `replay.py`'s most expensive lesson, and one the Migros run
#: repeated when a click "succeeded" and added nothing to the basket.
CHECK_KINDS = ("expect", "gone")

ALL_KINDS = ACT_KINDS + REACH_KINDS + FLOW_KINDS + CHECK_KINDS

#: A URL scheme. Deliberately excludes `.` from the scheme characters,
#: although RFC 3986 allows it: `javascript:alert(1)` and `file:///…` carry
#: no `//`, so testing for `://` would read them as bare hostnames and
#: helpfully prefix `https://`. Excluding the dot keeps `migros.com.tr:8080`
#: reading as a host-and-port rather than as a scheme named `migros.com.tr`.
_SCHEME = re.compile(r"^([a-z][a-z0-9+-]*):")

#: `{name}` only. No dots, no brackets, no spaces — the regex IS the
#: guarantee that substitution can never become evaluation.
PARAM = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

#: Fields a parameter may be substituted into. Restricted on purpose: a
#: param in `do` could turn a `wait` into a `click` at run time, which is
#: exactly the class of surprise this format exists to prevent.
SUBSTITUTABLE = ("value", "text", "keys", "script", "any", "url", "app",
                 "window", "id", "goal")


class Trust(str, Enum):
    """How far a script is allowed to run on its own.

    Identical in spirit to the agent's `schema.Trust`, and for the same
    reason: a script that has never run is not the same risk as one that
    has worked cleanly a dozen times, and the difference should be recorded
    rather than assumed.
    """

    DRAFT = "draft"              # never run clean; supervise every step
    SUPERVISED = "supervised"    # runs, but a human is watching
    TRUSTED = "trusted"          # may run unattended
    QUARANTINED = "quarantined"  # refused — failed in a way that mattered


#: Anything arriving from outside lands here, whatever it claims about
#: itself. Trust is local knowledge and cannot travel inside a file.
IMPORT_TRUST = Trust.DRAFT


class ScriptError(ValueError):
    """The script is malformed. Raised at validation, never mid-run."""


@dataclass(frozen=True)
class Selector:
    """How to find one thing on screen, without naming a coordinate.

    Signals combine rather than compete, because any one of them alone is
    ambiguous on a real screen. `text="ara"` as a substring also matches
    "kararar", "Arama alanı" and "Hazırara"; adding `kind="button"` removes
    nearly all of that for free, because the merged scene knows what is a
    button and OCR alone never could.

    `near` is preferred over `nth` for the reason `locator.py` recorded:
    an ordinal breaks the moment the page reflows, a neighbour does not.
    """

    text: str = ""
    #: button | link | edit | combobox | text | icon | … see vision.KINDS
    kind: str = ""
    #: exact | contains — contains is the default because UI text is rarely
    #: quoted exactly in a script, but exact is what you want for a word
    #: that is a substring of another ("Ara" inside "Arama").
    match: str = "contains"
    #: Alternatives for the same control. A trilingual button is one
    #: element, not three scripts.
    any: tuple[str, ...] = ()
    #: Disambiguate by a neighbour's text rather than by position.
    near: str = ""
    #: 1-based. Last resort.
    nth: int = 0
    #: A template name, for controls with no text at all.
    icon: str = ""
    #: The control's automation id — on a web page this is the DOM `id`
    #: attribute, which UIA passes straight through (`header-hemen-tab`).
    #: The most stable handle there is: it survives a redesign, a language
    #: change and the rotating advert text that made this site's search box
    #: unfindable by name. Prefer it whenever the page provides one.
    id: str = ""
    #: "content" restricts matching to the app's document rectangle — the
    #: page in a browser, the sheet in an editor — excluding the app's own
    #: frame. Not cosmetic: a live Migros page offered 270 elements, 44 of
    #: them Chrome's own, including two buttons named "Kapat" that close
    #: the tab and the browser. A page-aimed selector that matched one of
    #: those would end the run by doing exactly what it was told.
    area: str = ""
    #: "last" = of everything that matches, the one closest to where the
    #: previous action landed. For a control that REPLACES the one just
    #: clicked: Migros swaps a card's add button for a quantity stepper in
    #: the same spot, and the stepper's "+" has the same name on every card
    #: already in the basket - so position relative to the last click is
    #: the only thing that says which card it belongs to.
    nearest: str = ""

    def describe(self) -> str:
        bits = []
        if self.text:
            bits.append(f"{self.match} {self.text!r}")
        if self.any:
            bits.append(f"any of {list(self.any)}")
        if self.id:
            bits.append(f"id {self.id!r}")
        if self.icon:
            bits.append(f"icon {self.icon!r}")
        if self.kind:
            bits.append(f"kind={self.kind}")
        if self.near:
            bits.append(f"near {self.near!r}")
        if self.nth:
            bits.append(f"#{self.nth}")
        if self.area:
            bits.append(f"in the {self.area}")
        if self.nearest:
            bits.append(f"nearest the {self.nearest} action")
        return ", ".join(bits) or "<empty selector>"


@dataclass(frozen=True)
class Pick:
    """Choose among equal candidates by what they are FOR, not where they are.

    A results page offers twenty identical "add to basket" buttons. `nth`
    picks by position and `near` by a neighbour, and on a shopping page
    both are guesses — measured on three real searches, the first result
    was a latte, a labne and a sourdough baguette, and a plain `nth: 1`
    bought all three.

    So when the buttons are indistinguishable, ask about the thing each
    one BELONGS to. `label` says how to describe a candidate (its nearest
    card title), `goal` and `reject` say what a good one looks like, and a
    decision model scores them. Measured on jev-1.13: ~400 ms warm,
    $0.000035, and the wrong answer scored 0.000 in all three cases.

    `min_margin` rather than a confidence floor, deliberately. A correct
    "peynir" answer came back at confidence 0.61 - below the usual 0.80
    gate - with a margin of 0.44. When options are near-duplicates the
    probability mass splits across the plausible ones and confidence
    drops; the GAP to the runner-up is what actually says "this one".
    """

    goal: str
    reject: str = ""
    #: How to describe a candidate. Only "nearest_title" for now: the
    #: text of the nearest element above/around it, which on a product
    #: grid is the product name.
    label: str = "nearest_title"
    min_margin: float = 0.25
    #: What to do when the margin is too small. "fail" stops the step;
    #: "top" takes the model's own first choice anyway.
    #:
    #: The distinction that makes "top" safe is that it is NOT the page's
    #: first result - it is the best of the SCORED ones. A live "ekmek"
    #: search offered 24 near-identical breads and the margin came out at
    #: 0.05, which does not mean "I cannot tell bread from a latte"; it
    #: means several breads are equally fine. Refusing there is as wrong
    #: as guessing. Where the top pick genuinely could be the wrong KIND
    #: of thing, leave it at "fail".
    on_unsure: str = "fail"
    #: How many candidates to score. More is not better: probability
    #: spreads across near-duplicates, so 24 options can bury a clear
    #: answer under its own siblings.
    max_options: int = 10
    #: How far to look for a candidate's label, in pixels.
    radius: int = 280


@dataclass
class Step:
    """One instruction. `do` decides which other fields are meaningful."""

    do: str
    target: Selector | None = None
    #: `type` needs somewhere to put the text; kept separate from `target`
    #: so a step reads as "type X into Y" rather than "click Y with X".
    into: Selector | None = None
    value: str = ""
    keys: str = ""
    #: drag
    to: Selector | None = None
    #: open / focus. `window` is a title substring, and waiting for it is
    #: what makes `open` usable: a launch returns the instant the process
    #: starts, long before anything is on screen, so a script that did not
    #: wait would reliably act on the page it was trying to leave.
    app: str = ""
    url: str = ""
    window: str = ""
    maximize: bool = True
    #: Focus a matching window if one already exists, instead of starting
    #: another. Default TRUE, because launching a duplicate is almost never
    #: what a script means - eleven stray Migros tabs across one afternoon
    #: of testing say so - and because the browser you already have is the
    #: one carrying your session and your cookies.
    reuse: bool = True
    #: wait / wait_for
    seconds: float = 0.0
    timeout: float = 10.0
    #: scroll. Its own fields rather than reusing `value` and `times`, which
    #: are parsed only for `type` and `repeat` — sharing them left a
    #: scroll's direction unreachable, so every scroll silently went down.
    direction: str = "down"
    amount: int = 3
    #: if
    present: Selector | None = None
    then: list["Step"] = field(default_factory=list)
    otherwise: list["Step"] = field(default_factory=list)
    #: repeat
    times: int = 0
    for_each: tuple[str, ...] = ()
    #: `for_each: "{products}"` - the list comes from a parameter, split on
    #: new lines, ";" or ",". The one place a parameter holds a LIST: "buy
    #: süt, ekmek, peynir" becomes one script run over three items instead
    #: of a new script per shopping list.
    for_each_param: str = ""
    as_name: str = ""
    steps: list["Step"] = field(default_factory=list)
    #: call
    script: str = ""
    with_args: dict[str, str] = field(default_factory=dict)
    #: A remote source, pinned by content hash. See `library.py`.
    source: str = ""
    sha256: str = ""
    #: Post-condition — what must be true after this step.
    expect: Selector | None = None
    gone: Selector | None = None
    #: Choose among equally-matching candidates by meaning. See `Pick`.
    pick: "Pick | None" = None
    #: A failure here does not fail the script.
    optional: bool = False
    #: Press Enter (or another key) straight after typing, saving a step.
    then_key: str = ""
    #: type: "picks" types `value` as a header line, then one line per
    #: product `pick` chose so far, joined with shift+enter so a chat box
    #: keeps them in ONE message. Runner data, not an expression.
    lines_from: str = ""
    #: wait_message: words a NEW chat message must contain, words that end
    #: the run, and where to put a numeric code if one arrives instead.
    words: tuple[str, ...] = ()
    stop_words: tuple[str, ...] = ()
    code_as: str = ""
    stop_after: float = 0.0
    #: wait_message: a match unlocks the commit guard for the rest of the
    #: run - the person said "buy" in their own chat.
    authorizes: bool = False
    #: assist: what to achieve on a page nobody scripted, asked of a
    #: stronger model with a screenshot, a few steps at a time.
    goal: str = ""
    rounds: int = 4
    save_as: str = ""
    note: str = ""


@dataclass
class Script:
    """A named, parameterised sequence. Also a function, when called."""

    name: str
    steps: list[Step]
    params: dict[str, str] = field(default_factory=dict)
    version: int = 1
    trust: Trust = Trust.DRAFT
    description: str = ""
    #: Where it came from, for the report. Never grants trust.
    origin: str = ""
    #: Top-level keys this format does not interpret - `site`, `listed`,
    #: `tags`, `source_task`, `cancel` - kept so that loading and saving a
    #: script never silently drops what an app wrote into it.
    extra: dict = field(default_factory=dict)

    def declared_params(self) -> set[str]:
        return set(self.params)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def _check_url(url: str, where: str) -> None:
    """Only http(s), and only with a host.

    A URL in a script is handed to the shell to open, so the scheme decides
    what program receives it. `http`/`https` reach a browser; almost
    everything else is a registered protocol handler that can launch an
    arbitrary program with arbitrary arguments — which is a large door to
    leave open in a file that may have come from a URL itself.

    A bare `migros.com.tr` is accepted and normalised by `normalise_url`,
    because that is how people write them.
    """
    low = url.strip().lower()
    got = _SCHEME.match(low)
    if not got:
        return  # a bare host; normalise_url will make it https
    scheme = got.group(1)
    if scheme not in ("http", "https"):
        raise ScriptError(
            f"{where}: {scheme}: is not allowed in a url — only http and "
            f"https. Use `open` with an `app` to launch a program.")
    if not low.split(":", 1)[1].lstrip("/").strip():
        raise ScriptError(f"{where}: {url!r} has no host")


def normalise_url(url: str) -> str:
    """`migros.com.tr` → `https://migros.com.tr`. Left alone if it has one."""
    return url if "://" in url else f"https://{url}"


def _selector(raw: Any, where: str) -> Selector:
    if raw is None:
        raise ScriptError(f"{where}: a selector is required")
    if isinstance(raw, str):
        return Selector(text=raw)
    if not isinstance(raw, dict):
        raise ScriptError(f"{where}: a selector must be text or an object, "
                          f"got {type(raw).__name__}")
    match = str(raw.get("match", "contains"))
    if match not in ("contains", "exact"):
        raise ScriptError(f"{where}: match must be 'contains' or 'exact', "
                          f"got {match!r}")
    alts = raw.get("any") or ()
    if isinstance(alts, str):
        alts = (alts,)
    area = str(raw.get("area", "")).lower()
    if area not in ("", "content", "window"):
        raise ScriptError(f"{where}: area must be 'content' or 'window', "
                          f"got {area!r}")
    sel = Selector(
        text=str(raw.get("text", "")), kind=str(raw.get("kind", "")),
        match=match, any=tuple(str(a) for a in alts),
        near=str(raw.get("near", "")), nth=int(raw.get("nth", 0) or 0),
        icon=str(raw.get("icon", "")), area=area,
        id=str(raw.get("id", "")),
        nearest=str(raw.get("nearest", "")).lower())
    if sel.nearest not in ("", "last"):
        raise ScriptError(f"{where}: nearest must be 'last', got "
                          f"{sel.nearest!r}")
    if not (sel.text or sel.any or sel.icon or sel.kind or sel.id):
        raise ScriptError(f"{where}: a selector needs text, any, icon or kind "
                          f"— an empty one matches everything")
    if sel.nth < 0:
        raise ScriptError(f"{where}: nth is 1-based and cannot be negative")
    return sel


def _step(raw: Any, where: str) -> Step:
    if not isinstance(raw, dict):
        raise ScriptError(f"{where}: a step must be an object")
    do = str(raw.get("do", "")).strip()
    if do not in ALL_KINDS:
        raise ScriptError(f"{where}: unknown step {do!r}. "
                          f"Known: {', '.join(sorted(ALL_KINDS))}")

    s = Step(do=do, optional=bool(raw.get("optional", False)),
             note=str(raw.get("note", "")))

    if do in ("click", "double_click", "right_click", "move",
              "drag", "expect", "gone", "wait_for", "capture"):
        s.target = _selector(raw.get("target"), f"{where}.target")
    if do == "scroll" and raw.get("target") is not None:
        # Optional: scrolling the focused pane is the common case, and
        # naming a target is how you scroll a particular list instead.
        s.target = _selector(raw["target"], f"{where}.target")
    if do == "drag":
        s.to = _selector(raw.get("to"), f"{where}.to")
    if do == "type":
        # `into` is optional: straight after ctrl+l or a Tab the caret is
        # already where it should be, and making a script name the address
        # bar it just focused is both noise and a thing that can fail.
        if raw.get("into") or raw.get("target"):
            s.into = _selector(raw.get("into") or raw.get("target"),
                               f"{where}.into")
        if "value" not in raw:
            raise ScriptError(f"{where}: type needs a value")
        s.value = str(raw["value"])
        s.then_key = str(raw.get("then", ""))
    if do == "open":
        s.app = str(raw.get("app", "")).strip()
        s.url = str(raw.get("url", "")).strip()
        if not s.app and not s.url:
            raise ScriptError(f"{where}: open needs an app or a url")
        if s.url and not PARAM.search(s.url):
            # Substituted URLs are checked at run time instead, once the
            # parameter has a value.
            _check_url(s.url, where)
        s.window = str(raw.get("window", "")).strip()
        s.maximize = bool(raw.get("maximize", True))
        s.reuse = bool(raw.get("reuse", True))
        s.timeout = float(raw.get("timeout", 30.0))
    if do == "focus":
        s.window = str(raw.get("window", "")).strip()
        if not s.window:
            raise ScriptError(f"{where}: focus needs a window title")
        s.maximize = bool(raw.get("maximize", True))
        # Default FALSE here, unlike `open`: a `focus` may take a window the
        # person had before the run only if the script says so outright
        # (or the window is a reusable app's - the runner decides that).
        s.reuse = bool(raw.get("reuse", False))
        s.timeout = float(raw.get("timeout", 15.0))
    if do == "scroll":
        s.direction = str(raw.get("direction", "down")).lower()
        if s.direction not in ("up", "down"):
            raise ScriptError(f"{where}: direction must be 'up' or 'down', "
                              f"got {s.direction!r}")
        s.amount = int(raw.get("amount", 3) or 3)
        if not 1 <= s.amount <= 100:
            raise ScriptError(f"{where}: amount must be 1-100")
    if do == "key":
        s.keys = str(raw.get("keys", "")).strip()
        if not s.keys:
            raise ScriptError(f"{where}: key needs keys")
    if do == "wait":
        s.seconds = float(raw.get("seconds", 1.0))
        if not 0 <= s.seconds <= 300:
            raise ScriptError(f"{where}: seconds must be 0-300, "
                              f"got {s.seconds}")
    if do in ("wait_for", "expect", "gone"):
        s.timeout = float(raw.get("timeout", 10.0))
    if do in ("wait_for", "expect", "gone", "open", "focus"):
        if not 0 < s.timeout <= 300:
            raise ScriptError(f"{where}: timeout must be 0-300")
    if do == "type":
        s.lines_from = str(raw.get("lines_from", ""))
        if s.lines_from not in ("", "picks"):
            raise ScriptError(f"{where}: lines_from must be 'picks'")
    if do == "wait_message":
        # A person answering from their phone takes minutes, not seconds,
        # so this is the one wait allowed to be long.
        def _words(key):
            v = raw.get(key) or ()
            return tuple(str(x) for x in ([v] if isinstance(v, str) else v))
        s.words = _words("words")
        s.stop_words = _words("stop_words")
        s.code_as = str(raw.get("code_as", ""))
        if not (s.words or s.code_as):
            raise ScriptError(f"{where}: wait_message needs words or code_as")
        if s.code_as and not PARAM.fullmatch("{" + s.code_as + "}"):
            raise ScriptError(f"{where}: code_as={s.code_as!r} is not a name")
        s.stop_after = float(raw.get("stop_after", 0.0))
        s.authorizes = bool(raw.get("authorizes", False))
        s.timeout = float(raw.get("timeout", 1800.0))
        if not 0 < s.timeout <= 7200:
            raise ScriptError(f"{where}: timeout must be 0-7200")
    if do == "capture":
        s.save_as = str(raw.get("save_as", ""))
        if not PARAM.fullmatch("{" + s.save_as + "}"):
            raise ScriptError(f"{where}: capture needs a valid save_as name")
        if s.optional:
            raise ScriptError(f"{where}: capture cannot be optional")
    if do in ("assist", "observe"):
        if do == "observe" and not raw.get("expect"):
            raise ScriptError(f"{where}: observe needs an expect completion check")
        s.goal = str(raw.get("goal", "")).strip()
        if not s.goal:
            raise ScriptError(f"{where}: assist needs a goal")
        s.rounds = int(raw.get("rounds", 4))
        if not 1 <= s.rounds <= 10:
            raise ScriptError(f"{where}: rounds must be 1-10")
    if do == "if":
        s.present = _selector(raw.get("present"), f"{where}.present")
        s.then = [_step(x, f"{where}.then[{i}]")
                  for i, x in enumerate(raw.get("then") or [])]
        s.otherwise = [_step(x, f"{where}.else[{i}]")
                       for i, x in enumerate(raw.get("else") or [])]
        if not s.then and not s.otherwise:
            raise ScriptError(f"{where}: if needs then or else")
    if do == "repeat":
        each = raw.get("for_each")
        s.times = int(raw.get("times", 0) or 0)
        if isinstance(each, str):
            m = PARAM.fullmatch(each.strip())
            if not m:
                raise ScriptError(f"{where}: for_each as text must be exactly "
                                  f"one parameter, like \"{{products}}\"")
            s.for_each_param = m.group(1)
            s.as_name = str(raw.get("as", "item"))
            if not PARAM.fullmatch("{" + s.as_name + "}"):
                raise ScriptError(f"{where}: as={s.as_name!r} is not a valid "
                                  f"name")
        elif each is not None:
            if not isinstance(each, list) or not each:
                raise ScriptError(f"{where}: for_each must be a non-empty "
                                  f"list of strings — a query is not allowed, "
                                  f"because it would need scoping rules")
            s.for_each = tuple(str(x) for x in each)
            s.as_name = str(raw.get("as", "item"))
            if not PARAM.fullmatch("{" + s.as_name + "}"):
                raise ScriptError(f"{where}: as={s.as_name!r} is not a valid "
                                  f"name")
        elif s.times <= 0:
            raise ScriptError(f"{where}: repeat needs times or for_each")
        if s.times > 1000:
            raise ScriptError(f"{where}: times={s.times} is too many")
        s.steps = [_step(x, f"{where}.steps[{i}]")
                   for i, x in enumerate(raw.get("steps") or [])]
        if not s.steps:
            raise ScriptError(f"{where}: repeat needs steps")
    if do == "call":
        s.script = str(raw.get("script", "")).strip()
        if not s.script:
            raise ScriptError(f"{where}: call needs a script name")
        args = raw.get("with") or {}
        if not isinstance(args, dict):
            raise ScriptError(f"{where}: with must be an object")
        s.with_args = {str(k): str(v) for k, v in args.items()}
        s.source = str(raw.get("source", ""))
        s.sha256 = str(raw.get("sha256", "")).lower()
        if s.source and not s.sha256:
            raise ScriptError(
                f"{where}: a remote script must be pinned. Add the sha256 of "
                f"the exact content you reviewed — without it the runner "
                f"would execute whatever the server returns today.")
        if s.sha256 and not re.fullmatch(r"[0-9a-f]{64}", s.sha256):
            raise ScriptError(f"{where}: sha256 must be 64 hex characters")

    if raw.get("pick") is not None:
        pk = raw["pick"]
        if not isinstance(pk, dict):
            raise ScriptError(f"{where}.pick: must be an object")
        goal = str(pk.get("goal", "")).strip()
        if not goal:
            raise ScriptError(f"{where}.pick: needs a goal - what a GOOD "
                              f"candidate looks like")
        label = str(pk.get("label", "nearest_title"))
        if label != "nearest_title":
            raise ScriptError(f"{where}.pick: label must be 'nearest_title'")
        margin = float(pk.get("min_margin", 0.25))
        if not 0.0 <= margin <= 1.0:
            raise ScriptError(f"{where}.pick: min_margin must be 0-1")
        unsure = str(pk.get("on_unsure", "fail")).lower()
        if unsure not in ("fail", "top"):
            raise ScriptError(f"{where}.pick: on_unsure must be "
                              f"'fail' or 'top', got {unsure!r}")
        s.pick = Pick(goal=goal, reject=str(pk.get("reject", "")),
                      label=label, min_margin=margin, on_unsure=unsure,
                      max_options=int(pk.get("max_options", 10)),
                      radius=int(pk.get("radius", 280)))

    if raw.get("expect") is not None:
        s.expect = _selector(raw["expect"], f"{where}.expect")
    if raw.get("gone") is not None:
        s.gone = _selector(raw["gone"], f"{where}.gone")
    return s


#: The top-level keys `parse` reads itself. Anything else goes to `extra`.
_TOP_KEYS = ("name", "version", "description", "params", "trust", "steps")


def parse(raw: Any, origin: str = "") -> Script:
    """Turn JSON into a Script, or explain exactly why it cannot.

    Every error names its path (`steps[3].then[1].target`) because a script
    Claude wrote is fixed by Claude reading the error, and "invalid script"
    is not something anything can act on.
    """
    if not isinstance(raw, dict):
        raise ScriptError("a script must be an object")
    name = str(raw.get("name", "")).strip()
    if not name:
        raise ScriptError("a script needs a name")
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise ScriptError(f"{name}: steps must be a non-empty list")

    params = {str(k): str(v) for k, v in (raw.get("params") or {}).items()}
    for p in params:
        if not PARAM.fullmatch("{" + p + "}"):
            raise ScriptError(f"{name}: {p!r} is not a valid parameter name")

    script = Script(
        name=name,
        steps=[_step(s, f"{name}.steps[{i}]")
               for i, s in enumerate(steps_raw)],
        params=params, version=int(raw.get("version", 1) or 1),
        description=str(raw.get("description", "")),
        origin=origin,
        # Whatever the file claims, an import is a draft. Trust is local
        # knowledge and does not travel.
        trust=IMPORT_TRUST,
        extra={k: v for k, v in raw.items() if k not in _TOP_KEYS})
    check_params(script)
    return script


def substitute(text: str, values: dict[str, str], where: str = "") -> str:
    """Replace `{name}` with a value. Only that, and only in string fields.

    An unknown name is an error rather than an empty string: a script that
    types "" into a search box and carries on is worse than one that refuses
    to start.
    """
    missing = [m.group(1) for m in PARAM.finditer(text)
               if m.group(1) not in values]
    if missing:
        raise ScriptError(f"{where}: no value for {', '.join(missing)}")
    return PARAM.sub(lambda m: values[m.group(1)], text)


def used_params(step: Step) -> set[str]:
    """Every `{name}` this step and its children mention."""
    out: set[str] = set()
    for field_name in SUBSTITUTABLE:
        val = getattr(step, field_name if field_name != "any" else "value", "")
        if isinstance(val, str):
            out |= {m.group(1) for m in PARAM.finditer(val)}
    for sel in (step.target, step.into, step.to, step.present, step.expect,
                step.gone):
        if sel is None:
            continue
        for s in (sel.text, sel.near, sel.icon, sel.id, *sel.any):
            out |= {m.group(1) for m in PARAM.finditer(s)}
    for v in step.with_args.values():
        out |= {m.group(1) for m in PARAM.finditer(v)}
    if step.for_each_param:
        out.add(step.for_each_param)
    for v in step.for_each:
        out |= {m.group(1) for m in PARAM.finditer(v)}
    if step.pick is not None:
        for t in (step.pick.goal, step.pick.reject):
            out |= {m.group(1) for m in PARAM.finditer(t)}
    for child in (*step.then, *step.otherwise, *step.steps):
        out |= used_params(child)
    if step.as_name:
        out.discard(step.as_name)
    return out


def check_params(script: Script) -> None:
    """Every `{name}` must be declared, before anything runs.

    Caught here rather than at the step that needs it, so a script does not
    get four steps into a form and then discover it has nothing to type.
    """
    declared = script.declared_params()
    for i, step in enumerate(script.steps):
        unknown = used_params(step) - declared
        if unknown:
            raise ScriptError(
                f"{script.name}.steps[{i}]: uses {sorted(unknown)} but the "
                f"script declares {sorted(declared) or 'no parameters'}")
        if step.do == "capture":
            if step.save_as in declared:
                raise ScriptError(f"{script.name}: capture cannot overwrite {step.save_as}")
            declared.add(step.save_as)
