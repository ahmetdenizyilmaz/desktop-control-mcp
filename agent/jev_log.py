"""Read back what was asked of Jev, and what came back.

    python jev_log.py                      the default journal, summarised
    python jev_log.py --file x.jsonl       another one
    python jev_log.py --full               every option's probability
    python jev_log.py --state              the state that was sent, too
    python jev_log.py --only op,pick       just these questions
    python jev_log.py --unsure 0.8         only calls where something was
                                           below this confidence

Jev returns no rationale, so when a decision looks wrong the distribution is
the whole story: which option nearly won, how close it was, and what the
model was shown at the time. `--unsure` is the one to reach for first — it
filters a long run down to the handful of moments the loop hesitated, which
is where the interesting mistakes live.

Written by `core/jev.py` when `JEV_LOG` is set, one JSON object per line.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

DEFAULT = Path(__file__).resolve().parent / "logs" / "jev.jsonl"


def conf_of(a: dict) -> float:
    """One scale for all three primitives, as `fast.py` gates them."""
    if a.get("type") == "noul":
        return abs(float(a.get("noul", 0.5)) - 0.5) * 2.0
    return float(a.get("confidence", 0.0))


def show(rec: dict, full: bool, state: bool, only: set[str]) -> None:
    head = (f"{rec.get('t', '')}  {rec.get('latency_ms', 0)}ms  "
            f"{rec.get('model', '')}  "
            f"{rec.get('usage', {}).get('input_tokens', 0)} tok")
    print(f"\n{'=' * 78}\n{head}")
    if rec.get("error"):
        print(f"  ERROR: {rec['error']}")

    if state:
        s = rec.get("state")
        txt = json.dumps(s, ensure_ascii=False, indent=2) if not isinstance(s, str) else s
        print("  --- state sent ---")
        for line in txt.splitlines():
            print(f"    {line}")

    qs = rec.get("questions") or {}
    for key, a in (rec.get("answers") or {}).items():
        if only and key not in only:
            continue
        kind = a.get("type")
        q = qs.get(key) or {}
        instr = q.get("instructions")
        if isinstance(instr, dict):
            instr = " / ".join(f"{k}: {v}" for k, v in instr.items())

        if kind == "noul":
            print(f"  [{key}] noul  p={a.get('noul'):.3f}  "
                  f"(confidence {conf_of(a):.2f})")
        elif kind == "choice":
            probs = a.get("probabilities") or {}
            top = sorted(probs.items(), key=lambda kv: -kv[1])
            shown = top if full else top[:4]
            print(f"  [{key}] choice -> {a.get('choice')!r}  "
                  f"conf={a.get('confidence', 0):.2f}")
            for opt, p in shown:
                if p <= 0 and not full:
                    continue
                bar = "#" * int(round(p * 24))
                print(f"        {opt[:26]:<26} {p:5.3f} {bar}")
            if not full and len(top) > len(shown):
                print(f"        … {len(top) - len(shown)} more options")
        elif kind == "score":
            leg = a.get("legend") or {}
            print(f"  [{key}] score  {a.get('score', 0):.2f}  "
                  f"conf={a.get('confidence', 0):.2f}")
            for lvl, p in sorted((a.get("probabilities") or {}).items()):
                print(f"        {lvl} {str(leg.get(lvl, ''))[:24]:<24} {p:5.3f}")
        if instr:
            print(f"        asked: {str(instr)[:150]}")


def main(argv: list[str]) -> int:
    path = DEFAULT
    full = "--full" in argv
    state = "--state" in argv
    only: set[str] = set()
    unsure = 0.0
    if "--file" in argv:
        path = Path(argv[argv.index("--file") + 1])
    if "--only" in argv:
        only = {s.strip() for s in argv[argv.index("--only") + 1].split(",")}
    if "--unsure" in argv:
        unsure = float(argv[argv.index("--unsure") + 1])

    if not path.exists():
        print(f"no journal at {path}\nRun with JEV_LOG set, e.g.\n"
              f"  $env:JEV_LOG = '{DEFAULT}'")
        return 1

    recs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    kept = recs
    if unsure:
        kept = [r for r in recs
                if any(conf_of(a) < unsure for a in (r.get("answers") or {}).values())]

    for r in kept:
        show(r, full, state, only)

    tok = sum((r.get("usage") or {}).get("input_tokens", 0) for r in recs)
    lat = [r.get("latency_ms", 0) for r in recs if r.get("latency_ms")]
    lat.sort()
    print(f"\n{'=' * 78}")
    print(f"{len(kept)} shown of {len(recs)} calls  |  {tok} input tokens  |  "
          f"${tok / 1_000_000 * 0.042:.6f}  |  "
          f"p50 {lat[len(lat) // 2] if lat else 0}ms")
    print(f"journal: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
