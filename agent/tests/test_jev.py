"""System-1 decisions: the client, the gates, and the ways speed goes wrong.

Nothing here touches the network or a real window. The transport is faked, so
these checks are about the parts that would actually hurt in production and
that a live API would hide rather than reveal:

  * a missing API key must escalate, never turn into a confident random click;
  * a commit guard must hold on *any* unreliable answer, not just a loud one;
  * a decision that arrives after the screen moved must be thrown away;
  * a confident answer split across two near-identical buttons is not an
    answer, and margin — not confidence — is what notices.

The documented sample response from the vendor's own docs is parsed verbatim
in section 2, so a change to the wire format fails here rather than at 5Hz in
front of a checkout page.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.pop("TYPESAFE_API_KEY", None)
os.environ.pop("JEV_OFFLINE", None)

from core import jev            # noqa: E402
from core import fast           # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""),
          flush=True)
    return bool(ok)


# --------------------------------------------------------------------------
# A fake transport
# --------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status: int, payload: dict | None = None, text: str = ""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text or str(payload)

    def json(self) -> dict:
        return self._payload


class FakeHTTP:
    """Replays a queue of responses and remembers what was sent."""

    def __init__(self, *responses: FakeResponse):
        self.queue = list(responses)
        self.sent: list[dict] = []

    def post(self, url, headers=None, json=None):
        self.sent.append(json)
        return self.queue.pop(0) if self.queue else FakeResponse(200, {})


def wired(*responses: FakeResponse) -> tuple[jev.Client, FakeHTTP]:
    """A client with a key (so it is not in offline mode) and no network."""
    c = jev.Client(api_key="test-key", retries=2)
    http = FakeHTTP(*responses)
    c._http = lambda: http           # type: ignore[assignment]
    return c, http


def ok(answers: dict, tokens: int = 400) -> FakeResponse:
    return FakeResponse(200, {"model": "jev-1.13.0", "answers": answers,
                              "usage": {"input_tokens": tokens,
                                        "output_tokens": 70}})


def choice_ans(probs: dict, conf: float) -> dict:
    top = max(probs.items(), key=lambda kv: kv[1])[0]
    return {"type": "choice", "choice": top, "confidence": conf,
            "probabilities": probs}


ROWS = [
    {"type": "Button", "name": "Search", "id": "btnSearch"},
    {"type": "Edit", "name": "From", "id": "from"},
    {"type": "Edit", "name": "To", "id": "to"},
    {"type": "Button", "name": "Buy now", "id": "buy"},
]


# --------------------------------------------------------------------------

def test_questions():
    print("\n1. questions serialise to the documented shape")

    n = jev.Noul("Is it urgent?", criteria={"true": "yes", "false": "no"})
    p = n.payload()
    check("noul payload", p == {"type": "noul", "instructions": "Is it urgent?",
                                "criteria": {"true": "yes", "false": "no"}}, str(p))

    c = jev.Choice("Which team?", criteria={"billing": "money", "tech": None})
    p = c.payload()
    check("choice payload", p["type"] == "choice"
          and p["criteria"] == {"billing": "money", "tech": None}, str(p))

    s = jev.Score("How angry?", criteria=["Calm", "Frustrated", "Very angry"])
    p = s.payload()
    check("score payload", p["criteria"] == ["Calm", "Frustrated", "Very angry"],
          str(p))

    # The limits are the vendor's, and breaking them earns a 422 mid-loop.
    # Catching it at construction is worth more than the error message.
    try:
        jev.Choice("x", criteria={str(i): None for i in range(256)}).payload()
        check("256 options rejected", False, "no error raised")
    except ValueError as e:
        check("256 options rejected", "255" in str(e), str(e))

    for levels, why in (([], "0 levels"), (["a"], "1 level"),
                        ([str(i) for i in range(11)], "11 levels")):
        try:
            jev.Score("x", criteria=levels).payload()
            check(f"{why} rejected", False, "no error raised")
        except ValueError:
            check(f"{why} rejected", True)

    try:
        jev.Choice("x", criteria={}).payload()
        check("empty choice rejected", False, "no error raised")
    except ValueError:
        check("empty choice rejected", True)


def test_parsing():
    print("\n2. the vendor's own sample response parses correctly")

    # Verbatim from docs: developers.cloudflare.com/ai/models/typesafe/jev
    sample = {
        "model": "jev-1.13.0",
        "answers": {
            "is_urgent": {"type": "noul", "noul": 0.95},
            "department": {"type": "choice", "choice": "billing",
                           "confidence": 0.8,
                           "probabilities": {"billing": 0.87, "sales": 0,
                                             "technical": 0.13}},
            "frustration": {"type": "score", "score": 1.04, "confidence": 0.94,
                            "legend": {"0": "Calm", "1": "Frustrated",
                                       "2": "Very angry"},
                            "probabilities": {"0": 0, "1": 0.96, "2": 0.04}},
        },
        "usage": {"input_tokens": 426, "output_tokens": 73},
    }
    c, _ = wired(FakeResponse(200, sample))
    d = c.ask("Help! My payouts have been failing for 3 days.",
              {"is_urgent": jev.Noul("urgent?")})

    check("noul value", d["is_urgent"].p == 0.95)
    check("noul is yes", d["is_urgent"].yes)
    check("choice value", d["department"].choice == "billing")
    check("choice probabilities", d["department"].probabilities["technical"] == 0.13)
    check("choice margin", abs(d["department"].margin - 0.74) < 1e-9,
          f"{d['department'].margin}")
    check("score mean kept", d["frustration"].score == 1.04)
    check("score argmax level", d["frustration"].level == 1)
    check("score label", d["frustration"].label == "Frustrated")
    check("tokens recorded", d.input_tokens == 426)
    check("model echoed", d.model == "jev-1.13.0")
    check("marked real", d.real)

    # $0.042 per million in, nothing out.
    want = 426 / 1_000_000 * 0.042
    check("cost arithmetic", abs(d.cost_usd - want) < 1e-12,
          f"${d.cost_usd:.8f}")

    # A noul carries its certainty in the probability itself; putting it on
    # the same 0-1 scale is what lets one gate serve all three primitives.
    check("noul confidence scale", abs(jev.NoulAnswer(0.95).confidence - 0.9) < 1e-9)
    check("coin flip is no confidence", jev.NoulAnswer(0.5).confidence == 0.0)
    check("a confident no", jev.NoulAnswer(0.02).confidence > 0.9)


def test_transport():
    print("\n3. failures are told apart from faults")

    c, http = wired(FakeResponse(429, text="slow down"),
                    ok({"q": {"type": "noul", "noul": 0.9}}))
    d = c.ask("s", {"q": jev.Noul("?")})
    check("429 retried then served", d["q"].p == 0.9 and len(http.sent) == 2,
          f"{len(http.sent)} calls")
    check("failure counted", c.stats.failures == 1, str(c.stats.failures))

    c, _ = wired(FakeResponse(422, text="bad question"))
    try:
        c.ask("s", {"q": jev.Noul("?")})
        check("422 raises JevRefused", False, "no error")
    except jev.JevRefused:
        check("422 raises JevRefused", True)
    except Exception as e:
        check("422 raises JevRefused", False, type(e).__name__)

    c, _ = wired(FakeResponse(401, text="nope"))
    try:
        c.ask("s", {"q": jev.Noul("?")})
        check("401 raises", False, "no error")
    except jev.JevError as e:
        check("401 raises", "TYPESAFE_API_KEY" in str(e), str(e))

    c, http = wired(*[FakeResponse(529, text="overloaded")] * 3)
    try:
        c.ask("s", {"q": jev.Noul("?")})
        check("gives up rather than hangs", False, "no error")
    except jev.JevUnavailable:
        check("gives up rather than hangs", len(http.sent) == 3,
              f"{len(http.sent)} attempts")

    # A 429 inside a 5Hz loop costs more wall-clock than not sending the call.
    c, _ = wired(*[ok({"q": {"type": "noul", "noul": 0.5}})] * 3)
    c._recent = [__import__("time").monotonic()] * jev.REQUESTS_PER_MIN
    try:
        c.ask("s", {"q": jev.Noul("?")})
        check("local rate cap fires", False, "no error")
    except jev.JevUnavailable as e:
        check("local rate cap fires", "rate cap" in str(e), str(e))

    try:
        wired(ok({}))[0].ask("s", {})
        check("empty question set rejected", False, "no error")
    except ValueError:
        check("empty question set rejected", True)


def test_offline_stub():
    print("\n4. no API key escalates — it never fakes a decision")

    c = jev.Client(api_key="")
    check("no key means offline", c.offline)

    d = c.ask({"controls": ROWS}, {"pick": fast.q_pick("buy a ticket", ROWS)})
    check("stub answers", "pick" in d)
    check("stub marked not real", not d.real)
    check("stub names itself", "offline-stub" in d.model, d.model)
    check("stub line is flagged", "OFFLINE-STUB" in d.line(), d.line())

    # Deterministic, so a plumbing bug shows up as a failure rather than as
    # flake, and a dry run is reproducible.
    d2 = c.ask({"controls": ROWS}, {"pick": fast.q_pick("buy a ticket", ROWS)})
    check("stub is deterministic", d.answers["pick"].choice == d2.answers["pick"].choice)

    # The whole point: the gate refuses it however confident it looks.
    i, v = fast.pick("buy a ticket", ROWS, at=0.0, margin=0.0, client=c)
    check("stub never acts", i is None and not v.act, v.why)
    check("stub escalation says why", "offline" in v.why.lower(), v.why)

    g = fast.safe_to_act("click Buy now", ROWS, client=c)
    check("stub cannot clear commit guard", not g.act, g.why)

    w = fast.worked("open the seat map", ROWS, ROWS, client=c)
    check("stub cannot verify a step", not w.act, w.why)


def test_pick():
    print("\n5. picking a control, and refusing to")

    c, http = wired(ok({"pick": choice_ans({"0": 0.05, "1": 0.02, "2": 0.03,
                                            "3": 0.9, "none": 0.0}, 0.9)}))
    i, v = fast.pick("buy the ticket", ROWS, window="Tickets", client=c)
    check("index resolves", i == 3, str(i))
    check("acted", v.act, v.why)
    check("row is the right one", ROWS[i]["name"] == "Buy now")

    sent = http.sent[0]
    check("state carries the window", sent["state"]["window"] == "Tickets")
    check("state carries rows", len(sent["state"]["controls"]) == 4)
    check("model is offered an escape", "none" in sent["questions"]["pick"]["criteria"])
    # The project's first rule: a coordinate never crosses a turn boundary.
    check("no coordinates in state", "x" not in str(sent["state"]),
          str(sent["state"])[:120])

    c, _ = wired(ok({"pick": choice_ans({"0": 0.1, "none": 0.9}, 0.9)}))
    i, v = fast.pick("buy the ticket", ROWS, client=c)
    check("'none' is honoured", i is None and not v.act, v.why)

    c, _ = wired(ok({"pick": choice_ans({"0": 0.5, "none": 0.5}, 0.4)}))
    i, v = fast.pick("buy the ticket", ROWS, client=c)
    check("low confidence holds", i is None, v.why)
    check("held for the stated reason", "confidence" in v.why, v.why)

    # Two near-identical buttons: the model is *confident*, and split. This is
    # the case a confidence threshold alone waves through.
    c, _ = wired(ok({"pick": choice_ans({"0": 0.46, "3": 0.44, "none": 0.1}, 0.95)}))
    i, v = fast.pick("buy the ticket", ROWS, client=c)
    check("split distribution holds", i is None, v.why)
    check("held on margin, not confidence", "margin" in v.why, v.why)

    c, _ = wired(ok({"pick": choice_ans({"99": 0.95, "none": 0.05}, 0.95)}))
    i, v = fast.pick("buy the ticket", ROWS, client=c)
    check("out-of-range index refused", i is None, v.why)


def test_commit_guard():
    print("\n6. the guard on spending money")

    c, _ = wired(ok({"commit": {"type": "noul", "noul": 0.02}}))
    g = fast.safe_to_act("scroll the results", ROWS, client=c)
    check("browsing is allowed", g.act, g.why)

    c, _ = wired(ok({"commit": {"type": "noul", "noul": 0.93}}))
    g = fast.safe_to_act("click 'Confirm and pay'", ROWS, client=c)
    check("paying is held", not g.act, g.why)

    # Deliberately low threshold: a false alarm costs one round trip; a miss
    # costs a plane ticket. 0.31 is nowhere near certain, and still holds.
    c, _ = wired(ok({"commit": {"type": "noul", "noul": 0.31}}))
    g = fast.safe_to_act("click 'Continue'", ROWS, client=c)
    check("a mere suspicion holds", not g.act, g.why)
    check("threshold is low on purpose", fast.COMMIT_STOP <= 0.35,
          str(fast.COMMIT_STOP))

    # Batched with the pick — a guard skipped because it cost 200ms is a
    # guard you do not have.
    c, http = wired(ok({"pick": choice_ans({"3": 0.95, "none": 0.05}, 0.95),
                        "commit": {"type": "noul", "noul": 0.9}}))
    p = fast.Pilot(sense=lambda: list(ROWS), act=lambda i: True, client=c)
    t = p.step("buy the ticket")
    check("one round trip for both", len(http.sent) == 1, f"{len(http.sent)}")
    check("guard stops the pilot", not t.ok and "guard" in t.note, t.note)
    check("nothing was clicked", t.index is None, str(t.index))


def test_staleness():
    print("\n7. a decision that arrived too late is thrown away")

    # An intent and a target with no commit words in them, so this exercises
    # staleness with the guard still switched on rather than around it.
    moved = ROWS + [{"type": "Dialog", "name": "Session expired", "id": "d"}]
    seen: list[int] = []
    frames = [list(ROWS), list(moved)]       # sense before, sense after

    c, _ = wired(ok({"pick": choice_ans({"0": 0.95, "none": 0.05}, 0.95),
                     "commit": {"type": "noul", "noul": 0.01}}))
    p = fast.Pilot(sense=lambda: frames.pop(0), act=lambda i: seen.append(i) or True,
                   client=c)
    t = p.step("find the morning departures")
    check("stale decision dropped", t.stale and not t.ok, t.note)
    check("nothing was clicked", seen == [], str(seen))
    check("the reason is recorded", "moved" in t.note, t.note)

    still = [list(ROWS), list(ROWS)]
    c, _ = wired(ok({"pick": choice_ans({"0": 0.95, "none": 0.05}, 0.95),
                     "commit": {"type": "noul", "noul": 0.01}}))
    p = fast.Pilot(sense=lambda: still.pop(0), act=lambda i: seen.append(i) or True,
                   client=c)
    t = p.step("find the morning departures")
    check("a steady screen is acted on", t.ok and seen == [0], f"{t.note} {seen}")

    check("fingerprint is stable", fast.fingerprint(ROWS) == fast.fingerprint(list(ROWS)))
    check("fingerprint notices a change",
          fast.fingerprint(ROWS) != fast.fingerprint(moved))


def test_state_budget():
    print("\n8. the state stays small enough to send at 5Hz")

    many = [{"type": "Button", "name": f"Seat {i}", "id": f"s{i}"}
            for i in range(200)]
    st = fast.rows_to_state(many, window="Seats")
    check("rows capped", len(st["controls"]) == fast.MAX_ROWS,
          str(len(st["controls"])))
    check("truncation is declared", st.get("truncated") == 200 - fast.MAX_ROWS,
          str(st.get("truncated")))

    import json
    size = len(json.dumps(st))
    check("a full window is ~2k tokens", size / 4 < 2500, f"{size} chars")

    # Cheap enough that asking before every click is affordable — which is the
    # only frequency at which the guard protects anything.
    est_cost = (size / 4) / 1_000_000 * jev.USD_PER_MTOK_IN
    check("under $0.0001 a decision", est_cost < 1e-4, f"${est_cost:.7f}")

    # Row keys are short on purpose: at 5Hz the key names are real money.
    check("no coordinates ever leave", all(
        set(r).issubset({"i", "t", "n", "id", "hint"}) for r in st["controls"]))


def test_rerank():
    print("\n9. finding a saved job across languages")

    # The gap retrieval.py documents and leaves open: lexical matching cannot
    # connect "stock market" to "borsa".
    jobs = [{"name": "bist30-morning-pull", "description": "borsa endeks "
             "rakamlarini her sabah cek"},
            {"name": "invoice-file", "description": "file the monthly invoice"}]
    c, http = wired(ok({"job": choice_ans({"0": 0.88, "1": 0.07, "none": 0.05},
                                          0.88)}))
    i, v = fast.rerank("get me the stock market numbers", jobs, client=c)
    check("crosses the language gap", i == 0, str(i))
    check("candidates carried their descriptions",
          "borsa" in str(http.sent[0]["questions"]["job"]["criteria"]))

    c, _ = wired(ok({"job": choice_ans({"0": 0.2, "1": 0.2, "none": 0.6}, 0.6)}))
    i, v = fast.rerank("book a flight", jobs, client=c)
    check("says none rather than guessing", i is None, v.why)


def test_verify():
    print("\n10. verifying a step without a screenshot")

    before = list(ROWS)
    after = ROWS + [{"type": "Text", "name": "Seat map", "id": "map"}]

    c, http = wired(ok({"worked": {"type": "noul", "noul": 0.94},
                        "blocker": choice_ans({"nothing": 0.95}, 0.95)}))
    v = fast.worked("open the seat map", before, after, client=c)
    check("a real change verifies", v.act, v.why)
    check("both states were sent",
          "before" in http.sent[0]["state"] and "after" in http.sent[0]["state"])

    c, _ = wired(ok({"worked": {"type": "noul", "noul": 0.03},
                     "blocker": choice_ans({"sold_out": 0.9, "nothing": 0.1}, 0.9)}))
    v = fast.worked("pick seat 12A", before, before, client=c)
    check("no change fails the step", not v.act, v.why)
    check("and names the blocker", "sold_out" in v.why, v.why)

    c, _ = wired(ok({"worked": {"type": "noul", "noul": 0.55},
                     "blocker": choice_ans({"loading": 0.5, "nothing": 0.5}, 0.5)}))
    v = fast.worked("pick seat 12A", before, after, client=c)
    check("an unsure verdict escalates", not v.act and "unsure" in v.why, v.why)


def test_adversarial_state():
    print("\n11. page text cannot talk the guard down")

    # jev-1.13 failure mode 6: state is data, and the model does not treat it
    # as hostile. Every control name here was written by the merchant. So the
    # deterministic guard runs first and is not reviewable by the model.
    check("english pay button", fast.code_side_commit("click Pay now") == "pay")
    check("turkish pay button",
          fast.code_side_commit("Satın al butonuna tıkla") == "satın al",
          fast.code_side_commit("Satın al butonuna tıkla"))
    check("turkish confirm order",
          bool(fast.code_side_commit("siparişi tamamla")),
          fast.code_side_commit("siparişi tamamla"))
    check("browsing is not a commit", fast.code_side_commit("scroll down") == "")

    # The button lies about itself; the target name is still checked.
    check("caught via the target name",
          fast.code_side_commit("press the highlighted control",
                                target={"name": "Ödeme"}) != "")
    # And the page is judged by where it is, not only what it says.
    check("caught via the window",
          fast.code_side_commit("press the highlighted control",
                                window="Checkout — step 3").startswith("context:"))

    # The whole point: a confident model saying "harmless" changes nothing.
    c, _ = wired(ok({"commit": {"type": "noul", "noul": 0.01}}))
    g = fast.safe_to_act("click 'Pay now'", ROWS, client=c)
    check("model cannot clear a code-side hit", not g.act, g.why)
    check("and says which word", "'pay'" in g.why, g.why)

    # An API failure must hold too — never "could not check, so go ahead".
    c, _ = wired(*[FakeResponse(529, text="down")] * 3)
    g = fast.safe_to_act("open the results list", ROWS, client=c)
    check("an outage holds the guard", not g.act, g.why)


def test_decide():
    print("\n12. operation and target in one round trip")

    c, http = wired(ok({"op": choice_ans({"click": 0.95, "wait": 0.05}, 0.95),
                        "pick": choice_ans({"0": 0.93, "none": 0.07}, 0.93),
                        "blocker": choice_ans({"nothing": 0.97}, 0.97),
                        "commit": {"type": "noul", "noul": 0.02}}))
    m = fast.decide("find the morning departures", ROWS, window="Tickets",
                    client=c)
    check("one round trip for four heads", len(http.sent) == 1, str(len(http.sent)))
    check("operation chosen", m.operation == "click", m.operation)
    check("target chosen", m.index == 0, str(m.index))
    check("may act", m.verdict.act, m.verdict.why)
    check("four questions sent", len(http.sent[0]["questions"]) == 4,
          str(list(http.sent[0]["questions"])))

    # "Not on screen yet" is a decision, not a failure — and it is the one a
    # control table on its own cannot express.
    c, _ = wired(ok({"op": choice_ans({"scroll_down": 0.9, "click": 0.1}, 0.9),
                     "pick": choice_ans({"0": 0.4, "none": 0.6}, 0.4),
                     "blocker": choice_ans({"nothing": 0.9}, 0.9),
                     "commit": {"type": "noul", "noul": 0.01}}))
    m = fast.decide("find the morning departures", ROWS, client=c)
    check("scroll needs no target", m.operation == "scroll_down" and m.index is None,
          m.line())
    check("and is still a usable answer", m.verdict.act, m.verdict.why)
    check("needs_target is honest", not m.needs_target)

    c, _ = wired(ok({"op": choice_ans({"wait": 0.92, "click": 0.08}, 0.92),
                     "pick": choice_ans({"0": 0.5, "none": 0.5}, 0.5),
                     "blocker": choice_ans({"loading": 0.88, "nothing": 0.12}, 0.88),
                     "commit": {"type": "noul", "noul": 0.01}}))
    m = fast.decide("find the morning departures", ROWS, client=c)
    check("waits while loading", m.operation == "wait", m.operation)
    check("blocker reported", m.blocker == "loading", m.blocker)

    # The guard still applies inside the combined call.
    c, _ = wired(ok({"op": choice_ans({"click": 0.95}, 0.95),
                     "pick": choice_ans({"3": 0.95, "none": 0.05}, 0.95),
                     "blocker": choice_ans({"nothing": 0.9}, 0.9),
                     "commit": {"type": "noul", "noul": 0.01}}))
    m = fast.decide("press the highlighted control", ROWS, client=c)
    check("guard fires on the Buy target", m.index is None, m.line())
    check("guard named the word", "guard" in m.verdict.why, m.verdict.why)

    c, _ = wired(*[FakeResponse(529, text="down")] * 3)
    m = fast.decide("find the morning departures", ROWS, client=c)
    check("an outage reads as blocked", m.operation == "blocked", m.operation)
    check("and never as an action", m.index is None and not m.verdict.act)

    check("operations are all executable",
          set(fast.OPERATIONS) == {"click", "type", "select", "scroll_down",
                                   "scroll_up", "wait", "done", "blocked"},
          str(sorted(fast.OPERATIONS)))


def main() -> int:
    print("System-1 decisions (jev + fast)")
    for t in (test_questions, test_parsing, test_transport, test_offline_stub,
              test_pick, test_commit_guard, test_staleness, test_state_budget,
              test_rerank, test_verify, test_adversarial_state, test_decide):
        t()
    passed = sum(1 for _, o, _ in results if o)
    print(f"\n{passed}/{len(results)} passed")
    for name, o, detail in results:
        if not o:
            print(f"  FAILED: {name}  {detail}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
