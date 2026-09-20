# Desktop Agent — purpose, systems, and what is left

Current as of the session that finished phases 00–06.
This is the working reference: what the product is for, what exists, how the
pieces fit, and what is still missing.

---

## 1. Purpose

Stop doing repetitive computer work by hand.

You describe a job in plain language. The app carries it out on this PC using
the same applications and websites you would have used. When a job is worth
repeating it can be saved and replayed. Over time it builds a library of work
it knows how to do, which can be shared and adapted to other machines.

**Not a script runner.** Scripts break when a window moves. This drives the
real UI, verifies each step actually worked, and repairs itself when an
application changes.

### The three parts

| part | responsibility |
|---|---|
| **Brain** — Claude, in-process via the Agent SDK | understands the task, chooses the steps, recovers from surprises |
| **Hands** — the engine in `core/` | finds any control by name, clicks, types, never hits the wrong window, yields to the user |
| **Memory** — `store.py` + `replay.py` | records a job, replays it anywhere, repairs it as apps drift |

Macros are an **accelerator for repeated work, not the product**. Most tasks
never need one.

---

## 2. Systems

### Application
| file | role |
|---|---|
| `app.py` | the standalone app: native window, Claude session, settings, task feed |
| `ui/app.html` | the interface — task box, live progress, saved jobs, settings |
| `tools_sdk.py` | the 40 in-process tools Claude drives, plus its operating instructions |

One process. No server, no port, no browser. Tools run in-process, so there is
no MCP subprocess and no IPC.

### Engine (`core/`)
| module | role |
|---|---|
| `locator.py` | tiered element resolution: Win32 → UIA → OCR labels → vision → container offset |
| `actuator.py` | clicking and typing. Invoke first (no cursor theft, works on background windows), injection as fallback |
| `act.py` | atomic resolve → act → verify, so a coordinate never crosses a turn boundary |
| `arbiter.py` | who may drive: cross-process lock, user-yield detection, ESC+ESC abort |
| `input_monitor.py` | one shared low-level hook; separates physical input from injected |
| `display.py` | wake-lock, lock/asleep detection, blank-frame guard |
| `browser.py` | Playwright on its own thread; indexed page, batched actions, remembered selectors |
| `recorder.py` | captures a *human* demonstration as events + change-deduplicated frames |
| `replay.py` | runs a job, verifies each step, improvises through a bounded ladder |
| `journal.py` | records the *agent's own* acting tool calls; a saved journal is redone by calling the same tools again (the `replay` tool) |
| `screencast.py` | optional low-size screen video, ~15 fps, downscaled, OpenCV mp4v (MJPG/.avi fallback) |
| `overlay.py` | a top-most, click-through orange border around the whole screen while the agent is working (region-shaped layered window) |
| `store.py` | macro storage, split shareable / local |
| `schema.py` | the portable job format and trust ladder |
| `machine_profile.py` | symbol resolution so jobs work on any PC |
| `retrieval.py` | finding a saved job from a description |
| `schedule.py` | running work on a cadence |
| `induce.py` | a demonstration becomes a draft job, for confirmation |
| `paths.py` | resources vs user data — the difference a frozen build needs |
| `noconsole.py` | stops a windowed build flashing cmd on every subprocess |
| `secrets.py` | DPAPI credential vault; `secret://` refs; redaction |
| `channel.py` | reaching you when you are away — app, Telegram, mail |
| `watchdog.py` | notices a stalled task and pushes it to continue |

### Legacy, superseded
`daemon.py`, `ui/index.html`, `agent_mcp.py` — the earlier server-plus-browser
approach and the external MCP server. Kept for reference; **not part of the
product**. The repo-root `controller.py` / `ui_parser.py` / `mcp_server.py` are
the original project and are only used for the OCR model loading.

---

## 3. Principles, and what each cost to learn

**Never store a coordinate.** A pixel position dies when a window moves, a
theme changes, or the job runs on another screen. Jobs store identity — "the
button named Save" — resolved fresh each time. This is what makes a job
portable.

