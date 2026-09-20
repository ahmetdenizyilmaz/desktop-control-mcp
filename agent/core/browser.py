"""Browser control — read the page, never the pixels.

The desktop half of this system had to fight for accuracy because Windows
apps vary in what they publish. The web does not have that problem: every page
exposes its own structure, so element identity is free and exact.

That makes web automation both faster and inherently portable — a selector
built from a role and an accessible name survives redesigns, zoom levels,
window sizes and other machines, because it never referred to a position.

Headless by default because it is faster and does not steal your screen.
Headed automatically when a site blocks headless — which Google and Cloudflare
routinely do on login flows, so the fallback is a normal path, not an edge case.
When it is headed it is maximised, so the page gets the whole screen.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from typing import Any

PROFILE_DIR = paths.data("browser_profile")

#: Sites known to reject headless. Learned at runtime and persisted, so the
#: second attempt at a site never repeats the first one's failure.
HEADED_HINTS = {"accounts.google.com", "colab.research.google.com",
                "login.microsoftonline.com", "challenges.cloudflare.com"}

_BOT_WALL = re.compile(
    r"(unusual traffic|verify you are human|enable javascript and cookies|"
    r"couldn't sign you in|browser or app may not be secure|checking your browser)",
    re.I)


@dataclass
class PageElement:
    role: str
    name: str
    tag: str = ""
    testid: str = ""
    text: str = ""
    enabled: bool = True

    def locators(self) -> list[dict]:
        """Selector stack, most stable first — same principle as the desktop."""
        out = []
        if self.testid:
            out.append({"type": "web_testid", "value": {"value": self.testid}})
        if self.role and self.name:
            out.append({"type": "web_role",
                        "value": {"role": self.role, "name": self.name}})
        if self.name:
            out.append({"type": "web_text", "value": {"value": self.name}})
        return out


class Browser:
    """A persistent, logged-in browser the agent drives.

    Uses one persistent profile so a site logged into once stays logged in.
    It deliberately does NOT attach to the user's running Chrome: Chrome locks
    its profile directory, and sharing it would mean the agent acting inside
    the user's live session with their cookies.
    """

    def __init__(self, headless: bool = True, profile_dir: Path | None = None):
        self.headless = headless
        self.profile_dir = Path(profile_dir or PROFILE_DIR)
        self._pw = None
        self._ctx = None
        self._page = None
        self._lock = threading.Lock()
        self.last_error: str = ""
        self.downloads_dir = self.profile_dir.parent / "downloads"
        self._downloads: list[dict] = []
        self._pending = False
        self._recording = False
        self._web_events: list[dict] = []
        self._sites = self._load_sites()
        self._flows = self._load_flows()
        self._snap: dict = {}
        # Built-in hints seed the learned set; experience adds to it.
        self.headed_sites: set[str] = set(HEADED_HINTS) | {
            h for h, s in self._sites.items() if s.get("needs_headed")}

    # -- learned per-site behaviour --------------------------------------
    @property
    def _sites_file(self) -> Path:
        return self.profile_dir.parent / "browser_sites.json"

    def _load_sites(self) -> dict:
        try:
            return json.loads(self._sites_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_sites(self) -> None:
        try:
            self._sites_file.parent.mkdir(parents=True, exist_ok=True)
            self._sites_file.write_text(json.dumps(self._sites, indent=2),
                                        encoding="utf-8")
        except Exception:
            pass

    def learn(self, host: str, needs_headed: bool | None = None,
              logged_in: bool | None = None) -> None:
        """Remember how a site behaves, so the next run does not rediscover it.

        Without this, every session pays the cost of trying headless, hitting a
        bot wall, tearing the browser down and relaunching visible — for the
        same handful of sites, forever.
        """
        if not host:
            return
        s = self._sites.setdefault(host, {})
        if needs_headed is not None:
            s["needs_headed"] = needs_headed
            if needs_headed:
                self.headed_sites.add(host)
        if logged_in is not None:
            s["logged_in"] = logged_in
        s["last_seen"] = time.time()
        self._save_sites()

    def knows(self, host: str) -> dict:
        return dict(self._sites.get(host, {}))

    # -- lifecycle -------------------------------------------------------
    #: The page size a headless browser renders at. There is no window to
    #: fill, so the page has to be told how big it is.
    HEADLESS_VIEWPORT = {"width": 1440, "height": 900}

    def launch_options(self, headless: bool | None = None) -> dict:
        """What Chromium is launched with. Kept apart from start() so it can
        be checked without opening a browser.

        A visible browser opens MAXIMISED. In a small window less of the page
        is on screen — content below the fold, controls pushed off the edge,
        menus that a site folds into an icon at narrow widths — and none of
        it can be found or clicked until someone scrolls or resizes.
        Chromium honours --start-maximized, but a fixed viewport would then
        pin the page to 1440x900 inside the big window, so the viewport is
        left to the window instead. Headless keeps the fixed size: there is
        no window for the page to fill.
        """
        headless = self.headless if headless is None else headless
        args = ["--disable-blink-features=AutomationControlled"]
        opts: dict = {"user_data_dir": str(self.profile_dir),
                      "headless": headless, "args": args,
                      "accept_downloads": True}
        if headless:
            opts["viewport"] = dict(self.HEADLESS_VIEWPORT)
        else:
            args.append("--start-maximized")
            opts["no_viewport"] = True
        return opts

    def start(self, headless: bool | None = None) -> bool:
        from playwright.sync_api import sync_playwright
        with self._lock:
            if self._ctx is not None:
                return True
            if headless is not None:
                self.headless = headless
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            self.downloads_dir.mkdir(parents=True, exist_ok=True)
            self._pw = sync_playwright().start()
            self._ctx = self._pw.chromium.launch_persistent_context(
                **self.launch_options())
            self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
            # A click that opens a new tab must not leave the agent driving the
            # old one. Following the popup is almost always what was meant.
            self._ctx.on("page", self._on_new_page)
            return True

    def _on_new_page(self, page) -> None:
        # A popup starts life at about:blank and navigates a moment later.
        # Switching immediately is right; *acting* immediately is not, so the
        # wait is deferred to _ensure() rather than done in the event handler
        # (blocking inside a sync-API handler deadlocks the driver).
        self._page = page
        self._pending = True

    def _record_download(self, dl) -> dict:
        """Save a download under our own folder with its suggested name.

        Playwright discards a download when the page closes unless it is saved
        explicitly, so this is not optional bookkeeping.
        """
        name = dl.suggested_filename or "download"
        dest = self.downloads_dir / name
        n = 1
        while dest.exists():
            dest = self.downloads_dir / f"{Path(name).stem} ({n}){Path(name).suffix}"
            n += 1
        try:
            dl.save_as(str(dest))
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        rec = {"ok": True, "path": str(dest), "name": dest.name,
               "bytes": dest.stat().st_size if dest.exists() else 0,
               "url": dl.url, "at": time.time()}
        self._downloads.append(rec)
        return rec

    # -- recording what the user does in the browser ----------------------
    #: Injected into every page and frame. The low-level input hook that
    #: records desktop demonstrations sees a click on the browser window and
    #: nothing more — it cannot know which DOM element was under the cursor,
    #: so a demonstrated web flow recorded nothing usable. This listens where
    #: that information actually exists.
    RECORD_JS = r"""() => {
      if (window.__daRec) return;
      window.__daRec = true;

      const sel = (el) => {
        if (!el || el === document.body) return '';
        const t = el.getAttribute('data-testid') || el.getAttribute('data-test');
        if (t) return '[data-testid="' + t + '"]';
        if (el.id) return '#' + CSS.escape(el.id);
        const n = el.getAttribute('name');
        if (n) return el.tagName.toLowerCase() + '[name="' + n + '"]';
        const al = el.getAttribute('aria-label');
        if (al) return el.tagName.toLowerCase() + '[aria-label="' + al + '"]';
        // Fall back to a positional path. Least durable, recorded anyway so
        // the step exists and can be repaired, rather than silently lost.
        let path = '', node = el;
        while (node && node.nodeType === 1 && node !== document.body) {
          const p = node.parentNode;
          const i = p ? Array.prototype.indexOf.call(p.children, node) + 1 : 1;
          path = '>' + node.tagName.toLowerCase() + ':nth-child(' + i + ')' + path;
          node = p;
        }
        return path.slice(1);
      };

      const label = (el) => (
        el.getAttribute('aria-label') || el.innerText || el.placeholder
        || el.getAttribute('name') || el.title || '').replace(/\s+/g, ' ')
        .trim().slice(0, 60);

      const send = (ev) => {
        try { window.__daPush(ev); } catch (e) {}
      };

      // `change` fires on BLUR, not while typing, so a field filled early but
      // left late lands after everything that happened in between. Stamp the
      // moment typing STARTED and report that, so sorting by time reproduces
      // the order the user actually worked in.
      const started = new Map();
      document.addEventListener('input', (e) => {
        const el = e.target;
        if (!el || !el.tagName) return;
        const k = sel(el);
        if (k && !started.has(k)) started.set(k, Date.now());
      }, true);

      document.addEventListener('click', (e) => {
        const el = e.target.closest(
          'a,button,input,select,textarea,summary,[role],[onclick]') || e.target;
        if (!el || el.tagName === 'HTML') return;
        const type = (el.getAttribute('type') || '').toLowerCase();
        if (el.tagName === 'INPUT' && (type === 'checkbox' || type === 'radio')) {
          send({kind: el.checked ? 'check' : 'uncheck', selector: sel(el),
                label: label(el), url: location.href, t: Date.now()});
          return;
        }
        send({kind: 'click', selector: sel(el), label: label(el),
              url: location.href, t: Date.now()});
      }, true);

      document.addEventListener('change', (e) => {
        const el = e.target;
        if (!el || !el.tagName) return;
        const type = (el.getAttribute('type') || '').toLowerCase();
        if (type === 'checkbox' || type === 'radio') return;   // handled above
        if (el.tagName === 'SELECT') {
          send({kind: 'select', selector: sel(el), label: label(el),
                value: el.options[el.selectedIndex]?.text || el.value,
                url: location.href, t: Date.now()});
          return;
        }
        if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
          // A password is NEVER captured. Recording one would write it into a
          // job file that is meant to be shareable, in plain text, and the
          // user would have no idea it was there.
          const secret = (type === 'password');
          const k = sel(el);
          send({kind: 'fill', selector: k, label: label(el),
                value: secret ? '' : String(el.value || '').slice(0, 200),
                secret: secret, url: location.href,
                t: started.get(k) || Date.now()});
          started.delete(k);
        }
      }, true);
    }"""

    def start_recording(self) -> dict:
        """Watch what the user does in this browser, by DOM identity."""
        self._ensure()
        self._web_events: list[dict] = []
        if getattr(self, "_recording", False):
            return {"ok": True, "note": "already recording"}
        try:
            self._ctx.expose_binding(
                "__daPush", lambda source, ev: self._web_events.append(ev))
        except Exception:
            pass                     # already exposed from an earlier session
        try:
            # An arrow function cannot be invoked without wrapping parens:
            # "() => {...}()" is a syntax error, and a broken init script fails
            # silently — the listeners simply never attach and the recording
            # comes back empty with no error anywhere.
            self._ctx.add_init_script("(" + self.RECORD_JS + ")()")
            # Pages already open predate the init script, so inject there too.
            # evaluate() invokes a function expression itself, no parens needed.
            for pg in self._ctx.pages:
                try:
                    pg.evaluate(self.RECORD_JS)
                except Exception:
                    continue
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        self._recording = True
        return {"ok": True}

    def stop_recording(self) -> list[dict]:
        self._recording = False
        evs = list(getattr(self, "_web_events", []))
        # Fills report when typing began rather than when focus left, so a
        # sort by time is now the order the user actually worked in.
        return sorted(evs, key=lambda e: e.get("t", 0))

    def recorded(self) -> list[dict]:
        return list(getattr(self, "_web_events", []))

    def stop(self) -> None:
        with self._lock:
            try:
                if self._ctx:
                    self._ctx.close()
                if self._pw:
                    self._pw.stop()
            except Exception:
                pass
            self._ctx = self._pw = self._page = None

    @property
    def running(self) -> bool:
        return self._ctx is not None

    def _ensure(self):
        if self._ctx is None:
            self.start()
        # A popup can open and be closed again immediately — Chromium does this
        # to blocked `target=_blank` navigations. Following it would leave the
        # agent holding a dead page, and every later action would fail with a
        # closed-page error that says nothing about the cause.
        if self._page is None or self._page.is_closed():
            live = [p for p in self._ctx.pages if not p.is_closed()]
            self._page = live[-1] if live else self._ctx.new_page()
            self._pending = False
        if self._pending:
            self._pending = False
            try:
                self._page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass
        return self._page

    # -- navigation ------------------------------------------------------
    def goto(self, url: str, wait: str = "domcontentloaded") -> dict:
        # Add a scheme only when there is none at all. Testing for "http"
        # specifically mangles every other scheme — file://, about:, data: —
        # into https://file///... which then fails DNS.
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url):
            url = "https://" + url

        m = re.match(r"^https?://([^/]+)", url)
        host = m.group(1) if m else ""
        if any(h in host for h in self.headed_sites) and self.headless:
            # Known to reject headless — start visible rather than fail first.
            self.stop()
            self.start(headless=False)

        page = self._ensure()
        page.goto(url, wait_until=wait, timeout=45000)

        body = ""
        try:
            body = page.inner_text("body")[:4000]
        except Exception:
            pass

        if _BOT_WALL.search(body) and self.headless:
            # Retry visibly. This is expected on login flows, not an error.
            self.stop()
            self.start(headless=False)
            self.learn(host, needs_headed=True)     # persisted, not just in memory
            page = self._ensure()
            page.goto(url, wait_until=wait, timeout=45000)
        elif host and self.headless:
            # Headless worked here. Recording the success is what lets a site
            # that once needed a visible window go back to being fast.
            self.learn(host, needs_headed=False)

        return {"url": page.url, "title": page.title(),
                "headless": self.headless,
                "known": self.knows(host),
                "note": ("switched to a visible browser — this site blocks headless"
                         if not self.headless else "")}

    # -- perception ------------------------------------------------------
    def elements(self, limit: int = 60) -> list[PageElement]:
        """Interactive elements, by role and accessible name."""
        page = self._ensure()
        js = """() => {
          const sel = 'a,button,input,select,textarea,[role=button],[role=link],'
            + '[role=tab],[role=checkbox],[role=menuitem],[contenteditable=true]';
          const out = [];
          for (const el of document.querySelectorAll(sel)) {
            const r = el.getBoundingClientRect();
            if (r.width < 1 || r.height < 1) continue;
            const cs = getComputedStyle(el);
            if (cs.visibility === 'hidden' || cs.display === 'none') continue;
            const name = (el.getAttribute('aria-label') || el.innerText
                          || el.value || el.placeholder || el.title || '').trim();
            out.push({
              role: el.getAttribute('role') || el.tagName.toLowerCase(),
              name: name.slice(0, 80),
              tag: el.tagName.toLowerCase(),
              testid: el.getAttribute('data-testid') || el.getAttribute('data-test') || '',
              text: (el.innerText || '').trim().slice(0, 60),
              enabled: !el.disabled
            });
          }
          return out;
        }"""
        try:
            raw = page.evaluate(js)
        except Exception as e:
            self.last_error = str(e)
            return []
        return [PageElement(**d) for d in raw[:limit]]

    # -- indexed snapshot -------------------------------------------------
    #: Stamped onto every interactive element so an index resolves to exactly
    #: one node. Re-stamped on every snapshot, because a page that re-renders
    #: throws the old ones away.
    IDX_ATTR = "data-da-idx"

    SNAPSHOT_JS = r"""(maxN) => {
      const SEL = 'a,button,input,select,textarea,summary,'
        + '[role=button],[role=link],[role=tab],[role=checkbox],[role=radio],'
        + '[role=menuitem],[role=option],[role=switch],[contenteditable=true],'
        + '[onclick]';
      document.querySelectorAll('[data-da-idx]').forEach(e =>
        e.removeAttribute('data-da-idx'));
      const vh = innerHeight, vw = innerWidth;
      const out = [];
      let i = 0;
      for (const el of document.querySelectorAll(SEL)) {
        if (i >= maxN) break;
        const r = el.getBoundingClientRect();
        if (r.width < 2 || r.height < 2) continue;
        const cs = getComputedStyle(el);
        if (cs.visibility === 'hidden' || cs.display === 'none'
            || cs.opacity === '0') continue;
        const tag = el.tagName.toLowerCase();
        const name = (el.getAttribute('aria-label') || el.innerText
                      || el.placeholder || el.title
                      || el.getAttribute('name') || el.value || '')
                     .replace(/\s+/g, ' ').trim().slice(0, 70);
        el.setAttribute('data-da-idx', String(i));
        out.push({
          i: i,
          tag: tag,
          type: (el.getAttribute('type') || '').toLowerCase(),
          role: el.getAttribute('role') || '',
          name: name,
          value: (tag === 'select' || tag === 'input' || tag === 'textarea')
                 ? String(el.value || '').slice(0, 40) : '',
          checked: !!el.checked,
          disabled: !!el.disabled,
          testid: el.getAttribute('data-testid') || el.getAttribute('data-test') || '',
          inview: r.top < vh && r.bottom > 0 && r.left < vw && r.right > 0,
          options: tag === 'select'
            ? Array.from(el.options).slice(0, 8).map(o => o.text.trim()) : []
        });
        i++;
      }
      return {elements: out, title: document.title, url: location.href,
              scrollY: Math.round(scrollY),
              scrollMax: Math.max(0, document.body.scrollHeight - innerHeight)};
    }"""

    def snapshot(self, limit: int = 120) -> dict:
        """Index every interactive element, stamping the DOM so it stays exact.

        This is the difference between "click the thing called Submit" — which
        has to be searched for, ambiguously, on every single action — and
        "click 12", which is one attribute selector.
        """
        page = self._ensure()
        try:
            data = page.evaluate(self.SNAPSHOT_JS, limit)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            return {"elements": [], "title": "", "url": "",
                    "error": self.last_error}
        self._snap = data
        return data

    def render(self, snap: dict | None = None, only_inview: bool = False) -> str:
        """The page as a model should see it: one short line per element.

        Deliberately not JSON. The same page renders several times smaller this
        way, and every token saved here is latency on every single turn.
        """
        snap = snap if snap is not None else getattr(self, "_snap", None)
        if not snap:
            snap = self.snapshot()
        els = snap.get("elements", [])
        if only_inview:
            els = [e for e in els if e.get("inview")]
        lines = []
        for e in els:
            bits = []
            if e.get("type"):
                bits.append(e["type"])
            if e.get("value"):
                bits.append('value="%s"' % e["value"])
            if e.get("checked"):
                bits.append("checked")
            if e.get("disabled"):
                bits.append("disabled")
            if e.get("options"):
                bits.append("options=" + "|".join(e["options"]))
            attrs = (" " + " ".join(bits)) if bits else ""
            lines.append("[%d]<%s%s>%s" % (e["i"], e["tag"], attrs,
                                           e.get("name", "")))
        head = "%s — %s" % (snap.get("title", ""), snap.get("url", ""))
        tail = ""
        if snap.get("scrollMax"):
            tail = "\n(scrolled %s of %spx — scroll for more)" % (
                snap.get("scrollY", 0), snap["scrollMax"])
        return head + "\n" + "\n".join(lines) + tail

    def _by_index(self, idx: int):
        """Locator for a stamped index, or None if the page has moved on."""
        page = self._ensure()
        try:
            loc = page.locator('[%s="%d"]' % (self.IDX_ATTR, int(idx)))
            return loc if loc.count() > 0 else None
        except Exception:
            return None

    # -- remembered selectors --------------------------------------------
    @property
    def _flows_file(self) -> Path:
        return self.profile_dir.parent / "browser_flows.json"

    def _load_flows(self) -> dict:
        try:
            return json.loads(self._flows_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_flows(self) -> None:
        try:
            self._flows_file.parent.mkdir(parents=True, exist_ok=True)
            self._flows_file.write_text(json.dumps(self._flows, indent=2),
                                        encoding="utf-8")
        except Exception:
            pass

    def _host(self) -> str:
        """The bucket a remembered selector belongs to.

        Not just http(s): a file:// page has no netloc, and keying everything
        local under one empty string would make every local page share — and
        corrupt — one another's cache.
        """
        try:
            from urllib.parse import urlsplit
            u = urlsplit(self._page.url or "")
            return u.netloc or u.path.rsplit("/", 1)[-1] or u.scheme
        except Exception:
            return ""

    def remember(self, label: str, selector: str, ok: bool = True) -> None:
        """Record which selector actually worked for a named target here.

        This is what makes the second run of a task cheap. The first time,
        finding "Place order" means racing four Playwright strategies; every
        time after, it is one CSS query — provided the site has not changed,
        which is why misses are counted and a selector that stops working is
        dropped rather than retried forever.
        """
        host = self._host()
        if not host or not label:
            return
        site = self._flows.setdefault(host, {})
        entry = site.setdefault(label, {"selector": selector, "hits": 0,
                                        "misses": 0})
        if ok:
            entry["selector"] = selector
            entry["hits"] = entry.get("hits", 0) + 1
            entry["misses"] = 0
        else:
            entry["misses"] = entry.get("misses", 0) + 1
            if entry["misses"] >= 2:
                # Twice wrong is not a fluke. Forget it and let the slow path
                # rediscover the truth, rather than preferring a stale answer.
                site.pop(label, None)
        self._save_flows()

    def recall(self, label: str) -> str:
        return (self._flows.get(self._host(), {})
                .get(label, {}).get("selector", ""))

    def _selector_for(self, el) -> str:
        """A durable selector for something we just acted on successfully.

        Prefers what will still be true tomorrow: a test id, then an id, then
        a name attribute. Falls back to the index stamp, which is only valid
        until the next snapshot — recorded so the fast path can at least try.
        """
        try:
            return el.evaluate(
                """e => {
                    const t = e.getAttribute('data-testid')
                           || e.getAttribute('data-test');
                    if (t) return '[data-testid="' + t + '"]';
                    if (e.id) return '#' + CSS.escape(e.id);
                    const n = e.getAttribute('name');
                    if (n) return e.tagName.toLowerCase()
                                  + '[name="' + n + '"]';
                    return '';
                }""")
        except Exception:
            return ""

    # -- one action, by index or by name ----------------------------------
    def act(self, step: dict) -> dict:
        """Perform a single step. The unit that `run` batches.

        Targets resolve in the order that costs least: a remembered selector,
        then a stamped index, then a search by name. Each rung is slower and
        less certain than the one above it.
        """
        page = self._ensure()
        do = (step.get("do") or "").lower()
        target = step.get("target")
        value = step.get("value", "")
        label = str(step.get("label") or target or "")

        if do in ("goto", "open"):
            r = self.goto(str(value or target))
            return {"ok": True, "did": f"opened {r.get('url', '')}"}
        if do in ("wait", "wait_for"):
            r = self.wait_for(text=str(value or ""), timeout_s=step.get("timeout", 10))
            return {"ok": r.get("ok", False), "did": f"waited for {value!r}",
                    "error": r.get("error", "")}
        if do == "scroll":
            page.mouse.wheel(0, int(value or 600))
            return {"ok": True, "did": f"scrolled {value or 600}px"}
        if do in ("press", "key"):
            page.keyboard.press(str(value))
            return {"ok": True, "did": f"pressed {value}"}
        if do in ("read", "extract"):
            return {"ok": True, "did": "read the page", "text": self.text(4000)}

        # -- everything below needs an element -------------------------
        # Validate the verb BEFORE resolving a target: reporting "no element
        # for 0" when the real problem is a misspelled action sends the caller
        # looking in entirely the wrong place.
        ELEMENT_ACTIONS = ("click", "fill", "type", "select", "check",
                           "uncheck", "hover")
        if do not in ELEMENT_ACTIONS:
            return {"ok": False, "did": "",
                    "error": f"unknown action {do!r}",
                    "hint": "one of: goto, click, fill, select, check, uncheck, "
                            "hover, press, scroll, wait, read"}

        loc, how = None, ""
        remembered = self.recall(label) if label else ""
        if remembered:
            try:
                cand = page.locator(remembered)
                if cand.count() > 0:
                    loc, how = cand.first, "remembered"
            except Exception:
                loc = None
        if loc is None and isinstance(target, int):
            loc = self._by_index(target)
            how = "index"
        if loc is None and isinstance(target, str) and target:
            # A recorded step carries a CSS selector, not a human-readable
            # name. Searching for "#who" by label or role finds nothing, and
            # the step fails with "no element" while the element is right
            # there — so recognise a selector and use it as one.
            if target[0] in "#.[" or ">" in target or target.startswith("//"):
                try:
                    cand = page.locator(target)
                    if cand.count() > 0:
                        loc, how = cand.first, "selector"
                except Exception:
                    loc = None
        if loc is None and isinstance(target, str) and target:
            for strategy, cand in (("testid", page.get_by_test_id(target)),
                                   ("label", page.get_by_label(target, exact=False)),
                                   ("role", page.get_by_role("button", name=target,
                                                             exact=False)),
                                   ("text", page.get_by_text(target, exact=False))):
                try:
                    if cand.count() > 0:
                        loc, how = cand.first, strategy
                        break
                except Exception:
                    continue
        if loc is None:
            if remembered:
                self.remember(label, remembered, ok=False)
            return {"ok": False, "did": "", "error": f"no element for {target!r}",
                    "hint": "call snapshot again — the page has changed"}

        try:
            if do == "click":
                loc.click(timeout=8000)
            elif do in ("fill", "type"):
                loc.fill(str(value), timeout=8000)
            elif do == "select":
                loc.select_option(label=str(value), timeout=8000)
            elif do == "check":
                loc.check(timeout=8000)
            elif do == "uncheck":
                loc.uncheck(timeout=8000)
            elif do == "hover":
                loc.hover(timeout=8000)
        except Exception as e:
            if how == "remembered":
                self.remember(label, remembered, ok=False)
            # Playwright's exception class is just "Error"; the useful part
            # is the first line of the message.
            first = str(e).strip().splitlines()[0][:110] if str(e).strip() else ""
            return {"ok": False, "did": "",
                    "error": f"{do} failed: {first or type(e).__name__}"}

        if label and how != "remembered":
            sel = self._selector_for(loc)
            if sel:
                self.remember(label, sel, ok=True)
        elif how == "remembered":
            self.remember(label, remembered, ok=True)
        return {"ok": True, "did": f"{do} {target!r} via {how}"}

    def run(self, steps: list[dict], stop_on_error: bool = True) -> dict:
        """Do several things in ONE call.

        This is the single biggest speed win available. The dominant cost is
        not the click, it is the model round trip around it — seconds each. A
        six-field form is six round trips as separate tool calls and one as a
        batch. On failure it stops and hands back a fresh page snapshot, so
        recovery costs one more turn rather than another six.
        """
        results, t0 = [], time.time()
        for n, step in enumerate(steps or [], 1):
            r = self.act(step)
            r["step"] = n
            results.append(r)
            if not r.get("ok") and stop_on_error:
                return {"ok": False, "done": n - 1, "of": len(steps),
                        "failed_at": n, "steps": results,
                        "elapsed_s": round(time.time() - t0, 2),
                        "page": self.render(self.snapshot())}
        # A batch that ends in a click or Enter usually leaves a navigation in
        # flight. Reporting the old URL makes the caller think nothing
        # happened and spend another round trip checking — which is exactly
        # the cost this method exists to remove.
        try:
            self._page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        return {"ok": True, "done": len(results), "of": len(steps),
                "steps": results, "elapsed_s": round(time.time() - t0, 2),
                "url": self._page.url if self._page else "",
                "title": (self._page.title() if self._page else "")}

    def text(self, max_chars: int = 6000) -> str:
        page = self._ensure()
        try:
            return page.inner_text("body")[:max_chars]
        except Exception:
            return ""

    # -- action ----------------------------------------------------------
    def click(self, name: str = "", role: str = "", testid: str = "",
              nth: int = 0) -> dict:
        """Click by identity, never by position."""
        page = self._ensure()
        attempts = []
        if testid:
            attempts.append(("testid", page.get_by_test_id(testid)))
        if role and name:
            attempts.append(("role+name",
                             page.get_by_role(role, name=name, exact=False)))
        if name:
            attempts.append(("label", page.get_by_label(name, exact=False)))
            attempts.append(("text", page.get_by_text(name, exact=False)))

        for how, loc in attempts:
            try:
                target = loc.nth(nth)
                target.click(timeout=6000)
                return {"ok": True, "via": how, "url": page.url}
            except Exception as e:
                self.last_error = f"{how}: {type(e).__name__}"
                continue
        return {"ok": False, "error": f"no element matching {name or testid!r}",
                "hint": "call elements() to see the real roles and names"}

    def fill(self, name: str, value: str) -> dict:
        page = self._ensure()
        for how, loc in (("label", page.get_by_label(name, exact=False)),
                         ("placeholder", page.get_by_placeholder(name, exact=False)),
                         ("role", page.get_by_role("textbox", name=name))):
            try:
                loc.first.fill(value, timeout=6000)
                return {"ok": True, "via": how}
            except Exception:
                continue
        return {"ok": False, "error": f"no input matching {name!r}"}

    def press(self, key: str) -> dict:
        page = self._ensure()
        page.keyboard.press(key)
        return {"ok": True}

    def wait_for(self, text: str = "", url_regex: str = "",
                 timeout_s: float = 15.0) -> dict:
        page = self._ensure()
        try:
            if url_regex:
                page.wait_for_url(re.compile(url_regex), timeout=timeout_s * 1000)
            if text:
                page.get_by_text(text, exact=False).first.wait_for(
                    timeout=timeout_s * 1000)
            return {"ok": True, "url": page.url}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}", "url": page.url}

    # -- files -----------------------------------------------------------
    def download(self, name: str = "", role: str = "", testid: str = "",
                 timeout_s: float = 60.0) -> dict:
        """Click something that produces a file, and keep the file.

        A plain click() on a download link appears to succeed and silently
        loses the file — Playwright drops it unless the download event is
        awaited. That silent loss is why this is a separate method.
        """
        page = self._ensure()
        try:
            with page.expect_download(timeout=timeout_s * 1000) as info:
                r = self.click(name=name, role=role, testid=testid)
                if not r.get("ok"):
                    return r
            return self._record_download(info.value)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "hint": "no download started — the click may have navigated "
                            "instead; check with text() or elements()"}

    def download_url(self, url: str, timeout_s: float = 60.0) -> dict:
        """Fetch a file directly, reusing the browser's cookies.

        Needed whenever a file sits behind a login: a plain HTTP request has
        none of the session the browser is carrying.
        """
        page = self._ensure()
        try:
            with page.expect_download(timeout=timeout_s * 1000) as info:
                page.evaluate("u => { const a = document.createElement('a');"
                              "a.href = u; a.download = ''; document.body.appendChild(a);"
                              "a.click(); a.remove(); }", url)
            return self._record_download(info.value)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def downloads(self) -> list[dict]:
        return list(self._downloads)

    def upload(self, paths: str | list[str], name: str = "",
               testid: str = "") -> dict:
        """Attach a file to a file input, or to a button that opens one."""
        page = self._ensure()
        files = [paths] if isinstance(paths, str) else list(paths)
        missing = [p for p in files if not Path(p).exists()]
        if missing:
            return {"ok": False, "error": f"file not found: {missing[0]}"}

        # A real <input type=file> can be set directly — no dialog involved.
        try:
            loc = (page.get_by_test_id(testid) if testid
                   else page.locator("input[type=file]").first)
            loc.set_input_files(files, timeout=6000)
            return {"ok": True, "via": "input", "files": files}
        except Exception:
            pass
        # Otherwise the page uses a styled button that opens the OS dialog;
        # intercepting the chooser avoids driving a native file dialog.
        try:
            with page.expect_file_chooser(timeout=10000) as fc:
                r = self.click(name=name, testid=testid)
                if not r.get("ok"):
                    return r
            fc.value.set_files(files)
            return {"ok": True, "via": "chooser", "files": files}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "hint": "no file input found; call elements() to see the page"}

    # -- tabs ------------------------------------------------------------
    def tabs(self) -> list[dict]:
        if self._ctx is None:
            return []
        self._ensure()
        out = []
        for i, p in enumerate(self._ctx.pages):
            try:
                out.append({"index": i, "url": p.url, "title": p.title(),
                            "active": p is self._page})
            except Exception:
                out.append({"index": i, "url": "(closed)", "title": "",
                            "active": False})
        return out

    def new_tab(self, url: str = "") -> dict:
        self._ensure()
        self._page = self._ctx.new_page()
        if url:
            return self.goto(url)
        return {"ok": True, "tabs": len(self._ctx.pages)}

    def switch_tab(self, index: int) -> dict:
        if self._ctx is None or not (0 <= index < len(self._ctx.pages)):
            return {"ok": False, "error": f"no tab {index}",
                    "tabs": self.tabs()}
        self._page = self._ctx.pages[index]
        try:
            self._page.bring_to_front()
        except Exception:
            pass
        return {"ok": True, "url": self._page.url, "title": self._page.title()}

    def close_tab(self, index: int | None = None) -> dict:
        if self._ctx is None:
            return {"ok": False, "error": "browser not running"}
        pages = self._ctx.pages
        page = self._page if index is None else (
            pages[index] if 0 <= index < len(pages) else None)
        if page is None:
            return {"ok": False, "error": f"no tab {index}"}
        if len(pages) == 1:
            # Closing the last tab kills the context; keep a blank one instead
            # so the agent still has somewhere to navigate.
            page.goto("about:blank")
            return {"ok": True, "note": "last tab kept, blanked instead of closed"}
        page.close()
        self._page = self._ctx.pages[-1]
        return {"ok": True, "tabs": len(self._ctx.pages), "url": self._page.url}

    # -- session ---------------------------------------------------------
    def signed_in(self, host: str = "") -> dict:
        """Which sites the persistent profile is carrying a session for.

        Cookie presence is evidence, not proof — a session can be expired
        server-side. It is reported as "has a session", never as "logged in",
        because acting on the stronger claim is how a task ends up filling a
        form nobody is authenticated for.
        """
        if self._ctx is None:
            return {"running": False, "hosts": [],
                    "note": "browser not started; nothing to report"}
        try:
            cookies = self._ctx.cookies()
        except Exception as e:
            return {"running": True, "error": f"{type(e).__name__}: {e}"}
        SESSIONISH = ("sid", "session", "auth", "login", "token", "sso", "secure")
        hosts: dict[str, int] = {}
        for c in cookies:
            d = (c.get("domain") or "").lstrip(".")
            if any(k in (c.get("name") or "").lower() for k in SESSIONISH):
                hosts[d] = hosts.get(d, 0) + 1
        if host:
            n = sum(v for d, v in hosts.items() if host.endswith(d) or d.endswith(host))
            return {"running": True, "host": host, "has_session": n > 0,
                    "session_cookies": n}
        return {"running": True,
                "hosts": sorted(hosts, key=lambda d: -hosts[d])[:20],
                "note": "a stored session can still be expired; verify by loading the page"}

    def screenshot(self, path: str | None = None) -> str:
        page = self._ensure()
        p = path or str(paths.data("screenshots") /
                        "browser.png")
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=p, full_page=False)
        return p

    def status(self) -> dict:
        if self._ctx is not None:
            self._ensure()
        return {
            "running": self.running,
            "headless": self.headless,
            "url": (self._page.url if self._page else None),
            "title": (self._page.title() if self._page else None),
            "profile": str(self.profile_dir),
            "sites_needing_visible": sorted(self.headed_sites),
            "tabs": len(self._ctx.pages) if self._ctx else 0,
            "downloads": len(self._downloads),
            "downloads_dir": str(self.downloads_dir),
            "sites_learned": len(self._sites),
        }


class ThreadedBrowser:
    """Runs a Browser on a thread that has no event loop.

    Playwright's sync API refuses to start inside a running asyncio loop, and
    every tool handler in this app is a coroutine — so the browser worked
    perfectly in tests, which call it directly, and failed every single time it
    was reached through a tool. The agent's own diagnosis of a live run was
    "the MCP browser backend is broken (async-loop bug)", after which it
    installed Playwright itself and drove Chromium from Bash instead.

    The fix is not to rewrite the engine against the async API. It is to give
    the sync one what it needs: a thread of its own. Playwright objects are
    thread-affine, so ALL calls must land on that same thread — which is why
    this proxies every method rather than only the ones that happened to fail.
    """

    def __init__(self, *args, **kwargs):
        inner = Browser(*args, **kwargs)
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_q", queue.Queue())
        object.__setattr__(self, "_busy", False)
        t = threading.Thread(target=self._serve, name="browser", daemon=True)
        object.__setattr__(self, "_thread", t)
        t.start()

    @property
    def busy(self) -> bool:
        """Is the worker mid-call, or is there work waiting for it?

        Callers that must not block — the window's status poll, above all —
        ask this first. Everything here is marshalled onto one thread, so a
        cheap-looking call queues behind whatever long operation is running.
        """
        return bool(object.__getattribute__(self, "_busy")) or not self._q.empty()

    def _serve(self) -> None:
        while True:
            job = self._q.get()
            if job is None:
                return
            fn, args, kwargs, box, done = job
            object.__setattr__(self, "_busy", True)
            try:
                box.append(("ok", fn(*args, **kwargs)))
            except BaseException as e:            # noqa: BLE001 - re-raised below
                box.append(("err", e))
            finally:
                object.__setattr__(self, "_busy", False)
                done.set()

    def _call(self, fn, args, kwargs):
        # Already on the worker: call straight through, or we would deadlock
        # waiting for ourselves.
        if threading.current_thread() is self._thread:
            return fn(*args, **kwargs)
        box, done = [], threading.Event()
        self._q.put((fn, args, kwargs, box, done))
        if not done.wait(timeout=300):
            raise TimeoutError("browser call did not return within 300s")
        kind, value = box[0]
        if kind == "err":
            raise value
        return value

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr
        def proxy(*args, **kwargs):
            return self._call(attr, args, kwargs)
        proxy.__name__ = getattr(attr, "__name__", name)
        proxy.__doc__ = getattr(attr, "__doc__", None)
        return proxy

    def __setattr__(self, name, value):
        # `browser.headless = False` must reach the real object, not shadow it.
        setattr(self._inner, name, value)


#: shared instance the app and tools use. Threaded, because the tools that
#: reach it are coroutines and Playwright's sync API cannot start under one.
browser = ThreadedBrowser()

