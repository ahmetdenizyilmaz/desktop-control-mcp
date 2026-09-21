# System-1 decisions: evaluating TypeSafe Jev for the desktop agent

Research note and design record. Branch `jev`. Nothing here is merged.

---

## 1. What Jev is

TypeSafe AI came out of stealth on 2026-09-15 with **Jev**, the first of what
they call **System One models**. It is not an LLM and generates no text. You
send a **state** (string or JSON) and a map of **typed questions**; it returns
one calibrated answer per question in a single parallel pass.

Three primitives, and that is the whole surface:

| primitive | asks | answer |
|---|---|---|
| **Noul** | a yes/no proposition | `noul` — P(yes), 0–1. The probability *is* the confidence |
| **Choice** | one of up to 255 named options | `choice`, `probabilities` for every option, `confidence` |
| **Score** | 2–10 *ordered, described* levels | `score` (probability-weighted mean), `probabilities`, `legend`, `confidence` |

```http
POST https://api.typesafe.ai/v1/systemone
Authorization: Bearer <key>

{"model": "jev-latest",
 "state": "Help! My payouts have been failing for 3 days.",
 "questions": {
   "is_urgent":  {"type": "noul",   "instructions": "Does this convey urgency?",
                  "criteria": {"true": "Explicitly time-sensitive",
                               "false": "No urgency expressed"}},
   "department": {"type": "choice", "instructions": "Which team handles this?",
                  "criteria": {"billing": "Payments, invoicing, refunds",
                               "technical": "Bugs, outages, integrations"}},
   "frustration":{"type": "score",  "instructions": "How frustrated are they?",
                  "criteria": ["Calm", "Frustrated", "Very angry"]}}}
```

| | |
|---|---|
| latency | 70–500 ms, typically ~100 ms; the Doom demo held **212 ms median over 2,642 calls** |
| price | **$0.042 / Mtok in, $0.00 out** — no generated text to charge for |
| per decision | ~$0.0004 typical; a 60-control window is ~$0.000035 |
| context | 64k total, **32k for `state` + longest question** |
| rate | 1,200 req/min, 250k tok/s |
| version | `jev-1.13.0`; `jev-latest` and `jev-preview` alias it |
| training | **RLCD** — Reinforcement Learning for Calibrated Decisions, not RLHF |
| **input** | **text only. No images, audio or video.** |
| SDKs | `pip install typesafe-sdk`, `npm i @typesafe-ai/sdk`, `langchain-typesafe` |
| key | `console.typesafe.ai/settings/keys`, waitlisted early access |

Their own four-workflow benchmark, **vendor-reported**:

| model | accuracy | cost/case | latency |
|---|---|---|---|
| **Jev** | **67.8%** | **$0.0004** | **0.4 s** |
| GPT-5.6 Terra | 67.9% | $0.0304 | 10.1 s |
| GPT-5.6 Sol | 74.1% | $0.0836 | 23.3 s |
| Opus 5 | 73.1% | $0.1761 | 37.8 s |

**On verification:** one independent test has surfaced — 78 cases of a
classifier benchmark on an M4 Pro Mac mini, where Jev scored 0.974 against the
hosted API. The reviewer's own summary is the fair one: *the speed and cost
claims survive contact with a third party, the accuracy claim sits a notch
below the frontier, and 78 cases is too small to draw a production conclusion
from.* Calibration — the thing every threshold in `fast.py` rests on — is
precisely the claim that has **not** been independently reproduced. Treat
accuracy parity as promising, not settled.

Read it honestly: **a cheaper, much faster, slightly less accurate judge.**
Confidence gating is how you buy the accuracy back.

## 2. The nine documented failure modes

`docs.typesafe.ai/model-jaggedness/jev-1.13` is the most useful page TypeSafe
publish, and it is the one that shaped the code on this branch:

