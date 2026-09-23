"""Benchmark target — buttons with known rects, across styles AND kinds.

Ground truth for BOTH benchmarks:
  * detection: ground_truth.json with each button's absolute screen rect,
               tagged with `kind` and `style` so results break down per category
  * clicking:  every real click appended to clicks.log

Categories are chosen so the methods should visibly diverge:

  kind=text        has readable text        -> OCR should win
  kind=glyph       unicode symbol only      -> OCR weak, vision/UIA should hold
  kind=drawn       Canvas shape, NO text    -> OCR blind, UIA may be nameless
  style=flat       borderless, low contrast -> hard edges for YOLO
  style=dark       dark theme               -> contrast/threshold stress
  size=tiny        20x20                    -> small-target limit

Run:  python bench/target_app.py
"""

import ctypes
import json
import time
import tkinter as tk
from pathlib import Path

# Match controller.py — physical pixels, so rects line up with screenshots.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except (AttributeError, OSError):
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass

BENCH_DIR = Path(__file__).parent
GROUND_TRUTH = BENCH_DIR / "ground_truth.json"
CLICK_LOG = BENCH_DIR / "clicks.log"

WIN_X, WIN_Y = 150, 120
WIN_W, WIN_H = 1500, 900

LIGHT_BG = "#f2f2f2"
DARK_BG = "#1e1e1e"

# (id, label, kind, style, w, h)
#   kind:  text | glyph | drawn
#   style: raised | flat | dark | tinted
SPEC = [
    # --- row 1: classic raised text, large -------------------------------
    ("b01", "Save",      "text",  "raised", 160, 42),
    ("b02", "Open",      "text",  "raised", 160, 42),
    ("b03", "Delete",    "text",  "raised", 160, 42),
    ("b04", "Settings",  "text",  "raised", 160, 42),
    # --- row 2: flat / borderless text -----------------------------------
    ("b05", "Search",    "text",  "flat",   120, 34),
    ("b06", "Refresh",   "text",  "flat",   120, 34),
    ("b07", "Export",    "text",  "flat",   120, 34),
    ("b08", "Import",    "text",  "flat",   120, 34),
    # --- row 3: tinted text, small ---------------------------------------
    ("b09", "Cut",       "text",  "tinted",  70, 28),
    ("b10", "Copy",      "text",  "tinted",  70, 28),
    ("b11", "Paste",     "text",  "tinted",  70, 28),
    ("b12", "Undo",      "text",  "tinted",  70, 28),
    # --- row 4: unicode glyph icons, NO words ----------------------------
    ("b13", "play",      "glyph", "raised",  40, 40),
    ("b14", "stop",      "glyph", "raised",  40, 40),
    ("b15", "gear",      "glyph", "raised",  40, 40),
    ("b16", "magnifier", "glyph", "raised",  40, 40),
    # --- row 5: glyph icons, flat + tiny ---------------------------------
    ("b17", "plus",      "glyph", "flat",    26, 26),
    ("b18", "cross",     "glyph", "flat",    26, 26),
    ("b19", "arrow",     "glyph", "flat",    26, 26),
    ("b20", "check",     "glyph", "flat",    26, 26),
    # --- row 6: canvas-drawn shapes, no text at all ----------------------
    ("b21", "circle",    "drawn", "raised",  44, 44),
    ("b22", "triangle",  "drawn", "raised",  44, 44),
    ("b23", "square",    "drawn", "raised",  44, 44),
    ("b24", "diamond",   "drawn", "raised",  44, 44),
    # --- row 7: dark theme, mixed ----------------------------------------
    ("b25", "Commit",    "text",  "dark",   130, 36),
    ("b26", "Branch",    "text",  "dark",   130, 36),
    ("b27", "merge",     "glyph", "dark",    36, 36),
    ("b28", "dot",       "drawn", "dark",    36, 36),
    # --- row 8: tiny targets — the small-target limit ---------------------
    ("b29", "t1",        "text",  "raised",  20, 20),
    ("b30", "t2",        "text",  "flat",    20, 20),
    ("b31", "t3",        "glyph", "raised",  20, 20),
    ("b32", "t4",        "drawn", "raised",  20, 20),
]

GLYPHS = {
    "play": "▶", "stop": "■", "gear": "⚙", "magnifier": "⚲",
    "plus": "✚", "cross": "✖", "arrow": "➤", "check": "✔",
    "merge": "⑂", "t3": "●",
}

COLS = 4
PAD_X, PAD_Y = 50, 50
CELL_W, CELL_H = 340, 100


