"""Record, then run ONE script: music, shop, WhatsApp, 'buy', checkout, SMS.

    python jobs/errand.py [script-name]

The whole task is the script `errands/music-shop-checkout`; this file only
wraps it in what a script cannot do for itself - a screen recording with
the computer's sound (capped at 5 minutes), the drawn cursor, and the two
models: Jev for choosing products, Opus (via the Claude Code CLI) for a
step that is stuck or a page nobody scripted.

Payment is gated in the runner, not here: the commit guard refuses every
checkout-like click until a `wait_message` with `authorizes` has seen the
person write "buy" in their own chat.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "agent"))
sys.stdout.reconfigure(encoding="utf-8")

from cursor import Cursor, hidden_cursor                      # noqa: E402
from record import Recorder                                   # noqa: E402
from script import DesktopActuator, Library, Runner           # noqa: E402
from script.helper import OpusHelper                          # noqa: E402
from vision.elements import Watcher                           # noqa: E402

RECORD_MAX_S = 300


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "errands/music-shop-checkout"
    started = time.time()

    def log(msg: str) -> None:
        print(f"[{time.time() - started:6.1f}s] {msg}", flush=True)

    def on_step(o) -> None:
        mark = "ok " if o.ok else ("-- " if o.skipped else "XX ")
        log(f"{mark}{o.where:<58} {o.detail[:110]}")

    rec = Recorder(max_s=RECORD_MAX_S)
    rec.start()
    log(f"recording (max {RECORD_MAX_S}s) -> {rec.out.name}")
    cur = Cursor(hide_from_capture=False)     # visible in the video
    run = None
    try:
        with hidden_cursor():
            cur.start()
            lib = Library()
            run = Runner(lib.resolve(name), Watcher(hwnd=None),
                         DesktopActuator(), on_step=on_step, cursor=cur,
                         helper=OpusHelper(log=log))
            run.run()
            cur.stop()
    finally:
        out = rec.stop()
        log(f"recording: {out} ({out.stat().st_size / 1e6:.1f} MB, "
            f"audio={rec.audio_ok})")
    rep = run.report
    print("\n" + rep.summary())
    print(f"stopped by: {rep.stopped_by or '-'} | authorized: "
          f"{run.authorized or 'no'} | opus calls: {run.helper.calls} | "
          f"jev picks: {run.chose}")
    print("\nBOUGHT (exact):")
    for label, qty in rep.bought.items():
        print(f"  {qty} x {label}")
    for r in rep.rescued:
        print(f"  rescued: {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
