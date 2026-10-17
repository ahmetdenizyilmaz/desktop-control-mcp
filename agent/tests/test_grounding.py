"""Vision grounding fallback — every gate, with nothing real behind it.

No screen is captured, no mouse is moved, no model is loaded. The service is
a loopback HTTP mock that answers whatever a check tells it to; the capture
is a drawn picture pushed through the same encoder the real path uses; the
actuator is a stub that only records what it was asked to click. What is
tested is the part that can go wrong silently: the coordinate mapping, the
refusals, and that a service that is down leaves the agent exactly as it
was before the feature existed.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cleanup import scratch_dir  # noqa: E402

_TMP = str(scratch_dir("grounding_test_"))
os.environ["DESKTOP_AGENT_DATA"] = _TMP

from PIL import Image                      # noqa: E402

from core import grounding, tasklog        # noqa: E402
from core.actuator import ActResult        # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  — {detail}" if detail else ""), flush=True)
    return bool(ok)


# ---------------------------------------------------------------------------
# the mock service
# ---------------------------------------------------------------------------

class Mock:
    """A /health and /ground on 127.0.0.1 whose answers a check can set."""

    def __init__(self) -> None:
        self.health = {"ok": True, "model_loaded": True, "adapter": "mock",
                       "model_step": 4, "steps_default": 4}
        self.answer = {"x": 400, "y": 300, "confidence": 0.9}
        self.delay = 0.0
        self.requests: list[dict] = []
        self._srv = None

    def ground(self, payload: dict) -> dict:
        import base64
        import io
        img = Image.open(io.BytesIO(base64.b64decode(payload["image_b64"])))
        out = {"ok": True, "raw": "<point>", "latency_ms": 42,
               "image_size": list(img.size), "resized_size": list(img.size),
               "model_step": 4, "confidence": None}
        out.update(self.answer)
        return out

    def start(self) -> "Mock":
        mock = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):          # keep the suite's output clean
                pass

            def _send(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/health":
                    return self._send(mock.health)
                self._send({"ok": False, "error": "no such route"}, 404)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(n) or b"{}")
                if self.path != "/ground":
                    return self._send({"ok": False, "error": "no such route"}, 404)
                mock.requests.append(payload)
                if mock.delay:
                    time.sleep(mock.delay)
                self._send(mock.ground(payload))

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        # The timeout check abandons a request mid-sleep; the handler's
        # write to a closed socket is expected, not worth a traceback.
        self._srv.handle_error = lambda *a: None
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        return self

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._srv.server_address[1]}"

    def stop(self) -> None:
        self._srv.shutdown()


# ---------------------------------------------------------------------------
# the fakes around the module
# ---------------------------------------------------------------------------

clicks: list = []
captures: list = []
prefers: list = []
ocr_boxes: list = []
observed: list = []
detector_calls: list = []


def fake_capture(size=(800, 600), region=None, rect=None, title="Notepad",
                 hwnd=1234):
    img = Image.new("RGB", size, (180, 180, 180))
    region = region or {"left": 100, "top": 50, "width": size[0],
                        "height": size[1]}
    if rect is None:
        rect = (region["left"], region["top"], region["left"] + region["width"],
                region["top"] + region["height"])
    return grounding.encode(img, region, rect, title, hwnd)


class Env:
    """One check's world: what the capture returns, where the window is
    afterwards, what the actuator answers."""

    def __init__(self, cap="default", rect_after=None,
                 act=ActResult(True, "sendinput", 5.0, True, "(x,y)"),
                 cands=None, ocr=None, observe=None, dpi=1.0,
                 detector=None) -> None:
        # None is meaningful here: "the capture fails".
        self.cap = fake_capture() if cap == "default" else cap
        self.rect_after = rect_after
        self.act = act
        # The world around the point: UIA candidates, what OCR reads
        # (a function (img, box) -> [(rect, text)] in picture pixels, or
        # None = no OCR engine), what the screen looks like before/after
        # the click (a list of states, the last repeated), the DPI scale,
        # and the detector's boxes.
        self.cands = list(cands or [])
        self.ocr = ocr
        self.observe = list(observe or [{}])
        self.dpi = dpi
        self.detector = detector

    def __enter__(self):
        self._saved = (grounding._capture_window, grounding._window_rect,
                       grounding._act, grounding._uia_candidates,
                       grounding._ocr_lines, grounding._observe,
                       grounding._dpi_scale, grounding._detector_candidates)
        captures.clear()
        clicks.clear()
        prefers.clear()
        ocr_boxes.clear()
        observed.clear()
        detector_calls.clear()

        def capture(hwnd):
            captures.append(hwnd)
            if self.cap is None:
                raise grounding.CaptureError("no window on any screen")
            return self.cap

        def rect(hwnd):
            return self.rect_after or self.cap.window_rect

        def act(el, prefer="inject"):
            clicks.append(el)
            prefers.append(prefer)
            if isinstance(self.act, Exception):
                raise self.act
            return self.act

        def uia(hwnd, budget_s, max_nodes=1500):
            return [grounding.Cand(**c.__dict__) for c in self.cands]

        def ocr(img, box):
            ocr_boxes.append(tuple(box))
            return None if self.ocr is None else self.ocr(img, box)

        def observe(cand, rect, hwnd):
            observed.append(rect)
            st = self.observe.pop(0) if len(self.observe) > 1 else self.observe[0]
            if isinstance(st, Exception):
                raise st
            return st

        def detector(cap):
            detector_calls.append(cap)
            return list(self.detector or [])

        grounding._capture_window = capture
        grounding._window_rect = rect
        grounding._act = act
        grounding._uia_candidates = uia
        grounding._ocr_lines = ocr
        grounding._observe = observe
        grounding._dpi_scale = lambda hwnd: self.dpi
        grounding._detector_candidates = detector
        return self

    def __exit__(self, *exc):
        (grounding._capture_window, grounding._window_rect,
         grounding._act, grounding._uia_candidates, grounding._ocr_lines,
         grounding._observe, grounding._dpi_scale,
         grounding._detector_candidates) = self._saved
        return False


#: The checks in sections 1–16 test the geometry gates and the plumbing as
#: they were before snap / read-back / verify / the learning log existed, so
#: they run with those switched off; sections 17+ switch them on.
LEGACY = {"snap": False, "readback": False, "verify": False,
          "learning_log": False, "agreement": False}


def configure(url: str, **kw) -> None:
    grounding.configure({"enabled": True, "url": url, "timeout_s": 5,
                         **LEGACY, **kw})
    grounding._health.update(ok=None, at=0.0, detail="")
    grounding.recent.clear()


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def test_off_by_default(mock: Mock):
    print("\n1. off by default, and off means nothing happens")
    grounding.configure({})
    check("disabled in the defaults", grounding.enabled() is False)
    check("the default URL is loopback",
          grounding.DEFAULTS["url"] == "http://127.0.0.1:8765")
    before = len(mock.requests)
    with Env():
        d = grounding.click_by_vision("click Save", 1234)
    check("disabled: no capture, no request, no click",
          d.outcome == "disabled" and not captures and not clicks
          and len(mock.requests) == before, d.summary())
    check("a disabled attempt is not logged as a decision",
          len(grounding.recent) == 0)


def test_settings_cleaning():
    print("\n2. the settings block is sanitised, never trusted")
    s = grounding.clean_settings({"enabled": "yes", "url": "http://localhost:9000/",
                                  "timeout_s": "7", "min_confidence": "0.4",
                                  "require_inside_window": 0, "dry_run": "true",
                                  "bogus": 1})
    check("strings become the right types",
          s["enabled"] is True and s["timeout_s"] == 7.0
          and s["min_confidence"] == 0.4 and s["require_inside_window"] is False
          and s["dry_run"] is True, str(s))
    check("a trailing slash is dropped", s["url"] == "http://localhost:9000")
    check("unknown keys are dropped", "bogus" not in s)
    s = grounding.clean_settings({"url": "http://example.com:8765"})
    check("a non-loopback URL is refused — screenshots stay on this machine",
          s["url"] == grounding.DEFAULTS["url"], s["url"])
    s = grounding.clean_settings({"timeout_s": -5, "min_confidence": 3})
    check("out-of-range numbers are clamped",
          s["timeout_s"] == 1.0 and s["min_confidence"] == 1.0, str(s))
    s = grounding.clean_settings({"min_confidence": ""})
    check("an empty threshold means no gate", s["min_confidence"] is None)
    s = grounding.clean_settings('{"enabled": true}')
    check("a JSON string is accepted too", s["enabled"] is True)
    s = grounding.clean_settings("not json")
    check("garbage gives the defaults", s == grounding.DEFAULTS)
    got = grounding.configure({"vision_grounding": {"enabled": True,
                                                    "url": "http://127.0.0.1:1"}})
    check("configure accepts the whole settings dict",
          got["enabled"] is True and got["url"] == "http://127.0.0.1:1")
    grounding.configure({})


def test_mapping():
    print("\n3. a point in the picture becomes a point on the screen")
    region = {"left": 100, "top": 50, "width": 800, "height": 600}
    check("offset only, when the picture was sent as captured",
          grounding.map_point(400, 300, region, (800, 600)) == (500, 350))
    check("a monitor left of the primary: negative origin",
          grounding.map_point(10, 20, {"left": -1920, "top": -200,
                                       "width": 1920, "height": 1080},
                              (1920, 1080)) == (-1910, -180))
    big = {"left": 0, "top": 0, "width": 3200, "height": 2400}
    check("a shrunk capture scales back up (2x)",
          grounding.map_point(1280, 960, big, (1600, 1200)) == (2560, 1920))
    check("scale and offset together, rounded",
          grounding.map_point(1, 1, {"left": 7, "top": 3, "width": 300,
                                     "height": 300}, (100, 100)) == (10, 6))
    check("inside_image is half-open",
          grounding.inside_image(0, 0, (800, 600))
          and grounding.inside_image(799, 599, (800, 600))
          and not grounding.inside_image(800, 300, (800, 600))
          and not grounding.inside_image(10, -1, (800, 600)))
    check("inside_rect is half-open",
          grounding.inside_rect(100, 50, (100, 50, 900, 650))
          and not grounding.inside_rect(900, 300, (100, 50, 900, 650)))
    cap = fake_capture(size=(3200, 2400), region={"left": 0, "top": 0,
                                                  "width": 3200, "height": 2400})
    check(f"encode shrinks a long edge over {grounding.MAX_SIDE}",
          cap.sent_size == (2560, 1920), str(cap.sent_size))
    check("and keeps the region as captured",
          cap.region["width"] == 3200 and cap.region["height"] == 2400)
    check("a small capture is sent as is",
          fake_capture().sent_size == (800, 600))


def test_accepted_click(mock: Mock):
    print("\n4. the happy path: capture, ask, gate, click through the actuator")
    configure(mock.url)
    mock.answer = {"x": 400, "y": 300, "confidence": 0.9}
    before = len(mock.requests)
    with Env():
        d = grounding.click_by_vision("click Save", 1234, history=["typed hello"])
    check("outcome is clicked", d.outcome == "clicked" and d.clicked
          and d.accepted, d.summary())
    check("the point is the image point plus the capture's origin",
          d.point == (500, 350) and d.image_point == (400, 300), str(d.point))
    check("exactly one click went to the actuator", len(clicks) == 1)
    el = clicks[0]
    check("as an Element centred on the point, owned by the window",
          el.center == (500, 350) and el.hwnd == 1234
          and el.source == "vision:ground", f"{el.center} {el.hwnd} {el.source}")
    check("the method is the actuator's", d.method == "sendinput")
    check("service fields are carried", d.confidence == 0.9
          and d.latency_ms == 42 and d.model_step == 4 and d.raw == "<point>")
    check("the window was captured, not the desktop", captures == [1234])
    req = mock.requests[-1]
    check("one request, with the picture, the instruction and the history",
          len(mock.requests) == before + 1 and req.get("image_b64")
          and req.get("instruction") == "click Save"
          and req.get("history") == ["typed hello"], str({k: v for k, v in
                                                           req.items() if k != "image_b64"}))
    check("no image_path — the picture travels inline", "image_path" not in req)
    check("the decision is on the recent list",
          grounding.recent and grounding.recent[-1]["outcome"] == "clicked")
    check("health is remembered as up",
          grounding._health["ok"] is True and "mock" in grounding._health["detail"])


def test_scaled_capture(mock: Mock):
    print("\n5. a large window: the answer is scaled back to screen pixels")
    configure(mock.url)
    big = fake_capture(size=(3200, 2400), region={"left": -3200, "top": 0,
                                                  "width": 3200, "height": 2400})
    mock.answer = {"x": 1280, "y": 960, "confidence": 0.8}
    with Env(cap=big):
        d = grounding.click_by_vision("click the icon", 1234)
    check("sent at 2560x1920", d.sent_size == (2560, 1920))
    check("(1280,960) in the sent picture -> (-3200+1600, 1200)",
          d.clicked and d.point == (-1600, 1200), d.summary())
    check("the service's reported image_size matched what was sent",
          mock.requests[-1] is not None and d.outcome == "clicked")


def test_size_mismatch(mock: Mock):
    print("\n6. a service that saw a different picture is not trusted")
    configure(mock.url)
    mock.answer = {"x": 100, "y": 100, "image_size": [640, 480]}
    with Env():
        d = grounding.click_by_vision("click Save", 1234)
    check("refused on image_size mismatch", d.outcome == "refused"
          and "640x480" in d.reason and not clicks, d.summary())


def test_outside_window(mock: Mock):
    print("\n7. a point outside the picture is refused, whatever the setting")
    configure(mock.url)
    mock.answer = {"x": 900, "y": 300}
    with Env():
        d = grounding.click_by_vision("click Save", 1234)
    check("x past the right edge: refused, no click",
          d.outcome == "refused" and "outside" in d.reason and not clicks,
          d.summary())
    mock.answer = {"x": 10, "y": -1}
    with Env():
        d = grounding.click_by_vision("click Save", 1234)
    check("negative y: refused", d.outcome == "refused" and not clicks)
    configure(mock.url, require_inside_window=False)
    mock.answer = {"x": 900, "y": 300}
    with Env():
        d = grounding.click_by_vision("click Save", 1234)
    check("even with require_inside_window off",
          d.outcome == "refused" and not clicks, d.summary())
    # The picture is clipped to the desktop, so a point inside the picture is
    # inside the window — unless the window was reported larger than it is.
    configure(mock.url)
    mock.answer = {"x": 400, "y": 300}
    small = fake_capture(rect=(100, 50, 300, 200))     # window rect too small
    with Env(cap=small):
        d = grounding.click_by_vision("click Save", 1234)
    check("a point outside the window's own rectangle is refused",
          d.outcome == "refused" and "outside the window" in d.reason
          and not clicks, d.summary())


def test_no_window(mock: Mock):
    print("\n8. no window to bound the click")
    configure(mock.url)
    with Env():
        d = grounding.click_by_vision("click Save", None)
    check("require_inside_window: refused before any capture",
          d.outcome == "refused" and not captures and not clicks, d.summary())
    configure(mock.url, require_inside_window=False)
    mock.answer = {"x": 400, "y": 300}
    screen = fake_capture(region={"left": 0, "top": 0, "width": 800,
                                  "height": 600}, rect=None, title="", hwnd=0)
    with Env(cap=screen):
        d = grounding.click_by_vision("click Save", None)
    check("with it off, the primary screen is captured and clicked",
          d.outcome == "clicked" and d.point == (400, 300) and captures == [None],
          d.summary())


def test_confidence(mock: Mock):
    print("\n9. the confidence gate")
    configure(mock.url, min_confidence=0.5)
    mock.answer = {"x": 400, "y": 300, "confidence": 0.31}
    with Env():
        d = grounding.click_by_vision("click Save", 1234)
    check("0.31 under a 0.5 gate: refused, no click",
          d.outcome == "refused" and "0.31" in d.reason and not clicks,
          d.summary())
    mock.answer = {"x": 400, "y": 300, "confidence": 0.5}
    with Env():
        d = grounding.click_by_vision("click Save", 1234)
    check("exactly at the gate passes", d.outcome == "clicked")
    mock.answer = {"x": 400, "y": 300, "confidence": None}
    with Env():
        d = grounding.click_by_vision("click Save", 1234)
    check("no confidence reported: the gate does not apply",
          d.outcome == "clicked" and d.confidence is None, d.summary())
    configure(mock.url)                              # min_confidence None
    mock.answer = {"x": 400, "y": 300, "confidence": 0.05}
    with Env():
        d = grounding.click_by_vision("click Save", 1234)
    check("no gate configured: a low confidence still clicks",
          d.outcome == "clicked")


def test_service_down(mock: Mock):
    print("\n10. the service is down: the fallback is simply unavailable")
    configure("http://127.0.0.1:9")            # discard port: nothing listens
    with Env():
        d = grounding.click_by_vision("click Save", 1234)
    check("unavailable, with the reason",
          d.outcome == "unavailable" and "service down" in d.reason, d.summary())
    check("no capture, no click", not captures and not clicks)
    check("down is remembered", grounding._health["ok"] is False)
    at = grounding._health["at"]
    with Env():
        d = grounding.click_by_vision("click Save", 1234)
    check("the next attempt does not knock again within the retry window",
          grounding._health["at"] == at and d.outcome == "unavailable")
    ok, why = grounding.health(force=True)
    check("a forced health check asks again", ok is False and why)

    print("   ... and a service that answers badly")
    configure(mock.url)
    saved = mock.health
    mock.health = {"ok": False, "error": "model not loaded"}
    with Env():
        d = grounding.click_by_vision("click Save", 1234)
    check("/health not ok: unavailable", d.outcome == "unavailable", d.summary())
    mock.health = saved

    configure(mock.url)
    mock.answer = {"ok": False, "error": "could not parse a point"}
    with Env():
        d = grounding.click_by_vision("click Save", 1234)
    check("/ground ok:false: refused with the service's error",
          d.outcome == "refused" and "could not parse" in d.reason and not clicks,
          d.summary())

    configure(mock.url, timeout_s=1)
    mock.answer = {"x": 400, "y": 300}
    mock.delay = 2.5
    with Env():
        d = grounding.click_by_vision("click Save", 1234)
    mock.delay = 0.0
    check("a call past timeout_s is unavailable, not a click",
          d.outcome == "unavailable" and not clicks, d.summary())
    check("and marks the service down", grounding._health["ok"] is False)
    grounding._health.update(ok=None, at=0.0)

    configure(mock.url)
    with Env(cap=None):
        d = grounding.click_by_vision("click Save", 1234)
    check("a failed capture (asleep, locked, minimised) is unavailable",
          d.outcome == "unavailable" and "capture failed" in d.reason
          and not clicks and len(mock.requests) > 0, d.summary())


def test_dry_run(mock: Mock):
    print("\n11. dry run: everything but the click")
    configure(mock.url, dry_run=True)
    mock.answer = {"x": 400, "y": 300, "confidence": 0.7}
    with Env():
        d = grounding.click_by_vision("click Save", 1234)
    check("accepted, not clicked", d.outcome == "dry_run" and d.accepted
          and not d.clicked, d.summary())
    check("no click reached the actuator", not clicks)
    check("the would-be point is in the reason", "(500,350)" in d.reason
          and d.point == (500, 350), d.reason)
    check("it is logged like any decision",
          grounding.recent[-1]["outcome"] == "dry_run")
    mock.answer = {"x": 900, "y": 300}
    with Env():
        d = grounding.click_by_vision("click Save", 1234)
    check("a dry run still refuses a bad point", d.outcome == "refused")


def test_window_moved(mock: Mock):
    print("\n12. the window moved while the model was thinking")
    configure(mock.url)
    mock.answer = {"x": 400, "y": 300}
    with Env(rect_after=(140, 50, 940, 650)):
        d = grounding.click_by_vision("click Save", 1234)
    check("refused, no click", d.outcome == "refused" and "moved" in d.reason
          and not clicks, d.summary())


def test_actuator_refusal(mock: Mock):
    print("\n13. the actuator keeps its own veto")
    configure(mock.url)
    mock.answer = {"x": 400, "y": 300}
    with Env(act=ActResult(False, "sendinput", 3.0,
                           detail="could not foreground window 1234; refused to inject")):
        d = grounding.click_by_vision("click Save", 1234)
    check("a refused injection is not a click",
          d.outcome == "not_clicked" and not d.clicked and d.accepted
          and "foreground" in d.reason, d.summary())
    with Env(act=RuntimeError("boom")):
        d = grounding.click_by_vision("click Save", 1234)
    check("an actuator exception is a decision, not a crash",
          d.outcome == "not_clicked" and "boom" in d.reason)


def test_history_provider(mock: Mock):
    print("\n14. action history comes from the app when the caller has none")
    configure(mock.url)
    mock.answer = {"x": 400, "y": 300}
    grounding.history_provider = lambda: [f"step {i}" for i in range(10)]
    try:
        with Env():
            d = grounding.click_by_vision("click Save", 1234)
    finally:
        grounding.history_provider = None
    req = mock.requests[-1]
    check(f"the last {grounding.HISTORY_N} actions are sent",
          req.get("history") == [f"step {i}" for i in range(4, 10)],
          str(req.get("history")))
    grounding.history_provider = lambda: 1 / 0
    try:
        with Env():
            d = grounding.click_by_vision("click Save", 1234)
    finally:
        grounding.history_provider = None
    check("a broken provider costs nothing",
          d.outcome == "clicked" and "history" not in mock.requests[-1])


def test_tasklog(mock: Mock):
    print("\n15. every decision reaches the task log when logging is on")
    configure(mock.url)
    mock.answer = {"x": 400, "y": 300, "confidence": 0.66}
    log = tasklog.start("grounding log test", True)
    try:
        with Env():
            grounding.click_by_vision("click Save", 1234)
        configure(mock.url, min_confidence=0.9)
        with Env():
            grounding.click_by_vision("click Save", 1234)
    finally:
        tasklog.stop("done")
    lines = [json.loads(l) for l in
             (log.dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    ev = [l for l in lines if l["kind"] == "grounding"]
    check("two grounding events", len(ev) == 2, str(len(ev)))
    check("with instruction, point, confidence, latency and outcome",
          ev[0]["instruction"] == "click Save" and ev[0]["point"] == [500, 350]
          and ev[0]["confidence"] == 0.66 and ev[0]["latency_ms"] == 42
          and ev[0]["outcome"] == "clicked", json.dumps(ev[0])[:200])
    check("the refusal says why", ev[1]["outcome"] == "refused"
          and "0.66" in ev[1]["reason"])
    shot = ev[0].get("screenshot")
    check("the picture the model saw is kept",
          shot and (log.dir / shot).exists(), str(shot))
    check("and the log lives under the test's data dir, not the real one",
          str(log.dir).startswith(_TMP), str(log.dir))


def test_act_integration(mock: Mock):
    print("\n16. act.click_element: the fallback fires only on a plain miss")
    from core import act, locator
    saved = (locator.resolve, locator.foreground_window, locator.invalidate_cache)
    locator.resolve = lambda locs, scope=None, order=None, allow_slow=False: \
        locator.ResolveResult(element=None, tried=[(0, "uia_name_type", False, 0.01)])
    locator.foreground_window = lambda: 1234
    locator.invalidate_cache = lambda hwnd=None: None
    try:
        grounding.configure({})
        before = len(mock.requests)
        r = act.click_element(name="Save")
        check("disabled: the miss is reported as before",
              not r.ok and r.error == "no locator resolved"
              and len(mock.requests) == before, r.summary())

        configure(mock.url)
        mock.answer = {"x": 400, "y": 300, "confidence": 0.8}
        with Env():
            r = act.click_element(name="Save", control="Button")
        check("enabled: the click succeeds via vision:ground",
              r.ok and r.locator_type == "vision:ground"
              and r.method == "sendinput" and r.verified is None, r.summary())
        check("the instruction names the target and its type",
              mock.requests[-1]["instruction"] == "click Save (Button)",
              mock.requests[-1]["instruction"])
        check("the element handed back is at the clicked point, in the window",
              r.element is not None and r.element.center == (500, 350)
              and r.element.hwnd == 1234 and r.element.source == "vision:ground")
        check("the attempt is on the tried list",
              r.tried and r.tried[-1][1] == "vision:ground" and r.tried[-1][2])

        configure(mock.url, dry_run=True)
        with Env():
            r = act.click_element(name="Save")
        check("dry run: not ok, and the error says where it would have clicked",
              not r.ok and "dry run" in r.error and "(500,350)" in r.error
              and not clicks, r.error)

        configure(mock.url, min_confidence=0.95)
        with Env():
            r = act.click_element(name="Save")
        check("a refusal is reported with the reason, after the miss",
              not r.ok and r.error.startswith("no locator resolved; vision "
                                              "grounding: confidence"), r.error)

        configure(mock.url)
        n = len(mock.requests)
        with Env():
            r = act.click_element(name="Save", index=2)
        check("an ordinal (index=) never falls back to vision",
              not r.ok and len(mock.requests) == n and not clicks, r.error)
        with Env():
            r = act.click_element(name="Save", near="Cancel")
        check("nor does a neighbour (near=)",
              not r.ok and len(mock.requests) == n and not clicks)

        locator.resolve = lambda locs, scope=None, order=None, allow_slow=False: \
            locator.ResolveResult(element=None, reason="only 2 named that",
                                  tried=[(0, "uia_name_type", False, 0.01)])
        with Env():
            r = act.click_element(name="Save")
        check("nor a resolution that said something else",
              not r.ok and len(mock.requests) == n and not clicks, r.error)
    finally:
        locator.resolve, locator.foreground_window, locator.invalidate_cache = saved
        grounding.configure({})


# ---------------------------------------------------------------------------
# snap, read-back, agreement, risk, verify, learning log  (sections 17+)
# ---------------------------------------------------------------------------

def C(rect, name="", ctype="ButtonControl", control="ctrl", **kw):
    """A fake UIA candidate. `control` is a sentinel: a live handle would be
    a uiautomation control; None means 'nothing to Invoke'."""
    return grounding.Cand(rect=tuple(rect), name=name, control_type=ctype,
                          control=control, **kw)


# Model point (400,300) in the 800x600 picture at (100,50) = (500,350) on screen.
SAVE = (480, 340, 540, 365)          # contains the point
OPEN = (520, 370, 560, 395)          # 28 px away
FAR = (560, 340, 600, 365)           # 60 px away
BIG = (300, 200, 900, 600)           # a large row around everything


def full(mock: Mock, **kw) -> None:
    """Everything new on, except what a check switches."""
    base = {"snap": True, "readback": True, "verify": False,
            "learning_log": False, "agreement": True}
    base.update(kw)
    configure(mock.url, **base)


def img_ocr(lines_screen):
    """An OCR stub: lines given in SCREEN pixels, answered in picture pixels
    (the fake capture's origin is (100,50), scale 1)."""
    def f(img, box):
        return [((r[0] - 100, r[1] - 50, r[2] - 100, r[3] - 50), t)
                for r, t in lines_screen]
    return f


def test_label_matching():
    print("\n17. read-back matching: normalised, fuzzy, with aliases")
    ms = grounding.match_score
    check("case, spacing and punctuation do not matter",
          ms("Open file", "  OPEN  File... ") == 1.0)
    check("'7' is the accessibility name 'Seven'", ms("7", "Seven") == 1.0)
    check("'+' is 'Plus', '÷' is 'Divide by', '=' is 'Equals', '×' is 'Multiply by'",
          ms("+", "Plus") == 1.0 and ms("÷", "Divide by") == 1.0
          and ms("=", "Equals") == 1.0 and ms("×", "Multiply by") == 1.0)
    check("a digit is never another digit",
          ms("7", "1") == 0.0 and ms("Seven", "One") < 0.8)
    check("an OCR slip still matches ('Sve' for 'Save')",
          ms("Save", "Sve") >= 0.8, f"{ms('Save', 'Sve'):.3f}")
    check("an extra word does not ('Save' vs 'Save as')",
          ms("Save", "Save as") < 0.8, f"{ms('Save', 'Save as'):.3f}")
    check("a different word does not ('Sign in' vs 'Sign out')",
          ms("Sign in", "Sign out") < 0.8, f"{ms('Sign in', 'Sign out'):.3f}")
    check("a shortcut hint is ignored ('Bold (Ctrl+B)')",
          ms("Bold", "Bold (Ctrl+B)") == 1.0)
    check("the leading words of a long name match, a short prefix does not",
          ms("Furkan Fizix", "Furkan Fizix 15:30 see you") >= 0.8
          and ms("Save", "Save changes") < 0.8)
    check("an AutomationId reads as words",
          grounding.split_identifier("num7Button") == "num 7 Button")
    check("best_label picks the best of several",
          grounding.best_label("Seven", ["", "num 7 Button", "7"])[1] == "7")
    check("an empty label never matches",
          ms("Save", "") == 0.0 and ms("", "Save") == 0.0)
    check("Turkish-first OCR's dotless i does not break English ('Vıew' = 'View')",
          ms("View", "Vıew") == 1.0 and ms("Kapalı", "KAPALI") == 1.0)
    from types import SimpleNamespace as W
    words = [W(text="File", left=10, top=0, width=30, height=14),
             W(text="Edit", left=70, top=0, width=30, height=14),
             W(text="Save", left=130, top=0, width=30, height=14),
             W(text="as", left=164, top=0, width=14, height=14)]
    segs = [" ".join(w.text for w in s) for s in grounding._segments(words)]
    check("an OCR line of toolbar labels is split at wide gaps, words kept together",
          segs == ["File", "Edit", "Save as"], str(segs))
    check("the target is taken out of an instruction",
          grounding.target_from_instruction("click Save (Button)") == "Save"
          and grounding.target_from_instruction("Click on 'New tab'") == "New tab")


def test_risky_names():
    print("\n18. risky names")
    bad = ["Delete", "Delete file", "Pay now", "Buy it now", "Sign out",
           "Log  out", "Shut down", "Send", "Submit order", "Uninstall",
           "Format", "Empty Recycle Bin", "Reset settings", "Checkout",
           "Confirm purchase", "Transfer"]
    good = ["Save", "Open", "Display", "Payload", "Formatting", "Settings",
            "Removed items", "7", "Plus", "Sender name"]
    missed = [n for n in bad if not grounding.risky(n)]
    check(f"{len(bad)} risky names are caught", not missed, str(missed))
    flagged = [n for n in good if grounding.risky(n)]
    check(f"{len(good)} ordinary names are not (whole words only)",
          not flagged, str(flagged))


def test_choose():
    print("\n19. snap + read-back, as a pure choice")
    calls = []

    def rt(lines):
        def f(box):
            calls.append(box)
            return lines
        return f

    kw = dict(radius=40, readback=False, min_ratio=0.8, allow_unreadable=False,
              high_risk=False, read_text=rt([]))
    a, big = C(SAVE, "Save"), C(BIG, "", "ListItemControl")
    ch = grounding.choose(500, 350, [big, a], "Save", **kw)
    check("inside two elements: the tightest is chosen",
          ch.cand is a and ch.distance == 0 and ch.snapped)
    ch = grounding.choose(510, 320, [a], "Save", **kw)
    check("outside but within the radius: the nearest, 20 px away",
          ch.cand is a and ch.distance == 20, str(ch.distance))
    ch = grounding.choose(500, 350, [C(FAR, "Cancel")], "x", **kw)
    check("too far (60 px > 40): no snap, the raw point stays",
          ch.cand is None and not ch.snapped and not ch.refuse)
    ch = grounding.choose(500, 350, [C(FAR, "Cancel")], "x",
                          **{**kw, "radius": 80})
    check("a larger radius reaches it", ch.cand is not None)

    kw["readback"] = True
    calls.clear()
    ch = grounding.choose(500, 350, [a], "Save", **kw)
    check("read-back: the element's UIA name matches",
          ch.cand is a and ch.readback["ok"] is True
          and ch.readback["source"] == "uia")
    check("and no OCR was needed", not calls)
    o = C(OPEN, "Open")
    ch = grounding.choose(500, 350, [a, o], "Open", **kw)
    check("the point is on 'Save', 'Open' is next to it: 'Open' wins",
          ch.cand is o and ch.readback["ok"] is True, ch.cand and ch.cand.name)
    ch = grounding.choose(500, 350, [a], "Print", **kw)
    check("nothing called 'Print' there: not found, saying what was",
          ch.cand is None and ch.refuse and "'Save'" in ch.refuse
          and ch.readback["ok"] is False, ch.refuse)
    ch = grounding.choose(500, 350, [C(SAVE, "Seven")], "7", **kw)
    check("alias: '7' read back from 'Seven'", ch.readback.get("ok") is True)
    u = C(SAVE, "")
    calls.clear()
    ch = grounding.choose(500, 350, [u], "Save",
                          **{**kw, "read_text": rt([((485, 345, 530, 360), "Save")])})
    check("an unnamed element is read by OCR of its box",
          ch.cand is u and ch.readback["ok"] is True
          and ch.readback["source"] == "ocr" and u.ocr_text == "Save")
    check("one OCR pass, covering the point and the element",
          len(calls) == 1 and calls[0][0] <= 480 and calls[0][2] >= 540)
    ch = grounding.choose(500, 350, [], "Settings",
                          **{**kw, "read_text": rt([((470, 340, 530, 360), "Settings")])})
    check("no element at all, but the text is there: snap to the text",
          ch.cand is not None and ch.cand.source == "ocr"
          and ch.cand.rect == (470, 340, 530, 360) and ch.readback["ok"] is True)
    ch = grounding.choose(500, 350, [], "Settings",
                          **{**kw, "read_text": rt([((470, 500, 530, 520), "Settings")])})
    check("the same text 150 px away is not taken", ch.refuse != "")
    ch = grounding.choose(500, 350, [], "Bluetooth", **kw)
    check("an absent target: nothing readable names it -> not found",
          ch.cand is None and "not found" in ch.refuse, ch.refuse)
    ch = grounding.choose(500, 350, [C(SAVE, "")], "Save",
                          **{**kw, "read_text": rt(None)})
    check("an unnamed element and no OCR engine: not found, and why",
          ch.refuse and "no OCR" in ch.refuse, ch.refuse)
    ch = grounding.choose(500, 350, [C(SAVE, "")], "Save",
                          **{**kw, "allow_unreadable": True})
    check("readback_allow_unreadable: an unreadable icon is accepted",
          ch.cand is not None and not ch.refuse and ch.readback["ok"] is None)
    ch = grounding.choose(500, 350, [C(SAVE, "")], "Save",
                          **{**kw, "allow_unreadable": True, "high_risk": True})
    check("... but not when the answer is high-risk",
          ch.refuse and "disagreed" in ch.refuse, ch.refuse)
    ch = grounding.choose(500, 350, [C(SAVE, "Brushes")], "Save",
                          **{**kw, "allow_unreadable": True})
    check("... and never when the element reads as something else",
          ch.refuse and "Brushes" in ch.refuse)


def test_snap_click(mock: Mock):
    print("\n20. end to end: snapped, read back, clicked at the element")
    full(mock)
    mock.answer = {"x": 400, "y": 300}
    with Env(cands=[C(BIG, "", "ListItemControl"), C(SAVE, "Save")]):
        d = grounding.click_by_vision("click Save", 1234, target="Save")
    check("clicked", d.outcome == "clicked" and d.clicked, d.summary())
    check("at the element's centre, not the model's point",
          d.click_point == (510, 352) and d.point == (500, 350), str(d.click_point))
    el = clicks[0] if clicks else None
    check("the element (its rect and UIA handle) went to the actuator",
          el is not None and el.rect == SAVE and el.control == "ctrl"
          and el.source == "vision:ground")
    check("with prefer='invoke', as for any named click", prefers == ["invoke"])
    check("the decision names what was snapped, in screen and picture pixels",
          d.snapped and d.snapped["name"] == "Save"
          and d.snapped["box"] == list(SAVE)
          and d.snapped["box_image"] == [380, 290, 440, 315], str(d.snapped))
    check("read-back recorded", d.readback and d.readback["ok"] is True)

    with Env(cands=[C(SAVE, "Save")]):
        d = grounding.click_by_vision("click Save", 1234, target="Save",
                                      prefer="inject")
    check("the caller's prefer is passed through", prefers == ["inject"])
    with Env(cands=[C(SAVE, "Save", control=None)]):
        d = grounding.click_by_vision("click Save", 1234, target="Save")
    check("no UIA handle (a detector or OCR box): always an injected click",
          prefers == ["inject"] and d.clicked)

    with Env(cands=[C(SAVE, "Save"), C(OPEN, "Open")]):
        d = grounding.click_by_vision("click Open", 1234)
    check("the model hit the neighbour; the requested control is clicked",
          d.clicked and d.click_point == (540, 382) and clicks[0].rect == OPEN,
          d.summary())
    check("the target is derived from the instruction when not given",
          d.target == "Open")

    with Env(cands=[C(SAVE, "Save")]):
        d = grounding.click_by_vision("click Print", 1234)
    check("read-back mismatch: not_found, no click",
          d.outcome == "not_found" and not clicks and not d.clicked
          and "'Save'" in d.reason, d.summary())
    with Env(cands=[], ocr=img_ocr([])):
        d = grounding.click_by_vision("click Bluetooth settings", 1234)
    check("a target not on screen gets a point from the model — and is refused",
          d.outcome == "not_found" and not clicks and d.image_point == (400, 300),
          d.summary())

    full(mock, readback=False)
    with Env(cands=[C(FAR, "Cancel")]):
        d = grounding.click_by_vision("click Save", 1234)
    check("read-back off, nothing within 40 px: the raw point is clicked",
          d.clicked and d.click_point == (500, 350) and d.snapped is None
          and prefers == ["inject"], d.summary())
    with Env(cands=[C(FAR, "Cancel")], dpi=2.0):
        d = grounding.click_by_vision("click Save", 1234)
    check("at 200% the radius doubles (80 px): it snaps",
          d.clicked and d.click_point == (580, 352), d.summary())
    with Env(cands=[C(SAVE, "Delete")]):
        d = grounding.click_by_vision("click OK", 1234)
    check("read-back off and the element there is 'Delete': refused",
          d.outcome == "refused" and "risky" in d.reason and not clicks,
          d.summary())

    full(mock, snap=False)
    with Env(cands=[C(SAVE, "Save")]):
        d = grounding.click_by_vision("click Save", 1234)
    check("snap off, read-back on: checked on the element under the point, "
          "clicked at the raw point",
          d.clicked and d.click_point == (500, 350) and d.readback["ok"] is True,
          d.summary())

    full(mock, dry_run=True)
    with Env(cands=[C(SAVE, "Save")]):
        d = grounding.click_by_vision("click Save", 1234)
    check("dry run says where and on what it would click",
          d.outcome == "dry_run" and "(510,352)" in d.reason
          and "'Save'" in d.reason and not clicks, d.reason)


def test_risk_guard(mock: Mock):
    print("\n21. risky targets are refused before anything is asked")
    full(mock)
    n = len(mock.requests)
    with Env(cands=[C(SAVE, "Delete")]):
        d = grounding.click_by_vision("click Delete", 1234, target="Delete")
    check("'Delete': refused, no capture, no request, no click",
          d.outcome == "refused" and "allow_risky" in d.reason
          and not captures and len(mock.requests) == n and not clicks,
          d.summary())
    with Env(cands=[C(SAVE, "Sign out")]):
        d = grounding.click_by_vision("click Sign out", 1234)
    check("'Sign out' too", d.outcome == "refused")
    full(mock, allow_risky=True)
    mock.answer = {"x": 400, "y": 300}
    with Env(cands=[C(SAVE, "Delete")]):
        d = grounding.click_by_vision("click Delete", 1234)
    check("allow_risky: the same click goes through (read-back still applies)",
          d.outcome == "clicked" and len(clicks) == 1, d.summary())


def test_agreement(mock: Mock):
    print("\n22. agreement: the model's first look against its answer")
    full(mock)
    mock.answer = {"x": 400, "y": 300}
    with Env(cands=[C(SAVE, "Save")]):
        d = grounding.click_by_vision("click Save", 1234)
    check("the request asks for it", mock.requests[-1].get("agreement") is True)
    check("a service without the fields is fine: no risk recorded",
          d.clicked and d.agreement_distance is None and not d.high_risk)
    full(mock, agreement=False)
    with Env(cands=[C(SAVE, "Save")]):
        grounding.click_by_vision("click Save", 1234)
    check("agreement off: not asked", "agreement" not in mock.requests[-1])

    mock.answer = {"x": 400, "y": 300, "agreement_distance": 0.12,
                   "pass1_point": [10, 10]}
    full(mock, readback=False)
    with Env(cands=[C(SAVE, "Save")]):
        d = grounding.click_by_vision("click Save", 1234)
    check("disagreement 0.12 > 0.05 with read-back off: refused",
          d.outcome == "refused" and d.high_risk and "disagree" in d.reason
          and not clicks, d.summary())
    check("the fields are kept", d.agreement_distance == 0.12
          and d.pass1_point == (10, 10))
    full(mock)
    with Env(cands=[C(SAVE, "Save")]):
        d = grounding.click_by_vision("click Save", 1234)
    check("high-risk but read-back passes: clicked",
          d.clicked and d.high_risk, d.summary())
    full(mock, readback_allow_unreadable=True)
    with Env(cands=[C(SAVE, "")], ocr=img_ocr([])):
        d = grounding.click_by_vision("click Save", 1234)
    check("high-risk and unreadable: not found even when unreadable is allowed",
          d.outcome == "not_found" and not clicks, d.summary())
    mock.answer = {"x": 400, "y": 300, "agreement_distance": 0.01,
                   "pass1_point": [401, 300]}
    with Env(cands=[C(SAVE, "")], ocr=img_ocr([])):
        d = grounding.click_by_vision("click Save", 1234)
    check("agreeing (0.01) and unreadable, allowed: clicked",
          d.clicked and not d.high_risk, d.summary())
    mock.answer = {"x": 400, "y": 300, "agreement_distance": "junk",
                   "pass1_point": "junk"}
    full(mock)
    with Env(cands=[C(SAVE, "Save")]):
        d = grounding.click_by_vision("click Save", 1234)
    check("malformed fields are ignored, not trusted",
          d.clicked and d.agreement_distance is None and d.pass1_point is None)
    mock.answer = {"x": 400, "y": 300}


def test_verify(mock: Mock):
    print("\n23. after the click: did anything observable change?")
    from PIL import ImageDraw
    ch = grounding._changed
    check("foreground change", ch({"fg": 1}, {"fg": 2}) != "")
    check("focus change", ch({"fg": 1, "focus": ((0, 0, 1, 1), "a")},
                             {"fg": 1, "focus": ((5, 5, 9, 9), "b")}) != "")
    check("pattern state change (a toggle)",
          ch({"state": (0, None, None, None)}, {"state": (1, None, None, None)}) != "")
    check("nothing at all", ch({"fg": 1, "state": None}, {"fg": 1, "state": None}) == "")
    base = Image.new("RGB", (800, 600), (255, 255, 255))
    caret = base.copy()
    ImageDraw.Draw(caret).line([(300, 200), (300, 216)], fill=(0, 0, 0))
    menu = base.copy()
    ImageDraw.Draw(menu).rectangle([400, 380, 600, 530], fill=(60, 60, 60))
    hover = base.copy()
    ImageDraw.Draw(hover).rectangle([380, 290, 440, 315], fill=(0, 0, 255))
    t0 = grounding.thumb(base, (374, 284, 446, 321))
    check("a blinking caret is not a change",
          ch({"thumb": t0}, {"thumb": grounding.thumb(caret, (374, 284, 446, 321))}) == "")
    check("a menu opening below the element is",
          "window changed" in ch({"thumb": t0},
                                 {"thumb": grounding.thumb(menu, (374, 284, 446, 321))}))
    check("the element's own hover highlight is masked out",
          ch({"thumb": t0}, {"thumb": grounding.thumb(hover, (374, 284, 446, 321))}) == "")

    full(mock, verify=True, verify_timeout_s=0.3)
    mock.answer = {"x": 400, "y": 300}
    with Env(cands=[C(SAVE, "Save")], observe=[{"fg": 1}, {"fg": 1}, {"fg": 2}]):
        d = grounding.click_by_vision("click Save", 1234)
    check("a change after the click: verified True, with the reason",
          d.clicked and d.verified is True and "foreground" in d.verify_reason
          and "verified" in d.reason, d.summary())
    check("observed around the clicked element", observed and observed[0] == SAVE)
    t = time.time()
    with Env(cands=[C(SAVE, "Save")], observe=[{"fg": 1, "thumb": t0}]):
        d = grounding.click_by_vision("click Save", 1234)
    took = time.time() - t
    check("no change: verified False, still one click — never retried",
          d.clicked and d.verified is False and len(clicks) == 1
          and "UNVERIFIED" in d.reason, d.summary())
    check("it waited for the timeout, not longer", 0.25 <= took < 2.5, f"{took:.2f}s")
    with Env(cands=[C(SAVE, "Save")], observe=[RuntimeError("locked")]):
        d = grounding.click_by_vision("click Save", 1234)
    check("no baseline (screen unreadable): clicked, verified None",
          d.clicked and d.verified is None and "no baseline" in d.verify_reason,
          d.summary())

    from core import act, locator
    saved = (locator.resolve, locator.foreground_window, locator.invalidate_cache)
    locator.resolve = lambda locs, scope=None, order=None, allow_slow=False: \
        locator.ResolveResult(element=None, tried=[(0, "uia_name_type", False, 0.01)])
    locator.foreground_window = lambda: 1234
    locator.invalidate_cache = lambda hwnd=None: None
    try:
        with Env(cands=[C(SAVE, "Save")], observe=[{"fg": 1}, {"fg": 2}]):
            r = act.click_element(name="Save")
        check("act.click_element reports verified=True and the element's rect",
              r.ok and r.verified is True and r.element.rect == SAVE
              and r.locator_type == "vision:ground", r.summary())
        with Env(cands=[C(SAVE, "Save")], observe=[{"fg": 1}]):
            r = act.click_element(name="Save")
        check("... and verified=False when nothing changed",
              r.ok and r.verified is False and "UNVERIFIED" in r.summary(),
              r.summary())
        with Env(cands=[C(SAVE, "Save")]):
            r = act.click_element(name="Print")
        check("a read-back refusal reaches the model as a clean miss",
              not r.ok and "not found" in r.error and not clicks, r.error)
        with Env():
            r = act.click_element(name="Delete")
        check("a risky name reaches the model as a refusal",
              not r.ok and "risky" in r.error, r.error)
    finally:
        locator.resolve, locator.foreground_window, locator.invalidate_cache = saved


def test_detector(mock: Mock):
    print("\n24. the detector, when UIA has nothing near the point")
    full(mock)
    mock.answer = {"x": 400, "y": 300}
    det = [C(SAVE, "Save", "text", control=None, source="detector:text")]
    with Env(cands=[], detector=det, ocr=img_ocr([])):
        d = grounding.click_by_vision("click Save", 1234)
    check("off by default: not run", not detector_calls and d.outcome == "not_found")
    full(mock, detector_fallback=True)
    with Env(cands=[], detector=det):
        d = grounding.click_by_vision("click Save", 1234)
    check("on, with no UIA element near: run, and its box is clicked (injected)",
          len(detector_calls) == 1 and d.clicked and d.click_point == (510, 352)
          and prefers == ["inject"] and d.snapped["source"] == "detector:text",
          d.summary())
    with Env(cands=[C(SAVE, "Save")], detector=det):
        d = grounding.click_by_vision("click Save", 1234)
    check("not run when UIA has an element there", not detector_calls and d.clicked)


def test_learning_log(mock: Mock):
    print("\n25. the learning log, and the export of verified rows")
    import importlib.util
    root = grounding.learning_dir()
    import shutil
    shutil.rmtree(root, ignore_errors=True)
    full(mock, verify=True, verify_timeout_s=0.2, learning_log=True)
    mock.answer = {"x": 400, "y": 300, "agreement_distance": 0.02,
                   "pass1_point": [398, 301]}
    with Env(cands=[C(SAVE, "Save")], observe=[{"fg": 1}, {"fg": 2}]) as env:
        grounding.click_by_vision("click Save", 1234)
        sent = env.cap.png
    with Env(cands=[C(SAVE, "Save")]):
        grounding.click_by_vision("click Print", 1234)
    with Env():
        grounding.click_by_vision("click Delete", 1234)
    with Env(cands=[C(SAVE, "Save")], observe=[{"fg": 1}]):
        grounding.click_by_vision("click Save", 1234)          # unverified
    log = root / "decisions.jsonl"
    lines = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()] \
        if log.exists() else []
    check("one line per decision under <data>/grounding", len(lines) == 4
          and str(log).startswith(_TMP), f"{len(lines)} at {log}")
    r0 = lines[0] if lines else {}
    need = ("ts", "instruction", "window_title", "image", "model_point", "snapped",
            "readback", "agreement", "outcome", "verified", "latency_ms")
    check("with every field", all(k in r0 for k in need),
          str(sorted(set(need) - set(r0))))
    check("the values", r0.get("instruction") == "click Save"
          and r0.get("window_title") == "Notepad" and r0.get("model_point") == [400, 300]
          and r0.get("snapped", {}).get("name") == "Save"
          and r0.get("snapped", {}).get("type") == "ButtonControl"
          and r0.get("snapped", {}).get("box_image") == [380, 290, 440, 315]
          and r0.get("readback", {}).get("ok") is True
          and r0.get("agreement", {}).get("distance") == 0.02
          and r0.get("outcome") == "clicked" and r0.get("verified") is True
          and r0.get("latency_ms") == 42, json.dumps(r0)[:300])
    png = root / r0.get("image", "missing") if r0.get("image") else None
    check("the picture saved is exactly the one sent",
          png is not None and png.exists() and png.read_bytes() == sent)
    check("a refusal before capture has no picture",
          lines[2]["outcome"] == "refused" and lines[2]["image"] is None)
    check("a not-found is logged with its read-back",
          lines[1]["outcome"] == "not_found"
          and lines[1]["readback"]["ok"] is False)

    spec = importlib.util.spec_from_file_location(
        "export_grounding", Path(__file__).resolve().parents[1] / "tools" /
        "export_grounding.py")
    ex = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ex)
    out = Path(_TMP) / "rows.jsonl"
    n, k, rows = ex.export(log, out)
    check("export: 4 decisions, 1 verified-correct row", n == 4 and k == 1,
          f"{n} {k}")
    row = rows[0] if rows else {}
    check("the row: image, instruction, normalised point and box, source",
          row.get("source") == "jev-verified" and row.get("instruction") == "click Save"
          and Path(row.get("image", "")).exists()
          and row.get("box") == [0.475, 0.48333, 0.55, 0.525]
          and row.get("point") == [0.5125, 0.50417], json.dumps(row)[:300])
    check("written as JSON lines",
          out.exists() and len(out.read_text(encoding="utf-8").splitlines()) == 1)

    full(mock, learning_log=True, learning_log_max_shots=0)
    grounding._shots.update(n=None, dir=None)
    with Env(cands=[C(SAVE, "Save")]):
        grounding.click_by_vision("click Save", 1234)
    last = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    check("past learning_log_max_shots the line is kept, the picture is not",
          last["image"] is None and last["outcome"] == "clicked")
    full(mock, learning_log=False)
    before = len(log.read_text(encoding="utf-8").splitlines())
    with Env(cands=[C(SAVE, "Save")]):
        grounding.click_by_vision("click Save", 1234)
    check("learning_log off: nothing written",
          len(log.read_text(encoding="utf-8").splitlines()) == before)
    mock.answer = {"x": 400, "y": 300}


