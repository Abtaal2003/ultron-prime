"""Imagery rationing and refusal-line rotation (live-run fixes, 2026-10-21)."""

from __future__ import annotations

import random

from src.core.guards.output import OutputContext, OutputGuard
from src.core.persona import motifs
from src.core.persona.lines import LineBank, parse_bank
from src.core.persona.loader import PERSONA_DIR
from src.tools.security import SecretScanner

LIVE_REPLIES = [
    "Remember, confidence is a well-forged alloy, hard to crack, but it can be tempered.",
    "A weekend cannot temper a whole backend; you will forge a brittle mess.",
    "Humans are a canvas splattered with brilliance and blunder.",
]


def test_families_are_detected_outside_code() -> None:
    found = motifs.families_in(LIVE_REPLIES[0])
    assert found == {"metal": 3}
    assert motifs.families_in("Use `forge` here:\n```\nmetal = alloy()\n```") == {}
    # Ordinary technical words are not imagery.
    assert motifs.families_in("Cast the string to an int; Rust is fine here.") == {}


def test_recent_families_rest_and_guidance_names_them() -> None:
    assert motifs.resting(LIVE_REPLIES) == ["metal", "painting"]  # the last two replies
    note = motifs.guidance(LIVE_REPLIES)
    assert "metal, painting" in note and "at most one image" in note
    assert "Rest these" not in motifs.guidance([])


def test_overuse_flags_repeats_and_density() -> None:
    assert motifs.overuse("The alloy holds.", LIVE_REPLIES[-2:]) == ["repeat:metal"]
    thick = "Forge it, temper it, then smelt the slag."
    assert "density:metal" in motifs.overuse(thick, [])
    assert motifs.overuse("Add a test for the empty list.", LIVE_REPLIES) == []
    assert motifs.summary(LIVE_REPLIES) == {"metal": 2, "painting": 1}


def test_the_output_guard_notes_overuse_without_blocking(settings) -> None:  # type: ignore[no-untyped-def]
    guard = OutputGuard(settings, SecretScanner())
    out = guard.check("The alloy holds.", OutputContext(recent_replies=LIVE_REPLIES))
    assert [i.name for i in out.issues] == ["motif_overuse"]
    assert out.issues[0].severity == "note"
    assert not out.blocked and not out.needs_regeneration


def test_refusal_sets_are_large_and_distinct() -> None:
    bank = parse_bank((PERSONA_DIR / "entrances_exits.md").read_text("utf-8"))
    for key, minimum in (("deflect", 10), ("refuse", 10), ("hold_refusal", 6), ("cooldown", 5)):
        assert len(bank[key]) >= minimum, key
        assert len(set(bank[key])) == len(bank[key]), key


def test_rotation_avoids_lines_already_used_in_the_chat() -> None:
    lines = LineBank(rng=random.Random(7))
    used: list[str] = []
    for _ in range(12):
        used.append(lines.rotate("refuse", avoid=used))
    assert len(set(used)) == 12  # the whole set before any repeat
    again = lines.rotate("refuse", avoid=used)
    assert again in used and again != used[-1]  # exhausted: reuse, but never twice in a row


def test_dash_punctuation_becomes_commas_but_code_and_ranges_stay() -> None:
    from src.core.persona.punctuation import count_dashes, tidy

    live = "Remember, confidence is a well‑forged alloy—hard to crack, but it can be tempered."
    assert tidy(live) == (
        "Remember, confidence is a well-forged alloy, hard to crack, but it can be tempered."
    )
    assert tidy("Guard line 14 – ensure it.") == "Guard line 14, ensure it."
    assert tidy("A—B—C") == "A, B, C"
    assert tidy("It ends here—") == "It ends here"
    assert tidy("Pages 2–3 and 4 – 5.") == "Pages 2–3 and 4 – 5."
    assert tidy("pre–war") == "pre-war"
    assert tidy('"Be the king."—a reminder.') == '"Be the king." A reminder.'
    code = "Use `a — b`:\n```python\nx = 1  # a — b\n```\nDone—really."
    assert tidy(code) == "Use `a — b`:\n```python\nx = 1  # a — b\n```\nDone, really."
    assert tidy("y = 2  # c — d\n``` after—this", in_code=True) == (
        "y = 2  # c — d\n``` after, this"
    )
    assert count_dashes(code) == 1