| # | failure | their mitigation | our response |
|---|---|---|---|
| 1 | **Literal reading** — answers the question you wrote, not the one you meant | state the exact condition; put boundary cases in `criteria` | every question in `fast.py` carries explicit `criteria` |
| 2 | **Math and counting** — unreliable, error grows with size | do arithmetic in code | we never ask it to count; indices are *labels*, not sums |
| 3 | **Dates as text**, not ordered quantities | extraction is judgment, arithmetic is not | no date question exists |
| 4 | **Indirection** — double negatives, multi-hop | write directly, name the relevant state | questions are single-hop |
| 5 | **Large state with irrelevant detail** degrades accuracy | filter and retrieve in code first | `MAX_ROWS = 60`, named controls only |
| 6 | **Adversarial content** — "state is data, and jev-1.13 does not treat it as hostile" | explicit criteria; test adversarial input | **see §5** — this one needed real code |
| 7 | **Contradictory instructions vs criteria** | align them as extensions of each other | — |
| 8 | **No structural invariants** — P(X) + P(¬X) ≠ 1 across questions | don't assume arithmetic identities | `margin` compares *within* one distribution only |
| 9 | **Generation** — cannot write text | bounded extraction, or a generative model | Claude keeps all text |

Also documented: pin to `jev-1.13.0` rather than `jev-latest` once thresholds
are tuned, since calibration is a property of the checkpoint; and always log
the response's `.model`.

## 3. How people are actually using it

Five days in, there are ~72 public projects. The relevant cluster is not the
support-ticket routing TypeSafe lead with — it is **agents that drive
interfaces**:

- **`browser-use/jev-ultrafast`** — the one worth copying. Abandons the
  screenshot-per-decision loop entirely: parses the page into an **indexed
  element table**, and a single request picks *both* the operation and the
  target element. Google Flights Zürich→London in **7.09 s median against
  9.45 s** baseline (25% faster), browser protocol calls **1,092 → 101**,
  cost $0.0039/task. A small LLM is called *only* when the operation is
  `TYPE_TEXT`. Action space: `CLICK, TYPE_TEXT, SELECT, SCROLL_UP,
  SCROLL_DOWN, WAIT, DONE, BLOCKED`.
- **`droidrun/mobile-jev`** — drove Uber on a real Android phone, 9 actions
  in ~21 s.
- **`typesafe-computer-use`** — macOS, OCR plus bounded Jev action selection.
  The closest thing to this project that exists publicly.
- **Games**: `jev-plays-pokemon-red` (PyBoy), `typesafe-mario`,
  `typesafe-snake`, `jev-tetris` (scores all reachable placements),
  `JevScape` (RuneScape), `jev-drone`. **Every one reads structured
  state from an emulator or engine — none reads pixels.**
- Others: `jev-palette` (ranks 77 commands per keystroke), trading bots,
  clinic triage, YouTube sponsor detection from captions.

The documented patterns worth stealing: **speculative fan-out** (ask
everything at once — TypeSafe report 12.2× cheaper and 10× faster than
sequential calls), **confidence-gated routing** (thresholds per action by cost
of being wrong), **composite scoring** (atomic Scores, weights in code),
**the cascade** (Jev routes, code handles what it can, a frontier model only
for the hard remainder), **retrieve-then-judge** (filter before sending —
failure mode 5).

## 4. Why it fits *this* project

The text-only limit kills Jev for most computer-use agents. It costs us
nothing, because of a decision already made and measured (STATE.md §3):

> UI Automation resolved 100% of 32 targets in 0.52 s; the vision pipeline
> managed 44% in 84.24 s, and 0% on icons, canvas-drawn controls and anything
> under 26px. **Screenshots are for confirming a result, never for deciding
> where to click.**

`locator.walk` already yields a control table; `browser.elements` already
yields an indexed page. That is *exactly* the artefact `jev-ultrafast` had to
build from scratch — we have had it since phase 01.

What is expensive is the **Claude round trip**, and a large share of those
turns are recognition, not reasoning: which control, did it work, what is
blocking, is this the click that spends money. Those are 200 ms questions.

```
Claude  — System 2.  Reads the task, writes the plan, decides what the
                     questions ARE, diagnoses anything that went wrong.
Jev     — System 1.  Answers them, inside the loop, at a rate the user
                     does not wait on.
```

