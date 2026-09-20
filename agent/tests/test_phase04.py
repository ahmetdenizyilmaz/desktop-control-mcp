"""Portability, trust and learning — the properties that let a macro travel.

The end-to-end suite already proves a macro can be saved and replayed on this
machine. What it does not prove is the harder claim: that the saved file is safe
to hand to someone else, that it will resolve against *their* paths rather than
mine, and that trust cannot ride along inside it.

Every check here runs against a throwaway store, so the real macros/ directory
is untouched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cleanup import scratch_dir  # noqa: E402

from core import machine_profile as mp  # noqa: E402
from core.schema import (Action, Locator, LocatorType, Macro,  # noqa: E402
                         MacroStats, LocalState, Step, Trust,
                         PROMOTION_CLEAN_RUNS, reliability)
from core.store import Store, order_for, promote  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""),
          flush=True)
    return ok


def sample_macro() -> Macro:
    return Macro(
        name="Save the report",
        description="Export the open sheet into the downloads folder",
        steps=[
            Step(action=Action.CLICK, intent="open the File menu",
                 locators=[Locator(LocatorType.UIA_NAME_TYPE,
                                   {"name": "File", "type": "MenuItem"}),
                           Locator(LocatorType.UIA_AUTOMATION_ID,
                                   {"automation_id": "FileTabButton"}),
                           Locator(LocatorType.OCR_TEXT, {"text": "File"})]),
            Step(action=Action.TYPE, intent="enter the destination path",
                 args={"text": "{DOWNLOADS}\\report.xlsx"}),
            Step(action=Action.TYPE, intent="log in",
                 args={"text": "secret://site-pw"}),
        ],
        requires_symbols=["DOWNLOADS"],
        requires_capabilities=["excel"],
        requires_secrets=["site-pw"],
        permissions=["input", "files:{DOWNLOADS}"],
        author="test",
    )


def main() -> int:
    print("=" * 72)
    print("PORTABILITY, TRUST AND LEARNING")
    print("=" * 72)

    tmp = scratch_dir("agent_store_")
    st = Store(tmp)
    m = sample_macro()

    print("\n1. round-trip through disk")
    st.save(m, LocalState(macro_id=m.id))
    m2, l2 = st.load(m.id)
    check("macro survives save/load", m2.name == m.name and len(m2.steps) == 3)
    check("step intent is preserved", m2.steps[0].intent == "open the File menu",
          "intent is what improvisation falls back on — losing it loses recovery")
    check("locators survive in order",
          [x.type for x in m2.steps[0].locators] == [x.type for x in m.steps[0].locators])
    check("locator values survive",
          m2.steps[0].locators[0].value["name"] == "File")
    check("enum fields come back as enums",
          isinstance(m2.steps[0].action, Action) and isinstance(l2.trust, Trust),
          f"{type(m2.steps[0].action).__name__}/{type(l2.trust).__name__}")

    print("\n2. the shareable file is actually shareable")
    raw = (tmp / m.id / "macro.json").read_text(encoding="utf-8")
    doc = json.loads(raw)
    user = Path.home().name
    check("no absolute user path in the file", user.lower() not in raw.lower(),
          f"searched for {user!r}")
    check("no drive letter path", "C:\\\\" not in raw and "C:/" not in raw)
    check("uses a symbol instead of a path", "{DOWNLOADS}" in raw)
    check("no secret value, only a reference",
          "secret://site-pw" in raw and "requires_secrets" in doc)
    for banned in ("trust", "locator_stats", "stats", "adaptation_log",
                   "machine", "hostname"):
        check(f"shareable half carries no {banned!r}", banned not in doc)
    check("declares its blast radius up front", doc["permissions"],
          str(doc["permissions"]))

    print("\n3. export writes only the shareable half")
    out = tmp / "shared.json"
    st.export(m.id, out)
    shared = json.loads(out.read_text(encoding="utf-8"))
    check("export omits local state", "trust" not in shared and "stats" not in shared,
          "nothing to scrub — the split does it by construction")
    check("export keeps the steps", len(shared["steps"]) == 3)

    print("\n4. an imported macro lands untrusted")
    # Give the local half a spotless record, then export and re-import: trust
    # must not survive the trip, or a published macro could declare itself safe.
    good = LocalState(macro_id=m.id, trust=Trust.TRUSTED,
                      stats=MacroStats(runs=9, successes=9))
    st.save(m, good)
    st.export(m.id, out)
    st.delete(m.id)
    new_id = st.import_macro(out, origin="from-friend")
    _, li = st.load(new_id)
    check("imported trust is DRAFT", li.trust is Trust.DRAFT, li.trust.value)
    check("imported run history is empty", li.stats.runs == 0 and li.stats.successes == 0,
          "trust is earned per machine, never inherited from a file")
    check("origin is recorded", "from-friend" in (li.origin or ""), li.origin or "")

    print("\n5. symbols resolve against THIS machine")
    profile = {"symbols": {"DOWNLOADS": r"D:\Users\someone\Downloads",
                           "EXCEL": r"D:\Office\excel.exe"},
               "capabilities": ["excel"]}
    got = mp.resolve("{DOWNLOADS}\\report.xlsx", profile)
    check("symbol is substituted", got == r"D:\Users\someone\Downloads\report.xlsx", got)
    check("text without symbols is untouched",
          mp.resolve("plain text", profile) == "plain text")
    try:
        mp.resolve("{NO_SUCH_SYMBOL}\\x", profile)
        check("unknown symbol raises rather than guessing", False,
              "silently leaving {NO_SUCH_SYMBOL} would type it literally")
    except mp.MissingSymbol as e:
        check("unknown symbol raises rather than guessing", True, str(e)[:50])

    real = mp.load_profile()
    check("this machine has a real DOWNLOADS symbol",
          bool(real.get("symbols", {}).get("DOWNLOADS")),
          real.get("symbols", {}).get("DOWNLOADS", "(missing)"))

    print("\n6. requirements are checked before running, not during")
    ok, missing = mp.check_requirements(["DOWNLOADS"], ["excel"], profile)
    check("satisfiable macro passes preflight", ok, str(missing))
    ok2, missing2 = mp.check_requirements(["DOWNLOADS", "ONEDRIVE"],
                                          ["excel", "photoshop"], profile)
    check("missing symbol is reported", not ok2 and any("ONEDRIVE" in x for x in missing2),
          str(missing2))
    check("missing capability is reported", any("photoshop" in x for x in missing2),
          "better to say 'you don't have Photoshop' than to fail on step 7")

    print("\n7. reliability is smoothed, so one lucky hit is not proof")
    check("an untried locator is 0.5", reliability(0, 0) == 0.5)
    check("one hit beats no data", reliability(1, 1) > reliability(0, 0))
    check("one hit is not certainty", reliability(1, 1) < 0.7,
          f"{reliability(1, 1):.2f} — Laplace smoothing keeps it honest")
    check("many hits approach certainty", reliability(50, 50) > 0.95)
    check("a total failure sinks below the prior", reliability(10, 0) < 0.1)
    check("more evidence moves it further",
          reliability(20, 20) > reliability(5, 5))

    print("\n8. the machine reorders locators from its own experience")
    loc = LocalState(macro_id="x")
    # Locator 0 is the author's first choice but fails here; locator 2 works.
    for _ in range(6):
        loc.record(0, 0, False)
        loc.record(0, 2, True)
    order = order_for(loc, 0, 3)
    check("the proven locator goes first", order[0] == 2, str(order))
    check("the failing locator goes last", order[-1] == 0, str(order))
    check("the untried one sits in the middle", order[1] == 1, str(order))
    fresh = order_for(LocalState(macro_id="y"), 0, 3)
    check("with no data the author's order is kept", fresh == [0, 1, 2], str(fresh))

    print("\n9. the trust ladder")
    l = LocalState(macro_id="t")
    check("starts as DRAFT", l.trust is Trust.DRAFT)
    l.stats.runs, l.stats.successes = 1, 1
    check("one clean run reaches SUPERVISED", promote(l) is Trust.SUPERVISED,
          l.trust.value)
    l.stats.successes = PROMOTION_CLEAN_RUNS - 1
    check(f"{PROMOTION_CLEAN_RUNS - 1} clean runs is not yet TRUSTED",
          promote(l) is Trust.SUPERVISED, l.trust.value)
    l.stats.successes = PROMOTION_CLEAN_RUNS
    check(f"{PROMOTION_CLEAN_RUNS} clean runs reaches TRUSTED",
          promote(l) is Trust.TRUSTED, l.trust.value)
    l.stats.failures = 1
    check("any failure demotes a TRUSTED macro", promote(l) is Trust.SUPERVISED,
          "back under observation until it proves itself again")
    q = LocalState(macro_id="q", trust=Trust.QUARANTINED,
                   stats=MacroStats(runs=20, successes=20))
    check("quarantine is not escapable by good behaviour",
          promote(q) is Trust.QUARANTINED,
          "a quarantined macro was stopped for a reason a success cannot answer")

    print("\n10. listing summarises without opening every macro")
    st.save(m, LocalState(macro_id=m.id, trust=Trust.SUPERVISED,
                          stats=MacroStats(runs=4, successes=3, failures=1)))
    rows = {r["id"]: r for r in st.list()}
    row = rows.get(m.id)
    check("listed", row is not None)
    if row:
        check("shows trust", row["trust"] == "supervised", str(row["trust"]))
        check("shows success rate", row["success_rate"] == 0.75, str(row["success_rate"]))
        check("shows step count", row["steps"] == 3)
        check("shows what it needs", row["requires"] == ["excel"], str(row["requires"]))
    bad = tmp / "corrupt"
    bad.mkdir()
    (bad / "macro.json").write_text("{ not json", encoding="utf-8")
    check("a corrupt macro does not break the list",
          isinstance(st.list(), list) and m.id in {r["id"] for r in st.list()},
          "one bad file must not hide every good one")

    print("\n11. delete")
    check("delete removes it", st.delete(m.id) and not st.path(m.id).exists())
    check("delete of a missing id is False", st.delete("nope") is False)

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
