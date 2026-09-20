"""The browser-use style path: index the page, batch the actions, remember.

Three claims are being tested, and each has a specific way of being wrong:

  indexing    an index must resolve to exactly one node, and must go stale
              loudly rather than silently pointing at whatever moved into
              its place;
  batching    a batch must stop at the first failure and hand back enough to
              recover in one turn, not plough on doing damage;
  memory      a remembered selector must be dropped when it stops working,
              or the fast path becomes a confidently wrong path.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cleanup import scratch_dir  # noqa: E402

from core.browser import Browser  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""),
          flush=True)
    return ok


PAGE = """<!doctype html><html><head><title>Checkout</title></head><body>
<h1>Checkout</h1>
<nav><a href="#a">Home</a><a href="#b">Orders</a></nav>
<label for="who">Full name</label><input id="who" name="who" placeholder="Full name">
<label for="mail">Email</label><input id="mail" name="mail" placeholder="Email">
<label for="city">City</label><input id="city" name="city" placeholder="City">
<select id="plan" name="plan"><option>Basic</option><option>Pro</option></select>
<input type="checkbox" id="terms" name="terms"><label for="terms">Accept terms</label>
<button data-testid="submit-btn">Place order</button>
<button id="cancel" disabled>Cancel</button>
<input id="hidden-one" style="display:none" placeholder="never show me">
<div id="out"></div>
<script>
document.querySelector('[data-testid=submit-btn]').onclick = () => {
  document.getElementById('out').textContent =
    'ORDERED:' + who.value + '/' + city.value +
    (terms.checked ? '/terms' : '') + '/' + plan.value;
};
</script></body></html>"""


def main() -> int:
    print("=" * 72)
    print("BROWSER — INDEXED PAGE, BATCHED ACTIONS, REMEMBERED SELECTORS")
    print("=" * 72)

    tmpdir = Path(tempfile.gettempdir())
    page_file = tmpdir / "agent_p11_checkout.html"
    page_file.write_text(PAGE, encoding="utf-8")
    profile = scratch_dir("agent_p11_prof_")

    b = Browser(headless=True, profile_dir=profile)
    try:
        b.goto(page_file.as_uri())

        print("\n1. the page as a numbered list")
        snap = b.snapshot()
        els = snap["elements"]
        check("indexes the interactive elements", len(els) >= 8, f"{len(els)} found")
        check("indices are 0..n-1 with no gaps",
              [e["i"] for e in els] == list(range(len(els))))
        check("skips what cannot be seen",
              not any("never show me" in (e.get("name") or "") for e in els),
              "a display:none input is not something a user can act on")
        check("carries the title and url", snap["title"] == "Checkout"
              and snap["url"].startswith("file://"))

        text = b.render(snap)
        check("renders one line per element",
              len(text.splitlines()) == len(els) + 1, str(len(text.splitlines())))
        check("shows a select's options", "options=Basic|Pro" in text)
        check("shows disabled state", "disabled" in text)
        as_json = json.dumps([e.__dict__ for e in b.elements()])
        check("is much smaller than the JSON form",
              len(text) * 2 < len(as_json),
              f"{len(text)} vs {len(as_json)} chars — every token is latency")

        print("\n2. an index resolves to exactly one node")
        idx = {e["name"]: e["i"] for e in els}
        loc = b._by_index(idx["Full name"])
        check("index resolves", loc is not None)
        check("and to a single element", loc is not None and loc.count() == 1,
              "an ambiguous target is how the wrong button gets clicked")
        check("an index that was never stamped resolves to nothing",
              b._by_index(9999) is None)

        print("\n3. one call, many actions")
        t0 = time.time()
        r = b.run([
            {"do": "fill", "target": idx["Full name"], "value": "Ada Lovelace",
             "label": "name"},
            {"do": "fill", "target": idx["Email"], "value": "ada@example.com",
             "label": "email"},
            {"do": "fill", "target": idx["City"], "value": "London", "label": "city"},
            {"do": "select", "target": idx["Basic Pro"], "value": "Pro",
             "label": "plan"},
            {"do": "check", "target": idx["terms"], "label": "terms"},
            {"do": "click", "target": idx["Place order"], "label": "submit"},
        ])
        took = time.time() - t0
        check("all steps ran", r["ok"] and r["done"] == 6, f"{r['done']}/{r['of']}")
        check("in one call", r["elapsed_s"] > 0, f"{took*1000:.0f}ms of browser work")
        body = b.text()
        check("the page agrees it happened",
              "ORDERED:Ada Lovelace/London/terms/Pro" in body,
              body[-46:].replace("\n", " "))
        check("every step is reported individually",
              len(r["steps"]) == 6 and all("did" in s for s in r["steps"]))

        print("\n4. a batch stops at the first failure")
        b.goto(page_file.as_uri())
        snap = b.snapshot()
        idx = {e["name"]: e["i"] for e in snap["elements"]}
        r = b.run([
            {"do": "fill", "target": idx["Full name"], "value": "Grace",
             "label": "name"},
            {"do": "click", "target": 9999, "label": "nonexistent"},
            {"do": "fill", "target": idx["City"], "value": "SHOULD NOT HAPPEN",
             "label": "city"},
        ])
        check("reports failure", not r["ok"])
        check("says which step", r["failed_at"] == 2, str(r.get("failed_at")))
        check("did not run the rest", "SHOULD NOT HAPPEN" not in b.text(),
              "ploughing on after a failure is how a batch does damage")
        check("the earlier step did run", r["done"] == 1, str(r["done"]))
        check("hands back a fresh page listing", "[0]<" in r.get("page", ""),
              "so recovery is one more turn, not a fresh start")

        print("\n5. it remembers what worked")
        b.goto(page_file.as_uri())
        b.snapshot()
        b.run([{"do": "fill", "target": "Full name", "value": "x", "label": "name"}])
        check("a selector was stored", bool(b.recall("name")), b.recall("name"))
        check("and it is a durable one", b.recall("name") in ("#who", '[name="who"]'),
              "an id outlives a re-render; a position does not")

        b.goto(page_file.as_uri())
        r = b.run([
            {"do": "fill", "target": "Full name", "value": "Grace Hopper",
             "label": "name"},
            {"do": "fill", "target": "City", "value": "Arlington", "label": "city"},
            {"do": "click", "target": "Place order", "label": "submit"},
        ])
        hows = [s.get("did", "") for s in r["steps"]]
        check("the repeat run uses memory, not search",
              all("remembered" in h for h in hows), "; ".join(hows)[:70])
        check("and still works", "ORDERED:Grace Hopper/Arlington" in b.text(),
              "a fast path that is wrong is worse than a slow one")

        print("\n6. memory survives a restart")
        # A second Browser object, not a second launch — Chrome locks its
        # profile directory. The claim under test is that flows load from disk
        # on construction, not that two browsers can share one profile.
        host = b._host()
        b2 = Browser(headless=True, profile_dir=profile)
        check("flows were written to disk", b._flows_file.exists(),
              b._flows_file.name)
        check("a fresh Browser loads them", host in b2._flows, host)
        check("with the same selector",
              b2._flows.get(host, {}).get("name", {}).get("selector")
              == b.recall("name"),
              b2._flows.get(host, {}).get("name", {}).get("selector", ""))

        print("\n7. a selector that stops working is dropped")
        b.goto(page_file.as_uri())
        b.remember("ghost", "#no-such-element", ok=True)
        check("stored", b.recall("ghost") == "#no-such-element")
        r = b.run([{"do": "click", "target": "Place order", "label": "ghost"}],
                  stop_on_error=False)
        check("a stale selector falls through to a search", r["ok"],
              r["steps"][0].get("did") or r["steps"][0].get("error", ""))
        check("and the search is what actually resolved it",
              "remembered" not in r["steps"][0].get("did", ""),
              r["steps"][0].get("did", ""))
        b.remember("ghost", "#no-such-element", ok=False)
        b.remember("ghost", "#no-such-element", ok=False)
        check("two misses and it is forgotten", b.recall("ghost") == "",
              "twice wrong is not a fluke; preferring a stale answer forever is")

        print("\n8. targets can still be named, not just numbered")
        b.goto(page_file.as_uri())
        r = b.run([{"do": "fill", "target": "Email", "value": "a@b.c",
                    "label": "email-by-name"}])
        check("a name still resolves", r["ok"], str(r["steps"][0])[:60])

        print("\n9. non-element actions")
        r = b.run([{"do": "read"}])
        check("read returns the page text", r["ok"] and r["steps"][0].get("text"),
              "so a batch can end by extracting what it came for")
        r = b.run([{"do": "scroll", "value": 200}])
        check("scroll works without a target", r["ok"])
        r = b.run([{"do": "nonsense", "target": 0}])
        check("an unknown action is refused", not r["ok"],
              r["steps"][0].get("error", ""))

    finally:
        b.stop()

    print("\n10. the tools are wired up")
    import asyncio
    import tools_sdk
    fn = {t.name: t for t in tools_sdk.ALL_TOOLS}
    for n in ("web_page", "web_do", "web_learned"):
        check(f"{n} exposed", n in fn)

    # Only the paths that return BEFORE touching the browser are exercised
    # here. Playwright's sync API refuses to start inside an asyncio loop
    # at all, and awaiting a tool handler creates one. What the handlers do
    # once they reach the browser is snapshot()/run(), which sections 1-9
    # test directly; this is about argument handling and registration.
    def call(name, a):
        async def go():
            return (await fn[name].handler(a))["content"][0]["text"]
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(go())
        finally:
            loop.close()

    out = call("web_do", {"steps": "not json"})
    check("web_do rejects malformed steps", "JSON" in out, out[:44])
    out = call("web_do", {"steps": "[]"})
    check("web_do rejects an empty list", "non-empty" in out, out[:40])
    out = call("web_learned", {})
    check("web_learned reports what was remembered",
          "city" in out or "Nothing learned" in out, out[:52])
    check("web_page tells the model to act by number",
          "number" in fn["web_page"].description.lower())
    check("web_do advertises itself as the batch tool",
          "one call" in fn["web_do"].description.lower())
    check("the prompt teaches the fast path",
          "web_page -> web_do" in tools_sdk.SYSTEM_PROMPT
          and "label" in tools_sdk.SYSTEM_PROMPT)



    print("\n11. the tools work where the app actually calls them")
    # This is the section that would have caught the real bug. Everything
    # above drives Browser directly and synchronously; the app reaches it
    # through coroutine tool handlers, and Playwright's sync API refuses to
    # start inside a running asyncio loop. So the browser passed 35 tests and
    # failed every single time it was used for real — the agent's own words on
    # a live run were "the MCP browser backend is broken (async-loop bug)",
    # after which it installed Playwright itself and drove Chromium from Bash.
    import asyncio as _aio
    from core.browser import ThreadedBrowser
    from core.browser import browser as shared

    check("the shared browser runs on its own thread",
          isinstance(shared, ThreadedBrowser),
          "a sync Playwright cannot live in the loop the tools run in")

    probe = tmpdir / "agent_p11_async.html"
    probe.write_text(
        "<!doctype html><title>Async probe</title>"
        "<input id=a placeholder='Name'><input id=b placeholder='City'>"
        "<button data-testid=go>Go</button><div id=out></div>"
        "<script>document.querySelector('[data-testid=go]').onclick=()=>"
        "out.textContent='OK:'+a.value+'/'+b.value;</script>",
        encoding="utf-8")

    async def drive():
        out = {}
        out["open"] = await call_async("web_open", {"url": probe.as_uri()})
        out["page"] = await call_async("web_page", {})
        out["do"] = await call_async("web_do", {"steps": json.dumps([
            {"do": "fill", "target": 0, "value": "Ada", "label": "p-name"},
            {"do": "fill", "target": 1, "value": "London", "label": "p-city"},
            {"do": "click", "target": 2, "label": "p-go"},
            {"do": "read"},
        ])})
        return out

    async def call_async(name, a):
        return (await fn[name].handler(a))["content"][0]["text"]

    got = _aio.run(drive())
    check("web_open works from a coroutine", "Opened" in got["open"],
          got["open"].splitlines()[0][:52])
    check("web_page returns a numbered listing", "[0]<" in got["page"],
          "this returned an async-loop error before the fix")
    check("web_do runs the whole batch", "4/4 steps" in got["do"],
          got["do"].splitlines()[0])
    check("and the page really changed",
          "OK:Ada/London" in got["do"], "proof it acted, not just reported")
    check("selectors were remembered through the tool layer too",
          shared.recall("p-name") in ("#a", '[name="a"]'),
          shared.recall("p-name"))

    try:
        shared.stop()
    except Exception:
        pass

    print("\n" + "=" * 72)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"{passed}/{len(results)} passed")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name}  {detail}")
    print("=" * 72)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
