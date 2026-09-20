"""Scheduling — running work on a cadence.

Time code is where quiet, expensive bugs live: a schedule that fires an hour
late every day, or twice, or never again after the first run. Everything here
is checked against a fixed reference time rather than the wall clock, so the
suite gives the same answer at 3am as at noon.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cleanup import scratch_dir  # noqa: E402

from core.schedule import (Cadence, Schedule, Scheduler,  # noqa: E402
                           next_due, parse)

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""),
          flush=True)
    return ok


#: A Wednesday at 10:00 local. Every "next due" answer below is relative to it.
REF = datetime(2026, 8, 19, 10, 0, 0).timestamp()


def human(ts):
    return datetime.fromtimestamp(ts).strftime("%a %Y-%m-%d %H:%M") if ts else "—"


def main() -> int:
    print("=" * 72)
    print("SCHEDULING")
    print("=" * 72)
    print(f"   reference time: {human(REF)}")

    print("\n1. reading a time out of plain language")
    cases = [
        ("every morning at 9", Cadence.DAILY, "09:00", []),
        ("every day at 18:30", Cadence.DAILY, "18:30", []),
        ("weekdays at 8am", Cadence.WEEKLY, "08:00",
         ["monday", "tuesday", "wednesday", "thursday", "friday"]),
        ("monday and thursday at 7pm", Cadence.WEEKLY, "19:00",
         ["monday", "thursday"]),
        ("every 3 hours", Cadence.HOURLY, None, None),
    ]
    for text, cad, at, days in cases:
        r = parse(text)
        ok = r["cadence"] is cad and (at is None or r["at"] == at) \
            and (days is None or r["days"] == days)
        check(f"{text!r}", ok, f"{r['cadence'].value} {r['at']} {r['days']}")

    check("'every evening at 9' means 21:00, not 09:00",
          parse("every evening at 9")["at"] == "21:00",
          parse("every evening at 9")["at"])
    check("a pm hour is converted", parse("at 7pm")["at"] == "19:00")
    check("midnight is not turned into noon", parse("at 12am")["at"] == "00:00")

    print("\n2. Turkish reads the same")
    check("'her sabah' is every morning", parse("her sabah")["at"] == "09:00")
    check("'cuma 14:00' is Friday at two",
          parse("cuma 14:00")["days"] == ["friday"]
          and parse("cuma 14:00")["at"] == "14:00", str(parse("cuma 14:00")))
    check("'her akşam' is the evening", parse("her akşam")["at"] == "18:00",
          parse("her akşam")["at"])

    print("\n3. nonsense is refused, not guessed at")
    for junk in ("", "sometime", "whenever you feel like it", "soon"):
        check(f"{junk!r} is not understood", not parse(junk)["understood"],
              "a silently misparsed schedule fires at the wrong hour forever")

    print("\n4. when it next fires")
    later = Schedule("a", "t", Cadence.DAILY, at="18:00")
    nd = next_due(later, REF)
    check("later today stays today", human(nd).endswith("08-19 18:00"), human(nd))

    earlier = Schedule("b", "t", Cadence.DAILY, at="08:00")
    nd = next_due(earlier, REF)
    check("already past rolls to tomorrow", human(nd).endswith("08-20 08:00"),
          human(nd))

    weekly = Schedule("c", "t", Cadence.WEEKLY, at="09:00", days=["friday"])
    nd = next_due(weekly, REF)
    check("weekly finds the right weekday", human(nd).startswith("Fri"), human(nd))
    check("and it is in the future", nd > REF)

    multi = Schedule("d", "t", Cadence.WEEKLY, at="09:00",
                     days=["monday", "thursday"])
    nd = next_due(multi, REF)
    check("picks the nearest of several days", human(nd).startswith("Thu"),
          human(nd))

    hourly = Schedule("e", "t", Cadence.HOURLY, every_hours=3)
    nd = next_due(hourly, REF)
    check("hourly counts from now", abs(nd - (REF + 3 * 3600)) < 2, human(nd))

    off = Schedule("f", "t", Cadence.DAILY, at="09:00", enabled=False)
    check("a disabled schedule has no next time", next_due(off, REF) is None)

    once = Schedule("g", "t", Cadence.ONCE, at="18:00")
    check("a one-off has a first time", next_due(once, REF) is not None)
    once.runs = 1
    check("and never fires again after it runs", next_due(once, REF) is None,
          "otherwise 'once' silently becomes 'daily'")

    print("\n5. storing them")
    tmp = scratch_dir("agent_sched_") / "schedules.json"
    sch = Scheduler(tmp)
    s = sch.add("summarise unread mail", "weekdays at 8am")
    check("added", s.id in sch.items)
    check("cadence came from the phrase", s.cadence is Cadence.WEEKLY,
          s.describe())
    check("next time computed on add", s.next_due is not None, human(s.next_due))

    again = Scheduler(tmp)
    check("survives a restart", s.id in again.items,
          "a schedule that vanishes when the app closes is not a schedule")
    check("and keeps its cadence type",
          isinstance(again.items[s.id].cadence, Cadence)
          and again.items[s.id].cadence is Cadence.WEEKLY)

    check("can be disabled", sch.enable(s.id, False)
          and not sch.items[s.id].enabled)
    check("disabling clears the next time", sch.items[s.id].next_due is None)
    check("and re-enabled", sch.enable(s.id, True)
          and sch.items[s.id].next_due is not None)
    check("can be removed", sch.remove(s.id) and s.id not in sch.items)
    check("removing an unknown id is False", sch.remove("nope") is False)

    print("\n6. firing")
    fired = []
    sch2 = Scheduler(scratch_dir("agent_sched2_") / "s.json",
                     runner=lambda s: (fired.append(s.task), True)[1])
    a = sch2.add("do the thing", "every day at 09:00")
    sch2.items[a.id].next_due = time.time() - 5      # make it overdue

    due = sch2.due_now()
    check("an overdue schedule is due", [d.id for d in due] == [a.id])
    check("a future one is not",
          not any(d.id == b.id for d in sch2.due_now())
          if (b := sch2.add("later", "every day at 09:00")) else False)

    sch2.mark_ran(a.id, ok=True)
    check("running advances the clock", sch2.items[a.id].next_due > time.time(),
          human(sch2.items[a.id].next_due))
    check("and is not immediately due again", not any(
        d.id == a.id for d in sch2.due_now()),
        "otherwise one schedule fires every tick forever")
    check("the run is counted", sch2.items[a.id].runs == 1)

    print("\n7. a one-off retires itself")
    o = sch2.add("just this once", "tomorrow at noon")
    sch2.items[o.id].next_due = time.time() - 1
    sch2.mark_ran(o.id, ok=True)
    check("disabled after running", not sch2.items[o.id].enabled)
    check("and no longer due", not any(d.id == o.id for d in sch2.due_now()))

    print("\n8. missed while the app was closed")
    m = sch2.add("morning report", "every day at 09:00")
    sch2.items[m.id].next_due = time.time() - 7200          # two hours ago
    away = sch2.due_while_away()
    check("reported as missed", any(x.id == m.id for x in away),
          "the app cannot fire while it is closed; saying so beats pretending")
    before_runs = sch2.items[m.id].runs
    sch2.mark_missed(m.id)
    check("a miss is not counted as a run", sch2.items[m.id].runs == before_runs)
    check("the miss is recorded", sch2.items[m.id].misses == 1)
    check("and the clock moves on", sch2.items[m.id].next_due > time.time(),
          "otherwise it is reported as missed on every startup forever")

    print("\n9. a busy machine defers rather than dropping")
    busy = Scheduler(scratch_dir("agent_sched3_") / "s.json",
                     runner=lambda s: False)        # always "not now"
    d = busy.add("something", "every day at 09:00")
    busy.items[d.id].next_due = time.time() - 5
    still = busy.due_now()
    check("still due when the runner refused", any(x.id == d.id for x in still),
          "two tasks driving one desktop is what the arbiter exists to prevent")
    check("and not counted as run", busy.items[d.id].runs == 0)

    print("\n10. the app and the model can both reach it")
    import app as appmod
    import tools_sdk
    a2 = appmod.Agent()
    st = a2.get_state()
    check("state carries schedules", "schedules" in st and "missed" in st)
    r = a2.add_schedule("check the deploy", "every 2 hours")
    check("app can add one", r.get("ok"), str(r))
    if r.get("ok"):
        check("described back to the user", "2 hours" in r["when"], r["when"])
        check("app can remove it", a2.remove_schedule(r["id"])["ok"])
    check("app refuses an unparseable time",
          not a2.add_schedule("x", "whenever")["ok"])
    check("schedule_task exposed to the model",
          any(getattr(t, "name", "") == "schedule_task" for t in tools_sdk.ALL_TOOLS))

    print("\n" + "=" * 72)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"{passed}/{len(results)} passed")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name}  {detail}")
    print("=" * 72)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
