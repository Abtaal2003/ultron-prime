"""Configuration validation, database schema, and the event bus."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError
from src.config import Settings, parse_route
from src.core.db import Database, SqliteTurnEventSink, from_json, new_id, to_json
from src.core.events import (
    EVENT_ADAPTER,
    EventBus,
    InMemorySink,
    SystemNotice,
    ToolStart,
    parse_event,
)
from src.logging_setup import redact_text, redact_value


def make(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


def test_defaults_match_spec() -> None:
    s = make()
    assert s.port == 8787 and s.access_mode == "local"
    assert s.egress_policy == "confirm_tainted" and s.camel_mode == "off"
    assert s.quarantine_sources == ["web", "uploads_external"]
    assert s.effort_for("TECH") == "high" and s.effort_for("REFUSAL") == "low"
    assert s.tool_enabled("anything")


def test_csv_lists_and_routes() -> None:
    s = make(allowed_hosts="a.example, b.example", fallback_chain="fake:x,groq:y")
    assert s.allowed_hosts == ["a.example", "b.example"]
    assert s.fallback_chain == ["fake:x", "groq:y"]
    assert parse_route("groq:openai/gpt-oss-20b").model == "openai/gpt-oss-20b"


@pytest.mark.parametrize("bad", ["nocolon", "nope:model", "groq:"])
def test_bad_routes_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        make(route_main=bad)


def test_lan_requires_tls() -> None:
    with pytest.raises(ValidationError):
        make(access_mode="lan")
    make(access_mode="lan", tls_cert_file="c", tls_key_file="k")


def test_cross_check_upload_limits() -> None:
    with pytest.raises(ValidationError):
        make(upload_max_mb=30, upload_max_mb_per_message=10)


def test_secrets_never_in_repr() -> None:
    s = make(anthropic_api_key="sk-ant-supersecretvalue123456")
    assert "supersecret" not in repr(s)
    assert s.key_presence()["anthropic"] is True
    assert s.secret_values() == ["sk-ant-supersecretvalue123456"]


def test_paths_resolve_absolute(tmp_path: Path) -> None:
    s = make(sqlite_path=str(tmp_path / "x.db"))
    assert s.sqlite_file == tmp_path / "x.db"
    assert s.workspace_dir.is_absolute()


def test_redaction() -> None:
    text = "key sk-ant-abcdefghijklmnopqrstuv and gsk_abcdefghijklmnopqrstuvwx"
    out = redact_text(text, ["custom-secret-value"])
    assert "abcdefghijklmnop" not in out
    assert (
        redact_text("my custom-secret-value here", ["custom-secret-value"]) == "my [redacted] here"
    )
    nested = redact_value({"a": ["sk-ant-abcdefghijklmnopqrstuv"], "b": 1})
    assert nested == {"a": ["[redacted]"], "b": 1}
    fake_google = "AQ." + "Ab" + "x" * 48  # the newer AI Studio key shape, not a real key
    assert redact_text(f"key={fake_google}", []) == "key=[redacted]"
    assert redact_text("AQ.short is fine", []) == "AQ.short is fine"


async def test_schema_and_fts(db: Database) -> None:
    await db.execute(
        "INSERT INTO chats(id,title,created_at,updated_at) VALUES ('c1','t','now','now')"
    )
    await db.execute(
        "INSERT INTO messages(id,chat_id,role,content,created_at) VALUES "
        "('m1','c1','user','forging vibranium alloy','now')"
    )
    rows = await db.fetchall("SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'vibranium'")
    assert len(rows) == 1
    await db.execute("UPDATE messages SET content='painting a canvas' WHERE id='m1'")
    assert not await db.fetchall(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'vibranium'"
    )
    assert await db.fetchall("SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'canvas'")
    await db.execute("DELETE FROM chats WHERE id='c1'")
    assert await db.scalar("SELECT COUNT(*) FROM messages") == 0  # cascade


async def test_transaction_rolls_back(db: Database) -> None:
    with pytest.raises(RuntimeError):
        async with db.transaction() as tx:
            await tx.execute(
                "INSERT INTO chats(id,title,created_at,updated_at) VALUES ('c2','t','n','n')"
            )
            raise RuntimeError("boom")
    assert await db.scalar("SELECT COUNT(*) FROM chats") == 0


async def test_wal_enabled(db: Database) -> None:
    assert (await db.scalar("PRAGMA journal_mode")) == "wal"
    assert (await db.scalar("PRAGMA user_version")) == 1


def test_json_helpers() -> None:
    assert from_json(to_json({"a": "é"})) == {"a": "é"}
    assert from_json(None, []) == []
    assert new_id("x").startswith("x_")


def test_event_roundtrip() -> None:
    ev = ToolStart(turn_id="t", call_id="c", tool_name="x", validated_args={"a": 1}, seq=3)
    parsed = parse_event("ToolStart", ev.model_dump_json())
    assert parsed == ev
    assert (
        EVENT_ADAPTER.validate_python({"type": "SystemNotice", "message": "hi"}).type
        == "SystemNotice"
    )


async def test_bus_sequence_and_resume() -> None:
    bus = EventBus(InMemorySink())
    for i in range(3):
        await bus.publish(SystemNotice(turn_id="t1", message=f"m{i}"))
    got = [e async for e in _take(bus.subscribe("t1", after_seq=1), 1)]
    assert [e.seq for e in got] == [2]  # type: ignore[attr-defined]


async def _take(it, n):  # type: ignore[no-untyped-def]
    count = 0
    async for item in it:
        yield item
        count += 1
        if count >= n:
            return


async def test_bus_live_after_replay_no_gap_no_dupes() -> None:
    bus = EventBus(InMemorySink())
    await bus.publish(SystemNotice(turn_id="t", message="a"))

    async def consume() -> list[int]:
        return [e.seq async for e in bus.subscribe("t")]

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    await bus.publish(SystemNotice(turn_id="t", message="b"))
    await bus.publish(SystemNotice(turn_id="t", message="c"))
    await bus.close_turn("t")
    seqs = await asyncio.wait_for(task, 2)
    assert seqs == [1, 2, 3, 4]  # three notices then TurnClosed


async def test_bus_subscribe_after_close_replays_and_ends() -> None:
    bus = EventBus(InMemorySink())
    await bus.publish(SystemNotice(turn_id="t", message="a"))
    await bus.close_turn("t")
    seqs = [e.seq async for e in bus.subscribe("t")]
    assert seqs == [1, 2]
    with pytest.raises(RuntimeError):
        await bus.publish(SystemNotice(turn_id="t", message="late"))


async def test_bus_requires_turn_id() -> None:
    with pytest.raises(ValueError):
        await EventBus().publish(SystemNotice(message="x"))


async def test_sqlite_sink_persists_and_purges(db: Database) -> None:
    await db.execute("INSERT INTO chats(id,title,created_at,updated_at) VALUES ('c','t','n','n')")
    await db.execute(
        "INSERT INTO turns(id,chat_id,status,started_at,finished_at) VALUES "
        "('t','c','done','2020-01-01T00:00:00.000','2020-01-01T00:00:00.000')"
    )
    bus = EventBus(SqliteTurnEventSink(db))
    await bus.publish(SystemNotice(turn_id="t", message="a"))
    await bus.close_turn("t")
    sink = SqliteTurnEventSink(db)
    assert await sink.last_seq("t") == 2
    assert len(await sink.read_after("t", 0)) == 2
    assert await sink.purge_older_than(24) == 2
    assert await sink.last_seq("t") == 0
