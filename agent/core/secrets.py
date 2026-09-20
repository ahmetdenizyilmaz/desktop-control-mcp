"""Credential vault — encrypted by Windows, to your login, on this machine.

Uses DPAPI (CryptProtectData) rather than a password-derived key, so there is
no master password to lose and no key material for this app to look after:
Windows ties the ciphertext to your user account. The consequence is
deliberate — **a vault file copied to another machine cannot be read**, which
is exactly the property a shared macro needs.

Jobs never contain a secret. They contain `secret://gmail-password`, and the
value is fetched at the moment it is used. So publishing a job cannot leak a
credential, and a job downloaded from someone else prompts for its own.

What this is not: a defence against you-with-a-debugger. Anything running as
your user can call CryptUnprotectData too. It protects against the file being
read — by another user, from a backup, from a synced folder, or by a macro
that got shared — not against local code you have already chosen to run.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from . import paths

VAULT_FILE = paths.data("vault.json")
SECRET_RE = re.compile(r"^secret://([A-Za-z0-9._:-]+)$")

crypt32 = ctypes.windll.crypt32
kernel32 = ctypes.windll.kernel32

CRYPTPROTECT_UI_FORBIDDEN = 0x01


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _extract(blob: DATA_BLOB) -> bytes:
    out = ctypes.string_at(blob.pbData, blob.cbData)
    kernel32.LocalFree(blob.pbData)
    return out


class VaultError(Exception):
    pass


def _protect(plain: str, entropy: str = "") -> bytes:
    inp = _blob(plain.encode("utf-8"))
    ent = _blob(entropy.encode("utf-8")) if entropy else None
    out = DATA_BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(inp), "desktop-agent",
        ctypes.byref(ent) if ent else None,
        None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        raise VaultError(f"CryptProtectData failed (err {kernel32.GetLastError()})")
    return _extract(out)


def _unprotect(cipher: bytes, entropy: str = "") -> str:
    inp = _blob(cipher)
    ent = _blob(entropy.encode("utf-8")) if entropy else None
    out = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(inp), None,
        ctypes.byref(ent) if ent else None,
        None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        raise VaultError(
            "CryptUnprotectData failed — this vault was written by a different "
            "Windows account or on a different machine, and cannot be read here.")
    return _extract(out).decode("utf-8")


# ---------------------------------------------------------------------------

@dataclass
class SecretInfo:
    name: str
    note: str = ""
    created_at: float = 0.0
    used_at: float | None = None
    uses: int = 0


class Vault:
    def __init__(self, path: Path | str = VAULT_FILE):
        self.path = Path(path)

    # -- storage ---------------------------------------------------------
    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "secrets": {}}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"version": 1, "secrets": {}}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # -- api -------------------------------------------------------------
    def put(self, name: str, value: str, note: str = "") -> SecretInfo:
        """Store a secret. Overwrites silently — rotation is the normal case."""
        if not name:
            raise VaultError("a secret needs a name")
        data = self._read()
        # Entropy binds the ciphertext to this name, so a blob lifted from one
        # entry cannot be decrypted as another.
        blob = _protect(value, entropy=f"desktop-agent:{name}")
        existing = data["secrets"].get(name, {})
        data["secrets"][name] = {
            "cipher": blob.hex(),
            "note": note or existing.get("note", ""),
            "created_at": existing.get("created_at") or time.time(),
            "uses": existing.get("uses", 0),
            "used_at": existing.get("used_at"),
        }
        self._write(data)
        return self.info(name)

    def peek(self, name: str) -> str:
        """Decrypt WITHOUT recording a use.

        `get` counts a use and writes that back to disk, which is right when a
        credential is actually typed into something and wrong everywhere else.
        Redaction reads every secret on every outbound message; counting those
        made "12 uses" mean "12 progress messages went past", not "typed 12
        times" — and the write on each read invalidated the very cache meant
        to stop the repeated decryption.
        """
        entry = self._read()["secrets"].get(name)
        if entry is None:
            raise VaultError(f"no secret named {name!r}")
        return _unprotect(bytes.fromhex(entry["cipher"]),
                          entropy=f"desktop-agent:{name}")

    def get(self, name: str) -> str:
        """Decrypt and record that it was used. For actually using it."""
        data = self._read()
        entry = data["secrets"].get(name)
        if entry is None:
            raise VaultError(f"no secret named {name!r}")
        value = _unprotect(bytes.fromhex(entry["cipher"]),
                           entropy=f"desktop-agent:{name}")
        entry["uses"] = entry.get("uses", 0) + 1
        entry["used_at"] = time.time()
        self._write(data)
        return value

    def has(self, name: str) -> bool:
        return name in self._read()["secrets"]

    def delete(self, name: str) -> bool:
        data = self._read()
        if name in data["secrets"]:
            del data["secrets"][name]
            self._write(data)
            return True
        return False

    def info(self, name: str) -> SecretInfo:
        e = self._read()["secrets"].get(name, {})
        return SecretInfo(name=name, note=e.get("note", ""),
                          created_at=e.get("created_at", 0.0),
                          used_at=e.get("used_at"), uses=e.get("uses", 0))

    def list(self) -> list[SecretInfo]:
        """Names and metadata only — never values."""
        return [self.info(n) for n in sorted(self._read()["secrets"])]


vault = Vault()


# ---------------------------------------------------------------------------
# secret:// resolution
# ---------------------------------------------------------------------------

def is_ref(text: str) -> bool:
    return bool(SECRET_RE.match((text or "").strip()))


def ref_name(text: str) -> str | None:
    m = SECRET_RE.match((text or "").strip())
    return m.group(1) if m else None


def resolve(text: str, v: Vault | None = None) -> str:
    """Turn a secret:// reference into its value. Anything else passes through."""
    name = ref_name(text)
    if name is None:
        return text
    return (v or vault).get(name)


def required_by(steps: list) -> list[str]:
    """Which secrets a job needs, without reading any of them."""
    out: set[str] = set()
    for s in steps or []:
        args = getattr(s, "args", None) or (s.get("args") if isinstance(s, dict) else {})
        for val in (args or {}).values():
            n = ref_name(str(val))
            if n:
                out.add(n)
    return sorted(out)


def redact(text: str, v: Vault | None = None) -> str:
    """Strip any stored secret value out of text before it is logged or shown.

    Values reach places they should not: a page's text, an error message, a
    transcript the user later shares. Everything user-visible passes through
    here so a credential cannot be echoed back by accident.
    """
    vv = v or vault
    out = text or ""
    for name, val in _known_values(vv):
        if val and len(val) >= 4 and val in out:
            out = out.replace(val, f"«{name}»")
    return out


#: Decrypted values, keyed by the vault file's modification time. Redaction ran
#: on every outbound message and decrypted the WHOLE vault each time — measured
#: at 187ms with a dozen secrets, growing linearly, and pulling every stored
#: password through memory to check a progress line that almost never contains
#: one. The values are already in memory during that check either way; caching
#: them until the file changes does the work once instead of every message.
_value_cache: tuple[float, int, list[tuple[str, str]]] = (0.0, 0, [])


def _known_values(vv: "Vault") -> list[tuple[str, str]]:
    global _value_cache
    try:
        stamp = vv.path.stat().st_mtime
        size = vv.path.stat().st_size
    except OSError:
        return []
    cached_stamp, cached_size, values = _value_cache
    if stamp == cached_stamp and size == cached_size and values:
        return values
    values = []
    for info in vv.list():
        try:
            values.append((info.name, vv.peek(info.name)))
        except VaultError:
            continue
    _value_cache = (stamp, size, values)
    return values
