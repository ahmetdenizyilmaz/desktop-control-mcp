# The script language

## Observed stages across sites

For an unfamiliar page, use `observe` instead of guessing all its controls:

```json
{
  "name": "example/transfer-reference",
  "steps": [
    {"do":"open","url":"https://first.example","reuse":false},
    {"do":"observe","goal":"Reveal the reference","expect":{"id":"reference"},"rounds":4},
    {"do":"capture","target":{"id":"reference"},"save_as":"reference"},
    {"do":"open","url":"https://second.example","reuse":false},
    {"do":"observe","goal":"Enter reference {reference} and check it","expect":"Reference verified","rounds":4}
  ]
}
```

These are illustrative sites/selectors. Completion selectors must identify the
actual outcome, not a permanent header. Each round sends fresh elements and a
screenshot to the configured helper, together with execution history and captured
evidence. Generated batches are validated completely before execution. The runner
checks completion itself after each batch, saving a model call just to say done.
Existing commit and foreground checks remain in force.

`capture` stores the selected element's name, source, window and timestamp in
`report.observations`; later `{reference}` substitutions use that observed text.
Top-level captures must precede use and cannot overwrite input parameters. Pass
values into called scripts explicitly with `with`. Model-generated values are
not treated as captured evidence. The app logs captures and checkpoints when
full logging is enabled.

State persists across stages **within a run**. Automatic restart recovery of these
stages, named browser-tab sessions and function return maps are not implemented;
the existing restart mechanism skips completed repeat items only. Screenshots/UIA
remain the production observation path. The opt-in `tests/live_observe.py` uses a
test DOM adapter with real Codex and headed Chrome against two local origins.

Run deterministic regressions with `python tests/test_observe.py` from `pipeline/`.
Run the live test from `agent/` with
`../.venv/Scripts/python.exe ../pipeline/tests/live_observe.py`.
Prompts, replies, screenshots and timing go to `agent/workspace/observe-test/`.

A script is a JSON file listing steps. It is **data, never code** — which
is the decision everything else follows from, because scripts are loaded
from a server or a git repo, and a step list can be checked before
anything touches the mouse where a fetched Python file cannot.

```json
{
  "name": "migros/search",
  "params": {"product": ""},
  "steps": [
    {"do": "click", "target": {"id": "product-search-combobox--trigger"}},
    {"do": "key",  "keys": "ctrl+a"},
    {"do": "type", "value": "{product}", "then": "enter"},
    {"do": "wait_for", "target": {"text": "{product}", "area": "content"},
                       "timeout": 25}
  ]
}
```

Run it:

```
python -m script check migros/weekly-shop     # resolve, verify, touch nothing
python -m script run   migros/weekly-shop
python -m script run   migros/search --set product=süt
```

or from Python:

```python
from script import Library, Runner, DesktopActuator
from vision.elements import Watcher

lib = Library()
run = lib.resolve("migros/weekly-shop")          # verifies everything first
report = Runner(run, Watcher(), DesktopActuator()).run()
print(report.summary())
```

**Measured, on a real Migros run:** ten products opened from a cold
browser, **10/10 in 77 s** — about 7 s per product, of which roughly 1.3 s
is one `look()`.

---

## What the runner can see

Three sources merge into one scene (`pipeline/vision/elements.py`). There
is **no HTML page source** — but UIA on a browser reads the accessibility
tree, which is DOM-derived, and that covers more than it sounds like:

| | sees | measured on a live page |
|---|---|---|
| **UIA** | roles, names, values, **DOM `id`**, **`href`**, and **content scrolled out of view** | 201 elements, 130 of them below the fold |
| **OCR** | any text drawn as pixels, including inside images | 95 elements, visible area only |
| **templates** | icons with no text, once taught | on demand |

**Scrolling mostly takes care of itself.** UIA reports elements that are
scrolled out of sight, so a word below the fold is still found by name or
id, and `Invoke` activates it without scrolling at all. What UIA reports
for those is their *virtual* coordinate, which can be past the bottom of
the monitor — so when a coordinate click is unavoidable the actuator
calls `ScrollIntoView` first and **refuses rather than clicking a
coordinate that is not on screen**. A blind click there lands on nothing,
or on another monitor, and reports success.

OCR-only content below the fold is genuinely invisible, so scroll for it:
`{"do": "scroll", "direction": "down", "amount": 5}`.