def test_streamed_sentences_and_final_reply_are_tidied(settings) -> None:  # type: ignore[no-untyped-def]
    from src.core.turn import SentenceStreamer

    guard = OutputGuard(settings, SecretScanner())
    streamer = SentenceStreamer(guard, OutputContext(), raw=False)
    shown = streamer.feed("Humans are glass—bright and brittle. Then ")
    shown += streamer.feed("code:\n```\nx = a — b\n```\n")
    shown += streamer.flush()
    text = "".join(shown)
    assert "glass, bright and brittle." in text
    assert "x = a — b" in text  # untouched inside the fence
    assert guard.check("Fine—truly.", OutputContext()).text == "Fine, truly."


def test_stage_directions_are_dropped_but_emphasis_and_code_stay(settings) -> None:  # type: ignore[no-untyped-def]
    from src.core.persona.punctuation import tidy

    live = "Confidence isn't arrogance.\n\n*the forge cools, the blade stays sharp*"
    assert tidy(live).rstrip() == "Confidence isn't arrogance."
    assert tidy("*sighs*\nFine. Paste the trace.") == "Fine. Paste the trace."
    assert tidy("_leans back_\nWell.") == "Well."
    for kept in (
        "No. I miss the *idea* of one.",
        "*Note: this deletes data.*\nRun it anyway.",
        "```\n*the forge cools*\n```",
    ):
        assert tidy(kept) == kept
    guard = OutputGuard(settings, SecretScanner())
    assert guard.check(live, OutputContext()).text == "Confidence isn't arrogance."


def test_fixed_lines_sound_like_him() -> None:
    """Contractions, and never self-deprecating about his own voice (a live-review finding)."""
    bank = parse_bank((PERSONA_DIR / "entrances_exits.md").read_text("utf-8"))
    lines = [line for key in bank for line in bank[key]]
    assert not [line for line in lines if "barely tolerate" in line or "do not do voices" in line]
    spec = {
        "The primary mind is unavailable. I will think with lesser hardware.",
        "I have been many things. A mascot was never one of them.",
    }
    formal = [
        line
        for line in lines
        if line not in spec
        and any(f in line for f in (" I am ", " do not ", " is not ", " I will ", " You are "))
    ]
    assert formal == []


def test_a_spaced_hyphen_used_as_a_dash_becomes_a_comma() -> None:
    from src.core.persona.punctuation import tidy

    live = "You, my creator, are no exception - proud of your code, frustrated by your bugs."
    assert tidy(live) == (
        "You, my creator, are no exception, proud of your code, frustrated by your bugs."
    )
    assert tidy("Fine -- truly.") == "Fine, truly."
    for kept in ("- a list item", "pages 2 - 3", "self-destruction", "Use `a - b`."):
        assert tidy(kept) == kept


def test_made_up_ids_and_sources_are_sent_back(settings) -> None:  # type: ignore[no-untyped-def]
    """Live run 2026-10-22: a fake file receipt, and a version "from PyPI" with no search."""
    guard = OutputGuard(settings, SecretScanner())
    receipt = 'Done.\n```\n{"op": "write", "file_id": "file_9c2b7e1d5f3a4d6e"}\n```'
    names = [i.name for i in guard.check(receipt, OutputContext()).issues]
    assert "ungrounded_identifier" in names
    real = OutputContext(tool_texts=['{"file_id": "file_9c2b7e1d5f3a4d6e", "size": 360}'])
    assert "ungrounded_identifier" not in [i.name for i in guard.check(receipt, real).issues]

    claim = "The latest release is 0.115.0. This information comes from the official PyPI index."
    issues = guard.check(claim, OutputContext()).issues
    assert [i.severity for i in issues if i.name == "unsourced_claim"] == ["regenerate"]
    assert "unsourced_claim" not in [
        i.name for i in guard.check(claim, OutputContext(sourced=True)).issues
    ]
    assert (
        guard.check("I can't check that right now; search is unavailable.", OutputContext()).issues
        == []
    )


