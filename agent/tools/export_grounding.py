"""Turn the vision-grounding learning log into training rows.

The fallback (core/grounding.py) writes one JSON line per decision to
<data>/grounding/decisions.jsonl, with the exact picture it sent under
<data>/grounding/captures/. A decision is a usable training example only when
it is known to be RIGHT, which here means all three of:

    outcome  == "clicked"      input actually went out
    readback.ok is True        the clicked element's own label matched the
                               requested target (UIA name or OCR of its box)
                               — not a "second look": the model agreeing with
                               itself is not a label
    verified is True           something observable changed afterwards

Each such decision becomes one row:

    {"image": "<absolute path of the png>", "instruction": "click Save",
     "point": [x, y], "box": [x1, y1, x2, y2], "source": "jev-verified", ...}

`point` and `box` are normalised to 0..1 by the size of the picture sent,
and describe the element that was clicked (its box and centre) — not the
model's raw point, which may have been snapped onto it from a neighbour.

Local files only; nothing here touches the network.

    python tools/export_grounding.py                 # data root's log -> stdout summary
    python tools/export_grounding.py --out rows.jsonl
    python tools/export_grounding.py --log path/to/decisions.jsonl --out rows.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _default_log() -> Path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from core import paths
    return paths.data_root() / "grounding" / "decisions.jsonl"


def is_verified_correct(rec: dict) -> bool:
    rb = rec.get("readback") or {}
    return (rec.get("outcome") == "clicked" and rec.get("verified") is True
            and rb.get("ok") is True and rb.get("source") != "second_look"
            and bool(rec.get("image"))
            and bool((rec.get("snapped") or {}).get("box_image")))


def to_row(rec: dict, base: Path) -> dict | None:
    """One decision -> one training row, or None if it cannot be one."""
    if not is_verified_correct(rec):
        return None
    try:
        w, h = (int(v) for v in rec["image_size"])
        x1, y1, x2, y2 = (float(v) for v in rec["snapped"]["box_image"])
    except (KeyError, TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    img = (base / rec["image"]).resolve()
    if not img.exists():
        return None

    def cx(v: float) -> float:
        return round(min(max(v / w, 0.0), 1.0), 5)

    def cy(v: float) -> float:
        return round(min(max(v / h, 0.0), 1.0), 5)

    box = [cx(x1), cy(y1), cx(x2), cy(y2)]
    return {
        "image": str(img),
        "instruction": rec.get("instruction") or f"click {rec.get('target', '')}",
        "point": [cx((x1 + x2) / 2), cy((y1 + y2) / 2)],
        "box": box,
        "source": "jev-verified",
        "target": rec.get("target", ""),
        "window_title": rec.get("window_title", ""),
        "model_point": ([cx(rec["model_point"][0]), cy(rec["model_point"][1])]
                        if rec.get("model_point") else None),
        "ts": rec.get("ts"),
    }


def export(log: Path, out: Path | None = None) -> tuple[int, int, list[dict]]:
    """(decisions read, rows exported, rows). Writes `out` when given."""
    log = Path(log)
    rows, n = [], 0
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            n += 1
            row = to_row(rec, log.parent)
            if row is not None:
                rows.append(row)
    if out is not None:
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return n, len(rows), rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--log", type=Path, default=None,
                    help="decisions.jsonl (default: the data root's)")
    ap.add_argument("--out", type=Path, default=None,
                    help="where to write the rows (JSON lines)")
    a = ap.parse_args(argv)
    log = a.log or _default_log()
    n, k, _ = export(log, a.out)
    print(f"{n} decision(s) in {log}; {k} verified-correct row(s)"
          + (f" -> {a.out}" if a.out else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
