"""Where scripts come from: a local folder, and a URL pinned by hash.

Loading a script from a server means executing data someone else controls,
on a machine where the runner can click. That is worth being blunt about,
because the mitigation is not obvious and the obvious mitigations do not
work:

    HTTPS                 proves you reached the right HOST, not that the
                          host serves what it served yesterday.
    a git URL             the branch moves. `main` today is not `main`
                          tomorrow, and nothing tells you it changed.
    reviewing it once     reviews the copy you fetched, not the next one.

So the reference carries a **sha256 of the exact content that was
reviewed**, and content that does not hash to it is refused. That reduces
"trust this server, forever, for anything" to "trust these exact bytes" —
the guarantee a git commit id gives, and the only one strong enough for
something that drives a mouse.

Consequences that fall out of pinning, all of them good:

  * the cache is keyed by hash, so a pinned script is fetched **once** and
    is thereafter a local file. Offline runs work.
  * a changed remote is a loud error at load time, not a surprise mid-run.
  * updating a pin is a visible edit to the calling script — it shows up
    in a diff, which is where a change of behaviour belongs.

Two more rules, both about timing and trust:

  **Everything resolves before anything runs.** `resolve()` walks the whole
  call graph, fetches, verifies, parses, and checks depth and cycles — and
  only then does the runner touch the screen. A script cannot fetch
  halfway through a checkout, which also means a network failure costs you
  nothing rather than a half-finished basket.

  **An import is always a draft**, whatever it says about itself, and the
  effective trust of a run is the MINIMUM over every script reachable from
  the entry point. Without that, trust launders: import an unreviewed
  script, have a trusted local one call it, and unreviewed steps run
  unattended.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .schema import (MAX_CALL_DEPTH, IMPORT_TRUST, Script, ScriptError, Step,
                     Trust, parse)

#: Where local scripts live. One JSON file per script; the name in the file
#: is what `call` refers to, and the path is only where it happens to sit.
SCRIPTS = Path(__file__).resolve().parent / "scripts"

#: Verified remote content, keyed by hash. Content-addressed, so it is
#: never stale and never needs invalidating: a different pin is a
#: different file.
CACHE = Path(__file__).resolve().parent / ".cache"

#: A script is text. Anything this large is not a script, and refusing it
#: early keeps a hostile or broken URL from being a memory problem.
MAX_FETCH_BYTES = 1 << 20  # 1 MiB

FETCH_TIMEOUT_S = 15.0

#: Least to most trusted. `min` over a call chain needs an order, and the
#: enum is a string enum, so the order has to be written down.
TRUST_ORDER = (Trust.QUARANTINED, Trust.DRAFT, Trust.SUPERVISED,
               Trust.TRUSTED)


def weaker(a: Trust, b: Trust) -> Trust:
    return a if TRUST_ORDER.index(a) <= TRUST_ORDER.index(b) else b


class FetchError(ScriptError):
    """A remote script could not be fetched, or did not match its pin."""


@dataclass
class Resolved:
    """An entry script plus every script it can reach, all verified.

    Handed to the runner as a closed set: once this exists, running needs
    no network and no further lookups.
    """

    entry: Script
    scripts: dict[str, Script] = field(default_factory=dict)
    #: Minimum trust over the whole graph — what the run is actually
    #: allowed to do.
    trust: Trust = Trust.DRAFT
    #: Name → where it came from, for the report.
    origins: dict[str, str] = field(default_factory=dict)
    #: Names that arrived from a URL. The runner supervises these even when
    #: everything else about the run says it need not.
    imported: set[str] = field(default_factory=set)

    def get(self, name: str) -> Script:
        got = self.scripts.get(name)
        if got is None:
            raise ScriptError(f"no script named {name!r} in this run")
        return got


class Library:
    """Resolves script names to verified scripts.

    `allow_fetch=False` is the honest default for anything unattended: it
    turns a missing pin into an error instead of a download. Turn it on
    when you mean to install something.
    """

    def __init__(self, roots: list[Path | str] | None = None,
                 cache: Path | str = CACHE,
                 allow_fetch: bool = False,
                 allow_hosts: tuple[str, ...] = (),
                 strict: bool = True) -> None:
        self.roots = [Path(r) for r in (roots or [SCRIPTS])]
        for r in self.roots:
            r.mkdir(parents=True, exist_ok=True)
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.allow_fetch = allow_fetch
        self.allow_hosts = tuple(h.lower() for h in allow_hosts)
        self._local: dict[str, Script] | None = None
        #: strict=False: one unreadable or duplicate file is reported here
        #: and skipped, instead of making every other script unreachable. An
        #: app that saves scripts Claude wrote needs that - a single bad
        #: generated file must not stop recognition for the whole library.
        self.strict = strict
        self.errors: list[str] = []
        self._paths: dict[str, Path] = {}

    # -- local -------------------------------------------------------------

    def _scan(self) -> dict[str, Script]:
        """Read every local script once, keyed by its declared name.

        Keyed by the `name` INSIDE the file, not by filename, so moving a
        file does not break callers and two files cannot silently claim the
        same name — the second one is an error rather than a coin toss.
        """
        found: dict[str, Script] = {}
        where: dict[str, Path] = {}
        self.errors = []
        for root in self.roots:
            for path in sorted(root.rglob("*.json")):
                # Deleted scripts and old versions live beside the library,
                # not in it.
                rel = path.relative_to(root).parts
                if rel and rel[0] in (".trash", ".history"):
                    continue
                try:
                    try:
                        raw = json.loads(path.read_text(encoding="utf-8"))
                    except Exception as exc:
                        raise ScriptError(f"{path}: not readable JSON — {exc}")
                    script = parse(raw, origin=str(path))
                    # A local file is not an import; it is as trusted as its
                    # own record says.
                    script.trust = _declared_trust(raw)
                    if script.name in found:
                        raise ScriptError(
                            f"two scripts both named {script.name!r}: "
                            f"{where[script.name]} and {path}")
                except ScriptError as exc:
                    if self.strict:
                        raise
                    self.errors.append(str(exc))
                    continue
                found[script.name] = script
                where[script.name] = path
        self._paths = where
        return found

    def path_of(self, name: str) -> Path | None:
        """The file a local script was loaded from."""
        self.local()
        return self._paths.get(name)

    def delete(self, name: str) -> Path:
        """Move a script to `.trash/` beside the library. Never unlinks:
        a script someone spent a run teaching is not deleted by one click."""
        path = self.path_of(name)
        if path is None:
            raise ScriptError(f"no local script named {name!r}")
        root = next(r for r in self.roots
                    if r in path.parents or r == path.parent)
        dest = root / ".trash" / f"{time.strftime('%Y%m%d-%H%M%S')}-" \
            f"{path.relative_to(root).as_posix().replace('/', '__')}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        path.replace(dest)
        self.reload()
        return dest

    def local(self) -> dict[str, Script]:
        if self._local is None:
            self._local = self._scan()
        return self._local

    def reload(self) -> None:
        self._local = None

    def save(self, script: Script, root: Path | None = None) -> Path:
        """Write a script to the local folder. Used by the recorder."""
        root = root or self.roots[0]
        safe = "".join(c if c.isalnum() or c in "-_/" else "_"
                       for c in script.name)
        path = root / f"{safe}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(to_json(script), indent=2,
                                   ensure_ascii=False), encoding="utf-8")
        self.reload()
        return path

    # -- remote ------------------------------------------------------------

    def fetch(self, url: str, sha256: str) -> Script:
        """Get a pinned script, from the cache if its hash is already here.

        The hash is checked against the bytes as received, BEFORE they are
        parsed — so malformed or hostile content never reaches the parser
        on the strength of having come from the right URL.
        """
        digest = sha256.lower()
        cached = self.cache / f"{digest}.json"
        if cached.exists():
            body = cached.read_bytes()
            # Re-verified on every load: the cache is a file on disk like
            # any other, and checking a hash is microseconds.
            if hashlib.sha256(body).hexdigest() != digest:
                cached.unlink(missing_ok=True)
            else:
                return _from_bytes(body, origin=f"{url} (cached)")

        if not self.allow_fetch:
            raise FetchError(
                f"{url} is not in the cache and fetching is off. Run with "
                f"allow_fetch=True once to install it, or commit the script "
                f"locally.")

        parts = urlparse(url)
        if parts.scheme != "https":
            raise FetchError(f"{url}: only https is allowed")
        host = (parts.hostname or "").lower()
        if self.allow_hosts and host not in self.allow_hosts:
            raise FetchError(f"{host} is not in the allowed hosts "
                             f"{list(self.allow_hosts)}")

        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "desktop-controller-scripts/1",
                "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
                body = resp.read(MAX_FETCH_BYTES + 1)
        except urllib.error.URLError as exc:
            raise FetchError(f"{url}: {exc}") from exc
        if len(body) > MAX_FETCH_BYTES:
            raise FetchError(f"{url}: larger than {MAX_FETCH_BYTES} bytes")

        got = hashlib.sha256(body).hexdigest()
        if got != digest:
            raise FetchError(
                f"{url} does not match its pin.\n"
                f"  expected {digest}\n"
                f"  received {got}\n"
                f"The content changed since it was reviewed. Read the new "
                f"version, and update the sha256 in the calling script only "
                f"if you are satisfied with it.")

        cached.write_bytes(body)
        return _from_bytes(body, origin=url)

    # -- the whole graph ---------------------------------------------------

    def resolve(self, name: str) -> Resolved:
        """Load `name` and everything it calls, verified, before any action.

        Depth and cycles are refused HERE. A cycle discovered at run time is
        a loop that clicks, and by the time you notice it has already done
        something a hundred times.
        """
        local = self.local()
        entry = local.get(name)
        if entry is None:
            raise ScriptError(
                f"no script named {name!r}. Known: "
                f"{', '.join(sorted(local)) or '(none)'}")

        out = Resolved(entry=entry, scripts={name: entry},
                       trust=entry.trust, origins={name: entry.origin})

        # Iterative depth-first walk, carrying the path so a cycle can be
        # reported as the chain that causes it rather than as a depth
        # number nobody can act on.
        stack: list[tuple[Script, tuple[str, ...]]] = [(entry, (name,))]
        while stack:
            script, path = stack.pop()
            for step in _walk(script.steps):
                if step.do != "call":
                    continue
                target = step.script
                if target in path:
                    chain = " → ".join((*path, target))
                    raise ScriptError(f"call cycle: {chain}")
                if len(path) >= MAX_CALL_DEPTH:
                    raise ScriptError(
                        f"calls nested deeper than {MAX_CALL_DEPTH}: "
                        f"{' → '.join((*path, target))}")

                if target not in out.scripts:
                    if step.source:
                        child = self.fetch(step.source, step.sha256)
                        if child.name != target:
                            raise ScriptError(
                                f"{script.name} calls {target!r} but "
                                f"{step.source} contains {child.name!r}")
                        # However the file describes itself, it arrived
                        # from outside. Trust is local knowledge.
                        child.trust = IMPORT_TRUST
                        out.imported.add(target)
                    else:
                        child = local.get(target)
                        if child is None:
                            raise ScriptError(
                                f"{script.name} calls {target!r}, which is "
                                f"not in the library and has no source")
                    out.scripts[target] = child
                    out.origins[target] = child.origin
                    out.trust = weaker(out.trust, child.trust)

                child = out.scripts[target]
                missing = child.declared_params() - set(step.with_args)
                if missing:
                    raise ScriptError(
                        f"{script.name} calls {target!r} without "
                        f"{sorted(missing)} — a called script sees only its "
                        f"`with:`, so every parameter must be passed")
                extra = set(step.with_args) - child.declared_params()
                if extra:
                    raise ScriptError(
                        f"{script.name} passes {sorted(extra)} to {target!r}, "
                        f"which declares {sorted(child.declared_params())}")
                stack.append((child, (*path, target)))
        return out


def _walk(steps: list[Step]):
    """Every step, including the ones inside if/repeat."""
    for step in steps:
        yield step
        yield from _walk(step.then)
        yield from _walk(step.otherwise)
        yield from _walk(step.steps)


def _declared_trust(raw: dict) -> Trust:
    try:
        return Trust(str(raw.get("trust", "draft")))
    except ValueError:
        return Trust.DRAFT


def _from_bytes(body: bytes, origin: str) -> Script:
    try:
        raw = json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise FetchError(f"{origin}: not readable JSON — {exc}") from exc
    return parse(raw, origin=origin)


def pin(url: str, timeout: float = FETCH_TIMEOUT_S) -> str:
    """Fetch a URL and print its sha256, so a pin can be written by hand.

    Deliberately separate from `fetch`: computing a pin is the moment you
    are supposed to READ the script. Making it one command that both
    installs and trusts would remove exactly the step that matters.
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": "desktop-controller-scripts/1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read(MAX_FETCH_BYTES + 1)
    if len(body) > MAX_FETCH_BYTES:
        raise FetchError(f"{url}: larger than {MAX_FETCH_BYTES} bytes")
    return hashlib.sha256(body).hexdigest()


