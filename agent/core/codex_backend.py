"""Codex plans; the existing guarded script runner performs desktop actions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

settings = {}

def _run(args, **kwargs):
    """Poll cancellation while communicating; terminate the Windows process tree."""
    timeout = kwargs.pop('timeout')
    content = kwargs.pop('input')
    kwargs.pop('capture_output', None)
    deadline = time.monotonic() + timeout
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, **kwargs)
    try:
        while True:
            sdk = sys.modules.get('tools_sdk')
            if sdk is not None and sdk.should_stop():
                raise RuntimeError('Codex call stopped by user')
            if time.monotonic() >= deadline:
                raise TimeoutError('Codex call timed out')
            try:
                stdout, stderr = proc.communicate(content, timeout=0.25)
                return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                content = None
    finally:
        if proc.poll() is None:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                               capture_output=True, creationflags=0x08000000, timeout=10)
            else:
                proc.kill()
            proc.communicate()

def configure(value):
    global settings
    settings = dict(value or {})

def enabled():
    return settings.get('planning_engine', 'claude') == 'codex'

def executable(name):
    return shutil.which(name + '.cmd') or shutil.which(name)

def login_status(name):
    exe = executable(name)
    if not exe:
        return {'installed': False, 'logged_in': False, 'detail': 'Not installed'}
    args = ['login', 'status'] if name == 'codex' else ['auth', 'status']
    try:
        p = subprocess.run([exe, *args], capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=12,
                           creationflags=0x08000000 if os.name == 'nt' else 0)
        if name == 'claude':
            data = json.loads(p.stdout)
            ok = bool(data.get('loggedIn'))
            detail = data.get('authMethod', 'Unknown') if ok else 'Not logged in'
        else:
            raw = p.stdout + p.stderr
            ok = p.returncode == 0 and 'Logged in' in raw
            detail = 'ChatGPT login' if ok and 'ChatGPT' in raw else ('API key login' if ok else 'Not logged in')
        return {'installed': True, 'logged_in': ok, 'detail': detail}
    except Exception:
        return {'installed': True, 'logged_in': None, 'detail': 'Status unavailable'}

def call(prompt, *, system='', images=(), timeout=300, kind='write', record=True):
    exe = executable('codex')
    if not exe:
        raise RuntimeError('Codex CLI is not installed')
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='desktop-codex-') as folder:
        output = Path(folder) / 'reply.txt'
        args = [exe, 'exec', '--ephemeral', '--sandbox', 'read-only',
                '--skip-git-repo-check', '--json', '-C', folder,
                '--output-last-message', str(output)]
        model = str(settings.get('codex_model', '')).strip()
        if model:
            args += ['--model', model]
        for shot in images:
            args += ['--image', str(Path(shot).resolve())]
        args += ['-']
        full = ('Return instructions as data. Do not execute commands or control the desktop.\n'
                + system + '\n\n' + prompt)
        proc = _run(args, input=full, capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=timeout,
                              creationflags=0x08000000 if os.name == 'nt' else 0)
        if proc.returncode or not output.exists():
            raise RuntimeError('Codex call failed; check Codex login and model access')
        reply = output.read_text(encoding='utf-8').strip()
        if not reply:
            raise RuntimeError('Codex returned no answer')
    elapsed = time.monotonic() - start
    if record:
        from . import ledger, tasklog
        ledger.record(kind, elapsed, None, 'codex ' + (model or 'default'))
        tasklog.model('codex ' + (model or 'default'), kind, full, reply,
                      elapsed, None, shot=next(iter(images), None))
    return reply