**Ask the system, don't look at the screen.** Measured on 32 targets: UI
Automation resolved 100% in 0.52s; the vision pipeline managed 44% in 84.24s,
and 0% on icons, canvas-drawn controls and anything under 26px. Screenshots
are for confirming a result, never for deciding where to click.

**Click without touching the mouse.** UIA Invoke works on background windows
and leaves the cursor alone. Coordinate clicking on a covered window does not
fail safely — it clicks whatever is on top, i.e. the wrong application.

**Verify every step.** A job without verification is a script that fails
silently three steps later.

**The user always wins.** Physical input pauses the run; switching windows
ends it; physical ESC+ESC aborts it. Injected keys are distinguished from real
ones, so the agent cannot press its own kill switch.

**Fail loudly, never plausibly.** A blank screen must not read as "nothing on
screen"; a locked session must not read as text. Both were real bugs.

---

## 4. What works today

`python tests/run_all.py` runs everything — **799/799 across 16 suites
in ~150s** (2026-09-09), each suite in its own process so one teardown cannot
fake the next one's setup.

| suite | result |
|---|---|
| `test_phase01` — find and click any control | 33/33 |
| `test_phase02` — arbitration, kill switch, motion-vs-intent | 38/38 |
| `test_phase04` — portability, trust ladder, locator learning | 52/52 |
| `test_phase05` — vault, channel, questions, pause, cancellation | 80/80 |
| `test_phase08` — finding a job by describing it | 38/38 |
| `test_lessons` — task memory: save on struggle, recall next time | 48/48 |
| `test_clipboard` — paste a screenshot into a web app | 13/13 |
| `test_desktop_layer` — layout, names, walk, capture, maximise, waiting, batching, pointer paths, colours + mosaic, edge tracing, stroke-set replay, stalled-tool recovery, action journal + screen recording | 171/171 |
| `test_phase09` — scheduling | 53/53 |
| `test_phase10` — demonstration to job, editing a job | 49/49 |
| `test_stop_during_question` — Stop and close while asking; rename; icon | 66/66 |
| `test_labelling` — naming unnamed controls | 18/18, zero mis-assignments |
| `test_browser` — structure, tabs, downloads, uploads, sessions | 39/39 |
| `test_phase11` — indexed page, batching, memory, async layer | 50/50 |
| `test_phase12` — recording the browser, and replaying it | 31/31 |
| `test_endtoend` — drive a real application, incl. a six-step `do_steps` batch and a `mouse_path` drag-select | 20/20 |

