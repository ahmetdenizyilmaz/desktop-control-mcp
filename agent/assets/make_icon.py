"""Draw the application icon.

    python agent/assets/make_icon.py

Writes icon.ico beside this file, and prints the favicon for ui/app.html.

The mark is a pointer arrow on a blue tile: the one glyph everybody reads as
"something is driving the cursor", and a shape that survives 16x16. Each size
in the .ico is drawn at that size, not shrunk from one big master — a 256px
rendering scaled down to 16px is a smear, and 16px is what the taskbar, the
title bar and the Alt-Tab list actually show. The arrow gets a little bolder
and its outline a little thicker as the tile gets smaller, for the same reason
a typeface has optical sizes.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
ICO = HERE / "icon.ico"

SIZES = (16, 32, 48, 64, 128, 256)

#: The app's own palette (ui/app.html): accent blue on the dark navy ground.
TILE_TOP = (91, 141, 255)          # --accent
TILE_BOTTOM = (52, 98, 214)
INK = (13, 20, 31)                 # --bg
WHITE = (255, 255, 255)

#: The classic pointer, in a unit square, tip at the origin.
ARROW = [(0.00, 0.00), (0.00, 0.78), (0.20, 0.60), (0.33, 0.90),
         (0.45, 0.85), (0.32, 0.56), (0.58, 0.56)]


def _render(size: int, oversample: int = 8) -> Image.Image:
    """One tile at one size, drawn oversized and downsampled for clean edges."""
    s = size * oversample
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Tile: a rounded square with a soft vertical gradient. The radius is a
    # fraction of the size so every entry has the same silhouette.
    radius = int(s * 0.22)
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, s - 1, s - 1), radius=radius, fill=255)
    grad = Image.new("RGBA", (s, s))
    gd = ImageDraw.Draw(grad)
    for y in range(s):
        t = y / max(1, s - 1)
        col = tuple(int(TILE_TOP[i] + (TILE_BOTTOM[i] - TILE_TOP[i]) * t) for i in range(3))
        gd.line([(0, y), (s, y)], fill=col + (255,))
    img.paste(grad, (0, 0), mask)

    # Arrow: white with a navy outline so it holds against the blue and
    # against a light taskbar alike. Smaller tiles get a bigger arrow and a
    # heavier outline, or the shape closes up into a blob.
    scale = {16: 0.66, 32: 0.62}.get(size, 0.58) * s
    outline = max(1, int(s * ({16: 0.075, 32: 0.06}.get(size, 0.045))))
    ox = s * ({16: 0.20, 32: 0.21}.get(size, 0.23))
    oy = s * ({16: 0.16, 32: 0.17}.get(size, 0.19))
    pts = [(ox + x * scale, oy + y * scale) for x, y in ARROW]
    draw.polygon(pts, fill=INK)
    draw.line(pts + [pts[0]], fill=INK, width=outline * 2, joint="curve")
    inner = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(inner).polygon(pts, fill=WHITE)
    img.alpha_composite(inner)

    return img.resize((size, size), Image.Resampling.LANCZOS)


def build() -> dict[int, Image.Image]:
    return {n: _render(n) for n in SIZES}


def favicon_data_uri(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def main() -> int:
    frames = build()
    biggest = frames[max(SIZES)]
    others = [frames[n] for n in SIZES if n != max(SIZES)]
    # append_images makes Pillow store each frame as drawn, instead of
    # resampling the first one to every requested size.
    biggest.save(ICO, format="ICO", sizes=[(n, n) for n in SIZES],
                 append_images=others)
    print(f"wrote {ICO}  ({ICO.stat().st_size} bytes, sizes {SIZES})")
    print("favicon (32px):")
    print(favicon_data_uri(frames[32]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