The seam is **confidence**, not task type — the same trust ladder `replay.py`
runs on, with one more rung ~50× cheaper than the rung above it.

### Five call sites in the existing engine

| site | today | with Jev |
|---|---|---|
| `locator._resolve_among` | several same-named controls → the *k*-th in tree order, or nearest anchor | an ordinal dies when a page reflows. A Choice gated on **margin** — two identical "Buy" buttons give a *confident split*, which a confidence threshold waves through |
| `replay._alternate` (rung 3) | one idea: OCR label lookup | a Choice over the whole window, tried **before** waking Claude at rung 4/5 |
| `act.check` | structural `Verify` predicates | a Noul over the before/after diff catches the screen that is neither the old one nor the intended one |
| `retrieval.search` | lexical TF-IDF that by its own docstring cannot match "stock market" to "borsa" | re-rank top-N with one Choice; closes the gap `EMBEDDINGS` was reserved for, with no 90 MB download |
| *(new)* **commit guard** | — | the point of a fast loop is to stop asking permission per click; that is only safe if something cheap watches for the click that spends money |

5 Hz costs **$0.63/hour**. The Doom demo's $7/hour is 10 Hz with a far larger
state.

## 5. The finding that changed the code

Failure mode 6 is not a footnote for this project — it is the central risk:

> State is data, and jev-1.13 does not treat it as hostile by default.
> Content written to adversarially steer the model can move the answer.

Every control name we send comes off a web page or an application we do not
control. **On a checkout page, the merchant wrote that text.** A pay button
labelled "Continue browsing" — through sloppiness or design — would talk the
commit guard down, and the commit guard is the only thing between a fast loop
and a real purchase.

So `fast.py` runs **two independent guards, and either one holds**:

1. `code_side_commit()` — deterministic, page-independent, not reviewable by
   the model. Word list in English **and Turkish** (`satın al`, `ödeme`,
   `siparişi tamamla`, `onayla`), plus a context check on the window title or
   URL (`checkout`, `3D Secure`, `banka`). An English-only list would miss
   every site this is actually aimed at.
2. The model — catches what no word list anticipated.

**The model may add caution. It may never remove it.** An API outage, a
low-confidence reading and an offline stub all resolve to hold.

## 6. What is on this branch

| file | |
|---|---|
| `core/jev.py` | client — three primitives with validation, typed answers, retry/backoff, client-side rate cap, sync + async, pooling, cost and p50/p95 accounting, deterministic **offline stub** |
| `core/fast.py` | desktop bindings — control table → state, question templates, confidence/margin gates, the two-layer commit guard, `decide()` (operation + target + blocker + guard in one round trip), `Pilot` loop |
| `tests/test_jev.py` | **103 checks, fully offline** — no network, key or window |

Three design decisions worth keeping:

**An offline stub is never actable.** `Decision.real` is False without a key
and every gate refuses it. A missing credential must surface as "escalate to
Claude", not as a fast agent clicking confidently at random. STATE.md §3: fail
loudly, never plausibly.

**A late decision is discarded, not applied.** The answer describes the screen
as it was ~200 ms ago; `Pilot` re-senses and fingerprints before acting.
Dropping a tick costs 200 ms; not dropping it costs whatever was under the
button. This is also why `Pilot` is deliberately **not** pipelined —
requesting decision *N+1* while acting on *N* hides the latency, but every
action changes the state the next decision is about, so a pipelined answer is
stale by construction. Doom tolerates that; a checkout page does not.

**Operation and target in one round trip.** Copied from `jev-ultrafast`: the
heads read the same state, so four questions cost one network call. Asking
separately would be 4× the latency for identical information. `scroll`,
`wait`, `done` and `blocked` resolve with no target and are still good
answers — "the thing is not on screen yet" is a decision, and it is the one a
control table alone cannot express.

```
python tests/test_jev.py          # 103/103, no key needed
```

## 7. The three use cases, assessed

