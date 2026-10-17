# A3 — vision click grounding, as a fallback

Branch `a3-grounding`. A small local vision model that, when every locator
has missed, is shown the target window and asked where the thing is. Off by
default. Nothing about the default path changes.

## What was added

| file | change |
|---|---|
| `core/grounding.py` | **new.** The whole feature: settings block, health check, window capture, the HTTP call, the gates, risky-name guard, agreement, snap to a UIA element (detector optional), read-back (UIA labels / Windows OCR), the click through `actuator.click`, after-click verification, the task log and the learning log |
| `core/act.py` | `click_element`: after `do_step` reports the plain miss (`"no locator resolved"`, no `index`/`near`), and only if enabled, one call into `grounding.click_by_vision(..., target=name, prefer=prefer)`. A click comes back as `locator_type="vision:ground"` with `verified` True/False/None and the clicked element's rect; a refusal keeps the miss and appends the reason |
| `tools_sdk.py` | the `click` tool's reply for a vision click adds `, verified` or `, NOT verified: nothing visibly changed, look before going on` |
| `tools/export_grounding.py` | **new.** Learning log -> training rows (verified-correct decisions only) |
| `app.py` | the `vision_grounding` block in `DEFAULTS`; a `"block"` kind in `SETTINGS_SCHEMA` / `_clean_value` that delegates to `grounding.clean_settings`; `update_settings` pushes the block to the module and wires the action history (the last six `tools_sdk.journal()` lines) |
| `tests/test_grounding.py` | **new.** 180 checks, fully offline: loopback mock service, drawn pictures instead of captures, fake UIA candidate lists, stubbed OCR and change detection, a recording stub instead of the actuator. Sections 1–16 (the original 82 checks) run with the new stages off and test the original gates unchanged |
| `tests/run_all.py` | the suite registered (non-interactive) |
| `tests/cleanup.py` | the suite's scratch-dir prefix |

## How the fallback triggers

```
tools_sdk.click  ->  act.click_element(name=...)  ->  do_step  ->  locator.resolve
                                                                        |
                                                   win32 / UIA / OCR all missed
                                                                        |
                       error == "no locator resolved"  and  no index/near  and  enabled
                                                                        |
                     grounding.click_by_vision("click <name>", hwnd, target=<name>)
```

Inside `click_by_vision`, in order — any gate that fails ends the attempt
with a logged reason and no input:

1. `enabled` (settings) — otherwise nothing at all happens, not even a log line
2. **risky name** — a target matching `delete, remove, uninstall, format,
   erase, wipe, pay, payment, buy, purchase, checkout, place order, send,
   submit, confirm, transfer, withdraw, sign out, log out, log off, shut
   down, restart, reboot, reset, discard, empty trash / recycle bin,
   deactivate, close account, unsubscribe, overwrite, trash, donate`
   (whole words, English) is `refused` before anything is captured or
   asked, unless `allow_risky`. Named (UIA) clicks are not affected
3. `GET /health` once; a service found down is not asked again for 60 s, so
   a dead service costs one connect per minute, not one per click
4. a window to bound the click (`require_inside_window`); the whole screen is
   never captured unless that gate is switched off
5. capture **the target window** (foreground, or the `window=` the caller
   named), clipped to the virtual desktop, in physical pixels; a blank,
   asleep or locked display raises and the attempt is "unavailable"
6. `POST /ground` with the PNG inline (`image_b64`), the instruction, the
   last actions as `history`, and `"agreement": true` (when `agreement`)
7. a model that answers `none` (the stage-2 model can: `ok:false`,
   `error:"no_point"`, `raw:"none"`) is a clean **`not_found`** — "the model
   says 'X' is not in 'Window'" — with nothing snapped or read.
   Otherwise the answer is refused if: the service says `ok:false`; its `image_size`
   is not the picture we sent; the point is outside the picture; the
   confidence is below `min_confidence` (when both exist — measured
   useless, leave it null); the window **moved while the model was
   thinking**; the mapped point is outside the window's rectangle
8. **agreement**: `agreement_distance > max_disagreement` (0.05) marks the
   answer high-risk (measured: ~20% right vs ~52% when the two looks
   agree). High-risk with `readback` off = `refused`; high-risk with
   read-back on must pass read-back with a real label
   (`readback_allow_unreadable` does not apply). Missing or malformed
   fields = no signal, not an error
