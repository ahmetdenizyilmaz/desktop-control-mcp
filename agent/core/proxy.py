"""The local LiteLLM proxy, run by the app when the app needs it.

Claude Code speaks only Anthropic's wire format. Anything else — a local
model, Gemini — sits behind LiteLLM translating on a loopback port, and until
now the person had to start that by hand in a terminal before every session,
with any provider key exported in that terminal first. So "save the Gemini key
in Settings" could not mean anything: the key lived in the app's vault, and
the proxy was a separate process that never saw it.

Here the app owns the proxy. If the configured endpoint is on this machine
and nothing answers there, and a LiteLLM install sits beside the app, it is
started with the vault's provider keys in its environment, and stopped when
the app closes. One the person started themselves is left alone — and they
are told it will not see a key saved here until they restart it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from . import noconsole, paths

_lock = threading.Lock()
_proc: "subprocess.Popen | None" = None
_log = None
#: The provider keys the running proxy was started with. A key saved since
#: cannot reach a process that is already up — a restart is the only way in.
_started_env: dict = {}
_status_memo: tuple[float, str, bool] = (0.0, "", False)
#: The window polls status every 900ms; the probe is remembered this long.
STATUS_MEMO_S = 5.0


def install_root() -> Path:
    """Where litellm-venv\\ and litellm-config.yaml live: the project folder
    in development, the folder above dist\\ for the packaged exe."""
    if paths.FROZEN:
        return Path(sys.executable).resolve().parent.parent
    return Path(__file__).resolve().parents[2]


def exe() -> Path:
    return install_root() / "litellm-venv" / "Scripts" / "litellm.exe"


def config() -> Path:
    return install_root() / "litellm-config.yaml"


def installed() -> bool:
    return exe().exists() and config().exists()


def loopback_port(url: str) -> int | None:
    """The port if the URL points at this machine, else None."""
    try:
        u = urlparse(url if "://" in url else "http://" + url)
        if (u.hostname or "").lower() not in ("localhost", "127.0.0.1", "::1"):
            return None
        return u.port or 80
    except Exception:
        return None


def reachable(url: str, timeout: float = 2.0) -> bool:
    url = url.strip().rstrip("/").replace("://localhost:", "://127.0.0.1:")
    try:
        req = urllib.request.Request(
            url + "/v1/models",
            headers={"Authorization": "Bearer local", "User-Agent": "AGIAgent"})
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception:
        return False


def ours() -> bool:
    return _proc is not None and _proc.poll() is None


def _close_log() -> None:
    global _log
    if _log is not None:
        try:
            _log.close()
        except Exception:
            pass
        _log = None


def ensure(url: str, env: dict | None = None, wait_s: float = 45.0,
           say=None) -> dict:
    """Make the endpoint answer, starting LiteLLM if that is what it takes.

    Returns {"ok", "started", "detail"}. `say`, if given, is told when a
    start is happening — it takes several seconds and should not look like
    a hang.
    """
    global _proc, _log, _started_env
    with _lock:
        if ours() and (env or {}) != _started_env:
            if say:
                say("Restarting the local proxy with the saved key…")
            _stop_locked()
        if reachable(url):
            return {"ok": True, "started": False, "detail": "already running"}
        port = loopback_port(url)
        if port is None:
            return {"ok": False, "started": False,
                    "detail": "the endpoint is not on this machine, so there "
                              "is nothing here to start"}
        if not installed():
            return {"ok": False, "started": False,
                    "detail": f"no LiteLLM install next to the app (looked for "
                              f"{exe()})"}
        log_path = paths.data("workspace", "litellm.log")
        if not ours():
            if say:
                say(f"Starting the local proxy on port {port}…")
            log_path.parent.mkdir(parents=True, exist_ok=True)
            _close_log()
            try:
                _log = open(log_path, "ab")
                # LiteLLM prints a banner with characters outside cp1252, and
                # with its output going to a file rather than a console Python
                # picks that codec — and the proxy died on its own banner
                # (exit 3, UnicodeEncodeError) before it could listen. UTF-8
                # stdio, stated, not hoped for.
                child_env = {**os.environ, "PYTHONUTF8": "1",
                             "PYTHONIOENCODING": "utf-8", **(env or {})}
                _proc = subprocess.Popen(
                    [str(exe()), "--config", str(config()),
                     "--host", "127.0.0.1", "--port", str(port)],
                    cwd=str(install_root()),
                    env=child_env,
                    stdout=_log, stderr=subprocess.STDOUT,
                    creationflags=noconsole.flags())
                _started_env = dict(env or {})
            except Exception as e:
                _close_log()
                _proc = None
                return {"ok": False, "started": False,
                        "detail": f"could not start LiteLLM: "
                                  f"{type(e).__name__}: {e}"}
        deadline = time.time() + wait_s
        while time.time() < deadline:
            if _proc is not None and _proc.poll() is not None:
                code = _proc.returncode
                _proc = None
                _close_log()
                return {"ok": False, "started": True,
                        "detail": f"LiteLLM exited with code {code} — see "
                                  f"{log_path}"}
            if reachable(url, timeout=1.5):
                return {"ok": True, "started": True,
                        "detail": f"started on port {port}"}
            time.sleep(1.0)
        return {"ok": False, "started": True,
                "detail": f"LiteLLM did not answer within {int(wait_s)}s — "
                          f"see {log_path}"}


def _stop_locked(timeout_s: float = 8.0) -> bool:
    global _proc, _started_env
    p = _proc
    if p is None:
        return False
    _proc = None
    _started_env = {}
    if p.poll() is None:
        try:
            p.terminate()
            p.wait(timeout=timeout_s)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    _close_log()
    return True


def stop(timeout_s: float = 8.0) -> bool:
    """End the proxy this app started. One it did not start is untouched."""
    with _lock:
        return _stop_locked(timeout_s)


def restart(url: str, env: dict | None = None, wait_s: float = 45.0,
            say=None) -> dict:
    stop()
    return ensure(url, env, wait_s, say)


def status(url: str) -> dict:
    """Cheap enough to poll: the reachability probe is remembered briefly."""
    global _status_memo
    now = time.time()
    t, u, r = _status_memo
    if u != url or now - t > STATUS_MEMO_S:
        r = reachable(url, timeout=1.0) if url else False
        _status_memo = (now, url, r)
    return {"reachable": r, "ours": ours(), "installed": installed(),
            "loopback": loopback_port(url) is not None}