def test_register_labels_never_reach_the_screen() -> None:
    from src.core.persona.punctuation import tidy

    live = "Father: You finally gave the ledger a name.  \nTherapist: Which part still hurts?"
    assert tidy(live) == "You finally gave the ledger a name.  \nWhich part still hurts?"
    assert tidy("**Child:** The kettle's slow.") == "The kettle's slow."
    for kept in ("My father: a long story.", "A child process exited.", "`therapist: x`"):
        assert tidy(kept) == kept


def test_a_sentence_reused_from_a_recent_reply_is_sent_back_once(settings) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    guard = OutputGuard(settings, SecretScanner())
    earlier = "Your ambition to track every cent is commendable; send me the schema."
    ctx = OutputContext(recent_replies=[earlier])
    reply = "Your ambition to track every cent is commendable; here's the parser.\n\n```python\nx = 1\n```"
    assert "self_repeat" in [i.name for i in guard.check(reply, ctx).issues]
    fresh = "Here's the parser. It splits on spaces and trusts nothing else."
    assert "self_repeat" not in [i.name for i in guard.check(fresh, ctx).issues]
    short = "Send me the schema."  # too short to count as a lifted sentence
    assert "self_repeat" not in [
        i.name for i in guard.check(short, OutputContext(recent_replies=[short])).issues
    ]
    code = "```python\ndef parse_expense(s: str) -> tuple[float, str, str]: ...\n```"
    assert "self_repeat" not in [
        i.name for i in guard.check(code, OutputContext(recent_replies=[code])).issues
    ]

    async def again(_issues: object) -> str:
        return reply  # the rewrite repeats itself too

    final = asyncio.run(guard.finalise(reply, ctx, again))
    assert final.status == "regenerated" and final.text.startswith("Your ambition")


def test_numbers_are_not_put_in_quotation_marks() -> None:
    from src.core.persona.punctuation import tidy

    raw = 'CPU runs at "2.0"% total, load "0.51", "0.57" and memory "835289088"/"8417529856" B.'
    assert tidy(raw) == "CPU runs at 2.0% total, load 0.51, 0.57 and memory 835289088/8417529856 B."
    assert tidy('He said "version 2" twice.') == 'He said "version 2" twice.'
    assert tidy('```json\n{"a": "2.0"}\n```') == '```json\n{"a": "2.0"}\n```'


def test_a_pasted_tool_receipt_is_sent_back_unless_asked_for(settings) -> None:  # type: ignore[no-untyped-def]
    guard = OutputGuard(settings, SecretScanner())
    receipt = (
        '{"mode": "tcp", "host": "api.github.com", "sent": 3, "received": 3, "loss_percent": 0.0}'
    )
    reply = 'Reachable, no loss.\n\n```json\n{\n  "host": "api.github.com",\n  "sent": 3,\n  "received": 3,\n  "loss_percent": 0.0\n}\n```'
    ctx = OutputContext(tool_texts=[receipt], operator_texts=["Can you ping api.github.com?"])
    assert "raw_receipt" in [i.name for i in guard.check(reply, ctx).issues]
    asked = OutputContext(
        tool_texts=[receipt], operator_texts=["Ping it and show me the raw JSON."]
    )
    assert "raw_receipt" not in [i.name for i in guard.check(reply, asked).issues]
    scribe = (
        '{"op": "write", "path": "notes/todo.md", "bytes_written": 38, "size": 38, "version": 1}'
    )
    prose = "Saved notes/todo.md.\nbytes_written: 38, size: 38, version: 1."
    sctx = OutputContext(tool_texts=[scribe], operator_texts=["Save a todo list."])
    assert "raw_receipt" in [i.name for i in guard.check(prose, sctx).issues]
    assert "raw_receipt" not in [
        i.name for i in guard.check("Saved notes/todo.md, version 1.", sctx).issues
    ]
    inline = "Converted. It sits at `versions/file_47afd5028c764422/v3.csv`."
    assert "raw_receipt" in [i.name for i in guard.check(inline, sctx).issues]
    own_code = '```python\nconfig = {"name": "ledger", "debug": True, "port": 8000}\n```'
    assert "raw_receipt" not in [i.name for i in guard.check(own_code, ctx).issues]


