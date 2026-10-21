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
