"""Reaching the user when they are not at the machine.

An unattended job that pauses at 2am and waits for someone to notice has
failed, even though nothing errored. This is how it asks.

Routing, in order of preference:

    app        instant, and the user is already looking at it
    telegram   seconds, push, inline Approve / Deny buttons
    mail       minutes, no buttons — a last resort

Telegram is first among the remote options because it has an official bot API:
a token, a chat id, and replies come back over the same interface. WhatsApp Web
would mean automating an unofficial surface that breaks on their updates and
needs a browser session pinned open.

**A Telegram bot cannot start a conversation.** There is no send-to-@username
API — the platform blocks bot-initiated contact to prevent spam. So pairing is
mandatory: the user opens t.me/<bot>?start=<token> once, and only then does the
bot learn the chat id it is allowed to write to. The token proves it is them
rather than anyone else who found the bot.
"""

from __future__ import annotations

import json
import queue
import secrets as _pysecrets
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from . import paths

from .secrets import vault, VaultError

CONFIG_FILE = paths.data("channel.json")
API = "https://api.telegram.org/bot{token}/{method}"


class Answer(str, Enum):
    APPROVE = "approve"
    DENY = "deny"
    TEXT = "text"
    TIMEOUT = "timeout"
    UNREACHABLE = "unreachable"


@dataclass
class Reply:
    answer: Answer
    text: str = ""
    via: str = ""
    waited_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.answer is Answer.APPROVE

    def summary(self) -> str:
        if self.answer is Answer.TIMEOUT:
            return f"no answer after {self.waited_s:.0f}s"
        if self.answer is Answer.UNREACHABLE:
            return "could not reach the user on any channel"
        return f"{self.answer.value} via {self.via}" + (f": {self.text}" if self.text else "")


def _load() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

