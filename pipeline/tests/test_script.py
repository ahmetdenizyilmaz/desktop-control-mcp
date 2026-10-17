"""The script package: the properties that must not quietly break.

Chosen, like the vision suite, by what would fail SILENTLY:

  * a remote script that changed since it was reviewed, and runs anyway.
  * a trusted script that calls an imported one, and the import inherits
    the trust — the laundering hole the min-of-chain rule exists to close.
  * a `call` cycle discovered at run time, which is a loop that CLICKS.
  * "skip and carry on" implemented as skip-the-step, so a failed search
    is followed by a click that adds the wrong product to the basket.
  * a step that acted, did nothing, and reported success.
  * `"ara"` quietly resolving to "Arama alanı" because it matched first.
  * a stop request ignored by a loop already running.

Nothing here touches the screen, the network or the clock beyond short
sleeps: the scene is built by hand and the actuator is a recorder, so a
failure means the logic changed, never that the machine was busy.

    python tests/test_script.py
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from script import library as lib_mod                          # noqa: E402
from script import resolve as res_mod                          # noqa: E402
from script.guard import is_commit                             # noqa: E402
from script.library import Library, FetchError                 # noqa: E402
from script.runner import Runner, Stopped                      # noqa: E402
from script.schema import (MAX_CALL_DEPTH, Script, ScriptError,  # noqa: E402
                           normalise_url,
                           Selector, Trust, parse, substitute)
from vision.elements import Element, Scene                     # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}"
          + (f"  — {detail}" if detail and not ok else ""))


def raises(name: str, fn, fragment: str = "") -> None:
    """Assert that `fn` refuses, and that it says why."""
    try:
        fn()
    except Exception as exc:
        text = str(exc)
        ok = fragment.lower() in text.lower() if fragment else True
        check(name, ok, f"raised {type(exc).__name__}: {text[:90]}")
        return
    check(name, False, "did not raise")


# --------------------------------------------------------------------------
# A fake screen
# --------------------------------------------------------------------------

def el(kind: str, name: str, x: int, y: int, w: int = 100, h: int = 30,
       clickable: bool = True, value: str = "", source: str = "uia",
       confidence: float = 1.0) -> Element:
    return Element(kind=kind, name=name, left=x, top=y, width=w, height=h,
                   source=source, clickable=clickable, value=value,
                   confidence=confidence)


def scene(*elements: Element) -> Scene:
    return Scene(elements=list(elements), frame=None, ms=1.0)


class FakeWatcher:
    """Serves a canned scene, and can change it when something is clicked."""

    def __init__(self, *scenes: Scene) -> None:
        self.scenes = list(scenes)
        self.i = 0
        self.looks = 0
        self.pixel_looks = 0
        # What `_settle_on` re-points and clears.
        self.hwnd = None
        self._scene = None
        self._fp = ""

    def scene(self, force: bool = False) -> Scene:
        self.looks += 1
        return self.scenes[min(self.i, len(self.scenes) - 1)]

    def text_scene(self) -> Scene:
        # The pixel-only path serves the same canned scene; the point of
        # the fake is WHICH path was taken, so it is counted separately.
        self.pixel_looks += 1
        return self.scenes[min(self.i, len(self.scenes) - 1)]

    def advance(self) -> None:
        self.i += 1


class FakeActuator:
    """Records everything, succeeds unless told otherwise."""

    def __init__(self, fail_on: str = "", watcher: FakeWatcher | None = None,
                 advance_on: str = "",
                 windows: dict[str, int] | None = None) -> None:
        self.log: list[str] = []
        self.fail_on = fail_on
        self.watcher = watcher
        self.advance_on = advance_on
        #: title -> hwnd, what find_window searches
        self.windows = dict(windows or {})

    def _note(self, what: str) -> bool:
        self.log.append(what)
        if self.fail_on and self.fail_on in what:
            return False
        if self.watcher and self.advance_on and self.advance_on in what:
            self.watcher.advance()
        return True

    def click(self, e, button="left"):
        return self._note(f"click:{e.name}")

    def double_click(self, e):
        return self._note(f"double:{e.name}")

    def type_text(self, text, e):
        return self._note(f"type:{text}->{e.name if e else ''}")

    def press_key(self, key):
        return self._note(f"key:{key}")

    def drag(self, a, b):
        return self._note(f"drag:{a.name}->{b.name}")

    def scroll(self, e, direction, amount):
        return self._note(f"scroll:{direction}:{amount}")

    def move(self, e):
        return self._note(f"move:{e.name}")

    # -- reaching ----------------------------------------------------------

    def launch(self, app, url):
        self._note(f"launch:{app}:{url}")
        return f"launched {app or 'browser'}"

    def find_window(self, title):
        if not title:
            return 0
        for name, hwnd in self.windows.items():
            if title.lower() in name.lower():
                return hwnd
        return 0

    def foreground_window(self):
        return 4242

    def focus_window(self, hwnd, maximize=True):
        return self._note(f"focus:{hwnd}:{'max' if maximize else 'nomax'}")


def run_script(raw: dict, watcher, actuator, **kw):
    """Parse and run one script with no library involved."""
    script = parse(raw, origin="test")
    script.trust = Trust(raw.get("trust", "draft"))
    resolved = lib_mod.Resolved(entry=script, scripts={script.name: script},
                                trust=script.trust)
    return Runner(resolved, watcher, actuator, **kw)


# --------------------------------------------------------------------------
print("\nschema — what a script may say")
# --------------------------------------------------------------------------

ok = parse({"name": "t", "steps": [{"do": "click", "target": "Ara"}]})
check("a bare string target becomes a text selector",
      ok.steps[0].target == Selector(text="Ara"))

check("an import always lands as draft, whatever it claims",
      parse({"name": "t", "trust": "trusted",
             "steps": [{"do": "wait", "seconds": 1}]}).trust is Trust.DRAFT)

raises("an unknown step is refused by name",
       lambda: parse({"name": "t", "steps": [{"do": "teleport"}]}),
       "unknown step")

raises("an empty selector is refused",
       lambda: parse({"name": "t", "steps": [{"do": "click", "target": {}}]}),
       "matches everything")

raises("type without a value is refused",
       lambda: parse({"name": "t",
                      "steps": [{"do": "type", "into": "box"}]}),
       "needs a value")

raises("for_each over a query or phrase is refused",
       lambda: parse({"name": "t", "steps": [
           {"do": "repeat", "for_each": "the search results", "as": "x",
            "steps": [{"do": "click", "target": "a"}]}]}),
       "exactly one parameter")

raises("for_each over an UNDECLARED list parameter is refused",
       lambda: parse({"name": "t", "steps": [
           {"do": "repeat", "for_each": "{results}", "as": "x",
            "steps": [{"do": "click", "target": "a"}]}]}),
       "declares no parameters")

raises("an undeclared parameter is caught before anything runs",
       lambda: parse({"name": "t", "steps": [
           {"do": "type", "into": "box", "value": "{product}"}]}),
       "declares no parameters"),

check("a declared parameter passes validation",
      parse({"name": "t", "params": {"product": ""}, "steps": [
          {"do": "type", "into": "box", "value": "{product}"}]}).name == "t")

check("substitution replaces only {name}",
      substitute("buy {product} now", {"product": "süt"}) == "buy süt now")

raises("substitution refuses an unknown name rather than typing nothing",
       lambda: substitute("{missing}", {}), "no value for")

check("an expression is not a parameter and is left alone",
      substitute("{a} + 1", {"a": "2"}) == "2 + 1")

raises("a remote call with no pin is refused at parse time",
       lambda: parse({"name": "t", "steps": [
           {"do": "call", "script": "x",
            "source": "https://example.com/x.json"}]}),
       "must be pinned")

raises("a malformed pin is refused",
       lambda: parse({"name": "t", "steps": [
           {"do": "call", "script": "x", "source": "https://e.com/x.json",
            "sha256": "nope"}]}),
       "64 hex")

# --------------------------------------------------------------------------
print("\nlibrary — where scripts come from")
# --------------------------------------------------------------------------

tmp = Path(tempfile.mkdtemp(prefix="scripts-"))
cache = Path(tempfile.mkdtemp(prefix="cache-"))


def write(name: str, raw: dict) -> None:
    p = tmp / f"{name.replace('/', '_')}.json"
    p.write_text(json.dumps(raw), encoding="utf-8")


write("leaf", {"name": "leaf", "params": {"x": ""},
               "steps": [{"do": "type", "into": "box", "value": "{x}"}]})
write("top", {"name": "top", "trust": "trusted", "steps": [
    {"do": "call", "script": "leaf", "with": {"x": "hello"}}]})

L = Library(roots=[tmp], cache=cache)
r = L.resolve("top")
check("resolve pulls in the whole call graph up front",
      set(r.scripts) == {"top", "leaf"}, str(sorted(r.scripts)))
check("trust is the MINIMUM over the chain, not the entry's own",
      r.trust is Trust.DRAFT, f"got {r.trust}")

write("solo", {"name": "solo", "trust": "trusted",
               "steps": [{"do": "wait", "seconds": 0}]})
L.reload()
check("a chain of trusted scripts stays trusted",
      L.resolve("solo").trust is Trust.TRUSTED)

write("a", {"name": "a", "steps": [{"do": "call", "script": "b", "with": {}}]})
write("b", {"name": "b", "steps": [{"do": "call", "script": "a", "with": {}}]})
L.reload()
raises("a call cycle is refused at load, not discovered while clicking",
       lambda: L.resolve("a"), "cycle")

for i in range(MAX_CALL_DEPTH + 2):
    nxt = f"d{i + 1}"
    write(f"d{i}", {"name": f"d{i}", "steps": [
        {"do": "call", "script": nxt, "with": {}}]})
write(f"d{MAX_CALL_DEPTH + 2}", {"name": f"d{MAX_CALL_DEPTH + 2}",
                                 "steps": [{"do": "wait", "seconds": 0}]})
L.reload()
raises("calls nested past the cap are refused with the chain named",
       lambda: L.resolve("d0"), "deeper than")

write("needs", {"name": "needs", "params": {"a": "", "b": ""},
                "steps": [{"do": "wait", "seconds": 0}]})
write("caller", {"name": "caller", "steps": [
    {"do": "call", "script": "needs", "with": {"a": "1"}}]})
L.reload()
raises("a call missing a parameter is caught before the run",
       lambda: L.resolve("caller"), "without ['b']")

write("caller2", {"name": "caller2", "steps": [
    {"do": "call", "script": "needs", "with": {"a": "1", "b": "2", "c": "3"}}]})
L.reload()
raises("a call passing an undeclared parameter is caught too",
       lambda: L.resolve("caller2"), "which declares")

dup = tmp / "other.json"
dup.write_text(json.dumps({"name": "solo", "steps": [
    {"do": "wait", "seconds": 0}]}), encoding="utf-8")
L.reload()
raises("two files claiming the same name is an error, not a coin toss",
       lambda: L.local(), "both named")
dup.unlink()
L.reload()

# -- pinning, without a network --------------------------------------------

remote = {"name": "remote", "steps": [{"do": "wait", "seconds": 0}]}
body = json.dumps(remote).encode("utf-8")
digest = hashlib.sha256(body).hexdigest()
(cache / f"{digest}.json").write_bytes(body)

write("importer", {"name": "importer", "steps": [
    {"do": "call", "script": "remote", "with": {},
     "source": "https://example.com/remote.json", "sha256": digest}]})
L.reload()
r = L.resolve("importer")
check("a pinned script already in the cache loads with no network",
      "remote" in r.scripts)
check("an imported script is recorded as imported",
      r.imported == {"remote"}, str(r.imported))

wrong = hashlib.sha256(b"something else entirely").hexdigest()
write("bad", {"name": "bad", "steps": [
    {"do": "call", "script": "remote", "with": {},
     "source": "https://example.com/remote.json", "sha256": wrong}]})
L.reload()
raises("an unpinned fetch is refused when fetching is off",
       lambda: L.resolve("bad"), "not in the cache and fetching is off")

# Content that does not hash to its pin must be refused even from the cache.
poisoned = cache / f"{wrong}.json"
poisoned.write_bytes(json.dumps({"name": "remote", "steps": [
    {"do": "click", "target": "Öde"}]}).encode("utf-8"))
L2 = Library(roots=[tmp], cache=cache, allow_fetch=False)
raises("cached content that does not match its own hash is discarded",
       lambda: L2.resolve("bad"), "not in the cache")

L3 = Library(roots=[tmp], cache=cache, allow_fetch=True,
             allow_hosts=("trusted.example",))
raises("a host outside the allow-list is refused",
       lambda: L3.fetch("https://evil.example/x.json", wrong), "not in the allowed")
raises("plain http is refused",
       lambda: L3.fetch("http://trusted.example/x.json", wrong), "only https")

check("pinned trust never rises: an import is draft even inside a trusted top",
      L.resolve("importer").trust is Trust.DRAFT)

# --------------------------------------------------------------------------
print("\nresolve — which of these is 'Ara'")
# --------------------------------------------------------------------------

page = scene(
    el("button", "Ara", 900, 100, 60, 30),
    el("text", "Arama alanı", 600, 100, 140, 20, clickable=False),
    el("link", "Kategori arama", 100, 400, 160, 20),
    el("edit", "", 600, 100, 260, 30, value=""),
    el("button", "Sepete Ekle", 800, 300),
    el("button", "Sepete Ekle", 800, 400),
)

r = res_mod.find(page, Selector(text="Ara", kind="button"))
check("kind alone disambiguates the search button",
      r.best is not None and r.best.name == "Ara",
      r.explain())

r = res_mod.find(page, Selector(text="ara"))
check("a bare substring finds the exact match first",
      r.best.name == "Ara", r.explain())
check("...and still reports the others as candidates",
      len(r.matches) >= 3, f"{len(r.matches)} candidates")

r = res_mod.find(page, Selector(text="Arama", match="exact"))
check("exact refuses a substring", r.best is None, r.explain())

r = res_mod.find(page, Selector(text="Arama alanı", match="exact"))
check("exact accepts the whole name", r.best is not None)

r = res_mod.find(page, Selector(text="Sepete Ekle", kind="button"))
check("two identical buttons are reported as ambiguous, not guessed",
      r.ambiguous, r.explain())

r = res_mod.find(page, Selector(text="Sepete Ekle", kind="button", nth=1))
check("nth picks in reading order and resolves the ambiguity",
      not r.ambiguous and r.best.top == 300, r.explain())

r = res_mod.find(page, Selector(text="Sepete Ekle", kind="button", nth=9))
check("nth past the end is nothing, not the last one", r.best is None)

spaced = scene(el("button", "Sepete   Ekle", 10, 10))
r = res_mod.find(spaced, Selector(text="Sepete Ekle"))
check("OCR's extra whitespace does not break a multi-word selector",
      r.best is not None, r.explain())

# -- the DOM id, which UIA passes straight through -------------------------

ids = Scene(elements=[
    Element("link", "Sanalmarket sekmesi", 400, 300, 120, 30, "uia", True,
            "", "header-sanalmarket-tab"),
    Element("link", "Hemen sekmesi", 540, 300, 120, 30, "uia", True,
            "", "header-hemen-tab"),
    Element("button", "Sanalmarket", 900, 300, 120, 30, "uia", True, "", ""),
], frame=None, ms=1.0, content=(0, 200, 2880, 1700))

r = res_mod.find(ids, Selector(id="header-hemen-tab"))
check("an exact automation id wins outright, with no text needed",
      r.best is not None and r.best.name == "Hemen sekmesi" and not r.ambiguous,
      r.explain())

r = res_mod.find(ids, Selector(id="header-"))
check("a partial id matches several and says so",
      len(r.matches) == 2, r.explain())

r = res_mod.find(ids, Selector(id="does-not-exist"))
check("an id that is not on the page matches nothing, not a fuzzy guess",
      r.best is None, r.explain())

r = res_mod.find(ids, Selector(text="Sanalmarket", id="header-sanalmarket-tab"))
check("id and text together narrow to the one element with both",
      r.best.kind == "link", r.explain())

check("an id-only selector is a valid selector",
      parse({"name": "t", "steps": [
          {"do": "click", "target": {"id": "x"}}]}).steps[0].target.id == "x")

check("{param} substitutes into an id like any other text field",
      parse({"name": "t", "params": {"tab": ""}, "steps": [
          {"do": "click", "target": {"id": "header-{tab}-tab"}}]}).name == "t")

raises("...and an undeclared one in an id is still caught",
       lambda: parse({"name": "t", "steps": [
           {"do": "click", "target": {"id": "header-{tab}"}}]}),
       "declares no parameters")

hat = scene(el("listitem", "Kağıt Havlu 12'li", 10, 10))
r = res_mod.find(hat, Selector(text="kâğıt havlu"))
check("the optional Turkish circumflex matches either spelling",
      r.best is not None, r.explain())
check("...and the reverse direction too",
      res_mod.find(scene(el("listitem", "Kâğıt Havlu", 10, 10)),
                   Selector(text="kağıt")).best is not None)
check("but ç/ş/ğ/ö/ü are still distinct letters, not decorations",
      res_mod.find(scene(el("button", "çay", 10, 10)),
                   Selector(text="cay", match="exact")).best is None)

turkish = scene(el("button", "SATIN AL", 10, 10), el("button", "Iptal", 10, 60))
r = res_mod.find(turkish, Selector(text="satın al"))
check("Turkish dotted/dotless I folds both ways",
      r.best is not None and r.best.name == "SATIN AL", r.explain())

near = scene(
    el("edit", "", 600, 100, 260, 30),
    el("edit", "", 600, 900, 260, 30),
    el("text", "Ara", 560, 100, 40, 20, clickable=False),
)
r = res_mod.find(near, Selector(kind="edit", near="Ara"))
check("near picks the field beside the anchor, not the far one",
      r.best.top == 100, r.explain())

r = res_mod.find(page, Selector(any=["Search", "Ara", "Git"], kind="button"))
check("any: matches whichever alternative is on screen",
      r.best.name == "Ara", r.explain())

loose = scene(el("button", "Karar verme aracını aç", 10, 10))
r = res_mod.find(loose, Selector(text="ara"))
check("an incidental substring inside a long name scores low",
      r.matches and r.matches[0].score < 0.5,
      r.explain())

filled = scene(el("edit", "", 10, 10, value="süt"))
r = res_mod.find(filled, Selector(text="süt", kind="edit"))
check("a field is findable by what was typed into it",
      r.best is not None, r.explain())

# --------------------------------------------------------------------------
print("\nguard — what must stop for a human")
# --------------------------------------------------------------------------

check("English commit words are caught", is_commit("Place order") == "order")
check("Turkish commit words are caught", is_commit("Siparişi Tamamla") != "")
check("payment is caught in Turkish", is_commit("Ödeme Yap") != "")
check("delete is caught", is_commit("Sil") == "sil")
check("an ordinary button is not a commitment",
      is_commit("Sepete Ekle") == "", is_commit("Sepete Ekle"))
check("a word boundary stops 'buy' firing on 'buyer'",
      is_commit("Buyer information") == "", is_commit("Buyer information"))
check("Turkish removal words the basket dialog actually uses are caught",
      is_commit("Çıkar") != "" and is_commit("Sepeti Boşalt") != "",
      f'{is_commit("Çıkar")!r} {is_commit("Sepeti Boşalt")!r}')
check("...but 'Vazgeç' (cancel) is not treated as a commitment",
      is_commit("Vazgeç") == "", is_commit("Vazgeç"))
check("the guard reads value and id, not just the name",
      is_commit("", "", "checkout-submit") != "")

# --------------------------------------------------------------------------
print("\nrunner — doing it, and admitting when it did not work")
# --------------------------------------------------------------------------

w = FakeWatcher(scene(el("button", "Ara", 10, 10),
                      el("edit", "", 100, 10)))
a = FakeActuator()
rep = run_script({"name": "t", "steps": [
    {"do": "click", "target": {"text": "Ara", "kind": "button"}},
    {"do": "type", "into": {"kind": "edit"}, "value": "süt", "then": "enter"},
]}, w, a).run()
check("a clean run reports ok", rep.ok, rep.summary())
check("typing with `then` presses the key without a second step",
      a.log == ["click:Ara", "type:süt->", "key:enter"], str(a.log))

# -- the failure policy, which is the whole point --------------------------

w = FakeWatcher(scene(el("button", "Ara", 10, 10),
                      el("button", "Sepete Ekle", 10, 60)))
a = FakeActuator(fail_on="click:Ara")
rep = run_script({"name": "t", "steps": [
    {"do": "click", "target": {"text": "Ara", "kind": "button"}},
    {"do": "click", "target": {"text": "Sepete Ekle", "kind": "button"}},
]}, w, a).run()
check("a failed step abandons the rest — the basket is not corrupted",
      a.log == ["click:Ara"], str(a.log))
check("...and the run is not ok", not rep.ok)
check("...and the failure is named in the report",
      len(rep.failures()) == 1 and "Ara" in rep.failures()[0].detail,
      rep.summary())

w = FakeWatcher(scene(el("button", "Kapat", 10, 10),
                      el("button", "Devam", 10, 60)))
a = FakeActuator(fail_on="click:Kapat")
rep = run_script({"name": "t", "steps": [
    {"do": "click", "target": "Kapat", "optional": True},
    {"do": "click", "target": "Devam"},
]}, w, a).run()
check("an optional step failing does not abandon the iteration",
      a.log == ["click:Kapat", "click:Devam"], str(a.log))
check("...and an all-optional-failure run still reports ok", rep.ok,
      rep.summary())

# for_each: one bad item, the rest still attempted
w = FakeWatcher(scene(el("edit", "", 10, 10), el("button", "Ekle", 10, 60)))
a = FakeActuator(fail_on="type:peynir")
rep = run_script({"name": "t", "steps": [
    {"do": "repeat", "for_each": ["süt", "peynir", "ekmek"], "as": "p",
     "steps": [
         {"do": "type", "into": {"kind": "edit"}, "value": "{p}"},
         {"do": "click", "target": "Ekle"},
     ]}]}, w, a).run()
check("a failed item is skipped and the next one is still attempted",
      rep.items == {"süt": True, "peynir": False, "ekmek": True},
      str(rep.items))
check("...and the failed item did NOT go on to click Add",
      a.log == ["type:süt->", "click:Ekle", "type:peynir->",
                "type:ekmek->", "click:Ekle"], str(a.log))
check("...and the report names what was missed",
      "peynir" in rep.summary() and not rep.ok, rep.summary())

# -- verification -----------------------------------------------------------

w = FakeWatcher(scene(el("button", "Ekle", 10, 10)))
a = FakeActuator()
rep = run_script({"name": "t", "steps": [
    {"do": "click", "target": "Ekle", "expect": "Sepetim", "timeout": 0.1},
]}, w, a).run()
check("a click that changed nothing is a FAILED step, not a success",
      not rep.ok and "never appeared" in rep.failures()[0].detail,
      rep.summary())

w = FakeWatcher(scene(el("button", "Ekle", 10, 10)),
                scene(el("button", "Ekle", 10, 10),
                      el("text", "Sepetim 1", 10, 60, clickable=False)))
a = FakeActuator(watcher=w, advance_on="click:Ekle")
rep = run_script({"name": "t", "steps": [
    {"do": "click", "target": "Ekle", "expect": "Sepetim", "timeout": 1.0},
]}, w, a).run()
check("a click that DID change the page passes its post-condition",
      rep.ok, rep.summary())

# -- the guard, in the loop -------------------------------------------------

w = FakeWatcher(scene(el("button", "Siparişi Onayla", 10, 10)))
a = FakeActuator()
rep = run_script({"name": "t", "steps": [
    {"do": "click", "target": "Siparişi Onayla"}]}, w, a).run()
check("a commit is not clicked when nothing can confirm it",
      a.log == [] and not rep.ok, str(a.log))

asked: list[str] = []
a = FakeActuator()
rep = run_script({"name": "t", "steps": [
    {"do": "click", "target": "Siparişi Onayla"}]}, w, a,
    confirm=lambda word, e: (asked.append(word), True)[1]).run()
check("a confirmed commit goes through, and the handler was told which word",
      a.log == ["click:Siparişi Onayla"] and asked == ["onayla"],
      f"{a.log} {asked}")

a = FakeActuator()
rep = run_script({"name": "t", "steps": [
    {"do": "click", "target": "Siparişi Onayla"},
    {"do": "click", "target": "Siparişi Onayla"}]}, w, a,
    confirm=lambda word, e: False).run()
check("a refused commit fails the step rather than carrying on",
      a.log == [] and not rep.ok, str(a.log))

w2 = FakeWatcher(scene(el("button", "Öde", 10, 10)))
a = FakeActuator()
script = parse({"name": "t", "steps": [{"do": "click", "target": "Öde"}]})
script.trust = Trust.TRUSTED
resolved = lib_mod.Resolved(entry=script, scripts={"t": script},
                            trust=Trust.TRUSTED, imported={"x"})
rep = Runner(resolved, w2, a).run()
check("a trusted run that imported anything still stops at a commit",
      a.log == [] and not rep.ok, str(a.log))

# -- stop means stop --------------------------------------------------------

w = FakeWatcher(scene(el("button", "Ekle", 10, 10)))
a = FakeActuator()
count = {"n": 0}


def stop_after_two() -> bool:
    count["n"] += 1
    return count["n"] > 2


rep = run_script({"name": "t", "steps": [
    {"do": "repeat", "times": 20,
     "steps": [{"do": "click", "target": "Ekle"}]}]}, w, a,
    should_stop=stop_after_two).run()
check("a stop request ends a repeat mid-flight",
      rep.stopped and len(a.log) < 20, f"{len(a.log)} clicks")
check("...and a stopped run is never reported ok", not rep.ok)

# -- conditionals -----------------------------------------------------------

w = FakeWatcher(scene(el("button", "Kapat", 10, 10),
                      el("button", "Devam", 10, 60)))
a = FakeActuator()
rep = run_script({"name": "t", "steps": [
    {"do": "if", "present": "Kapat",
     "then": [{"do": "click", "target": "Kapat"}],
     "else": [{"do": "click", "target": "Devam"}]}]}, w, a).run()
check("if takes the then branch when the element is present",
      a.log == ["click:Kapat"], str(a.log))

w = FakeWatcher(scene(el("button", "Devam", 10, 60)))
a = FakeActuator()
rep = run_script({"name": "t", "steps": [
    {"do": "if", "present": "Kapat",
     "then": [{"do": "click", "target": "Kapat"}],
     "else": [{"do": "click", "target": "Devam"}]}]}, w, a).run()
check("...and the else branch when it is not",
      a.log == ["click:Devam"] and rep.ok, str(a.log))

# -- area:content, the trap that nearly closed the browser -----------------

# Modelled on the real Migros scene: Chrome's frame on top, page below.
chrome = Scene(elements=[
    el("button", "Kapat", 2810, 28, 48, 24),        # window close
    el("button", "Kapat", 490, 28, 48, 24),         # tab close
    el("button", "Kapat", 1400, 800, 90, 40),       # the modal's close
    el("button", "Tümünü Kabul Et", 1400, 900, 200, 40),
], frame=None, ms=1.0, content=(0, 242, 2880, 1704))

r = res_mod.find(chrome, Selector(text="Kapat", kind="button"))
check("without area, a page selector can reach the browser's own frame",
      len(r.matches) == 3, r.explain())

r = res_mod.find(chrome, Selector(text="Kapat", kind="button", area="content"))
check("area:content excludes the window frame entirely",
      len(r.matches) == 1 and r.best.center == (1445, 820), r.explain())
check("...so the one match is unambiguous and safe to click",
      not r.ambiguous, r.explain())

bare = Scene(elements=[el("button", "Kapat", 10, 10)], frame=None, ms=1.0,
             content=None)
r = res_mod.find(bare, Selector(text="Kapat", area="content"))
check("a window with no document falls back to the whole window, not nothing",
      r.best is not None, r.explain())

# Scrolled-out content is still page content. Bounding the bottom of the
# viewport discarded 5 of 7 steppers on a live results page, so a product
# that HAD been added reported failure.
scrolled = Scene(elements=[
    el("button", "Kapat", 2810, 28, 48, 24),          # browser frame, above
    el("button", "Sepete Ekle", 1200, 900, 90, 40),   # visible
    el("button", "Sepete Ekle", 1200, 2650, 90, 40),  # scrolled below fold
], frame=None, ms=1.0, content=(0, 242, 2880, 1704))

r = res_mod.find(scrolled, Selector(text="Sepete Ekle", area="content"))
check("area:content keeps content scrolled BELOW the viewport",
      len(r.matches) == 2, r.explain())
r = res_mod.find(scrolled, Selector(text="Kapat", area="content"))
check("...while still excluding the frame ABOVE the document",
      r.best is None, r.explain())

raises("a nonsense area is refused at parse time",
       lambda: parse({"name": "t", "steps": [
           {"do": "click", "target": {"text": "x", "area": "sidebar"}}]}),
       "must be 'content' or 'window'")

# -- the pixel-only path, and the trap in it -------------------------------

# The browser's TAB says "süt - Migros" while the page below still shows
# the previous search. A text selector answered from pixels alone, with no
# document rectangle, would match the tab and report success early.
tabbed = Scene(elements=[
    el("tab", "süt - Migros", 300, 40, 260, 30),
    el("listitem", "Pınar Süt 1 L", 900, 800, 300, 40),
], frame=None, ms=1.0, content=(0, 242, 2880, 1704))

r = res_mod.find(tabbed, Selector(text="süt", area="content"))
check("area:content keeps a text match out of the browser's tab strip",
      r.best is not None and r.best.kind == "listitem", r.explain())

r = res_mod.find(tabbed, Selector(text="süt"))
check("...and without it, the tab DOES match - which is why it is carried",
      len(r.matches) == 2, r.explain())

w = FakeWatcher(tabbed)
a = FakeActuator()
rep = run_script({"name": "t", "steps": [
    {"do": "wait_for", "target": {"text": "Pınar", "area": "content"},
     "timeout": 1}]}, w, a).run()
check("a text-only selector takes the PIXEL path, never the tree",
      rep.ok and w.pixel_looks == 1 and w.looks == 0,
      f"pixel={w.pixel_looks} uia={w.looks}")

w = FakeWatcher(tabbed)
rep = run_script({"name": "t", "steps": [
    {"do": "wait_for", "target": {"text": "Pınar", "kind": "listitem"},
     "timeout": 1}]}, w, a).run()
check("...but naming a kind needs the tree, so it takes the full look",
      rep.ok and w.looks == 1 and w.pixel_looks == 0,
      f"pixel={w.pixel_looks} uia={w.looks}")

# -- open and focus: reaching the thing to act on --------------------------

w = FakeWatcher(scene(el("edit", "", 10, 10)))
a = FakeActuator(windows={})          # nothing open yet
rep = run_script({"name": "t", "steps": [
    {"do": "open", "app": "chrome", "url": "https://www.migros.com.tr",
     "window": "Chrome", "timeout": 5}]}, w, a).run()
check("open launches when there is nothing to reuse",
      a.log[0] == "launch:chrome:https://www.migros.com.tr", str(a.log))

# Reuse is the default, because a second browser leaves your session in
# the first one - and because eleven stray tabs accumulated without it.
a = FakeActuator(windows={"Migros Sanal Market - Chrome": 777})
rep = run_script({"name": "t", "steps": [
    {"do": "open", "app": "chrome", "window": "Migros", "timeout": 5}]},
    w, a).run()
check("open REUSES a matching window instead of launching a second one",
      a.log == ["focus:777:max"] and rep.ok, str(a.log))
check("...and re-points the scene at it",
      w.hwnd == 777 and w._scene is None, f"hwnd={w.hwnd}")

class _Spawning(FakeActuator):
    def launch(self, app, url):
        self.windows["Migros Sanal Market - Chrome (new)"] = 778
        return super().launch(app, url)

    def find_windows(self, title):
        return [h for n, h in self.windows.items() if title.lower() in n.lower()]


a = _Spawning(windows={"Migros Sanal Market - Chrome": 777})
rep = run_script({"name": "t", "steps": [
    {"do": "open", "app": "chrome", "window": "Migros", "reuse": False,
     "timeout": 5}]}, w, a).run()
check("reuse:false launches anyway, and drives the NEW window, not the old one",
      a.log == ["launch:chrome:", "focus:778:max"], str(a.log))

a = FakeActuator(windows={})
rep = run_script({"name": "t", "steps": [
    {"do": "open", "app": "chrome", "window": "Migros", "timeout": 0.5}]},
    w, a).run()
check("a window that never appears fails the step instead of carrying on",
      not rep.ok and "no window titled" in rep.failures()[0].detail,
      rep.summary())

check("a bare host is normalised to https",
      normalise_url("migros.com.tr") == "https://migros.com.tr")
check("...and a real URL is left alone",
      normalise_url("http://x.dev/a") == "http://x.dev/a")

raises("a non-http scheme in a url is refused",
       lambda: parse({"name": "t", "steps": [
           {"do": "open", "url": "javascript:alert(1)"}]}), "only http")
raises("...including one that would launch a program",
       lambda: parse({"name": "t", "steps": [
           {"do": "open", "url": "file:///C:/Windows/System32/cmd.exe"}]}),
       "only http")
check("a host with a port is a host, not a scheme named 'migros.com.tr'",
      normalise_url("migros.com.tr:8080/x") == "https://migros.com.tr:8080/x")
raises("open with neither app nor url is refused",
       lambda: parse({"name": "t", "steps": [{"do": "open"}]}),
       "needs an app or a url")

# The trap: "" as a substring matches every window on the desktop.
w2 = FakeWatcher(scene(el("edit", "", 10, 10)))
a = FakeActuator(windows={"Some Unrelated Window": 11})
rep = run_script({"name": "t", "params": {"title": ""}, "steps": [
    {"do": "focus", "window": "{title}", "timeout": 1}]}, w2, a).run()
check("an empty window title means the FOREGROUND window, not the first one",
      a.log == ["focus:4242:max"] and w2.hwnd == 4242, str(a.log))

a = FakeActuator(windows={"Migros - Chrome": 99})
rep = run_script({"name": "t", "steps": [
    {"do": "focus", "window": "migros", "maximize": False}]}, w2, a).run()
check("focus matches a title case-insensitively and honours maximize:false",
      a.log == ["focus:99:nomax"], str(a.log))

# -- typing into whatever has focus ----------------------------------------

w = FakeWatcher(scene(el("edit", "", 10, 10)))
a = FakeActuator()
rep = run_script({"name": "t", "steps": [
    {"do": "key", "keys": "ctrl+l"},
    {"do": "type", "value": "migros.com.tr", "then": "enter"}]}, w, a).run()
check("type with no `into` goes to the focused control",
      a.log == ["key:ctrl+l", "type:migros.com.tr->", "key:enter"]
      and rep.ok, str(a.log))
check("...and the report says where it went",
      "focused control" in rep.outcomes[1].detail, rep.outcomes[1].detail)

# -- scroll, whose direction was unreachable for one commit -----------------

w = FakeWatcher(scene(el("listitem", "row", 10, 10)))
a = FakeActuator()
rep = run_script({"name": "t", "steps": [
    {"do": "scroll", "direction": "up", "amount": 7}]}, w, a).run()
check("scroll direction and amount reach the actuator",
      a.log == ["scroll:up:7"], str(a.log))

a = FakeActuator()
rep = run_script({"name": "t", "steps": [{"do": "scroll"}]}, w, a).run()
check("scroll needs no target — the focused pane is the common case",
      a.log == ["scroll:down:3"] and rep.ok, str(a.log))

raises("a nonsense scroll direction is refused at parse time",
       lambda: parse({"name": "t", "steps": [
           {"do": "scroll", "direction": "sideways"}]}), "must be 'up' or 'down'")

# -- ambiguity stops the runner --------------------------------------------

w = FakeWatcher(scene(el("button", "Sepete Ekle", 10, 10),
                      el("button", "Sepete Ekle", 10, 60)))
a = FakeActuator()
rep = run_script({"name": "t", "steps": [
    {"do": "click", "target": "Sepete Ekle"}]}, w, a).run()
check("two equally good candidates stop the step instead of picking one",
      a.log == [] and "ambiguous" in rep.failures()[0].detail,
      rep.summary())

# -- every remaining step kind, so none of them is only ever a docstring ----

w = FakeWatcher(scene(el("listitem", "rapor.txt", 10, 10),
                      el("listitem", "Arşiv", 400, 10),
                      el("button", "Gönder", 10, 200)))
a = FakeActuator()
rep = run_script({"name": "t", "steps": [
    {"do": "double_click", "target": "rapor.txt"},
    {"do": "right_click", "target": "rapor.txt"},
    {"do": "move", "target": "Arşiv"},
    {"do": "drag", "target": "rapor.txt", "to": "Arşiv"},
    {"do": "wait", "seconds": 0.05},
]}, w, a).run()
check("double_click, right_click, move, drag and wait all reach the actuator",
      a.log == ["double:rapor.txt", "click:rapor.txt", "move:Arşiv",
                "drag:rapor.txt->Arşiv"] and rep.ok, str(a.log))

raises("drag without a destination is refused at parse time",
       lambda: parse({"name": "t", "steps": [
           {"do": "drag", "target": "a"}]}), "to")

a = FakeActuator(fail_on="double:")
rep = run_script({"name": "t", "steps": [
    {"do": "double_click", "target": "rapor.txt"}]}, w, a).run()
check("a double_click that does not go through fails the step",
      not rep.ok and "double-click" in rep.failures()[0].detail,
      rep.summary())

# wait_for / expect / gone as steps in their own right
a = FakeActuator()
rep = run_script({"name": "t", "steps": [
    {"do": "wait_for", "target": "Gönder", "timeout": 1},
    {"do": "expect", "target": {"text": "Gönder", "kind": "button"},
     "timeout": 1},
    {"do": "gone", "target": "Sepetim", "timeout": 1},
]}, w, a).run()
check("wait_for, expect and gone pass when the screen agrees",
      rep.ok and a.log == [], rep.summary())

rep = run_script({"name": "t", "steps": [
    {"do": "wait_for", "target": "Hiçbir Zaman", "timeout": 0.4}]},
    w, a).run()
check("wait_for times out and says so rather than carrying on",
      not rep.ok and "did not appear" in rep.failures()[0].detail,
      rep.summary())

rep = run_script({"name": "t", "steps": [
    {"do": "gone", "target": "Gönder", "timeout": 0.4}]}, w, a).run()
check("gone fails while the thing is still there",
      not rep.ok and "still on screen" in rep.failures()[0].detail,
      rep.summary())

# -- the off-screen coordinate guard ----------------------------------------
# UIA reports elements scrolled out of sight with their VIRTUAL coordinates.
# Invoking one works; injecting a click at y=1896 on an 1800px screen does
# not — it lands on nothing and reports success.
try:
    from script.runner import DesktopActuator             # noqa: E402
    real = DesktopActuator()
    box = real._virtual_screen()
    on = el("button", "visible", (box[0] + box[2]) // 2,
            (box[1] + box[3]) // 2, 40, 20)
    check("an on-screen element resolves to its own centre",
          real._reach(on) == on.center, f"{real._reach(on)} vs {on.center}")
    off = el("button", "below the fold", 100, box[3] + 500, 40, 20)
    check("an off-screen element with no way to scroll is REFUSED, not clicked",
          real._reach(off) is None, str(real._reach(off)))
    check("...so a coordinate click on it returns failure",
          real.click(off) is False)
    check("find_window refuses an empty title at the actuator too",
          real.find_window("") == 0)
    # Read-only: enumerating windows touches nothing. The snapshot list
    # must hold every drivable window and more (minimized, untitled...).
    _every, _some = set(real.all_hwnds()), set(real.all_windows())
    check("the before-run snapshot holds every drivable window and more",
          _some <= _every and len(_every) > len(_some),
          f"{len(_some)} drivable of {len(_every)}")
    # A window an app keeps HIDDEN is not the person's: Explorer pre-creates
    # its next window that way and `explorer.exe` just shows it, so counting
    # it as pre-existing made every `open explorer` "find no new window".
    # Minimized stays theirs - that window is visible, merely iconic. The
    # probe window is a tool window (no taskbar button) parked off-screen.
    import ctypes as _ct
    _u = _ct.windll.user32
    _u.CreateWindowExW.restype = _ct.c_void_p
    _u.CreateWindowExW.argtypes = [_ct.c_uint, _ct.c_wchar_p, _ct.c_wchar_p,
                                   _ct.c_uint, _ct.c_int, _ct.c_int, _ct.c_int,
                                   _ct.c_int, _ct.c_void_p, _ct.c_void_p,
                                   _ct.c_void_p, _ct.c_void_p]
    _h = _u.CreateWindowExW(0x80, "STATIC", "runner snapshot probe",
                            0x80000000, -10000, -10000, 8, 8,
                            None, None, None, None)   # WS_EX_TOOLWINDOW, WS_POPUP, hidden
    try:
        check("a HIDDEN top-level window is not in the before-run snapshot",
              bool(_h) and _h not in set(real.all_hwnds()), f"hwnd {_h}")
        _u.ShowWindow(_ct.c_void_p(_h), 7)            # SW_SHOWMINNOACTIVE
        check("...a MINIMIZED one is", _h in set(real.all_hwnds()), f"hwnd {_h}")
    finally:
        if _h:
            _u.DestroyWindow(_ct.c_void_p(_h))
except Exception as exc:
    check("the off-screen coordinate guard", False,
          f"{type(exc).__name__}: {exc}")

# -- pick: choosing between candidates that look identical ------------------

# A product grid: four identical add buttons, four different products.
grid = Scene(elements=[
    el("link", "Sütaş Latte 1 L", 1100, 1400, 300, 30, clickable=False),
    el("button", "Ürünü sepete ekle", 1140, 1520, 48, 40),
    el("link", "İçim Süt 1 L", 1430, 1400, 300, 30, clickable=False),
    el("button", "Ürünü sepete ekle", 1470, 1520, 48, 40),
    el("link", "Alpro Yulaflı İçecek 1 L", 1760, 1400, 300, 30, clickable=False),
    el("button", "Ürünü sepete ekle", 1800, 1520, 48, 40),
], frame=None, ms=1.0, content=(0, 242, 2880, 1704))

asked_with = {}


def fake_chooser(goal, reject, options):
    asked_with.update({"goal": goal, "reject": reject, "options": dict(options)})
    key = next((k for k, v in options.items() if "İçim Süt" in v), None)
    return (key, 0.58) if key else ("c0", 0.02)


w = FakeWatcher(grid)
a = FakeActuator()
rep = run_script({"name": "t", "params": {"product": ""}, "steps": [
    {"do": "click",
     "target": {"text": "sepete ekle", "kind": "button", "area": "content"},
     "pick": {"goal": "plain drinking {product}", "reject": "latte, oat"}}]},
    w, a, chooser=fake_chooser).run({"product": "süt"})
check("pick resolves candidates that are otherwise identical",
      rep.ok and a.log == ["click:Ürünü sepete ekle"], f"{rep.summary()} {a.log}")
check("...each candidate is labelled with its own card title",
      set(asked_with["options"].values())
      == {"Sütaş Latte 1 L", "İçim Süt 1 L", "Alpro Yulaflı İçecek 1 L"},
      str(asked_with.get("options")))
check("...and {param} is substituted into the goal",
      asked_with["goal"] == "plain drinking süt", asked_with.get("goal"))
check("...clicking the CHOSEN element, not the first on the page",
      a.log and True)

# The chosen button must be the one beside "İçim Süt", not the first.
seen = []
w = FakeWatcher(grid)
a2 = FakeActuator()
run_script({"name": "t", "steps": [
    {"do": "click", "target": {"text": "sepete ekle", "kind": "button"},
     "pick": {"goal": "plain milk"}}]}, w, a2,
    chooser=lambda g, r, o: (
        next(k for k, v in o.items() if "İçim" in v), 0.6)).run()
check("the element clicked is the one beside the chosen label",
      len(a2.log) == 1, str(a2.log))

# Low margin must FAIL, not silently fall back to the first candidate.
a3 = FakeActuator()
rep = run_script({"name": "t", "steps": [
    {"do": "click", "target": {"text": "sepete ekle", "kind": "button"},
     "pick": {"goal": "plain milk", "min_margin": 0.3}}]},
    FakeWatcher(grid), a3,
    chooser=lambda g, r, o: ("c0", 0.05)).run()
check("a low margin fails the step rather than guessing the first one",
      not rep.ok and a3.log == []
      and "no clear choice" in rep.failures()[0].detail, rep.summary())

# "several are equally fine" is not "I have no idea". on_unsure:top takes
# the model's own best guess - which is still a SCORED candidate, unlike
# the page's first result.
a3b = FakeActuator()
picked = []
rep = run_script({"name": "t", "steps": [
    {"do": "click", "target": {"text": "sepete ekle", "kind": "button"},
     "pick": {"goal": "plain milk", "min_margin": 0.3,
              "on_unsure": "top"}}]},
    FakeWatcher(grid), a3b,
    chooser=lambda g, r, o: (
        picked.append(next(k for k, v in o.items() if "İçim" in v))
        or picked[-1], 0.05)).run()
check("on_unsure:top proceeds with the model's pick despite a low margin",
      rep.ok and len(a3b.log) == 1, rep.summary())

raises("a nonsense on_unsure is refused at parse time",
       lambda: parse({"name": "t", "steps": [
           {"do": "click", "target": "x",
            "pick": {"goal": "g", "on_unsure": "shrug"}}]}),
       "'fail' or 'top'")

# max_options caps how many get scored - probability spreads across
# near-duplicates, so two dozen siblings bury a clear answer.
many = Scene(elements=[e for i in range(14) for e in (
    el("link", f"Bread number {i}", 100 + i * 60, 1400, 55, 20, clickable=False),
    el("button", "Ürünü sepete ekle", 100 + i * 60, 1520, 40, 30))],
    frame=None, ms=1.0, content=(0, 242, 2880, 1704))
sizes = []
run_script({"name": "t", "steps": [
    {"do": "click", "target": {"text": "sepete ekle", "kind": "button"},
     "pick": {"goal": "bread", "max_options": 6, "on_unsure": "top"}}]},
    FakeWatcher(many), FakeActuator(),
    chooser=lambda g, r, o: (sizes.append(len(o)) or "c0", 0.9)).run()
check("max_options caps how many candidates are scored",
      sizes == [6], str(sizes))

# A chooser that cannot answer must not be silently believed.
a4 = FakeActuator()


def refuses(goal, reject, options):
    raise __import__("script.choose", fromlist=["x"]).NoChoice("no key")


rep = run_script({"name": "t", "steps": [
    {"do": "click", "target": {"text": "sepete ekle", "kind": "button"},
     "pick": {"goal": "plain milk"}}]}, FakeWatcher(grid), a4,
    chooser=refuses).run()
check("a chooser that refuses fails the step, it does not fall through",
      not rep.ok and a4.log == [], rep.summary())

# One candidate needs no decision at all.
single = scene(el("button", "Ürünü sepete ekle", 100, 100))
a5 = FakeActuator()
rep = run_script({"name": "t", "steps": [
    {"do": "click", "target": {"text": "sepete ekle", "kind": "button"},
     "pick": {"goal": "plain milk"}}]}, FakeWatcher(single), a5,
    chooser=lambda *_: (_ for _ in ()).throw(AssertionError("should not ask"))
).run()
check("a single candidate is taken without asking anything",
      rep.ok and a5.log == ["click:Ürünü sepete ekle"], rep.summary())

raises("pick without a goal is refused at parse time",
       lambda: parse({"name": "t", "steps": [
           {"do": "click", "target": "x", "pick": {"reject": "y"}}]}),
       "needs a goal")

check("a pick step round-trips through save/load",
      lib_mod.to_json(parse(lib_mod.to_json(parse({"name": "t", "steps": [
          {"do": "click", "target": "x",
           "pick": {"goal": "g", "reject": "r", "min_margin": 0.4}}]}))))
      ["steps"][0]["pick"] == {"goal": "g", "reject": "r", "min_margin": 0.4})

# -- the visual cursor is driven, and can never break a step ---------------

class FakeCursor:
    def __init__(self, explode=False):
        self.states, self.moves, self.explode = [], [], explode

    def state(self, name):
        if self.explode:
            raise RuntimeError("boom")
        self.states.append(name)

    def move_to(self, x, y):
        if self.explode:
            raise RuntimeError("boom")
        self.moves.append((x, y))


cur = FakeCursor()
w = FakeWatcher(scene(el("button", "Ekle", 100, 200, 60, 40),
                      el("edit", "", 400, 200)))
a = FakeActuator()
rep = run_script({"name": "t", "steps": [
    {"do": "click", "target": "Ekle"},
    {"do": "type", "into": {"kind": "edit"}, "value": "x"},
    {"do": "key", "keys": "enter"},
]}, w, a, cursor=cur).run()
check("the cursor is told what is happening, in order",
      rep.ok and "moving" in cur.states and "click" in cur.states
      and "type" in cur.states, str(cur.states))
check("...and moved to the element, even for TYPE where no mouse is used",
      (130, 220) in cur.moves and (450, 215) in cur.moves, str(cur.moves))

# A decoration must never be able to fail a step.
boom = FakeCursor(explode=True)
a = FakeActuator()
rep = run_script({"name": "t", "steps": [
    {"do": "click", "target": "Ekle"}]}, w, a, cursor=boom).run()
check("a cursor that raises cannot fail the run",
      rep.ok and a.log == ["click:Ekle"], rep.summary())

# The commit guard shows its own state before asking.
cur2 = FakeCursor()
a = FakeActuator()
run_script({"name": "t", "steps": [
    {"do": "click", "target": "Siparişi Onayla"}]},
    FakeWatcher(scene(el("button", "Siparişi Onayla", 10, 10))), a,
    cursor=cur2, confirm=lambda w_, e: True).run()
check("a commitment shows the blocked state before the human is asked",
      "blocked" in cur2.states, str(cur2.states))

# -- calls ------------------------------------------------------------------

L.reload()
write("child", {"name": "child", "params": {"p": ""}, "steps": [
    {"do": "type", "into": {"kind": "edit"}, "value": "{p}"}]})
write("parent", {"name": "parent", "steps": [
    {"do": "repeat", "for_each": ["a", "b"], "as": "item", "steps": [
        {"do": "call", "script": "child", "with": {"p": "{item}"}}]}]})
L.reload()
w = FakeWatcher(scene(el("edit", "", 10, 10)))
a = FakeActuator()
rep = Runner(L.resolve("parent"), w, a).run()
check("call passes its arguments and a function is just a script",
      a.log == ["type:a->", "type:b->"], str(a.log))

write("peeker", {"name": "peeker", "params": {}, "steps": [
    {"do": "type", "into": {"kind": "edit"}, "value": "{item}"}]})
L.reload()
raises("a called script cannot see the caller's scope",
       lambda: L.resolve("peeker"), "declares no parameters")

# -- round trip -------------------------------------------------------------

original = {"name": "rt", "version": 1, "params": {"p": ""}, "trust": "draft",
            "steps": [
                {"do": "click", "target": {"text": "Ara", "kind": "button"}},
                {"do": "type", "into": "box", "value": "{p}", "then": "enter"},
                {"do": "repeat", "for_each": ["x", "y"], "as": "i",
                 "steps": [{"do": "click", "target": "{i}"}]},
                {"do": "scroll", "direction": "up", "amount": 5},
            ]}
once = parse(original)
twice = parse(lib_mod.to_json(once))
check("a script survives a save/load round trip unchanged",
      lib_mod.to_json(once) == lib_mod.to_json(twice),
      json.dumps(lib_mod.to_json(twice))[:120])

# -- the shipped examples parse --------------------------------------------

shipped = Library()
try:
    known = shipped.local()
    check("the shipped Migros scripts parse",
          "migros/add-product" in known and "migros/weekly-shop" in known,
          str(sorted(known)))
    r = shipped.resolve("migros/weekly-shop")
    check("the weekly shop resolves its call graph",
          set(r.scripts) == {"migros/weekly-shop", "migros/open",
                             "migros/add-product", "migros/search",
                             "browser/dismiss", "browser/goto"},
          str(sorted(r.scripts)))
    check("the browser shortcuts parse and resolve on their own",
          set(shipped.resolve("browser/new-tab").scripts)
          == {"browser/new-tab", "browser/goto"},
          str(sorted(shipped.resolve("browser/new-tab").scripts)))
    check("the shipped scripts are draft, so a commit would stop",
          r.trust is Trust.DRAFT)
except Exception as exc:
    check("the shipped Migros scripts parse", False, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
print("\nthe app's engine hooks — trust, rescue, the list, the library")
# --------------------------------------------------------------------------

# -- honour_trust: "Supervised" asks even a trusted script -----------------
wt = FakeWatcher(scene(el("button", "Öde", 10, 10)))


def trusted_run(**kw):
    sc = parse({"name": "t", "steps": [{"do": "click", "target": "Öde"}]})
    sc.trust = Trust.TRUSTED
    rv = lib_mod.Resolved(entry=sc, scripts={"t": sc}, trust=Trust.TRUSTED)
    act = FakeActuator()
    asked_ = []
    Runner(rv, wt, act,
           confirm=lambda word, e: (asked_.append(word), True)[1], **kw).run()
    return act, asked_


act_, asked_ = trusted_run()
check("by default a TRUSTED script commits without asking",
      act_.log == ["click:Öde"] and asked_ == [], f"{act_.log} {asked_}")
act_, asked_ = trusted_run(honour_trust=False)
check("honour_trust=False (Supervised) asks even a trusted script",
      act_.log == ["click:Öde"] and asked_ == ["öde"], f"{act_.log} {asked_}")


# -- rescue_tries: a second try is told what the first did -----------------
class FakeHelper:
    name = "fake"

    def __init__(self, plans):
        self.plans = list(plans)
        self.budget = 10
        self.calls: list[dict] = []

    def rescue(self, goal, error, scene, history=None):
        self.calls.append({"goal": goal, "history": list(history or [])})
        self.budget -= 1
        return self.plans.pop(0) if self.plans else {"steps": []}

    def parse(self, plan, where):
        from script.schema import _step
        return [_step(s, where) for s in plan.get("steps") or []]


wr = FakeWatcher(scene(el("button", "OK", 10, 10)))
ar = FakeActuator()
hp = FakeHelper([{"why": "nothing obvious", "steps": []},
                 {"why": "the button is OK", "steps": [
                     {"do": "click", "target": {"text": "OK",
                                                "match": "exact"}}]}])
rep = run_script({"name": "t", "steps": [
    {"do": "click", "target": {"text": "Missing", "match": "exact"}}]},
    wr, ar, helper=hp, rescue_tries=2).run()
check("rescue_tries=2 asks the helper twice when the first try fails",
      len(hp.calls) == 2 and rep.ok and ar.log == ["click:OK"],
      f"{len(hp.calls)} calls, ok={rep.ok}, {ar.log}")
check("the second try is told what the first one did",
      hp.calls[1]["history"] and "try 1" in hp.calls[1]["history"][0],
      str(hp.calls[1]["history"] if len(hp.calls) > 1 else "no 2nd call"))

hp1 = FakeHelper([{"steps": []}, {"steps": []}])
rep = run_script({"name": "t", "steps": [
    {"do": "click", "target": {"text": "Missing", "match": "exact"}}]},
    FakeWatcher(scene(el("button", "OK", 10, 10))), FakeActuator(),
    helper=hp1).run()
check("the default is still ONE rescue try per step",
      len(hp1.calls) == 1 and not rep.ok, f"{len(hp1.calls)} calls")

# -- the chat list: "n)" not "n." ------------------------------------------
wl = FakeWatcher(scene(el("edit", "Type a message", 10, 10)))
al = FakeActuator()
rn = run_script({"name": "t", "steps": [
    {"do": "type", "into": {"text": "Type a message", "kind": "edit"},
     "value": "List:", "lines_from": "picks"}]}, wl, al)
rn.report.bought = {"Süt 1 L": 1, "Ekmek": 3}
rn.run()
typed = [x for x in al.log if x.startswith("type:")]
check("a bought list numbers lines as '1)' - a chat renders '1.' twice",
      any(x.startswith("type:1) Süt 1 L") for x in typed)
      and any(x.startswith("type:2) Ekmek  x3") for x in typed),
      str(typed))

# -- a tolerant library ----------------------------------------------------
tl = Path(tempfile.mkdtemp(prefix="tolerant-"))
(tl / "good.json").write_text(json.dumps(
    {"name": "good", "steps": [{"do": "wait", "seconds": 0}]}),
    encoding="utf-8")
(tl / "bad.json").write_text("{ not json", encoding="utf-8")
(tl / "dup.json").write_text(json.dumps(
    {"name": "good", "steps": [{"do": "wait", "seconds": 0}]}),
    encoding="utf-8")
raises("a strict library still refuses a bad file",
       lambda: Library(roots=[tl], cache=cache).local(), "")
soft = Library(roots=[tl], cache=cache, strict=False)
check("a tolerant library skips a bad file and a duplicate, and says so",
      set(soft.local()) == {"good"} and len(soft.errors) == 2,
      f"{sorted(soft.local())} errors={soft.errors}")

# -- extra keys survive, delete goes to the trash --------------------------
xs = parse({"name": "site/x", "listed": False, "tags": ["a", "b"],
            "cancel": {"window": "WhatsApp", "words": ["cancel"]},
            "steps": [{"do": "wait", "seconds": 0}]})
out = lib_mod.to_json(xs)
check("keys the format does not read are kept through a save",
      out.get("listed") is False and out.get("tags") == ["a", "b"]
      and out.get("cancel", {}).get("window") == "WhatsApp"
      and list(out)[-1] == "steps",
      str(out))

dl = Path(tempfile.mkdtemp(prefix="deleting-"))
dlib = Library(roots=[dl], cache=cache, strict=False)
dlib.save(parse({"name": "site/gone", "steps": [{"do": "wait",
                                                  "seconds": 0}]}))
check("path_of finds the file a script came from",
      dlib.path_of("site/gone") is not None
      and dlib.path_of("site/gone").exists())
dest = dlib.delete("site/gone")
check("delete moves the file to .trash, and the library forgets it",
      dest.exists() and ".trash" in dest.parts
      and "site/gone" not in dlib.local(),
      f"{dest} {sorted(dlib.local())}")


# --------------------------------------------------------------------------
print("\nhooks — stuck vs waiting, as the host sees it")
# --------------------------------------------------------------------------
import time as _time                                        # noqa: E402
from script.runner import RunHooks                          # noqa: E402

events: list[tuple[str, dict]] = []
hk = RunHooks(phase=lambda k, i: events.append((k, i)))
wv = FakeWatcher(scene(el("button", "OK", 10, 10)))
rep = run_script({"name": "t", "steps": [
    {"do": "click", "target": {"text": "OK", "match": "exact"}}]},
    wv, FakeActuator(), hooks=hk).run()
kinds = [k for k, _ in events]
check("phase events come in order: start, acted, end",
      kinds == ["step_start", "acted", "step_end"], str(kinds))

events.clear()
rep = run_script({"name": "t", "steps": [
    {"do": "click", "target": {"text": "Nope", "match": "exact"}}]},
    FakeWatcher(scene(el("button", "OK", 10, 10))), FakeActuator(),
    hooks=RunHooks(phase=lambda k, i: events.append((k, i)))).run()
check("a failed step reports step_failed with its reason",
      any(k == "step_failed" and "Nope" in i.get("detail", "")
          for k, i in events), str(events))

asked_early = []


def fail_after(sec):
    def p(elapsed, timeout):
        asked_early.append(elapsed)
        return "fail" if elapsed >= sec else "keep"
    return p


t0 = _time.time()
rep = run_script({"name": "t", "steps": [
    {"do": "wait_for", "target": {"text": "Never", "match": "exact"},
     "timeout": 20}]},
    FakeWatcher(scene(el("button", "OK", 10, 10))), FakeActuator(),
    hooks=RunHooks(patience=fail_after(0.5))).run()
took = _time.time() - t0
check("patience 'fail' gives up long before the step's own timeout",
      not rep.ok and took < 3, f"{took:.1f}s of a 20s timeout")

calls = {"n": 0}


def extend_twice(elapsed, timeout):
    if elapsed >= timeout and calls["n"] < 2:
        calls["n"] += 1
        return "extend"
    return "keep"


t0 = _time.time()
rep = run_script({"name": "t", "steps": [
    {"do": "wait_for", "target": {"text": "Never", "match": "exact"},
     "timeout": 0.5}]},
    FakeWatcher(scene(el("button", "OK", 10, 10))), FakeActuator(),
    hooks=RunHooks(patience=extend_twice)).run()
took = _time.time() - t0
check("patience 'extend' keeps a busy wait going past its timeout",
      not rep.ok and calls["n"] == 2 and took >= 2.0, f"{took:.1f}s")

held = {"n": 0}


def hold_once():
    if held["n"] == 0:
        held["n"] += 1
        _time.sleep(1.2)
        return 1.2
    return 0.0


t0 = _time.time()
wa = FakeWatcher(scene(el("button", "OK", 10, 10)))
rep = run_script({"name": "t", "steps": [
    {"do": "wait_for", "target": {"text": "Never", "match": "exact"},
     "timeout": 1.0}]}, wa, FakeActuator(),
    hooks=RunHooks(hold=hold_once)).run()
took = _time.time() - t0
check("a pause moves the deadline instead of eating the wait",
      took >= 2.0, f"{took:.1f}s for a 1s wait with a 1.2s pause")


class ChatWatcher(FakeWatcher):
    """Pixels of a chat: 'buy' arrives on the Nth read."""

    def __init__(self, arrive_after):
        super().__init__(scene(el("text", "Sepet hazir", 10, 10,
                                  source="ocr")))
        self.reads = 0
        self.arrive_after = arrive_after
        self.content = (0, 0, 1000, 1000)
        self.hwnd = 0

    def text_scene(self):
        self.reads += 1
        items = [el("text", "Sepet hazir", 10, 10, source="ocr")]
        if self.reads > self.arrive_after:
            items.append(el("text", "buy", 10, 60, source="ocr"))
        return scene(*items)


expired = []
rep = run_script({"name": "t", "steps": [
    {"do": "wait_message", "words": ["buy"], "timeout": 1}]},
    ChatWatcher(arrive_after=3), FakeActuator(),
    hooks=RunHooks(wait_expired=lambda w: (expired.append(w), "keep")[1])
).run()
check("a chat wait that runs out is kept going when the host says so",
      rep.ok and expired, f"ok={rep.ok} expired={expired}")

ev2 = []
rep = run_script({"name": "t", "steps": [
    {"do": "wait_message", "words": ["buy"], "timeout": 30}]},
    ChatWatcher(arrive_after=1), FakeActuator(),
    hooks=RunHooks(phase=lambda k, i: ev2.append(k))).run()
check("a chat wait tells the host it is WAITING (begin and end)",
      "wait_begin" in ev2 and "wait_end" in ev2
      and ev2.index("wait_begin") < ev2.index("wait_end"), str(ev2))

ev3 = []
wc = FakeWatcher(scene(el("button", "Siparişi Onayla", 10, 10)))
run_script({"name": "t", "steps": [
    {"do": "click", "target": "Siparişi Onayla"}]}, wc, FakeActuator(),
    confirm=lambda w_, e: True,
    hooks=RunHooks(phase=lambda k, i: ev3.append(k))).run()
check("asking for approval is reported as a wait, not a stall",
      "confirm_begin" in ev3 and "confirm_end" in ev3, str(ev3))

rep = run_script({"name": "t", "steps": [
    {"do": "repeat", "for_each": ["a", "b", "c"], "as": "x",
     "steps": [{"do": "click", "target": {"text": "{x}", "match": "exact"}}]}]},
    FakeWatcher(scene(el("button", "a", 10, 10), el("button", "b", 10, 60),
                      el("button", "c", 10, 110))),
    FakeActuator(), skip_items={"a", "b"})
ra = rep.act if hasattr(rep, "act") else None
out = rep.run()
check("skip_items resumes a repeat without redoing finished items",
      rep.act.log == ["click:c"] and out.items == {"a": True, "b": True,
                                                   "c": True},
      f"{rep.act.log} {out.items}")


# --------------------------------------------------------------------------
print("\nlist parameters - one script, any shopping list")
# --------------------------------------------------------------------------
_btns = scene(el("button", "süt", 0, 0), el("button", "ekmek", 0, 40),
              el("button", "peynir", 0, 80))
_list_script = {"name": "t", "params": {"items": ""}, "steps": [
    {"do": "repeat", "for_each": "{items}", "as": "x",
     "steps": [{"do": "click", "target": {"text": "{x}", "match": "exact"}}]}]}
_lp = FakeActuator()
_lo = run_script(_list_script, FakeWatcher(_btns), _lp).run(
    {"items": "süt, ekmek, peynir"})
check("a list parameter repeats over its comma-separated items",
      _lp.log == ["click:süt", "click:ekmek", "click:peynir"]
      and _lo.items == {"süt": True, "ekmek": True, "peynir": True},
      str(_lp.log))
_lp2 = FakeActuator()
run_script(_list_script, FakeWatcher(_btns), _lp2).run(
    {"items": "- süt\n- ekmek\n- süt"})
check("one item per line, bullets and repeats dropped",
      _lp2.log == ["click:süt", "click:ekmek"], str(_lp2.log))
_lo3 = run_script(_list_script, FakeWatcher(_btns), FakeActuator()).run(
    {"items": "  "})
check("an empty list fails the step instead of silently doing nothing",
      not _lo3.ok and any("empty" in o.detail for o in _lo3.failures()),
      str([o.detail for o in _lo3.failures()]))
_ev = []
run_script(_list_script, FakeWatcher(_btns), FakeActuator(),
           hooks=RunHooks(phase=lambda k, i: _ev.append((k, i.get("item")))))\
    .run({"items": "süt, ekmek"})
check("each finished item is reported (what a resume checkpoint records)",
      [x for x in _ev if x[0] == "item_end"] == [("item_end", "süt"),
                                                 ("item_end", "ekmek")],
      str(_ev))


class _MisreadWatcher(FakeWatcher):
    """OCR reads 'jpy', the accessibility tree has the real text."""

    def text_scene(self):
        self.pixel_looks += 1
        return scene(el("text", "jpy ikinci deneme", 0, 0, source="ocr"))

    def scene(self, force=False):
        self.looks += 1
        return scene(el("tab", "jev ikinci deneme. Modified.", 0, 0))


_mw = _MisreadWatcher(scene())
_mr = run_script({"name": "t", "steps": [
    {"do": "wait_for", "target": "jev ikinci deneme", "timeout": 5}]},
    _mw, FakeActuator()).run()
check("an OCR misread cannot fail a text check the tree can answer",
      _mr.ok and _mw.pixel_looks >= 1 and _mw.looks >= 1,
      f"ok={_mr.ok} pixels={_mw.pixel_looks} tree={_mw.looks}")

# --------------------------------------------------------------------------
print("\nthe provider query: text labels, id AND text, kinds it cannot ask")
# --------------------------------------------------------------------------
from script import runner as runner_mod                      # noqa: E402

_real_q = (runner_mod.query.available, runner_mod.query.find)
_display = Element("text", "Display is 0", 10, 10, 200, 40, "uia", True, "",
                   "CalculatorResults", 1.0, None)
_asked = []


def _fake_find(hwnd, aid="", name="", kinds=(), limit=60):
    _asked.append((aid, name, kinds))
    if aid == "CalculatorResults" or name == "Display is 0":
        return [_display]
    return []


runner_mod.query.available = lambda: True
runner_mod.query.find = _fake_find
_qw = FakeWatcher(scene())
_qw.hwnd = 99
_qr = run_script({"name": "t", "steps": [{"do": "wait", "seconds": 0}]}, _qw,
                 FakeActuator())
_sel = lambda **k: parse({"name": "t", "steps": [{"do": "expect", "target": k}]},
                         origin="test").steps[0].target
h1, g1 = _qr._ask(_sel(text="Display is 0", match="exact", id="CalculatorResults"))
check("id + the text it shows: found", h1 and g1 is _display)
h2, g2 = _qr._ask(_sel(text="Display is 42", match="exact", id="CalculatorResults"))
check("id + a text it does NOT show: conclusively absent (gone can pass)",
      h2 and g2 is None)
h3, g3 = _qr._ask(_sel(text="Display is 0", match="exact"))
check("an exact name is asked of the provider", h3 and g3 is _display)
h4, g4 = _qr._ask(_sel(text="Back", kind="icon"))
check("a kind no condition can express is not answered as 'absent'",
      h4 is False and g4 is None)
check("'text' is a kind the provider can be asked for",
      runner_mod.query.queryable("text") and not runner_mod.query.queryable("icon"))
runner_mod.query.available, runner_mod.query.find = _real_q

# --------------------------------------------------------------------------
print("\nopen: the person's windows are not the run's")
# --------------------------------------------------------------------------


class _WinActuator(FakeActuator):
    """Windows that exist, appear on launch, and die - with the process
    behind each, and the ones a taskbar hides, as the real actuator sees
    them."""

    def __init__(self, windows, spawn=None, front=4242, exes=None, hidden=()):
        super().__init__(windows=windows)
        self.spawn, self.front, self.dead = spawn, front, set()
        #: hwnd -> image name of the process behind it ("" = unreadable)
        self.exes = dict(exes or {})
        #: minimized or cloaked: a top-level handle that exists but that the
        #: drivable list (`all_windows`) drops, as `locator.list_windows` does
        self.hidden = set(hidden)
        #: hwnd -> (pid, class); a test changes one to recycle the handle
        self.ids: dict[int, tuple] = {}

    def launch(self, app, url):
        out = super().launch(app, url)
        if self.spawn:
            self.windows[self.spawn[0]] = self.spawn[1]
        return out

    def _drivable(self):
        return [h for h in self.windows.values()
                if h not in self.dead and h not in self.hidden]

    def find_windows(self, title):
        return [h for n, h in self.windows.items()
                if title.lower() in n.lower() and h in self._drivable()]

    def all_windows(self):
        return self._drivable()

    def all_hwnds(self):
        return [h for h in self.windows.values() if h not in self.dead]

    def is_window(self, h):
        return h in self.windows.values() and h not in self.dead

    def foreground_window(self):
        return self.front

    def window_exe(self, h):
        return self.exes.get(h, "")

    def window_identity(self, h):
        return self.ids.get(h, (h, "Window"))


def _why(rep, log=None):
    return str(rep.failures()[0].detail if rep.failures() else (log or rep.summary()))


_open_explorer = {"name": "t", "steps": [
    {"do": "open", "app": "explorer", "window": "File Explorer", "timeout": 1}]}
_wa = _WinActuator({"Downloads - File Explorer": 111},
                   spawn=("Home - File Explorer", 222))
_ww = FakeWatcher(scene())
_r = run_script(_open_explorer, _ww, _wa).run()
check("open Explorer with the person's folder window open: a NEW window is "
      "launched and driven", _r.ok and _ww.hwnd == 222 and "launch:explorer:" in _wa.log
      and "focus:111:max" not in _wa.log, str(_wa.log))
_wb = _WinActuator({"Downloads - File Explorer": 111})
_wbw = FakeWatcher(scene())
_rb = run_script(_open_explorer, _wbw, _wb).run()
check("…and if no new window appears, the step FAILS rather than use theirs",
      not _rb.ok and _wbw.hwnd is None and "focus:111:max" not in _wb.log
      and "no new window" in _why(_rb), _why(_rb, _wb.log))
_wc = _WinActuator({"Main Page - Wikipedia - Google Chrome": 333},
                   exes={333: "chrome.exe"})
_wcw = FakeWatcher(scene())
_rc = run_script({"name": "t", "steps": [
    {"do": "open", "app": "chrome", "url": "https://en.wikipedia.org/",
     "window": "Wikipedia", "timeout": 1}]}, _wcw, _wc).run()
check("a browser is still reused: the person's session is the point",
      _rc.ok and _wcw.hwnd == 333 and not any(x.startswith("launch") for x in _wc.log),
      str(_wc.log))
_wd = _WinActuator({"Downloads - File Explorer": 111, "Home - File Explorer": 222},
                   front=444)
_wdr = run_script({"name": "t", "steps": [{"do": "wait", "seconds": 0}]},
                  FakeWatcher(scene()), _wd)
_wd.windows["All Control Panel Items"] = 444            # appeared during the run
_wdr.watcher.hwnd = 222
_wd.dead.add(222)                                        # Explorer replaced its window
_wdr._follow_replaced()
check("a window that replaced the one being driven is followed",
      _wdr.watcher.hwnd == 444)
_wdr.watcher.hwnd = 444
_wd.dead.add(444)
_wd.front = 111                                          # the person's window is in front
_wdr._follow_replaced()
check("…but never into a window the person already had",
      _wdr.watcher.hwnd == 444)
_we = _WinActuator({"Downloads - File Explorer": 111, "Home - File Explorer": 222})
_wer = run_script({"name": "t", "steps": [{"do": "wait", "seconds": 0}]},
                  FakeWatcher(scene()), _we)
_we.windows["Wallpaper - File Explorer"] = 555
check("focus prefers a window the run opened over one the person had",
      _wer._pick_window("File Explorer") == 555)

# -- focus: the person's window is not a fallback ---------------------------
# Win11 Notepad opens a TAB in the window the person already has, so `open
# notepad` correctly finds no new window; the rescue then proposed
# {"do":"focus","window":"Notepad"} - and the person's Notepad was
# focused, maximized and typed into.
_fa = _WinActuator({"Untitled - Notepad": 101}, exes={101: "notepad.exe"})
_faw = FakeWatcher(scene())
_fr = run_script({"name": "t", "steps": [
    {"do": "focus", "window": "Notepad", "timeout": 0.5}]}, _faw, _fa).run()
check("focus on a window the person had open is REFUSED, not driven",
      not _fr.ok and _fa.log == [] and _faw.hwnd is None
      and "not the run's to use" in _why(_fr), _why(_fr, _fa.log))
_fb = _WinActuator({"Untitled - Notepad": 101}, exes={101: "notepad.exe"})
_fbw = FakeWatcher(scene(el("edit", "", 10, 10)))
_fh = FakeHelper([{"why": "focus the Notepad that is open", "steps": [
    {"do": "focus", "window": "Notepad", "reuse": True, "timeout": 0.3}]}])
_frb = run_script({"name": "t", "steps": [
    {"do": "open", "app": "notepad", "window": "Notepad", "timeout": 0.3},
    {"do": "type", "value": "hello"}]}, _fbw, _fb, helper=_fh).run()
check("…and a rescue's `focus` cannot claim it either, even with reuse:true",
      not _frb.ok and "focus:101:max" not in _fb.log
      and not any(x.startswith("type") for x in _fb.log), str(_fb.log))
_fc = _WinActuator({"Untitled - Notepad": 101}, exes={101: "notepad.exe"})
_fcw = FakeWatcher(scene())
_frc = run_script({"name": "t", "steps": [
    {"do": "focus", "window": "Notepad", "reuse": True}]}, _fcw, _fc).run()
check("the SCRIPT itself may say reuse:true and take it",
      _frc.ok and _fc.log == ["focus:101:max"] and _fcw.hwnd == 101, str(_fc.log))
_fd = _WinActuator({"Wikipedia - Google Chrome": 102}, exes={102: "chrome.exe"})
_fdw = FakeWatcher(scene())
_frd = run_script({"name": "t", "steps": [
    {"do": "focus", "window": "Wikipedia"}]}, _fdw, _fd).run()
check("a reusable app's window (their browser) may be focused without asking",
      _frd.ok and _fd.log == ["focus:102:max"], str(_fd.log))
_fe = _WinActuator({"Downloads - File Explorer": 111}, front=111,
                   exes={111: "explorer.exe"})
_few = FakeWatcher(scene())
_fre = run_script({"name": "t", "params": {"title": ""}, "steps": [
    {"do": "focus", "window": "{title}"}]}, _few, _fe).run()
check("an empty title means the foreground - unless that is the person's window",
      not _fre.ok and _fe.log == [] and "not the run's to use" in _why(_fre),
      _why(_fre, _fe.log))
_ff = _WinActuator({"Main Page - Wikipedia - Google Chrome": 333},
                   exes={333: "chrome.exe"})
_ffw = FakeWatcher(scene())
_frf = run_script({"name": "t", "steps": [
    {"do": "open", "app": "chrome", "url": "https://en.wikipedia.org/",
     "window": "Wikipedia", "timeout": 1},
    {"do": "focus", "window": "Wikipedia"}]}, _ffw, _ff).run()
check("a window an `open` reused is the run's from then on: `focus` finds it",
      _frf.ok and _ff.log == ["focus:333:max", "focus:333:max"], str(_ff.log))

# -- open without a window title: a new window, never "whatever is in front"
_oa = _WinActuator({"Downloads - File Explorer": 111}, front=111,
                   spawn=("Untitled - Paint", 505), exes={111: "explorer.exe"})
_oaw = FakeWatcher(scene())
_ora = run_script({"name": "t", "steps": [
    {"do": "open", "app": "paint", "timeout": 1}]}, _oaw, _oa).run()
check("open with no window title settles on the window the launch created",
      _ora.ok and _oaw.hwnd == 505 and "focus:111:max" not in _oa.log, str(_oa.log))
_ob = _WinActuator({"Downloads - File Explorer": 111}, front=111,
                   exes={111: "explorer.exe"})
_obw = FakeWatcher(scene())
_orb = run_script({"name": "t", "steps": [
    {"do": "open", "app": "paint", "timeout": 0.5}]}, _obw, _ob).run()
check("…and when nothing new appears (a slow launch) it fails rather than "
      "drive the window in front",
      not _orb.ok and _obw.hwnd is None and "focus:111:max" not in _ob.log
      and "no new window" in _why(_orb), _why(_orb, _ob.log))
_oc = _WinActuator({"Terminal": 7, "Google - Google Chrome": 333}, front=7,
                   exes={7: "windowsterminal.exe", 333: "chrome.exe"})
_ocw = FakeWatcher(scene())
_orc = run_script({"name": "t", "steps": [
    {"do": "open", "app": "chrome", "url": "https://example.org", "timeout": 1}]},
    _ocw, _oc).run()
check("a reusable app with no window title settles on ITS window, not the "
      "terminal in front", _orc.ok and _ocw.hwnd == 333 and "focus:7:max" not in _oc.log,
      str(_oc.log))
_od = _WinActuator({"Downloads - File Explorer": 111}, front=111,
                   exes={111: "explorer.exe"})
_odw = FakeWatcher(scene())
_odr = run_script({"name": "t", "steps": [
    {"do": "open", "app": "paint", "timeout": 1},
    {"do": "open", "app": "charmap", "timeout": 0.5}]}, _odw, _od)
_od.spawn = ("Untitled - Paint", 505)
_ord = _odr.run()
check("a second `open` waits for ITS window, not the one the first step opened",
      not _ord.ok and _odw.hwnd == 505 and _od.log.count("focus:505:max") == 1
      and _ord.outcomes[0].ok and not _ord.outcomes[1].ok, str(_od.log))

# -- the before-run snapshot sees minimized windows -------------------------
# A minimized window reports a -32000 rectangle and fails the drivable
# list's size filter; the person's minimized Explorer, restored mid-run,
# therefore looked like a window the run had opened.
_ma = _WinActuator({"Downloads - File Explorer": 111}, hidden={111})
_mar = run_script({"name": "t", "steps": [{"do": "wait", "seconds": 0}]},
                  FakeWatcher(scene()), _ma)
check("a window minimized at run start is in the snapshot",
      111 in _mar._before_run and 111 not in _ma.all_windows())
_ma.windows["Home - File Explorer"] = 222                  # the run's own
_mar._point_at(222)
_ma.dead.add(222)                                          # ...replaced itself
_ma.hidden.discard(111)                                    # the person restored theirs
_ma.front = 111
_mar._follow_replaced()
check("…so restoring it mid-run does not make it the run's to follow into",
      _mar.watcher.hwnd == 222)

# -- reuse is by process, not by title --------------------------------------
_pa = _WinActuator({"Wikipedia notes - Notepad": 201},
                   spawn=("Wikipedia - Google Chrome", 202),
                   exes={201: "notepad.exe", 202: "chrome.exe"})
_paw = FakeWatcher(scene())
_pra = run_script({"name": "t", "steps": [
    {"do": "open", "app": "chrome", "url": "https://en.wikipedia.org/",
     "window": "Wikipedia", "timeout": 1}]}, _paw, _pa).run()
check("open chrome does not reuse a Notepad document titled 'Wikipedia'",
      _pra.ok and _paw.hwnd == 202 and "focus:201:max" not in _pa.log
      and any(x.startswith("launch") for x in _pa.log), str(_pa.log))
_pb = _WinActuator({"Main Page - Wikipedia - Google Chrome": 333})   # exe unreadable
_pbw = FakeWatcher(scene())
_prb = run_script({"name": "t", "steps": [
    {"do": "open", "app": "chrome", "url": "https://en.wikipedia.org/",
     "window": "Wikipedia", "timeout": 0.3}]}, _pbw, _pb).run()
check("a window whose process cannot be read is not assumed to be the browser",
      "focus:333:max" not in _pb.log and any(x.startswith("launch") for x in _pb.log),
      str(_pb.log))
_pc = _WinActuator({"Settings": 301}, exes={301: "systemsettings.exe"})
_pcw = FakeWatcher(scene())
_prc = run_script({"name": "t", "steps": [
    {"do": "open", "app": "ms-settings:display", "window": "Settings",
     "timeout": 1}]}, _pcw, _pc).run()
check("ms-settings:<page> reuses the Settings window by its process",
      _prc.ok and _pc.log == ["focus:301:max"], str(_pc.log))

# -- a recycled handle is not the same window --------------------------------
_ra = _WinActuator({"Home - File Explorer": 222}, front=444)
_rar = run_script({"name": "t", "steps": [{"do": "wait", "seconds": 0}]},
                  FakeWatcher(scene()), _ra)
_ra.windows["Home - File Explorer"] = 222
_rar._point_at(222)
_ra.windows["All Control Panel Items"] = 444
_rar._follow_replaced()
check("a live handle with the same process and class behind it is still ours",
      _rar.watcher.hwnd == 222)
_ra.ids[222] = (9999, "Notepad")                           # the handle was recycled
_rar._follow_replaced()
check("…but the same handle number over a different process is a different "
      "window, and the run moves on", _rar.watcher.hwnd == 444)

# -- the provider query and a click: a label is not the control -------------
_real_q2 = (runner_mod.query.available, runner_mod.query.find)
_save_label = Element("text", "Save", 10, 10, 60, 20, "uia", True, "", "", 1.0, None)
_save_btn = el("button", "Save", 80, 10)


def _fake_find2(hwnd, aid="", name="", kinds=(), limit=60):
    if name == "Save":
        return [_save_label] if "button" not in kinds else [_save_btn]
    return []


runner_mod.query.available = lambda: True
runner_mod.query.find = _fake_find2
_qw2 = FakeWatcher(scene(_save_btn))
_qw2.hwnd = 99
_qr2 = run_script({"name": "t", "steps": [{"do": "wait", "seconds": 0}]}, _qw2,
                  FakeActuator())
h5, g5 = _qr2._ask(_sel(text="Save", match="exact"))
check("for an `expect`, the query may answer with the text label", h5 and g5 is _save_label)
h6, g6 = _qr2._ask(_sel(text="Save", match="exact"), clickable=True)
check("for a CLICK it may not: a text-only answer falls through to the walk",
      h6 is False and g6 is None)
_qa2 = FakeActuator()
_qrep = run_script({"name": "t", "steps": [
    {"do": "click", "target": {"text": "Save", "match": "exact"}}]}, _qw2, _qa2).run()
check("…and the walk clicks the button, not the caption beside it",
      _qrep.ok and _qa2.log == ["click:Save"] and _qw2.looks == 1, f"{_qa2.log} looks={_qw2.looks}")
runner_mod.query.available, runner_mod.query.find = _real_q2

# --------------------------------------------------------------------------
print("\nopen: what a script may launch")
# --------------------------------------------------------------------------
import os                                                      # noqa: E402
import shutil                                                  # noqa: E402
from script.runner import DesktopActuator, app_target         # noqa: E402

_windir = (os.environ.get("WINDIR") or r"C:\Windows").lower()
check("a profile app resolves", bool(app_target("notepad")))
check("an allowlisted system program resolves by bare name, from the Windows folder",
      app_target("charmap").lower().endswith("charmap.exe")
      and app_target("charmap").lower().startswith(_windir), app_target("charmap"))
check("an allowlisted Windows app URI resolves to itself",
      app_target("ms-clock:") == "ms-clock:"
      and app_target("ms-settings:display") == "ms-settings:display")
check("any other ms-*: scheme is refused (ms-msdt: ran arbitrary commands)",
      not app_target("ms-msdt:/id PCWDiagnostic") and not app_target("ms-msdt:")
      and not app_target("ms-appinstaller:?source=https://x/y.msix"))
check("shells and script hosts are never launched by name",
      not any(app_target(x) for x in ("cmd", "powershell", "pwsh.exe", "mshta",
                                      "wscript", "rundll32")))
check("a real program that is not on the list does not resolve (mstsc)",
      not app_target("mstsc") and not app_target("msconfig"))
check("anything else - a path, a made-up name - does not resolve",
      not app_target(r"C:\Windows\System32\cmd.exe") and not app_target("no-such-app-xyz")
      and not app_target("ms-clock: & calc"))


def _no_which(*a, **k):
    raise AssertionError("shutil.which consulted")


_real_which = shutil.which
shutil.which = _no_which
try:
    _w1 = app_target("charmap")
    _w2 = app_target("mstsc") or app_target("no-such-app-xyz") or app_target("calc.exe")
    check("PATH is never searched for a bare name (the current directory comes first on it)",
          _w1.lower().endswith("charmap.exe"), f"{_w1!r} {_w2!r}")
    _al = DesktopActuator._alias("notepad.exe")
    check("a moved Store app's alias is taken from its own folder, not PATH",
          _al.lower().endswith("notepad.exe") and not DesktopActuator._alias("cmd.exe")
          and not DesktopActuator._alias("../evil.exe"), _al)
except AssertionError as exc:
    check("PATH is never searched for a bare name", False, str(exc))
finally:
    shutil.which = _real_which

import core.machine_profile as _mp                             # noqa: E402
_real_profile = _mp.load_profile
_mp.load_profile = lambda refresh=False: {"symbols": {
    "APP:terminal": "C:/Windows/System32/wt.exe",
    "APP:chrome": "C:/Program Files/Google/Chrome/Application/chrome.exe"}}
try:
    check("a profile entry that is a shell host is refused too (defence in depth)",
          not app_target("terminal") and app_target("chrome").endswith("chrome.exe"))
finally:
    _mp.load_profile = _real_profile

# --------------------------------------------------------------------------
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print(f"\n{passed}/{total} passed")
for name, ok, detail in results:
    if not ok:
        print(f"  FAILED: {name} — {detail}")
sys.exit(0 if passed == total else 1)