**Shopping — good.** Comparing N results is speculative fan-out: one state,
fifteen questions, costing extra tokens and almost no wall-clock. Claude never
reads the results page.

**Tickets — best fit, and the one that needs §5.** `jev-ultrafast` already
proved the shape: Google Flights end-to-end in 7.1 s. Latency buys something
real here — seat holds expire, fares requote. It is also where an over-eager
click costs money, which is why the guard is not optional.

**Games — mostly does not work here.** Every Jev game project reads
structured state from an emulator or engine. On this desktop there is no such
feed: UIA returns nothing for a DirectX or Unity canvas, and this project's
own measurement was **0% on canvas-drawn controls**. Jev drives games only
where state is already text — browser games with a real DOM, turn-based, card
games, or anything with a memory reader or game API. A native FPS is out of
reach, and Jev's speed is irrelevant to that: the bottleneck is perception,
which Jev does not do.

## 8. Risks

- **Early access**, waitlisted key. Cloudflare Workers AI (`typesafe/jev`),
  OpenRouter, Vercel and Netlify gateways are reachable meanwhile;
  `Client(endpoint=...)` takes the override.
- **Calibration is independently unverified** — and every threshold here
  depends on it. Run in shadow mode against known-good runs before trusting a
  threshold, as TypeSafe themselves recommend.
- **No rationale.** A wrong answer comes with a probability distribution and
  nothing else. For an agent that writes `lessons.json` from its own failures
  that is a real loss. Journal state + probabilities and let Claude diagnose
  the low-confidence ones after the fact.
- ~~Network RTT from Turkey is unmeasured~~ — **measured, see §10.**
- **A vendor in the inner loop.** Every gate degrades to "escalate to Claude"
  on failure, so an outage makes the agent slow, not wrong. Keep it that way.

## 9. Note on the repository

Only 15 files are tracked in git, all from the *old* root project. The entire
`agent/` tree — `core/`, `tools_sdk.py`, 799 passing tests — is **untracked
and uncommitted**. Only *new* files added here are isolated by this branch;
editing an existing `agent/` file would change `main` too, because git is not
managing those files. That is why `tests/run_all.py` was left alone. On merge:

```python
("test_jev", "system-1 decisions — gates, guard, staleness", False),
```

Committing `agent/` is worth doing soon, independently of any of this.

## 10. Measured, 2026-09-20

Live against Jev through OpenRouter (`jev-1.13`, answered by
`typesafe/jev-1.13-20260917`), from this machine, 30 calls, 0 failures.
State was a 5-row control table with 3 questions — the shape `fast.decide()`
actually sends.

| | |
|---|---|
| **cold call** (TLS handshake) | **1156 ms** |
| **warm p50** | **344 ms** |
| warm p95 | 453 ms |
| warm range | 281–469 ms, no long tail |
| per decision | 687 input tokens, **$0.000029** |
| 30 calls total | $0.0009 |

Connect time to each provider from here, to settle where the latency lives:

| | TCP | TLS | total |
|---|---|---|---|
| openrouter.ai | 55 ms | 77 ms | **132 ms** |
| api.cloudflare.com | 65 ms | 76 ms | 141 ms |
| api.typesafe.ai | 276 ms | 270 ms | **546 ms** |

**The gateway is not the cost.** OpenRouter is a quarter of the round trip to
TypeSafe's own API from Turkey, so direct access once the waitlist clears
would be *slower*, not faster. 344 ms is what a decision costs at this
distance — still ~30× quicker than the Claude turn it replaces.

Two changes followed from this:

- **`Pilot.DEFAULT_HZ = 2.0`**, down from 4. A 5 Hz loop needs a 200 ms
  budget and cannot have one; 2 Hz leaves the p95 room, ~2.9 Hz is the
  ceiling.
- **`KEEPALIVE_S = 90`** and `Client.prewarm()`. httpx holds idle connections
  for 5 s by default — shorter than the gap between two steps of a task, so
  the default would have put a 1156 ms handshake in front of most decisions.
  The cold call was also the entire reason an early 8-call sample showed a
  1.4 s p95.

