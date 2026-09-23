"""Record, play music, shop 20 products, and WhatsApp yourself the list.

    python jobs/shop_and_report.py

Everything that touches the machine runs through the pipeline's own runner
and scripts; this file only sequences them and passes one result across.

The interesting part is the hand-off. The script language has no variables
on purpose, so the shop cannot "return" what it bought to the WhatsApp
script. Instead the runner records what `pick` chose for each item
(`Report.picks`), and this job GENERATES the next script from it — a plain
JSON file with the exact product names as a literal `for_each`, written to
`scripts/generated/` where it can be read before or after it runs. Data in,
data out; nothing is evaluated.

"Buy" here means ADD TO BASKET. Checkout spends money, and the commit guard
would stop it for a human anyway.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "agent"))

from cursor import Cursor, hidden_cursor                      # noqa: E402
from record import Recorder                                   # noqa: E402
from script import DesktopActuator, Library, Runner           # noqa: E402
from vision.elements import Watcher                           # noqa: E402

GENERATED = ROOT / "script" / "scripts" / "generated"


def run(lib: Library, name: str, cur, log) -> "Runner":
    r = Runner(lib.resolve(name), Watcher(hwnd=None), DesktopActuator(),
               cursor=cur, on_step=lambda o: log.append((name, o)))
    t0 = time.time()
    r.run()
    first = r.report.summary().split("\n")
    print(f"  {name:<24} {time.time() - t0:6.1f}s  "
          f"{' | '.join(first[:2])}", flush=True)
    return r


def whatsapp_script(products: list[str]) -> Path:
    """The generated script: open your own chat, send each product."""
    GENERATED.mkdir(parents=True, exist_ok=True)
    doc = {
        "name": "whatsapp/send-bought",
        "version": 1,
        "description": (f"GENERATED {time.strftime('%Y-%m-%d %H:%M')} from "
                        f"the shop's picks: send each of the {len(products)} "
                        f"products added to the basket to 'Message yourself'."),
        "params": {},
        "trust": "draft",
        "steps": [
            {"do": "call", "script": "whatsapp/open-self", "with": {}},
            {"do": "repeat", "for_each": products, "as": "product",
             "steps": [{"do": "call", "script": "whatsapp/send-self",
                        "with": {"message": "{product}"}}]},
        ],
    }
    path = GENERATED / "whatsapp-send-bought.json"
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    return path


def main() -> int:
    log: list = []
    rec = Recorder()
    rec.start()
    print(f"recording -> {rec.out.name}", flush=True)
    # Captured on purpose this time: the video should show the cursor. The
    # vision code tolerates it for one run; normally it hides from capture.
    cur = Cursor(hide_from_capture=False)
    started = time.time()
    try:
        with hidden_cursor():
            cur.start()
            lib = Library()
            run(lib, "media/spotify-play", cur, log)
            shop = run(lib, "migros/big-shop", cur, log)

            order = [k for k in shop.r.entry.steps[1].for_each]
            picks = [shop.report.picks[k] for k in order
                     if shop.report.items.get(k) and k in shop.report.picks]
            print(f"  bought {len(picks)}/{len(order)} -> generating the "
                  f"WhatsApp script", flush=True)
            path = whatsapp_script(picks)
            lib.reload()
            wa = run(lib, "whatsapp/send-bought", cur, log)
            sent = sum(1 for v in wa.report.items.values() if v)
            print(f"  sent {sent}/{len(picks)} messages", flush=True)
            print(f"  generated script: {path}", flush=True)
            for k in order:
                print(f"    {k:<14} -> {shop.report.picks.get(k, '(not added)')}")
            cur.stop()
    finally:
        out = rec.stop()
        print(f"total {time.time() - started:.0f}s | recording: {out} "
              f"({out.stat().st_size / 1e6:.1f} MB, audio={rec.audio_ok})",
              flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
