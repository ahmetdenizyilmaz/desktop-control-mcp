# System-1 decisions: evaluating TypeSafe Jev for the desktop agent

Research note and design record. Branch `jev`. Nothing here is merged.

---

## 1. What Jev is

TypeSafe AI came out of stealth on 2026-09-15 with **Jev**, which they call a
*System One model*. It is not an LLM and does not generate text. You send it a
**state** (a string or JSON object) and a map of **typed questions**; it
returns one calibrated answer per question in a single parallel pass.

Three question primitives, and that is the whole surface:

| primitive | asks | answer |
|---|---|---|
| **Noul** | a yes/no proposition | `noul` — P(yes), 0–1. The probability *is* the confidence |
| **Choice** | pick one of up to 255 named options | `choice`, `probabilities` for every option, `confidence` |
| **Score** | rate against 2–10 *ordered, described* levels | `score` (probability-weighted mean), `probabilities`, `legend`, `confidence` |

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

Numbers, from the vendor's docs and launch material:

| | |
|---|---|
| latency | 70–500 ms end to end; their Doom demo held **212 ms median over 2,642 calls**, measured on a laptop |
| price | **$0.042 / Mtok in, $0.00 out** — there is no generated text to charge for |
| per decision | ~$0.0004 on their benchmark; a 60-control window is ~$0.00005 |
| context | 64k total, **32k budget for `state` + longest question** |
| rate | 1,200 req/min, 250k tok/s |
| version | `jev-1.13.0`; `jev-latest` and `jev-preview` both alias it |
| training | "RLCD" — Reinforcement Learning for Calibrated Decisions, rather than RLHF |
| **input** | **text only. No images, no audio, no video.** |

Their own four-workflow benchmark, vendor-reported and not independently
verified:

| model | accuracy | cost/case | latency |
|---|---|---|---|
| **Jev** | **67.8%** | **$0.0004** | **0.4 s** |
| GPT-5.6 Terra | 67.9% | $0.0304 | 10.1 s |
| GPT-5.6 Sol | 74.1% | $0.0836 | 23.3 s |
| Opus 5 | 73.1% | $0.1761 | 37.8 s |

Read that honestly: **Jev is a cheaper, much faster, slightly less accurate
judge**. It is ~5 points behind a frontier model at 1/400th the cost and
1/90th the latency. Confidence gating is how you buy the accuracy back — you
act on the concentrated answers and escalate the rest.

Availability: early access, waitlisted keys, rolling out in batches. Also
reachable through **Cloudflare Workers AI** (`typesafe/jev`), OpenRouter,
Vercel and Netlify AI gateways, and AIMLAPI. SDKs: `typesafe-sdk` (Python),
a JS/TS client, and `langchain-typesafe`.

## 2. Why it fits *this* project unusually well

The text-only limit is what kills Jev for most computer-use agents. It costs
us nothing, because of a decision this project already made and measured
(STATE.md §3):

> UI Automation resolved 100% of 32 targets in 0.52 s; the vision pipeline
> managed 44% in 84.24 s, and 0% on icons, canvas-drawn controls and anything
> under 26px. **Screenshots are for confirming a result, never for deciding
> where to click.**

The engine already turns the screen into text before anything decides
anything — `locator.walk` yields a control table, `browser.elements` yields an
indexed page, `read_labels` yields OCR labels. The state Jev demands is the
state we already produce. We would be feeding it the exact artefact the
architecture was built around.

What is expensive in the loop is not the clicking. `actuator` Invoke is
sub-millisecond and works on background windows. What is expensive is the
**round trip to Claude**, and a large share of those turns are not reasoning
at all — they are recognition:

- "which of these 60 controls is the one?"
- "did that step do what it was for?"
- "is this a cookie banner, a login wall, or the checkout dialog?"
- "is the thing I am about to click the one that spends the money?"

Those are 200 ms questions. Today they cost a full turn each.

**The division of labour this argues for:**

```
Claude  — System 2.  Reads the task, writes the plan, decides what the
                     questions ARE, diagnoses anything that went wrong.
Jev     — System 1.  Answers those questions, over and over, inside the
                     loop, at a rate the user does not wait on.
```

The seam is **confidence**, not task type — the same trust ladder `replay.py`
already runs on, with one more rung near the bottom that is ~50× cheaper than
the rung above it.

## 3. Five places in the existing engine

| site | today | with Jev |
|---|---|---|
| `locator._resolve_among` | several controls share a name → take the *k*-th in tree order, or the one nearest an anchor | an ordinal dies the moment a page reflows. A Choice over the candidates, gated on **margin** (two identical "Buy" buttons produce a confident *split*, which confidence alone waves through) |
| `replay._alternate` — rung 3 | one idea: look the name up via OCR labels | a Choice over everything in the window, tried **before** waking Claude at rung 4/5. Turns one trick into a general repair for 200 ms |
| `act.check` | structural `Verify` predicates | a Noul over the before/after control diff catches the interesting failure: a screen that is neither the old one nor the intended one |
| `retrieval.search` | lexical TF-IDF + fuzzy, which by its own docstring *cannot match "stock market" to "borsa"* and therefore hands candidates to Claude | re-rank the top-N with one Choice. The docstring reserved `EMBEDDINGS` for this gap; this closes it without a 90 MB download |
| *(new)* **commit guard** | — | the point of a fast loop is to stop asking permission for every click. That is only safe if something cheap watches for the click that spends money. At $0.00005 you can afford to ask before **every** action, which is the only frequency that protects anything |

