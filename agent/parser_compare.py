"""What each parser can see on the same page, at the same moment.

    python parser_compare.py                         migros, all parsers
    python parser_compare.py --url ... --goal "..."

Three ways to turn one screen into text, run back to back on the same page so
the comparison is fair, and then the same Jev question asked of each so the
answer is not "which produced more rows" but "which produced rows a decision
could be made from". That is the only comparison that matters: a parser that
lists 200 things nobody can choose between is worse than one that lists 40
that name themselves.

  DOM       browser.snapshot() — the page's own accessibility information
  UIA       the Windows UI Automation tree for the browser window
  OCR       Florence-2 reading the pixels

The three are not interchangeable. DOM only exists inside a browser; UIA
works on any Windows app but sees a browser as one opaque pane unless the
renderer exposes its tree; OCR works on anything at all, including a game or
a remote desktop, and knows nothing about what is clickable.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import browser as browsermod   # noqa: E402
from core import fast, jev               # noqa: E402

GOAL = "search the site for milk (süt)"


def timed(label, fn):
    t0 = time.perf_counter()
    try:
        out = fn()
    except Exception as e:
        print(f"  {label:<10} FAILED after {(time.perf_counter()-t0)*1000:.0f}ms: "
              f"{type(e).__name__}: {e}")
        return None, 0.0
    return out, (time.perf_counter() - t0) * 1000


def dom_rows(b):
    snap = b.snapshot(limit=200)
    rows = []
    for e in snap.get("elements", []):
        n = (e.get("name") or "").strip()
        if n and not e.get("disabled"):
            rows.append({"type": e.get("role") or e.get("tag") or "",
                         "name": n[:70], "id": e.get("testid") or "",
                         "value": e.get("value") or ""})
    return rows


def uia_rows(title_part):
    from core import locator
    h = locator.find_window(title_contains=title_part)
    if not h:
        raise RuntimeError(f"no window matching {title_part!r}")
    rows, _ = fast.window_candidates(h, cap=200)
    return rows


def ocr_rows(region):
    """Florence-2 OCR over a screen region -> one row per line of text.

    Marked `type: "text"` because that is all OCR can honestly claim. It
    cannot tell a button from a heading, which is exactly the information a
    decision needs and the reason this tier is a last resort.
    """
    from pathlib import Path as P
    from huggingface_hub import snapshot_download
    from transformers import AutoProcessor, AutoModelForCausalLM
    import torch, mss
    from PIL import Image

    md = P(snapshot_download("microsoft/OmniParser-v2.0"))
    proc = AutoProcessor.from_pretrained("microsoft/Florence-2-base-ft",
                                         trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(str(md / "icon_caption"),
                                                 trust_remote_code=True).eval()
    with mss.mss() as s:
        shot = s.grab(region)
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    inp = proc(text="<OCR>", images=img, return_tensors="pt")
    with torch.inference_mode():
        ids = model.generate(input_ids=inp["input_ids"],
                             pixel_values=inp["pixel_values"],
                             max_new_tokens=400, num_beams=1)
    text = proc.batch_decode(ids, skip_special_tokens=True)[0]
    parts = [p.strip() for p in text.replace("  ", "\n").split("\n") if p.strip()]
    return [{"type": "text", "name": p[:70], "id": "", "value": ""} for p in parts]


def ask(label, rows, client):
    """The same question, of each parser's output."""
    if not rows:
        print(f"  {label:<10} no rows — nothing to ask")
        return
    t0 = time.perf_counter()
    m = fast.decide(GOAL, rows, window="migros.com.tr", guard=False, client=client)
    dt = (time.perf_counter() - t0) * 1000
    tgt = rows[m.index]["name"] if m.index is not None else "-"
    ok = "ACT " if m.verdict.act else "HOLD"
    print(f"  {label:<10} {dt:5.0f}ms  {ok} op={m.operation:<11} "
          f"conf={m.verdict.confidence:.2f}  {tgt[:44]!r}")
    if not m.verdict.act:
        print(f"             {m.verdict.why}")


def main(argv):
    url = "https://www.migros.com.tr"
    if "--url" in argv:
        url = argv[argv.index("--url") + 1]

    b = browsermod.Browser(headless=False)   # headed: UIA needs a real window
    if not b.start():
        print("browser failed to start")
        return 1
    try:
        b.goto(url)
        time.sleep(4.0)

        print(f"\n{'=' * 74}\nWHAT EACH PARSER SEES  —  {url}\n{'=' * 74}")

        results = {}
        for label, fn in (("DOM", lambda: dom_rows(b)),
                          ("UIA", lambda: uia_rows("Migros")),
                          ("OCR", lambda: ocr_rows({"left": 0, "top": 0,
                                                    "width": 1600, "height": 900}))):
            rows, ms = timed(label, fn)
            if rows is None:
                continue
            results[label] = rows
            named = sum(1 for r in rows if r["name"])
            print(f"\n{label}  —  {len(rows)} rows in {ms:.0f}ms")
            for r in rows[:8]:
                v = f"  = {r['value'][:20]!r}" if r.get("value") else ""
                print(f"     {r['type'][:12]:<12} {r['name'][:52]}{v}")
            if len(rows) > 8:
                print(f"     … {len(rows) - 8} more")

        print(f"\n{'=' * 74}\nTHE SAME JEV QUESTION, ASKED OF EACH\n"
              f"goal: {GOAL}\n{'=' * 74}")
        c = jev.Client(backend="openrouter", timeout=20.0)
        if c.offline:
            print("  no OPENROUTER_API_KEY — skipping the decision half")
        else:
            c.prewarm()
            for label, rows in results.items():
                ask(label, rows, c)
            print(f"\n  {c.stats.report()}")
    finally:
        b.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