9. **snap** (`snap`): list the window's actionable UIA elements (buttons,
   split buttons, links, edits, combo/check/radio boxes, menu/tab/list/
   tree/data/header items, sliders; on-screen, inside the window; a Store
   app's CoreWindow child too) within `snap_budget_s`. Radius =
   `snap_radius_px` x the window's DPI scale (40 -> 80 physical px at
   200%). With `detector_fallback` and no UIA element within the radius,
   the OmniParser-style detector (`ui_parser.parse_ui`: YOLO boxes + OCR
   text, imported lazily) runs on the captured picture; icon captions are
   never used as names
10. **read-back** (`readback`): among the elements within the radius, the
    nearest whose own label matches the target wins — the model often hits
    the NEIGHBOUR of the right control. Labels: UIA Name, HelpText,
    AutomationId (split into words). Where the UIA labels do not match, one
    OCR pass (Windows.Media.Ocr via `pipeline/vision/ocr.py`, ~10 ms warm,
    crop upscaled 2x, lines split into per-label segments at wide gaps;
    EasyOCR only if already loaded) over the neighbourhood; each box reads
    the text whose centre it contains. A matching line of text with no
    element around it (a canvas) is a target too. The OCR box reaches
    `readback_ocr_reach_px` (x DPI) further left and right of the point
    than the radius, so a label longer than the radius is read whole.
    Nothing matching = **`not_found`**, a clean miss reported to the model
    ("the control at the model's point reads 'Brushes', not 'Pencil'").
    Without read-back: the tightest element containing the point, else the
    nearest within the radius, else the raw point
10b. **second look** (`second_look`, off by default): when NOTHING readable
    is at the point (an unnamed icon, a canvas) the model is asked again on
    a crop of `second_look_fraction` of the picture around the click point,
    magnified towards the service's pixel budget (at most 2.5x); the click
    goes ahead only if the new point is on the same element (its box grown
    by half the radius) or, with no element, within the radius. `none` or a
    point elsewhere = `not_found`. Never for a high-risk answer. Recorded as
    `readback.source = "second_look"` and in `second_look` in the logs, and
    never exported as a training row (the model agreeing with itself is not
    a label)
11. the chosen element's own name is risky (possible when read-back is off)
    -> `refused`; the snapped centre must still be inside the window
12. `dry_run`: log "would click (x,y) on 'Save'" and stop
13. the click: a snapped UIA element goes to `actuator.click(element,
    prefer=<the caller's prefer>)` — Invoke first, as for any named click
    (works with the window covered); a detector/OCR box or the raw point is
    an injected click at its centre, which foregrounds the window first and
    **refuses** if it cannot. With `snap` off the raw point is clicked even
    when read-back checked the element under it
14. **verify** (`verify`): a baseline is taken just before the click
    (foreground window, focused element, the element's toggle/selection/
    expand/value state, a 64x64 grey thumbnail of the window with the
    clicked element masked out so its own hover highlight does not count;
    a blinking caret moves too few cells to count). After the click it is
    polled every 0.15 s for `verify_timeout_s` (1.5 s). Any change =
    `verified: true` with the reason; none = `verified: false`
    ("UNVERIFIED"). Nothing is ever retried. A baseline that cannot be
    taken (locked screen) = `verified: null`

Coordinates: the service answers in pixels of the sent picture; the mapping
back is the capture's origin plus a scale factor (only when a capture over
2560 px on its long edge was shrunk). `actuator.py` makes the process
per-monitor DPI aware at import and the capture pins its own thread the same
way (`make_thread_dpi_aware`), so `window_rect`, `mss`, UIA rectangles and
`SendInput` share one physical-pixel space; a second monitor to the left
just has a negative origin. `map_point`, `screen_to_image`, `rect_to_image`,
`rect_to_screen`, `rect_distance`, `choose`, `match_score`, `risky` and
`_changed` are pure and tested.

## What is recorded

Every decision — instruction, target, point in the picture, point on the
screen, snapped element, read-back, agreement, click point, verified, the
service's latency, ours, outcome and reason — goes to `grounding.recent`
(last 50, in memory), the `grounding` Python logger, and, when *Log
everything* is on, a `grounding` event in the task's `events.jsonl` with the
exact picture the model saw under `shots/`.

**Learning log** (`learning_log`, on whenever the feature is on): one JSON
line per decision in `<data root>/grounding/decisions.jsonl`:

```
ts, instruction, target, window_title,
image                 "captures/<stamp>.png" — the exact PNG sent (relative),
                      or null (no capture, or past learning_log_max_shots)
image_size, region,
model_point           [x, y] in the picture;  model_point_screen
snapped               {name, type, automation_id, source, box, box_image, ocr_text}
snap_distance,
readback              {done, ok (true/false/null), score, label, source, reason}
agreement             {distance, pass1_point, high_risk}
click_point, outcome, reason, verified, verify_reason,
latency_ms, seconds, model_step
```

