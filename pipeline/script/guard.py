"""The check that runs before a click, and that no model may overrule.

Mirrors `agent/core/fast.py`'s `code_side_commit`, deliberately as a
separate copy rather than an import, because this package is meant to run
without the agent present. The two lists should be kept in step; if they
drift, the safer of the two wins, and neither may shrink without a reason
written down.

The reasoning is jev-1.13's documented failure mode 6 — *state is data, and
the model does not treat it as hostile*. On a checkout page the merchant
wrote the button labels. A model asked "is this safe to click?" is reading
the defendant's own testimony. So the decision of whether an action is
irreversible is made HERE, in code, from the text of the thing about to be
clicked, and a model may only ever ADD caution to that answer, never
remove it.
"""

from __future__ import annotations

import re

#: Words that make an action worth stopping for. Turkish and English,
#: because that is what this machine drives. Matched on folded text with
#: word boundaries — `_BUY` would otherwise fire on "buyer" and "ödeme
#: seçenekleri" would never be reached because "öde" matched first.
COMMIT_WORDS = (
    # money
    "pay", "checkout", "buy", "order", "purchase", "confirm", "place order",
    "öde", "ödeme", "satın al", "sipariş", "onayla", "siparişi tamamla",
    # destructive
    "delete", "remove", "erase", "format", "uninstall", "reset",
    "sil", "kaldır", "biçimlendir", "sıfırla", "çıkar", "boşalt",
    # outward-facing
    "send", "post", "publish", "share", "submit", "apply",
    "gönder", "yayınla", "paylaş",
    # account
    "sign out", "log out", "deactivate", "close account", "change password",
    "çıkış", "hesabı kapat", "şifre değiştir",
)

_PATTERNS = tuple(
    re.compile(r"(?<!\w)" + re.escape(w) + r"(?!\w)", re.IGNORECASE)
    for w in COMMIT_WORDS)


def fold(s: str) -> str:
    return (s.replace("İ", "i").replace("I", "ı")
             .replace("Ş", "ş").replace("Ğ", "ğ").casefold())


def is_commit(*texts: str) -> str:
    """The word that makes this a commitment, or "" if there is none.

    Takes several strings because a button's identity is spread across its
    name, its automation id and any label bound to it — and the one that
    says "öde" is not reliably the one a selector matched on.
    """
    for text in texts:
        if not text:
            continue
        folded = fold(text)
        for word, pattern in zip(COMMIT_WORDS, _PATTERNS):
            if pattern.search(folded):
                return word
    return ""