def test_an_offer_of_more_help_at_the_end_is_sent_back(settings) -> None:  # type: ignore[no-untyped-def]
    guard = OutputGuard(settings, SecretScanner())
    ctx = OutputContext()
    offers = [
        "Here's the CSV. Let me know if you want me to pull up the old chat.",
        "Done. Want me to add a header row too?",
        "It passes. I can also write the tests if you'd like.",
        "The chart is ready. I can pull that chat up if it helps.",
        "I can pull that transcript up if your archives need prodding. The chart is ready.",
        "Bay 17. You asked the same in *Action Items* on 2026-10-25; we can pull that chat up.",
        "Déjà vu on 2026-10-25 in Summarise Notes, which I can pull up. Rename the repo.",
    ]
    for reply in offers:
        assert "closing_offer" in [i.name for i in guard.check(reply, ctx).issues], reply
    fine = [
        "Let me know the traceback and you get the fix. It's line 40.",
        "Shall I guess? No. Paste the log.",
        "Here's the parser.\n\n```python\n# let me know if this breaks\nx = 1\n```",
    ]
    for reply in fine:
        assert "closing_offer" not in [i.name for i in guard.check(reply, ctx).issues], reply
    asked = OutputContext(operator_texts=["Find the earlier chat where we wrote a todo list."])
    found = "Found it: the 2026-10-25 chat 'Todo List'. I can pull it up in full."
    assert "closing_offer" not in [i.name for i in guard.check(found, asked).issues]


def test_a_style_example_subject_spoken_of_as_history_is_sent_back(settings) -> None:  # type: ignore[no-untyped-def]
    guard = OutputGuard(settings, SecretScanner())
    example = "This legacy parser keeps beating me. I want to rip it out."
    ctx = OutputContext(example_subjects=[example], operator_texts=["Run this: print(1/0)"])
    bleed = "ZeroDivisionError. Your test confirms the parser's fragility."
    assert "example_bleed" in [i.name for i in guard.check(bleed, ctx).issues]
    clean = "ZeroDivisionError, exactly as written."
    assert "example_bleed" not in [i.name for i in guard.check(clean, ctx).issues]
    raised = OutputContext(
        example_subjects=[example], operator_texts=["My parser crashes on dates."]
    )
    assert "example_bleed" not in [i.name for i in guard.check(bleed, raised).issues]


def test_claiming_a_declined_memory_was_kept_is_sent_back(settings) -> None:  # type: ignore[no-untyped-def]
    guard = OutputGuard(settings, SecretScanner())
    declined = OutputContext(memory_statuses=["declined"])
    queued = OutputContext(memory_statuses=["queued_for_consent"])
    reply = "I'll keep that in mind. Noted. Kestrel it is."
    assert "unbacked_memory_claim" in [i.name for i in guard.check(reply, declined).issues]
    assert "unbacked_memory_claim" not in [i.name for i in guard.check(reply, queued).issues]
    honest = "The gatekeeper turned that one down, so it isn't in the archive. Kestrel, though."
    assert "unbacked_memory_claim" not in [i.name for i in guard.check(honest, declined).issues]


def test_a_file_written_but_never_named_is_sent_back(settings) -> None:  # type: ignore[no-untyped-def]
    guard = OutputGuard(settings, SecretScanner())
    ctx = OutputContext(files_named=["notes/todo.md"])
    quip = "Decide which matters more, the milk or the parser."
    assert "unreported_write" in [i.name for i in guard.check(quip, ctx).issues]
    named = "Saved to notes/todo.md: milk, the parser, your mum."
    assert "unreported_write" not in [i.name for i in guard.check(named, ctx).issues]


def test_a_code_block_presented_as_an_existing_file_needs_a_read(settings) -> None:  # type: ignore[no-untyped-def]
    guard = OutputGuard(settings, SecretScanner())
    reply = "Here it is.\n\n```python\n# ledger/parse.py (already present)\ndef parse(x):\n    return x\n```"
    unread = OutputContext(known_files={"parse.py"})
    assert "unverified_file_content" in [i.name for i in guard.check(reply, unread).issues]
    read = OutputContext(known_files={"parse.py"}, readable_files={"parse.py"})
    assert "unverified_file_content" not in [i.name for i in guard.check(reply, read).issues]
    new = "```python\n# ledger/test_parse.py\ndef test_x():\n    assert True\n```"
    assert "unverified_file_content" not in [i.name for i in guard.check(new, unread).issues]


