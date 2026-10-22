"""Tracebacks never print API keys (found 2026-10-22: logging was never configured)."""

from __future__ import annotations

import logging
from typing import Any

import pytest
import structlog
from src import cli
from src.logging_setup import configure_logging, get_logger

FAKE = "AQ." + "Fake" * 12


@pytest.fixture
def restore_logging() -> Any:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    structlog.reset_defaults()
    root.handlers[:], root.level = handlers, level


def _fail(api_key: str) -> None:
    headers = {"x-goog-api-key": api_key}  # a local, as in a provider SDK frame
    raise RuntimeError(f"request failed with {len(headers)} headers")


def test_configured_logging_hides_keys_and_locals(capsys: Any, restore_logging: Any) -> None:
    configure_logging("INFO", "pretty", secrets_to_hide=[FAKE])
    try:
        _fail(FAKE)
    except RuntimeError:
        get_logger("t").exception("turn_failed", note=f"key was {FAKE}")
    err = capsys.readouterr().err
    assert "turn_failed" in err and "RuntimeError" in err
    assert FAKE not in err


def test_the_cli_configures_logging_before_running_a_command(
    monkeypatch: pytest.MonkeyPatch, restore_logging: Any
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cli, "_setup_logging", lambda: calls.append("logging"))
    monkeypatch.setattr(cli, "cmd_check", lambda args: calls.append("check") or 0)
    assert cli.main(["check"]) == 0
    assert calls == ["logging", "check"]


async def test_building_a_runtime_outside_the_cli_still_hides_keys(
    settings: Any, capsys: Any, restore_logging: Any
) -> None:
    """A harness that calls build_runtime directly leaked a key on 2026-10-22."""
    from langgraph.checkpoint.memory import InMemorySaver
    from src.core.llm.fake import FakeProvider
    from src.core.wiring import build_runtime

    structlog.reset_defaults()
    from pydantic import SecretStr

    keyed = settings.model_copy(update={"gemini_api_key": SecretStr(FAKE)})
    rt = await build_runtime(
        keyed, providers={"fake": FakeProvider(name="fake")}, checkpointer=InMemorySaver()
    )
    try:
        assert structlog.is_configured()
        try:
            _fail(FAKE)
        except RuntimeError:
            get_logger("t").exception("turn_failed")
        assert FAKE not in capsys.readouterr().err
    finally:
        await rt.close()
