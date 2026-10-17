# Codex engine

In Settings, select **Planning and fallback engine → Codex**. Jev remains
the default script/product decider. The Codex model field may be blank to
use the CLI default. Claude's model and transport fields apply to Claude.

Codex handles script authoring, screenshot rescues, parameter extraction,
and bounded fallback replanning through the existing guarded script runner.
It does not use Claude's SDK tool-agent loop. Fallback makes at most two
new planning attempts; it never silently switches to Claude on failure.

The installed Codex CLI supplies authentication. Settings checks both
`codex login status` and `claude auth status`, without displaying account
emails or credentials. Authentication status is not a model-access test;
use **Test Codex** to make a small real request. Refresh status after login.

Timing, prompts, replies and supplied screenshots use the existing ledger
and optional task logger. Codex does not report a dollar charge here, so
the ledger records cost as unreported, never zero or an invented amount.

This is a source change. The packaging manifest includes the adapter, but
an existing executable must be rebuilt before it has the new settings.

Validation: `python -m unittest tests.test_codex_backend` and
`python -X utf8 tests/test_engine.py`. A live synthetic screenshot with the
script guide produced a schema-valid script.

Live GUI test (2026-09-24): selected Codex in Settings using an isolated data
folder, verified both login statuses and instant save, submitted a Notepad
task through Run task. Jev followed by Codex completed it in 23 seconds;
the five script steps took 0.9 seconds. An independent UIA TextPattern read
confirmed the exact Turkish text. The first attempt exposed a text-verifier
false negative; typed-text checks now consult the focused editor's actual
text when the expectation is exactly the entered text and no Enter follows.
No purchases, messages or saved documents were part of this test.

Switching per task (2026-10-14): the task bar has an **Engine** picker next to
Stop — the same `planning_engine` setting as Settings, saved at once and used
from the next task (locked while a task runs). Its labels name the model each
engine will use ("Claude · Opus 5.5", "Codex · default model"). Switching is
manual only: neither engine hands a failed task to the other. Default set to
Claude on Opus 5.5 through the Claude login (provider anthropic, model
claude-opus-5-5, transport cli); the LiteLLM base_url is kept for when the
provider is set back to custom. Checked: headless render of the picker at
1180x812 and 880x560, a real "Reply OK" through each engine.