# --------------------------------------------------------------------------
# Serialisation, for the recorder and for `save`
# --------------------------------------------------------------------------

def _sel_json(sel) -> dict | str:
    if sel is None:
        return {}
    out: dict = {}
    if sel.text:
        out["text"] = sel.text
    if sel.kind:
        out["kind"] = sel.kind
    if sel.match != "contains":
        out["match"] = sel.match
    if sel.any:
        out["any"] = list(sel.any)
    if sel.near:
        out["near"] = sel.near
    if sel.nth:
        out["nth"] = sel.nth
    if sel.id:
        out["id"] = sel.id
    if sel.icon:
        out["icon"] = sel.icon
    if sel.area:
        out["area"] = sel.area
    if sel.nearest:
        out["nearest"] = sel.nearest
    # A bare string round-trips to Selector(text=...), and reads better in
    # the common case.
    if list(out) == ["text"]:
        return sel.text
    return out


def step_json(step: Step) -> dict:
    out: dict = {"do": step.do}
    if step.target is not None:
        out["target"] = _sel_json(step.target)
    if step.into is not None:
        out["into"] = _sel_json(step.into)
    if step.to is not None:
        out["to"] = _sel_json(step.to)
    if step.do == "type":
        out["value"] = step.value
        if step.then_key:
            out["then"] = step.then_key
        if step.lines_from:
            out["lines_from"] = step.lines_from
    if step.do == "wait_message":
        if step.words:
            out["words"] = list(step.words)
        if step.stop_words:
            out["stop_words"] = list(step.stop_words)
        if step.code_as:
            out["code_as"] = step.code_as
        if step.stop_after:
            out["stop_after"] = step.stop_after
        if step.authorizes:
            out["authorizes"] = True
        out["timeout"] = step.timeout
    if step.do == "assist":
        out["goal"] = step.goal
        out["rounds"] = step.rounds
    if step.keys:
        out["keys"] = step.keys
    if step.do in ("open", "focus"):
        if step.app:
            out["app"] = step.app
        if step.url:
            out["url"] = step.url
        if step.window:
            out["window"] = step.window
        if not step.maximize:
            out["maximize"] = False
        if step.do == "open" and not step.reuse:
            out["reuse"] = False
        out["timeout"] = step.timeout
    if step.do == "scroll":
        out["direction"] = step.direction
        out["amount"] = step.amount
    if step.do == "wait":
        out["seconds"] = step.seconds
    if step.do in ("wait_for", "expect", "gone") and step.timeout != 10.0:
        out["timeout"] = step.timeout
    if step.present is not None:
        out["present"] = _sel_json(step.present)
    if step.then:
        out["then"] = [step_json(s) for s in step.then]
    if step.otherwise:
        out["else"] = [step_json(s) for s in step.otherwise]
    if step.times:
        out["times"] = step.times
    if step.for_each:
        out["for_each"] = list(step.for_each)
        out["as"] = step.as_name
    if step.steps:
        out["steps"] = [step_json(s) for s in step.steps]
    if step.script:
        out["script"] = step.script
        if step.with_args:
            out["with"] = dict(step.with_args)
        if step.source:
            out["source"] = step.source
            out["sha256"] = step.sha256
    if step.expect is not None:
        out["expect"] = _sel_json(step.expect)
    if step.gone is not None:
        out["gone"] = _sel_json(step.gone)
    if step.pick is not None:
        pk = {"goal": step.pick.goal}
        if step.pick.reject:
            pk["reject"] = step.pick.reject
        if step.pick.min_margin != 0.25:
            pk["min_margin"] = step.pick.min_margin
        if step.pick.on_unsure != "fail":
            pk["on_unsure"] = step.pick.on_unsure
        if step.pick.max_options != 10:
            pk["max_options"] = step.pick.max_options
        if step.pick.radius != 280:
            pk["radius"] = step.pick.radius
        out["pick"] = pk
    if step.optional:
        out["optional"] = True
    if step.note:
        out["note"] = step.note
    return out


def to_json(script: Script) -> dict:
    out: dict = {"name": script.name, "version": script.version}
    if script.description:
        out["description"] = script.description
    if script.params:
        out["params"] = dict(script.params)
    out["trust"] = script.trust.value
    for k, v in (script.extra or {}).items():
        if k not in out and k != "steps":
            out[k] = v
    out["steps"] = [step_json(s) for s in script.steps]
    return out