**What is NOT available:** raw HTML, CSS classes, `data-*` attributes,
computed styles, `display:none` elements. Getting those means real DOM
access — Chrome started with `--remote-debugging-port` and driven over
CDP, or Playwright's own browser (`agent/core/browser.py` does this, at
44 ms a snapshot). Neither is wired into this pipeline.

---

## Steps

| `do` | does | needs |
|---|---|---|
| `open` | launch an app and/or a URL | `app` and/or `url`; `window`, `timeout` |
| `focus` | bring a window to the front | `window`; `maximize` |
| `click` `double_click` `right_click` | press something | `target` |
| `type` | put text in a field | `value`; optional `into`, `then` |
| `key` | a key or combo | `keys` — `enter`, `ctrl+a` |
| `drag` | drag one thing onto another | `target`, `to` |
| `scroll` | scroll | optional `target`, `direction`, `amount` |
| `move` | hover | `target` |
| `wait` | sleep | `seconds` |
| `wait_for` | wait until something appears | `target`, `timeout` |
| `expect` / `gone` | assert present / absent | `target`, `timeout` |
| `if` | branch on what is on screen | `present`, `then`, `else` |
| `repeat` | loop | `times` **or** `for_each` + `as`, and `steps` |
| `call` | run another script | `script`, `with` |

Any step also takes:

- **`expect`** / **`gone`** — a post-condition checked against a *fresh*
  look after the step. This is the most valuable field in the language:
  a click that reported success and changed nothing is how this project
  once claimed to have bought milk that was never in the basket. A step
  whose post-condition fails **is a failed step.**

  **Pick evidence that is caused by the step.** The first version of
  `migros/add-product` expected `"Sepetim"` — which is a permanent header
  button, so it was on the page whether or not anything had been added.
  Vacuous, and vacuous in exactly the way the field exists to prevent. The
  real evidence was the quantity stepper: it *replaces* a card's add
  button, and only for a card that actually went into the basket.
- **`optional: true`** — this step failing does not abandon the iteration.
  For the cookie banner that may or may not be there.
- **`note`** — why the step is written the way it is. Ignored at runtime.

---

## Getting to the thing: `open` and `focus`

```json
{"do": "open", "app": "chrome", "url": "https://www.migros.com.tr",
 "window": "Migros", "timeout": 60}
{"do": "focus", "window": "Migros", "maximize": true}
```

**What `app` may name** — three allowlists, and nothing outside them:

| form | examples | resolved how |
|---|---|---|
| a machine-profile name | `chrome`, `edge`, `notepad`, `paint`, `calculator`, `spotify`, `teams` | the profile's path or Start-menu id — the same table the agent's `open_app` uses |
| a Windows app URI | `ms-clock:`, `ms-settings:`, `ms-settings:display`, `ms-calculator:`, `ms-paint:`, `ms-photos:` | opened as-is. **Only these schemes**: `ms-msdt:` ran arbitrary commands and `ms-appinstaller:` installs packages, so "any `ms-*:`" is not a category |
| a bare system program | `charmap`, `mspaint`, `notepad`, `calc`, `explorer`, `control`, `magnify`, `osk`, `snippingtool`, `write`, `msinfo32`, `winver` | `<name>.exe` from `%WINDIR%\System32`, `%WINDIR%` or `SysWOW64` — **never from PATH**, which on Windows starts with the current directory, so a `notepad.exe` dropped next to a script would be the one started |

Shells and script hosts (`cmd`, `powershell`, `wscript`, `mshta`,
`rundll32`, `wt`…) are refused whatever names them, including a profile
entry. A `url` with no `app` goes to the default browser. Only `http` and
`https` are allowed — every other scheme is a registered protocol handler
that can launch an arbitrary program, which is too large a door for a file
that may itself have come from a URL.

**Whose window is it.** The rule the runner enforces is *only touch
windows we opened*. Before a run starts it snapshots every top-level
window handle the person has — minimized, on another virtual desktop,
untitled, all of them — and those are the person's. (A window an app keeps
*hidden* is not: Explorer pre-creates its next window that way and a launch
merely shows it, so a hidden window that comes up after `open` is the run's.)
From then on:

