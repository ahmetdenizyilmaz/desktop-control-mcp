"""Run a whole multi-task plan with System-1 decisions, from one planning call.

    python jev_plan_run.py --plan logs/plan.json --headed --record logs/run.mp4

The experiment this exists for: how few Claude turns can a long task cost?
One model call writes the plan — a list of tasks, each a list of narrow,
screen-independent goals, with any text that must be typed supplied up front,
because the decision model cannot generate text. After that, every step is
perception (the DOM snapshot) plus one Jev call, until something surprises
the loop and it stops to say so.

Plan format:

    {"url": "https://…",
     "tasks": [{"name": "süt",
                "goals": [{"goal": "type the product name into the site "
                                   "search box and run the search",
                           "type": "süt"},
                          {"goal": "add the first matching product to the "
                                   "basket"}]},
               …]}

History is reset at each task. It has to be: `already_done` tells the model
not to repeat itself, and "type into the search box" is exactly what every
product starts with — carried across tasks, the second product's search reads
as a repeat of the first.

A failed task does not end the plan; the loop moves to the next one. Two
failed tasks running does end it — that is a site-level obstacle (a login
wall, a delivery-method dialog), not a bad product, and thrashing through
eight more tasks against it would only spend calls.

The commit guard is armed throughout and nothing here overrides it. A guard
hold is reported as such, not as a failure: reaching the last step before
money and stopping there is the point of the design.

Create `logs/STOP` to halt the run between steps.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import browser as browsermod          # noqa: E402
from core import fast, jev                      # noqa: E402
from jev_live_shop import rows_from_snapshot    # noqa: E402

HERE = Path(__file__).resolve().parent
STOP_FILE = HERE / "logs" / "STOP"
MAX_STEPS_PER_GOAL = 7

#: How long the page gets to respond before it is re-read. Navigation needs
#: longer than filling a field; both are waiting on the site, not on a model.
SETTLE = {"click": 2.2, "submit": 2.5, "select": 1.5, "type": 0.8,
          "scroll_down": 0.8, "scroll_up": 0.8}


#: Finds what the engine's snapshot cannot see, and scopes to an open modal.
#:
#: `SNAPSHOT_JS` lists semantic controls — a, button, input, [role=…],
#: [onclick]. Component frameworks routinely build clickable things out of
#: none of those: Migros's delivery-method cards are bare <div>s inside
#: <fe-selectable-card><mat-card>, clickable only through a framework
#: listener, and the scout confirmed the snapshot does not list them. The
#: decision model cannot choose a row it was never shown, so no wording of a
#: goal could have got past that dialog.
#:
#: The signal used is the one a person uses: the cursor turns into a hand.
#: `cursor` is inherited, so only the OUTERMOST pointer element is taken (its
#: parent is not a pointer), and only when no semantic control sits inside or
#: around it — those are already listed.
#:
#: The second half is ARIA's own rule: an open modal blocks the page behind
#: it, so while one is open only its contents are offered. That also shrinks
#: a 130-row page to the four rows that matter.
#:
#: Kept in the runner rather than in `browser.py` on purpose: it changes what
#: every snapshot returns, and that deserves the full test suite first.
AUGMENT_JS = r"""(startIdx) => {
  const SEL = 'a,button,input,select,textarea,summary,[role=button],[role=link],'
    + '[role=tab],[role=checkbox],[role=radio],[role=menuitem],[role=option],'
    + '[role=switch],[contenteditable=true],[onclick]';
  const big = m => { const r = m.getBoundingClientRect();
                     return r.width > 80 && r.height > 80; };
  const modals = Array.from(document.querySelectorAll(
      '[role=dialog],[aria-modal=true],dialog[open],mat-dialog-container'))
      .filter(big);
  const modal = modals.length ? modals[modals.length - 1] : null;
  const scope = modal || document.body;
  const extra = []; let i = startIdx;
  // Is this card the chosen one? A role-less card usually keeps its state on
  // a wrapper (class "…radio-checked", aria-checked) or on a hidden native
  // input beside it, so look a few levels up — but stop at the first ancestor
  // holding more than one radio/checkbox, or every card in the group would
  // inherit the one checked input. Without this the plan's condition "one
  // store is already picked" could not be verified from the state, and the
  // model split 0.74 / 0.10 between confirming and picking a store itself.
  const isSelected = (el) => {
    let p = el;
    for (let d = 0; d < 4 && p && p !== document.body; d++, p = p.parentElement) {
      if (p.querySelectorAll('input[type=radio],input[type=checkbox]').length > 1)
        break;
      if (p.matches('[aria-checked="true"],[aria-selected="true"],'
                    + '[class*="radio-checked"],[class*="checkbox-checked"]'))
        return true;
      if (p.querySelector('input:checked,[aria-checked="true"]')) return true;
    }
    return false;
  };
  // An overlay is appended at the END of <body>, after every element of the
  // page behind it — so the engine's snapshot, which stops at its element
  // limit in DOM order, runs out before reaching the dialog's own buttons.
  // Measured: the store-list dialog showed its eight store cards and not the
  // "Mağazadan Teslim Al" button that confirms them. Any semantic control in
  // the modal the snapshot did not reach is stamped here.
  if (modal) {
    for (const el of modal.querySelectorAll(SEL)) {
      if (el.hasAttribute('data-da-idx')) continue;
      const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      if (r.width < 2 || r.height < 2 || cs.visibility === 'hidden'
          || cs.display === 'none' || cs.opacity === '0') continue;
      const secret = (el.getAttribute('type') || '').toLowerCase() === 'password';
      const name = (el.getAttribute('aria-label') || el.innerText
                    || el.placeholder || el.title || '')
                     .replace(/\s+/g, ' ').trim();
      if (!name) continue;
      const tag = el.tagName.toLowerCase();
      el.setAttribute('data-da-idx', String(i));
      extra.push({i: i, tag: tag, role: el.getAttribute('role') || '',
                  name: name.slice(0, 70),
                  value: (!secret && (tag === 'input' || tag === 'textarea'
                                      || tag === 'select'))
                         ? String(el.value || '').slice(0, 40) : '',
                  testid: el.id || '', inview: true, disabled: !!el.disabled});
      i++;
    }
  }
  for (const el of scope.querySelectorAll('*')) {
    if (extra.length >= 40) break;
    if (el.matches(SEL) || el.closest(SEL) || el.querySelector(SEL)) continue;
    const cs = getComputedStyle(el);
    if (cs.cursor !== 'pointer' || cs.visibility === 'hidden'
        || cs.display === 'none') continue;
    const p = el.parentElement;
    if (p && getComputedStyle(p).cursor === 'pointer') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) continue;
    if (!(r.top < innerHeight && r.bottom > 0)) continue;
    const name = (el.getAttribute('aria-label') || el.innerText || '')
                   .replace(/\s+/g, ' ').trim();
    if (!name || name.length > 90) continue;
    el.setAttribute('data-da-idx', String(i));
    extra.push({i: i, tag: el.tagName.toLowerCase(), role: 'clickable',
                name: name.slice(0, 70),
                value: isSelected(el) ? 'SELECTED' : '',
                testid: el.id || '', inview: true, disabled: false});
    i++;
  }
  const inModal = modal
    ? Array.from(modal.querySelectorAll('[data-da-idx]'))
        .map(e => +e.getAttribute('data-da-idx'))
    : null;
  // The dialog's own title, by ARIA's rules first: aria-label, then
  // aria-labelledby, then the first heading-like node inside it.
  let modalTitle = '';
  if (modal) {
    modalTitle = (modal.getAttribute('aria-label') || '').trim();
    const by = modal.getAttribute('aria-labelledby');
    if (!modalTitle && by) {
      const t = document.getElementById(by.split(' ')[0]);
      if (t) modalTitle = (t.innerText || '').trim();
    }
    if (!modalTitle) {
      const h = modal.querySelector('[id*="dialog-title"],[class*="dialog-title"],'
                                    + '[role=heading],h1,h2,h3');
      if (h) modalTitle = (h.innerText || '').trim();
    }
    modalTitle = modalTitle.replace(/\s+/g, ' ').slice(0, 80);
  }
  return {extra: extra, inModal: inModal, modal: !!modal, modalTitle: modalTitle};
}"""


def read_page(b) -> tuple[list[dict], list[int], str]:
    """Snapshot, plus role-less clickables, scoped to a modal when one is open."""
    snap = b.snapshot(limit=140)
    els = list(snap.get("elements", []))
    dialog = ""
    try:
        start = max([int(e["i"]) for e in els], default=-1) + 1
        aug = b._ensure().evaluate(AUGMENT_JS, start)
        # Role-less clickables go FIRST. They are few and mostly header
        # furniture — including the basket summary ("1 Sepetim 225,00 TL"),
        # the one place the page says whether an add worked. Appended after
        # a 125-row results page they would sit past the 60-row cap and
        # never reach the model. Order among the page's own rows — which is
        # what "the first product" relies on — is untouched.
        els = (aug.get("extra") or []) + els
        inside = aug.get("inModal")
        if inside:
            keep = set(inside)
            scoped = [e for e in els if int(e["i"]) in keep]
            if scoped:
                els = scoped
        # Say so when the table is a dialog's. Scoping to the modal without
        # naming it cost the first full run: the plan's goal was conditional
        # on "if the delivery dialog is shown", the state showed two rows and
        # a page title, and the model — never told a dialog was open — put
        # 15% on "no such dialog" and landed at 0.77 against a 0.80 gate.
        # The uncertainty was honest; the perception was incomplete.
        if aug.get("modal"):
            dialog = (f" | A DIALOG IS OPEN on top of the page, titled "
                      f"'{aug.get('modalTitle') or 'untitled'}'. Only the "
                      f"dialog's contents are listed.")
    except Exception as e:                    # never let the extra pass cost a run
        print(f"        (augment skipped: {type(e).__name__})", flush=True)
    rows, stamped = rows_from_snapshot({"elements": els})
    return rows, stamped, snap.get("title", "") + dialog


def _real_profile_browser(profile_dir: str):
    """A Browser driving the user's OWN Chrome profile, not the agent's.

    Why: the agent's profile is a stranger to every site. The user's is
    logged in, has their delivery address, and remembers their basket — and
    the whole reason the UIA path existed was to reach that session. This
    reaches it without giving up DOM speed: measured on a Migros results
    page, 44 ms to read through the DOM against 7.1 s through UIA, where the
    products did not even appear until row 113.

    Two things must be true, and both are checked by the caller: real Chrome
    is launched (`channel="chrome"`, not Playwright's bundled Chromium, which
    can rewrite a real profile's state), and Chrome is not already running —
    a profile cannot be open twice, and forcing it risks the profile.

    `--start-maximized` and the automation-flag removal come from the
    engine's own launch options; only the profile and the channel change.
    """
    b = browsermod.Browser(headless=False, profile_dir=Path(profile_dir))
    base = b.launch_options

    def opts(headless=None):
        o = base(headless=False)
        o["channel"] = "chrome"
        # Chrome refuses a profile that another instance holds. Saying so is
        # better than Playwright's own timeout, which reads as a crash.
        o.setdefault("args", []).append("--no-first-run")
        return o

    b.launch_options = opts
    return b


class WebPage:
    """The agent's own Playwright browser: DOM snapshot in, indexed clicks out."""

    def __init__(self, url: str, headed: bool, profile: str = "") -> None:
        if profile:
            self.b = _real_profile_browser(profile)
        else:
            self.b = browsermod.Browser(headless=not headed)
        if not self.b.start():
            raise RuntimeError("browser failed to start")
        self.b.goto(url)
        time.sleep(3.0)

    def read(self):
        return read_page(self.b)

    def host(self) -> str:
        return self.b._host()

    def act(self, op: str, handle, typed: str) -> dict:
        if op == "submit":
            return self.b.act({"do": "press", "value": "Enter"})
        if op in ("click", "select"):
            # A custom dropdown opens on click and its options then appear as
            # ordinary clickable rows, so `select` degrades to a click.
            return self.b.act({"do": "click", "target": handle})
        if op == "type":
            return self.b.act({"do": "fill", "target": handle, "value": typed})
        if op in ("scroll_down", "scroll_up"):
            return self.b.act({"do": "scroll",
                               "value": "down" if op == "scroll_down" else "up"})
        if op == "wait":
            time.sleep(1.5)
            return {"ok": True, "did": "waited"}
        return {"ok": False, "error": f"no handler for {op}"}

    def where(self) -> str:
        try:
            return self.b._ensure().url
        except Exception:
            return ""

    def close(self) -> None:
        self.b.stop()


#: UIA control types worth offering as rows, and what to call them. Text,
#: images and groups are left out: they are most of a page's nodes and none
#: of its choices.
_UIA_KINDS = {"HyperlinkControl": "link", "ButtonControl": "button",
              "SplitButtonControl": "button", "EditControl": "edit",
              "ComboBoxControl": "combobox", "CheckBoxControl": "checkbox",
              "RadioButtonControl": "radio", "MenuItemControl": "menuitem",
              "TabItemControl": "tab", "ListItemControl": "listitem"}


class RealBrowser:
    """The user's OWN browser, driven as a Windows program through UIA.

    The point of this path is the session: the user's everyday browser is
    already logged in, with their address and their basket, and the agent's
    Playwright profile is a stranger to the site. Nothing is injected into
    the page — it is read through Windows accessibility and clicked through
    UIA Invoke, exactly as the engine drives any other application.

    Three things differ from the DOM path and are handled here:

    * The walk is rooted at the page's DocumentControl. Rooted at the window,
      a breadth-first walk spends its row budget on the browser's own toolbar
      — measured earlier: 200 rows of chrome, and the model's best guess for
      "the search box" was the address bar.
    * It is depth-first, so rows come out in document order, which is what
      "the first product" means. `locator.walk` is breadth-first by design.
    * It stops at ~75 interactive rows. `fast.decide` sends 60 anyway, and
      every UIA node costs COM round trips: a bounded read is what keeps a
      step near a second instead of several.

    Every action passes through the engine's Arbiter first, so physical
    ESC+ESC aborts the run and deliberate user input pauses it — the promise
    the fast loop did not keep until now.
    """

    ROW_LIMIT = 230

    def __init__(self, url: str, hint: str) -> None:
        import os
        from core import actuator, locator
        from core.arbiter import Arbiter
        self.locator, self.actuator = locator, actuator
        self.hint = hint.lower()

        os.startfile(url)                    # the default browser, their profile
        self.hwnd = 0
        t0 = time.time()
        while time.time() - t0 < 25 and not self.hwnd:
            time.sleep(1.0)
            for w in locator.list_windows():
                if (self.hint in w["title"].lower()
                        and "Chrome_WidgetWin" in locator.window_class(w["hwnd"])):
                    self.hwnd = w["hwnd"]
                    break
        if not self.hwnd:
            raise RuntimeError(f"no browser window with {hint!r} in its title "
                               f"appeared within 25s")
        actuator.focus_window(self.hwnd)
        actuator.maximize_window(self.hwnd)
        time.sleep(3.0)

        # Started AFTER the browser is in front: the arbiter baselines the
        # foreground window, and would read our own launch as the user
        # switching away.
        self.arb = Arbiter(owner="jev-plan-run", use_overlay=False)
        if not self.arb.start():
            raise RuntimeError(f"arbiter refused: {self.arb.state.detail}")
        if not getattr(self.arb, "hook_ok", True):
            print("  WARNING: keyboard hook unavailable — ESC+ESC will not "
                  "abort; use logs/STOP", flush=True)

    # -- perception ---------------------------------------------------------
    def _document(self):
        loc = self.locator
        loc.invalidate_cache(self.hwnd)
        root = loc.uia_root(self.hwnd)
        # A browser window holds several DocumentControls — measured on
        # Chrome: nine, of which the first five are empty and unnamed, and two
        # belong to a chat widget. The page is the one the window is named
        # after ("<document name> - Google Chrome"); failing that, the biggest.
        title = loc.window_title(self.hwnd).lower()
        best, best_area = None, -1
        for ch in loc.walk(root, max_depth=30, max_nodes=1500, budget_s=3.0):
            try:
                if ch.ControlTypeName != "DocumentControl":
                    continue
                name = (ch.Name or "").strip().lower()
                r = loc._rect_of(ch)
            except Exception:
                continue
            if not r:
                continue
            if name and title.startswith(name):
                return ch
            area = (r[2] - r[0]) * (r[3] - r[1])
            if name and area > best_area:
                best, best_area = ch, area
        return best or root

    def read(self):
        loc = self.locator
        rows, handles = [], []
        t0, nodes = time.time(), 0
        stack = [(self._document(), "")]
        while (stack and nodes < 5000 and time.time() - t0 < 8.0
               and len(rows) < self.ROW_LIMIT):
            c, parent = stack.pop()
            nodes += 1
            try:
                ctype = c.ControlTypeName
                name = (c.Name or "").strip()
            except Exception:
                continue
            try:
                kids = c.GetChildren()
            except Exception:
                kids = []
            for k in reversed(kids):
                stack.append((k, name or parent))

            kind = _UIA_KINDS.get(ctype)
            if not kind or not name:
                continue
            el = loc._to_element(c, "uia:doc", self.hwnd)
            if el is None:
                continue
            value = ""
            if kind in ("edit", "combobox"):
                try:
                    secret = bool(getattr(c, "IsPassword", False))
                    value = "" if secret else (c.GetValuePattern().Value or "")
                except Exception:
                    value = ""
                # Chrome names a bare <input> after its placeholder — on
                # Migros, a rotating advert. The authored label sits on the
                # wrapper, which UIA exposes as the parent's name.
                if parent and parent.lower() not in name.lower():
                    name = f"{name} [inside: {parent}]"
            # A run of identically-named rows is a carousel, not a choice.
            # Measured on the Migros home page: 29 consecutive "Afişe Git"
            # banner links, which would have eaten a third of the budget and
            # pushed the products off the end. Only CONSECUTIVE repeats are
            # collapsed — a results page interleaves each product's add
            # button with its name link, so those never form a run, and
            # "the first product" still means the first one listed.
            if len(rows) >= 2 and all(r["type"] == kind and r["name"] == name[:90]
                                      for r in rows[-2:]):
                continue
            rows.append({"type": kind, "name": name[:90],
                         "id": (getattr(el, "automation_id", "") or "")[:40],
                         "value": str(value)[:40]})
            handles.append(el)
        return rows, handles, self.locator.window_title(self.hwnd)

    def host(self) -> str:
        return ""

    # -- action -------------------------------------------------------------
    def act(self, op: str, el, typed: str) -> dict:
        act = self.actuator
        what = f"{op} {getattr(el, 'name', '')[:40]}".strip()
        if STOP_FILE.exists() or not self.arb.guard(what):
            return {"ok": False, "stop": True,
                    "error": "stopped — ESC+ESC, user input, or logs/STOP"}
        if op in ("click", "select"):
            r = act.click(el, prefer="invoke")
            return {"ok": r.ok, "did": f"click via {r.method}", "error": r.detail}
        if op == "type":
            act.focus_window(self.hwnd)
            try:
                el.control.SetFocus()
            except Exception:
                act.click(el, prefer="inject")
            time.sleep(0.25)
            act.send_keys("ctrl+a")
            r = act.type_text(typed)
            return {"ok": r.ok, "did": f"typed {typed!r}", "error": r.detail}
        if op == "submit":
            act.focus_window(self.hwnd)
            r = act.press_key("enter")
            return {"ok": r.ok, "did": "pressed Enter", "error": r.detail}
        if op in ("scroll_down", "scroll_up"):
            act.focus_window(self.hwnd)
            r = act.press_key("pagedown" if op == "scroll_down" else "pageup")
            return {"ok": r.ok, "did": op, "error": r.detail}
        if op == "wait":
            time.sleep(1.5)
            return {"ok": True, "did": "waited"}
        return {"ok": False, "error": f"no handler for {op}"}

    def where(self) -> str:
        return self.locator.window_title(self.hwnd)

    def close(self) -> None:
        # The browser is the user's: it stays open, on whatever page the run
        # ended at. Only our hold on the machine is released.
        try:
            self.arb.stop()
        finally:
            try:
                from core import overlay
                overlay.restore_system_cursor()
            except Exception:
                pass


def pursue(b, c, goal: str, typed: str, history: list) -> tuple[str, int, str]:
    """Drive one goal. Returns (status, steps, note).

    status: "done" | "guard" | "blocked" | "failed" | "stopped"
    """
    recent: list[tuple[str, str]] = []
    for step in range(1, MAX_STEPS_PER_GOAL + 1):
        if STOP_FILE.exists():
            return "stopped", step - 1, "logs/STOP present"

        rows, stamped, title = b.read()
        if not rows:
            return "failed", step - 1, "nothing readable on the page"

        t0 = time.perf_counter()
        m = fast.decide(goal, rows, window=f"{title} {b.host()}",
                        history=history, client=c)
        dt = (time.perf_counter() - t0) * 1000

        target = rows[m.index]["name"] if m.index is not None else ""
        print(f"    [{step}] {dt:4.0f}ms  {m.operation:<11} {target[:46]!r}"
              + (f"   blocker={m.blocker}" if m.blocker else ""), flush=True)

        if m.operation == "done":
            return "done", step, ""
        if not m.verdict.act or (m.index is None and m.needs_target):
            why = m.verdict.why
            print(f"        HELD: {why}", flush=True)
            return ("guard" if "guard" in why else "blocked"), step, why
        if m.operation == "blocked":
            return "blocked", step, m.blocker or "model reports blocked"

        handle = stamped[m.index] if m.index is not None else None
        if m.operation == "type" and not typed:
            return "failed", step, ("the plan supplied no text for a typing "
                                    "step — the decision model cannot write one")
        r = b.act(m.operation, handle, typed)

        if r.get("stop"):
            return "stopped", step, str(r.get("error"))[:120]
        if not r.get("ok"):
            print(f"        action failed: {r.get('error')}", flush=True)
            return "failed", step, str(r.get("error"))[:120]

        time.sleep(SETTLE.get(m.operation, 1.0))
        after, _, _ = b.read()
        # The typed text is part of what was done: "type Arama alanı" twice
        # is a loop, "type … = süt" then "type … = yumurta" is progress.
        label = f"{target} = {typed!r}" if m.operation == "type" else target
        entry = fast.did(m.operation, label, rows, after)
        print(f"        -> {r.get('did', 'ok')}   changed={entry['changed']}",
              flush=True)
        history.append(entry)

        recent.append((m.operation, target))
        if len(recent) >= 3 and len(set(recent[-3:])) == 1:
            return "failed", step, "same move three times"
        noops = [h for h in history[-2:] if not h["changed"]]
        if len(history) >= 2 and len(noops) == 2:
            return "blocked", step, "two actions running changed nothing"

    return "failed", MAX_STEPS_PER_GOAL, "step budget spent"


def main(argv: list[str]) -> int:
    if "--plan" not in argv:
        print(__doc__)
        return 1
    plan = json.loads(Path(argv[argv.index("--plan") + 1])
                      .read_text(encoding="utf-8"))
    headed = "--headed" in argv
    record = argv[argv.index("--record") + 1] if "--record" in argv else ""

    c = jev.Client(backend="openrouter", timeout=20.0)
    if c.offline:
        print("No OPENROUTER_API_KEY set.")
        return 1
    if STOP_FILE.exists():
        STOP_FILE.unlink()

    cast = None
    if record:
        from core.screencast import ScreenCast
        cast = ScreenCast(record, fps=15, scale=0.5)
        print(f"recording to {record}: {'ok' if cast.start() else cast.error}")

    c.prewarm()
    print(f"opening {plan['url']}", flush=True)
    try:
        if "--real-browser" in argv:
            # Read and send far more rows than the DOM path needs. Measured on
            # a Migros results page through UIA: the first product sits at row
            # 113, behind the header, the category tabs and the whole filter
            # sidebar. A 60-row state simply has no products in it, and the
            # model answers "none" — correctly, about a table that was cut
            # before the goods. 230 rows is ~16k tokens, about $0.0007 a call.
            fast.MAX_ROWS = 230
            b = RealBrowser(plan["url"], plan.get("window_hint", "migros"))
            print("driving YOUR browser through UIA. ESC+ESC aborts; "
                  "hands off the mouse and keyboard while it runs.", flush=True)
        elif "--chrome-profile" in argv:
            prof = argv[argv.index("--chrome-profile") + 1]
            import subprocess
            running = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-Process chrome -ErrorAction SilentlyContinue).Count"],
                capture_output=True, text=True).stdout.strip()
            if running and running != "0":
                print(f"Chrome is running ({running} processes). A profile "
                      f"cannot be open twice — close Chrome completely "
                      f"(check the tray) and run this again.")
                if cast is not None:
                    cast.stop()
                return 1
            print(f"using your real Chrome profile: {prof}", flush=True)
            b = WebPage(plan["url"], True, profile=prof)
        else:
            b = WebPage(plan["url"], headed)
    except RuntimeError as e:
        print(f"could not start: {e}")
        if cast is not None:
            cast.stop()
        return 1

    t_run = time.perf_counter()
    results = []
    try:
        failed_running = 0
        for ti, task in enumerate(plan["tasks"], 1):
            print(f"\n{'=' * 72}\nTASK {ti}/{len(plan['tasks'])}: "
                  f"{task['name']}\n{'=' * 72}", flush=True)
            history: list = []
            t0, calls0 = time.perf_counter(), c.stats.calls
            status, note = "done", ""
            for g in task["goals"]:
                print(f"  goal: {g['goal']}", flush=True)
                status, _, note = pursue(b, c, g["goal"], g.get("type", ""),
                                         history)
                if status != "done":
                    break
            results.append({"task": task["name"], "status": status,
                            "note": note, "s": time.perf_counter() - t0,
                            "calls": c.stats.calls - calls0})
            print(f"  => {status.upper()}" + (f" — {note}" if note else ""),
                  flush=True)
            if status == "stopped":
                break
            failed_running = 0 if status in ("done", "guard") else failed_running + 1
            if failed_running >= 2:
                print("\ntwo tasks running could not be completed — this is "
                      "the site, not the products. Stopping to hand back.",
                      flush=True)
                break
        print(f"\nfinal page: {b.where()[:110]}")
    finally:
        time.sleep(2.0)
        b.close()
        if cast is not None:
            info = cast.stop()
            print(f"recording: {info.get('path')}  {info.get('seconds', 0):.0f}s  "
                  f"{info.get('frames')} frames  "
                  f"{(info.get('bytes') or 0) / 1e6:.1f} MB")

    wall = time.perf_counter() - t_run
    print(f"\n{'=' * 72}\nSUMMARY\n{'=' * 72}")
    for r in results:
        print(f"  {r['status']:<8} {r['task'][:34]:<34} {r['calls']:>2} calls "
              f"{r['s']:5.0f}s  {r['note'][:50]}")
    ok = sum(1 for r in results if r["status"] == "done")
    print(f"\n  {ok}/{len(plan['tasks'])} tasks done, "
          f"{sum(1 for r in results if r['status'] == 'guard')} held by guard")
    print(f"  wall {wall:.0f}s   |   {c.stats.report()}")
    print("  Claude turns spent inside the run: 0 "
          "(one planning call before it)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