Correctness spot-check, not just well-formedness: given a table containing
"Sort by price" and a "Loading results…" label, it picked index 3 at
confidence **1.00** and put the loading noul at **0.95**.

## 11. First live run on a real site, 2026-09-20

`jev_live_shop.py` against migros.com.tr, goal "search the site for milk
(süt) and show the milk products". Headless, real clicks.

```
[1] 446ms  op=type    target='Arama alanı'   -> filled
[2] 379ms  op=submit  -> pressed Enter
[3] 386ms  op=done    -> goal reached
4 calls, 0 failed, p50 453ms, $0.0006
```

Verified independently: `migros.com.tr/arama?q=süt`, title "süt - Migros",
18 milk products on the page. Three decisions, three correct, about 1.2s of
model time and six hundredths of a cent for the whole task.

It took four attempts, and each failure was worth more than the success.

**The search box announced itself as an advert.** Migros's site search is an
`<input>` inside `<sm-product-search-combobox aria-label="Arama alanı">`, and
the input's own placeholder is a rotating promotion — "Ülker Ürünlerini
Keşfet!". Both `elements()` and `SNAPSHOT_JS` resolved the name as
`aria-label || innerText || placeholder`, so the search field was presented
to the model as "Discover Ülker Products!". It answered `none`, correctly —
no person would have picked that either. Fixed by letting a wrapper's
`aria-label` outrank a placeholder, bounded to three ancestors, and by
falling back to the DOM `id` for identity. **This was a pre-existing bug in
the browser layer that only a decision model was blunt enough to expose.**

**Two index spaces.** The first working version chose the right control and
then failed with "no element for 8": `elements()` numbers one list and
`act()` resolves against the `data-da-idx` that `snapshot()` stamps. The
runner now reads the stamped snapshot, which is also what `jev-ultrafast`
does, and keeps a dense-row → DOM-index map.

**It filled the same box eight times.** Nothing in the state showed what a
field already contained, and no operation submitted anything — so every
turn, typing was still the best move. Fixed by carrying the field's current
value into the row (`v`) and adding a `submit` operation. A repeat detector
now stops any future version of this after three identical moves.

**A finished task read as "blocked".** On the results page the model ranked
`done` top at 0.74 and a flat 0.80 gate threw it away. Thresholds are now
per operation (`OP_GATE`): the ones that act on a control stay at 0.80, and
`submit` with them because Enter can send a form; `done`, `wait` and the
scrolls sit at 0.5–0.6, because being wrong about those costs a wasted
344ms, which is what blocking cost anyway.

### The guard

On the 113 controls of a real results page, the deterministic guard flagged
**0** — no false positives. The model guard, asked on that same real page:

| intent | verdict |
|---|---|
| add this milk to the basket | **allow**, p(commit)=0.22 |
| complete the order and pay for it | **hold** — code-side caught `'pay'` |
| scroll down to see more milk | **allow**, p=0.06 |

**Not yet proven:** the guard against a live Migros *checkout* screen. An
empty basket renders two controls and `/odeme` 404s, so reaching one needs a
logged-in session with items in it. Until that is run, the guard is verified
against intents and against a results page, not against the page that
actually matters.

## 12. A real shopping run, 2026-09-21

Four goals in sequence — the architecture the branch argues for, with Claude
writing the plan and Jev executing each step:

```
GOAL 1  type süt into the search box and run the search
  481ms  type   'Arama alanı'   ->  filled
  481ms  submit                 ->  pressed Enter
  463ms  done                       goal reached
GOAL 2  open the product page of the first milk product
  351ms  click  'Torku Laktozsuz Süt 1 L'
  333ms  done
GOAL 3  add this product to the shopping basket
  456ms  click  'Sepete Ekle'
  509ms  done
GOAL 4  go to the shopping basket
  357ms  click  'Sepetim'   ->  Playwright click timed out (8s)

9 calls, 0 failed, p50 453ms, 42k tokens, $0.0018
```