`test_lessons`, `test_clipboard`, `test_desktop_layer` and
`test_stop_during_question` never move the mouse or take the user's focus
(the windows they need, they launch themselves), so they run while the
machine is in use — which is when a regression in them is most likely to be
noticed. `test_browser` has one known timing flake ("popup became the active
tab"); it passes on a direct re-run.

Capabilities: launch apps, enumerate controls by name in ~0.1–0.5s, click,
type, press keys, read text off unnamed controls, browse and act on web pages,
record and replay jobs, log in without ever seeing the password, ask you a
question mid-task and wait, refuse to act while the user is working, abort on
ESC+ESC, notice and nudge a stalled task.

---

## 4b. What the first real task taught us (2026-09-08)

The app was given a real job — find a watch on Trendyol, screenshot it with the
price, send it to a WhatsApp group with a message — and it failed after 7m13s.
Everything below was found by that one run and the review that followed. It is
recorded because each item was invisible from the tests: every suite was green
while the app could not do the task.

**It died of its own screenshot.** `screenshot` saved a JPEG and told the model
to open it with the Read tool. A full-screen read came back as ~1MB of base64
and the Agent SDK caps one message at 1MB: *"JSON message exceeded maximum
buffer size"*. The tool now returns the picture inline, downscaled to ~200KB,
and still saves the full-size file for sending. `max_buffer_size` is raised to
64MB as a backstop.

**A web page looked empty.** `walk` stopped at depth 14; page content sits
20–30 levels below a browser window, so `find_controls` listed the toolbar and
nothing else, and the model fell back to screenshots and PIL cropping. Depth is
now 40 — but raising it alone would have been a regression, because the walk
was depth-first: on a busy page the node budget was spent inside the document
and the browser's own toolbar was never reached. **The walk is breadth-first
now**, so a truncated walk drops the deepest nodes rather than whole shallow
branches, and it reports when it was cut short instead of implying the control
does not exist.

**The keyboard was wrong, in a way only this machine shows.** `VkKeyScanW`
returns the key in the low byte and the shift state in the high byte; the high
byte was masked off. On the Turkish Q layout that means:

| wanted | actually sent |
|---|---|
| `/` (shift+7) | `7` |
| `@` (ctrl+alt+Q) | `q` |
| `+` (shift+4) | `4` |

So `ctrl+alt+/` — WhatsApp Web's search shortcut — went out as `ctrl+alt+7` and
did nothing, which is what sent the run hunting for another way in. Modifiers
are now derived from the shift state, a character the layout cannot produce is
refused rather than sent as key 0xFF, and `\n` / `\t` in `type_text` are real
Enter and Tab presses (a newline used to be dropped, so every URL cost a second
call to press Enter). Characters above U+FFFF go out as surrogate pairs, so an
emoji is no longer mangled.

**A name that was listed could not be clicked.** Names were compared exactly and
case-sensitively, while web pages pad them with non-breaking spaces. A control
printed a moment earlier by `find_controls` resolved to "no locator resolved".
Comparison now folds case and collapses whitespace.

**A click could land in another window.** With no `window=`, resolution started
at the desktop root, so a name as ordinary as "Send" could match inside a
different application — including this app's own window — and clicking it
reported success. It now defaults to the foreground window, and a named window
that is not open is an error rather than a silent desktop-wide search.

**Every task began by searching for the tools it already had.** Tool search
defers the tool list behind a lookup the model must call first. That pays off
with hundreds of tools; there are 34 here. Measured on "call machine_info and
report the screen size": 4 turns / 22.4s with it on, 2 turns / 5.2s with it
off. The failed run spent four such round trips. `ENABLE_TOOL_SEARCH=false` is
now passed to the CLI.

**The window stopped updating halfway through.** The UI re-rendered only when
the feed's length changed, and the app sends a fixed window of the last 120
lines — so past 120 lines the length never changed again and the display froze.
That run produced over 275. Lines now carry a sequence number.

**One screenshot stopped the PC from ever sleeping.** `assert_awake()` called
`keep_awake()`, which sets a *persistent* no-sleep state, and that state belongs
to the thread that set it — the long-lived tools worker. Nothing could release
it, so the user's display and screensaver stayed disabled for as long as the app
was open. Reading pixels now nudges the idle timers once instead of holding
them; the persistent hold stays with `KeepSessionOpen`, which is task-scoped.

**Sending a picture had no good path at all.** Attaching through a web app meant
driving a native file dialog in the user's own language. There is now a
`copy_to_clipboard` tool: screenshot, put it on the clipboard, click the message
box, Ctrl+V. `core/clipboard.py`, 13 checks.

### And the thing that stops it happening twice

`core/lessons.py` — task memory. When the model struggles with something and
then gets it working, it calls `save_lesson` with the problem and the fix. At
the start of every task the relevant lessons are recalled and placed **above**
the task text, so applying them costs nothing. Retrieval reuses the job
library's scorer. Two lessons from this very run are already stored: how to
reach a chat in WhatsApp Web, and that Trendyol blocks the agent's browser so
the user's own Chrome must be driven instead.

---

## 5. Unfinished

### Phase 05 — Credentials and reaching you
**Done.** `core/secrets.py` + `core/channel.py`.
- DPAPI vault. No master password; Windows ties the ciphertext to your login,
  so a copied vault file is unreadable elsewhere — exactly what a shared macro
  needs. Entropy binds each blob to its own name, so a relabelled ciphertext
  will not decrypt.
- `secret://name` references resolved at the moment of use. A job file carries
  the reference, never the value; publishing one cannot leak a credential.
- `use_credential` types the value straight from the vault to the keyboard.
  **The model never sees it**, so it cannot land in a transcript. Every
  outbound message passes through `redact()` as a second line of defence.
- Telegram bot with a pairing link and inline Approve / Deny. Pairing is not
  optional: a bot cannot DM a username, so it has no chat id until you press
  Start. The channel reports "unreachable" rather than pretending.
- Mail fallback, send-only.
- `ask_user` / `notify_user`: a task can pause for a decision and resume.
  Silence is never read as approval; a typed password is accepted from the app
  but **not** over Telegram, which would leave it in a chat log forever.
- Wired into the app: a blocked task raises the window and shows the question
  above the composer (not in the feed, where it could scroll out of sight);
  Settings has a credential editor and the Telegram token / pair / test flow.
  If nobody answers in the window it falls through to Telegram rather than
  hanging.
- `Arbiter.rebaseline()`: answering our own question is a physical click on a
  window that just took focus — the two signals that mean "the user took over".
  Without this the run stopped the instant you clicked Approve.

### Phase 06 — Browser
**Done, 35/35.** Navigation, structure reading, clicking, filling, plus:
- **Per-site learning persisted** in `browser_sites.json`. A site that blocks
  headless is remembered, so the next run goes straight to a visible window
  instead of repeating the same failed attempt — and a site that starts working
  headless again is demoted back, since bot walls get fixed.
- **Downloads.** `web_download` rather than `web_click`, because Playwright
  discards a download unless the event is awaited: a plain click on a download
  link *appears to succeed* and silently loses the file. `download_url` fetches
  through the browser's cookies, which is the only way to get a file that sits
  behind a login.
- **Uploads.** Sets a real `<input type=file>` directly; falls back to
  intercepting the file chooser when the page uses a styled button, so no
  native Windows dialog has to be driven.
- **Tabs.** A click that opens a tab switches to it automatically — acting on
  the old tab afterwards is a classic and silent failure.
- **`web_session`** reports which hosts the profile holds a session for, so a
  task checks before trying to log in. Reported as "has a session", never
  "logged in": a cookie can be dead server-side.

Two real bugs found by writing the tests:
1. A popup starts at `about:blank` and navigates a moment later. Switching to
   it immediately meant acting on a blank page; the wait is now deferred to the
   next operation rather than done in the event handler, which would deadlock
   the sync driver.
2. Chromium opens *and instantly closes* a page for a blocked `target=_blank`.
   The agent was left holding a dead page, and every later action failed with a
   closed-page error that said nothing about the cause.

**Still missing:** recording web actions into a job (the desktop half records;
the web half does not).

### Phase 07 — App
**Mostly done.** Window, task box, live feed, settings, credentials, Telegram
setup, plus:
- **Comes to the front when a task finishes**, and flashes in the taskbar.
- **A status strip showing the tool running right now and for how long**, going
  amber past 20 seconds. "It stuck and not continuing" was usually a slow step
  with nothing on screen to say so — ambiguity, not a hang.
- **Pause / Resume.** The wait sits inside the tools, not between model turns,
  so pausing stops the *next action* rather than taking effect several
  keystrokes later. It blocks rather than returning an error, because an error
  makes the model improvise around the pause instead of holding still. Stop
  releases a pause first — otherwise a paused task never reaches the cancel
  check and Stop does nothing.

- **Editing a saved job**: `edit_job` shows, renames, re-describes, removes a
  step or deletes. Deliberately coarse — hand-editing a locator is how a
  working job becomes a broken one that still looks right. Removing a step
  resets that job's locator statistics, because they are keyed by step index
  and would otherwise describe the wrong steps.
- **Packaged as `dist/DesktopAgent.exe`** — one file, 121 MB, no Python needed.
  `python agent/build_exe.py`.

### Phase 10 — A demonstration becomes a job
**Done, 48/48.** `core/induce.py` + `record_job` + `edit_job`.
- record → **confirm** → save. The middle step is not optional and the system
  prompt forbids skipping it: a job induced from a misread demonstration
  replays cleanly and does the wrong thing every time it runs, which is far
  worse than asking one question.
- The draft is written as English sentences, not JSON. A user can check
  "click 'Export', type the filename, press ctrl+s"; nobody can check a
  locator array.
- Uncertainty is surfaced rather than smoothed over: a click on a control with
  no name is kept but flagged inline with why, so the confirmation prompt
  draws the eye to the step most likely to be wrong.
- Noise is dropped and said out loud — clicks on our own window, double clicks
  registered as two, and scrolling (replay resolves by identity and scrolls the
  element into view itself, so replaying the scroll would fight it).
- **A demonstrated password is caught before saving.** Recording captures
  keystrokes verbatim, so demonstrating a login writes the password into a file
  meant to be shareable. It is flagged with the fix — store it and reference
  `secret://name` — and ordinary prose is not flagged, because crying wolf
  would train the user to ignore it.
- Trust starts at DRAFT however clean the recording looked. A demonstration is
  not a run.

### Phase 11 — The browser, made fast for repeated work
**Done, 50/50.** `snapshot` / `render` / `act` / `run` in `core/browser.py`,
plus `web_page`, `web_do`, `web_learned`.

The old path had two costs and only one of them was visible.

**The page as a numbered list.** `web_page` stamps every interactive element
with an index and renders one line per element:

```
[1]<input search>Search Wikipedia
[2]<button>Search
[8]<select value="Basic" options=Basic|Pro>Plan
```

An index resolves to exactly one node; a name has to be searched for and can
match several, which is how the wrong button gets clicked. Measured on a real
Wikipedia page: 40 elements in **917 characters** — the same page as JSON was
3.6× larger, and every token there is latency on every turn.

**One call, many actions.** `web_do` takes a list of steps. The browser work
was never the problem — filling six fields is ~120ms — but as six separate
tool calls it is *six model round trips*, and `§6` already identified that as
the dominant cost of everything. As one batch it is one. On failure it stops
at that step, says which, and returns a fresh page listing, so recovery costs
one more turn rather than starting over.

**It remembers what worked.** Every step carries a short `label`. When a target
resolves, the selector that found it is stored per site in
`browser_flows.json`, preferring what survives a re-render — a test id, then an
id, then a name attribute. The second run of the same task skips the search
entirely:

| | first run | second run |
|---|---|---|
| local checkout form, 3 actions | 191ms, searched | **83ms, all `via remembered`** |
| Wikipedia search, real site | 1.44s + a snapshot | **1.05s, no snapshot at all** |

A remembered selector that stops working is **dropped after two misses**, not
retried forever — a fast path that is confidently wrong is worse than a slow
one that is right. Between those, a stale selector falls through to the search
rather than failing the step.

`web_elements` / `web_click` / `web_fill` still work for a single one-off
action; the system prompt now points at `web_page` → `web_do` for anything
longer than one click.

**The browser had never worked inside the app.** Every tool handler is a
coroutine, and Playwright's sync API refuses to start inside a running asyncio
loop. So the browser passed 35 tests — all of which call `Browser` directly and
synchronously — and failed every single time it was reached for real. A live
task diagnosed it better than the test suite did: *"the MCP browser backend is
broken (async-loop bug)"*, after which the agent installed Playwright itself
and drove Chromium from Bash instead.

The fix is `ThreadedBrowser`: the shared instance runs on a thread of its own,
with no event loop, and every method proxies onto it. Playwright objects are
thread-affine, so all calls must land on the same thread — which is why it
proxies everything rather than only what happened to fail. `test_phase11 §11`
now drives the tools through a real `asyncio.run`, which is the layer that was
never covered.

**Measured on one real task**, "search Wikipedia for Ada Lovelace and report
two facts from the article":

| | tool calls | time |
|---|---|---|
| before — browser broken, agent worked around it via Bash | 13 | 92s |
| async fix | 5 | 51s |
| `web_open` reports targets it already knows on this host | 5 | 20s |
| `run()` settles navigation before reporting the URL | **4** | **17s** |

Each of the last two removed a whole round trip for a specific reason. The
known-targets line lets a repeat visit skip `web_page` entirely — the model's
own words on the second run were *"the search box is already a known target,
so I can search directly"*. Settling navigation stops `web_do` reporting the
pre-click URL, which had been making the caller spend another call checking
whether anything had happened.

Two smaller things this surfaced:
1. `_host()` only matched `http(s)`, so every `file://` page shared one empty
   bucket — local pages would have corrupted each other's cache.
2. An unknown action reported "no element for 0", blaming the target for a
   misspelled verb. Verbs are now validated before anything is resolved.

### Packaging
**Done.** `agent/build_exe.py` → `dist/DesktopAgent.exe`, 121 MB, one file.
- **Still needs Claude Code installed and signed in.** The Agent SDK does not
  embed a model client; it drives `claude.exe` as a subprocess. Bundling that
  is not an option — separate product, own auth and update path — so the app
  detects it at startup and says what to install rather than dying in an
  unreadable subprocess error.
- **The OCR tier is deliberately excluded**: torch + easyocr + ultralytics is
  over 2GB, for a fallback that is off by default. Bundling it would multiply
  the download roughly fiftyfold to serve a path most tasks never reach.
- `core/paths.py` splits resources from user data. A one-file build extracts to
  a temp directory and **deletes it on exit** — without this split the vault,
  macro library, schedules and browser logins would appear to work and vanish
  the moment the app closed. Data lives in `%LOCALAPPDATA%\DesktopAgent`.
- `core/noconsole.py` stops the cmd windows. A windowed build has no console,
  so Windows gives every console child a **new visible one** — the Claude CLI
  on every task, PowerShell, the browser driver. Our own call sites pass the
  flag; the ones we do not own (the SDK, Playwright) are caught by a narrow
  patch on `subprocess.Popen`, active only when there is genuinely no console
  to inherit. Measured after the fix: **0 console windows** during a full task
  run.

### Phase 08 — Finding a job by describing it
**Done, 38/38.** `core/retrieval.py` + the `find_job` tool.
- Scores a description against name, description, tags, apps and step intents,
  IDF-weighted over the library — so a word in every job stops being evidence
  while a word in one is decisive.
- **Embeddings were rejected, not skipped.** They mean a ~90MB download on
  first use, and this project has already been bitten by a tier that silently
  pulled 1.2GB. A library of tens-to-hundreds of jobs scores exhaustively in
  microseconds, so a vector index buys nothing. `EMBEDDINGS = False` marks
  where that tier goes if the library ever outgrows this.
- What lexical matching genuinely cannot do is connect "stock market" to
  "borsa". So `find_job` returns **candidates with scores and match reasons**
  and lets Claude judge — which is the half of the problem it is actually good
  at. A similarity threshold would just be guessing.
- Fuzzy matching is calibrated against measurements, not hope: bigram overlap
  scored `excel`/`exel` at 0.57, below any threshold that also excludes
  `excel`/`export`. `SequenceMatcher` puts real typos at 0.80–0.92 and
  unrelated words at 0.36–0.50, with nothing in between.
- Turkish folds onto ASCII before matching, including the İ/ı cases that
  `str.lower()` does not round-trip, so a Turkish query finds a Turkish job.

### Phase 09 — Scheduling
**Done, 53/53.** `core/schedule.py` + the `schedule_task` tool + a Schedule tab.
- Plain language in: "every morning at 9", "weekdays at 8am", "every 3 hours",
  "monday and thursday at 7pm", "her sabah", "cuma 14:00". Anything it cannot
  read is **refused**, never guessed — a misparsed schedule fires at the wrong
  hour every day and nobody traces it back.
- **Not the Windows Task Scheduler**, deliberately. A task launched by the
  service can land in a session with no desktop at all, and this agent exists
  to drive a desktop: it would fire, find no windows, and report success on
  having done nothing.
- The cost of owning the timer is that nothing fires while the app is closed.
  That is made visible rather than hidden: what came due while it was shut is
  **reported on startup, not run**. Silently executing six hours of missed work
  the instant the window opens is not what anyone means by "every morning".
- A busy machine defers rather than drops — two tasks driving one desktop is
  exactly what the arbiter exists to prevent, so the occurrence is re-offered
  on the next tick instead of being lost.

### Cross-cutting, not yet addressed
- **Speed.** The dominant cost is one model round trip per tool call; see §6.
- **Scheduling.** No "every morning at 9". The watchdog handles stalls, not cadence.
- **Sharing.** The format is ready and leak-checked; there is no library to publish to.
- **Excel and other nag-dialog apps.** An unlicensed Office shows a modal on
  most actions; each task currently rediscovers this.
- **Recording → job.** The recorder captures; turning a recording into a saved
  job still needs the intent-confirmation step from the plan.

---

### Foreground: the unattended case was broken

`focus_window` attached to the foreground thread's input queue, which is the
usual answer and is **not sufficient**. Windows has a second gate — the
foreground *lock timeout* — and it is precisely the unattended case that trips
it: a process nobody has interacted with, several levels down a spawn tree, is
refused outright.

It showed up as a test that passed run from a shell and failed run as a
grandchild of the test runner, the same five checks, every time, on the same
machine. That is not a test artifact. It is the product's whole target case:
an agent driving the desktop while the user is elsewhere is exactly a process
nobody has interacted with.

`focus_window` now escalates, verifying after each step:

1. attach and raise — enough when we already have some claim
2. synthetic ALT press, then retry — Windows counts a key event as evidence
   this process is being interacted with. Injected, so the arbiter does not
   read it as the user taking over.
3. drop `SPI_SETFOREGROUNDLOCKTIMEOUT`, retry, **restore it immediately** —
   leaving it at zero would let any process steal focus from the user
4. minimise and restore — a window coming back from minimised is granted focus

It still returns False if none worked, and the caller still refuses to inject
in that case: a coordinate click into a window that never came forward does
not fail safely, it clicks whatever is on top instead.

### Tests no longer damage the desktop they run on

Three separate rules were closing windows that were never ours:

* **"Home - File Explorer"** — justified when the end-to-end suite drove
  Notepad, which spawned one per launch. That fixture was replaced; the rule
  was not, so only the user's own Explorer windows were left to hit.
* **"Calculator"** — `test_phase01` *reuses* an already-open one rather than
  launching its own, so the sweep closed the user's.
* **`explorer.exe` launched unconditionally on every run** — the actual source
  of the litter, which had been misattributed to Notepad. Now only launched
  when there is no Explorer window to measure against.

Each suite snapshots the windows on screen before it starts and closes only
what it opened. `cleanup.py` is narrowed to names no human would have on
screen — `BENCH TARGET`, `E2E TARGET`, and the suite's own `desktopagent*`
marker.

`run_all` also refuses to start the desktop-driving suites while the machine
is in use, because under contention they do not fail cleanly — they report
"app logged None" and "could not foreground window", which read as code
regressions. `--force` overrides. The first version of that check treated a
bare `%` as progress and flagged a Chrome tab showing a stock price
("SILVER 69,4250 +2.01%") as a running job; it now requires the verb.

---

## 6. Why it feels slow, and what would fix it

Honest accounting, largest cost first.

| cause | cost | fixable? |
|---|---|---|
| One model round trip per tool call | seconds each; dominates everything | **partly fixed** — `web_do` batches a whole sequence into one |
| Arbiter settle on any user input | up to 5.8s **per action** while you watch | **fixed** — 31ms now, measured |
| UIA cache invalidated on every lookup | full tree rebuild per call | yes |
| `screenshot` + reading the image back | large payload, slow | yes — prefer `find_controls` |
| Old MCP server being reachable | 84s per call when chosen | **fixed** — `strict_mcp_config` |
| OCR labelling | ~15s | already opt-in only |

The arbiter one mattered most for perceived speed: sitting and watching means
moving the mouse, and every mouse movement triggered a pause plus an escalating
re-check. That is correct behaviour when you are *using* the machine and wrong
when you are merely *watching* it. Movement and intent are now tracked
separately — a click, a keystroke or a scroll still stops the run immediately;
bare cursor movement does not. `test_phase02` pins down **both** halves, since
losing the first makes it slow again and losing the second makes it drive over
you.

---

## 7. Open questions

1. Should the app itself hold the conversation (follow-up questions, "no, do it
   this way") or stay one-shot per task?
2. How much should it narrate? Every tool call is visible now, which is good
   for trust and noisy for routine work.
3. Excel specifically — worth special handling, or avoid it?
4. Packaging — is "runs from Python" acceptable, or is a real installer needed?
