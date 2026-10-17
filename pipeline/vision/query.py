"""Asking UI Automation a question, instead of reading the whole tree.

`elements.look()` walks every node and filters in Python. That is the right
shape when you want to know *what is on screen* — a scene for a model to
read, or a page you have never seen. It is the wrong shape when you
already know what you are looking for, and it is expensive for a reason
that does not improve: every property read is a cross-process COM call, and
a Chrome page is about 1800 nodes.

UIA can do the filtering on the provider's side. Measured on a live Migros
page, same window, same moment:

    full walk + filter in Python      879 ms     231 elements
    FindFirst by AutomationId          19 ms     exact hit
    FindAll  by Name                   40 ms

**46x**, and it is not a trick — the search runs inside the application
that owns the tree, and only the survivors cross the process boundary.

So the rule this module exists to enforce: **a selector that names
something should never cause a full walk.** `id` is a property condition.
`kind` is a property condition. An exact `text` is a property condition.
Only a substring match over unknown content needs the whole tree, and even
then the kind filter can shrink it first.

What this deliberately does NOT do is replace `look()`. OCR, templates and
the merge still need a scene, and the first look at an unfamiliar window
still has to enumerate it. This is the fast path for the steps in between,
which is where the time actually goes.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from .elements import KINDS, Element

_AGENT = Path(__file__).resolve().parents[2] / "agent"
if str(_AGENT) not in sys.path:
    sys.path.insert(0, str(_AGENT))

#: TreeScope_Children | TreeScope_Descendants. Subtree (7) would include the
#: root itself, which for a window root matches far too eagerly.
SCOPE = 2 | 4

#: What a query may hand back: the walk's kinds, plus a static TEXT control.
#: The walk leaves text to OCR, but a query only ever returns what a
#: selector named exactly (an id, an exact name), and dropping a match here
#: reads as "conclusively absent": Calculator's display (id
#: CalculatorResults, "Display is 0") and its mode header failed every
#: `expect` on 2026-10-16 while plainly on screen.
QUERY_KINDS = {**KINDS, "TextControl": "text"}

#: Our plain kind word -> the UIA ControlType id, for a property condition.
#: Built by inverting `QUERY_KINDS` so the two can never drift apart.
_TYPE_ID: dict[str, list[int]] = {}


def queryable(kind: str) -> bool:
    """Can a property condition express this kind at all?"""
    return not kind or kind.lower() in _type_ids()


def _type_ids() -> dict[str, list[int]]:
    if not _TYPE_ID:
        import uiautomation as auto
        for type_name, kind in QUERY_KINDS.items():
            got = getattr(auto.ControlType, type_name, None)
            if got is not None:
                _TYPE_ID.setdefault(kind, []).append(int(got))
    return _TYPE_ID


def _client():
    from uiautomation.uiautomation import _AutomationClient
    return _AutomationClient.instance().IUIAutomation


def available() -> bool:
    """Is the provider-side query path usable at all?

    The private `_AutomationClient` is what the library uses itself, but it
    is private, so a version bump could move it. Callers fall back to a
    full look rather than failing — slower, never wrong.
    """
    try:
        return _client() is not None
    except Exception:
        return False


def _condition(aid: str = "", name: str = "", kinds: tuple[str, ...] = ()):
    """AND of the property conditions this selector can express."""
    import uiautomation as auto

    iu = _client()
    parts = []
    if aid:
        parts.append(iu.CreatePropertyCondition(
            auto.PropertyId.AutomationIdProperty, aid))
    if name:
        parts.append(iu.CreatePropertyCondition(
            auto.PropertyId.NameProperty, name))
    if kinds:
        ids = [i for k in kinds for i in _type_ids().get(k.lower(), [])]
        if ids:
            ors = [iu.CreatePropertyCondition(
                auto.PropertyId.ControlTypeProperty, i) for i in ids]
            parts.append(ors[0] if len(ors) == 1
                         else iu.CreateOrConditionFromArray(ors))
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else iu.CreateAndConditionFromArray(parts)


def _to_element(raw: Any) -> Element | None:
    """Wrap one provider result, reading properties ONLY for survivors.

    This is where the saving is spent rather than lost: the full walk reads
    name, rectangle, value and id for all 1800 nodes. Here there are a
    handful, so the same reads cost nothing.
    """
    import uiautomation as auto

    try:
        ctrl = auto.Control.CreateControlFromElement(raw)
        if ctrl is None:
            return None
        kind = QUERY_KINDS.get(ctrl.ControlTypeName)
        if not kind:
            return None
        r = ctrl.BoundingRectangle
        left, top, right, bottom = r.left, r.top, r.right, r.bottom
        if right <= left or bottom <= top:
            return None
        name = (ctrl.Name or "").strip()
        value = ""
        if kind in ("edit", "combobox"):
            try:
                if not getattr(ctrl, "IsPassword", False):
                    value = (ctrl.GetValuePattern().Value or "")[:40]
            except Exception:
                value = ""
        try:
            aid = (ctrl.AutomationId or "")[:40]
        except Exception:
            aid = ""
        return Element(kind, name, left, top, right - left, bottom - top,
                       "uia", True, value, aid, 1.0, ctrl)
    except Exception:
        return None


def find(hwnd: int, aid: str = "", name: str = "",
         kinds: tuple[str, ...] = (), limit: int = 60) -> list[Element]:
    """Elements matching these exact properties, asked of the provider.

    Everything here is an EXACT match — that is what a property condition
    is. Substring matching stays in `resolve.py`, over whatever this
    returns or over a full scene when no condition applies.
    """
    from core import locator

    try:
        locator.invalidate_cache(hwnd)
        root = locator.uia_root(hwnd)
        cond = _condition(aid, name, kinds)
        if cond is None:
            return []
        found = root.Element.FindAll(SCOPE, cond)
        if not found:
            return []
        out = []
        for i in range(min(found.Length, limit)):
            got = _to_element(found.GetElement(i))
            if got is not None:
                out.append(got)
        return out
    except Exception:
        return []


def first(hwnd: int, aid: str = "", name: str = "",
          kinds: tuple[str, ...] = ()) -> Element | None:
    got = find(hwnd, aid, name, kinds, limit=1)
    return got[0] if got else None
