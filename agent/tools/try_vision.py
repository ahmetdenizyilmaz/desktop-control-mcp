"""Try the vision click on a real window: what JEV would do, without doing it.

Runs JEV's own core.grounding.click_by_vision — the model, snap, read-back,
the second look — with the vision_grounding settings from settings.json, as a
DRY RUN unless --click. Prints the decision and saves a picture marking what
happened under <data>/grounding/try/.

    python tools/try_vision.py "Save"                    the foreground window after a 3 s countdown
    python tools/try_vision.py "Save" --window Notepad   a window whose title contains 'Notepad'
    python tools/try_vision.py "Save" --window Notepad --click    really click it
    python tools/try_vision.py --list                    the windows it can see

The A3 service must be running (diffusionvlclaude/a3_service/start_server.ps1).

Picture: red = the model's point, blue box = the second look's crop and blue
dot its point, green box = the element chosen, green cross = where the click
goes (orange instead when it was refused).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import actuator  # noqa: E402,F401  (makes the process per-monitor DPI aware, as in the app)
from core import grounding, locator, paths  # noqa: E402


def settings_block() -> dict:
    try:
        s = json.loads(Path(paths.data("settings.json")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        s = {}
    return dict(s.get("vision_grounding") or {})


def draw(cap, d) -> Path:
    from PIL import ImageDraw
    img = cap.img.copy()
    dr = ImageDraw.Draw(img)
    w = max(2, img.width // 500)

    def to_img(pt):
        return grounding.screen_to_image(pt[0], pt[1], cap.region, cap.sent_size)

    if d.second_look:
        dr.rectangle(d.second_look["box_image"], outline=(40, 90, 255), width=w)
        if d.second_look.get("point"):
            x, y = to_img(d.second_look["point"])
            dr.ellipse((x - 4 * w, y - 4 * w, x + 4 * w, y + 4 * w), fill=(40, 90, 255))
    if d.snapped and d.snapped.get("box_image"):
        dr.rectangle(d.snapped["box_image"], outline=(0, 170, 0), width=w)
    if d.image_point:
        x, y = d.image_point
        dr.ellipse((x - 5 * w, y - 5 * w, x + 5 * w, y + 5 * w), outline=(230, 0, 0), width=w)
    if d.click_point:
        x, y = to_img(d.click_point)
        c = (0, 170, 0) if d.accepted else (255, 140, 0)
        dr.line((x - 8 * w, y, x + 8 * w, y), fill=c, width=w)
        dr.line((x, y - 8 * w, x, y + 8 * w), fill=c, width=w)
    out = grounding.learning_dir() / "try"
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"{time.strftime('%Y%m%d-%H%M%S')}-{d.outcome}.png"
    img.save(p)
    return p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("target", nargs="?", help="what to click, e.g. Save")
    ap.add_argument("--window", help="part of the window's title (default: the foreground window)")
    ap.add_argument("--click", action="store_true", help="really click (default: dry run)")
    ap.add_argument("--wait", type=float, default=3.0, help="countdown before using the foreground window")
    ap.add_argument("--show", action="store_true", help="open the marked picture")
    ap.add_argument("--list", action="store_true", help="list the windows and stop")
    a = ap.parse_args()

    if a.list:
        for w in locator.list_windows():
            print(f"{w['hwnd']:>10}  {w['title']}")
        return 0
    if not a.target:
        ap.error("say what to click, e.g.  try_vision.py \"Save\" --window Notepad")

    block = settings_block()
    block.update(enabled=True, dry_run=not a.click, learning_log=False)
    grounding.configure(block)
    ok, why = grounding.health(force=True)
    if not ok:
        print(f"The A3 service is not answering ({why}). Start it with "
              f"diffusionvlclaude\\a3_service\\start_server.ps1")
        return 2

    if a.window:
        hwnd = locator.find_window(a.window)
        if not hwnd:
            print(f"No window title contains {a.window!r}; try --list")
            return 2
    else:
        for i in range(int(a.wait), 0, -1):
            print(f"Click the window to test... {i}", flush=True)
            time.sleep(1)
        hwnd = locator.foreground_window()
    print(f"Window: {locator.window_title(hwnd)!r}   service: {why}")

    seen = {}
    real_capture = grounding._capture_window

    def capture(h):
        seen["cap"] = real_capture(h)
        return seen["cap"]

    grounding._capture_window = capture
    try:
        d = grounding.click_by_vision(f"click {a.target}", hwnd, target=a.target)
    finally:
        grounding._capture_window = real_capture

    print(f"\n{d.outcome.upper()}: {d.reason}")
    print(f"  model point (picture) {d.image_point}   raw answer {d.raw!r}   {d.latency_ms} ms")
    if d.snapped:
        print(f"  element: {d.snapped.get('name')!r} ({d.snapped.get('type')}), {d.snap_distance} px from the point")
    if d.readback and d.readback.get("done"):
        print(f"  read-back: ok={d.readback.get('ok')} via {d.readback.get('source') or '-'}"
              f"  label={d.readback.get('label')!r}")
    if d.second_look:
        sl = d.second_look
        print(f"  second look: agrees={sl.get('agrees')}  point={sl.get('point')}  "
              f"distance={sl.get('distance')}  raw={sl.get('raw')!r}  {sl.get('latency_ms')} ms")
    print(f"  total {d.seconds:.1f} s")
    if seen.get("cap") is not None:
        p = draw(seen["cap"], d)
        print(f"  picture: {p}")
        if a.show:
            os.startfile(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
