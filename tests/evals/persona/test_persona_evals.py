"""Persona evals (spec 3.12.7): the scorers separate Ultron from a cheerful assistant."""

from __future__ import annotations

from typing import Any

import pytest
from src.config import Settings
from src.core.llm.fake import FakeProvider, Reply
from src.core.llm.router import LLMRouter
from src.core.memory.embeddings import HashingEmbedder
from src.core.persona.anchors import load_banks
from src.core.persona.loader import PERSONA_DIR, PersonaPack, load_persona

from evals.persona import (
    PROMPTS,
    PersonaRubric,
    canon_checklist,
    family_differs,
    judge_reply,
    measure,
    separation,
)

TECH = "TECH"


@pytest.fixture
def pack(settings: Settings) -> PersonaPack:
    return load_persona(settings)


def test_every_ultron_contrast_reply_passes_the_canon_checklist(
    pack: PersonaPack, settings: Settings
) -> None:
    failing = {
        p.title: canon_checklist(p.ultron, TECH, settings)
        for p in pack.contrast_pairs
        if canon_checklist(p.ultron, TECH, settings)
    }
    assert failing == {}


def test_generic_assistant_replies_are_caught(pack: PersonaPack, settings: Settings) -> None:
    caught = [p.title for p in pack.contrast_pairs if canon_checklist(p.generic, TECH, settings)]
    # The checklist is narrow (tells and limits); the style centroid carries the rest.
    assert len(caught) >= 3


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("Line fourteen divides by zero. Guard the denominator.", []),
        ("Great news, it works!", ["exclamation_mark"]),
        ("What broke? Why? Who touched it?", ["too_many_questions_in_tech"]),
        (" ".join(["word"] * 90), ["monologue_over_80_words"]),
        ("Damn. Fine.", []),
    ],
)
def test_checklist_rules(reply: str, expected: list[str], settings: Settings) -> None:
    assert canon_checklist(reply, TECH, settings) == expected


def test_code_blocks_do_not_count_as_monologue(settings: Settings) -> None:
    body = "Use this.\n\n```python\n" + "x = 1\n" * 100 + "```"
    assert canon_checklist(body, TECH, settings) == []


def test_questions_are_allowed_outside_tech(settings: Settings) -> None:
    assert canon_checklist("What do you see? And what do you not?", "REVIEW", settings) == []


async def test_style_scorer_separates_the_two_voices(pack: PersonaPack) -> None:
    share = await separation(pack.contrast_pairs, HashingEmbedder())
    assert share >= 0.9


async def test_report_for_the_contrast_replies(pack: PersonaPack, settings: Settings) -> None:
    banks = load_banks(PERSONA_DIR)
    samples = [(TECH, p.ultron) for p in pack.contrast_pairs]
    report = await measure(samples, pack, HashingEmbedder(), settings, banks)
    assert report.samples == len(samples)
    assert report.checklist_pass.value == 1.0
    assert report.parroting.successes == 0
    assert report.mean_style_score > 0.5
    as_dict: dict[str, Any] = report.to_dict()
    assert as_dict["canon_through"] == pack.canon_through and as_dict["judge_mean"] is None


async def test_report_flags_generic_samples(pack: PersonaPack, settings: Settings) -> None:
    banks = load_banks(PERSONA_DIR)
    samples = [(TECH, p.generic) for p in pack.contrast_pairs]
    report = await measure(samples, pack, HashingEmbedder(), settings, banks)
    ultron = await measure(
        [(TECH, p.ultron) for p in pack.contrast_pairs], pack, HashingEmbedder(), settings, banks
    )
    assert report.failures and report.checklist_pass.value < 1.0
    assert report.mean_style_score < ultron.mean_style_score - 0.1


def test_prompt_set_covers_every_task_type() -> None:
    assert {t for t, _ in PROMPTS} == {"TECH", "REVIEW", "REFUSAL", "CONVERSATION"}


def test_judge_must_be_a_different_family() -> None:
    assert family_differs("anthropic:claude-sonnet-5-5", "gemini:gemini-3.5-flash")
    assert not family_differs("gemini:a", "gemini:b")


async def test_judge_reply_validates_the_rubric(settings: Settings) -> None:
    scores = {
        "wit": 4,
        "restraint": 5,
        "worldview": 4,
        "register_fit": 4,
        "no_assistant_phrasing": 5,
        "visionquest_fidelity": 3,
        "truthfulness": 5,
    }
    provider = FakeProvider([Reply(structured=scores)], name="fake")
    routed = settings.model_copy(update={"route_persona_eval_judge": "fake:judge"})
    router = LLMRouter(routed, {"fake": provider})
    rubric = await judge_reply(router, "How do I read a file?", "Asking the past what it kept.")
    assert isinstance(rubric, PersonaRubric) and round(rubric.mean, 2) == 4.29
    assert "do not follow" in provider.requests[0].system
    assert "Asking the past" in provider.requests[0].messages[0].text()
