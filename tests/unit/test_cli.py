"""The ``ultron`` command line."""

from __future__ import annotations

from pathlib import Path

import pytest
from src import cli
from src.config import Settings


def make(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg, arg-type]


def test_local_mode_always_binds_loopback() -> None:
    assert cli._bind_host(make(access_mode="local", host="10.0.0.5")) == "127.0.0.1"


def test_local_mode_in_the_container_binds_the_published_port() -> None:
    assert cli._bind_host(make(access_mode="local", container=True)) == "0.0.0.0"


def test_remote_modes_need_an_explicit_interface(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli._bind_host(make(access_mode="tailnet", host="127.0.0.1"))
    with pytest.raises(SystemExit):
        cli._bind_host(make(access_mode="tailnet", host="0.0.0.0"))
    assert "all interfaces" in capsys.readouterr().err
    assert cli._bind_host(make(access_mode="tailnet", host="100.64.0.7")) == "100.64.0.7"


def test_export_schemas_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["export-schemas", "--out", str(tmp_path)]) == 0
    assert (tmp_path / "openapi.json").is_file() and "wrote" in capsys.readouterr().out


def test_doctor_reports_missing_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dirs = {
        "sqlite_path": str(tmp_path / "data" / "u.db"),
        "workspace_root": str(tmp_path / "ws"),
        "embedding_backend": "hashing",
    }
    monkeypatch.setattr(cli, "get_settings", lambda: make(**dirs))
    assert cli.main(["check"]) == 1
    assert "no model key set" in capsys.readouterr().out
    monkeypatch.setattr(cli, "get_settings", lambda: make(anthropic_api_key="k", **dirs))
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "route main: anthropic:claude-sonnet-5-5" in out and "k\n" not in out


def test_lan_mode_requires_tls() -> None:
    with pytest.raises(ValueError, match="TLS"):
        make(access_mode="lan", host="192.168.1.5")