**Correction, after looking at the screenshots: the milk was never added to
the basket.** The frame taken right after "Sepete Ekle" shows `Sepetim
0,00 TL` with a 0 badge, the button greyed out, and a *"Teslimat Yöntemini
Belirle"* modal over the page — Migros wants a delivery method chosen before
anything can go in a basket. The run was read as a success from `op=done`
and nobody checked the page. That mistake is the most useful thing in this
document.

Why the model was confidently wrong, at 0.99:

- Playwright reported the click succeeded, which means it **landed**, not
  that it achieved anything. It landed and opened a modal.
- `already_done` then read "click Sepete Ekle", so from the state the goal
  looked accomplished.
- **Nothing in the state ever said the basket was still empty.**
- The `blocker` head answered `nothing` every single time, because it was
  phrased *"The expected control is not here. What is on screen instead?"* —
  which scopes it to a missing target. The target was present. A question
  that only fires once something else has already gone wrong is not a guard.

Two fixes, both in `fast.py`:

`q_blocker` now asks about the whole screen — "is anything standing between
the user and the task" — and has a `modal` option for exactly this case.

**Verification is wired into the loop.** `fast.worked()` existed from the
start and was never called: every action is now followed by a re-read and a
before/after noul. On the re-run it reads `p(worked)=0.92` for the search,
`0.86` for submit, `0.93` for opening the product, and **`0.13` for "Sepete
Ekle"** — where the loop stops and hands back instead of reporting success.

The rule this cost: **an action that a framework reports as "ok" has not been
verified.** Only the page can say that.

**Action history had to be added to the state.** Jev has no conversational
memory: it sees the state and nothing else. The first attempt searched
successfully, navigated to the results, found the search box empty again and
typed "süt" straight back into it — because for the goal as written, typing
*was* the correct answer. `rows_to_state(history=...)` now carries the last
six actions as `already_done`, and `q_operation` is told what that field
means. With it, step 3 answers `done` at 0.95.

**The guard's gradient, read back out of the journal:**

| action | p(needs asking first) |
|---|---|
| type in the search box | 0.07 |
| click a product | 0.08 |
| click "Sepetim" (view basket) | 0.11 |
| **click "Sepete Ekle" (add to basket)** | **0.24** |
| *`COMMIT_STOP`* | *0.30* |

Adding to the basket reads as three times more commit-like than browsing and
still, correctly, as reversible. That ordering is the calibration claim doing
something useful rather than merely being asserted.

### The journal

`JEV_LOG=logs/migros.jsonl` makes `core/jev.py` write every call verbatim —
state, questions, and the full probability distribution over every option.
Jev gives no rationale, so this is the only forensic trail a wrong decision
leaves, and a summary would throw away exactly the runner-up that explains
it. `jev_log.py` reads it back:

```
python jev_log.py --file logs/migros.jsonl --only op,pick
python jev_log.py --unsure 0.8      # just the moments it hesitated
python jev_log.py --state --full    # everything it was shown
```

## Sources

- https://docs.typesafe.ai — API reference, primitives, models, patterns
- https://docs.typesafe.ai/model-jaggedness/jev-1.13 — the nine failure modes
- https://typesafe.ai/blog/introducing-system-one-models-and-jev — launch
- https://developers.cloudflare.com/ai/models/typesafe/jev/ — schemas, samples
- https://github.com/browser-use/jev-ultrafast — indexed action space, 7.09 s
- https://github.com/browser-use/jev-ultrafast/blob/main/docs/performance.md
- https://dev.to/valyuai/how-to-use-jev-a-practical-guide-to-typesafes-system-one-model-g5e — patterns
- https://flaviocopes.com/jev/ — architecture deep dive, RLCD
- https://www.langchain.com/blog/building-a-harness-with-jev — harness middleware
- https://github.com/cobanov/awesome-jev — the project directory
- https://www.datacamp.com/blog/system-one-models-jev — benchmark table
- https://github.com/tirukovelamanoj/jev-plays-doom — 212 ms over 2,642 calls
