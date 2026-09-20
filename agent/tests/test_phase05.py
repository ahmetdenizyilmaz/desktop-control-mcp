"""Credentials and reaching the user.

Uses a throwaway vault file — the real one at agent/vault.json is never
touched, so running this cannot destroy a stored login.

The Telegram tests deliberately do NOT hit the network. What matters is that
the pairing state machine refuses to pretend it is configured, because a
channel that silently believes it can reach you is worse than no channel.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cleanup import scratch_dir  # noqa: E402

from core import channel as chan  # noqa: E402
from core.secrets import (Vault, VaultError, is_ref, redact,  # noqa: E402
                          ref_name, required_by, resolve)

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""),
          flush=True)
    return ok


def main() -> int:
    print("=" * 72)
    print("CREDENTIALS AND CHANNEL")
    print("=" * 72)

    tmp = scratch_dir("agent_vault_")
    v = Vault(tmp / "vault.json")

    print("\n1. store and retrieve")
    v.put("test-pw", "hunter2-çöğü", note="unit test")
    check("round-trips a value", v.get("test-pw") == "hunter2-çöğü")
    check("survives non-ASCII", "ğ" in v.get("test-pw"),
          "Turkish characters must not be mangled by the DPAPI round trip")
    check("has() finds it", v.has("test-pw"))
    check("has() rejects unknown", not v.has("no-such-thing"))

    print("\n2. the file itself leaks nothing")
    raw = (tmp / "vault.json").read_text(encoding="utf-8")
    check("plaintext absent from disk", "hunter2" not in raw,
          f"{len(raw)} bytes on disk, none of them the password")
    blob = json.loads(raw)["secrets"]["test-pw"]["cipher"]
    check("ciphertext is opaque", len(blob) > 100 and "hunter2" not in blob)

    print("\n3. entropy binds a blob to its own name")
    # Move one entry's ciphertext under a different name. DPAPI must refuse,
    # otherwise an attacker with write access could swap a low-value secret in
    # place of a high-value one and have it typed for them.
    v.put("other", "different-value")
    data = json.loads((tmp / "vault.json").read_text(encoding="utf-8"))
    data["secrets"]["other"]["cipher"] = blob
    (tmp / "vault.json").write_text(json.dumps(data), encoding="utf-8")
    try:
        v.get("other")
        check("rejects a relabelled blob", False, "decrypted under the wrong name")
    except VaultError:
        check("rejects a relabelled blob", True)
    v.delete("other")

    print("\n4. metadata without values")
    infos = v.list()
    check("list() returns metadata", any(i.name == "test-pw" for i in infos))
    check("list() carries no value",
          not any("hunter2" in str(vars(i)) for i in infos))
    before = v.info("test-pw").uses
    v.get("test-pw")
    check("use is counted", v.info("test-pw").uses == before + 1,
          f"{before} -> {v.info('test-pw').uses}")

    print("\n5. secret:// references")
    check("recognises a reference", is_ref("secret://gmail-password"))
    check("rejects a bare string", not is_ref("gmail-password"))
    check("rejects a lookalike", not is_ref("secret://has spaces"))
    check("extracts the name", ref_name("secret://test-pw") == "test-pw")
    check("resolves through the vault",
          resolve("secret://test-pw", v) == "hunter2-çöğü")
    check("passes non-references through",
          resolve("just text", v) == "just text")

    steps = [{"action": "fill", "args": {"value": "secret://site-pw", "sel": "#pw"}},
             {"action": "fill", "args": {"value": "me@example.com"}},
             {"action": "fill", "args": {"value": "secret://site-pw"}},
             {"action": "type", "args": {"text": "secret://totp"}}]
    need = required_by(steps)
    check("lists what a job needs", need == ["site-pw", "totp"], str(need))
    check("needs list is de-duplicated", need.count("site-pw") == 1)

    print("\n6. redaction")
    leaked = f"Login failed. Tried password {v.get('test-pw')} on the form."
    clean = redact(leaked, v)
    check("strips a stored value out of text", "hunter2" not in clean)
    check("says what was removed", "«test-pw»" in clean, clean[:70])
    v.put("tiny", "ab")
    check("ignores values too short to be a secret",
          redact("a rating of ab out of ten", v) == "a rating of ab out of ten",
          "a 2-char secret would otherwise redact half of ordinary English")
    v.delete("tiny")

    print("\n7. delete")
    check("delete removes it", v.delete("test-pw") and not v.has("test-pw"))
    check("delete of a missing name is False", v.delete("test-pw") is False)
    try:
        v.get("gone")
        check("get on a missing name raises", False)
    except VaultError:
        check("get on a missing name raises", True)

    print("\n8. channel refuses to bluff")
    # A fresh config directory: no token, no chat id.
    orig_cfg = chan.CONFIG_FILE
    chan.CONFIG_FILE = tmp / "channel.json"
    try:
        tg = chan.Telegram()
        check("unpaired is not 'configured'", not tg.configured)
        check("send() fails rather than no-ops silently", tg.send("hi") is False)
        link = tg.pairing_link()
        check("pairing needs a token first",
              not link["ok"] and "token" in link["error"].lower(), link.get("error", ""))
        pair = tg.complete_pairing(wait_s=0.1)
        check("cannot complete pairing out of order", not pair["ok"])

        c = chan.Channel()
        c.telegram, c.mail = tg, chan.Mail()
        r = c.ask("Are you there?", timeout_s=0.5)
        check("ask() reports unreachable, not approved",
              r.answer is chan.Answer.UNREACHABLE and not r.ok, r.summary())
        check("notify() reports nothing sent", c.notify("test") == [])
        st = c.status()
        check("status() shows the gap plainly",
              st["telegram"]["paired"] is False and st["app"] is False)
    finally:
        chan.CONFIG_FILE = orig_cfg

    print("\n9. the app answers first when it is open")
    chan.CONFIG_FILE = tmp / "channel.json"
    try:
        c = chan.Channel()
        c.telegram, c.mail = chan.Telegram(), chan.Mail()
        asked = []

        def app_handler(q, approve_deny, timeout_s):
            asked.append(q)
            return chan.Reply(chan.Answer.APPROVE, via="app")

        c.app_ask = app_handler
        r = c.ask("Continue with the transfer?")
        check("routed to the app", r.ok and r.via == "app", r.summary())
        check("app saw the question", asked and "transfer" in asked[0])
        check("pending is cleared afterwards", c.pending is None,
              "a stuck 'pending' would make the UI show a phantom question")

        # A broken app handler must fall through, not take the run down.
        c.app_ask = lambda *a: (_ for _ in ()).throw(RuntimeError("ui gone"))
        r = c.ask("Still there?", timeout_s=0.3)
        check("survives a dead UI handler",
              r.answer is chan.Answer.UNREACHABLE, r.summary())

        # Questions are redacted on the way out — a prompt that quotes a
        # password must not carry it to Telegram or into a mail body.
        v2 = Vault(tmp / "vault.json")
        v2.put("leaky", "SuperSecret123")
        import core.secrets as sec
        real, sec.vault = sec.vault, v2
        try:
            seen = []
            c.app_ask = lambda q, a, t: (seen.append(q),
                                         chan.Reply(chan.Answer.APPROVE, via="app"))[1]
            c.ask("Is SuperSecret123 the right password?")
            check("redacts a question before sending",
                  seen and "SuperSecret123" not in seen[0], seen[0] if seen else "")
        finally:
            sec.vault = real
    finally:
        chan.CONFIG_FILE = orig_cfg

    print("\n10. reply semantics")
    check("APPROVE is ok", chan.Reply(chan.Answer.APPROVE).ok)
    check("DENY is not ok", not chan.Reply(chan.Answer.DENY).ok)
    check("TEXT is not ok", not chan.Reply(chan.Answer.TEXT, text="maybe").ok,
          "a typed answer is information, not consent")
    check("TIMEOUT is not ok", not chan.Reply(chan.Answer.TIMEOUT).ok,
          "silence must never read as approval")
    check("timeout summary states the wait",
          "no answer" in chan.Reply(chan.Answer.TIMEOUT, waited_s=42).summary())

    print("\n11. tools are registered")
    import tools_sdk
    names = {getattr(t, "name", None) for t in tools_sdk.ALL_TOOLS}
    for n in ("list_credentials", "use_credential", "ask_user", "notify_user"):
        check(f"{n} exposed to the model", n in names, str(sorted(n for n in names if n))[:0])
    check("credential tool is documented in the prompt",
          "use_credential" in tools_sdk.SYSTEM_PROMPT
          and "never shown to you" in tools_sdk.SYSTEM_PROMPT)

    print("\n12. the app window can answer a blocked task")
    import threading
    import app as appmod

    # A real Agent, not a hand-built stand-in: mirroring __init__ here is how
    # the test silently drifts from the code it is meant to be checking.
    # Constructing one opens no window — the UI is bound separately in main().
    a = appmod.Agent()

    st = a.get_state()
    check("state carries the pending question", "question" in st)
    check("state carries channel status", "channel" in st)
    check("nothing pending when idle", st["question"] is None)
    check("answering nothing is refused",
          a.answer_question("approve")["ok"] is False,
          "a stray click must not satisfy a question that was never asked")

    chan.CONFIG_FILE = tmp / "channel.json"
    c = chan.Channel()
    c.telegram, c.mail = chan.Telegram(), chan.Mail()
    c.app_ask = a._ask_in_app
    try:
        seen = {}

        def answer_soon():
            for _ in range(60):
                if a._question:
                    seen["text"] = a._question["text"]
                    seen["result"] = a.answer_question("approve")
                    return
                time.sleep(0.05)

        th = threading.Thread(target=answer_soon)
        th.start()
        r = c.ask("Proceed with the transfer?", timeout_s=8)
        th.join(timeout=5)
        check("the window saw the question",
              "transfer" in seen.get("text", ""), seen.get("text", "")[:40])
        check("the task received the answer", r.ok and r.via == "app", r.summary())
        check("the prompt clears after answering", a.get_state()["question"] is None)
        check("a running task is what gets unblocked",
              seen.get("result", {}).get("ok") is True)

        # Nobody home: the app path must give up and let the next channel try,
        # rather than holding the task forever.
        t0 = time.time()
        r2 = c.ask("Anyone there?", timeout_s=1.0)
        check("unanswered app question falls through",
              r2.answer is chan.Answer.UNREACHABLE, r2.summary())
        check("and does not hang", time.time() - t0 < 4.0,
              f"{time.time() - t0:.1f}s")
        check("prompt is cleared after a timeout too",
              a.get_state()["question"] is None,
              "a stale prompt would block the composer forever")
    finally:
        chan.CONFIG_FILE = orig_cfg

    print("\n13. pause holds the machine without losing the work")
    import tools_sdk

    check("starts unpaused", not tools_sdk.is_paused())
    tools_sdk.set_paused(True)
    check("pause is visible to the UI",
          tools_sdk.is_paused() and a.get_state()["paused"] is True)

    # A paused tool must BLOCK, not return an error — an error would make the
    # model try something else, and "paused" has to mean nothing happens.
    released, out = threading.Event(), {}

    def call_a_tool():
        out["t0"] = time.time()
        out["result"] = tools_sdk._blocked()
        out["t1"] = time.time()
        released.set()

    th = threading.Thread(target=call_a_tool, daemon=True)
    th.start()
    time.sleep(0.8)
    check("a tool call is held, not rejected", not released.is_set(),
          "returning an error would make the model improvise around the pause")
    tools_sdk.set_paused(False)
    released.wait(timeout=5)
    check("resuming releases it", released.is_set())
    check("and it was actually held", out.get("t1", 0) - out.get("t0", 0) > 0.7,
          f"{(out.get('t1',0)-out.get('t0',0)):.1f}s")
    check("with no arbiter it then proceeds", out.get("result") is None,
          str(out.get("result"))[:50])
    check("pause is cleared afterwards", not tools_sdk.is_paused())

    print("\n14. the UI can say what is happening right now")
    a._current = {"tool": "web_read", "detail": "", "since": time.time() - 3}
    a.running, a._started_at = True, time.time() - 42
    s2 = a.get_state()
    check("names the running tool", s2["current"]["tool"] == "web_read")
    check("reports total elapsed", 41 < s2["elapsed"] < 45,
          f"{s2['elapsed']:.0f}s")
    a.running = False
    check("elapsed is zero when idle", a.get_state()["elapsed"] == 0)
    a._current = None

    # Stop must release a pause, or the task parks inside a tool forever and
    # never reaches the cancel check.
    tools_sdk.set_paused(True)
    a.stop_task()
    check("stop releases a pause", not tools_sdk.is_paused(),
          "otherwise Stop does nothing to a paused task")


    print("\n15. a wait on a human can be interrupted")
    # These waits run on the event loop the SDK reads messages on. A blocking
    # wait there means the cancel check in the message loop never gets a turn,
    # so Stop and ESC+ESC cannot be noticed until the wait ends by itself —
    # up to a quarter of an hour for a Telegram question nobody answers.
    import threading as _th
    import tools_sdk as _ts

    class _FakeTG(chan.Telegram):
        configured = True                      # pretend paired
        def _call(self, *a, **k):
            time.sleep(0.2)
            return {"ok": True, "result": []}
        def send(self, *a, **k):
            return True
        def _updates(self, timeout=5):
            time.sleep(0.2)
            return []

    tg = _FakeTG()
    tg.chat_id = "fake"

    _ts.set_cancelled(False)
    t0 = time.time()
    tg.ask("Approve?", timeout_s=4, cancelled=None)
    uninterrupted = time.time() - t0
    check("without a hook it waits the whole timeout", uninterrupted >= 3.5,
          f"{uninterrupted:.1f}s of 4s")

    _ts.set_cancelled(False)
    _th.Timer(1.0, lambda: _ts.set_cancelled(True)).start()
    t0 = time.time()
    r = tg.ask("Approve?", timeout_s=4, cancelled=_ts.should_stop)
    interrupted = time.time() - t0
    check("Stop cuts it short", interrupted < uninterrupted / 2,
          f"{interrupted:.1f}s vs {uninterrupted:.1f}s")
    check("and it does not report approval",
          not r.ok, f"{r.answer.value} — silence is never consent")
    _ts.set_cancelled(False)

    check("ESC+ESC counts as stopping too",
          "aborted" in _ts.should_stop.__doc__.lower()
          or "any route" in _ts.should_stop.__doc__.lower(),
          "should_stop covers both the button and the kill switch")
    check("a new task clears the flag", not _ts.should_stop())


    print("\n16. redaction is cheap, and does not lie about usage")
    # Redaction runs on every outbound message. It used to decrypt the whole
    # vault each time — 187ms with a dozen secrets, growing linearly, pulling
    # every stored password through memory to check a progress line that
    # almost never contains one.
    rv = Vault(tmp / "redact_vault.json")
    for i in range(10):
        rv.put(f"s{i}", f"value-number-{i}-abcdefgh")

    redact("warm the cache", rv)
    t0 = time.time()
    for _ in range(20):
        redact("an ordinary progress message with nothing secret in it", rv)
    per_ms = (time.time() - t0) / 20 * 1000
    check("a repeat call is fast", per_ms < 5.0, f"{per_ms:.2f}ms per call")

    check("it still redacts", "«s3»" in redact("leak value-number-3-abcdefgh", rv))
    rv.put("added-later", "brand-new-value-xyz")
    check("a newly stored secret is picked up",
          "«added-later»" in redact("and brand-new-value-xyz too", rv),
          "a cache that goes stale would leak the newest secret of all")

    print("\n17. peeking is not using")
    before = rv.info("s1").uses
    for _ in range(5):
        redact("nothing sensitive here", rv)
    check("redaction does not count as a use", rv.info("s1").uses == before,
          f"{before} -> {rv.info('s1').uses} — otherwise 'uses' counts "
          f"progress messages, not times the credential was typed")
    rv.get("s1")
    check("actually using it does count", rv.info("s1").uses == before + 1)
    check("peek returns the same value as get",
          rv.peek("s1") == "value-number-1-abcdefgh")
    try:
        rv.peek("no-such-secret")
        check("peek on a missing name raises", False)
    except VaultError:
        check("peek on a missing name raises", True)

    print("\n" + "=" * 72)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"{passed}/{len(results)} passed")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name}  {detail}")
    print("=" * 72)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
