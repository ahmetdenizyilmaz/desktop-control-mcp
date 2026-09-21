"""A client for TypeSafe's Jev — a decision model, not a language model.

Jev takes a *state* (text or JSON) and a map of *typed questions*, and returns
one typed, calibrated answer per question in a single parallel pass. It does
not generate text. There is no prose, no rationale, no chain of thought — only
a probability distribution over an answer space you defined up front.

**Why this project wants one.** The engine already turns the screen into text
before anything decides anything: `locator.walk` yields a control table,
`browser.elements` yields an indexed page, `read_labels` yields OCR labels.
That was forced on us by measurement (STATE.md §3: UIA resolved 100% of 32
targets in 0.52s; the vision pipeline managed 44% in 84.24s). The happy
consequence is that Jev's hard limit — *text only, no images* — costs us
nothing. The state we would have to invent for it is the state we already
produce.

What remains expensive is the round trip to Claude. Every "which of these 60
controls is the one?", every "did that step actually work?", every "is this a
cookie banner or the checkout dialog?" currently costs a full model turn.
Those are not reasoning; they are recognition. Jev answers them in ~200ms for
~$0.00005, and answers a dozen of them for barely more than one, because
independent questions are evaluated in parallel against the same state.

So the division of labour this module is built for:

    Claude  — System 2. Reads the task, writes the plan, decides what the
              questions *are*, diagnoses anything that went wrong.
    Jev     — System 1. Answers those questions, over and over, inside the
              loop, at a rate that does not make the user wait.

The seam between them is **confidence**, not task type. Jev reports how
concentrated its distribution is; below a threshold the decision is handed
back up. That is the same trust ladder `replay.py` already runs on, with one
more rung near the bottom that is 50x cheaper than the rung above it.

**What this module deliberately does not do.** It does not decide anything
itself, and it does not know what a window is — see `fast.py` for the desktop
bindings. It also refuses, by default, to let an offline stub answer be acted
on (`Decision.real`). A missing API key must not degrade into a fast agent
clicking confidently at random; per the project's rule, it fails loudly rather
than plausibly.

Reference: https://docs.typesafe.ai — endpoint POST /v1/systemone, three
primitives (noul/choice/score), 64k context of which 32k is state budget,
$0.042/Mtok in and nothing out, 1200 req/min.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

#: The public TypeSafe endpoint. Override per-client for a gateway
#: (Cloudflare Workers AI, OpenRouter, Vercel) or a local recording proxy.
ENDPOINT = "https://api.typesafe.ai/v1/systemone"

#: Cloudflare Workers AI carries Jev as a partner model. This matters because
#: TypeSafe's own access is a waitlist with no self-serve signup, while a
#: Cloudflare account can be opened in a minute — so this is the route that
#: can be working today. The request is the same three fields wrapped in
#: Cloudflare's envelope, and the response comes back under `result`.
#:
#: Caveat worth knowing before relying on it: Jev is listed there as a
#: third-party provider and the model card points at the dashboard for
#: pricing rather than quoting Neurons, so it is unclear whether the free
#: 10,000 Neurons/day allocation covers it. Assume billing may need enabling.
CF_ENDPOINT = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run"
CF_MODEL = "typesafe/jev"

#: OpenRouter, added 2026-09-18, three days after launch. **This is the route
#: that works without a TypeSafe invite**, and it is how most of the public
#: projects were built during the first week.
#:
#: Note it is *not* the chat-completions API — that endpoint rejects the model
#: outright ("jev-1.13 is a decisions model and cannot be used with the
#: chat/completions endpoint"). It is a separate Decisions endpoint whose body
#: is the same three fields as TypeSafe's own, which is why this client needs
#: nothing but a different URL and model id to use it.
OR_ENDPOINT = "https://openrouter.ai/api/v1/systemone"
OR_MODEL = "jev-1.13"


#: `jev-latest` tracks the current release (jev-1.13.0 at time of writing).
#: Pin an exact version if a threshold in `fast.py` was tuned against it —
#: calibration is a property of the checkpoint, not of the API.
MODEL = "jev-latest"

#: Input only. Output is free: there is no generated text to charge for.
USD_PER_MTOK_IN = 0.042

#: Published limits. Enforced client-side too, because hitting 429 inside a
#: 5Hz loop costs more wall-clock than simply not sending the call.
REQUESTS_PER_MIN = 1200

#: Documented budget for `state` plus the longest question. The control table
#: for a window is ~2-4k tokens, so this is not a binding constraint for us;
#: it is here so `fast.py` can trim a pathological page instead of eating 422.
STATE_TOKEN_BUDGET = 32_000

#: Retried with backoff; anything else is surfaced immediately.
RETRY_STATUS = (429, 500, 502, 503, 504, 529)

#: How long an idle connection is held open. httpx defaults to 5s, which is
#: shorter than the gap between two steps of a task, so the default would put
#: a TLS handshake in front of most decisions.
#:
#: Measured from this machine via OpenRouter, 30 calls: the handshake costs
#: **1156ms against a warm 344ms** — the cold call is 3.4x the warm one, and
#: was the entire reason an early sample showed a 1.4s p95. Holding the
#: connection is the single cheapest latency win available here.
KEEPALIVE_S = 90.0

#: Where each backend keeps its credential, its default endpoint and its
#: default model id. Kept as data so adding the Vercel or Netlify gateway
#: later is a row, not a branch.
BACKENDS: dict[str, dict[str, str]] = {
    "typesafe":   {"env": "TYPESAFE_API_KEY",     "endpoint": ENDPOINT,
                   "model": MODEL},
    "openrouter": {"env": "OPENROUTER_API_KEY",   "endpoint": OR_ENDPOINT,
                   "model": OR_MODEL},
    "cloudflare": {"env": "CLOUDFLARE_API_TOKEN", "endpoint": CF_ENDPOINT,
                   "model": CF_MODEL},
}


class JevError(RuntimeError):
    """Any failure to obtain a decision."""


class JevRefused(JevError):
    """422 — the request was malformed. A bug here, not a transient fault."""


class JevUnavailable(JevError):
    """Out of retries, or no transport. Callers should escalate, not guess."""


# --------------------------------------------------------------------------
# Questions
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Noul:
    """A yes/no question. The answer is P(yes) — the probability *is* the
    confidence, so there is no separate field."""

    instructions: str
    #: Optional {"true": "what yes means", "false": "what no means"}. Worth
    #: writing: it is the difference between "urgent" and "urgent to *us*".
    criteria: Mapping[str, str] | None = None

    def payload(self) -> dict:
        p: dict[str, Any] = {"type": "noul", "instructions": self.instructions}
        if self.criteria:
            p["criteria"] = dict(self.criteria)
        return p


@dataclass(frozen=True)
class Choice:
    """Pick one of up to 255 named options.

    `criteria` maps option key -> description, or None when the key speaks for
    itself. Descriptions are worth far more than clever key names: the model
    reads them, and they are the only place to say what *distinguishes* two
    options that sound alike.
    """

    instructions: str
    criteria: Mapping[str, str | None]

    def payload(self) -> dict:
        if not self.criteria:
            raise ValueError("a Choice needs at least one option")
        if len(self.criteria) > 255:
            raise ValueError(f"a Choice takes at most 255 options, "
                             f"got {len(self.criteria)}")
        return {"type": "choice", "instructions": self.instructions,
                "criteria": dict(self.criteria)}


@dataclass(frozen=True)
class Score:
    """Rate the state against 2-10 *ordered, described* levels.

    The levels are a rubric, not a number line. "0 = calm, 1 = frustrated,
    2 = very angry" scores far better than "rate 0-10", because every level
    has a meaning the model can recognise rather than interpolate.
    """

    instructions: str
    criteria: Sequence[str]

    def payload(self) -> dict:
        n = len(self.criteria)
        if not 2 <= n <= 10:
            raise ValueError(f"a Score needs 2-10 levels, got {n}")
        return {"type": "score", "instructions": self.instructions,
                "criteria": list(self.criteria)}


Question = Noul | Choice | Score


# --------------------------------------------------------------------------
# Answers
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class NoulAnswer:
    p: float

    @property
    def yes(self) -> bool:
        return self.p >= 0.5

    @property
    def confidence(self) -> float:
        """How far from a coin flip, on the same 0-1 scale Choice/Score use.

        The API reports no confidence for a noul because the probability
        already carries it; this makes the three primitives gate alike.
        """
        return abs(self.p - 0.5) * 2.0

    def sure(self, at: float = 0.8) -> bool:
        return self.confidence >= at

    def __repr__(self) -> str:
        return f"<noul p={self.p:.3f}>"


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    confidence: float
    probabilities: Mapping[str, float]

    def sure(self, at: float = 0.8) -> bool:
        return self.confidence >= at

    @property
    def ranked(self) -> list[tuple[str, float]]:
        return sorted(self.probabilities.items(), key=lambda kv: -kv[1])

    @property
    def margin(self) -> float:
        """Top probability minus runner-up.

        Distinct from `confidence`, and often the better gate when the options
        are near-duplicates: two plausible "Buy" buttons produce a *confident*
        distribution split across both, and the margin is what notices.
        """
        r = self.ranked
        return (r[0][1] - r[1][1]) if len(r) > 1 else r[0][1] if r else 0.0

    def __repr__(self) -> str:
        return (f"<choice {self.choice!r} conf={self.confidence:.2f} "
                f"margin={self.margin:.2f}>")


@dataclass(frozen=True)
class ScoreAnswer:
    score: float
    confidence: float
    legend: Mapping[str, str]
    probabilities: Mapping[str, float]

    @property
    def level(self) -> int:
        """The most likely level, as an index.

        `score` is the probability-weighted mean, which can land between two
        levels and name a rung that does not exist. When you need a label,
        take the argmax; when you need to compare or sort, take the mean.
        """
        if not self.probabilities:
            return 0
        return int(max(self.probabilities.items(), key=lambda kv: kv[1])[0])

    @property
    def label(self) -> str:
        return str(self.legend.get(str(self.level), ""))

    def sure(self, at: float = 0.8) -> bool:
        return self.confidence >= at

    def __repr__(self) -> str:
        return (f"<score {self.score:.2f} ({self.label!r}) "
                f"conf={self.confidence:.2f}>")


Answer = NoulAnswer | ChoiceAnswer | ScoreAnswer


@dataclass
class Decision:
    """Every answer from one call, plus what it cost to get them."""

    answers: dict[str, Answer]
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0
    #: False for a stub answer produced with no API key. `fast.py` will not
    #: act on one. Never let a missing credential look like a cheap success.
    real: bool = True

    @property
    def cost_usd(self) -> float:
        return self.input_tokens / 1_000_000 * USD_PER_MTOK_IN

    def __getitem__(self, key: str) -> Answer:
        return self.answers[key]

    def __contains__(self, key: str) -> bool:
        return key in self.answers

    def line(self) -> str:
        """One line for the journal. The probabilities are the point: when a
        decision turns out wrong, this is the only evidence of why."""
        bits = []
        for k, a in self.answers.items():
            if isinstance(a, NoulAnswer):
                bits.append(f"{k}={a.p:.2f}")
            elif isinstance(a, ChoiceAnswer):
                bits.append(f"{k}={a.choice}@{a.confidence:.2f}")
            else:
                bits.append(f"{k}={a.score:.1f}@{a.confidence:.2f}")
        tag = "" if self.real else " OFFLINE-STUB"
        return (f"jev {self.latency_s * 1000:.0f}ms "
                f"${self.cost_usd:.6f} {' '.join(bits)}{tag}")


def _parse(raw: Mapping[str, Any]) -> dict[str, Answer]:
    out: dict[str, Answer] = {}
    for key, a in (raw or {}).items():
        kind = a.get("type")
        if kind == "noul":
            out[key] = NoulAnswer(p=float(a.get("noul", 0.0)))
        elif kind == "choice":
            out[key] = ChoiceAnswer(
                choice=str(a.get("choice", "")),
                confidence=float(a.get("confidence", 0.0)),
                probabilities={str(k): float(v) for k, v
                               in (a.get("probabilities") or {}).items()})
        elif kind == "score":
            out[key] = ScoreAnswer(
                score=float(a.get("score", 0.0)),
                confidence=float(a.get("confidence", 0.0)),
                legend={str(k): str(v) for k, v
                        in (a.get("legend") or {}).items()},
                probabilities={str(k): float(v) for k, v
                               in (a.get("probabilities") or {}).items()})
        else:
            raise JevError(f"unknown answer type {kind!r} for {key!r}")
    return out


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

@dataclass
class Stats:
    calls: int = 0
    failures: int = 0
    input_tokens: int = 0
    latencies: list[float] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return self.input_tokens / 1_000_000 * USD_PER_MTOK_IN

    def _pct(self, q: float) -> float:
        if not self.latencies:
            return 0.0
        s = sorted(self.latencies)
        return s[min(len(s) - 1, int(q * len(s)))]

    def report(self) -> str:
        return (f"{self.calls} calls, {self.failures} failed, "
                f"p50 {self._pct(0.5) * 1000:.0f}ms "
                f"p95 {self._pct(0.95) * 1000:.0f}ms, "
                f"{self.input_tokens} tok, ${self.cost_usd:.4f}")


class Client:
    """Thread-safe, retrying, rate-capped, and honest when it has no key.

    Construct one per process and share it: the connection pool is what keeps
    a 5Hz loop off the TLS handshake path, and it is the single biggest
    difference between 200ms and 400ms per decision.
    """

    def __init__(self, api_key: str | None = None, *,
                 endpoint: str | None = None, model: str | None = None,
                 timeout: float = 5.0, retries: int = 2,
                 offline: bool | None = None,
                 backend: str = "typesafe", account_id: str | None = None,
                 log: str | Path | None = None) -> None:
        spec = BACKENDS.get(backend)
        if spec is None:
            raise ValueError(f"unknown backend {backend!r}; "
                             f"one of {sorted(BACKENDS)}")
        self.backend = backend
        self.api_key = api_key or os.environ.get(spec["env"]) or ""
        self.model = model or spec["model"]
        self.account_id = ""
        if backend == "cloudflare":
            self.account_id = (account_id
                               or os.environ.get("CLOUDFLARE_ACCOUNT_ID") or "")
            if self.api_key and not self.account_id:
                raise ValueError("cloudflare backend needs CLOUDFLARE_ACCOUNT_ID")
            self.endpoint = endpoint or spec["endpoint"].format(
                account=self.account_id)
        else:
            self.endpoint = endpoint or spec["endpoint"]
        self.timeout = timeout
        self.retries = retries
        # No key, or forced: answer from the stub so the loop can be wired,
        # measured and tested without credentials — and mark every answer.
        env_off = os.environ.get("JEV_OFFLINE", "").strip().lower() in {"1", "true", "yes"}
        self.offline = (not self.api_key) if offline is None else offline
        self.offline = self.offline or env_off
        # Every call is journalled when this is set, so a decision can be
        # inspected long after the Decision object is gone. JEV_LOG sets it
        # for a whole run without touching any call site.
        self.log_path = log or os.environ.get("JEV_LOG") or ""
        self.stats = Stats()
        self._lock = threading.Lock()
        self._recent: list[float] = []
        self._client = None
        self._aclient = None

    # -- transport ---------------------------------------------------------

    def _http(self):
        if self._client is None:
            import httpx
            self._client = httpx.Client(
                timeout=self.timeout,
                limits=httpx.Limits(max_keepalive_connections=8,
                                    max_connections=16,
                                    keepalive_expiry=KEEPALIVE_S))
        return self._client

    def _ahttp(self):
        if self._aclient is None:
            import httpx
            self._aclient = httpx.AsyncClient(
                timeout=self.timeout,
                limits=httpx.Limits(max_keepalive_connections=8,
                                    max_connections=16))
        return self._aclient

    def prewarm(self) -> float:
        """Pay the TLS handshake before the user is waiting on it.

        Measured here: a cold call is 1156ms against a warm 344ms. Call this
        when a task starts, not when the first decision is needed, and the
        first decision costs what every other one costs.

        Sends the smallest legal request — one short noul, ~30 tokens, about
        $0.000001. Returns the seconds it took, or 0.0 if offline or it
        failed; a failure here is not worth interrupting a task for, since
        the next real call will report it properly.
        """
        if self.offline:
            return 0.0
        t0 = time.monotonic()
        try:
            self.ask("ready", {"_warm": Noul("Is this text non-empty?")})
        except Exception:
            return 0.0
        return time.monotonic() - t0

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # -- rate ---------------------------------------------------------------

    def _gate(self) -> None:
        """Drop the call rather than earn a 429 the loop will wait on."""
        now = time.monotonic()
        with self._lock:
            self._recent = [t for t in self._recent if now - t < 60.0]
            if len(self._recent) >= REQUESTS_PER_MIN:
                raise JevUnavailable(
                    f"local rate cap: {REQUESTS_PER_MIN} requests/min reached")
            self._recent.append(now)

    # -- request building ---------------------------------------------------

    def _body(self, state: Any, questions: Mapping[str, Question]) -> dict:
        if not questions:
            raise ValueError("ask() needs at least one question")
        body = {"model": self.model,
                "state": state if isinstance(state, (str, dict, list))
                         else str(state),
                "questions": {k: q.payload() for k, q in questions.items()}}
        if self.backend == "cloudflare":
            # Workers AI is the only one of the three that reshapes the call:
            # the model id stays at the top and the rest moves under `input`.
            return {"model": self.model,
                    "input": {k: v for k, v in body.items() if k != "model"}}
        # TypeSafe and OpenRouter take the same three fields, which is the
        # whole reason switching between them is a URL and nothing else.
        return body

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"}

    def _record(self, dt: float, tokens: int) -> None:
        with self._lock:
            self.stats.calls += 1
            self.stats.input_tokens += tokens
            self.stats.latencies.append(dt)

    def _log(self, body: Mapping[str, Any], data: Mapping[str, Any],
             dt: float, error: str = "") -> None:
        """Append one call to a JSONL journal, exactly as it went and came back.

        Jev returns no rationale. When a decision turns out wrong, the full
        probability distribution over every option is the *only* evidence of
        why — and it is gone the moment the Decision is discarded. So the
        state, the questions and the raw answers are all kept verbatim, not
        summarised: a summary throws away precisely the runner-up that
        explains the mistake.

        One JSON object per line, so it can be read with `jev_log.py`, grepped,
        or loaded into anything. Failures are written too, with `error` set —
        a call that never answered is usually the interesting one.
        """
        if not self.log_path:
            return
        import datetime as _dt
        rec = {
            "t": _dt.datetime.now().isoformat(timespec="milliseconds"),
            "backend": self.backend,
            "model": data.get("model") or self.model,
            "latency_ms": round(dt * 1000),
            "state": body.get("state") if "state" in body
                     else (body.get("input") or {}).get("state"),
            "questions": body.get("questions") if "questions" in body
                         else (body.get("input") or {}).get("questions"),
            "answers": data.get("answers") or {},
            "usage": data.get("usage") or {},
        }
        if error:
            rec["error"] = error
        try:
            p = Path(self.log_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except Exception:
            # A journal that cannot be written must never take a task down.
            pass

    def _decide(self, data: Mapping[str, Any], dt: float,
                body: Mapping[str, Any] | None = None) -> Decision:
        # Workers AI wraps every response in the standard Cloudflare envelope.
        # Unwrap before parsing, and surface their error list rather than
        # reporting "no answers" for what is really a rejected request.
        if self.backend == "cloudflare" and "result" in data:
            if data.get("success") is False:
                raise JevError(f"cloudflare: {data.get('errors')}")
            data = data.get("result") or {}
        if body is not None:
            self._log(body, data, dt)
        usage = data.get("usage") or {}
        d = Decision(answers=_parse(data.get("answers") or {}),
                     model=str(data.get("model") or self.model),
                     input_tokens=int(usage.get("input_tokens") or 0),
                     output_tokens=int(usage.get("output_tokens") or 0),
                     latency_s=dt)
        self._record(dt, d.input_tokens)
        return d

    # -- the call -----------------------------------------------------------

    def ask(self, state: Any, questions: Mapping[str, Question]) -> Decision:
        """One state, many questions, one round trip.

        Batching is close to free — questions are evaluated independently and
        in parallel against the same state, so you pay for the extra question
        tokens and almost no extra latency. Ask everything you *might* want
        (the "speculative fan-out" pattern) and let the caller ignore what it
        turns out not to need; a second call costs 200ms, a second question
        costs about 2ms.
        """
        body = self._body(state, questions)
        if self.offline:
            d = _stub(body, self.model)
            self._log(body, {"model": d.model, "answers": {},
                             "usage": {"input_tokens": d.input_tokens}}, 0.0,
                      error="offline stub — not a real decision")
            return d

        self._gate()
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            t0 = time.monotonic()
            try:
                r = self._http().post(self.endpoint, headers=self._headers(),
                                      json=body)
            except Exception as e:                       # transport-level
                last = e
                self.stats.failures += 1
            else:
                dt = time.monotonic() - t0
                if r.status_code == 200:
                    return self._decide(r.json(), dt, body)
                if r.status_code == 422:
                    raise JevRefused(f"422: {r.text[:300]}")
                if r.status_code == 401:
                    raise JevError("401: TYPESAFE_API_KEY missing or invalid")
                if r.status_code not in RETRY_STATUS:
                    raise JevError(f"{r.status_code}: {r.text[:300]}")
                last = JevError(f"{r.status_code}: {r.text[:200]}")
                self.stats.failures += 1
            if attempt < self.retries:
                time.sleep(_backoff(attempt))
        raise JevUnavailable(f"no decision after {self.retries + 1} attempts: {last}")

    async def ask_async(self, state: Any,
                        questions: Mapping[str, Question]) -> Decision:
        """Same call, awaitable — the form the desktop loop actually wants.

        A decision is ~200ms and the UI keeps moving during it. Fire the next
        request while acting on the last one; see `fast.py::Pilot`.
        """
        import asyncio
        body = self._body(state, questions)
        if self.offline:
            return _stub(body, self.model)

        self._gate()
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            t0 = time.monotonic()
            try:
                r = await self._ahttp().post(self.endpoint,
                                             headers=self._headers(), json=body)
            except Exception as e:
                last = e
                self.stats.failures += 1
            else:
                dt = time.monotonic() - t0
                if r.status_code == 200:
                    return self._decide(r.json(), dt, body)
                if r.status_code == 422:
                    raise JevRefused(f"422: {r.text[:300]}")
                if r.status_code == 401:
                    raise JevError("401: TYPESAFE_API_KEY missing or invalid")
                if r.status_code not in RETRY_STATUS:
                    raise JevError(f"{r.status_code}: {r.text[:300]}")
                last = JevError(f"{r.status_code}: {r.text[:200]}")
                self.stats.failures += 1
            if attempt < self.retries:
                await asyncio.sleep(_backoff(attempt))
        raise JevUnavailable(f"no decision after {self.retries + 1} attempts: {last}")


def _backoff(attempt: int) -> float:
    return min(1.5, 0.15 * (2 ** attempt)) * (0.7 + random.random() * 0.6)


# --------------------------------------------------------------------------
# Offline stub
# --------------------------------------------------------------------------

def _stub(body: Mapping[str, Any], model: str) -> Decision:
    """A deterministic fake, so the loop can be built and tested with no key.

    It is a hash, not a model: same state and question always give the same
    answer, which makes tests reproducible and makes plumbing bugs visible.
    Every answer it produces is marked `real=False`, and `fast.py` refuses to
    act on those — a stub must never be mistaken for a cheap real decision.
    """
    import hashlib

    state = body.get("state")
    blob = state if isinstance(state, str) else json.dumps(state, sort_keys=True,
                                                           default=str)
    answers: dict[str, Answer] = {}
    for key, q in (body.get("questions") or {}).items():
        seed = int(hashlib.sha256(f"{blob}\x00{key}".encode()).hexdigest()[:8], 16)
        rnd = random.Random(seed)
        kind = q.get("type")
        if kind == "noul":
            answers[key] = NoulAnswer(p=round(rnd.random(), 3))
        elif kind == "choice":
            opts = list((q.get("criteria") or {}).keys()) or ["?"]
            w = [rnd.random() for _ in opts]
            tot = sum(w) or 1.0
            probs = {o: round(x / tot, 3) for o, x in zip(opts, w)}
            top = max(probs.items(), key=lambda kv: kv[1])
            answers[key] = ChoiceAnswer(choice=top[0], confidence=round(top[1], 3),
                                        probabilities=probs)
        else:
            levels = list(q.get("criteria") or ["low", "high"])
            w = [rnd.random() for _ in levels]
            tot = sum(w) or 1.0
            probs = {str(i): round(x / tot, 3) for i, x in enumerate(w)}
            mean = sum(int(i) * p for i, p in probs.items())
            top = max(probs.items(), key=lambda kv: kv[1])
            answers[key] = ScoreAnswer(
                score=round(mean, 3), confidence=round(top[1], 3),
                legend={str(i): s for i, s in enumerate(levels)},
                probabilities=probs)
    # A token estimate, not a count — enough to keep cost arithmetic honest
    # in a dry run without pretending to be the real tokenizer.
    est = max(1, len(blob) // 4 + 20 * len(answers))
    return Decision(answers=answers, model=f"{model}/offline-stub",
                    input_tokens=est, latency_s=0.0, real=False)


# --------------------------------------------------------------------------

_default: Client | None = None


def client() -> Client:
    """The shared client. One pool, one set of statistics, one rate cap."""
    global _default
    if _default is None:
        _default = Client()
    return _default


def ask(state: Any, questions: Mapping[str, Question]) -> Decision:
    return client().ask(state, questions)


async def ask_async(state: Any, questions: Mapping[str, Question]) -> Decision:
    return await client().ask_async(state, questions)