def test_new_settings():
    print("\n26. the new settings: defaults and cleaning")
    D = grounding.DEFAULTS
    check("defaults", D["snap"] is True and D["snap_radius_px"] == 40
          and D["readback"] is True and D["readback_min_ratio"] == 0.8
          and D["readback_allow_unreadable"] is False and D["agreement"] is True
          and D["max_disagreement"] == 0.05 and D["allow_risky"] is False
          and D["verify"] is True and D["verify_timeout_s"] == 1.5
          and D["detector_fallback"] is False and D["learning_log"] is True,
          str(D))
    s = grounding.clean_settings({"snap": "no", "allow_risky": "yes",
                                  "readback_min_ratio": 3, "max_disagreement": "0.1",
                                  "snap_radius_px": "-5", "learning_log_max_shots": "12.7",
                                  "verify_timeout_s": "x"})
    check("strings, clamps and junk",
          s["snap"] is False and s["allow_risky"] is True
          and s["readback_min_ratio"] == 1.0 and s["max_disagreement"] == 0.1
          and s["snap_radius_px"] == 0.0 and s["learning_log_max_shots"] == 12
          and s["verify_timeout_s"] == 1.5, str(s))
    check("a ratio below 0.5 is raised to 0.5 (never 'anything matches')",
          grounding.clean_settings({"readback_min_ratio": 0.1})["readback_min_ratio"] == 0.5)
    old = {"enabled": True, "url": "http://127.0.0.1:8765", "timeout_s": 60,
           "min_confidence": None, "require_inside_window": True, "dry_run": True}
    s = grounding.clean_settings(old)
    check("a block saved before these keys existed gets their defaults",
          s["dry_run"] is True and s["readback"] is True and s["snap"] is True
          and set(s) == set(D))