Local files only; the data root is `agent/` from source,
`%LOCALAPPDATA%\AGIAgent` packaged.

Export to training rows — only decisions that were clicked, read back as the
target, AND verified:

```
python tools/export_grounding.py --out rows.jsonl     [--log path/to/decisions.jsonl]
-> {"image": "<abs png>", "instruction": "click Save", "point": [0.5125, 0.50417],
    "box": [0.475, 0.48333, 0.55, 0.525], "source": "jev-verified",
    "target": "Save", "window_title": ..., "model_point": [...], "ts": ...}
```

`point`/`box` describe the CLICKED element, normalised by the sent picture's
size — not the model's raw point, which may have been snapped from a
neighbour.

What the model is told: a vision click reads `Clicked 'X' via vision:ground
(uia:invoke), verified` or `..., NOT verified: nothing visibly changed, look
before going on`; a refusal reads `Could not click 'X': no locator resolved;
vision grounding: not found — the control at the model's point reads
'Brushes', not 'X'` (or the risky / disagreement reason); a dry run is
reported as **not** clicked.

## How to enable

`settings.json` (under the data root — `agent/` from source,
`%LOCALAPPDATA%\AGIAgent` packaged):

```json
"vision_grounding": {
  "enabled": true,
  "url": "http://127.0.0.1:8765",
  "timeout_s": 60,
  "min_confidence": null,
  "require_inside_window": true,
  "dry_run": false,
  "snap": true,
  "snap_radius_px": 40,
  "snap_budget_s": 1.0,
  "detector_fallback": false,
  "readback": true,
  "readback_min_ratio": 0.8,
  "readback_allow_unreadable": false,
  "agreement": true,
  "max_disagreement": 0.05,
  "allow_risky": false,
  "verify": true,
  "verify_timeout_s": 1.5,
  "learning_log": true,
  "learning_log_max_shots": 2000
}
```

| key | default | meaning |
|---|---|---|
| `enabled` | `false` | the whole feature |
| `url` | `http://127.0.0.1:8765` | loopback only; anything else is reset to the default |
| `timeout_s` | `60` | per `/ground` call. 1–600 |
| `min_confidence` | `null` | measured useless as a signal; leave null |
| `require_inside_window` | `true` | a window must bound the click |
| `dry_run` | `false` | everything but the click |
| `snap` | `true` | move the point onto the actionable element containing it, else the nearest within the radius |
| `snap_radius_px` | `40` | at 96 DPI; scaled by the window's DPI. 0–400 |
| `snap_budget_s` | `1.0` | UIA walk budget (measured 0.2–0.3 s on real windows). 0.1–10 |
| `detector_fallback` | `false` | YOLO+OCR detector when UIA has nothing near the point. Heavy (torch, ~1 GB of models) — and torch currently fails to load in the main venv (DLL init error), in which case it is skipped with a log line |
| `readback` | `true` | the chosen element's label must match the target, else `not_found` |
| `readback_min_ratio` | `0.8` | 0.5–1.0. Score = min(whole-string similarity, word-level F1): an OCR slip ('Sve') passes, an extra or different word ('Save as', 'Sign out') does not. Aliases: digits <-> words ('7' = 'Seven'), operators (+ plus, - minus, × * multiply by, ÷ / divide by, = equals, . decimal separator, % percent, ± positive negative); shortcut hints '(Ctrl+B)' and Turkish dotless i are ignored; a target of 12+ chars that is the leading words of a longer name passes |
| `readback_allow_unreadable` | `false` | click an element with NO readable label (unnamed icon, nothing OCR can read) — never when high-risk, never when it reads as something else |
| `readback_ocr_reach_px` | `200` | extra OCR reach left and right of the point, at 96 DPI (scaled by DPI). 0–1000 |
| `second_look` | `false` | for an unreadable target, re-ask on a magnified crop and click only if it agrees (see 10b and the numbers below) |
| `second_look_fraction` | `0.55` | the second look's crop, as a fraction of the picture's sides. 0.15–0.9 |
| `agreement` | `true` | send `"agreement": true`; use `agreement_distance` / `pass1_point` when returned |
| `max_disagreement` | `0.05` | above it the answer is high-risk (read-back required). 0–1 |
| `allow_risky` | `false` | let vision click delete/pay/send/sign-out-like targets |
| `verify` | `true` | after-click change detection |
| `verify_timeout_s` | `1.5` | 0.2–10 |
| `learning_log` | `true` | decisions.jsonl + captures under `<data>/grounding/` |
| `learning_log_max_shots` | `2000` | stop saving PNGs past this many (the lines continue) |

