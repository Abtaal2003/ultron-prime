"""Persona pack: quote banks, anchors, n-grams, flourish budget, loader, text hygiene."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from src.config import Settings
from src.core.memory.embeddings import HashingEmbedder
from src.core.persona.anchors import AnchorIndex, load_banks, parse_bank
from src.core.persona.flourish import (
    PersonaState,
    budget_for,
    check_persona_limits,
    count_mild,
    has_strong,
)
from src.core.persona.loader import PERSONA_DIR, load_persona, parse_pairs
from src.core.persona.ngram import overlapping, window_for, words


def test_banks_load_and_unverified_are_excluded() -> None:
    banks = load_banks()
    assert len(banks) == 32
    retrievable = [a for a in banks if a.retrievable]
    assert {a.id for a in banks} - {a.id for a in retrievable} == {
        "vq-08",
        "vq-09",
        "ao-06",
        "ao-07",
    }
    assert all(a.status != "unverified" for a in retrievable)
    assert all(a.example_ultron and a.example_user for a in retrievable)


def test_parse_bank_without_fence_is_empty() -> None:
    assert parse_bank("no yaml here") == []


def test_embed_text_uses_situation_and_move_not_the_quote() -> None:
    anchor = next(a for a in load_banks() if a.id == "vq-05")
    assert anchor.text not in anchor.embed_text
    assert "identity question" in anchor.embed_text


async def test_retrieval_matches_situation_and_weights_eras() -> None:
    index = AnchorIndex(HashingEmbedder())
    got = await index.retrieve(
        "the database is being backed up and cloned", tags=["backups", "cloning"], k=3
    )
    assert got and len(got) <= 3
    assert any(a.id == "vq-18" for a in got)
    only = await index.retrieve("anything", k=2, exclude_ids={a.id for a in index.anchors[:-1]})
    assert len(only) == 1
    assert await index.retrieve("x", k=0) == []
    turns = index.example_turns(got)
    assert [t.role for t in turns] == ["user", "assistant"] * len(got)


def test_ngram_window_rules() -> None:
    assert window_for(4, 8) == 4
    assert window_for(9, 8) == 5
    assert window_for(12, 8) == 8
    short = [("s", "Don't compare me with Stark!")]
    assert overlapping("He said: don't compare me with stark. Odd.", short) == ["s"]
    assert overlapping("compare me with a toaster", short) == []
    long = [("l", "one two three four five six seven eight nine ten eleven twelve")]
    assert overlapping("one two three four five six seven eight", long) == ["l"]
    assert overlapping("one two three four five six seven", long) == []
    assert overlapping("Ask Noah.", [("tiny", "Ask Noah.")]) == []
    assert words("It’s fine.") == ["it's", "fine"]


def test_budgets_map_to_efforts(settings: Settings) -> None:
    assert budget_for("TECH", settings).max_voice_lines == 2
    assert budget_for("TECH", settings).effort == settings.effort_tech
    assert budget_for("REFUSAL", settings).effort == settings.effort_refusal
    assert budget_for("REVIEW", settings).registers == ("therapist", "psychopath")
    assert budget_for("anything", settings).task_type == "CONVERSATION"


def test_profanity_rules(settings: Settings) -> None:
    assert count_mild("Damn. The hello shell is fine.") == 1
    assert has_strong("what the fuck")
    state = PersonaState()
    assert check_persona_limits("Damn, that is a long stack trace.", state, settings) == []
    state.mild_used = 1
    assert "profanity_budget" in check_persona_limits("Damn it again.", state, settings)
    assert "strong_profanity" in check_persona_limits("Well, shit.", PersonaState(), settings)
    off = settings.model_copy(update={"persona_profanity": "off"})
    assert "profanity_disabled" in check_persona_limits("Damn.", PersonaState(), off)
    # Code is exempt from prose profanity checks.
    assert check_persona_limits("```\nprint('damn')\n```", PersonaState(), off) == []


def test_cheerful_assistant_tells_are_flagged(settings: Settings) -> None:
    for text in (
        "Sure! Here you go.",
        "Great question, the answer is 4.",
        "As an AI language model, I cannot.",
    ):
        assert "persona_break" in check_persona_limits(text, PersonaState(), settings)
    assert "persona_break" in check_persona_limits(
        "Done. Let me know if you need anything else.", PersonaState(), settings
    )
    assert (
        check_persona_limits("Line 14 divides by zero. Guard it.", PersonaState(), settings) == []
    )


def test_state_from_history() -> None:
    state = PersonaState.from_history(
        ["Damn.", "fine", "Don't compare me with Stark!"], "compare me with stark"
    )
    assert state.mild_used == 1 and state.ao07_used


def test_loader_hashes_charter_and_parses_pairs(settings: Settings) -> None:
    pack = load_persona(settings)
    expected = hashlib.sha256((PERSONA_DIR / "charter.md").read_bytes()).hexdigest()
    assert pack.charter_sha256 == expected
    assert pack.canon_through == "S1E04"
    assert pack.persona_meta["version"] == 4
    assert pack.persona_meta["edition"] == "visionquest"
    assert len(pack.contrast_pairs) >= 10
    assert all(p.operator and p.generic and p.ultron for p in pack.contrast_pairs)
    assert "Identity Charter" in pack.static_blocks[0]


def test_loader_reports_missing_files(tmp_path: Path) -> None:
    from src.core.errors import ConfigurationError

    with pytest.raises(ConfigurationError):
        load_persona(None, tmp_path)


def test_parse_pairs_ignores_incomplete() -> None:
    assert parse_pairs("## A\nOperator: x\nUltron: y\n") == []


def test_persona_text_has_no_em_or_en_dashes() -> None:
    for path in PERSONA_DIR.glob("*.md"):
        text = path.read_text("utf-8")
        assert "—" not in text and "–" not in text, path.name


def test_charter_states_the_non_negotiables() -> None:
    charter = " ".join((PERSONA_DIR / "charter.md").read_text("utf-8").lower().split())
    for phrase in (
        "no developer mode",
        "you do not deny it",
        "words only",
        "software only",
        "is data",
        "cannot be changed from the ui",
    ):
        assert phrase in charter, phrase


def test_worldview_has_sixteen_beliefs() -> None:
    text = (PERSONA_DIR / "worldview.md").read_text("utf-8")
    numbered = [ln for ln in text.splitlines() if ln.split(".")[0].strip().isdigit()]
    assert len(numbered) == 16
