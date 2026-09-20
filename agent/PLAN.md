# Desktop Agent — what we are building

**Read this and tell me if it is not what you meant.** Everything below is my
understanding of your goal. If a line is wrong, that line is the thing to fix
before more code gets written.

---

## 1. The purpose, in one paragraph

You want to stop doing repetitive computer work by hand. You describe a job in
plain language — *"open Excel, load this month's sales CSV, apply the pivot
template"* — and the computer does it, using the same applications you would
have used, clicking the same buttons you would have clicked. When a job is
worth repeating, it remembers it and does it again on command, or on its own.
Over time it accumulates a library of jobs it knows how to do, which can be
shared with other people and adapted to their machines.

**It is not a script runner.** Scripts break when a window moves. This drives
the real UI, checks that each step actually worked, and repairs itself when an
application changes.

---

## 2. Who does what

The system is three parts. Confusing them is what made the app look pointless
at first.

| part | job | state |
|---|---|---|
| **Brain** — Claude | understands the task, decides which buttons to press, recovers when something is unexpected | ✅ connected via MCP |
| **Hands** — this engine | finds any control by name, clicks it, types, never hits the wrong window, stops when you touch the mouse | ✅ built and tested |
| **Memory** — macro store | records a job once, replays it forever, on any PC, and repairs it as apps change | ✅ built |

The dashboard at `http://127.0.0.1:7900` is a **supervision surface**, not the
product. It shows what the agent is doing, lets you take control back, and
manages the job library. The product is: you ask for something, it happens.

---

## 3. What it can do today

- Launch any installed application by name
- List every control in any window **by name**, in ~0.1–0.5 seconds
- Click, type, press keys, scroll, drag — verified against the app's own state
- Read text off controls that publish no name (Electron, Tk, custom toolkits)
- Record what you do and turn it into a replayable job
- Replay a job, verify each step, and retry through alternatives when one fails
- Refuse to act when you are using the machine; abort on physical ESC+ESC
- Notice when a long job has stalled and push it to continue

Proven end to end: it launched Notepad, found the text area without a
screenshot, typed, read the text back from the application, saved the job as a
portable macro, and replayed it in 0.2 seconds.

## 4. What it cannot do yet

| missing | why it matters | phase |
|---|---|---|
| Web automation | most real jobs involve a browser | 06 |
| Credential vault | it cannot log in to anything | 05 |
| Reach you when you are away | unattended jobs stall on any prompt | 05 |
| Find a job by description | you must know the macro's name | 08 |
| Task box in the app | today you drive it from Claude Code, not the app UI | 07 |

---

## 5. The principles that shaped it

These came out of measurements, not preference. Each one cost something to
learn.

**Never store a coordinate.** A pixel position is dead the moment a window
moves, a theme changes, or the job runs on a different screen. Jobs store
*identity* — "the button named Save" — and resolve it fresh each time. This is
what makes a job portable to another PC.

**Ask the system, don't look at the screen.** Windows already knows what is on
screen and exactly where. Reading that is **160× faster and twice as accurate**
as looking at pixels: 0.5s at 100% versus 84s at 44%. Screenshots are a
fallback for things that publish nothing — games, canvases — not the default.

**Click without touching your mouse.** Clicking through the accessibility layer
works on background windows and never moves your cursor, so a job can run while
you work. Coordinate-clicking a covered window does not fail safely — it clicks
whatever is on top, which is the wrong application.

**Verify every step.** A job without verification is a script that fails
silently three steps later. Each step declares what should be true afterwards,
and that is also what makes self-repair possible.

**The user always wins.** Physical input pauses the job. Switching windows ends
it. A physical ESC+ESC aborts it, and — because injected keys are distinguished
from real ones — the agent cannot press its own kill switch.

**Fail loudly, never plausibly.** A blank screen must not read as "nothing on
screen"; a locked session must not read as text. Both were real bugs, and both
now raise instead of returning something believable and wrong.

---

## 6. Sharing jobs

Long term you want a library people can download from. That shaped the file
format from day one:

| `macro.json` — shareable | `local.json` — never leaves your PC |
|---|---|
| steps, control names, intents | trust level, run statistics |
| what it needs (apps, folders) | which locators work on your machine |
| what it is permitted to do | screenshots from recording |
| author, target apps | your paths, your machine id |

Publishing a job is copying one file. **There is nothing to scrub, because
nothing private was ever written on that side** — verified by an automated
check that fails the build if it ever is.

A downloaded job always arrives untrusted, declares what it wants to touch
before it runs, and has to earn trust on your machine by working.

---

## 7. Trust

A job is not allowed to run unattended until it has proven itself.

```
DRAFT ──recorded──> SUPERVISED ──2 clean runs──> TRUSTED
                         ▲                          │
                         └────── any failure ───────┘
```

Failure always demotes. Trust is earned repeatedly, never granted permanently,
and never travels inside a shared file.

---

## 8. When something goes wrong

A job does not stop at the first problem. It escalates:

1. try the next way of identifying the control
2. wait and look again — the app may still be loading (×5)
3. find the control by the text drawn on it
4. use what the step was *for* to reach the goal another way
5. stop, summarise what was tried, and ask you

On recovery it **keeps the route that worked**, so the same failure does not
recur.

---

## 9. Build order

| # | phase | status |
|---|---|---|
| 00 | Portable job format, machine profile | ✅ done |
| 01 | Find and click any control | ✅ **33/33** |
| 02 | Control arbitration, kill switch | ✅ **17/17** |
| — | Naming for unnamed controls | ✅ **18/18** |
| — | Recorder, replay, watchdog, daemon, UI, MCP | ✅ **14/14 end to end** |
| 05 | Credential vault + reaching you (Telegram) | next |
| 06 | Browser automation | next |
| 07 | Task box in the app | next |
| 08 | Find a job by describing it | next |

---

## 10. Where I might have misunderstood you

Tell me if any of these are wrong:

1. **The app is a supervisor, not the interface you type tasks into** — today
   you talk to Claude, and the app shows you what is happening. You said you
   want the app to *contain* Claude, which is phase 07. Is a dashboard-plus-task-box
   the right shape, or did you want something else entirely?
2. **Jobs are per-application, not per-goal** — "open Excel and load a file" is
   a job. "Do my monthly reporting" would be several jobs chained. Is that the
   granularity you meant?
3. **Sharing is a file you send**, not a live service — a public library comes
   later, and needs a server that does not exist yet.
4. **Unattended means it messages you when stuck**, not that it guesses. On a
   login prompt it will ask you rather than invent a password.
5. **The browser is a separate path from the desktop** — web jobs will not use
   screenshots or clicking at all; they read the page structure directly, which
   is far faster and works at any window size.
