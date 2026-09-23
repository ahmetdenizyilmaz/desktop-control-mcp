"""Provider keys for the engines, from the vault - and a live test for each.

Jev used to read its key from an environment variable only. A live run
started without it, every product choice failed, and Opus quietly did the
choosing instead - slower, dearer, and not what was asked for. So the key
lives in the vault like every other secret, and the Settings page can prove
it works with one real decision instead of trusting that it is there.

Jev's OpenRouter backend uses the SAME OpenRouter key the Claude provider
already stores (`openrouter-api-key`): one account, one key, asked for once.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time

from .secrets import vault

OPENROUTER_KEY = "openrouter-api-key"      # shared with app.py
JEV_SECRETS = {
    "openrouter": (OPENROUTER_KEY, ""),
    "typesafe": ("jev-typesafe-key", ""),
    "cloudflare": ("jev-cloudflare-token", "jev-cloudflare-account"),
}
JEV_BACKENDS = tuple(JEV_SECRETS)


def _secret(name: str) -> str:
    if not name:
        return ""
    try:
        return vault.get(name) if vault.has(name) else ""
    except Exception:
        return ""


def has_jev_key(settings: dict) -> bool:
    key_name, _ = JEV_SECRETS.get(settings.get("jev_backend") or "openrouter",
                                  ("", ""))
    try:
        return bool(key_name) and vault.has(key_name)
    except Exception:
        return False


def save_jev_key(backend: str, key: str, account: str = "") -> dict:
    if backend not in JEV_SECRETS:
        return {"ok": False, "error": f"unknown backend {backend!r}"}
    key_name, acct_name = JEV_SECRETS[backend]
    key = (key or "").strip()
    if not key:
        vault.delete(key_name)
        return {"ok": True, "saved": False}
    vault.put(key_name, key, note=f"Jev decision model ({backend})")
    if acct_name and account.strip():
        vault.put(acct_name, account.strip(), note="Cloudflare account id")
    _clients.clear()
    return {"ok": True, "saved": True}


_clients: dict = {}


def jev_client(settings: dict):
    """One shared client per (backend, key): the warm connection pool is
    most of the difference between a 200 ms and a 400 ms decision."""
    from . import jev
    backend = settings.get("jev_backend") or "openrouter"
    key_name, acct_name = JEV_SECRETS.get(backend, JEV_SECRETS["openrouter"])
    key, acct = _secret(key_name), _secret(acct_name)
    tag = (backend, hashlib.sha256(key.encode()).hexdigest()[:12], acct)
    c = _clients.get(tag)
    if c is None:
        try:
            c = jev.Client(api_key=key or None, backend=backend,
                           account_id=acct or None)
        except ValueError:
            # Cloudflare without an account id: honest offline, not a crash.
            c = jev.Client(api_key=None, backend="openrouter", offline=True)
        _clients.clear()
        _clients[tag] = c
    return c


def test_jev(settings: dict) -> dict:
    """One real decision, timed. Proves the key, the backend and the model."""
    from . import jev
    c = jev_client(settings)
    if c.offline:
        return {"ok": False, "error": "no key saved for "
                f"{settings.get('jev_backend') or 'openrouter'}"}
    t0 = time.time()
    try:
        d = c.ask({"note": "connection test"},
                  {"pick": jev.Choice(
                      instructions="Which one is a fruit?",
                      criteria={"apple": "an apple", "hammer": "a hammer",
                                "car": "a car"})})
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    a = d.answers.get("pick")
    return {"ok": bool(d.real and a is not None and a.choice == "apple"),
            "real": d.real, "choice": getattr(a, "choice", ""),
            "margin": round(float(getattr(a, "margin", 0.0)), 2),
            "latency_ms": int((time.time() - t0) * 1000),
            "model": d.model, "backend": c.backend, "at": time.time()}


def test_claude(settings: dict, env: dict | None = None) -> dict:
    """Is the Claude CLI installed, logged in, and answering on this model?"""
    exe = shutil.which("claude")
    if not exe:
        return {"ok": False, "error": "Claude Code CLI not installed"}
    model = settings.get("rescue_model") or settings.get("model") or "opus"
    t0 = time.time()
    try:
        import os
        p = subprocess.run(
            [exe, "-p", "Reply with exactly: OK", "--model", model,
             "--output-format", "json"],
            capture_output=True, text=True, encoding="utf-8", timeout=90,
            env={**os.environ, **(env or {})}, creationflags=0x08000000)
        out = json.loads(p.stdout or "{}")
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "no answer in 90 s"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if out.get("is_error"):
        return {"ok": False, "error": str(out.get("result", ""))[:200]}
    return {"ok": "OK" in str(out.get("result", "")), "model": model,
            "latency_ms": int((time.time() - t0) * 1000),
            "cost_usd": out.get("total_cost_usd"), "at": time.time()}


def test_claude_api(model: str = "opus") -> dict:
    """One direct API call (no CLI): does the key work on this model?"""
    from . import claude_api
    r = claude_api.route()
    if r is None:
        return {"ok": False, "error": "no Anthropic or OpenRouter key saved"}
    got = claude_api.call("Reply with exactly: OK", model, max_tokens=200,
                          timeout=60, kind="decide", record=False)
    if got is None:
        return {"ok": False, "route": r[0], "error": claude_api.last_error}
    return {"ok": "OK" in got.text, "model": got.model, "route": got.route,
            "latency_ms": int(got.seconds * 1000), "cost_usd": got.cost,
            "at": time.time()}


def _get_json(url: str, headers: dict, timeout: float = 15.0) -> dict:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "AGIAgent",
                                               **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def test_openrouter() -> dict:
    """Is the OpenRouter key valid, and how much of its limit is left?"""
    key = _secret(OPENROUTER_KEY)
    if not key:
        return {"ok": False, "error": "no OpenRouter key saved"}
    t0 = time.time()
    try:
        d = _get_json("https://openrouter.ai/api/v1/key",
                      {"Authorization": f"Bearer {key}"}).get("data", {})
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
    limit, used = d.get("limit"), d.get("usage")
    return {"ok": True, "latency_ms": int((time.time() - t0) * 1000),
            "limit": limit, "usage": used,
            "left": (round(limit - used, 2)
                     if isinstance(limit, (int, float))
                     and isinstance(used, (int, float)) else None),
            "at": time.time()}


def test_gemini() -> dict:
    key = _secret("gemini-api-key")
    if not key:
        return {"ok": False, "error": "no Gemini key saved"}
    t0 = time.time()
    try:
        d = _get_json("https://generativelanguage.googleapis.com/v1beta/"
                      f"models?key={key}", {})
    except Exception as e:
        msg = str(e).replace(key, "***")
        return {"ok": False, "error": f"{type(e).__name__}: {msg}"[:200]}
    return {"ok": True, "latency_ms": int((time.time() - t0) * 1000),
            "models": len(d.get("models", [])), "at": time.time()}