- **`open` waits for a NEW window** — one that was not there before the
  launch — and drives that. A folder or a document the person already had
  open with a matching title is not used, even if nothing new appears: the
  step fails with *"…already open were not the run's to use"*. (Win11
  Notepad is the known case: it opens a tab in the window you already
  have, so `open notepad` on a desktop with Notepad up fails, correctly.)
- **The exceptions are apps whose one window IS the person's**: a browser
  (`chrome`, `edge`, `firefox`, `brave`, `opera` — their session, cookies
  and tabs), `spotify`, `teams`, `outlook`, `whatsapp`, `clock`, and
  `ms-settings:`. `open chrome, window: Wikipedia` reuses the Chrome window
  showing Wikipedia rather than starting a second browser. Reuse is decided
  by the **process behind the window**, not its title: a Notepad document
  called "Wikipedia notes" is not Chrome. `reuse: false` forces a new
  window even for these.
- **`open` with no `window`** waits for whatever window the launch creates
  (or, for a reusable app, that app's own window) — never simply "whatever
  is in front", because a slow launch leaves the person's window there.
- **`focus` follows the same rule.** It finds a window the run opened or
  was already allowed into; a window the person had before the run is
  taken only if it belongs to a reusable app, or the script itself says
  `"reuse": true` on the step. Steps a model proposes mid-run (a rescue,
  an `assist`) get no such say — `{"do": "focus", "window": "Notepad"}`
  was exactly what a rescue reached for after `open notepad` found no new
  window, and the person's Notepad got typed into.
- A window the run is driving that **replaces itself** (Explorer does this
  on the way to Control Panel) is followed to the window that took its
  place — only if that one is the run's too.

**`window` is the part that matters.** A launch returns the instant the
process starts, long before anything is on screen, so without it the next
step acts on the page you were trying to leave. Waiting on the window
*title* also waits for the page: Chrome's window exists in milliseconds,
but a window titled "Migros" means the site loaded.

Both steps then **re-point the scene at that window**. `look()` reads one
window's accessibility tree, so a script that opened Chrome and carried on
searching would still be reading the window it started in — finding
nothing, or worse, finding a stale match and clicking it.

An empty `window` (a `{title}` whose caller did not care) means *whatever
is in front*, never "the first window on the desktop" — a substring test
against `""` matches everything.

---

## Shortcuts are just scripts

There is no shortcut mechanism, because there does not need to be one.
`script/scripts/browser/` ships three:

| script | does |
|---|---|
| `browser/goto` | ctrl+L, type the URL, wait for the page |
| `browser/new-tab` | ctrl+T, then `browser/goto` |
| `browser/dismiss` | clear cookie banners and promo modals |

```json
{"do": "call", "script": "browser/goto",
 "with": {"url": "migros.com.tr", "title": "Migros"}}
```

`browser/goto` is three steps and shows why `type` takes no `into`:

```json
{"do": "key",  "keys": "ctrl+l"},
{"do": "type", "value": "{url}", "then": "enter"},
{"do": "focus", "window": "{title}", "timeout": 45}
```

ctrl+L already put the caret in the address bar, so naming it would be
noise *and* one more thing that can fail to resolve — and it is one
keystroke across Chrome, Edge and Firefox where finding the element would
be three selectors that each break on a redesign.

Add your own by dropping a JSON file in the folder. Since a function is a
script, your shortcut library and your script library are the same thing.

---

## Selectors — finding the thing

A selector is a string (shorthand for `{"text": ...}`) or an object:

```json
{"id": "product-search-combobox--trigger"}    the DOM id — prefer this
"Ara"                                         text, substring match
{"text": "Ara", "kind": "button"}             kind kills most ambiguity
{"text": "Sepete Ekle", "match": "exact"}     no substrings
{"any": ["Ara", "Search", "Git"]}             whichever is on screen
{"text": "Ara", "near": "Arama alanı"}        disambiguate by a neighbour
{"kind": "edit", "near": "Ara"}               no text at all — the box next to "Ara"
{"text": "Sepete Ekle", "nth": 1}             1-based, reading order
{"icon": "close_tab"}                         a template, for things with no text
{"text": "Kapat", "area": "content"}          the PAGE, not the app's own frame
```

**Use `id` whenever the page provides one.** UIA passes a web page's DOM
`id` attribute straight through as the automation id — 158 of them on a
Migros page — and it is the most stable handle there is: it survives a
redesign, a language change, and text that changes by itself.

That last case is not hypothetical. Migros's search box is named after a
*rotating advert*: it read `"Arama alanı"` one hour and `"Nutella Loves
Bread"` the next. It was the single most fragile thing in the original
run. Its id, `product-search-combobox--trigger`, has not moved:

```json
{"do": "click", "target": {"id": "product-search-combobox--trigger"}}
```

Rewriting the Migros scripts around ids took the ten-product run from
**9/10 in 92 s to 10/10 in 77 s**. An exact id match wins outright and
skips text scoring entirely; a partial id filters, and can be combined
with `text` to narrow further. Find the ids on any page with:

```python
for e in look(hwnd=h).elements:
    if e.automation_id and not e.automation_id.startswith("view_"):
        print(e.kind, e.automation_id, repr(e.name))
```

(`view_N` ids are Chrome's own controls, not the page's.)

**`area: "content"` is not cosmetic — use it for anything on a web page.**
A live Migros page offered 270 elements, of which the **first 44 were
Chrome's own**: tab strip, toolbar, bookmarks. Among them were *two*
buttons named "Kapat" — one closing the tab, one closing the browser. A
generic "dismiss the modal" selector matched both, and only escaped
closing the browser because two matches made it ambiguous. `area:
"content"` restricts matching to the window's document rectangle, which is
where the page lives. A window with no document falls back to the whole
window, so it never turns a working selector into a silent miss.

Multi-word text works as written: `elements.py` binds OCR lines to the
controls that contain them, so `"Sepete Ekle"` is one element's name, and
runs of whitespace are collapsed before comparison because OCR returns
`"Sepete  Ekle"` often enough to matter.

**`kind` is the cheapest disambiguator there is.** On a Turkish shopping
page, `"ara"` as a substring matches the search button, the search field's
label, a category filter and a truncated tooltip. Adding
`"kind": "button"` removes all but one, for free, because the merged scene
knows what is a button and OCR alone never could. Kinds come from
`vision.elements.KINDS`: `button`, `link`, `edit`, `combobox`, `checkbox`,
`radio`, `menuitem`, `tab`, `listitem`, `slider`, `treeitem`, plus `text`
and `icon`.

Prefer `near` over `nth`: an ordinal breaks the moment the page reflows, a
neighbour does not.

**Ambiguity stops the step.** If the runner-up scores within 0.08 of the
winner, the step fails and says so rather than picking. A resolver that
silently takes the first of five is how you click the wrong button and
never find out.

---

## Parameters and functions

`{name}` is substituted into text fields. That is the *whole* templating
language — no expressions, no arithmetic, no indexing. `{count + 1}` is
not supported and will not be: the moment a script needs arithmetic, JSON
is the wrong host and the work belongs in a model turn.

**A function is just a script, and calling one is a step.** There is no
separate concept, which also means the library of scripts you keep in a
folder or a repo *is* the function library:

```json
{"do": "call", "script": "migros/add-product", "with": {"product": "süt"}}
```

Ten products become one loop over one function:

```json
{"do": "repeat",
 "for_each": ["süt", "peynir", "yumurta", "ekmek", "makarna",
              "pirinç", "domates", "elma", "çay", "kâğıt havlu"],
 "as": "product",
 "steps": [{"do": "call", "script": "migros/add-product",
            "with": {"product": "{product}"}}]}
```

Three rules keep this from becoming an interpreter:

- **`for_each` takes a literal list or parameters** — never a query
  result, because that would need scoping rules, and then shadowing rules.
- **A called script sees only its `with:`.** No scope inheritance. Every
  parameter it declares must be passed, and passing one it does not
  declare is an error — both checked before the run starts.
- **Depth is capped at 8 and cycles are refused at load time.** A cycle
  found at run time is a loop that *clicks*, and by the time you notice it
  has already done something a hundred times.

---

## Failure: skip the item, not the step

The default is that **a failed step abandons the rest of its iteration**,
and the next iteration of a `repeat` starts fresh. Failures are collected
and reported at the end.

The naive reading — ignore the failure, run the next step — is actively
dangerous, and this exact script shows why. If `search for "{product}"`
fails and the runner carries on, the next step clicks the first result of
the *previous* product's search, which is still on screen, and adds the
wrong thing to the basket. Carrying on past a failed step does not do
nothing; it does something wrong, confidently, and reports success.

So ten products are attempted independently, each failure listed at the
end, and a failed one cannot corrupt the next:

```
migros/weekly-shop: 47 steps in 82.3s
  8/10 items completed
  missed: pirinç, çay
  FAILED migros/add-product[pirinç].steps[3] (wait_for): contains 'pirinç', kind=link did not appear within 15.0s
```

Use `"optional": true` for the narrow case the naive reading was reaching
for — a step whose failure genuinely does not matter.

---

## Where scripts come from

Local first: every `.json` under `script/scripts/`, keyed by the `name`
*inside* the file rather than by filename, so moving a file does not break
callers and two files claiming one name is an error rather than a coin
toss.

Remote scripts are **pinned by content hash**:

```json
{"do": "call", "script": "shared/login",
 "source": "https://raw.githubusercontent.com/you/scripts/main/login.json",
 "sha256": "3f1c…64 hex chars"}
```

A `source` without a `sha256` is refused at parse time. This is not
ceremony — it is the only thing that works:

- **HTTPS** proves you reached the right host, not that the host serves
  what it served yesterday.
- **A git URL** moves. `main` today is not `main` tomorrow, and nothing
  tells you it changed.
- **Reviewing it once** reviews the copy you fetched, not the next one.

The hash reduces *"trust this server, forever, for anything"* to *"trust
these exact bytes"* — the guarantee a commit id gives, and the only one
strong enough for something that drives a mouse. Content that does not
hash to its pin is refused with both hashes printed, including from the
local cache.

Good things that fall out of pinning: a pinned script is fetched **once**
and is a local file thereafter, so offline runs work; a changed remote is
a loud error at load time rather than a surprise mid-run; and updating a
pin is a visible edit to the calling script, which is where a change of
behaviour belongs.

To add one, fetch and read it first — deliberately two steps, because
computing the pin is the moment you are supposed to look at what you are
installing:

```python
from script import pin
print(pin("https://raw.githubusercontent.com/you/scripts/main/login.json"))
```

`Library(allow_fetch=True, allow_hosts=("raw.githubusercontent.com",))`
turns fetching on. It is **off by default**, so a missing pin is an error
rather than a download.

**Everything resolves before anything runs.** `resolve()` walks the whole
call graph — fetching, verifying, parsing, checking depth, cycles and
parameters — and only then does the runner touch the screen. A script
cannot fetch halfway through a checkout, and a network failure costs you
nothing instead of half a basket.

---

## Trust

`draft` → `supervised` → `trusted`, with `quarantined` below all three.

Two rules, and the second is the one that matters:

1. **An import is always `draft`**, whatever the file says about itself.
   Trust is local knowledge and does not travel inside a file.
2. **A run's trust is the minimum over its whole call chain.** Without
   this, trust launders: import an unreviewed script, have a trusted local
   one call it, and unreviewed steps run unattended.

---

## The commit guard

Before any click, the element's name, value and automation id are checked
against a word list (`guard.py`, English and Turkish): *öde, satın al,
onayla, sil, checkout, place order, delete, send…*. A match stops for a
human unless the run is `trusted` **and** imported nothing.

This check is in code, and no model participates in it. The reasoning is
jev-1.13's documented failure mode 6 — *state is data, and the model does
not treat it as hostile*. On a checkout page the merchant wrote the button
labels, so a model asked "is this safe to click?" is reading the
defendant's own testimony. A model may only ever **add** caution to this
answer, never remove it.

Wire a handler to be asked:

```python
Runner(run, watcher, actuator,
       confirm=lambda word, el: input(f"{el.name!r} ({word})? [y/N] ") == "y",
       should_stop=lambda: esc_pressed)
```

With no `confirm`, a flagged action is **not clicked** and the step fails.
A refused action fails the step too, rather than carrying on — a refused
checkout should not be followed by the steps that assume it happened.

`should_stop` is polled before every step, every retry and every loop
iteration. Any loop that can click has to honour a stop signal, or it is a
way to keep driving after the human has already said no.

---

## Testing

`python tests/test_script.py` — 213 checks, no screen, no network, no
clock beyond short sleeps. The scene is built by hand and the actuator is
a recorder, so a failure means the logic changed rather than that the
machine was busy.
