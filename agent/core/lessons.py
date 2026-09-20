"""Hard-won task knowledge: what went wrong, and what finally worked.

A *macro* (see `store.py`) records the exact steps of one repeatable job. A
*lesson* is smaller and more general: the single thing you had to discover the
hard way about an app or a website, so the next task of that kind begins
already knowing it.

    title:    Finding a chat in WhatsApp Web
    problem:  The chats are not in the accessibility tree by name, and the
              ctrl+alt+/ search shortcut does nothing on a Turkish keyboard.
    solution: find_controls(window="WhatsApp", filter="<the group name>")
              lists the chat as a DataItem; click it by name. The list has to
              have rendered first.

Two halves, matching what the user asked for:

  * **recall** — the relevant lessons are put in front of the model *before*
    it plans a task, so checking them costs nothing. `app.py` calls
    `recall()` at the start of every task; the model may also call it directly
    through the `recall_lessons` tool.
  * **save** — the model calls `save_lesson` after it struggled and then
    succeeded, writing down the fix so the struggle is not repeated.

Retrieval reuses `retrieval.Index` — the same IDF-weighted lexical scorer the
job library uses — so behaviour (Turkish folding, fuzzy typo matching, stop
words) is identical and there is one thing to reason about, not two.

Storage is a single JSON file under the data root, written atomically. A
lesson library is tens of entries of a few lines each; a file per lesson would
be ceremony for nothing.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from difflib import SequenceMatcher
from typing import Any

from core import paths
from core.retrieval import Index, _fold, _similar


def _store() -> Any:
    """Path to the lesson file, resolved on each call.

    Not a module constant: `paths.data()` honours DESKTOP_AGENT_DATA, and the
    tests set that to a temp directory so a run never touches the real
    library. A constant captured at import would ignore the override.
    """
    return paths.data("lessons.json")

#: Below this retrieval score a lesson is not worth surfacing. Lower than the
#: job library's 0.5: a task description is broad ("send a message to the
#: group") and may land on only the app name and one word of the problem, yet
#: still be the lesson that saves the run.
RECALL_FLOOR = 0.32

#: Two lessons this similar in title are the same lesson said twice. Saving the
#: second updates the first rather than cluttering recall with near-duplicates.
DEDUP_TITLE_SIM = 0.85


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _slug(text: str) -> str:
    base = "-".join(_fold(text).split())[:48].strip("-")
    return base or "lesson"


class LessonStoreError(Exception):
    """The library exists but could not be read, so it must not be written."""


def _read() -> tuple[list[dict], bool]:
    """(lessons, readable). `readable` is False only when the file is THERE
    but unusable — locked by an indexer or antivirus, or hand-edited into
    invalid JSON. An absent file is perfectly readable: it means no lessons
    yet."""
    p = _store()
    if not p.exists():
        return [], True
    try:
        raw = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError too: the file can be there, readable by the OS,
        # and still not be text we wrote.
        return [], False
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return [], False
    if not isinstance(data, list):
        return [], False
    # Keep only entries that are actually lessons. A hand-edited file can
    # contain a bare number or string, and every consumer here assumes a dict
    # — one stray value used to turn recall into an AttributeError.
    return [r for r in data if isinstance(r, dict)], True


def load() -> list[dict]:
    """Every lesson on file. Never raises — recall must not be able to fail a
    task, so an unreadable library simply recalls nothing."""
    rows, _ = _read()
    return rows


def _save_all(rows: list[dict]) -> None:
    """Atomic write: a crash mid-save must not truncate the library. Write a
    sibling temp file, flush it, then replace — os.replace is atomic on NTFS."""
    store = _store()
    store.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(store.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, store)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _norm_list(value: Any) -> list[str]:
    """Accept apps/keywords as a list, or as the comma/space string the tool
    layer receives — the SDK tool schema only has strings."""
    if not value:
        return []
    if isinstance(value, str):
        # Split on separators only, not spaces, so "google chrome" stays one
        # entry. The retrieval index tokenises the words itself.
        for sep in (";", "\n"):
            value = value.replace(sep, ",")
        items = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = [str(p) for p in value]
    else:
        return []
    return sorted({p.strip() for p in items if p and p.strip()})


def _row_for_index(L: dict) -> dict:
    """Shape a lesson for `retrieval.Index`.

    The task query at recall time names the app and the situation, so the
    fields that must match it get the heaviest weights: the title (name, x3)
    and the problem (description, x3), which is where the app and symptom are
    described. The solution is indexed too but at intent weight (x1) — it is
    the answer, not what the user's phrasing will resemble.
    """
    # Every field is coerced, not trusted: a hand-edited file can hold a bare
    # string where a list belongs, and "keywords" + "apps" would then either
    # concatenate two strings or raise.
    kws, apps = _norm_list(L.get("keywords")), _norm_list(L.get("apps"))
    return {
        "id": str(L.get("id", "")),
        "name": str(L.get("title", "") or ""),
        "description": str(L.get("problem", "") or ""),
        "tags": kws + apps,
        "requires": apps,
        "intents": [str(L.get("solution", "") or "")],
    }


def recall(query: str, limit: int = 3, min_score: float = RECALL_FLOOR) -> list[dict]:
    """The lessons most relevant to what is about to be attempted.

    Returns full lesson dicts (with a `score` added), highest first. Broad by
    design — a couple of near-misses in front of the model are cheap; a missed
    lesson means repeating a solved struggle.
    """
    rows = load()
    if not rows or not (query or "").strip():
        return []
    by_id = {L.get("id", ""): L for L in rows}
    index = Index([_row_for_index(L) for L in rows])
    # IDF is computed over the library itself, so with only a handful of
    # lessons no term can distinguish anything and every score is compressed
    # towards the same small number: a solid single-word hit on a one-lesson
    # library scores about 0.19, well under a bar tuned for a full one. The
    # first lessons ever saved would be the ones that never came back.
    # Measured, so the constant is not a guess:
    #   1 lesson,  "type into excel but keys do nothing" -> 0.19 (matched excel)
    #   1 lesson,  "what is currently on my screen"      -> 0.00 (no match)
    # The bar has to sit between those, and lowering it is safe precisely
    # because a small library cannot produce many candidates to be wrong about.
    if len(rows) < 5:
        min_score = min(min_score, RECALL_FLOOR / 3)
    hits = index.search(query, limit=limit, min_score=min_score)
    out = []
    for c in hits:
        L = by_id.get(c.id)
        if L:
            out.append({**L, "score": c.score})
    return out


def _title_sim(a: str, b: str) -> float:
    """Similarity for dedup only.

    Deliberately NOT `retrieval._similar`: that treats any shared prefix as
    0.9, which is right for matching a query to a job and wrong here — it
    made "Excel: save as CSV with UTF-8 BOM" count as the same lesson as
    "Excel: save as CSV" and overwrite it. A plain ratio keeps a longer,
    more specific title as its own lesson.
    """
    return SequenceMatcher(None, a, b).ratio()


def _find_duplicate(rows: list[dict], title: str) -> dict | None:
    ft = _fold(title)
    for L in rows:
        fl = _fold(L.get("title", ""))
        if fl == ft or _title_sim(fl, ft) >= DEDUP_TITLE_SIM:
            return L
    return None


def add(title: str, problem: str, solution: str,
        apps: Any = None, keywords: Any = None,
        source_task: str = "") -> tuple[dict, bool]:
    """Record a lesson, or refresh an existing one on the same subject.

    Returns (lesson, created): created is False when an existing lesson with a
    matching title was updated instead. The freshest problem/solution wins,
    because a lesson is usually re-saved precisely because the last wording was
    not quite right; apps and keywords accumulate rather than replace.
    """
    title = (title or "").strip()
    problem = (problem or "").strip()
    solution = (solution or "").strip()
    apps_l = _norm_list(apps)
    kw_l = _norm_list(keywords)
    rows, readable = _read()
    if not readable:
        # The file is there and we could not parse or open it. Writing now
        # would replace every stored lesson with this one. Move it aside if
        # possible — it is evidence — and otherwise refuse outright.
        p = _store()
        try:
            p.replace(p.with_name(p.name + ".corrupt"))
            rows = []
        except OSError as e:
            raise LessonStoreError(
                f"{p.name} could not be read or moved aside ({e}); refusing to "
                f"overwrite it") from e

    dup = _find_duplicate(rows, title) if title else None
    if dup is not None:
        dup["title"] = title or dup["title"]
        if problem:
            dup["problem"] = problem
        if solution:
            dup["solution"] = solution
        dup["apps"] = sorted(set(dup.get("apps") or []) | set(apps_l))
        dup["keywords"] = sorted(set(dup.get("keywords") or []) | set(kw_l))
        dup["updated"] = _now()
        dup["revisions"] = int(dup.get("revisions") or 0) + 1
        _save_all(rows)
        return dup, False

    now = _now()
    lesson = {
        # A slug for a human reading the file, a random suffix for uniqueness:
        # the old per-second suffix repeated every 28 hours, and two lessons
        # sharing an id made recall return one of them twice and remove()
        # delete both.
        "id": f"{_slug(title)}-{uuid.uuid4().hex[:8]}",
        "title": title,
        "problem": problem,
        "solution": solution,
        "apps": apps_l,
        "keywords": kw_l,
        "source_task": (source_task or "")[:200],
        "created": now,
        "updated": now,
        "revisions": 0,
    }
    rows.append(lesson)
    _save_all(rows)
    return lesson, True


def remove(lesson_id: str) -> bool:
    rows = load()
    kept = [L for L in rows if L.get("id") != lesson_id]
    if len(kept) == len(rows):
        return False
    _save_all(kept)
    return True


def build_prompt(task: str, limit: int = 3) -> tuple[str, list[dict]]:
    """The task as the model should receive it, with any lessons above it.

    Lives here rather than inline in the app so that the recall half can be
    tested at all: it is wrapped in a try/except at the call site, which means
    a broken version would silently do nothing and every test would still
    pass.
    """
    hits = recall(task, limit=limit)
    if not hits:
        return task, []
    return as_prompt_block(hits) + "\n\nTASK: " + task, hits


def as_prompt_block(hits: list[dict]) -> str:
    """Render recalled lessons as a compact preamble for the task prompt."""
    if not hits:
        return ""
    lines = ["--- WHAT YOU LEARNED FROM PAST TASKS (apply these before improvising) ---"]
    for L in hits:
        lines.append(f"• {L.get('title', '(untitled)')}")
        if L.get("apps"):
            lines.append(f"    Applies to: {', '.join(L['apps'])}")
        if L.get("problem"):
            lines.append(f"    Problem: {L['problem']}")
        if L.get("solution"):
            lines.append(f"    What worked: {L['solution']}")
    lines.append("--- end of lessons ---")
    return "\n".join(lines)
