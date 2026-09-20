"""Macro + locator schema — the portable contract.

Design rule: a macro NEVER contains a machine-specific value.
  * no absolute paths      -> symbols like {DOWNLOADS}, {APP:excel}
  * no pixel coordinates   -> locator stacks resolved at replay time
  * no secrets             -> secret:// references resolved from DPAPI

That is what lets the same macro run on another PC, at another screen
size, with another theme.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Trust — how much supervision a macro needs (see Trust ladder below)
# ---------------------------------------------------------------------------

class Trust(str, Enum):
    DRAFT = "draft"            # just recorded, never replayed
    SUPERVISED = "supervised"  # replay step-by-step, verify each, narrate
    TRUSTED = "trusted"        # replay unattended
    QUARANTINED = "quarantined"  # failed badly; needs human review


# ---------------------------------------------------------------------------
# Locators — ordered stack, cheapest + most stable first
# ---------------------------------------------------------------------------

class LocatorType(str, Enum):
    # --- desktop -----------------------------------------------------------
    UIA_AUTOMATION_ID = "uia_automation_id"  # most stable: dev-assigned id
    UIA_NAME_TYPE = "uia_name_type"          # Name + ControlType
    UIA_PATH = "uia_path"                    # structural path in the tree
    WIN32_CLASS = "win32_class"              # class name + child index
    RELATIVE = "relative"                    # offset from an anchor element
    OCR_TEXT = "ocr_text"                    # text match via OCR
    VISION = "vision"                        # icon/caption match (last resort)
    NORM_COORD = "norm_coord"                # fraction of window, never screen
    # --- web ---------------------------------------------------------------
    WEB_ROLE = "web_role"                    # ARIA role + accessible name
    WEB_TESTID = "web_testid"                # data-testid / data-test
    WEB_LABEL = "web_label"                  # associated <label> text
    WEB_CSS = "web_css"
    WEB_XPATH = "web_xpath"
    WEB_TEXT = "web_text"


#: Preference order used when recording. Earlier = tried first at replay.
LOCATOR_PRIORITY: list[LocatorType] = [
    LocatorType.UIA_AUTOMATION_ID,
    LocatorType.WEB_TESTID,
    LocatorType.WEB_ROLE,
    LocatorType.UIA_NAME_TYPE,
    LocatorType.WEB_LABEL,
    LocatorType.UIA_PATH,
    LocatorType.WEB_CSS,
    LocatorType.WIN32_CLASS,
    LocatorType.RELATIVE,
    LocatorType.WEB_TEXT,
    LocatorType.OCR_TEXT,
    LocatorType.VISION,
    LocatorType.WEB_XPATH,
    LocatorType.NORM_COORD,
]


@dataclass
class Locator:
    """Pure, shareable identity. Deliberately carries NO usage data.

    Hit/miss statistics live in the local sidecar (LocalState), because a macro
    published to a shared database must not carry how often you ran it or which
    route worked on your machine.
    """
    type: LocatorType
    value: dict[str, Any]


def reliability(tries: int, hits: int) -> float:
    """Laplace-smoothed hit rate — untried locators sort mid-pack, not last."""
    return (hits + 1) / (tries + 2)


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

class Action(str, Enum):
    LAUNCH = "launch"
    FOCUS_WINDOW = "focus_window"
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    TYPE = "type"
    KEY = "key"
    SCROLL = "scroll"
    DRAG = "drag"
    WAIT_FOR = "wait_for"
    NAVIGATE = "navigate"      # web: open a url
    WEB_CLICK = "web_click"    # web: click by DOM selector
    WEB_FILL = "web_fill"      # web: type or choose, by DOM selector
    ASK_USER = "ask_user"      # pause, prompt (app or Telegram)
    RUN_MACRO = "run_macro"    # composition


@dataclass
class Verify:
    """What must be true after a step. A step without this fails silently."""
    appears: list[Locator] = field(default_factory=list)
    disappears: list[Locator] = field(default_factory=list)
    window_title_regex: str | None = None
    url_regex: str | None = None
    timeout_s: float = 8.0


@dataclass
class Step:
    action: Action
    #: plain-language purpose — what improvisation falls back on when
    #: every locator fails. This is the single most important field.
    intent: str
    locators: list[Locator] = field(default_factory=list)
    #: literal payload: text to type, keys, url, symbol refs, secret:// refs
    args: dict[str, Any] = field(default_factory=dict)
    verify: Verify | None = None
    optional: bool = False          # failure here does not fail the macro
    timeout_s: float = 15.0


# ---------------------------------------------------------------------------
# Macro
# ---------------------------------------------------------------------------

@dataclass
class Param:
    name: str
    type: str = "string"            # string | path | number | secret | choice
    prompt: str = ""
    default: Any = None
    choices: list[str] | None = None


@dataclass
class MacroStats:
    runs: int = 0
    successes: int = 0
    failures: int = 0
    improvisations: int = 0
    last_run: float | None = None
    last_ok: float | None = None
    mean_duration_s: float | None = None

    @property
    def success_rate(self) -> float:
        return self.successes / self.runs if self.runs else 0.0


@dataclass
class TargetApp:
    """What this macro was built against — lets a downloader check fit."""
    name: str                       # "excel" | "chrome" | "colab.research.google.com"
    kind: str = "app"               # app | website
    version_min: str | None = None
    version_seen: str | None = None  # what the author actually recorded on


@dataclass
class Macro:
    """SHAREABLE half. Safe to publish to a macro database as-is.

    Contains no paths, no coordinates, no secrets, no usage history, and no
    machine fingerprint. Everything machine-specific is a {SYMBOL} and
    everything sensitive is a secret:// reference resolved locally.
    """
    #: short natural-language purpose. Drives retrieval AND improvisation —
    #: when locators fail, this is what Claude is trying to accomplish.
    description: str
    name: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    version: int = 1
    schema_version: int = 1

    steps: list[Step] = field(default_factory=list)
    params: list[Param] = field(default_factory=list)

    #: symbols this macro needs resolved on the target machine
    requires_symbols: list[str] = field(default_factory=list)
    #: capability flags a machine must have (e.g. "excel", "chrome")
    requires_capabilities: list[str] = field(default_factory=list)
    #: secret:// keys the macro references (values never stored here)
    requires_secrets: list[str] = field(default_factory=list)
    #: what the agent is permitted to do — shown to a downloader BEFORE
    #: first run, so an untrusted macro declares its blast radius up front
    permissions: list[str] = field(default_factory=list)  # e.g. "input", "files:{DOWNLOADS}", "net"

    target_apps: list[TargetApp] = field(default_factory=list)
    author: str = ""                # freeform handle; never auto-filled from OS user
    license: str = ""
    tags: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class LocalState:
    """LOCAL half. Never shared, never published.

    Keyed alongside a Macro by id. Holds everything that is either sensitive
    (usage, machine identity) or meaningless elsewhere (resolved pixels).
    """
    macro_id: str
    trust: Trust = Trust.DRAFT
    stats: MacroStats = field(default_factory=MacroStats)

    #: "step_idx:locator_idx" -> {"tries": n, "hits": n}
    locator_stats: dict[str, dict[str, int]] = field(default_factory=dict)

    #: what Claude changed to make a downloaded macro work on THIS machine
    adaptation_log: list[dict] = field(default_factory=list)

    #: recording artefacts — frames, resolved coordinates, transcripts
    artifact_dir: str | None = None
    machine_fingerprint: str = ""
    origin: str = "local"           # local | imported:<source>

    def rank(self, step_idx: int, locator_idx: int) -> float:
        s = self.locator_stats.get(f"{step_idx}:{locator_idx}", {})
        return reliability(s.get("tries", 0), s.get("hits", 0))

    def record(self, step_idx: int, locator_idx: int, hit: bool) -> None:
        k = f"{step_idx}:{locator_idx}"
        s = self.locator_stats.setdefault(k, {"tries": 0, "hits": 0})
        s["tries"] += 1
        if hit:
            s["hits"] += 1


# ---------------------------------------------------------------------------
# Trust ladder — how a macro earns unattended execution
# ---------------------------------------------------------------------------
#
#   DRAFT ──record──> SUPERVISED ──2 clean runs──> TRUSTED
#                          ▲                          │
#                          └──────any failure─────────┘
#                                                     │
#                          QUARANTINED <──repeated failure / unsafe
#
# SUPERVISED: every step's verify must pass, Claude narrates, user sees result.
# TRUSTED:    runs unattended; watchdog still enforces per-step timeouts.
# A failure ALWAYS demotes. Trust is earned repeatedly, never permanently.

PROMOTION_CLEAN_RUNS = 2
