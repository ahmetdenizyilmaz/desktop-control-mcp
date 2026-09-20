"""Running work on a cadence — "every morning at 9".

The watchdog notices a task that has *stalled*. It has nothing to say about
when a task should *start*. That is this.

**Why not the Windows Task Scheduler.** It is the obvious answer and it is the
wrong one here. A scheduled task launched by the service runs in whatever
session Windows feels like — often session 0, which has no desktop at all —
and this agent's entire job is to drive a desktop. It would fire, find no
windows, and report success on having done nothing. Owning the timer inside
the running app means work only ever starts where there is a screen to drive.

The cost is that nothing fires while the app is closed. That is the honest
trade, and `due_while_away()` makes it visible rather than silent: on startup
the app can see what it missed and offer to catch up.

Times are local and stored as text ("09:00"), not as offsets. If the user says
nine in the morning they mean nine in the morning on both sides of a daylight
saving change, and an absolute timestamp quietly stops meaning that.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path

from . import paths

SCHEDULE_FILE = paths.data("schedules.json")

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday",
        "saturday", "sunday"]
WEEKDAYS = set(DAYS[:5])


class Cadence(str, Enum):
    ONCE = "once"
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"


@dataclass
class Schedule:
    """One recurring piece of work.

    `task` is the plain-language instruction, exactly as it would be typed
    into the app — so a schedule is readable a year later, and editing it is
    editing a sentence.
    """
    id: str
    task: str
    cadence: Cadence = Cadence.DAILY
    at: str = "09:00"                       # HH:MM local, for daily/weekly
    days: list[str] = field(default_factory=list)   # weekly only
    every_hours: int = 1                    # hourly only
    enabled: bool = True

    job_id: str = ""                        # optional saved job instead of text
    created_at: float = field(default_factory=time.time)
    last_run: float | None = None
    last_ok: bool | None = None
    next_due: float | None = None
    runs: int = 0
    misses: int = 0                         # due while the app was closed

    def describe(self) -> str:
        if self.cadence is Cadence.ONCE:
            return f"once at {self.at}"
        if self.cadence is Cadence.HOURLY:
            return (f"every hour" if self.every_hours == 1
                    else f"every {self.every_hours} hours")
        if self.cadence is Cadence.WEEKLY:
            days = ", ".join(d[:3].title() for d in self.days) or "no days set"
            return f"{days} at {self.at}"
        return f"every day at {self.at}"


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

_TIME = re.compile(r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.I)
_EVERY_H = re.compile(r"\bevery\s+(\d+)?\s*(hour|hours|saat)\b", re.I)

#: Words that name a time of day when no clock time is given.
PARTS = {"morning": "09:00", "sabah": "09:00",
         "noon": "12:00", "öğlen": "12:00", "oglen": "12:00",
         "afternoon": "14:00", "evening": "18:00", "aksam": "18:00",
         "akşam": "18:00", "night": "22:00", "gece": "22:00"}

DAY_WORDS = {**{d: d for d in DAYS},
             **{d[:3]: d for d in DAYS},
             "pazartesi": "monday", "salı": "tuesday", "sali": "tuesday",
             "çarşamba": "wednesday", "carsamba": "wednesday",
             "perşembe": "thursday", "persembe": "thursday",
             "cuma": "friday", "cumartesi": "saturday", "pazar": "sunday"}


def parse(text: str) -> dict:
    """Turn "every morning at 9" into a cadence, without a full date parser.

    Returns the fields plus a `phrase` echoing what was understood, so the
    caller can show it back. Silently guessing at a schedule is worse than
    asking — a job that fires at the wrong hour every day is a slow annoyance
    that nobody traces back to a misparse.
    """
    t = (text or "").strip().lower()
    out: dict = {"cadence": Cadence.DAILY, "at": "09:00", "days": [],
                 "every_hours": 1, "understood": False}
    if not t:
        return out

    # --- hourly -------------------------------------------------------
    m = _EVERY_H.search(t)
    if m:
        out["cadence"] = Cadence.HOURLY
        out["every_hours"] = max(1, int(m.group(1) or 1))
        out["understood"] = True
        out["phrase"] = Schedule("", "", Cadence.HOURLY,
                                 every_hours=out["every_hours"]).describe()
        return out

    # --- named days ---------------------------------------------------
    found_days = []
    if "weekday" in t or "hafta içi" in t or "hafta ici" in t:
        found_days = DAYS[:5]
    elif "weekend" in t or "hafta sonu" in t:
        found_days = DAYS[5:]
    else:
        for word, day in DAY_WORDS.items():
            if re.search(rf"\b{re.escape(word)}\b", t) and day not in found_days:
                found_days.append(day)
    # Order them Monday-first rather than in the order they were mentioned.
    found_days.sort(key=DAYS.index)

    # --- time of day --------------------------------------------------
    at = None
    for word, clock in PARTS.items():
        if word in t:
            at = clock
            break
    m = _TIME.search(t)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ampm = (m.group(3) or "").lower()
        # A bare "at 9" alongside "evening" or "night" means 21:00, not 09:00.
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        elif not ampm and hour < 12 and any(w in t for w in
                                            ("evening", "night", "aksam",
                                             "akşam", "gece")):
            hour += 12
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            at = f"{hour:02d}:{minute:02d}"

    if at:
        out["at"] = at
    out["days"] = found_days
    if found_days and ("every" in t or "weekly" in t or "her" in t or True):
        out["cadence"] = Cadence.WEEKLY if len(found_days) < 7 else Cadence.DAILY
    out["understood"] = bool(at or found_days)

    if "once" in t or "tomorrow" in t or "yarin" in t or "yarın" in t:
        out["cadence"] = Cadence.ONCE
        out["understood"] = True

    tmp = Schedule("", "", out["cadence"], out["at"], out["days"])
    out["phrase"] = tmp.describe()
    return out


# ---------------------------------------------------------------------------
# when next
# ---------------------------------------------------------------------------

def next_due(s: Schedule, after: float | None = None) -> float | None:
    """The next moment this should fire, as a local timestamp."""
    now = datetime.fromtimestamp(after if after is not None else time.time())

    if not s.enabled:
        return None

    if s.cadence is Cadence.HOURLY:
        return (now + timedelta(hours=max(1, s.every_hours))).timestamp()

    try:
        hh, mm = (int(x) for x in s.at.split(":"))
    except Exception:
        hh, mm = 9, 0
    today = now.replace(hour=hh, minute=mm, second=0, microsecond=0)

    if s.cadence is Cadence.ONCE:
        # Already past today means it fires tomorrow; once it has run at all,
        # it never fires again.
        if s.runs:
            return None
        return (today if today > now else today + timedelta(days=1)).timestamp()

    if s.cadence is Cadence.WEEKLY and s.days:
        wanted = {DAYS.index(d) for d in s.days if d in DAYS}
        if not wanted:
            return None
        for ahead in range(0, 8):
            cand = today + timedelta(days=ahead)
            if cand.weekday() in wanted and cand > now:
                return cand.timestamp()
        return None

    return (today if today > now else today + timedelta(days=1)).timestamp()


# ---------------------------------------------------------------------------
# the store and the timer
# ---------------------------------------------------------------------------

class Scheduler:
    """Holds the schedules and fires them. One thread, one lock, no cron."""

    #: How often to look. A minute is plenty for hour-and-minute cadences and
    #: costs nothing; anything finer just burns wakeups.
    TICK_S = 30.0

    def __init__(self, path: Path | str = SCHEDULE_FILE, runner=None):
        self.path = Path(path)
        #: called as runner(schedule) when something is due. Set by the app.
        self.runner = runner
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.items: dict[str, Schedule] = {}
        self.load()

    # -- persistence -----------------------------------------------------
    def load(self) -> None:
        self.items = {}
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        for d in raw.get("schedules", []):
            try:
                d["cadence"] = Cadence(d.get("cadence", "daily"))
                self.items[d["id"]] = Schedule(**d)
            except Exception:
                continue

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(
            {"schedules": [asdict(s) for s in self.items.values()]},
            indent=2, default=str), encoding="utf-8")

    # -- editing ---------------------------------------------------------
    def add(self, task: str, when: str = "", job_id: str = "",
            **overrides) -> Schedule:
        import uuid
        spec = parse(when) if when else {}
        s = Schedule(
            id=uuid.uuid4().hex[:10],
            task=task.strip(),
            cadence=overrides.get("cadence", spec.get("cadence", Cadence.DAILY)),
            at=overrides.get("at", spec.get("at", "09:00")),
            days=overrides.get("days", spec.get("days", [])),
            every_hours=overrides.get("every_hours", spec.get("every_hours", 1)),
            job_id=job_id,
        )
        s.next_due = next_due(s)
        with self._lock:
            self.items[s.id] = s
            self.save()
        return s

    def remove(self, sid: str) -> bool:
        with self._lock:
            if sid in self.items:
                del self.items[sid]
                self.save()
                return True
        return False

    def enable(self, sid: str, on: bool = True) -> bool:
        with self._lock:
            s = self.items.get(sid)
            if not s:
                return False
            s.enabled = on
            s.next_due = next_due(s) if on else None
            self.save()
        return True

    # -- what is due -----------------------------------------------------
    def due_now(self, at: float | None = None) -> list[Schedule]:
        now = at if at is not None else time.time()
        out = []
        for s in self.items.values():
            if not s.enabled:
                continue
            if s.next_due is None:
                s.next_due = next_due(s, now)
                continue
            if s.next_due <= now:
                out.append(s)
        return out

    def due_while_away(self) -> list[Schedule]:
        """Schedules whose moment passed while the app was not running.

        Reported rather than fired: silently running six hours of missed work
        the instant the app opens is not what anyone means by "every morning".
        """
        now = time.time()
        return [s for s in self.items.values()
                if s.enabled and s.next_due is not None and s.next_due <= now]

    def mark_ran(self, sid: str, ok: bool = True) -> None:
        with self._lock:
            s = self.items.get(sid)
            if not s:
                return
            s.last_run = time.time()
            s.last_ok = ok
            s.runs += 1
            s.next_due = next_due(s)
            if s.cadence is Cadence.ONCE:
                s.enabled = False
            self.save()

    def mark_missed(self, sid: str) -> None:
        with self._lock:
            s = self.items.get(sid)
            if not s:
                return
            s.misses += 1
            s.next_due = next_due(s)
            self.save()

    # -- the timer -------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.TICK_S):
            try:
                for s in self.due_now():
                    if self.runner is None:
                        # Nothing to run it: count the miss and move the clock
                        # on, rather than firing the same one every tick.
                        self.mark_missed(s.id)
                        continue
                    started = False
                    try:
                        started = bool(self.runner(s))
                    except Exception:
                        started = False
                    if started:
                        self.mark_ran(s.id, ok=True)
                    else:
                        # Busy with something else. Try again on the next tick
                        # rather than dropping the occurrence entirely.
                        pass
            except Exception:
                continue

    def status(self) -> list[dict]:
        out = []
        for s in sorted(self.items.values(), key=lambda x: (x.next_due or 0)):
            out.append({
                "id": s.id, "task": s.task, "when": s.describe(),
                "enabled": s.enabled, "runs": s.runs, "misses": s.misses,
                "job_id": s.job_id,
                "next_due": s.next_due,
                "next_human": (datetime.fromtimestamp(s.next_due).strftime("%a %H:%M")
                               if s.next_due else "—"),
                "last_ok": s.last_ok,
            })
        return out


scheduler = Scheduler()
