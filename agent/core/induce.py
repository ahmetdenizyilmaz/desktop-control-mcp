"""Turning a demonstration into a job — and asking before believing it.

The recorder captures what happened. What it cannot capture is *why*: a click
at (612, 340) on a control named "Export" is a fact, but "download this month's
invoices" is the thing worth saving. Only the model can bridge that, and only
the user can confirm it.

Hence the shape of this module. It does the mechanical half — dropping noise,
merging keystrokes into typed strings, keeping the locator stack that was
resolved live, and proposing a plain-language intent for each step from what
the elements were actually called. Then it stops and produces a **draft** for
the user to confirm, because the failure mode here is specific and expensive:
a job saved from a misread demonstration looks correct, replays cleanly, and
does the wrong thing every time it runs.

The draft is deliberately readable as English rather than as JSON. A user
confirming "click Export, then type the filename, then click Save" can spot a
wrong step; a user confirming a locator array cannot.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .schema import Action, LocalState, Locator, LocatorType, Macro, Step, Trust

#: Clicks this close together on the same element are one intent, not several.
DEDUP_WINDOW_S = 0.45

#: A demonstration always starts and ends with the user reaching for the
#: recorder itself. Those clicks are never part of the job. The app's window
#: title comes first (app.py APP_TITLE — keep the two equal, or every click
#: on our own window lands in the job); the name the app had before its
#: rename stays, so a demonstration made against that build reads the same.
CHROME_TITLES = ("AGI Agent", "Desktop Agent", "Recording", "Claude")


@dataclass
class DraftStep:
    action: Action
    intent: str
    locators: list[Locator] = field(default_factory=list)
    args: dict = field(default_factory=dict)
    window: str = ""
    confidence: float = 1.0
    note: str = ""
    #: Epoch seconds. Desktop events are already in seconds; browser events
    #: arrive from Date.now() in milliseconds and are converted. Without a
    #: common scale the two halves cannot be interleaved at all.
    at: float = 0.0

    def sentence(self) -> str:
        """One line a human can check without knowing the format."""
        if self.action is Action.TYPE:
            text = self.args.get("text", "")
            shown = text if len(text) <= 40 else text[:37] + "…"
            return f'type "{shown}"'
        if self.action is Action.KEY:
            return f"press {self.args.get('keys', '')}"
        if self.action is Action.LAUNCH:
            return f"open {self.args.get('app', '')}"
        if self.action is Action.CLICK:
            target = self.intent or "something unnamed"
            return f"click {target}"
        if self.action is Action.NAVIGATE:
            return f"open {self.args.get('url', '')[:64]}"
        if self.action is Action.WEB_CLICK:
            state = self.args.get("state")
            verb = {"checked": "tick", "unchecked": "untick"}.get(state, "click")
            return f"{verb} {self.args.get('label') or self.args.get('selector', '')}"
        if self.action is Action.WEB_FILL:
            what = self.args.get("label") or self.args.get("selector", "")
            val = str(self.args.get("value", ""))
            if val.startswith("secret://"):
                return f"enter a stored password into {what}"
            shown = val if len(val) <= 34 else val[:31] + "…"
            return f'enter "{shown}" into {what}'
        return self.action.value


@dataclass
class Draft:
    """A proposed job, not a saved one.

    Nothing here is written to the library until `confirm()` is called, and
    `confirm()` is only ever called after the user has seen `describe()`.
    """
    steps: list[DraftStep] = field(default_factory=list)
    name: str = ""
    description: str = ""
    windows: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def weakest(self) -> float:
        return min((s.confidence for s in self.steps), default=1.0)

    def describe(self) -> str:
        """What the user is asked to confirm."""
        lines = [f"I think this recording does:  {self.description or '(unnamed)'}",
                 ""]
        for i, s in enumerate(self.steps, 1):
            flag = ""
            if s.confidence < 0.6:
                flag = f"   ← not sure: {s.note}" if s.note else "   ← not sure"
            lines.append(f"  {i}. {s.sentence()}{flag}")
        if self.dropped:
            lines.append("")
            lines.append("Ignored: " + "; ".join(self.dropped[:4])
                         + (f" (+{len(self.dropped) - 4} more)"
                            if len(self.dropped) > 4 else ""))
        lines.append("")
        lines.append("Is that right? Correct anything that is wrong before it "
                     "is saved — a job saved from a misread demonstration "
                     "replays cleanly and does the wrong thing every time.")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# building the draft
# ---------------------------------------------------------------------------

def _is_our_chrome(window: str) -> bool:
    return any(c.lower() in (window or "").lower() for c in CHROME_TITLES)


def _locators_from(raw: list[dict]) -> list[Locator]:
    out = []
    for d in raw or []:
        try:
            out.append(Locator(LocatorType(d["type"]), d.get("value", {})))
        except Exception:
            continue
    return out


def _intent_for(ev: dict) -> tuple[str, float, str]:
    """A plain-language target, plus how much to trust it.

    A named control is the good case and needs no guessing. A nameless one is
    the case that matters: the step is kept, but flagged low-confidence so the
    confirmation step draws the user's eye to it rather than burying it.
    """
    name = (ev.get("element_name") or "").strip()
    etype = (ev.get("element_type") or "").replace("Control", "")
    if name:
        label = f"{name!r}" + (f" ({etype.lower()})" if etype else "")
        return label, 1.0, ""
    if ev.get("locators"):
        return (f"an unnamed {etype.lower() or 'control'}", 0.5,
                "the control has no name — replay will fall back to position")
    return ("a spot on screen", 0.25,
            "nothing identifiable was under the cursor; this step will not "
            "survive a window move")


def from_recording(summary: dict) -> Draft:
    """Mechanical pass over a recording. No model involved.

    Whatever a model later contributes — a better name, a better description —
    is layered on top of this; the step list itself comes from what was
    observed, so an imaginative description can never invent an action.
    """
    draft = Draft(duration_s=float(summary.get("duration_s") or 0))
    events = summary.get("events") or []

    last_click: tuple[str, float] | None = None
    for ev in events:
        window = ev.get("window_title") or ""
        kind = ev.get("type")

        if _is_our_chrome(window):
            draft.dropped.append(f"{kind} in {window[:28]}")
            continue

        if kind == "click":
            key = f"{ev.get('element_name')}|{ev.get('locators')}"
            now = float(ev.get("t") or 0)
            if last_click and last_click[0] == key and now - last_click[1] < DEDUP_WINDOW_S:
                draft.dropped.append("a repeated click on the same control")
                continue
            last_click = (key, now)
            label, conf, note = _intent_for(ev)
            draft.steps.append(DraftStep(
                Action.CLICK, label, _locators_from(ev.get("locators")),
                {}, window, conf, note, at=float(ev.get("t") or 0)))

        elif kind == "type":
            text = ev.get("text") or ""
            if not text.strip():
                continue
            draft.steps.append(DraftStep(
                Action.TYPE, "enter text", [], {"text": text}, window, 1.0,
                at=float(ev.get("t") or 0)))

        elif kind == "key":
            keys = ev.get("keys") or ""
            if not keys:
                continue
            draft.steps.append(DraftStep(
                Action.KEY, f"press {keys}", [], {"keys": keys}, window, 1.0,
                at=float(ev.get("t") or 0)))

        elif kind == "scroll":
            # Scrolling is how you find something, not part of doing it. Replay
            # resolves elements by identity and scrolls them into view itself,
            # so replaying the scroll would fight it.
            draft.dropped.append("scrolling (replay scrolls to the element itself)")

    draft.windows = sorted({s.window for s in draft.steps if s.window})
    if draft.windows and not draft.description:
        app = draft.windows[0].split(" - ")[-1] or draft.windows[0]
        draft.description = f"{len(draft.steps)} steps in {app}"
    return draft


#: What a recorded browser event becomes. `fill` covers typing; the rest map
#: one-for-one onto what `web_do` already knows how to perform.
WEB_ACTIONS = {"click": Action.WEB_CLICK, "fill": Action.WEB_FILL,
               "select": Action.WEB_FILL, "check": Action.WEB_CLICK,
               "uncheck": Action.WEB_CLICK}


def _web_intent(ev: dict) -> str:
    label = (ev.get("label") or "").strip()
    what = f"{label!r}" if label else f"the element at {ev.get('selector', '?')}"
    return {"click": f"click {what}",
            "fill": f"enter text into {what}",
            "select": f"choose an option in {what}",
            "check": f"tick {what}",
            "uncheck": f"untick {what}"}.get(ev.get("kind", ""), what)


def web_steps(events: list[dict]) -> tuple[list[DraftStep], list[str]]:
    """Turn recorded browser events into draft steps.

    Returns the steps and any warnings. A password field is recorded WITHOUT
    its value — the browser side refuses to capture one — so the step is kept
    and pointed at the vault instead of being silently dropped, which would
    leave a login flow that mysteriously does not log in.
    """
    steps: list[DraftStep] = []
    warnings: list[str] = []
    last_url = ""

    for ev in sorted(events or [], key=lambda e: e.get("t", 0)):
        kind = ev.get("kind", "")
        if kind not in WEB_ACTIONS:
            continue
        url = ev.get("url") or ""
        if url and url != last_url:
            # A file:// URL contains this machine's home directory. The job
            # format exists to be shareable, so embedding one silently would
            # produce a job that cannot possibly work anywhere else — and
            # leaks a username into a file meant to be published.
            local_file = url.lower().startswith("file:")
            steps.append(DraftStep(
                Action.NAVIGATE, f"open {url[:60]}", [],
                {"url": url}, window="browser",
                at=float(ev.get("t") or 0) / 1000.0,
                confidence=0.4 if local_file else 1.0,
                note=("this is a local file path, not a web address — it names "
                      "this machine and will not work on another one"
                      if local_file else "")))
            if local_file:
                warnings.append(
                    f"step {len(steps)} opens a local file. A shared job cannot "
                    f"use it; replace the path with a symbol or a real URL.")
            last_url = url

        selector = ev.get("selector") or ""
        args: dict = {"selector": selector, "label": ev.get("label", "")}
        conf, note = 1.0, ""

        if ev.get("secret"):
            args["value"] = "secret://"          # named at confirmation time
            conf, note = 0.5, ("a password was typed here; its value was not "
                               "recorded. Give the credential a name and the "
                               "job will fetch it from the vault")
            warnings.append(
                f"step {len(steps) + 1} types a password. Store it under a "
                f"name and the job will reference secret://<name> — a recorded "
                f"password would sit in plain text in a shareable file.")
        elif kind in ("fill", "select"):
            args["value"] = ev.get("value", "")
            # A <select> needs select_option, not fill. Both are WEB_FILL as
            # far as the job format is concerned, so the distinction has to
            # travel in the args or replay types into a dropdown and fails.
            if kind == "select":
                args["how"] = "select"

        if kind == "check":
            args["state"] = "checked"
        elif kind == "uncheck":
            args["state"] = "unchecked"

        # A positional selector is the least durable thing we can record.
        # Kept, but flagged, so the confirmation prompt shows it as the step
        # most likely to break when the page is redesigned.
        if selector and ">" in selector and not selector.startswith(("#", "[")):
            conf = min(conf, 0.45)
            note = note or ("this element had no id, name or test id — the "
                            "selector is positional and will not survive a "
                            "redesign")

        steps.append(DraftStep(WEB_ACTIONS[kind], _web_intent(ev), [],
                               args, window="browser", confidence=conf,
                               note=note, at=float(ev.get("t") or 0) / 1000.0))
    return steps, warnings


def from_web_recording(events: list[dict], desktop: dict | None = None) -> Draft:
    """A draft from browser events, optionally merged with desktop ones.

    Merged by timestamp rather than concatenated: a real task moves between
    the browser and an application, and replaying all the web steps first
    would be a different task from the one demonstrated.
    """
    draft = from_recording(desktop or {"events": [], "duration_s": 0})
    steps, warnings = web_steps(events)
    if not steps:
        return draft

    # Interleave by time. Concatenating would put every web step after every
    # desktop step, which for a task that moves between a browser and an
    # application is simply a different task from the one demonstrated.
    # Steps with no timestamp keep their relative order at the front.
    merged = draft.steps + steps
    draft.steps = sorted(merged, key=lambda s: (s.at == 0, s.at))
    draft.dropped.extend(warnings)
    if not draft.description:
        hosts = sorted({s.args.get("url", "") for s in steps if s.args.get("url")})
        where = hosts[0][:40] if hosts else "a web page"
        draft.description = f"{len(draft.steps)} steps in {where}"
    draft.windows = sorted(set(draft.windows) | {"browser"})
    return draft


def looks_sensitive(draft: Draft) -> list[str]:
    """Typed text that should have been a credential reference.

    A recording captures keystrokes verbatim, so demonstrating a login writes
    the password into the job file in plain text. That must be caught before
    the job is saved, not after it is shared.
    """
    warnings = []
    for i, s in enumerate(draft.steps, 1):
        if s.action is not Action.TYPE:
            continue
        text = s.args.get("text", "")
        prev = draft.steps[i - 2] if i >= 2 else None
        near_password = prev is not None and re.search(
            r"pass|şifre|sifre|pin|secret|token", prev.intent, re.I)
        looks_like_secret = (
            len(text) >= 8 and not text.count(" ")
            and re.search(r"[A-Za-z]", text) and re.search(r"\d|[^\w\s]", text))
        if near_password or looks_like_secret:
            warnings.append(
                f"step {i} types something that may be a password. Store it "
                f"with a name and let the job reference secret://<name> "
                f"instead — a job file is meant to be shareable.")
    return warnings


def to_macro(draft: Draft, name: str = "", description: str = "",
             tags: list[str] | None = None) -> tuple[Macro, LocalState]:
    """Freeze a confirmed draft into a job.

    Trust starts at DRAFT regardless of how clean the recording looked: the
    first real run is what earns anything, and a demonstration is not a run.
    """
    steps = [Step(action=s.action, intent=s.intent, locators=s.locators,
                  args=s.args) for s in draft.steps]
    macro = Macro(
        name=(name or draft.name or "recorded-job").strip(),
        description=(description or draft.description).strip(),
        steps=steps,
        tags=list(tags or []),
        permissions=["input"],
    )
    return macro, LocalState(macro_id=macro.id, trust=Trust.DRAFT,
                             origin="recorded")


def load_summary(recording_dir: Path | str) -> dict:
    """Read a saved recording back into the shape `from_recording` wants."""
    d = Path(recording_dir)
    events = []
    ev_file = d / "events.jsonl"
    if ev_file.exists():
        for line in ev_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue
    meta = {}
    if (d / "meta.json").exists():
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"events": events, "duration_s": meta.get("duration_s", 0),
            "keyframes": [], "windows_touched": sorted(
                {e.get("window_title", "") for e in events if e.get("window_title")})}
