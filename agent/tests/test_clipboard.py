"""The clipboard, used to paste a screenshot into a web app.

Driving a native file-open dialog to attach a picture is fragile; putting the
image on the clipboard and pressing Ctrl+V is not. That makes the clipboard a
load-bearing part of "screenshot this and send it to the group", so it gets
its own checks.

The clipboard is the USER'S, not ours. Whatever text is on it when this suite
starts is put back when it finishes — a test suite that silently ate your
clipboard would be a bug of its own.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cleanup import scratch_dir  # noqa: E402

from core import clipboard  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""),
          flush=True)
    return bool(ok)


def _make_png(path: Path, size=(64, 48), mode="RGB") -> Path:
    from PIL import Image
    img = Image.new(mode, size, (200, 30, 30) if mode == "RGB" else (200, 30, 30, 128))
    img.save(path)
    return path


def test_text():
    print("\n1. text, including the alphabet the user actually types")
    check("plain ascii round-trips",
          clipboard.set_text("hello") and clipboard.get_text() == "hello")
    turkish = "bu saat nasıl sizce beyler — İŞÇİ ğüöç"
    check("Turkish round-trips unmangled",
          clipboard.set_text(turkish) and clipboard.get_text() == turkish,
          repr(clipboard.get_text())[:60])
    check("empty string is allowed", clipboard.set_text("") is True)
    long_s = "x" * 200_000
    check("a very long string round-trips",
          clipboard.set_text(long_s) and clipboard.get_text() == long_s,
          f"{len(clipboard.get_text())} chars back")


def test_image():
    print("\n2. images — the reason this module exists")
    tmp = scratch_dir("clip_test_")
    png = _make_png(tmp / "shot.png")
    check("an image goes on the clipboard", clipboard.set_image(str(png)))
    check("and the clipboard reports holding one", clipboard.has_image())

    # A transparent PNG must not paste as a black rectangle.
    rgba = _make_png(tmp / "alpha.png", mode="RGBA")
    check("a transparent PNG is flattened, not rejected",
          clipboard.set_image(str(rgba)) and clipboard.has_image())

    from PIL import Image
    Image.new("RGB", (40, 40), (0, 0, 255)).save(tmp / "shot.jpg")
    check("a JPEG works too (what screenshot() saves)",
          clipboard.set_image(str(tmp / "shot.jpg")) and clipboard.has_image())


def test_failures_are_returned_not_raised():
    print("\n3. failure is a False, never an exception mid-task")
    tmp = scratch_dir("clip_bad_")
    missing = tmp / "does-not-exist.png"
    try:
        ok = clipboard.set_image(str(missing))
        check("a missing file returns False", ok is False, repr(ok))
    except Exception as e:
        check("a missing file returns False", False, f"raised {type(e).__name__}")

    junk = tmp / "notanimage.txt"
    junk.write_text("this is not a picture", encoding="utf-8")
    try:
        ok = clipboard.set_image(str(junk))
        check("a non-image file returns False", ok is False, repr(ok))
    except Exception as e:
        check("a non-image file returns False", False, f"raised {type(e).__name__}")

    try:
        check("get_text never raises", isinstance(clipboard.get_text(), str))
    except Exception as e:
        check("get_text never raises", False, f"raised {type(e).__name__}")


def test_text_clears_image():
    print("\n4. setting text replaces the image, so a stale picture is not pasted")
    tmp = scratch_dir("clip_seq_")
    clipboard.set_image(str(_make_png(tmp / "a.png")))
    clipboard.set_text("just text now")
    check("no image left on the clipboard after setting text",
          clipboard.has_image() is False)
    check("and the text is there", clipboard.get_text() == "just text now")


def main() -> int:
    print("=" * 72)
    print("CLIPBOARD — paste a screenshot into a web app")
    print("=" * 72)

    # Borrow the user's clipboard; hand it back exactly as found.
    saved = clipboard.get_text()
    try:
        test_text()
        test_image()
        test_failures_are_returned_not_raised()
        test_text_clears_image()
    finally:
        clipboard.set_text(saved)

    passed = sum(1 for _, ok, _ in results if ok)
    print("\n" + "=" * 72)
    print(f"{passed}/{len(results)} passed")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name}  {detail}")
    print("=" * 72)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
