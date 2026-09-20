"""Core daemon — one per machine, always warm, and the app's backend.

Why a daemon rather than more MCP servers, from measurements taken earlier:

    16s      model load paid on every MCP process start
    crash    a failed load reported as "still loading" forever
    3 panels could drive the mouse simultaneously, nothing arbitrating
    no home  macro state had nowhere to live between sessions

All four are lifetime problems, not API problems. Loading once and holding the
lock in a process that outlives any single Claude session fixes them together.

Bound to 127.0.0.1 with a token. The repository's existing HTTP server binds
0.0.0.0 with no auth, which is a full mouse/keyboard/screen surface exposed to
the local network; this replaces it rather than extending it.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from core import act, actuator, display, locator
from core.arbiter import Arbiter
from core.machine_profile import load_profile
from core.recorder import Recorder
from core.replay import Replayer
from core.store import Store

HERE = Path(__file__).resolve().parent
TOKEN_FILE = HERE / ".daemon-token"
RECORDINGS = HERE / "recordings"

app = Flask(__name__, static_folder=str(HERE / "ui"), static_url_path="")

store = Store()
_arbiter: Arbiter | None = None
_recorder: Recorder | None = None
_events: list[dict] = []
_events_lock = threading.Lock()


def token() -> str:
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    t = secrets.token_urlsafe(24)
    TOKEN_FILE.write_text(t, encoding="utf-8")
    return t


TOKEN = token()


def _push(kind: str, data: dict) -> None:
    with _events_lock:
        _events.append({"t": time.time(), "kind": kind, **data})
        del _events[:-400]


@app.before_request
def _auth():
    if request.path.startswith(("/api/",)):
        supplied = (request.headers.get("X-Token")
                    or request.args.get("token", ""))
        if supplied != TOKEN:
            return jsonify({"error": "bad or missing token"}), 401


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@app.get("/api/status")
def status():
    prof = load_profile()
    return jsonify({
        "ok": True,
        "machine": prof["machine"],
        "screen": prof["screen"],
        "capabilities": prof["capabilities"],
        "locked": display.is_locked(),
        "arbiter": _arbiter.status() if _arbiter else None,
        "recording": _recorder is not None,
        "macros": len(store.list()),
    })


@app.get("/api/windows")
def windows():
    return jsonify({"windows": locator.list_windows()})


@app.get("/api/events")
def events():
    since = float(request.args.get("since", 0))
    with _events_lock:
        return jsonify({"events": [e for e in _events if e["t"] > since],
                        "now": time.time()})


# ---------------------------------------------------------------------------
# control
# ---------------------------------------------------------------------------

@app.post("/api/control/acquire")
def acquire():
    global _arbiter
    if _arbiter is None:
        _arbiter = Arbiter(owner="AGI Agent app")
    ok = _arbiter.start()
    _push("control", acquired=ok, detail=_arbiter.state.detail)
    return jsonify({"ok": ok, "status": _arbiter.status()})


@app.post("/api/control/release")
def release():
    global _arbiter
    if _arbiter is not None:
        _arbiter.stop()
        _arbiter = None
    _push("control", acquired=False, detail="released")
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# recording
# ---------------------------------------------------------------------------

@app.post("/api/record/start")
def record_start():
    global _recorder
    if _recorder is not None:
        return jsonify({"ok": False, "error": "already recording"}), 409
    RECORDINGS.mkdir(exist_ok=True)
    d = RECORDINGS / time.strftime("rec_%Y%m%d_%H%M%S")
    _recorder = Recorder(d)
    ok = _recorder.start()
    if not ok:
        _recorder = None
        return jsonify({"ok": False, "error": "input hook failed to start"}), 500
    _push("record", state="started", dir=str(d))
    return jsonify({"ok": True, "dir": str(d)})


@app.post("/api/record/stop")
def record_stop():
    global _recorder
    if _recorder is None:
        return jsonify({"ok": False, "error": "not recording"}), 409
    meta = _recorder.stop()
    summary = _recorder.summary()
    _recorder = None
    _push("record", state="stopped", **meta)
    return jsonify({"ok": True, "meta": meta, "summary": summary})


@app.get("/api/record/live")
def record_live():
    if _recorder is None:
        return jsonify({"recording": False})
    return jsonify({"recording": True,
                    "events": len(_recorder.events),
                    "frames": len(_recorder.frames),
                    "last": [e.type for e in _recorder.events[-6:]]})


# ---------------------------------------------------------------------------
# macros
# ---------------------------------------------------------------------------

@app.get("/api/macros")
def macros():
    return jsonify({"macros": store.list()})


@app.get("/api/macros/<mid>")
def macro_detail(mid):
    try:
        m, l = store.load(mid)
    except Exception as e:
        return jsonify({"error": str(e)}), 404
    return jsonify({"macro": m.to_dict(), "local": {
        "trust": l.trust.value, "runs": l.stats.runs,
        "successes": l.stats.successes, "failures": l.stats.failures,
        "success_rate": l.stats.success_rate, "origin": l.origin}})


@app.delete("/api/macros/<mid>")
def macro_delete(mid):
    return jsonify({"ok": store.delete(mid)})


@app.post("/api/macros/<mid>/run")
def macro_run(mid):
    body = request.get_json(silent=True) or {}
    rp = Replayer(store, on_event=lambda k, d: _push(f"replay.{k}", d))
    res = rp.run(mid, params=body.get("params"), arbiter=_arbiter,
                 supervised=body.get("supervised"))
    return jsonify({
        "ok": res.ok, "reason": res.stopped_reason,
        "needs_user": res.needs_user,
        "trust": {"before": res.trust_before, "after": res.trust_after},
        "elapsed_s": round(res.elapsed_s, 2),
        "steps": [{"index": o.index, "intent": o.intent, "ok": o.ok,
                   "attempts": o.attempts, "improvised": o.improvised,
                   "error": o.error, "line": o.line()} for o in res.outcomes],
        "report": res.report(),
    })


@app.get("/api/macros/<mid>/export")
def macro_export(mid):
    m, _ = store.load(mid)
    return jsonify(m.to_dict())


# ---------------------------------------------------------------------------
# direct actions (used by the MCP layer and for smoke-testing from the UI)
# ---------------------------------------------------------------------------

@app.post("/api/act/click")
def api_click():
    b = request.get_json(force=True)
    if _arbiter and not _arbiter.guard(f"click {b.get('name') or b.get('automation_id')}"):
        return jsonify({"ok": False, "error": f"arbiter: {_arbiter.state.detail}"}), 409
    r = act.click_element(name=b.get("name"), control=b.get("control"),
                          automation_id=b.get("automation_id"),
                          text=b.get("text"), window=b.get("window"),
                          prefer=b.get("prefer", "invoke"))
    return jsonify({"ok": r.ok, "method": r.method, "error": r.error,
                    "locator_index": r.locator_index})


@app.post("/api/act/type")
def api_type():
    b = request.get_json(force=True)
    if _arbiter and not _arbiter.guard("type text"):
        return jsonify({"ok": False, "error": f"arbiter: {_arbiter.state.detail}"}), 409
    r = actuator.type_text(b.get("text", ""))
    return jsonify({"ok": r.ok, "method": r.method})


@app.post("/api/act/key")
def api_key():
    b = request.get_json(force=True)
    if _arbiter and not _arbiter.guard(f"key {b.get('keys')}"):
        return jsonify({"ok": False, "error": f"arbiter: {_arbiter.state.detail}"}), 409
    keys = b.get("keys", "")
    r = actuator.send_keys(keys) if "+" in keys else actuator.press_key(keys)
    return jsonify({"ok": r.ok, "method": r.method})


@app.get("/api/inspect")
def inspect():
    title = request.args.get("window")
    h = locator.find_window(title_contains=title) if title else locator.foreground_window()
    if not h:
        return jsonify({"error": "no window"}), 404
    root = locator.uia_root(h)
    els = []
    for c in locator.walk(root, max_depth=10, max_nodes=250, budget_s=3.0):
        r = locator._rect_of(c)
        if not r:
            continue
        try:
            els.append({"name": c.Name or "", "type": c.ControlTypeName or "",
                        "automation_id": c.AutomationId or "", "rect": r})
        except Exception:
            continue
    return jsonify({"window": locator.window_title(h), "hwnd": h,
                    "count": len(els), "elements": els[:200],
                    "diagnosis": locator.diagnose(h)})


# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


def run(host: str = "127.0.0.1", port: int = 7900) -> None:
    print("=" * 62)
    print("  AGI Agent daemon")
    print(f"  open:  http://{host}:{port}/?token={TOKEN}")
    print(f"  token: {TOKEN}")
    print("=" * 62, flush=True)
    app.run(host=host, port=port, threaded=True, debug=False)


if __name__ == "__main__":
    run()