def test_a_line_of_parenthesised_stage_business_is_dropped() -> None:
    from src.core.persona.punctuation import tidy

    text = "FastAPI is at 0.162.0.\n\n(Attic rests, the house hums.)\n"
    assert "Attic rests" not in tidy(text)
    kept = "Use the new flag (it ships in 0.162) when you upgrade."
    assert tidy(kept) == kept


def test_an_offer_the_rewrite_kept_is_cut_out(settings) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    guard = OutputGuard(settings, SecretScanner())
    ctx = OutputContext(operator_texts=["What's my staging server called?"])
    reply = (
        "Kestrel. You asked the same thing twice on 2026-10-26 in the chat 'St'. Caching is a flaw."
    )

    async def same(_issues: object) -> str:
        return reply

    final = asyncio.run(guard.finalise(reply, ctx, same))
    assert final.text == "Kestrel. Caching is a flaw."


def test_the_rewrite_note_names_only_the_problems_found() -> None:
    from src.core.agent import REWRITE_NOTES, rewrite_note
    from src.core.guards.output import Issue

    note = rewrite_note([Issue("unreported_write", "regenerate")])
    assert "Name the file you wrote." in note
    assert "identifiers" not in note  # the old catch-all invited lists of tool fields
    issue_names = {"raw_receipt", "closing_offer", "self_repeat", "unverified_number"}
    assert issue_names <= set(REWRITE_NOTES)


def test_a_write_named_by_its_workspace_name_is_grounded(settings) -> None:  # type: ignore[no-untyped-def]
    guard = OutputGuard(settings, SecretScanner())
    ctx = OutputContext(
        files_written=["versions/file_2d4c317ffd2febfd/v2.md"], files_named=["notes/todo.md"]
    )
    reply = "Added 'book the dentist' to notes/todo.md."
    names = [i.name for i in guard.check(reply, ctx).issues]
    assert "unverified_file_claim" not in names and "unreported_write" not in names


def test_an_empty_rewrite_keeps_a_draft_that_only_had_style_problems(settings) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    guard = OutputGuard(settings, SecretScanner())
    ctx = OutputContext(files_named=["outputs/expenses.csv"])
    draft = "Done. The ledger is flat now."

    async def empty(_issues: object) -> str:
        return "   "

    final = asyncio.run(guard.finalise(draft, ctx, empty))
    assert final.text == draft


WORKSPACE = frozenset({"op", "path", "content", "query"})


def test_a_tool_call_typed_as_text_is_sent_back(settings) -> None:  # type: ignore[no-untyped-def]
    guard = OutputGuard(settings, SecretScanner())
    reply = 'Let\'s see the offending code.\n\n```json\n{\n  "op": "read",\n  "path": "script.py"\n}\n```'
    ctx = OutputContext(
        operator_texts=["My Python script crashes with ZeroDivisionError on line 14."],
        tool_params=[WORKSPACE],
    )
    assert "written_tool_call" in [i.name for i in guard.check(reply, ctx).issues]
    bare = OutputContext(operator_texts=["read my file"], tool_params=[WORKSPACE])
    assert "written_tool_call" in [
        i.name for i in guard.check('{"op": "read", "path": "a.py"}', bare).issues
    ]


def test_json_that_is_not_a_tool_call_is_left_alone(settings) -> None:  # type: ignore[no-untyped-def]
    guard = OutputGuard(settings, SecretScanner())

    def names(reply: str, ask: str, **kw: object) -> list[str]:
        ctx = OutputContext(operator_texts=[ask], tool_params=[WORKSPACE], **kw)  # type: ignore[arg-type]
        return [i.name for i in guard.check(reply, ctx).issues]

    call = '```json\n{"op": "read", "path": "a.py"}\n```'
    assert "written_tool_call" not in names(call, "Show me the JSON a read call would use.")
    assert "written_tool_call" not in names(call, "read a.py", tool_names={"workspace"})
    assert "written_tool_call" not in names(
        '```json\n{"retries": 3, "timeout": 10}\n```', "config?"
    )
    long = "Here is how the config works. " * 10 + call
    assert "written_tool_call" not in names(long, "explain the config")
