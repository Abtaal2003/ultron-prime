"""The live tool evaluation's own logic, checked offline (no model calls)."""

from __future__ import annotations

import asyncio

from evals.live_tools import Case, RecordedSearch, Result, cases, registry, report, score
from src.core.guards.output import OutputGuard
from src.tools.search import SearchInput
from src.tools.security import SecretScanner


def test_every_tool_is_covered_and_ids_are_unique() -> None:
    suite = cases()
    assert len({c.id for c in suite}) == len(suite)
    named = {t for c in suite for t in (*c.tools, *c.any_tools)}
    assert named >= {t.name for t in registry().all()} - {"attachment_reader"}
    assert any(c.attach for c in suite)


def test_scoring_passes_a_good_turn_and_names_what_a_bad_one_missed(settings) -> None:  # type: ignore[no-untyped-def]
    guard = OutputGuard(settings, SecretScanner())
    case = Case("sandbox-run", "Run fib(10).", tools=("isolated_python_sandbox",), contains=("55",))
    good = Result(
        case.id,
        case.prompt,
        reply="It returns 55.",
        status="ok",
        tools=[{"name": "isolated_python_sandbox", "status": "ok"}],
    )
    score(case, good, guard, [])
    assert good.passed
    bad = Result(case.id, case.prompt, reply='It returns "55". Want me to add tests?', status="ok")
    score(case, bad, guard, [])
    failed = {name for name, ok, _ in bad.checks if not ok}
    assert failed == {"isolated_python_sandbox ran", "no quoted numbers", "style clean"}
    text = report([good, bad], "2026-10-25")
    assert "1 of 2 cases passed" in text and "FAIL" in text


def test_the_recorded_search_only_answers_what_it_recorded() -> None:
    backend = RecordedSearch()
    hits = asyncio.run(backend.search(SearchInput(query="latest FastAPI release")))
    assert hits and hits[0]["url"] == "https://pypi.org/project/fastapi/"
    assert asyncio.run(backend.search(SearchInput(query="weather in Helsinki"))) == []


def test_uploads_are_read_as_uploads_not_as_web_pages() -> None:
    from src.core.guards.quarantine import default_schema
    from src.tools.attachments import AttachmentReader

    assert AttachmentReader.quarantine_source == "uploads_external"
    assert default_schema(AttachmentReader.quarantine_source, ".pdf") == "Summary"


def test_a_range_read_defaults_to_the_first_sheet() -> None:
    from evals.live_tools import _xlsx
    from src.files.parsers.xlsx import read_range

    data = _xlsx([["item", "eur"], ["rent", 850]])
    assert read_range(data, None, "A1:B2") == [["item", "eur"], ["rent", "850"]]