Costs at working speed: a 60-control window state is ~830 tokens ≈ **$0.000035
per decision**. At 5 Hz that is **$0.63/hour** — and a desktop agent does not
need 5 Hz. Their Doom demo's "$7/hour" is 10 Hz with a much larger state.

## 4. The three use cases, assessed honestly

**Online shopping — good fit.** The loop is search → filter → compare N → pick
→ cart → checkout, and comparing N items is exactly the *speculative fan-out*
pattern: one state (the results page), fifteen questions at once, costing the
extra question tokens and almost no extra wall-clock. Claude never reads the
results page. Escalate only on low confidence.

**Plane / bus tickets — best fit, and the one that needs the guard.** This is
where latency buys something real rather than just feeling nicer: inventory
moves, seat holds expire, fares requote. A 0.2 s decision versus a 10 s turn
is the difference between getting the seat and getting "no longer available".
It is also where an over-eager click costs actual money, so the commit guard
is not optional here — it is the thing that makes running the loop fast
*acceptable*.

**Games — mostly does not work here, and this is worth being blunt about.**
TypeSafe's Doom demo reads structured game state *from the emulator* — enemy
kind, distance, bearing as JSON — not pixels, and it happily sees through
walls. On this desktop there is no such feed. UIA returns nothing useful for a
DirectX or Unity canvas; the project's own measurement was **0% on
canvas-drawn controls**. So Jev drives games only where the state is already
text: browser games with a real DOM (`browser.elements` gives it to us for
free), turn-based and card games, board games, anything with a memory reader
or a published game API. A native FPS is out of reach, and no amount of Jev
latency changes that — the bottleneck is perception, which Jev does not do.

## 5. What is on this branch

| file | |
|---|---|
| `core/jev.py` | the client. Three primitives with validation, typed answers, retry/backoff on 429·5xx·529, a client-side 1200/min cap, sync + async, connection pooling, cost and p50/p95 accounting, and a deterministic **offline stub** so the loop can be built, tested and measured with no API key |
| `core/fast.py` | the desktop bindings: control table → state, the five question templates, the confidence/margin gates, the commit guard, and `Pilot` — a sense→decide→act loop with a staleness check |
| `tests/test_jev.py` | 79 checks, fully offline — no network, no window, no key |

Two design decisions worth keeping:

**An offline stub must never be actable.** `Decision.real` is False without a
key, and every gate in `fast.py` refuses it. A missing credential has to
surface as "escalate to Claude", not as a fast agent clicking confidently at
random. STATE.md §3: fail loudly, never plausibly.

**A late decision is thrown away, not applied.** The answer describes the
screen as it was ~200 ms ago. `Pilot` re-senses and compares a fingerprint
before acting; if the window moved, the tick is dropped. Dropping a tick costs
200 ms. Not dropping it costs whatever was underneath the button. This is also
why `Pilot` is deliberately **not** pipelined — requesting decision *N+1*
while acting on *N* would hide the latency, but every action changes the state
the next decision is about, so the pipelined answer is stale by construction.
Doom can live with that; a checkout page cannot.

Run it:

```
python tests/test_jev.py          # 79/79, no key needed
```

Not wired into `tests/run_all.py`, and no existing file is modified — see §7.

## 6. Risks

- **Early access.** Needs a waitlisted key. Cloudflare Workers AI is the
  usable path today; `core/jev.py` takes an `endpoint` override for it.
- **~5 points behind Opus 5** on the vendor's own benchmark, vendor-reported.
  Gate accordingly; do not let it decide anything it is not confident about.
- **No rationale.** When Jev is wrong you get a probability distribution and
  nothing else. For an agent that writes `lessons.json` from its own failures
  that is a real loss. Mitigation: journal state + probabilities on every
  decision and let Claude post-hoc diagnose the low-confidence ones.
- **Calibration is not correctness.** A confident answer can be wrong; what
  calibration buys is that confident answers are wrong *less often*, which is
  what makes a threshold meaningful.
- **Network RTT from Turkey** is on top of the 70–500 ms. Unmeasured. First
  thing to check with a real key — `Client.stats.report()` prints p50/p95.
- **A vendor dependency in the inner loop.** Every gate degrades to "escalate
  to Claude" on failure, so an outage makes the agent slow, not wrong. Keep
  it that way.

## 7. Note on the repository

Only 15 files are tracked in git, and they are the *old* root project
(`controller.py`, `mcp_server.py`, `server.py`, …). The entire `agent/` tree —
the actual product, `core/`, `tools_sdk.py`, 799 passing tests — is
**untracked and uncommitted**.

That has a consequence for this branch: only the new files added here are
isolated by it. Editing an existing file under `agent/` would change the
working tree on `main` too, because git is not managing those files at all.
That is why `tests/run_all.py` was left alone. When this merges, the one-line
addition is:

```python
("test_jev", "system-1 decisions — gates, guard, staleness", False),
```

Independently of Jev: committing `agent/` is worth doing soon.

## Sources

- https://docs.typesafe.ai — API reference, primitives, models, patterns
- https://typesafe.ai/blog/introducing-system-one-models-and-jev — launch
- https://developers.cloudflare.com/ai/models/typesafe/jev/ — schemas, samples
- https://www.langchain.com/blog/building-a-harness-with-jev — harness patterns
- https://www.datacamp.com/blog/system-one-models-jev — benchmark table
- https://www.theregister.com/ai-and-ml/2026/09/16/typesafe_ai_debuts_model/ — Doom
- https://github.com/tirukovelamanoj/jev-plays-doom — the loop, 212 ms/2,642 calls
