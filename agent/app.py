"""AGI Agent — the standalone application.

One process, one window, no server:

    +-----------------------------------------+
    |  native window (pywebview)              |
    |    task box, live progress, settings    |
    +-----------------------------------------+
                    |  direct python calls
    +-----------------------------------------+
    |  Claude Agent SDK  (the brain)          |
    |    in-process tools (no MCP subprocess) |
    +-----------------------------------------+
    |  engine: desktop control + browser      |
    +-----------------------------------------+

You type a task. Claude decides the steps and drives the machine through the
tested engine. Saved jobs are an accelerator for repeated work, not the point.

Run:  python agent/app.py
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import webview
from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ResultMessage,
                              TextBlock, ThinkingBlock, ToolResultBlock,
                              ToolUseBlock, UserMessage, query)

# Before anything can spawn a child. A windowed build has no console, so
# Windows gives every console child a NEW visible window — the Claude CLI on
# each task, PowerShell, the browser driver. This silences all of them, and is
# a no-op when a console is present, so running from a terminal is unchanged.
from core import noconsole
noconsole.install()

import tools_sdk
from core import channel as chan
from core import paths
from core import proxy
from core.arbiter import Arbiter
from core.browser import browser
from core import machine_profile
from core.machine_profile import load_profile
from core.schedule import scheduler
from core.secrets import vault, VaultError
from core.store import Store
from core.watchdog import watchdog

#: The window title — and therefore what the window is found by when a task
#: needs to raise it, what the arbiter names as the owner of the machine, and
#: what a recording filters out as "the user reaching for the app itself".
#: core/induce.py carries the same string in CHROME_TITLES; keep them equal.
APP_TITLE = "AGI Agent"

#: Shipped inside the build (see build_exe.py), next to the UI.
ICON = paths.resource("assets", "icon.ico")

SETTINGS_FILE = paths.data("settings.json")
DEFAULTS = {
    "automation_level": "auto",         # supervised | auto
    "model": "",                        # blank = whatever Claude Code is set to
    # Used only if the main model cannot serve the request (overloaded, or the
    # account is out of capacity for it). Blank = no fallback, the task fails.
    "fallback_model": "",
    # "anthropic" (direct), "openrouter", or "custom" — any endpoint that
    # speaks the Anthropic wire format, which is what makes a LOCAL model
    # possible (via a translating proxy). See _provider_env.
    "provider": "anthropic",
    # Only for provider=custom, e.g. http://localhost:4000 for a LiteLLM proxy
    # in front of Ollama. Must serve /v1/messages, not OpenAI's endpoint.
    "base_url": "",
    # Cap on output tokens per reply. 0 = leave the CLI's own default (64000).
    # This matters on pay-as-you-go providers: OpenRouter reserves credit for
    # the FULL max_tokens up front, so a large default is refused outright on
    # a small balance ("requires more credits, or fewer max_tokens").
    "max_output_tokens": 0,
    # What each screenshot costs the model, as a visual-token budget: low
    # ~1000, standard ~1600, high ~4800 (the most a Claude 4.7+ model takes).
    # Never affects where a click lands — see tools_sdk.screenshot.
    "screenshot_detail": "standard",
    "watchdog_minutes": 60,
    "stall_minutes": 10,
    "browser_headless": True,
    "max_turns": 0,                     # 0 = no cap; a long task is not a stuck one
    # -- the script engine: Jev first, Claude second, the person third ----
    # Before any Claude turn, Jev checks the saved scripts for one that does
    # this task (core/engine.py). Off = every task goes straight to Claude.
    "scripts_first": True,
    # Which service answers Jev's decisions. Its key lives in the vault;
    # openrouter shares the key the Claude-via-OpenRouter option uses.
    "jev_backend": "openrouter",
    # How sure Jev must be that a saved script IS the task before running it.
    "jev_min_confidence": 0.6,
    # A stuck script step goes to Claude (with a screenshot) this many times
    # before the full Claude agent takes the rest of the task over.
    "rescue_tries": 2,
    "rescue_model": "opus",
}


def load_settings() -> dict:
    s = dict(DEFAULTS)
    if SETTINGS_FILE.exists():
        try:
            s.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return s


def save_settings(s: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")


def _blank_usage() -> dict:
    return {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0,
            "turns": 0, "cost": 0.0, "model": "", "models": {}}


#: List price per million tokens, as (input, output). A task is not billed at
#: one rate: output costs 5x input, a cache read a tenth of it, a cache write a
#: quarter above it. This file used to average all four into a single learned
#: number, which priced every task by the shape of the LAST one — and an
#: agent's shape swings hard between a three-turn answer and a sixty-turn
#: browser run that re-reads a cached prefix at every step.
#:
#: Images need no line of their own. The API turns a picture into visual tokens
#: (ceil(w/28) * ceil(h/28)) and bills them as ordinary input, so a screenshot
#: is already inside the input count — dense, but not separately priced.
PRICES = {
    "claude-fable-5-1":  (10.0, 50.0),
    "claude-mythos-5-1": (10.0, 50.0),
    "claude-fable-5":    (10.0, 50.0),
    "claude-opus-5":     (5.0, 25.0),
    "claude-opus-4-8":   (5.0, 25.0),
    "claude-opus-4-7":   (5.0, 25.0),
    "claude-opus-4-6":   (5.0, 25.0),
    "claude-sonnet-5":   (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5":  (1.0, 5.0),
    # Gemini, from ai.google.dev/gemini-api/docs/pricing, read 16 Sept 2026.
    # The 3.6-3.8 Flash figures are the promotional rate to 31 Dec 2026 and
    # double after it; the Pro tiers cost more than shown on prompts over
    # 200k. Their cache-read price is a tenth of input, like Claude's; a cache
    # WRITE on Gemini is billed as storage per hour instead, so the 1.25x
    # below slightly overstates the little of it the proxy ever reports.
    "gemini-3.8-flash":       (0.75, 3.75),
    "gemini-3.7-flash":       (0.75, 3.75),
    "gemini-3.6-flash":       (0.75, 3.75),
    "gemini-3.5-flash":       (1.50, 9.00),
    "gemini-3.5-flash-lite":  (0.30, 2.50),
    "gemini-3.1-flash-lite":  (0.25, 1.50),
    "gemini-3.1-pro-preview": (2.00, 12.00),
    "gemini-3-flash-preview": (0.50, 3.00),
    "gemini-2.5-pro":         (1.25, 10.00),
    "gemini-2.5-flash":       (0.30, 2.50),
    "gemini-2.5-flash-lite":  (0.10, 0.40),
}

#: Multipliers on the input price. Cache writes are quoted at the five-minute
#: TTL; a one-hour write costs 2.0x, and the SDK does not report which it made,
#: so the cheaper and far more common figure is the honest default.
CACHE_WRITE_MULT = 1.25
CACHE_READ_MULT = 0.1
#: ...except on the 5.1 pair, where a cache read is a fortieth of input.
CACHE_READ_MULT_5_1 = 0.025
#: Every Claude row above prices output at exactly 5x input; that ratio is
#: what the unpriced-model fallback below assumes. (Gemini's varies, 4-8x,
#: which is one more reason its rows are in the table rather than learned.)
OUTPUT_MULT = 5.0


def _price_key(model: str) -> str:
    """The table key for whatever the provider called itself.

    A proxy prefixes its vendor ("anthropic/claude-opus-5") and a pinned build
    suffixes a date ("claude-opus-5-20260401"); both are the same price.
    """
    m = (model or "").strip().lower().rsplit("/", 1)[-1]
    return re.sub(r"[-@]\d{6,8}$", "", m)


def unit_prices(model: str):
    """Dollars per token as (input, output, cache read, cache write), or None
    for a model the table does not know — a local one behind a proxy, say."""
    key = _price_key(model)
    row = PRICES.get(key)
    if not row:
        return None
    inp, out = row[0] / 1e6, row[1] / 1e6
    read = CACHE_READ_MULT_5_1 if key.endswith("-5-1") else CACHE_READ_MULT
    return inp, out, inp * read, inp * CACHE_WRITE_MULT


def cost_of(u: dict) -> float | None:
    """What this usage costs at list price, or None for an unpriced model."""
    p = unit_prices(u.get("model") or "")
    if not p:
        return None
    inp, out, read, write = p
    return (u["input"] * inp + u["output"] * out
            + u["cache_read"] * read + u["cache_creation"] * write)


#: For models the table cannot price, keep the old self-calibrating trick — but
#: calibrate the right thing. Weighting each class by its ratio to input
#: collapses a task to "input tokens equivalent", and THAT divides into a
#: reported cost to give a rate which holds whatever the next task's mix is.
RATES_FILE = paths.data("token_rates.json")


def _weighted_tokens(u: dict) -> float:
    return (u["input"] + u["output"] * OUTPUT_MULT
            + u["cache_read"] * CACHE_READ_MULT
            + u["cache_creation"] * CACHE_WRITE_MULT)


def load_rates() -> dict:
    try:
        return json.loads(RATES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def learn_rate(model: str, weighted: float, cost: float) -> None:
    if not model or weighted <= 0 or cost <= 0:
        return
    rates = load_rates()
    rate = cost / weighted
    # Smooth against the last figure so one odd task cannot skew the estimate.
    key = _price_key(model)
    prev = rates.get(key)
    rates[key] = rate if not prev else (prev * 0.7 + rate * 0.3)
    try:
        RATES_FILE.write_text(json.dumps(rates, indent=2), encoding="utf-8")
    except Exception:
        pass


def _usage_total(u: dict) -> int:
    return int(u["input"] + u["output"] + u["cache_read"] + u["cache_creation"])


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))


#: Where the OpenRouter key lives — in the DPAPI vault with every other
#: credential, never in settings.json, which is plain text on disk.
OPENROUTER_KEY = "openrouter-api-key"
OPENROUTER_URL = "https://openrouter.ai/api"
#: The Gemini key, same vault. It reaches the model by being placed in the
#: environment of the LiteLLM proxy the app starts (core/proxy.py) — the
#: proxy's config reads os.environ/GEMINI_API_KEY.
GEMINI_KEY = "gemini-api-key"


def _has_secret(name: str) -> bool:
    try:
        return vault.has(name)
    except Exception:
        return False


#: Where the app's own LiteLLM listens when nothing else is configured.
DEFAULT_PROXY_URL = "http://localhost:4000"


def _endpoint_url(settings: dict) -> str:
    """The custom endpoint in use, or '' when the provider has none.

    Gemini is reached through the local proxy too; the only differences from
    'custom' are that the URL defaults and the picker shows Gemini alone.
    """
    provider = settings.get("provider") or "anthropic"
    url = (settings.get("base_url") or "").strip().rstrip("/")
    if provider == "gemini":
        return url or DEFAULT_PROXY_URL
    if provider == "custom":
        return url
    return ""


def _provider_env(settings: dict) -> dict:
    """Environment that points the CLI at whichever provider is configured.

    Claude Code will talk to anything that speaks the Anthropic wire format,
    so OpenRouter needs only a base URL and its own key. ANTHROPIC_API_KEY is
    deliberately blanked: left set, the CLI falls back to authenticating
    against Anthropic directly and the two credentials conflict.

    Nothing is returned for the default provider, so the normal path is
    exactly as it was.
    """
    env = {}
    try:
        cap = int(settings.get("max_output_tokens") or 0)
    except (TypeError, ValueError):
        cap = 0
    if cap > 0:
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(cap)
    provider = settings.get("provider") or "anthropic"

    if provider in ("custom", "gemini"):
        url = _endpoint_url(settings)
        if not url:
            return env                  # nothing configured: stay on Anthropic
        # "localhost" is a trap here: the CLI is Node, which resolves it to
        # IPv6 ::1 first, while most local proxies (LiteLLM, Ollama) bind only
        # 0.0.0.0 — IPv4. The result is a flat "Connection refused" against a
        # server that is plainly running. Pin it to IPv4.
        url = url.replace("://localhost:", "://127.0.0.1:")
        # A local proxy usually wants no credential, but send one if saved so
        # the same path works for an authenticated gateway.
        try:
            key = vault.get(OPENROUTER_KEY)
        except Exception:
            key = ""
        env.update({"ANTHROPIC_BASE_URL": url,
                    "ANTHROPIC_AUTH_TOKEN": key or "local",
                    "ANTHROPIC_API_KEY": ""})
        return env

    if provider != "openrouter":
        return env
    try:
        key = vault.get(OPENROUTER_KEY)
    except Exception:
        return env                      # no key saved: stay on Anthropic
    if not key:
        return env
    env.update({"ANTHROPIC_BASE_URL": OPENROUTER_URL,
                "ANTHROPIC_AUTH_TOKEN": key,
                "ANTHROPIC_API_KEY": ""})
    return env


#: Tools that touch the machine — mouse, keyboard, screenshots, the UI tree.
#: The border goes RED while one of these runs.
_TOUCH_TOOLS = {
    "click", "type_text", "press_key", "do_steps", "mouse_path",
    "focus_window", "open_app", "screenshot", "find_controls", "read_labels",
    "list_windows", "sample_colors", "trace_edges", "copy_to_clipboard",
    "use_credential", "record_job", "script",
}
#: Tools that wait on a person. ORANGE.
_WAIT_TOOLS = {"ask_user", "wait_for_reply"}


def explain_provider_error(text: str) -> str:
    """Turn a raw provider error into something the user can act on, or ''.

    The SDK reports these as 'Claude Code returned an error result: success',
    which tells nobody anything. The useful part is in the message body.
    """
    t = (text or "").lower()
    if "402" in t or "requires more credits" in t:
        return ("OpenRouter refused the request on credit, not on anything "
                "wrong with the task. It reserves credit for the WHOLE output "
                "allowance up front, and Claude Code asks for 64000 tokens by "
                "default. Either add credit at "
                "https://openrouter.ai/settings/credits, or set Settings → "
                "'Longest reply' to something small (2000 is plenty for this "
                "app, which sends many short messages rather than a few long "
                "ones). Note a 'free' model still needs a credit balance.")
    if "401" in t and ("auth" in t or "api key" in t or "unauthorized" in t):
        return ("The provider rejected the API key. Re-enter it in Settings → "
                "Save key, and check the key is still valid at the provider.")
    if "429" in t or "rate limit" in t:
        return ("The provider is rate-limiting this key. Wait a moment, or set "
                "a Backup model in Settings so a busy model is not fatal.")
    if "404" in t and "model" in t:
        return ("That model id was not recognised by the provider. Pick one "
                "from the Settings dropdown rather than typing it by hand.")
    return ""


def _usage_line(u: dict) -> str:
    """A short 'tokens · screenshots · $cost' summary for a finished task."""
    total = _usage_total(u)
    if not total:
        return ""
    bit = f"{_fmt_tokens(total)} tokens"
    shots = tools_sdk.shot_stats()
    if shots.get("count"):
        bit += (f" · {shots['count']} screenshot(s), "
                f"~{_fmt_tokens(shots['tokens'])} visual tokens")
        if shots.get("unchanged"):
            bit += f" ({shots['unchanged']} skipped as unchanged)"
    if u.get("cost"):
        bit += f" · ${u['cost']:.2f}"
    return bit


#: The machine profile is built at most once per process, and never on the
#: window's thread. See Agent._machine_profile for why.
_profile_lock = threading.Lock()
_profile_thread: "threading.Thread | None" = None
_profile_built: "dict | None" = None


def _build_profile_in_background() -> None:
    global _profile_built
    try:
        _profile_built = load_profile()     # builds it, and caches it to disk
    except Exception:
        pass


class Agent:
    """The bridge the window calls into. Every method here is a UI action."""

    def __init__(self) -> None:
        self.settings = load_settings()
        self.store = Store()
        self.arbiter: Arbiter | None = None
        self.feed: list[dict] = []
        self.running = False
        self.current_task = ""
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._task_id = "task"
        #: Ever-increasing line number for the feed. The window is sent only
        #: the last 120 lines, so it cannot use the list's length to tell that
        #: something new arrived — past 120 lines the length never changes
        #: again and the display froze mid-task.
        self._seq = 0
        #: `running` is claimed here, before the worker thread starts, so two
        #: clicks (or a click and a scheduler tick) cannot both get past the
        #: check and open two sessions onto one desktop.
        self._start_lock = threading.Lock()

        self._last_browser: dict = {"running": False}
        self._current: dict | None = None       # tool running right now
        self._missed: list[dict] = []
        self._started_at: float = 0.0

        # A question the task is blocked on, waiting for this window to answer.
        self._question: dict | None = None
        self._answered = threading.Event()
        self._answer: "chan.Reply | None" = None
        chan.channel.app_ask = self._ask_in_app

        # The loop and task the SDK runs on, so Stop can reach the run from
        # the outside instead of waiting for the next message to notice it.
        # Set by _consume for the life of one run and cleared after.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._main_task: asyncio.Task | None = None
        self._streaming = False
        self._cancel_sent = False

        # Tokens (and cost) spent on the task in progress, shown live. The
        # per-message snapshots feeding it are keyed by API message, because
        # the SDK delivers one AssistantMessage per content block and they all
        # carry the same usage — summed naively, every turn counted twice.
        self._usage = _blank_usage()
        self._usage_msgs: dict[str, dict] = {}
        # The model most recently seen serving. Outlives the per-task usage
        # reset so the header keeps naming it between tasks.
        self._last_model = ""
        # The screen-wide "agent is working" border, and the background
        # question popup. Both created lazily so a headless import is cheap.
        self._border = None
        self._popup = None
        #: What the script engine is doing, for the window (core/engine.py).
        self._engine_status: dict = {"state": "idle"}
        #: Last result of each Settings "Test" button, shown until retested.
        self._key_tests: dict = {}

    # -- feed ------------------------------------------------------------
    def _say(self, kind: str, text: str, **extra) -> None:
        self._seq += 1
        self.feed.append({"t": time.time(), "seq": self._seq, "kind": kind,
                          "text": text, **extra})
        del self.feed[:-500]

    # -- token accounting ------------------------------------------------
    def _usage_view(self) -> dict:
        """Usage as the window should show it.

        Cost in order of authority: what the provider actually charged once the
        task ends, else this model's list price applied to the tokens spent so
        far, else a rate learned from past tasks on a model nothing can price.
        Only the first is exact, and the other two are flagged as estimates so
        the window can say which it is showing.
        """
        u = dict(self._usage)
        u["total"] = _usage_total(u)
        u["estimated"] = False
        shots = tools_sdk.shot_stats()
        u["shots"] = shots.get("count", 0)
        u["shot_tokens"] = shots.get("tokens", 0)
        u["shots_unchanged"] = shots.get("unchanged", 0)
        if not u["cost"] and u["total"]:
            priced = cost_of(u)
            if priced is None:
                rate = load_rates().get(_price_key(u.get("model") or ""))
                priced = _weighted_tokens(u) * rate if rate else 0.0
            if priced:
                u["cost"] = priced
                u["estimated"] = True
        return u

    def _add_usage(self, usage, message_id: str | None = None) -> None:
        """Fold one message's usage into the running task total — once per
        API message.

        Measured: a turn that answers with text and then a tool call arrives
        as TWO AssistantMessages with identical usage. Summing them doubled
        every input and cache figure (55,975 cache-creation tokens shown for
        a task whose bill said 29,071). Keyed by message_id the latest
        snapshot for a turn wins. Its output count is a count-so-far, not
        the final one; _settle_usage replaces all of this with the bill.
        """
        if not usage:
            return
        def g(k):
            v = usage.get(k) if isinstance(usage, dict) else getattr(usage, k, 0)
            return int(v or 0)
        key = message_id or f"anon-{len(self._usage_msgs)}"
        self._usage_msgs[key] = {"input": g("input_tokens"),
                                 "output": g("output_tokens"),
                                 "cache_read": g("cache_read_input_tokens"),
                                 "cache_creation": g("cache_creation_input_tokens")}
        for k in ("input", "output", "cache_read", "cache_creation"):
            self._usage[k] = sum(r[k] for r in self._usage_msgs.values())
        self._usage["turns"] = len(self._usage_msgs)

    def _settle_usage(self, msg) -> None:
        """The result's figures are the bill; the live sum was an estimate.

        `usage` is cumulative for the whole task, and `model_usage` breaks it
        down per model with the CLI's own cost — which is also how a side
        model shows up (Claude Code uses Haiku for small chores) that no
        AssistantMessage ever named.
        """
        u = getattr(msg, "usage", None) or {}
        def g(k):
            v = u.get(k) if isinstance(u, dict) else getattr(u, k, 0)
            return int(v or 0)
        if u:
            self._usage["input"] = g("input_tokens")
            self._usage["output"] = g("output_tokens")
            self._usage["cache_read"] = g("cache_read_input_tokens")
            self._usage["cache_creation"] = g("cache_creation_input_tokens")
        turns = getattr(msg, "num_turns", None)
        if turns:
            self._usage["turns"] = int(turns)
        models = {}
        for name, m in (getattr(msg, "model_usage", None) or {}).items():
            if not isinstance(m, dict):
                continue
            models[str(name)] = {
                "input": int(m.get("inputTokens") or 0),
                "output": int(m.get("outputTokens") or 0),
                "cache_read": int(m.get("cacheReadInputTokens") or 0),
                "cache_creation": int(m.get("cacheCreationInputTokens") or 0),
                "cost": float(m.get("costUSD") or 0.0),
            }
        if models:
            self._usage["models"] = models

    # -- the state border overlay ----------------------------------------
    def _border_color(self):
        """What the screen frame should say right now, or None for off.

        One colour at a time, by strict precedence:
          RED     the machine is being touched (mouse, keyboard, screen, UI tree)
          ORANGE  a person's answer is being waited on
          BLUE    thinking, or internal work (browser, files, MCP, memory)
        Red overrides orange, orange overrides blue — never a blend.
        """
        from core import overlay
        if not self.running or tools_sdk.is_paused():
            return None
        c = self._current or {}
        tool = c.get("tool", "")
        # replay only drives the machine when it is actually running a session
        touching = (tool in _TOUCH_TOOLS
                    or (tool == "replay" and "action=run" in (c.get("detail") or "")))
        if touching:
            return overlay.RED
        if self._question is not None or tool in _WAIT_TOOLS:
            return overlay.ORANGE
        return overlay.BLUE

    def _refresh_border(self) -> None:
        """Apply the current colour; a no-op unless the state changed."""
        try:
            color = self._border_color()
            if color == getattr(self, "_border_state", "unset"):
                return
            self._border_state = color
            if color is None:
                self._border_off()
                return
            from core import overlay
            if self._border is None:
                self._border = overlay.border()
            self._border.show(color)
            # The drawn cursor follows the same state — red while touching
            # the machine, blue otherwise, green when a person takes over —
            # and goes to wherever each action lands, including UIA invokes
            # and keystrokes that never move the real pointer. It replaces
            # the user's own arrow for as long as the agent has the machine.
            # See core/agent_cursor.py.
            from core import agent_cursor
            agent_cursor.show(color)
        except Exception:
            pass

    def _border_off(self) -> None:
        self._border_state = None
        try:
            from core import agent_cursor
            agent_cursor.hide()
            if self._border is not None:
                self._border.hide()
        except Exception:
            pass

    # -- the background question popup -----------------------------------
    def attach_windows(self, win, popup) -> None:
        """Given the app's own window and the small question popup (both from
        pywebview), so questions can surface without stealing focus."""
        self._win = win
        self._popup = popup
        self._popup_visible = False

    def _hwnd_of(self, win) -> int:
        try:
            native = getattr(win, "native", None)
            return int(native.Handle.ToInt64()) if native is not None else 0
        except Exception:
            return 0

    def _app_is_foreground(self) -> bool:
        """True when the app's OWN window is the active one. A foreground popup
        does NOT count — the user is mid-answer there, so the popup should stay."""
        try:
            import ctypes
            fg = ctypes.windll.user32.GetForegroundWindow()
            win = getattr(self, "_win", None)
            return bool(win is not None and fg and fg == self._hwnd_of(win))
        except Exception:
            return True             # unsure → behave as if attended, no popup

    def _popup_show(self) -> None:
        p = getattr(self, "_popup", None)
        if p is None or self._popup_visible:
            return
        try:
            p.show()
            self._popup_visible = True
            _round_window(self._hwnd_of(p))
        except Exception:
            pass

    def _popup_hide(self) -> None:
        p = getattr(self, "_popup", None)
        if p is None or not getattr(self, "_popup_visible", False):
            return
        try:
            p.hide()
        except Exception:
            pass
        self._popup_visible = False

    def _sync_question_surface(self) -> None:
        """Keep the question where the user can see it: the popup while the app
        is in the background, the in-app panel the instant the app is in front."""
        if self._question is None:
            self._popup_hide()
            return
        if self._app_is_foreground():
            self._popup_hide()
        else:
            self._popup_show()

    # -- exposed to the UI ------------------------------------------------
    def get_state(self) -> dict:
        prof = self._machine_profile()
        return {
            "running": self.running,
            "task": self.current_task,
            "feed": self.feed[-120:],
            "settings": self.settings,
            "machine": {
                "screen": f"{prof['screen']['w']}x{prof['screen']['h']}",
                "dpi": prof["screen"]["dpi_scale"],
                "apps": prof["capabilities"],
            },
            "arbiter": self.arbiter.status() if self.arbiter else None,
            "browser": self._browser_status(),
            "jobs": self.store.list(),
            "watchdog": watchdog.status(),
            "question": self._question,
            "channel": chan.channel.status(),
            "current": self._current,
            "paused": tools_sdk.is_paused(),
            "elapsed": (time.time() - self._started_at) if self.running else 0,
            "schedules": scheduler.status(),
            "missed": self._missed,
            "usage": self._usage_view(),
            "model": self._last_model or self.settings.get("model") or "",
            "keys": {"gemini": _has_secret(GEMINI_KEY),
                     "openrouter": _has_secret(OPENROUTER_KEY),
                     "jev": self._jev_has_key()},
            "key_tests": self._key_tests,
            "engine": self._engine_status,
            "proxy": (proxy.status(_endpoint_url(self.settings))
                      if _endpoint_url(self.settings) else None),
        }

    def _browser_status(self) -> dict:
        """Last known browser state, never a wait.

        `browser.status()` is marshalled onto the browser's own thread, so
        while a long web_do or a download is in flight it queues behind that
        work — for up to five minutes. The window polls this every 900ms, so
        the entire UI froze for exactly as long as the browser was busy, which
        is precisely when someone is watching it.
        """
        try:
            if getattr(browser, "busy", False):
                return self._last_browser
            self._last_browser = browser.status()
        except Exception:
            pass
        return self._last_browser

    def _machine_profile(self) -> dict:
        """The machine profile as the window should see it — never a wait.

        A first start has no cached profile, and building one walks Program
        Files and %LOCALAPPDATA% looking for the apps it can open: 30-45
        seconds on a full disk. get_state() is what the window's bridge polls
        every 900 ms, so building it in here froze the window for that long
        on first start — and, since the poll could not return until the scan
        had, a question asked in the meantime timed out unseen (a 30 s ask
        inside a 31 s poll). Build it once, in the background; until it is
        there report what is cheap to know — the screen — and no apps yet.

        The file is parsed here rather than through load_profile() on
        purpose: a read torn by the write in progress must count as "not
        ready yet", not fall through to a synchronous build on this thread.
        """
        global _profile_thread
        try:
            return json.loads(
                machine_profile.PROFILE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass                        # not built yet, or being written
        if _profile_built is not None:  # built, but could not be saved
            return _profile_built
        with _profile_lock:
            if _profile_thread is None:
                _profile_thread = threading.Thread(
                    target=_build_profile_in_background,
                    name="machine-profile", daemon=True)
                _profile_thread.start()
        return {"screen": machine_profile._screen(), "capabilities": []}

    # -- scheduling --------------------------------------------------------
    def _run_scheduled(self, s) -> bool:
        """Start a scheduled item. False means "not now" — try again shortly.

        Refusing while something else is running is deliberate: two tasks
        driving the same desktop at once is exactly what the arbiter exists to
        prevent, and the scheduler will simply re-offer it on the next tick.
        """
        if self.running:
            return False
        self._say("system", f"Scheduled: {s.task[:70]}")
        if s.job_id:
            return self.run_job(s.job_id).get("ok", False)
        return self.run_task(s.task).get("ok", False)

    def list_schedules(self) -> list[dict]:
        return scheduler.status()

    def add_schedule(self, task: str, when: str, job_id: str = "") -> dict:
        task = (task or "").strip()
        if not task and not job_id:
            return {"ok": False, "error": "Say what should be done."}
        from core.schedule import parse
        spec = parse(when)
        if not spec.get("understood"):
            return {"ok": False,
                    "error": f"Could not read {when!r} as a time. Try "
                             f"'every morning at 9', 'weekdays at 8am', "
                             f"'every 3 hours'."}
        s = scheduler.add(task, when, job_id=job_id)
        self._say("system", f"Scheduled '{task[:50]}' — {s.describe()}.")
        return {"ok": True, "id": s.id, "when": s.describe()}

    def remove_schedule(self, sid: str) -> dict:
        return {"ok": scheduler.remove(sid)}

    def toggle_schedule(self, sid: str, on: bool) -> dict:
        return {"ok": scheduler.enable(sid, on)}

    def pause_task(self, on: bool = True) -> dict:
        """Hold the run without losing it.

        Stop throws the work away; this does not. The wait happens inside the
        tools, so a pause takes effect before the next action rather than at
        the next model turn — which could be many keystrokes later.
        """
        tools_sdk.set_paused(bool(on))
        # Paused means nothing happens, so the frame goes dark; resume brings
        # back whichever colour the state calls for.
        self._refresh_border()
        self._say("system", "Paused — nothing will be touched until you resume."
                  if on else "Resumed.")
        return {"ok": True, "paused": tools_sdk.is_paused()}

    # -- questions from a running task -----------------------------------
    def _ask_in_app(self, question: str, approve_deny: bool,
                    timeout_s: float) -> "chan.Reply | None":
        """Put a question in the window and block the task until it is answered.

        Returns None if nobody answers in time, which lets the channel fall
        through to Telegram rather than the task giving up. Raising the window
        here is safe: the arbiter treats only *physical* input as a takeover.
        """
        self._question = {"text": question, "approve_deny": approve_deny,
                          "since": time.time()}
        self._answer = None
        self._answered.clear()
        self._say("ask", question)
        self._refresh_border()          # orange: waiting on a person
        # Do NOT drag the whole app to the front. If it is already in front the
        # in-app panel shows the question; if it is behind (the agent is off
        # working in another window) a small popup carries it instead, without
        # stealing the desktop. Which one is live is kept in step with focus.
        self._sync_question_surface()
        # Poll rather than one long wait: ESC+ESC and Stop have to be able to
        # release this. A single blocking wait meant an aborted task sat here
        # for up to five minutes waiting for an answer nobody was going to
        # give, with the kill switch apparently doing nothing.
        deadline = time.time() + min(timeout_s, 300)
        got = False
        while time.time() < deadline:
            self._sync_question_surface()
            if self._answered.wait(0.35):
                got = True
                break
            if tools_sdk.should_stop():
                self._question = None
                self._popup_hide()
                self._refresh_border()
                self._say("system", "Question abandoned — the task was stopped.")
                return chan.Reply(chan.Answer.DENY, via="app")
        self._question = None
        self._popup_hide()
        self._refresh_border()          # answered: back to whatever runs next
        if not got:
            self._say("system", "No answer here — trying to reach you elsewhere.")
            return None
        return self._answer

    def answer_question(self, answer: str, text: str = "") -> dict:
        """Called by the window when the user clicks Approve / Deny or types."""
        if self._question is None:
            return {"ok": False, "error": "Nothing is waiting on an answer."}
        kind = {"approve": chan.Answer.APPROVE,
                "deny": chan.Answer.DENY}.get(answer, chan.Answer.TEXT)
        self._answer = chan.Reply(kind, text=text or "", via="app")
        self._say("system", f"You answered: {answer}"
                            + (f" — {text}" if text and answer == "text" else ""))
        # Their click was physical input on a window that just took focus —
        # exactly what the arbiter treats as a takeover. It isn't one: they
        # clicked *because* we asked, so the run must not stop here. Done
        # before releasing the task, or it resumes into a YIELDED arbiter.
        if self.arbiter is not None:
            self.arbiter.rebaseline()
        self._popup_hide()
        self._answered.set()
        return {"ok": True}

    # -- credentials and channel setup ------------------------------------
    def list_models(self, provider: str = "") -> list[dict]:
        """The models that can be chosen, for the Settings dropdowns.

        Anthropic's are a short fixed list. OpenRouter publishes its whole
        catalogue (400+) without authentication, so it is fetched live and
        cached for the session — a stale hardcoded list is exactly what makes
        a model picker useless a month later.
        """
        provider = (provider or self.settings.get("provider") or "anthropic")
        if provider in ("custom", "gemini"):
            # Whatever the proxy is configured to serve — asked, not assumed.
            # LiteLLM (and anything OpenAI-shaped) publishes its list at
            # /v1/models, so the dropdown mirrors the proxy's config file: add
            # a model there and it appears here. The wildcard entry is skipped;
            # "Default" already means that.
            #
            # Gemini gets no "Default": the CLI would ask for its Claude model
            # name and the proxy's wildcard would hand that to the LOCAL model.
            # The first Gemini the proxy serves is used instead (see _drive).
            gemini = provider == "gemini"
            out = ([] if gemini else
                   [{"id": "", "name": "Default — whatever the endpoint serves"}])
            url = _endpoint_url({**self.settings, "provider": provider})
            url = url.replace("://localhost:", "://127.0.0.1:")   # see _provider_env
            if not url:
                return out
            cached = getattr(self, "_custom_models", None)
            if cached and cached[0] == (url, gemini):
                return cached[1]
            # Opening Settings is the first time most sessions touch the
            # proxy; bring it up so the list can be read from it.
            proxy.ensure(url, self._proxy_env(), wait_s=30)
            import urllib.request
            try:
                key = vault.get(OPENROUTER_KEY)
            except Exception:
                key = ""
            try:
                req = urllib.request.Request(
                    url + "/v1/models",
                    headers={"User-Agent": "AGIAgent",
                             "Authorization": f"Bearer {key or 'local'}"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    data = json.load(r).get("data", [])
                # Config-file order, not alphabetical: the first Gemini listed
                # there is the one meant to be tried first, and it is what a
                # task runs on when no model is chosen.
                for m in data:
                    mid = m.get("id", "")
                    if not mid or mid == "*":
                        continue
                    if gemini and not mid.lower().startswith("gemini"):
                        continue
                    out.append({"id": mid, "name": mid})
            except Exception as e:
                msg = (f"Endpoint not reachable ({type(e).__name__}); start "
                       f"the proxy, then reopen Settings")
                if out:
                    out[0]["name"] = "Default — " + msg[0].lower() + msg[1:]
                else:
                    out.append({"id": "", "name": msg})
                return out
            if gemini and not out:
                out.append({"id": "", "name": "No Gemini models in "
                                              "litellm-config.yaml"})
            self._custom_models = ((url, gemini), out)
            return out
        if provider != "openrouter":
            return [{"id": "", "name": "Default — whatever Claude Code is set to"},
                    {"id": "claude-opus-5", "name": "Opus 5 — most capable"},
                    {"id": "claude-fable-5-1", "name": "Fable 5.1"},
                    {"id": "claude-sonnet-5", "name": "Sonnet 5 — faster, cheaper"},
                    {"id": "claude-haiku-4-5-20251001", "name": "Haiku 4.5 — fastest"}]
        cached = getattr(self, "_or_models", None)
        if cached:
            return cached
        import urllib.request
        out = [{"id": "", "name": "Default — provider's own default"}]
        try:
            req = urllib.request.Request(
                OPENROUTER_URL + "/v1/models", headers={"User-Agent": "AGIAgent"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.load(r).get("data", [])
            for m in sorted(data, key=lambda m: m.get("id", "")):
                mid = m.get("id", "")
                if not mid or mid.endswith(":batch"):
                    continue        # batch variants are not for interactive use
                out.append({"id": mid, "name": m.get("name") or mid})
        except Exception as e:
            return [{"id": "", "name": f"Could not reach OpenRouter ({type(e).__name__}) "
                                       f"— type a model id by hand"}]
        self._or_models = out
        return out

    def save_openrouter_key(self, key: str) -> dict:
        """Store the OpenRouter key in the vault (not in settings.json)."""
        key = (key or "").strip()
        if not key:
            try:
                vault.delete(OPENROUTER_KEY)
            except Exception:
                pass
            return {"ok": True, "saved": False}
        try:
            vault.put(OPENROUTER_KEY, key, note="OpenRouter API key")
        except VaultError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "saved": True}

    # -- the local proxy ---------------------------------------------------
    def _proxy_env(self) -> dict:
        """Provider keys the local proxy needs, from the vault."""
        env = {}
        try:
            k = vault.get(GEMINI_KEY)
            if k:
                env["GEMINI_API_KEY"] = k
        except Exception:
            pass
        return env

    def _ensure_proxy(self, wait_s: float = 45.0) -> dict | None:
        """Start the local proxy if the custom endpoint needs one.
        None when the provider is not custom or no endpoint is set."""
        url = _endpoint_url(self.settings)
        if not url:
            return None
        return proxy.ensure(url, self._proxy_env(), wait_s,
                            say=lambda m: self._say("system", m))

    def save_gemini_key(self, key: str) -> dict:
        """Store the Gemini key in the vault, and get it to the proxy."""
        key = (key or "").strip()
        try:
            if key:
                vault.put(GEMINI_KEY, key, note="Gemini API key")
            else:
                vault.delete(GEMINI_KEY)
        except VaultError as e:
            return {"ok": False, "error": str(e)}
        # Whatever the SAVED provider is: the key was typed with Gemini in
        # mind, and the provider dropdown may not have been saved yet.
        url = _endpoint_url(self.settings) or DEFAULT_PROXY_URL
        if proxy.ours():
            r = proxy.restart(url, self._proxy_env())
            detail = ("Proxy restarted with the new key." if r["ok"]
                      else f"Proxy restart failed: {r['detail']}")
        elif proxy.reachable(url):
            detail = ("A LiteLLM you started yourself is running; it will "
                      "not see this key until you restart it with "
                      "GEMINI_API_KEY set — or close it, and the app "
                      "will start one with the key on the next task.")
        else:
            detail = "The app will start the proxy with this key when a task needs it."
        return {"ok": True, "saved": bool(key), "detail": detail}

    # -- the script engine's keys ----------------------------------------
    def _jev_has_key(self) -> bool:
        try:
            from core import keys
            return keys.has_jev_key(self.settings)
        except Exception:
            return False

    def save_jev_key(self, backend: str, key: str, account: str = "") -> dict:
        """Store Jev's key in the vault. Never in settings.json."""
        from core import keys
        backend = backend or self.settings.get("jev_backend") or "openrouter"
        try:
            r = keys.save_jev_key(backend, key, account)
        except VaultError as e:
            return {"ok": False, "error": str(e)}
        if r.get("ok"):
            self.update_settings({"jev_backend": backend})
            self._key_tests.pop("jev", None)
        return r

    def test_jev(self) -> dict:
        from core import keys
        r = keys.test_jev(self.settings)
        self._key_tests["jev"] = r
        return r

    def test_claude(self) -> dict:
        from core import keys
        r = keys.test_claude(self.settings)
        self._key_tests["claude"] = r
        return r

    def provider_status(self) -> dict:
        have = False
        try:
            have = bool(vault.get(OPENROUTER_KEY))
        except Exception:
            have = False
        return {"provider": self.settings.get("provider") or "anthropic",
                "openrouter_key": have}

    def list_credentials(self) -> list[dict]:
        return [{"name": i.name, "note": i.note, "uses": i.uses,
                 "used_at": i.used_at} for i in vault.list()]

    def save_credential(self, name: str, value: str, note: str = "") -> dict:
        name = (name or "").strip()
        if not name or not value:
            return {"ok": False, "error": "Both a name and a value are needed."}
        try:
            vault.put(name, value, note=note)
        except VaultError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "name": name}

    def delete_credential(self, name: str) -> dict:
        return {"ok": vault.delete(name)}

    def channel_status(self) -> dict:
        return chan.channel.status()

    def save_telegram_token(self, token: str) -> dict:
        return chan.channel.telegram.set_token(token)

    def telegram_pair(self) -> dict:
        """Step one: hand back the link the user must open."""
        return chan.channel.telegram.pairing_link()

    def telegram_pair_finish(self) -> dict:
        """Step two, after they press Start in Telegram."""
        return chan.channel.telegram.complete_pairing(wait_s=45)

    def telegram_test(self) -> dict:
        ok = chan.channel.telegram.send(
            f"Test message from {APP_TITLE}. If you can read this, "
            "I can reach you while a task is running.")
        return {"ok": ok, "error": "" if ok else "not paired yet"}

    def update_settings(self, patch: dict) -> dict:
        self.settings.update(patch or {})
        save_settings(self.settings)
        browser.headless = bool(self.settings.get("browser_headless", True))
        watchdog.check_every_s = float(self.settings.get("watchdog_minutes", 60)) * 60
        watchdog.stall_after_s = float(self.settings.get("stall_minutes", 10)) * 60
        return self.settings

    def run_task(self, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "Describe what you want done."}
        with self._start_lock:
            if self.running:
                return {"ok": False, "error": "A task is already running."}
            self.running = True         # claimed before the thread exists
            self.current_task = text
            self._cancel.clear()
            tools_sdk.set_cancelled(False)
            self.feed.clear()
            self._thread = threading.Thread(target=self._run, args=(text,),
                                            daemon=True)
            self._thread.start()
        return {"ok": True}

    def stop_task(self) -> dict:
        self._cancel.set()
        # Long waits inside tools poll this. They run on the event loop the
        # SDK reads messages on, so a blocking wait there cannot see the
        # cancel check in the message loop — it has to be told directly.
        tools_sdk.set_cancelled(True)
        # Release a pause first: a paused task is parked inside a tool and
        # would never reach the cancel check.
        tools_sdk.set_paused(False)
        if self._question is not None:          # and unblock a pending question
            self._answer = chan.Reply(chan.Answer.DENY, via="app")
            self._answered.set()
        # Then reach the SDK itself. The flags above only take effect when a
        # message next arrives from the CLI — and with a question just
        # answered "deny" on the user's behalf, that is after the model has
        # finished thinking about it: several seconds of a Stop that looked
        # ignored. Cancelling the task on its own loop ends the wait now.
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._cancel_in_band)
            except RuntimeError:
                pass                # loop already closed: the run is over
        self._say("system", "Stop requested — finishing the current step.")
        return {"ok": True}

    def _cancel_in_band(self) -> None:
        """Cancel the SDK task from inside its own loop — once, and only once
        the CLI is up.

        Once: the CancelledError is delivered at whatever the task is awaiting,
        and after the first one that is the SDK's own cleanup. A second cancel
        would land inside its shielded close() and skip the terminate-then-kill
        escalation that makes sure the CLI process is gone.

        Only once the CLI is up: before the first message the SDK is still
        spawning and initialising the subprocess, and a cancellation in the
        middle of that is the SDK's least-tested path. A Stop pressed that
        early is not lost — the loop in _consume acts on it at the first
        message, which is the init message a second or two later.
        """
        if self._cancel_sent or self._main_task is None or not self._streaming:
            return
        self._cancel_sent = True
        self._main_task.cancel()

    # -- shutdown ----------------------------------------------------------
    def begin_shutdown(self) -> None:
        """Called by the window's closing handler, on the window's own thread.

        It must not wait. That thread paints the window, and a join here froze
        it for up to ten seconds — the X appeared to do nothing, exactly when
        the user was trying to get away from a question they did not want to
        answer. Signal the stop and let the window go.
        """
        if self.running:
            self.stop_task()

    def finish_shutdown(self, timeout_s: float = 15.0) -> bool:
        """Called once the window is gone: wait for the task to put the machine
        back.

        The task thread is a daemon, so exiting now would kill it outright and
        KeepSessionOpen would never run its exit — leaving the screensaver and
        sleep timer disabled for the rest of the session. Bounded, so a wedged
        CLI cannot keep a closed app alive forever. True if the task ended.
        """
        t = self._thread
        if t is None or not t.is_alive():
            return True
        t.join(timeout=timeout_s)
        return not t.is_alive()

    def run_job(self, job_id: str) -> dict:
        from core.replay import Replayer
        with self._start_lock:
            if self.running:
                return {"ok": False, "error": "busy"}
            self.running = True

        def go():
            try:
                rp = Replayer(self.store,
                              on_event=lambda k, d: self._say("job", f"{k} {d}"))
                res = rp.run(job_id, arbiter=self.arbiter)
                self._say("result" if res.ok else "error", res.report())
            finally:
                self.running = False

        threading.Thread(target=go, daemon=True).start()
        return {"ok": True}

    # -- the actual work --------------------------------------------------
    def _run(self, task: str) -> None:
        self.running = True
        self._started_at = time.time()
        self._current = None
        self._usage = _blank_usage()    # tokens are counted per task
        self._usage_msgs = {}
        tools_sdk.reset_shots()
        tools_sdk.set_screenshot_detail(self.settings.get("screenshot_detail")
                                        or "standard")
        tools_sdk.set_paused(False)     # a new task never starts held
        watchdog.start()
        watchdog.begin(self._task_id, task[:60])

        self.arbiter = Arbiter(owner=APP_TITLE)
        if not self.arbiter.start():
            self._say("error", f"Could not take control: {self.arbiter.state.detail}")
            self.running = False
            return
        tools_sdk.set_arbiter(self.arbiter)
        tools_sdk.set_task(task)
        browser.headless = bool(self.settings.get("browser_headless", True))

        self._say("system", f"Working on: {task}")
        self._refresh_border()          # blue: running, thinking
        try:
            outcome = self._route(task)
            if outcome is not None and outcome.kind in ("done", "stopped"):
                return                  # the script engine finished it
            # A custom endpoint on this machine is usually LiteLLM, and it
            # may not be up yet. Started only now - a task a saved script
            # handled never needed it - and with the vault's provider keys,
            # rather than let the CLI die on "connection refused".
            r = self._ensure_proxy()
            if r is not None and not r["ok"]:
                self._say("error", f"The local endpoint is not answering: "
                                   f"{r['detail']}")
                return
            prompt = outcome.prompt if outcome is not None else ""
            asyncio.run(self._drive(task, prompt=prompt))
        except asyncio.CancelledError:
            # Not ours (ours is absorbed in _consume). Still end the run in
            # order rather than killing the thread with a traceback.
            self._say("system", "Stopped.")
        except Exception as e:
            # The SDK's own wording for a provider failure is
            # "returned an error result: success", which explains nothing —
            # the actionable part came through in the result message above.
            hint = explain_provider_error(str(e))
            if hint:
                self._say("error", "The model provider refused the request.")
                self._say("system", hint)
            else:
                self._say("error", f"{type(e).__name__}: {e}")
        finally:
            watchdog.finish(self._task_id, ok=True)
            self._engine_status = {"state": "idle"}
            if self.arbiter:
                self.arbiter.stop()
            tools_sdk.set_arbiter(None)
            tools_sdk.set_task("")
            tools_sdk.set_paused(False)
            self.arbiter = None
            self.running = False
            self._current = None
            self._border_off()
            self._popup_hide()
            mins, secs = divmod(int(time.time() - self._started_at), 60)
            took = f"Finished in {mins}m {secs}s." if mins else f"Finished in {secs}s."
            spent = _usage_line(self._usage)
            self._say("system", took + (f"  {spent}." if spent else ""))
            self._come_to_front()

    def _route(self, task: str):
        """Jev first: run a saved script if one fits. None = go to Claude.

        Never lets the engine take a task down with it - any failure in the
        engine itself hands the task to Claude, which is where it would
        have gone without the engine at all.
        """
        if not self.settings.get("scripts_first", True):
            return None
        try:
            from core.engine import ScriptEngine
            eng = ScriptEngine(
                self.settings, self._say,
                set_status=lambda st: setattr(self, "_engine_status",
                                              st.as_dict()),
                set_current=self._set_current_tool)
            return eng.handle(task)
        except Exception as e:
            self._say("system", f"Script engine error ({type(e).__name__}: "
                                f"{e}) — handing the task to Claude.")
            return None

    def _set_current_tool(self, cur: dict | None) -> None:
        self._current = cur
        self._refresh_border()

    def _come_to_front(self) -> None:
        """Raise the app window when the work is done.

        A long task means the user has gone elsewhere; the result is useless
        if it lands behind whatever they are looking at. Released AFTER the
        arbiter stops, so raising our own window is never mistaken for the
        user taking control.
        """
        try:
            from core import actuator, locator
            for w in locator.list_windows():
                if w["title"] == APP_TITLE:
                    actuator.focus_window(w["hwnd"])
                    try:
                        import ctypes
                        ctypes.windll.user32.FlashWindow(w["hwnd"], True)
                    except Exception:
                        pass
                    return
        except Exception:
            pass

    async def _drive(self, task: str, prompt: str = "") -> None:
        # Work in a scratch directory, never in the app's own source tree.
        workspace = paths.data("workspace")
        workspace.mkdir(parents=True, exist_ok=True)

        # Desktop tools plus the general-purpose ones. Without Bash and the
        # file tools the agent cannot parse a page it just downloaded, which
        # is most of what real tasks need.
        allowed = ([f"mcp__desktop__{t.name}" for t in tools_sdk.ALL_TOOLS]
                   + ["Bash", "Read", "Write", "Edit", "Glob", "Grep",
                      "WebFetch", "WebSearch", "TodoWrite"])

        opts = ClaudeAgentOptions(
            system_prompt=tools_sdk.SYSTEM_PROMPT,
            mcp_servers={"desktop": tools_sdk.build_server()},
            allowed_tools=allowed,
            # Use ONLY the tools this app provides. Without this the agent
            # inherits the user's Claude Code MCP config — including the old
            # screenshot-and-guess desktop server — and will happily call an
            # 84-second vision pipeline instead of the 0.5s one right here.
            strict_mcp_config=True,
            setting_sources=[],
            # This IS the automation app — a permission prompt has nowhere to
            # go, so gating tools here means silently denying them mid-task.
            # Safety lives in the arbiter instead: the user's own input pauses
            # the run and a physical ESC+ESC aborts it.
            permission_mode="bypassPermissions",
            cwd=str(workspace),
            # One tool result is one JSON line over the CLI pipe, and the
            # SDK's default line limit is 1 MB. A screenshot read back as an
            # image is ~0.5-1.1 MB of base64, so the default killed a task
            # mid-run ("JSON message exceeded maximum buffer size").
            max_buffer_size=64 * 1024 * 1024,
            env={
                # Tool search hides the tool list behind a lookup the model
                # must call before it can act. That pays off with hundreds of
                # tools; we have ~33, all relevant, so it is pure latency —
                # every task began by searching for the tools it was given.
                # Measured on "call machine_info and report the screen size":
                #   on  — 4 turns, 1 ToolSearch, 22.4s
                #   off — 2 turns, 0 ToolSearch,  5.2s
                # The real run that prompted this spent four of them.
                "ENABLE_TOOL_SEARCH": "false",
                # Empty unless a non-Anthropic provider is configured.
                **_provider_env(self.settings),
            },
        )
        # No turn cap. A long task is not a runaway task — the watchdog is
        # what catches genuinely stuck work, and the user can stop it at any
        # moment. Cutting off at an arbitrary count just loses the work.
        limit = int(self.settings.get("max_turns") or 0)
        if limit > 0:
            opts.max_turns = limit
        if self.settings.get("model"):
            opts.model = self.settings["model"]
        elif (self.settings.get("provider") or "") == "gemini":
            # No "Default" for Gemini (see list_models): the first one the
            # proxy serves, or the CLI would ask for a Claude name and the
            # wildcard would hand it to the local model.
            names = [m["id"] for m in self.list_models("gemini") if m["id"]]
            if names:
                opts.model = names[0]
                self._say("system", f"Model: {names[0]}")
        # A second model to fall back on if the first is overloaded, so a long
        # task degrades to a cheaper model instead of dying at step 40.
        if self.settings.get("fallback_model"):
            opts.fallback_model = self.settings["fallback_model"]

        # Put what was learned from past tasks like this one in front of the
        # model before it plans — the "check later easily" half of task memory.
        # Recalled lessons arrive with the task instead of costing a lookup
        # turn. Never let a lessons failure break the task itself.
        if prompt:
            # A takeover from a stuck script: the prompt already says what
            # was done and what is left. Lessons would only dilute it.
            await self._consume(query(prompt=prompt, options=opts))
            return
        prompt = task
        try:
            from core import lessons
            # Before the task, not after: the system prompt tells the model
            # these appear "above the task", and context it should apply while
            # reading the request has to arrive first.
            prompt, recalled = lessons.build_prompt(task, limit=3)
            if recalled:
                titles = ", ".join(f"'{L['title']}'" for L in recalled)
                self._say("system", f"Recalled {len(recalled)} lesson(s): {titles}")
        except Exception as e:
            # Recall is an optimisation; a broken library must not stop work.
            # Silence hid a genuinely broken feature during testing, so say so.
            self._say("system", f"(could not check past lessons: "
                                f"{type(e).__name__})")

        await self._consume(query(prompt=prompt, options=opts))

    async def _consume(self, agen) -> None:
        """Read the SDK's message stream, and leave it properly when stopped.

        Leaving is the delicate part. `query()` is three nested async
        generators; the middle one acloses the innermost in a `finally`, and
        the innermost spends seconds in its own `finally` waiting for the CLI
        process to exit. Returning out of the loop abandons all three at their
        `yield`. Their finalizers then start closing the chain on the loop,
        the innermost begins its wait — and asyncio.run() moves on to its
        shutdown sweep, which acloses the same generators a second time while
        the first close is still running: "aclose(): asynchronous generator
        is already running", logged on every Stop and every window close. The
        loop then closed with that first close, and the CLI's termination,
        still pending.

        So a stop is delivered as a cancellation of this task instead. The
        CancelledError is raised at the innermost await — the SDK waiting on
        the CLI — and unwinds the chain from the inside out, as part of this
        task: transport closed, CLI terminated, each `finally` run exactly
        once and run to completion before the loop can shut down. Nothing is
        left for the sweep to find. It also means Stop takes effect now, not
        whenever the model next finishes a thought.
        """
        self._loop = asyncio.get_running_loop()
        self._main_task = asyncio.current_task()
        self._streaming = False
        self._cancel_sent = False
        try:
            async for msg in agen:
                self._streaming = True
                if self._cancel.is_set() or (self.arbiter and self.arbiter.aborted):
                    # Ask for the cancellation and go back to waiting: it is
                    # delivered at that wait, which is where the unwind has
                    # to start. Breaking out here would abandon the chain.
                    self._cancel_in_band()
                    continue
                self._handle(msg)
        except asyncio.CancelledError:
            if not self._cancel_sent:
                raise                       # somebody else's — not ours to eat
            if self.arbiter and self.arbiter.aborted:
                self._say("system", "Aborted — you pressed ESC twice.")
            else:
                self._say("system", "Stopped by user.")
        finally:
            self._streaming = False
            self._main_task = None
            self._loop = None

    def _handle(self, msg) -> None:
        """One message from the SDK onto the feed and the watchdog."""
        if isinstance(msg, AssistantMessage):
            self._add_usage(getattr(msg, "usage", None),
                            getattr(msg, "message_id", None))
            # Which model is running decides the price. Take the LATEST, not
            # the first: a long task can start on the main model and finish on
            # the fallback, and pinning the first name bills every token after
            # the switch at the wrong rate.
            self._usage["model"] = (getattr(msg, "model", "")
                                    or self._usage["model"])
            self._last_model = self._usage["model"] or self._last_model
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    self._say("claude", block.text.strip())
                    watchdog.beat(self._task_id, block.text.strip()[:70])
                elif isinstance(block, ThinkingBlock):
                    watchdog.beat(self._task_id, "thinking")
                elif isinstance(block, ToolUseBlock):
                    name = block.name.replace("mcp__desktop__", "")
                    detail = ", ".join(f"{k}={v}" for k, v in
                                       list((block.input or {}).items())[:3])
                    self._say("action", f"{name}({detail[:80]})")
                    watchdog.beat(self._task_id, name)
                    # What is happening *right now*, so a long step reads
                    # as working rather than as stuck.
                    self._current = {"tool": name, "detail": detail[:80],
                                     "since": time.time()}
                    self._refresh_border()      # red / orange / blue by tool
        elif isinstance(msg, UserMessage):
            # Tool results come back as a UserMessage, not on the assistant
            # turn — clearing it there would never fire, and the UI would
            # show a finished tool as still running.
            for block in getattr(msg, "content", None) or []:
                if isinstance(block, ToolResultBlock):
                    self._current = None
                    watchdog.beat(self._task_id, "tool result")
                    self._refresh_border()      # back to blue: thinking
        elif isinstance(msg, ResultMessage):
            self._current = None
            self._refresh_border()
            self._settle_usage(msg)
            cost = getattr(msg, "total_cost_usd", None)
            if cost:
                self._usage["cost"] = float(cost)
                # The one moment a real price is known. A model the table
                # prices needs nothing from it; any other turns it into a rate
                # so the NEXT task can show an estimate instead of nothing.
                if not unit_prices(self._usage.get("model", "")):
                    learn_rate(self._usage.get("model", ""),
                               _weighted_tokens(self._usage), float(cost))
            body = getattr(msg, "result", "") or "Done."
            self._say("result", body)
            hint = explain_provider_error(body)
            if hint:
                self._say("system", hint)


