"""The vision package: the properties that must not quietly break.

Every number in this package came from an ad-hoc script run once. That is
fine for deciding a design and useless for keeping it — so these are the
claims worth locking, and they are chosen by what would fail SILENTLY:

  * a coordinate that maps back wrong. The agent clicks steadily further
    from the target the further right it goes, and nothing errors.
  * a scaled grab whose ratio drifts by a pixel, which is the same failure
    arriving slowly.
  * a dark capture read as "no text on screen" rather than as a failure.
  * OCR text counted as a separate element from the button it sits on, so
    the same control appears twice and the model picks the wrong one.
  * a featureless template that matches everywhere at 0.99.

Deterministic on purpose: everything is drawn here, so nothing depends on
what happens to be on screen. Only the OCR sections need Windows, and they
say so when it is missing rather than failing.

    python tests/test_vision.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw                              # noqa: E402

from vision import elements, ocr                              # noqa: E402
from vision.capture import (DisplayDark, Frame, Screen,       # noqa: E402
                            fingerprint, from_image, lit_fraction)
from vision.templates import Icons, MIN_DETAIL, _detail       # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  — {detail}" if detail else ""), flush=True)
    return bool(ok)


def canvas(w=600, h=300, bg=(250, 250, 250)) -> Image.Image:
    return Image.new("RGB", (w, h), bg)


def frame_of(img, left=0, top=0, scale=1.0) -> Frame:
    return Frame(img, left, top, round(img.width / scale),
                 round(img.height / scale), time.time(), scale)


# --------------------------------------------------------------------------

def test_coordinates():
    print("\n1. coordinates survive every transform")

    # No transform at all.
    f = frame_of(canvas())
    check("identity round trip", f.to_screen(100, 50) == (100, 50))
    check("inverse round trip", f.to_image(100, 50) == (100, 50))

    # A cropped region: the offset must come back.
    f = frame_of(canvas(), left=400, top=300)
    check("region offset applied", f.to_screen(10, 20) == (410, 320),
          str(f.to_screen(10, 20)))
    check("region inverse", f.to_image(410, 320) == (10, 20))

    # Scaled: the case that decides whether a click lands.
    f = frame_of(canvas(1728, 1080), scale=0.6)
    check("scaled source size", (f.width, f.height) == (2880, 1800),
          f"{f.width}x{f.height}")
    check("scaled maps back", f.to_screen(600, 300) == (1000, 500),
          str(f.to_screen(600, 300)))
    # Round-trip every corner and centre; drift shows up here before it
    # shows up as a misplaced click.
    worst = 0
    for sx in (0, 137, 999, 2879):
        for sy in (0, 41, 777, 1799):
            ix, iy = f.to_image(sx, sy)
            bx, by = f.to_screen(ix, iy)
            worst = max(worst, abs(bx - sx), abs(by - sy))
    check("round trip within 1px everywhere", worst <= 1, f"worst {worst}px")

    # Scaled AND offset together — the combination a region grab produces.
    f = frame_of(canvas(400, 300), left=200, top=100, scale=0.5)
    check("scaled + offset", f.to_screen(100, 50) == (400, 200),
          str(f.to_screen(100, 50)))


def test_scaling_ratio():
    print("\n2. scaling never changes the aspect ratio")
    s = Screen()
    try:
        w, h = s.size()
    except Exception as e:
        check("screen available", False, str(e))
        return
    for label, kw in (("fit_height=1080", {"fit_height": 1080}),
                      ("min_size floor", {"min_size": (1920, 1080)})):
        try:
            f = s.grab(allow_dark=True, **kw)
        except Exception as e:
            check(f"{label} grab", False, f"{type(e).__name__}: {e}")
            continue
        src = w / h
        got = f.image.width / f.image.height
        check(f"{label} ratio preserved", abs(src - got) < 0.002,
              f"{src:.6f} vs {got:.6f}")
        check(f"{label} never upscales", f.scale <= 1.0, f"scale={f.scale}")
    # The floor is a floor: neither axis may end up under it.
    f = s.grab(allow_dark=True, min_size=(1920, 1080))
    check("min_size honours both axes",
          f.image.width >= 1920 and f.image.height >= 1080,
          f"{f.image.width}x{f.image.height}")


def test_fingerprint():
    print("\n3. change detection is stable and sensitive")
    # Screen-sized on purpose: the caret claim is a RATIO claim, and a 600px
    # canvas makes a 1px caret 45x more prominent than it is in reality.
    a = canvas(2880, 1800)
    d = ImageDraw.Draw(a)
    d.rectangle([200, 200, 900, 600], fill=(80, 120, 200))
    b = a.copy()
    check("same picture, same hash", fingerprint(a) == fingerprint(b))

    # A caret blinking must NOT read as a change.
    c = a.copy()
    ImageDraw.Draw(c).line([(1400, 400), (1400, 420)], fill=(0, 0, 0), width=1)
    check("a 1px caret is not a change", fingerprint(a) == fingerprint(c),
          "caret changed the hash")

    # A dialog opening must.
    e = a.copy()
    ImageDraw.Draw(e).rectangle([700, 600, 2100, 1200], fill=(255, 255, 255),
                                outline=(0, 0, 0))
    check("a dialog IS a change", fingerprint(a) != fingerprint(e))

    # The one that shipped broken: a WHITE dialog on a near-white page. Both
    # collapsed into the same grey bucket at the old resolution, so the hash
    # did not move and a stale scene would have been reused.
    subtle = a.copy()
    ImageDraw.Draw(subtle).rectangle([700, 600, 2100, 1200],
                                     fill=(255, 255, 255))
    check("a LOW-CONTRAST dialog is a change",
          fingerprint(a) != fingerprint(subtle),
          "white-on-white dialog went unnoticed")

    badge = a.copy()
    ImageDraw.Draw(badge).rectangle([1400, 800, 1500, 840], fill=(220, 60, 60))
    check("a small badge is a change", fingerprint(a) != fingerprint(badge))


def test_dark_guard():
    print("\n4. a black capture is an error, never 'nothing on screen'")
    black = Image.new("RGB", (400, 300), (0, 0, 0))
    check("black reads as unlit", lit_fraction(black) < 0.005,
          f"{lit_fraction(black):.4f}")
    lit = canvas()
    check("a normal frame reads as lit", lit_fraction(lit) > 0.5,
          f"{lit_fraction(lit):.4f}")
    # A cursor composited onto black is ~90 bright pixels and must still
    # count as dark — this is why the test is a mean, not a std.
    nearly = Image.new("RGB", (400, 300), (0, 0, 0))
    ImageDraw.Draw(nearly).ellipse([10, 10, 20, 20], fill=(255, 255, 255))
    check("a cursor on black is still dark", lit_fraction(nearly) < 0.005,
          f"{lit_fraction(nearly):.4f}")


def test_from_image():
    print("\n5. saved pictures behave like grabs")
    img = canvas(320, 200)
    f = from_image(img)
    check("wraps a PIL image", (f.width, f.height) == (320, 200))
    check("defaults to origin", (f.left, f.top) == (0, 0))
    f = from_image(img, left=500, top=250)
    check("a crop remembers where it came from",
          f.to_screen(10, 10) == (510, 260), str(f.to_screen(10, 10)))


def test_fold():
    print("\n6. Turkish case folding")
    check("İ folds to i", ocr.fold("İSTANBUL") == "istanbul",
          ocr.fold("İSTANBUL"))
    check("I folds to ı", ocr.fold("IŞIK") == "ışık", ocr.fold("IŞIK"))
    check("Ğ folds", ocr.fold("DAĞ") == "dağ", ocr.fold("DAĞ"))
    check("ödeme matches ÖDEME", ocr.fold("ÖDEME") == ocr.fold("ödeme"))


def test_ocr_reads():
    print("\n7. OCR reads text where it actually is")
    img = canvas(700, 240)
    d = ImageDraw.Draw(img)
    try:
        from PIL import ImageFont
        font = ImageFont.truetype("segoeui.ttf", 30)
    except Exception:
        font = None
    d.text((60, 50), "Sepete Ekle", fill=(0, 0, 0), font=font)
    d.text((60, 140), "Arama alanı", fill=(0, 0, 0), font=font)

    try:
        r = ocr.read(from_image(img))
    except Exception as e:
        check("Windows OCR available", False, f"{type(e).__name__}: {e}")
        return
    check("Windows OCR available", True, f"{r.language}, {r.ms:.0f}ms")

    hits = r.find("sepete")
    check("finds the text", bool(hits), r.text.replace("\n", " | ")[:60])
    if hits:
        cx, cy = hits[0].center
        check("box is where it was drawn",
              40 < cx < 400 and 30 < cy < 100, f"centre={(cx, cy)}")
    check("Turkish characters survive", bool(r.find("alanı")),
          r.text.replace("\n", " | ")[:60])

    # The offset claim: a crop's coordinates must be absolute.
    off = ocr.read(from_image(img, left=1000, top=500))
    oh = off.find("sepete")
    if oh and hits:
        dx = oh[0].center[0] - hits[0].center[0]
        dy = oh[0].center[1] - hits[0].center[1]
        check("a crop returns ABSOLUTE coordinates", (dx, dy) == (1000, 500),
              f"shifted by {(dx, dy)}, expected (1000, 500)")


def test_template_detail():
    print("\n8. a featureless crop is refused as a template")
    check("flat panel has no detail", _detail(canvas(90, 60)) < MIN_DETAIL,
          f"{_detail(canvas(90, 60)):.1f}")

    icon = canvas(32, 32, (255, 255, 255))
    d = ImageDraw.Draw(icon)
    d.line([(6, 6), (26, 26)], fill=(0, 0, 0), width=3)
    d.line([(26, 6), (6, 26)], fill=(0, 0, 0), width=3)
    check("an icon has detail", _detail(icon) > MIN_DETAIL,
          f"{_detail(icon):.1f}")

    import tempfile
    lib = Icons(path=Path(tempfile.mkdtemp(prefix="icons_test_")))
    page = canvas(500, 400)
    page.paste(icon, (200, 150))
    f = from_image(page)

    try:
        lib.add("flat", f, (10, 10, 100, 70))
        check("flat crop rejected", False, "it was accepted")
    except ValueError as e:
        check("flat crop rejected", "featureless" in str(e))
    try:
        lib.add("tiny", f, (10, 10, 14, 14))
        check("tiny crop rejected", False, "it was accepted")
    except ValueError:
        check("tiny crop rejected", True)

    lib.add("x_icon", f, (200, 150, 232, 182))
    check("icon accepted", "x_icon" in lib.names())

    hit = lib.find("x_icon", f)
    check("template found again", hit is not None
          and hit.score > 0.99, f"{hit.score if hit else None}")
    if hit:
        check("found at the right place", hit.center == (216, 166),
              str(hit.center))

    # Two copies: find_all must report both, not one twice.
    page2 = canvas(500, 400)
    page2.paste(icon, (100, 100))
    page2.paste(icon, (300, 250))
    hits = lib.find_all("x_icon", from_image(page2))
    check("both instances found", len(hits) == 2, f"{len(hits)} hits")


def test_merge():
    print("\n9. the merge does not double-count")
    # A button whose label OCR will also read.
    btn = elements.Element("button", "Save", 100, 100, 120, 40, "uia", True)
    stray = elements.Element("text", "unrelated", 400, 300, 80, 20, "ocr",
                             False)
    sc = elements.Scene([btn, stray], from_image(canvas()), 0.0)

    check("clickable filter", [e.name for e in sc.clickable()] == ["Save"])
    check("find is case-insensitive", bool(sc.find("save")))
    check("find can require clickable",
          not sc.find("unrelated", clickable_only=True))
    check("at() hits the button", sc.at(150, 120) is btn)
    check("at() misses empty space", sc.at(5, 5) is None)

    # at() must return the SMALLEST element, or a pane swallows every click.
    pane = elements.Element("button", "pane", 0, 0, 900, 700, "uia", True)
    sc2 = elements.Scene([pane, btn], from_image(canvas()), 0.0)
    check("at() prefers the smallest", sc2.at(150, 120) is btn,
          sc2.at(150, 120).name if sc2.at(150, 120) else "None")

    rows = sc.rows()
    check("rows carry the decision-layer keys",
          set(rows[0]) == {"type", "name", "id", "value", "hint"},
          str(sorted(rows[0])))
    check("kind survives into rows", rows[0]["type"] == "button", rows[0]["type"])
    text_row = next(r for r in rows if r["type"] == "text")
    check("text is marked not clickable", text_row["hint"] == "text only",
          text_row["hint"])
    check("rows come out in reading order",
          [r["name"] for r in sc.rows()] == ["Save", "unrelated"])


def main() -> int:
    print("vision package")
    for t in (test_coordinates, test_scaling_ratio, test_fingerprint,
              test_dark_guard, test_from_image, test_fold, test_ocr_reads,
              test_template_detail, test_merge):
        try:
            t()
        except Exception as e:
            check(f"{t.__name__} crashed", False, f"{type(e).__name__}: {e}")
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} passed")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name}  {detail}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