A block saved before the newer keys existed keeps working: missing keys get
their defaults (`clean_settings`), unknown keys are dropped.

Start with `"dry_run": true` and read what it *would* have clicked in the
task log or the learning log before letting it click. There is no
Settings-tab control yet — edit the file, or `agent.update_settings(
{"vision_grounding": {...}})`; the block is cleaned the same way either
route.

## How to start the service

It is built separately, in the `diffusionvlclaude` folder next to this repo:

```powershell
powershell -ExecutionPolicy Bypass -File <diffusionvlclaude>\a3_service\start_server.ps1
```

Then `curl http://127.0.0.1:8765/health` should answer
`{"ok": true, "model_loaded": ..., "adapter": ..., ...}`. The agent only
needs the contract:

```
POST /ground   {"image_b64": "<png>", "instruction": "click Save", "history": [...],
                "agreement": true}
            -> {"ok": true, "x": 412, "y": 388, "confidence": 0.7|null,
                "latency_ms": 4100, "image_size": [W, H],
                "agreement_distance": 0.031|null,   # optional: 1-pass vs final answer,
                "pass1_point": [x, y]|null, ...}    #   / (picture diagonal / sqrt 2)
            |  {"ok": false, "error": "..."}
GET  /health   -> {"ok": true, "model_loaded": bool, "adapter": str, ...}
```

## Trying it on a real window

```
python tools/try_vision.py --list                          the windows it can see
python tools/try_vision.py "Save" --window Notepad         dry run: what JEV would do
python tools/try_vision.py "Save" --window Notepad --click really click
python tools/try_vision.py "Save"                          the foreground window after 3 s
```

Uses the settings' `vision_grounding` block (forced on; dry run unless
`--click`), prints the decision (model point, element, read-back, second
look, timing) and saves a marked picture under `<data>/grounding/try/`:
red = the model's point, green box = the element, cross = the click (orange
if refused), blue = the second look.

## Measured (2026-10-14, stage-2 model, 150 held-out items, 10 unseen apps/sites)

Same first answers, JEV's real code; correct / wrong clicks of 128 visible targets:

| world | before | + OCR reach 200 | + second look | unreadable clicked blindly |
|---|---|---|---|---|
| UIA names (normal) | 86 / 1 | 86 / 1 | 86 / 1 | 86 / 1 |
| UIA boxes, no names (icons) | 25 / 0 | 25 / 0 | 44 / 8 (+1 absent) | 46 / 15 (+2 absent) |
| no UIA (canvas) | 14 / 0 | 24 / 0 | 40 / 6 (+1 absent) | 49 / 13 (+3 absent) |

The OCR reach is free (on). The second look roughly halves the wrong
clicks of a blind click but stays ~85% precise; stricter variants (tighter
match, two crop sizes) did not fix it — the model re-points at the same wrong
icon. Hence off; an independent check (another model judging the crop) is
the open lever.

## Limits — read before relying on it

- **It is wrong often.** 56% of raw points land inside the requested element
  in the field test (links 100%, fields 75%, tabs 64%, icons 18–27%). Snap
  and read-back turn neighbour-misses into hits and absent targets into
  `not_found`; they cannot make an unlabelled icon checkable — with
  read-back on (the default) an icon nothing can read is refused, which is
  the price of precision. `readback_allow_unreadable` trades it back.
- **Read-back is only as good as the labels.** A UIA name that matched the
  target exactly would already have been found by the locator, so in
  practice read-back works on fuzzy UIA names ('Seven' for '7') and on OCR.
- **Cost:** ~4 s a call on this PC, plus the capture, ~0.3 s of UIA walk,
  ~10 ms of OCR, and up to `verify_timeout_s` when nothing changes.
- **One point, one click.** `index=` / `near=` (one of several same-named
  controls) never fall back to vision; a point cannot promise "the second
  one". Right-click, double-click, drag and `do_steps` are untouched.
- **Verification is a signal, not proof.** Any change in the window (an
  animation, a video) counts; an effect outside the target window that does
  not change the foreground does not. Nothing is retried automatically.
- **The confidence is not used.** Measured median 0.18 on hits, 0.15 on
  misses, 0.20 on absent targets.
- **Screenshots stay local**, by construction (loopback-only URL); the
  learning log keeps PNGs on disk under the data root (capped).
- **Not in the replay ladder.** `replay._alternate` and `do_step` for saved
  jobs do not use it; only the live `click` tool's primitive does.
- The settings UI has no control for the block yet.

```
python tests/test_grounding.py      # 180/180, no service, no screen, no mouse
python tools/export_grounding.py    # summary of the learning log
```
