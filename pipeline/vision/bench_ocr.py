"""How long does reading a screen actually take, and what drives it?

Single timings lied repeatedly while this package was being built: the same
full-screen read measured 164 ms once and 376 ms another time. Either the
cost depends on the content, or the measurement is noisy, and a benchmark
that does not separate those two is worse than no benchmark.

So each source is read N times and the spread is reported, not just a
median — and the same fixed images are used every run, so content is held
constant while noise is measured.

    python bench_ocr.py              saved images + the live screen
    python bench_ocr.py --runs 12
"""

from __future__ import annotations

import statistics as st
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image                                   # noqa: E402

from vision import ocr                                  # noqa: E402
from vision.capture import Frame, Screen, from_image    # noqa: E402

SHOTS = Path(__file__).resolve().parents[2] / "agent" / "logs" / "shots"


def scaled(frame: Frame, floor=(1920, 1080)) -> Frame:
    s = min(1.0, max(floor[0] / frame.width, floor[1] / frame.height))
    if s == 1.0:
        return frame
    img = frame.image.resize((round(frame.width * s), round(frame.height * s)),
                             Image.LANCZOS)
    return Frame(img, frame.left, frame.top, frame.width, frame.height,
                 time.time(), s)


def bench(label: str, frame: Frame, runs: int) -> None:
    ts, words = [], 0
    for _ in range(runs):
        t = time.perf_counter()
        r = ocr.read(frame)
        ts.append((time.perf_counter() - t) * 1000)
        words = len(r.words)
    ts.sort()
    med = ts[len(ts) // 2]
    # Spread as a fraction of the median: the number that says whether a
    # single timing can be trusted at all.
    spread = (ts[-1] - ts[0]) / med * 100 if med else 0
    per = med / words if words else 0
    print(f"  {label:<26}{frame.image.width}x{frame.image.height:<6}"
          f"{words:>6}w  med {med:6.1f}ms  min {ts[0]:6.1f}  max {ts[-1]:6.1f}"
          f"  spread {spread:4.0f}%  {per:5.2f}ms/word")


def main(argv: list[str]) -> int:
    runs = 8
    if "--runs" in argv:
        runs = int(argv[argv.index("--runs") + 1])

    print(f"{runs} reads per source; the same pixels every time.\n")
    sources: list[tuple[str, Frame]] = []

    for p in sorted(SHOTS.glob("*.png"))[:4]:
        sources.append((p.stem[:24], from_image(p)))

    s = Screen()
    live = s.grab()
    sources.append(("LIVE screen (native)", live))
    sources.append(("LIVE screen (1920 floor)", scaled(live)))

    # A blank frame: everything here that is not proportional to content.
    blank = Frame(Image.new("RGB", (2880, 1800), (255, 255, 255)),
                  0, 0, 2880, 1800, time.time())
    sources.append(("blank 2880x1800", blank))

    print(f"  {'source':<26}{'size':<13}{'words':>6}"
          f"  {'median':>9}  {'min':>8}  {'max':>7}  spread  per word")
    for label, f in sources:
        try:
            bench(label, f, runs)
        except Exception as e:
            print(f"  {label:<26}FAILED {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
