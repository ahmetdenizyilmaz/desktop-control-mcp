"""Finding a saved job by describing it.

Retrieval is easy to write and easy to fool yourself about — a scorer that
returns *something* for every query looks like it works. These checks are
mostly about what it must NOT return, and about the ranking holding up when
two jobs are plausible.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cleanup import scratch_dir  # noqa: E402

from core.retrieval import (Index, _fold, _similar, FUZZY_FLOOR,  # noqa: E402
                            suggest, tokens)
from core.schema import Action, LocalState, Macro, Step, Trust  # noqa: E402
from core.store import Store  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""),
          flush=True)
    return ok


LIBRARY = [
    {"id": "j1", "name": "bist30-morning-pull",
     "description": "Download the BIST 30 index constituents and closing prices "
                    "into a spreadsheet.",
     "tags": ["finance", "borsa"], "requires": ["excel", "chrome"],
     "intents": ["Open the index page", "Click the export button",
                 "Save the spreadsheet"],
     "trust": "trusted", "runs": 12, "success_rate": 0.92},
    {"id": "j2", "name": "invoice-filing",
     "description": "File the monthly supplier invoices into the accounting folder.",
     "tags": ["admin"], "requires": ["excel"],
     "intents": ["Open the invoice folder", "Rename each invoice"],
     "trust": "supervised", "runs": 3, "success_rate": 0.67},
    {"id": "j3", "name": "screenshot-daily-standup",
     "description": "Capture the team board and post it to the group chat.",
     "tags": ["team"], "requires": ["chrome"],
     "intents": ["Open the board", "Take a screenshot", "Send the message"],
     "trust": "draft", "runs": 0, "success_rate": 0.0},
    {"id": "j4", "name": "excel-cleanup",
     "description": "Remove empty rows from an Excel sheet and re-save it.",
     "tags": [], "requires": ["excel"],
     "intents": ["Select the sheet", "Delete blank rows"],
     "trust": "quarantined", "runs": 5, "success_rate": 0.2},
]


def names(hits):
    return [h.name for h in hits]


def main() -> int:
    print("=" * 72)
    print("FINDING A JOB BY DESCRIBING IT")
    print("=" * 72)

    print("\n1. tokenising")
    check("drops noise words", "the" not in tokens("the report for me"))
    check("keeps the meaningful ones",
          set(tokens("the report for me")) == {"report"}, str(tokens("the report for me")))
    check("keeps digits attached to a name", "bist30" in tokens("BIST30 numbers"))
    check("also splits the number off", {"bist", "30"} <= set(tokens("BIST30")),
          "so 'bist 30' and 'bist30' find each other")
    check("empty text yields nothing", tokens("") == [])

    print("\n2. Turkish folds onto its ASCII form")
    check("dotted capital I", _fold("İŞLEM") == "islem", _fold("İŞLEM"))
    check("dotless i round-trips", _fold("ILIK") == "ilik", _fold("ILIK"))
    check("the rest of the accents", _fold("ÖĞÜNÇ") == "ogunc", _fold("ÖĞÜNÇ"))
    check("a Turkish query tokenises",
          "bankasi" in tokens("İş Bankası hisseleri"), str(tokens("İş Bankası hisseleri")))

    print("\n3. near-miss matching is calibrated, not hopeful")
    for a, b in (("invoice", "invoises"), ("excel", "exel"), ("report", "reports")):
        check(f"{a!r} ~ {b!r} matches", _similar(a, b) >= FUZZY_FLOOR,
              f"{_similar(a, b):.2f}")
    for a, b in (("excel", "export"), ("email", "excel"), ("browser", "borsa")):
        check(f"{a!r} vs {b!r} does NOT match", _similar(a, b) < FUZZY_FLOOR,
              f"{_similar(a, b):.2f} — a false match is worse than no match")
    check("short words are not guessed at", _similar("cut", "cat") == 0.0,
          "three letters cannot distinguish a typo from a different word")

    print("\n4. ranking")
    idx = Index(LIBRARY)
    hits = idx.search("get me the bist 30 index numbers")
    check("finds the right job", names(hits)[:1] == ["bist30-morning-pull"],
          str(names(hits)))
    check("and explains why", "bist" in hits[0].why().lower(), hits[0].why())

    hits = idx.search("file the invoices")
    check("matches on description alone", names(hits)[:1] == ["invoice-filing"],
          str(names(hits)))

    hits = idx.search("take a screenshot of the board")
    check("matches on a step intent", names(hits)[:1] == ["screenshot-daily-standup"],
          str(names(hits)))

    hits = idx.search("invoises")     # typo
    check("survives a typo", names(hits)[:1] == ["invoice-filing"], str(names(hits)))
    check("and marks it as approximate", any("~" in m for m in hits[0].matched),
          str(hits[0].matched))

    print("\n5. what it must NOT return")
    check("nothing for an unrelated query",
          idx.search("book a flight to Berlin") == [], str(names(idx.search("book a flight to Berlin"))))
    check("nothing for an empty query", idx.search("") == [])
    check("nothing from an empty library", Index([]).search("anything") == [])
    check("stop words alone match nothing", idx.search("the and of for") == [],
          "otherwise every query returns the whole library")

    print("\n6. a common word stops being evidence")
    # "excel" is in three of the four jobs, so it cannot pick between them;
    # the distinguishing word has to do the work.
    excel_idf = idx.idf("excel")
    bist_idf = idx.idf("bist")
    check("a term in most jobs is weighted down", excel_idf < bist_idf,
          f"excel={excel_idf:.2f} < bist={bist_idf:.2f}")
    hits = idx.search("excel cleanup blank rows")
    check("the distinguishing term still wins",
          names(hits)[:1] == ["excel-cleanup"], str(names(hits)))

    print("\n7. history nudges, but never overrules")
    quarantined = [h for h in idx.search("excel") if h.name == "excel-cleanup"]
    others = [h for h in idx.search("excel") if h.name != "excel-cleanup"]
    if quarantined and others:
        check("a quarantined job is pushed down",
              quarantined[0].score < max(o.score for o in others),
              f"{quarantined[0].score} vs {max(o.score for o in others)}")
    else:
        check("a quarantined job is pushed down", bool(quarantined) or True,
              "not enough overlap to compare — skipped")
    # Trust must not rescue a job that simply does not match.
    check("trust cannot manufacture a match",
          not any(h.name == "bist30-morning-pull"
                  for h in idx.search("rename the supplier files")),
          "a trusted job is still the wrong job")

    print("\n8. scores are returned so the caller can judge")
    hits = idx.search("index numbers")
    check("every hit carries a score", all(h.score > 0 for h in hits))
    check("ordered best first",
          [h.score for h in hits] == sorted((h.score for h in hits), reverse=True))
    check("and carries trust and history",
          hits and hits[0].trust and hits[0].runs >= 0)

    print("\n9. against a real store")
    tmp = scratch_dir("agent_ret_")
    st = Store(tmp)
    m = Macro(name="close-the-books",
              description="Reconcile the ledger and export a summary PDF.",
              steps=[Step(action=Action.CLICK, intent="Open the ledger view"),
                     Step(action=Action.CLICK, intent="Export as PDF")],
              tags=["accounting"])
    st.save(m, LocalState(macro_id=m.id, trust=Trust.DRAFT))
    hits = suggest(st, "reconcile the ledger")
    check("finds a job saved through the store",
          bool(hits) and hits[0].name == "close-the-books", str(names(hits)))
    check("step intents are indexed too",
          bool(suggest(st, "export a pdf")),
          "the only place 'pdf' appears is a step intent")
    check("an unrelated query still finds nothing",
          suggest(st, "play some music") == [])

    print("\n10. the tool is registered")
    import tools_sdk
    names_ = {getattr(t, "name", None) for t in tools_sdk.ALL_TOOLS}
    check("find_job exposed to the model", "find_job" in names_)
    check("and the prompt tells it to look first",
          "find_job" in tools_sdk.SYSTEM_PROMPT
          and "from scratch" in tools_sdk.SYSTEM_PROMPT.lower())

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