def _round_window(hwnd: int) -> None:
    """Ask DWM to round a frameless window's corners (Windows 11).

    A frameless popup is a bare rectangle; WINDOW_CORNER_PREFERENCE = ROUND
    clips it to the same soft corners the OS gives normal windows. Harmless
    (a silent no-op) on Windows 10 and earlier."""
    if not hwnd:
        return
    try:
        import ctypes
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_ROUND = 2
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(ctypes.c_int(DWMWCP_ROUND)), ctypes.sizeof(ctypes.c_int))
    except Exception:
        pass


def _popup_geometry() -> tuple[int, int, int, int]:
    """A small window in the lower-right of the work area."""
    w, h = 400, 250
    try:
        import ctypes
        from ctypes import wintypes
        r = wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)
        scale = (ctypes.windll.user32.GetDpiForSystem() / 96.0) or 1.0
        area_r = int(r.right / scale)
        area_b = int(r.bottom / scale)
        return w, h, area_r - w - 22, area_b - h - 22
    except Exception:
        return w, h, 700, 400


def _window_geometry() -> tuple[int, int, int, int]:
    """Size AND position, both clamped to the usable desktop.

    Sizing alone is not enough: pywebview centres on the whole screen, not the
    work area, so a window that fits perfectly still ends up pushed down behind
    the taskbar — with the Run button underneath it, which makes the app look
    broken before it has done anything.
    """
    want_w, want_h = 1180, 812
    try:
        import ctypes
        from ctypes import wintypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)      # per-monitor
        except Exception:
            pass
        r = wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)
        scale = (ctypes.windll.user32.GetDpiForSystem() / 96.0) or 1.0
        area_x, area_y = int(r.left / scale), int(r.top / scale)
        area_w = int((r.right - r.left) / scale)
        area_h = int((r.bottom - r.top) / scale)

        # Leave room for the title bar, which sits outside the height we ask for.
        chrome = 34
        w = max(880, min(want_w, area_w - 40))
        h = max(560, min(want_h, area_h - chrome - 20))
        x = area_x + max(0, (area_w - w) // 2)
        y = area_y + max(0, (area_h - h - chrome) // 2)
        return w, h, x, y
    except Exception:
        return want_w, want_h, 80, 60


#: HICONs handed to the window. Kept for the life of the process: destroying
#: an icon a window is still drawing leaves it blank.
_ICON_HANDLES: list[int] = []


def _apply_window_icon(win) -> bool:
    """Hand the window the icon at the sizes Windows actually draws.

    pywebview's `icon=` sets Form.Icon, which loads one frame of the .ico —
    the 64px one, measured — and leaves Windows to scale it down for the
    title bar and the taskbar. That is a smear at 16px, precisely the size
    the file carries a frame drawn by hand for. WM_SETICON with LoadImage at
    the system's small and large icon metrics makes Windows use those frames
    instead. Form.Icon stays as the fallback if any of this is refused.
    """
    if sys.platform != "win32" or not ICON.exists():
        return False
    try:
        import ctypes
        u = ctypes.windll.user32
        native = getattr(win, "native", None)
        hwnd = int(native.Handle.ToInt64()) if native is not None else 0
        if not hwnd:
            hwnd = u.FindWindowW(None, APP_TITLE)
        if not hwnd:
            return False
        IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
        WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
        SM_CXICON, SM_CXSMICON = 11, 49
        u.LoadImageW.restype = ctypes.c_void_p
        u.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        u.SendMessageW.restype = ctypes.c_void_p
        u.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                   ctypes.c_void_p, ctypes.c_void_p]
        applied = False
        for which, metric, fallback in ((ICON_SMALL, SM_CXSMICON, 16),
                                        (ICON_BIG, SM_CXICON, 32)):
            n = u.GetSystemMetrics(metric) or fallback
            h = u.LoadImageW(None, str(ICON), IMAGE_ICON, n, n, LR_LOADFROMFILE)
            if h:
                _ICON_HANDLES.append(h)
                u.SendMessageW(hwnd, WM_SETICON, which, h)
                applied = True
        return applied
    except Exception:
        return False


def claude_cli_missing() -> str:
    """The one dependency a packaged build cannot carry.

    The Agent SDK does not embed a model client — it drives `claude.exe` as a
    subprocess. Bundling that is not an option: it is a separate product with
    its own sign-in and update path, and shipping a frozen copy would be worse
    than requiring the real one. Detecting it here turns an unreadable
    subprocess error into a sentence that says what to install.
    """
    import shutil
    if shutil.which("claude") or shutil.which("claude.exe"):
        return ""
    if (Path.home() / ".local" / "bin" / "claude.exe").exists():
        return ""
    return ("Claude Code is not installed on this machine, or is not on PATH. "
            "This app drives it to think. Install it from "
            "https://claude.com/claude-code and sign in, then start this again.")


def main() -> None:
    agent = Agent()
    agent.update_settings({})

    problem = claude_cli_missing()
    if problem:
        agent._say("error", problem)

    # Anything whose moment passed while the app was closed is REPORTED, not
    # fired. Silently running six hours of missed work the instant the window
    # opens is not what anyone means by "every morning at nine".
    scheduler.runner = agent._run_scheduled
    missed = scheduler.due_while_away()
    for s in missed:
        scheduler.mark_missed(s.id)
    agent._missed = [{"id": s.id, "task": s.task, "when": s.describe()}
                     for s in missed]
    if missed:
        agent._say("system",
                   f"{len(missed)} scheduled task(s) came due while the app was "
                   f"closed. They were not run — start any of them from the "
                   f"Schedule tab.")
    scheduler.start()

    # Run from source, the taskbar files this window under python.exe and
    # shows Python's icon on it whatever the window itself carries. An
    # explicit application id makes it our own entry. Harmless if refused.
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AGIAgent")
    except Exception:
        pass

    w, h, x, y = _window_geometry()
    win = webview.create_window(
        APP_TITLE,
        str(paths.resource("ui", "app.html")),
        js_api=agent,
        width=w, height=h, x=x, y=y, min_size=(880, 560),
        background_color="#0d141f",
    )

    # A small, frameless, always-on-top popup for questions the agent raises
    # while the app is in the background. It shares the app's own js_api, so it
    # reads the same state and answers through the same path — and stays hidden
    # until a background question actually needs it.
    pw, ph, px, py = _popup_geometry()
    popup = webview.create_window(
        "AGI Agent — question",
        str(paths.resource("ui", "popup.html")),
        js_api=agent,
        width=pw, height=ph, x=px, y=py,
        frameless=True, on_top=True, easy_drag=False, shadow=True,
        hidden=True, focus=False, background_color="#141d2b",
    )
    agent.attach_windows(win, popup)
    try:
        popup.events.shown += lambda: _round_window(agent._hwnd_of(popup))
    except Exception:
        pass

    def _on_closing():
        # On the window's own thread, so it only signals; the wait for the
        # task to put the machine back happens after start() returns, once
        # the window is already gone. See Agent.begin_shutdown.
        try:
            agent.begin_shutdown()
        except Exception:
            pass
        # Destroy the hidden popup too, or webview.start() waits forever for a
        # window nobody can see and the process never exits.
        try:
            agent._border_off()
            if agent._border is not None:
                agent._border.stop()
            from core import agent_cursor, overlay
            agent_cursor.stop()
            # stop() restores the user's cursor; this does it again from
            # here in case anything went wrong on the way.
            overlay.restore_system_cursor()
        except Exception:
            pass
        try:
            popup.destroy()
        except Exception:
            pass
        return True             # never veto the close

    try:
        win.events.closing += _on_closing
        win.events.shown += lambda: _apply_window_icon(win)
    except Exception:
        pass                    # older pywebview: nothing to hook, carry on
    # pywebview's own docs say `icon` is GTK/QT only; its WinForms backend
    # reads it too (Form.Icon), which covers the title bar, the taskbar and
    # Alt-Tab. The packaged exe additionally carries it in its resources.
    webview.start(debug=False, icon=str(ICON) if ICON.exists() else None)
    agent.finish_shutdown()
    proxy.stop()                # only a proxy this app started


if __name__ == "__main__":
    main()
