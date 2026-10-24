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
    own_code = '```python\nconfig = {"name": "ledger", "debug": True, "port": 8000}\n```'
    assert "raw_receipt" not in [i.name for i in guard.check(own_code, ctx).issues]