class Telegram:
    """Bot transport. Token lives in the vault; only the chat id is on disk."""

    def __init__(self) -> None:
        cfg = _load().get("telegram", {})
        self.chat_id: str | None = cfg.get("chat_id")
        self.pair_token: str | None = cfg.get("pair_token")
        self.bot_username: str | None = cfg.get("bot_username")
        self._offset = cfg.get("offset", 0)

    # -- config ----------------------------------------------------------
    @property
    def token(self) -> str | None:
        try:
            return vault.get("telegram-bot-token")
        except VaultError:
            return None

    def set_token(self, token: str) -> dict:
        vault.put("telegram-bot-token", token.strip(),
                  note="Telegram bot token from @BotFather")
        me = self._call("getMe") or {}
        user = (me.get("result") or {}).get("username")
        cfg = _load()
        tg = cfg.setdefault("telegram", {})
        if user:
            tg["bot_username"] = user
            self.bot_username = user
        _save(cfg)
        return {"ok": bool(user), "bot_username": user}

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    # -- pairing ---------------------------------------------------------
    def pairing_link(self) -> dict:
        """Produce the one-time link the user must open.

        This step cannot be skipped: until they press Start, the bot has no
        chat id and Telegram will not let it write to them.
        """
        if not self.token:
            return {"ok": False,
                    "error": "no bot token yet — create a bot with @BotFather "
                             "and save its token first"}
        if not self.bot_username:
            me = self._call("getMe") or {}
            self.bot_username = (me.get("result") or {}).get("username")
        if not self.bot_username:
            return {"ok": False, "error": "token rejected by Telegram"}

        tok = _pysecrets.token_hex(3)
        cfg = _load()
        cfg.setdefault("telegram", {})["pair_token"] = tok
        _save(cfg)
        self.pair_token = tok
        return {"ok": True,
                "link": f"https://t.me/{self.bot_username}?start={tok}",
                "instructions": "Open the link, press Start, then click Finish pairing."}

    def complete_pairing(self, wait_s: float = 90.0) -> dict:
        """Look for the /start message carrying our token."""
        if not self.token or not self.pair_token:
            return {"ok": False, "error": "call pairing_link() first"}
        deadline = time.time() + wait_s
        while time.time() < deadline:
            for upd in self._updates(timeout=8):
                msg = upd.get("message") or {}
                text = (msg.get("text") or "").strip()
                if text.startswith("/start") and self.pair_token in text:
                    chat = str((msg.get("chat") or {}).get("id"))
                    cfg = _load()
                    tg = cfg.setdefault("telegram", {})
                    tg["chat_id"] = chat
                    tg.pop("pair_token", None)
                    _save(cfg)
                    self.chat_id = chat
                    self.pair_token = None
                    self.send("Paired. I'll message you here when a task needs you.")
                    return {"ok": True, "chat_id": chat}
            time.sleep(1.0)
        return {"ok": False, "error": "no /start received — open the link and press Start"}

    # -- transport -------------------------------------------------------
    def _call(self, method: str, **params) -> dict | None:
        tok = self.token
        if not tok:
            return None
        url = API.format(token=tok, method=method)
        body = urllib.parse.urlencode(
            {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
             for k, v in params.items()}).encode()
        try:
            with urllib.request.urlopen(url, data=body, timeout=25) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            return None

    def _updates(self, timeout: int = 5) -> list[dict]:
        res = self._call("getUpdates", offset=self._offset + 1, timeout=timeout)
        if not res or not res.get("ok"):
            return []
        ups = res.get("result", [])
        if ups:
            self._offset = ups[-1]["update_id"]
            cfg = _load()
            cfg.setdefault("telegram", {})["offset"] = self._offset
            _save(cfg)
        return ups

    def send(self, text: str, buttons: list[tuple[str, str]] | None = None) -> bool:
        if not self.chat_id:
            return False
        params: dict = {"chat_id": self.chat_id, "text": text[:4000]}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": [
                [{"text": label, "callback_data": data} for label, data in buttons]]}
        res = self._call("sendMessage", **params)
        return bool(res and res.get("ok"))

    def ask(self, question: str, timeout_s: float = 900.0,
            approve_deny: bool = True, cancelled=None) -> Reply:
        """Send a question and wait for a button press or a typed answer.

        `cancelled` is checked on every pass. Without it this loop could hold
        the agent for a quarter of an hour with Stop and ESC+ESC both unable
        to reach it — the wait blocks the event loop those signals travel on.
        """
        t0 = time.time()
        if not self.configured:
            return Reply(Answer.UNREACHABLE, via="telegram")
        buttons = [("✅ Approve", "approve"), ("✖ Deny", "deny")] if approve_deny else None
        if not self.send(question, buttons):
            return Reply(Answer.UNREACHABLE, via="telegram")

        while time.time() - t0 < timeout_s:
            if cancelled is not None and cancelled():
                return Reply(Answer.TIMEOUT, via="telegram",
                             text="cancelled", waited_s=time.time() - t0)
            for upd in self._updates(timeout=10):
                cb = upd.get("callback_query")
                if cb:
                    data = cb.get("data", "")
                    self._call("answerCallbackQuery", callback_query_id=cb["id"],
                               text="Got it")
                    return Reply(Answer.APPROVE if data == "approve" else Answer.DENY,
                                 via="telegram", waited_s=time.time() - t0)
                msg = upd.get("message") or {}
                text = (msg.get("text") or "").strip()
                if text and not text.startswith("/"):
                    low = text.lower()
                    if low in ("yes", "y", "ok", "approve", "go"):
                        return Reply(Answer.APPROVE, text=text, via="telegram",
                                     waited_s=time.time() - t0)
                    if low in ("no", "n", "deny", "stop"):
                        return Reply(Answer.DENY, text=text, via="telegram",
                                     waited_s=time.time() - t0)
                    return Reply(Answer.TEXT, text=text, via="telegram",
                                 waited_s=time.time() - t0)
        return Reply(Answer.TIMEOUT, via="telegram", waited_s=time.time() - t0)