def test_none_reach_second_look(mock: Mock):
    print("\n27. 'none' answers, wider OCR, the second look")
    ok_none = {"ok": False, "error": "no_point", "raw": "none"}
    check("says_absent: 'none' / ' None. ' yes; a truncated '1', an ok answer, another error no",
          grounding.says_absent(ok_none)
          and grounding.says_absent({**ok_none, "raw": " None. "})
          and not grounding.says_absent({**ok_none, "raw": "1"})
          and not grounding.says_absent({"ok": True, "raw": "none"})
          and not grounding.says_absent({**ok_none, "error": "model_loading"}))
    check("look_box: centred, fraction of each side, shifted inside",
          grounding.look_box(400, 300, (800, 600), 0.5) == (200, 150, 600, 450)
          and grounding.look_box(10, 590, (800, 600), 0.5) == (0, 300, 400, 600)
          and grounding.look_box(5, 5, (40, 30), 0.15) == (0, 0, 28, 28))

    mock.answer = {"x": 400, "y": 300}
    saved = mock.ground
    seq: list = []

    def scripted(payload):
        out = saved(payload)            # the size fields of the picture received
        out.update(seq.pop(0) if seq else {})
        return out

    mock.ground = scripted
    try:
        # the model says the target is not there
        full(mock)
        seq[:] = [{"ok": False, "error": "no_point", "raw": "none", "x": None, "y": None}]
        n0 = len(mock.requests)
        with Env(cands=[C(SAVE, "Save")]):
            d = grounding.click_by_vision("click Bluetooth", 1234)
        check("'none' from the model: not_found, says so, nothing snapped or clicked",
              d.outcome == "not_found" and "not in" in d.reason and not clicks
              and d.snapped is None and len(mock.requests) - n0 == 1, d.summary())

        # wider OCR: the first box read grows by the reach on each side
        full(mock, second_look=False, readback_ocr_reach_px=0)
        with Env(cands=[], ocr=img_ocr([])):
            grounding.click_by_vision("click Pencil", 1234)
        narrow = ocr_boxes[0][2] - ocr_boxes[0][0] if ocr_boxes else None
        full(mock, second_look=False, readback_ocr_reach_px=100)
        with Env(cands=[], ocr=img_ocr([])):
            grounding.click_by_vision("click Pencil", 1234)
        wide = ocr_boxes[0][2] - ocr_boxes[0][0] if ocr_boxes else None
        check("readback_ocr_reach_px=100 reads 100 px further each side",
              narrow == 80 and wide == 280, f"{narrow} -> {wide}")
        with Env(cands=[], ocr=img_ocr([((420, 340, 700, 362), "Open the recent files list")])):
            d = grounding.click_by_vision("click Open the recent files list", 1234)
        check("a label longer than the radius, read by OCR, is clicked",
              d.clicked and d.readback["ok"] is True and d.readback["source"] == "ocr", d.summary())

        # the second look. First answer (400,300) in the 800x600 picture snaps
        # to SAVE, whose centre (510,352) on screen is (410,302) in the
        # picture; the 0.55 crop around it is (190,137)-(630,467), magnified
        # 2.5x -> 1100x825, so the same spot is (550,412) in the crop.
        import base64
        import io
        unnamed = [C(SAVE, "")]
        full(mock, second_look=True)
        seq[:] = [{"x": 400, "y": 300}, {"x": 550, "y": 412}]
        n0 = len(mock.requests)
        with Env(cands=unnamed, ocr=img_ocr([])):
            d = grounding.click_by_vision("click Save", 1234)
        sent = mock.requests[-1] if mock.requests else {}
        size2 = (Image.open(io.BytesIO(base64.b64decode(sent["image_b64"]))).size
                 if sent.get("image_b64") else None)
        check("unnamed element: a second look on a magnified crop agrees -> clicked",
              d.clicked and len(mock.requests) - n0 == 2 and size2 == (1100, 825)
              and d.second_look and d.second_look["agrees"] is True
              and d.second_look["point"] == [510, 352] and clicks and clicks[0].rect == SAVE,
              f"{d.summary()} {size2} {d.second_look}")
        check("read-back recorded as confirmed by the second look",
              d.readback["ok"] is True and d.readback["source"] == "second_look")

        seq[:] = [{"x": 400, "y": 300}, {"x": 50, "y": 50}]
        with Env(cands=unnamed, ocr=img_ocr([])):
            d = grounding.click_by_vision("click Save", 1234)
        check("the second look points elsewhere -> not_found, no click",
              d.outcome == "not_found" and "elsewhere" in d.reason and not clicks
              and d.second_look["agrees"] is False, d.summary())

        seq[:] = [{"x": 400, "y": 300}, {"ok": False, "error": "no_point", "raw": "none"}]
        with Env(cands=unnamed, ocr=img_ocr([])):
            d = grounding.click_by_vision("click Save", 1234)
        check("the second look says 'none' -> not_found, no click",
              d.outcome == "not_found" and "not there" in d.reason and not clicks, d.summary())

        seq[:] = [{"x": 400, "y": 300}, {"x": 560, "y": 420}]
        with Env(cands=[], ocr=img_ocr([])):
            d = grounding.click_by_vision("click Save", 1234)
        check("no element (a canvas): agreeing within the radius clicks the model's point",
              d.clicked and d.click_point == (500, 350) and d.second_look["agrees"], d.summary())

        seq[:] = [{"x": 400, "y": 300}, {"x": 550, "y": 412}]
        with Env(cands=[C(SAVE, "Brushes")], ocr=img_ocr([])):
            d = grounding.click_by_vision("click Save", 1234)
        check("an element that READS as something else is never second-looked",
              d.outcome == "not_found" and d.second_look is None and not clicks, d.summary())

        seq[:] = [{"x": 400, "y": 300, "agreement_distance": 0.2}, {"x": 550, "y": 412}]
        n0 = len(mock.requests)
        with Env(cands=unnamed, ocr=img_ocr([])):
            d = grounding.click_by_vision("click Save", 1234)
        check("a high-risk answer is refused without a second look",
              d.outcome == "not_found" and len(mock.requests) - n0 == 1 and not clicks, d.summary())

        full(mock)
        seq[:] = [{"x": 400, "y": 300}]
        n0 = len(mock.requests)
        with Env(cands=unnamed, ocr=img_ocr([])):
            d = grounding.click_by_vision("click Save", 1234)
        check("second_look off (the default): unreadable stays not_found, one request",
              d.outcome == "not_found" and len(mock.requests) - n0 == 1, d.summary())

        full(mock, readback_allow_unreadable=True, second_look=True)
        seq[:] = [{"x": 400, "y": 300}]
        n0 = len(mock.requests)
        with Env(cands=unnamed, ocr=img_ocr([])):
            d = grounding.click_by_vision("click Save", 1234)
        check("readback_allow_unreadable wins: clicked without asking again",
              d.clicked and len(mock.requests) - n0 == 1 and d.second_look is None, d.summary())
    finally:
        mock.ground = saved

    D = grounding.DEFAULTS
    check("defaults: second look OFF (measured ~85% precision), OCR reach 200, crop 0.55",
          D["second_look"] is False and D["readback_ocr_reach_px"] == 200
          and D["second_look_fraction"] == 0.55)
    s = grounding.clean_settings({"second_look": "no", "second_look_fraction": 5,
                                  "readback_ocr_reach_px": -3})
    check("cleaned and clamped", s["second_look"] is False and s["second_look_fraction"] == 0.9
          and s["readback_ocr_reach_px"] == 0.0, str(s))

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import export_grounding as ex
    rec = {"outcome": "clicked", "verified": True, "image": "captures/x.png",
           "readback": {"ok": True, "source": "uia"}, "snapped": {"box_image": [1, 2, 3, 4]}}
    check("export: a label-confirmed click is a training row; a second-look one is not",
          ex.is_verified_correct(rec)
          and not ex.is_verified_correct({**rec, "readback": {"ok": True, "source": "second_look"}}))


def main() -> int:
    print("=" * 72)
    print("VISION GROUNDING — the fallback for a target nothing can name")
    print("=" * 72)
    mock = Mock().start()
    try:
        test_off_by_default(mock)
        test_settings_cleaning()
        test_mapping()
        test_accepted_click(mock)
        test_scaled_capture(mock)
        test_size_mismatch(mock)
        test_outside_window(mock)
        test_no_window(mock)
        test_confidence(mock)
        test_service_down(mock)
        test_dry_run(mock)
        test_window_moved(mock)
        test_actuator_refusal(mock)
        test_history_provider(mock)
        test_tasklog(mock)
        test_act_integration(mock)
        test_label_matching()
        test_risky_names()
        test_choose()
        test_snap_click(mock)
        test_risk_guard(mock)
        test_agreement(mock)
        test_verify(mock)
        test_detector(mock)
        test_learning_log(mock)
        test_new_settings()
        test_none_reach_second_look(mock)
    finally:
        mock.stop()
        grounding.configure({})

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
