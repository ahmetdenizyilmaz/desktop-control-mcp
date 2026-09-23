"""One short answer from a small model, fast.

Filling a script's parameters from a task ("play tarkan on spotify" ->
query = "tarkan") and breaking a tie Jev could not are one-line jobs. Doing
them through the Claude CLI cost 10-18 s each - most of it starting the CLI.
A direct HTTP call to a small model through the OpenRouter key already in
the vault measured 1.2 s.

The CLI stays the fallback: no OpenRouter key, or the call failing, and the
answer still comes - just slower.
"""

from __future__ import annotations

import json
import time
import urllib.request

OPENROUTER_CHAT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "anthropic/claude-haiku-4.5"


def _openrouter_key() -> str:
    try:
        from .secrets import vault
        return vault.get("openrouter-api-key") if vault.has(
            "openrouter-api-key") else ""
    except Exception:
        return ""


def complete(prompt: str, settings: dict | None = None,
             cli_model: str = "haiku", env: dict | None = None,
             max_tokens: int = 300, timeout: float = 25.0) -> tuple[str, str]:
    """(answer text, how) - how is 'openrouter <ms>' or 'cli <ms>'."""
    settings = settings or {}
    key = _openrouter_key()
    t0 = time.time()
    if key:
        body = json.dumps({
            "model": settings.get("quick_model") or DEFAULT_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens, "temperature": 0}).encode()
        req = urllib.request.Request(
            OPENROUTER_CHAT, data=body,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json",
                     "User-Agent": "AGIAgent"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                out = json.load(r)
            text = out["choices"][0]["message"]["content"] or ""
            return text, f"openrouter {int((time.time() - t0) * 1000)} ms"
        except Exception:
            pass                               # fall through to the CLI
    from .authoring import _claude
    t0 = time.time()
    text = _claude(prompt, cli_model, 120, env)
    return text, f"cli {int((time.time() - t0) * 1000)} ms"
