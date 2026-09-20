"""Macro storage — shareable half on disk, local half beside it.

Layout:

    macros/
      <id>/
        macro.json         shareable: steps, locators, intents, requirements
        local.json         private: trust, stats, locator hit counts
        recording/         events, frames — never shared

The split is physical, not a convention, so publishing a macro is copying one
file. There is nothing to scrub because nothing sensitive was ever written on
that side.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from . import paths

from .schema import (LOCATOR_PRIORITY, LocalState, Locator, LocatorType, Macro,
                     MacroStats, Param, PROMOTION_CLEAN_RUNS, Step, Trust,
                     Verify, reliability)

ROOT = paths.data("macros")


# ---------------------------------------------------------------------------
# (de)serialisation
# ---------------------------------------------------------------------------

def _step_from(d: dict) -> Step:
    from .schema import Action
    v = d.get("verify")
    verify = None
    if v:
        verify = Verify(
            appears=[Locator(LocatorType(l["type"]), l["value"])
                     for l in v.get("appears", [])],
            disappears=[Locator(LocatorType(l["type"]), l["value"])
                        for l in v.get("disappears", [])],
            window_title_regex=v.get("window_title_regex"),
            url_regex=v.get("url_regex"),
            timeout_s=v.get("timeout_s", 8.0))
    return Step(
        action=Action(d["action"]),
        intent=d.get("intent", ""),
        locators=[Locator(LocatorType(l["type"]), l["value"])
                  for l in d.get("locators", [])],
        args=d.get("args", {}),
        verify=verify,
        optional=d.get("optional", False),
        timeout_s=d.get("timeout_s", 15.0))


def macro_from(d: dict) -> Macro:
    from .schema import TargetApp
    m = Macro(description=d["description"], name=d["name"], id=d["id"])
    m.version = d.get("version", 1)
    m.schema_version = d.get("schema_version", 1)
    m.steps = [_step_from(s) for s in d.get("steps", [])]
    m.params = [Param(**p) for p in d.get("params", [])]
    m.requires_symbols = d.get("requires_symbols", [])
    m.requires_capabilities = d.get("requires_capabilities", [])
    m.requires_secrets = d.get("requires_secrets", [])
    m.permissions = d.get("permissions", [])
    m.target_apps = [TargetApp(**t) for t in d.get("target_apps", [])]
    m.author = d.get("author", "")
    m.license = d.get("license", "")
    m.tags = d.get("tags", [])
    m.created_at = d.get("created_at", time.time())
    return m


def local_from(d: dict) -> LocalState:
    ls = LocalState(macro_id=d["macro_id"])
    ls.trust = Trust(d.get("trust", "draft"))
    ls.stats = MacroStats(**d.get("stats", {}))
    ls.locator_stats = d.get("locator_stats", {})
    ls.adaptation_log = d.get("adaptation_log", [])
    ls.artifact_dir = d.get("artifact_dir")
    ls.machine_fingerprint = d.get("machine_fingerprint", "")
    ls.origin = d.get("origin", "local")
    return ls


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class Store:
    def __init__(self, root: Path | str = ROOT):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, macro_id: str) -> Path:
        return self.root / macro_id

    # -- write -----------------------------------------------------------
    def save(self, macro: Macro, local: LocalState | None = None) -> Path:
        d = self.path(macro.id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "macro.json").write_text(
            json.dumps(macro.to_dict(), indent=2, default=str), encoding="utf-8")
        if local is not None:
            (d / "local.json").write_text(
                json.dumps(asdict(local), indent=2, default=str), encoding="utf-8")
        return d

    def save_local(self, local: LocalState) -> None:
        d = self.path(local.macro_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "local.json").write_text(
            json.dumps(asdict(local), indent=2, default=str), encoding="utf-8")

    # -- read ------------------------------------------------------------
    def load(self, macro_id: str) -> tuple[Macro, LocalState]:
        d = self.path(macro_id)
        macro = macro_from(json.loads((d / "macro.json").read_text(encoding="utf-8")))
        lp = d / "local.json"
        local = (local_from(json.loads(lp.read_text(encoding="utf-8")))
                 if lp.exists() else LocalState(macro_id=macro_id))
        return macro, local

    def list(self) -> list[dict]:
        out = []
        for d in sorted(self.root.iterdir()):
            mp = d / "macro.json"
            if not mp.is_dir() and mp.exists():
                try:
                    m = json.loads(mp.read_text(encoding="utf-8"))
                    lp = d / "local.json"
                    l = json.loads(lp.read_text(encoding="utf-8")) if lp.exists() else {}
                    out.append({
                        "id": m["id"], "name": m["name"],
                        "description": m["description"],
                        "steps": len(m.get("steps", [])),
                        "trust": l.get("trust", "draft"),
                        "runs": l.get("stats", {}).get("runs", 0),
                        "success_rate": _rate(l.get("stats", {})),
                        "tags": m.get("tags", []),
                        "requires": m.get("requires_capabilities", []),
                    })
                except Exception:
                    continue
        return out

    def delete(self, macro_id: str) -> bool:
        import shutil
        d = self.path(macro_id)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            return True
        return False

    # -- sharing ---------------------------------------------------------
    def export(self, macro_id: str, dest: Path | str) -> Path:
        """Copy ONLY the shareable half. Nothing to scrub by construction."""
        dest = Path(dest)
        macro, _ = self.load(macro_id)
        dest.write_text(json.dumps(macro.to_dict(), indent=2, default=str),
                        encoding="utf-8")
        return dest

    def import_macro(self, src: Path | str, origin: str = "imported") -> str:
        """Bring in someone else's macro. It always lands untrusted.

        Trust is per machine and earned by running here; it can never ride
        along inside a file, or a published macro could declare itself safe.
        """
        data = json.loads(Path(src).read_text(encoding="utf-8"))
        macro = macro_from(data)
        local = LocalState(macro_id=macro.id, trust=Trust.DRAFT,
                           origin=f"{origin}:{Path(src).name}")
        self.save(macro, local)
        return macro.id


def _rate(stats: dict) -> float:
    runs = stats.get("runs", 0)
    return round(stats.get("successes", 0) / runs, 2) if runs else 0.0


# ---------------------------------------------------------------------------
# Locator ordering from local statistics
# ---------------------------------------------------------------------------

def order_for(local: LocalState, step_idx: int, n_locators: int) -> list[int]:
    """Rank a step's locators by measured reliability, then by prior.

    The shareable macro carries the author's ordering; this machine's own
    experience refines it without writing anything back into the shared file.
    """
    def key(i: int):
        return (-local.rank(step_idx, i), i)
    return sorted(range(n_locators), key=key)


def promote(local: LocalState) -> Trust:
    """Advance the trust ladder. Failure always demotes."""
    s = local.stats
    if local.trust is Trust.QUARANTINED:
        return local.trust
    if s.failures and local.trust is Trust.TRUSTED:
        local.trust = Trust.SUPERVISED
    elif (local.trust is Trust.SUPERVISED
          and s.successes >= PROMOTION_CLEAN_RUNS
          and s.failures == 0):
        local.trust = Trust.TRUSTED
    elif local.trust is Trust.DRAFT and s.successes >= 1:
        local.trust = Trust.SUPERVISED
    return local.trust
