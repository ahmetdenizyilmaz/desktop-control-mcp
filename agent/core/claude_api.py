"""Claude called directly over the API - no `claude -p` in between.

Every CLI call starts a Node process, loads Claude Code's own system prompt
and tools, and only then asks the model: a one-line decision cost ~$0.09
and 10 s, a rescue 10-17 s. The same model over the Messages API answers a
one-liner in ~3 s for a fraction of a cent, and a rescue sends the
screenshot inline instead of asking the CLI to go read a file.

Credentials, first found wins:
  1. an Anthropic API key saved in Settings (vault `anthropic-api-key`)
  2. ANTHROPIC_API_KEY in the environment
  3. the OpenRouter key already in the vault - OpenRouter speaks the
     Anthropic wire format at /api, so it is the same SDK and the same model

None of them, or a call that fails: `call()` returns None and the caller
goes back to the CLI, which uses the Claude login. `configure()` picks the
transport from Settings ("api" or "cli").
"""

from __future__ import annotations

import base64
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

ANTHROPIC_KEY = "anthropic-api-key"
OPENROUTER_KEY = "openrouter-api-key"
OPENROUTER_BASE = "https://openrouter.ai/api"

#: The short names the CLI takes, as API model ids.
ALIASES = {
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
    "fable": "claude-fable-5-1",
}

#: $ per million tokens (input, output). Cache reads are 0.1x input, cache
#: writes 1.25x. Only used when the reply does not carry its own cost
#: (OpenRouter's does).
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
}

#: How hard the model thinks, by job. Decisions and parameter filling are
#: lookups; a rescue or a new script is real work.
EFFORT = {"decide": "low", "details": "low", "rescue": "medium",
          "write": "medium", "script": "medium"}

mode = "api"          # "api" or "cli" - set from Settings by configure()
last_error = ""       # why the last call fell back, for the Test button


@dataclass
class Reply:
    text: str
    seconds: float
    cost: float | None
    model: str          # as sent, e.g. "anthropic/claude-opus-5.5"
    route: str          # "anthropic", "anthropic-env" or "openrouter"


def configure(settings: dict) -> None:
    global mode
    mode = "cli" if (settings or {}).get("claude_transport") == "cli" else "api"


def enabled() -> bool:
    return mode == "api" and route() is not None


def _vault(name: str) -> str:
    try:
        from .secrets import vault
        return vault.get(name) if vault.has(name) else ""
    except Exception:
        return ""


def route() -> tuple[str, dict] | None:
    """(which, client kwargs) for the first credential there is."""
    key = _vault(ANTHROPIC_KEY)
    if key:
        return "anthropic", {"api_key": key}
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic-env", {}
    key = _vault(OPENROUTER_KEY)
    if key:
        return "openrouter", {"base_url": OPENROUTER_BASE,
                              "auth_token": key, "api_key": None}
    return None


def model_id(model: str, which: str) -> str:
    """'opus' -> 'claude-opus-5-5'; on OpenRouter 'anthropic/claude-opus-5.5'."""
    m = (model or "opus").strip()
    if "/" in m:                                   # already an OpenRouter id
        return m if which == "openrouter" else _from_openrouter(m)
    m = ALIASES.get(m.lower(), m)
    if which == "openrouter":
        return "anthropic/" + re.sub(r"-(\d+)-(\d+)$", r"-\1.\2", m)
    return m


def _from_openrouter(m: str) -> str:
    return re.sub(r"-(\d+)\.(\d+)$", r"-\1-\2", m.split("/", 1)[1])


def price(model: str, usage) -> float | None:
    rates = PRICES.get(_from_openrouter(model) if "/" in model else model)
    if not rates or usage is None:
        return None
    inp, out = rates
    fresh = getattr(usage, "input_tokens", 0) or 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    wrote = getattr(usage, "cache_creation_input_tokens", 0) or 0
    done = getattr(usage, "output_tokens", 0) or 0
    return (fresh * inp + read * inp * 0.1 + wrote * inp * 1.25
            + done * out) / 1e6


def _image_block(path: str | Path) -> dict:
    p = Path(path)
    media = "image/jpeg" if p.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    return {"type": "image", "source": {
        "type": "base64", "media_type": media,
        "data": base64.standard_b64encode(p.read_bytes()).decode("ascii")}}


def call(prompt: str, model: str = "opus", *, system: str = "",
         images: list | tuple = (), max_tokens: int = 8000,
         timeout: float = 150.0, kind: str = "write",
         record: bool = True) -> Reply | None:
    """One message to Claude. None when there is no credential or the call
    failed - the caller then uses the CLI. `system` is sent as a cached
    block: keep it the same between calls (the step language, the guide)."""
    global last_error
    r = route()
    if r is None:
        last_error = "no Anthropic or OpenRouter key"
        return None
    which, kw = r
    mid = model_id(model, which)
    content: list = [_image_block(p) for p in images if p]
    content.append({"type": "text", "text": prompt})
    req = {"model": mid, "max_tokens": max_tokens,
           "messages": [{"role": "user", "content": content}]}
    if system:
        req["system"] = [{"type": "text", "text": system,
                          "cache_control": {"type": "ephemeral"}}]
    effort = EFFORT.get(kind.split(":")[0])
    if effort:
        req["output_config"] = {"effort": effort}
    t0 = time.time()
    try:
        import anthropic
        client = anthropic.Anthropic(timeout=timeout, max_retries=1, **kw)
        try:
            msg = client.messages.create(**req)
        except anthropic.BadRequestError as e:
            # An older or non-Anthropic model that does not know `effort`:
            # ask once more without it.
            if "output_config" not in req or "effort" not in str(e):
                raise
            req.pop("output_config")
            msg = client.messages.create(**req)
    except Exception as e:
        last_error = f"{type(e).__name__}: {str(e)[:200]}"
        try:
            from . import tasklog
            tasklog.model(f"api {mid}", kind, prompt, f"(failed: {last_error})",
                          time.time() - t0)
        except Exception:
            pass
        return None
    last_error = ""
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    cost = getattr(msg.usage, "cost", None)       # OpenRouter says what it cost
    if cost is None:
        cost = price(mid, msg.usage)
    out = Reply(text, time.time() - t0, cost, mid, which)
    if record:
        from . import ledger, tasklog
        ledger.record(kind, out.seconds, cost, f"api {mid}")
        tasklog.model(f"api {mid}", kind, (system + "\n\n" if system else "")
                      + prompt, text, out.seconds, cost)
    return out
