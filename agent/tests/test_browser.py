"""Browser control — read structure, never pixels.

Uses a local page served from a temp file so the test does not depend on the
internet or on any site's markup staying still.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.browser import Browser  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""),
          flush=True)
    return ok


PAGE = """<!doctype html><html><head><title>Bench Page</title></head><body>
<h1>Order form</h1>
<label for="who">Full name</label><input id="who" placeholder="Full name">
<label for="mail">Email</label><input id="mail" placeholder="Email">
<select id="plan"><option>Basic</option><option>Pro</option></select>
<button data-testid="submit-btn">Place order</button>
<button id="cancel">Cancel</button>
<a href="#done" id="link">Terms and conditions</a>
<div id="out"></div>
<script>
document.querySelector('[data-testid=submit-btn]').onclick = () => {
  document.getElementById('out').textContent =
    'ORDERED:' + document.getElementById('who').value;
};
</script></body></html>"""


SECOND = """<!doctype html><html><head><title>Second Page</title></head>
<body><h1>Second</h1></body></html>"""

# A download link, a popup link and a file input — the three page shapes the
# simple click/fill path cannot handle.
FILES_PAGE = """<!doctype html><html><head><title>Files</title></head><body>
<h1>Files</h1>
<a id="dl" download="report.txt"
   href="data:text/plain;charset=utf-8,quarterly%20numbers%20go%20here">Download the report</a>