def _style_kwargs(style: str) -> dict:
    if style == "flat":
        return dict(relief="flat", bd=0, bg="#eaeaea", fg="#333333",
                    activebackground="#dcdcdc", highlightthickness=0)
    if style == "tinted":
        return dict(relief="ridge", bd=2, bg="#cfe3ff", fg="#10305a",
                    activebackground="#b5d4ff")
    if style == "dark":
        return dict(relief="flat", bd=1, bg="#2d2d30", fg="#e6e6e6",
                    activebackground="#3e3e42", highlightthickness=0)
    return dict(relief="raised", bd=2, bg="#ffffff", fg="#111111")


def _draw_shape(canvas: tk.Canvas, shape: str, w: int, h: int, fg: str):
    m = 5
    if shape in ("circle", "dot", "t4"):
        canvas.create_oval(m, m, w - m, h - m, fill=fg, outline=fg)
    elif shape == "triangle":
        canvas.create_polygon(w // 2, m, w - m, h - m, m, h - m, fill=fg, outline=fg)
    elif shape == "square":
        canvas.create_rectangle(m, m, w - m, h - m, fill=fg, outline=fg)
    elif shape == "diamond":
        canvas.create_polygon(w // 2, m, w - m, h // 2, w // 2, h - m, m, h // 2,
                              fill=fg, outline=fg)


def main() -> None:
    root = tk.Tk()
    root.title("BENCH TARGET")
    root.geometry(f"{WIN_W}x{WIN_H}+{WIN_X}+{WIN_Y}")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    root.configure(bg=LIGHT_BG)

    CLICK_LOG.write_text("", encoding="utf-8")

    # Dark band behind the dark-theme row so contrast is genuinely different
    dark_y = PAD_Y + 6 * CELL_H - 18
    tk.Frame(root, bg=DARK_BG).place(x=0, y=dark_y, width=WIN_W, height=CELL_H)

    status = tk.Label(root, text="ready", bg=LIGHT_BG, font=("Segoe UI", 10))
    status.place(x=PAD_X, y=WIN_H - 36)

    def on_click(bid: str, label: str):
        def handler(_evt=None):
            ts = time.time()
            with open(CLICK_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({"id": bid, "label": label, "t": ts}) + "\n")
            status.config(text=f"last click: {bid} ({label})  @{ts:.3f}")
        return handler

    widgets = {}
    for i, (bid, label, kind, style, w, h) in enumerate(SPEC):
        col, row = i % COLS, i // COLS
        x = PAD_X + col * CELL_W
        y = PAD_Y + row * CELL_H
        sk = _style_kwargs(style)

        if kind == "drawn":
            bg = sk.get("bg", "#ffffff")
            fg = "#e6e6e6" if style == "dark" else "#1a4fa0"
            cv = tk.Canvas(root, width=w, height=h, bg=bg,
                           highlightthickness=1 if style != "flat" else 0,
                           highlightbackground="#888888")
            _draw_shape(cv, label, w, h, fg)
            cv.bind("<Button-1>", on_click(bid, label))
            cv.place(x=x, y=y, width=w, height=h)
            wdg = cv
        else:
            text = GLYPHS.get(label, label) if kind == "glyph" else label
            font_size = 9 if h >= 26 else 7
            btn = tk.Button(root, text=text, command=on_click(bid, label),
                            font=("Segoe UI", font_size), **sk)
            btn.place(x=x, y=y, width=w, height=h)
            wdg = btn

        widgets[bid] = (wdg, label, kind, style)

    def dump_ground_truth():
        root.update_idletasks()
        entries = []
        for bid, (wdg, label, kind, style) in widgets.items():
            x1, y1 = wdg.winfo_rootx(), wdg.winfo_rooty()
            x2, y2 = x1 + wdg.winfo_width(), y1 + wdg.winfo_height()
            entries.append({
                "id": bid, "label": label, "kind": kind, "style": style,
                "rect": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
                "center": {"x": (x1 + x2) // 2, "y": (y1 + y2) // 2},
                "size": {"w": x2 - x1, "h": y2 - y1},
                "tiny": (x2 - x1) <= 26,
            })
        payload = {
            "window": {"x": root.winfo_rootx(), "y": root.winfo_rooty(),
                       "w": root.winfo_width(), "h": root.winfo_height()},
            "buttons": entries,
        }
        GROUND_TRUTH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        status.config(text=f"ground truth written: {len(entries)} targets")
        print(f"ground truth -> {GROUND_TRUTH} ({len(entries)} targets)")

    root.after(500, dump_ground_truth)
    root.mainloop()


if __name__ == "__main__":
    main()
