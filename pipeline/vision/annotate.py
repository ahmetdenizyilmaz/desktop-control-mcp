"""Draw what OCR claims onto the picture it read, so the claim can be judged.

A coordinate is either right or wrong, and the cheapest way to know which is
to put the box back on the pixels and look. Numbers in a table cannot show a
box that is four pixels high when the text is fourteen, or one that has
drifted a word to the left; a picture shows both instantly.

What gets drawn, and why each:

  word boxes    thin, per word — the unit OCR actually reports
  click dots    the CENTRE of each word box: the exact pixel an agent would
                click. This is the number that decides whether automation
                works, so it is drawn as a point, not implied by a rectangle.
  line boxes    the grouping OCR chose. Useful because a wrong grouping
                (two columns merged into one line) is invisible in the word
                boxes but obvious here.

    python annotate.py <image.png> [more.png ...]
    python annotate.py --screen            annotate a live grab
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vision import ocr                              # noqa: E402
from vision.capture import Screen, from_image       # noqa: E402

WORD = (255, 0, 0)
LINE = (0, 110, 255)
DOT = (255, 210, 0)
#: 1px outlines vanish on a downscaled view; these are drawn to be
#: seen at a glance, not to be pixel-exact.
WORD_W = 2
LINE_W = 1
DOT_R = 4


def annotate(frame, reading, out: Path, words: bool = True,
             lines: bool = True, dots: bool = True):
    """Return a copy of the frame with the reading drawn on it."""
    from PIL import ImageDraw

    img = frame.image.copy()
    d = ImageDraw.Draw(img)

    if lines:
        for l in reading.lines:
            x1, y1, x2, y2 = l.rect
            # Boxes are absolute screen coordinates; the image starts at the
            # frame's origin, so subtract it or every box lands off-picture
            # on a cropped grab.
            d.rectangle([x1 - frame.left, y1 - frame.top,
                         x2 - frame.left, y2 - frame.top],
                        outline=LINE, width=LINE_W)
    if words:
        for w in reading.words:
            x1, y1, x2, y2 = w.rect
            d.rectangle([x1 - frame.left, y1 - frame.top,
                         x2 - frame.left, y2 - frame.top],
                        outline=WORD, width=WORD_W)
    if dots:
        for w in reading.words:
            cx, cy = w.center
            cx -= frame.left
            cy -= frame.top
            d.ellipse([cx - DOT_R, cy - DOT_R, cx + DOT_R, cy + DOT_R],
                      fill=DOT, outline=(0, 0, 0))

    img.save(out)
    return img


def main(argv: list[str]) -> int:
    targets = [a for a in argv[1:] if not a.startswith("--")]
    outdir = Path(__file__).resolve().parent / "annotated"
    outdir.mkdir(exist_ok=True)

    frames = []
    if "--screen" in argv or not targets:
        frames.append(("screen", Screen().grab()))
    for t in targets:
        frames.append((Path(t).stem, from_image(t)))

    for name, frame in frames:
        r = ocr.read(frame)
        out = outdir / f"{name}.annotated.png"
        annotate(frame, r, out)
        print(f"{name}: {frame.width}x{frame.height}  {r.ms:.0f} ms  "
              f"{len(r.words)} words / {len(r.lines)} lines  -> {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