<button id="pop" onclick="window.open('SECOND_URL','_blank')">Open in a new tab</button>
<input type="file" id="up">
<div id="picked"></div>
<script>
document.getElementById('up').onchange = e => {
  document.getElementById('picked').textContent =
    'picked: ' + [...e.target.files].map(f => f.name).join(', ');
};
</script></body></html>"""


def main() -> int:
    print("=" * 72)
    print("BROWSER CONTROL")
    print("=" * 72)

    tmpdir = Path(tempfile.gettempdir())
    tmp = tmpdir / "agent_bench_page.html"
    tmp.write_text(PAGE, encoding="utf-8")
    url = tmp.as_uri()

    tmp2 = tmpdir / "agent_second_page.html"
    tmp2.write_text(SECOND, encoding="utf-8")
    tmp3 = tmpdir / "agent_files_page.html"
    tmp3.write_text(FILES_PAGE.replace("SECOND_URL", tmp2.as_uri()),
                    encoding="utf-8")

    b = Browser(headless=True,
                profile_dir=Path(tempfile.gettempdir()) / "agent_test_profile")
    try:
        print("\n1. launch and navigate")
        t0 = time.time()
        r = b.goto(url)
        check("opened a page headless", r["title"] == "Bench Page",
              f"{r['title']!r} in {(time.time()-t0)*1000:.0f}ms")
        check("stayed headless", b.headless, "no window stolen from the user")

        print("\n2. read the page structure (no screenshot)")
        t0 = time.time()
        els = b.elements()
        dt = (time.time() - t0) * 1000
        check("enumerates interactive elements", len(els) >= 5,
              f"{len(els)} found in {dt:.0f}ms")
        names = [e.name for e in els]
        check("reads accessible names", "Place order" in " ".join(names),
              ", ".join(n for n in names if n)[:70])
        tid = [e for e in els if e.testid]
        check("finds test ids (most stable selector)", bool(tid),
              tid[0].testid if tid else "")

        print("\n3. act by identity")
        check("fill by label", b.fill("Full name", "Ada Lovelace").get("ok"))
        r = b.click(testid="submit-btn")
        check("click by test id", r.get("ok"), f"via {r.get('via')}")
        time.sleep(0.3)
        check("page state changed as expected", "ORDERED:Ada Lovelace" in b.text(),
              b.text()[-40:].replace("\n", " "))

        print("\n4. selectors are position-independent")
        locs = [l for e in els if e.name == "Place order" for l in e.locators()]
        kinds = [l["type"] for l in locs]
        check("locator stack built", bool(locs), " > ".join(kinds))
        check("no coordinates anywhere in the stack",
              not any("coord" in k for k in kinds),
              "survives resize, zoom and other machines by construction")

        print("\n5. failure is actionable")
        r = b.click(name="No Such Button")
        check("missing element explains itself", not r.get("ok") and "hint" in r,
              r.get("hint", "")[:56])

        print("\n6. tabs")
        b.new_tab(tmp2.as_uri())
        tabs = b.tabs()
        check("a second tab opens", len(tabs) == 2, str(len(tabs)))
        check("the new tab is the one being driven",
              [t for t in tabs if t["active"]][0]["index"] == 1,
              "acting on the old tab after opening a new one is a classic bug")
        r = b.switch_tab(0)
        check("switch back by index", r.get("ok") and "Bench Page" in r["title"],
              r.get("title", ""))
        check("switching to a tab that isn't there fails clearly",
              not b.switch_tab(9).get("ok"))
        b.close_tab(1)
        check("close leaves the rest alone", len(b.tabs()) == 1)
        last = b.close_tab()
        check("closing the last tab keeps the browser alive",
              b.running and len(b.tabs()) == 1, last.get("note", ""))

        print("\n7. a click that opens a tab follows it")
        b.goto(tmp3.as_uri())
        b.click(name="Open in a new tab")
        active = [t for t in b.tabs() if t["active"]]
        check("popup became the active tab",
              bool(active) and "Second Page" in active[0]["title"],
              active[0]["title"] if active else "(none active)")
        check("and it is settled, not still blank",
              "Second" in b.text(),
              "acting on a popup before it navigates hits about:blank")

        # Chromium opens and instantly closes a page for a blocked target=_blank.
        # Following it would leave every later action failing on a dead page.
        b._page.close()
        check("recovers when the page under it is closed",
              bool(b.text()) or b.tabs(), "must not strand the agent on a dead page")
        check("and lands on a live tab",
              b._page is not None and not b._page.is_closed())
        b.goto(tmp3.as_uri())

        print("\n8. downloads are kept, not silently lost")
        b.goto(tmp3.as_uri())
        r = b.download(name="Download the report")
        check("download saved", r.get("ok"), r.get("error", "")[:60])
        if r.get("ok"):
            check("file is on disk with content",
                  Path(r["path"]).exists() and r["bytes"] > 0,
                  f"{r['name']} · {r['bytes']} bytes")
            check("content is what the page offered",
                  "quarterly" in Path(r["path"]).read_text(encoding="utf-8"))
            check("it is listed afterwards", any(d["name"] == r["name"]
                                                 for d in b.downloads()))
        r2 = b.download(name="No Such Link", timeout_s=3)
        check("a download that never starts says so",
              not r2.get("ok") and "hint" in r2, r2.get("hint", "")[:50])

        print("\n9. uploads")
        payload = Path(tempfile.gettempdir()) / "agent_upload.txt"
        payload.write_text("upload me", encoding="utf-8")
        r = b.upload(str(payload))
        check("attached to a real file input", r.get("ok"), str(r)[:60])
        if r.get("ok"):
            check("the page sees the file",
                  "agent_upload.txt" in b.text(), b.text()[-60:].replace("\n", " "))
        check("a missing file is refused before touching the page",
              not b.upload(r"C:\nope\missing.txt").get("ok"))

        print("\n10. it remembers how a site behaves")
        b.learn("stubborn.example", needs_headed=True)
        check("learned site is remembered in memory",
              "stubborn.example" in b.headed_sites)
        b2 = Browser(headless=True, profile_dir=b.profile_dir)
        check("and survives a restart",
              "stubborn.example" in b2.headed_sites,
              "otherwise every session repeats the same failed headless attempt")
        check("what it learned is inspectable",
              b2.knows("stubborn.example").get("needs_headed") is True)
        b.learn("stubborn.example", needs_headed=False)
        check("a site can go back to headless",
              Browser(headless=True, profile_dir=b.profile_dir)
              .knows("stubborn.example")["needs_headed"] is False,
              "sites fix their bot walls; the memory must not be one-way")

        print("\n11. session reporting is honest")
        s = b.signed_in()
        check("reports hosts, not a verdict", "hosts" in s and "note" in s,
              s.get("note", "")[:60])
        check("an unknown host has no session",
              b.signed_in("nowhere.invalid")["has_session"] is False)

        print("\n12. a visible browser gets the whole screen")
        # A small window shows less of the page, and what is off screen cannot
        # be acted on. Checked from the launch options rather than by opening
        # a headed browser, which would take the screen from whoever is using it.
        headed = b.launch_options(headless=False)
        check("a headed launch asks Chromium for a maximised window",
              "--start-maximized" in headed["args"] and headed["headless"] is False)
        check("and lets the page fill it — a fixed viewport would pin the page "
              "to 1440x900 inside the big window",
              headed.get("no_viewport") is True and "viewport" not in headed)
        headless = b.launch_options(headless=True)
        check("headless keeps its fixed viewport, having no window to fill",
              headless.get("viewport") == Browser.HEADLESS_VIEWPORT
              and "--start-maximized" not in headless["args"]
              and not headless.get("no_viewport"))

        # And start() really launches with them: restarted with a different
        # viewport handed in through launch_options, the page must come up at
        # that size. The same browser is restarted — which is what the headed
        # fallback does in production — because two Playwright instances
        # cannot share one thread. Still headless: no window appears.
        b.stop()
        b.launch_options = lambda headless=None: {
            **Browser.launch_options(b, headless),
            "viewport": {"width": 777, "height": 555}}
        try:
            b.start()
            check("start() launches with exactly these options",
                  b._page.viewport_size == {"width": 777, "height": 555},
                  str(b._page.viewport_size))
        finally:
            del b.launch_options

    finally:
        b.stop()

    passed = sum(1 for _, ok, _ in results if ok)
    print("\n" + "=" * 72)
    print(f"{passed}/{len(results)} checks passed")
    fails = [(n, d) for n, ok, d in results if not ok]
    for n, d in fails:
        print(f"  - {n}  {d}")
    print("=" * 72)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
