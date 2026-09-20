"""Hard-won task memory: saving what worked, recalling it next time.

The point of this feature is that a struggle is not repeated. So these checks
are mostly about the two ways it could quietly fail to deliver that: a lesson
that does not come back when the same kind of task is asked again, and a
near-duplicate saved every run until recall is noise. Storage runs against a
temp directory — a lesson must never leak into the real library from a test.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cleanup import scratch_dir  # noqa: E402

# Point every data path at a throwaway directory BEFORE anything reads it.
# scratch_dir removes it when the process exits, however the run ends.
_TMP = str(scratch_dir("lessons_test_"))
os.environ["DESKTOP_AGENT_DATA"] = _TMP

from core import lessons          # noqa: E402
import tools_sdk                  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""),
          flush=True)
    return bool(ok)


def _reset() -> None:
    """Empty the temp library between sections."""
    lessons._save_all([])


def test_storage_roundtrip():
    print("\n1. a lesson survives a save and load")
    _reset()
    lesson, created = lessons.add(
        title="Finding a chat in WhatsApp Web",
        problem="WhatsApp Web chats are not in the accessibility tree by name, "
                "and the ctrl+alt+/ search shortcut does nothing on a Turkish layout.",
        solution="find_controls with filter set to the group name lists the chat "
                 "as a DataItem; click it by name once the list has rendered.",
        apps="whatsapp, chrome", keywords="group, search, messaging")
    check("add reports it as newly created", created)
    rows = lessons.load()
    check("exactly one lesson on file", len(rows) == 1, f"{len(rows)}")
    check("apps parsed from the comma string", rows[0]["apps"] == ["chrome", "whatsapp"],
          str(rows[0]["apps"]))
    check("it has an id and timestamps",
          bool(rows[0].get("id")) and bool(rows[0].get("created")))
    check("the file really is under the temp dir, not the real library",
          str(lessons._store()).startswith(_TMP), str(lessons._store()))


def test_recall_finds_it():
    print("\n2. the same kind of task recalls the lesson")
    _reset()
    lessons.add(
        title="Finding a chat in WhatsApp Web",
        problem="WhatsApp Web chats are not exposed by name in the tree.",
        solution="Use find_controls with a filter of the group name.",
        apps="whatsapp")
    lessons.add(
        title="Trendyol blocks the headless browser",
        problem="Trendyol returns a Cloudflare challenge to the agent browser.",
        solution="Drive the user's own Chrome instead via the desktop tools.",
        apps="trendyol, chrome")

    hits = lessons.recall("send a message to a group on whatsapp")
    check("recall returns the WhatsApp lesson first",
          bool(hits) and hits[0]["title"].startswith("Finding a chat"),
          str([h["title"] for h in hits]))
    check("and it carries a score", bool(hits) and "score" in hits[0])

    other = lessons.recall("open trendyol and search for a watch")
    check("a different task recalls the Trendyol lesson",
          bool(other) and other[0]["title"].startswith("Trendyol"),
          str([h["title"] for h in other]))

    check("an unrelated task recalls nothing",
          lessons.recall("play some music on spotify") == [],
          str([h["title"] for h in lessons.recall("play some music on spotify")]))


def test_dedup_updates_not_duplicates():
    print("\n3. re-saving the same subject updates, never duplicates")
    _reset()
    lessons.add(title="Finding a chat in WhatsApp Web",
                problem="old problem", solution="old solution", apps="whatsapp")
    lesson, created = lessons.add(
        title="Finding a chat in WhatsApp web",           # different case only
        problem="new, better problem", solution="new, better solution",
        apps="chrome", keywords="group")
    rows = lessons.load()
    check("still one lesson, not two", len(rows) == 1, f"{len(rows)}")
    check("re-save reported as an update, not a create", created is False)
    check("the fresher solution won", rows[0]["solution"] == "new, better solution")
    check("apps accumulated across saves", rows[0]["apps"] == ["chrome", "whatsapp"],
          str(rows[0]["apps"]))
    check("a revision was counted", rows[0].get("revisions") == 1,
          str(rows[0].get("revisions")))


def test_turkish_query_meets_lesson():
    print("\n4. a lesson recalls across the Turkish/ASCII gap")
    _reset()
    lessons.add(title="İşlem geçmişini dışa aktarma",
                problem="Bankanın işlem sayfası tabloyu geç yüklüyor.",
                solution="Sayfayı kaydır, sonra web_read çağır.",
                apps="chrome", keywords="banka, islem")
    hits = lessons.recall("bankadan islem gecmisi disa aktar")
    check("folded Turkish query finds the lesson",
          bool(hits) and hits[0]["title"].startswith("İşlem"),
          str([h["title"] for h in hits]))


def test_prompt_block_renders():
    print("\n5. recalled lessons render as a task preamble")
    _reset()
    lessons.add(title="A demo lesson", problem="the problem", solution="the fix",
                apps="chrome")
    block = lessons.as_prompt_block(lessons.recall("a demo lesson"))
    check("block names the section", "WHAT YOU LEARNED FROM PAST TASKS" in block)
    check("block carries the solution", "the fix" in block, block[:80])
    check("an empty recall renders nothing", lessons.as_prompt_block([]) == "")


def test_tools_wired():
    print("\n6. the tools are exposed, offloaded, and work")
    names = {getattr(t, "name", None) for t in tools_sdk.ALL_TOOLS}
    check("save_lesson exposed to the model", "save_lesson" in names)
    check("recall_lessons exposed to the model", "recall_lessons" in names)
    lesson_tools = [t for t in tools_sdk.ALL_TOOLS
                    if t.name in ("save_lesson", "recall_lessons")]
    # Assert the count first: `all()` over an empty list is True, so without
    # this the check would pass loudest exactly when the tools went missing.
    check("both tools are present to be checked", len(lesson_tools) == 2,
          f"{len(lesson_tools)} found")
    check("both are offloaded to the worker thread like every tool",
          len(lesson_tools) == 2
          and all(getattr(t.handler, "_offloaded", False) for t in lesson_tools))

    _reset()
    out = asyncio.run(tools_sdk.save_lesson.handler(
        {"title": "Excel nag dialog on launch",
         "problem": "An unlicensed Excel shows a modal that swallows keystrokes.",
         "solution": "list_windows finds the dialog; close it before typing.",
         "apps": "excel"}))["content"][0]["text"]
    check("save_lesson confirms a save", "Saved" in out or "Updated" in out, out[:60])

    out = asyncio.run(tools_sdk.recall_lessons.handler(
        {"situation": "type into excel but keys do nothing"}))["content"][0]["text"]
    check("recall_lessons surfaces the saved lesson", "Excel nag dialog" in out, out[:80])

    empty = asyncio.run(tools_sdk.recall_lessons.handler(
        {"situation": "some totally unrelated xyzzy task"}))["content"][0]["text"]
    check("recall_lessons says so when nothing matches",
          "No lesson" in empty, empty[:60])


def test_credentials_never_saved():
    print("\n7. a credential in a lesson is redacted before it is written")
    from core import secrets
    _reset()
    # Store a secret, then try to save a lesson that quotes its value.
    vault = secrets.Vault(path=Path(_TMP) / "vault.json")
    secrets.vault = vault                       # redact() uses the module vault
    vault.put("test-pw", "hunter2-SECRET")
    try:
        asyncio.run(tools_sdk.save_lesson.handler(
            {"title": "Logging into the portal",
             "problem": "The password is hunter2-SECRET and it kept failing.",
             "solution": "Use use_credential 'test-pw', never type hunter2-SECRET.",
             "apps": "chrome"}))
        blob = lessons._store().read_text(encoding="utf-8")
        check("the secret value is not in the saved file",
              "hunter2-SECRET" not in blob,
              "leaked!" if "hunter2-SECRET" in blob else "")
    finally:
        secrets.vault = secrets.Vault()          # restore the default


def test_prompt_teaches_it():
    print("\n8. the system prompt tells the model to use task memory")
    p = tools_sdk.SYSTEM_PROMPT
    check("prompt mentions saving a lesson", "save_lesson" in p)
    check("prompt mentions the recalled-lessons preamble",
          "WHAT YOU LEARNED FROM PAST TASKS" in p)


def test_dedup_keeps_distinct_lessons():
    print("\n9. a more specific lesson is not swallowed by a shorter one")
    _reset()
    lessons.add(title="Excel: save as CSV", problem="p1", solution="general fix")
    lessons.add(title="Excel: save as CSV with UTF-8 BOM",
                problem="p2", solution="specific fix")
    rows = lessons.load()
    check("both are kept — one title starting with the other is not a duplicate",
          len(rows) == 2, f"{len(rows)}: {[r['title'] for r in rows]}")
    check("the general lesson still has its own solution",
          any(r["solution"] == "general fix" for r in rows))

    _reset()
    lessons.add(title="Sending a message on WhatsApp Web",
                problem="the chat list is not exposed by name", solution="use a filter",
                apps="whatsapp")
    hits = lessons.recall("what is currently on my screen")
    check("a question about 'what' does not drag in every WhatsApp lesson",
          hits == [], str([h["title"] for h in hits]))


def test_survives_a_damaged_library():
    print("\n10. a library it cannot read is preserved, never overwritten")
    _reset()
    lessons.add(title="A lesson worth keeping", problem="p", solution="s")
    store = lessons._store()
    store.write_text("{ this is not valid json", encoding="utf-8")

    check("recall on a damaged library returns nothing rather than raising",
          lessons.recall("anything at all") == [])

    lesson, created = lessons.add(title="Saved after the damage",
                                  problem="p", solution="s")
    check("a save still succeeds", created)
    check("the damaged file is kept aside as evidence",
          store.with_name(store.name + ".corrupt").exists())
    check("and the new lesson is on file",
          [r["title"] for r in lessons.load()] == ["Saved after the damage"])

    # A file that cannot even be moved aside must not be clobbered.
    store.write_text("{ still not json", encoding="utf-8")
    import unittest.mock as mock
    with mock.patch.object(type(store), "replace", side_effect=OSError("locked")):
        try:
            lessons.add(title="Should not be written", problem="p", solution="s")
            check("an unmovable damaged file refuses the write", False,
                  "it wrote anyway")
        except lessons.LessonStoreError:
            check("an unmovable damaged file refuses the write", True)
    check("the damaged content is still there untouched",
          "still not json" in store.read_text(encoding="utf-8"))


def test_ids_are_unique():
    print("\n11. ids stay unique, even for lessons saved in the same second")
    _reset()
    # Genuinely different subjects, saved back to back. "Lesson number 1/2/3"
    # would be wrong here: those titles ARE near-duplicates and dedup is
    # supposed to merge them, which would test the opposite of the intent.
    subjects = ["Excel refuses to close a nag dialog",
                "Trendyol blocks the headless browser",
                "WhatsApp Web hides its chat list",
                "Outlook drops the signature on reply",
                "Chrome downloads land in a temp folder",
                "Spotify ignores the media keys",
                "Explorer renames a file mid-copy",
                "Teams swallows the first keystroke"]
    for s in subjects:
        lessons.add(title=s, problem="p", solution="s")
    ids = [r["id"] for r in lessons.load()]
    check("each distinct lesson is stored separately",
          len(ids) == len(subjects), f"{len(ids)} of {len(subjects)}")
    check("every lesson has its own id", len(set(ids)) == len(ids),
          f"{len(ids)} lessons, {len(set(ids))} distinct ids")

    _reset()
    a, _ = lessons.add(title="Same words entirely", problem="p", solution="s")
    b, _ = lessons.add(title="Same words entirely!!", problem="p2", solution="s2")
    check("near-identical titles that merge keep one id",
          len(lessons.load()) == 1)


def test_auto_recall_builds_the_prompt():
    print("\n12. the automatic half: lessons reach the model above the task")
    _reset()
    task = "send a screenshot of the price to the whatsapp group"
    prompt, hits = lessons.build_prompt(task)
    check("with no lessons the task is passed through untouched",
          prompt == task and hits == [])

    lessons.add(title="Sending a picture in WhatsApp Web",
                problem="attaching through the file dialog is fragile",
                solution="copy_to_clipboard then ctrl+v",
                apps="whatsapp")
    prompt, hits = lessons.build_prompt(task)
    if not check("the lesson is recalled for this task", len(hits) == 1,
                 str([h["title"] for h in hits])):
        # Without this the next checks raise instead of reporting, and a
        # traceback hides the other sections' results.
        check("the lesson block comes ABOVE the task", False, "nothing recalled")
        return
    check("the lesson block comes ABOVE the task, as the system prompt promises",
          prompt.index("WHAT YOU LEARNED") < prompt.index(task))
    check("the task itself is still present and marked",
          prompt.endswith("TASK: " + task))
    check("the solution is carried into the prompt",
          "copy_to_clipboard" in prompt)


def test_remove():
    print("\n13. a lesson can be deleted")
    _reset()
    a, _ = lessons.add(title="Delete me", problem="p", solution="s")
    lessons.add(title="Keep me", problem="p", solution="s")
    check("removing by id returns True", lessons.remove(a["id"]) is True)
    check("only the other lesson remains",
          [r["title"] for r in lessons.load()] == ["Keep me"])
    check("removing an unknown id is False, not an error",
          lessons.remove("no-such-id") is False)


def main() -> int:
    print("=" * 72)
    print("TASK MEMORY — lessons: save on struggle, recall next time")
    print("=" * 72)
    test_storage_roundtrip()
    test_recall_finds_it()
    test_dedup_updates_not_duplicates()
    test_turkish_query_meets_lesson()
    test_prompt_block_renders()
    test_tools_wired()
    test_credentials_never_saved()
    test_prompt_teaches_it()
    test_dedup_keeps_distinct_lessons()
    test_survives_a_damaged_library()
    test_ids_are_unique()
    test_auto_recall_builds_the_prompt()
    test_remove()

    passed = sum(1 for _, ok, _ in results if ok)
    print("\n" + "=" * 72)
    print(f"{passed}/{len(results)} passed")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name}  {detail}")
    print("=" * 72)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