# ---------------------------------------------------------------------------
# Mail (send only)
# ---------------------------------------------------------------------------

class Mail:
    """One-way notification. No reply parsing — mail cannot carry a decision
    quickly enough to be worth the complexity, so it informs rather than asks."""

    def __init__(self) -> None:
        cfg = _load().get("mail", {})
        self.host = cfg.get("host", "")
        self.port = int(cfg.get("port", 587))
        self.user = cfg.get("user", "")
        self.to = cfg.get("to", "")

    @property
    def configured(self) -> bool:
        return bool(self.host and self.user and self.to)

    def configure(self, host: str, port: int, user: str, password: str, to: str) -> None:
        vault.put("mail-password", password, note=f"SMTP password for {user}")
        cfg = _load()
        cfg["mail"] = {"host": host, "port": int(port), "user": user, "to": to}
        _save(cfg)
        self.host, self.port, self.user, self.to = host, int(port), user, to

    def send(self, subject: str, body: str) -> bool:
        if not self.configured:
            return False
        import smtplib
        from email.message import EmailMessage
        try:
            pw = vault.get("mail-password")
        except VaultError:
            return False
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = self.user, self.to, subject
        m.set_content(body)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=25) as s:
                s.starttls()
                s.login(self.user, pw)
                s.send_message(m)
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class Channel:
    """Ask the user something, wherever they happen to be."""

    def __init__(self) -> None:
        self.telegram = Telegram()
        self.mail = Mail()
        #: set by the app while its window is open, so questions surface there
        self.app_ask = None
        self.pending: dict | None = None
        self._inbox: queue.Queue = queue.Queue()

    def status(self) -> dict:
        return {
            "app": self.app_ask is not None,
            "telegram": {"configured": self.telegram.configured,
                         "bot": self.telegram.bot_username,
                         "paired": bool(self.telegram.chat_id)},
            "mail": {"configured": self.mail.configured, "to": self.mail.to},
            "pending": self.pending,
        }

    def notify(self, text: str) -> list[str]:
        """Tell the user something. No answer expected."""
        sent = []
        from .secrets import redact
        text = redact(text)
        if self.telegram.configured and self.telegram.send(text):
            sent.append("telegram")
        if self.mail.configured and self.mail.send("AGI Agent", text):
            sent.append("mail")
        return sent

    def ask(self, question: str, approve_deny: bool = True,
            timeout_s: float = 900.0, cancelled=None) -> Reply:
        """Route a question to wherever the user can answer it."""
        from .secrets import redact
        question = redact(question)
        self.pending = {"question": question, "since": time.time()}
        from . import tasklog
        tasklog.event("question", text=question, approve_deny=approve_deny)
        try:
            reply = self._ask(question, approve_deny, timeout_s, cancelled)
            tasklog.event("answer", answer=reply.answer.value, text=reply.text,
                          via=reply.via, waited_s=round(reply.waited_s, 1))
            return reply
        finally:
            self.pending = None

    def _ask(self, question: str, approve_deny: bool, timeout_s: float,
             cancelled) -> Reply:
        try:
            if self.app_ask is not None:
                try:
                    r = self.app_ask(question, approve_deny, timeout_s)
                    if r is not None:
                        return r
                except Exception:
                    pass
            if cancelled is not None and cancelled():
                return Reply(Answer.TIMEOUT, text="cancelled")
            if self.telegram.configured:
                return self.telegram.ask(question, timeout_s, approve_deny,
                                         cancelled=cancelled)
            if self.mail.configured:
                self.mail.send("AGI Agent needs you", question
                               + "\n\n(Reply is not read — answer in the app.)")
                return Reply(Answer.TIMEOUT, via="mail")
            return Reply(Answer.UNREACHABLE)
        finally:
            self.pending = None


channel = Channel()
