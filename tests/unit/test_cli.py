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


def test_a_missing_embedding_model_is_one_plain_sentence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import huggingface_hub

    calls: list[bool] = []

    def offline(name: str, local_files_only: bool = False) -> str:
        calls.append(local_files_only)
        raise OSError("proxy said no")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", offline)
    assert cli._prefetch_embeddings(make(embedding_backend="local_light")) is False
    err = capsys.readouterr().err
    assert "Downloading the embedding model" in err
    assert "can't start: couldn't download the embedding model" in err
    assert "Traceback" not in err and calls == [True, False]
    monkeypatch.setattr(cli, "get_settings", lambda: make(embedding_backend="local_light"))
    assert cli.main(["serve"]) == 2


def test_a_cached_embedding_model_is_not_fetched_again(monkeypatch: pytest.MonkeyPatch) -> None:
    import huggingface_hub

    calls: list[bool] = []
    monkeypatch.setattr(
        huggingface_hub,
        "snapshot_download",
        lambda name, local_files_only=False: calls.append(local_files_only) or "/cache",
    )
    assert cli._prefetch_embeddings(make(embedding_backend="local")) is True
    assert calls == [True]
    assert cli._prefetch_embeddings(make(embedding_backend="hashing")) is True
    assert cli._prefetch_embeddings(make(embedding_backend="gemini")) is False


def test_local_mode_allows_its_own_port_as_an_origin() -> None:
    from src.api.app import _origins

    assert "http://localhost:9000" in _origins(make(port=9000))
    assert "http://127.0.0.1:9000" in _origins(make(port=9000))
    assert _origins(make()).count("http://localhost:8787") == 1
    remote = make(access_mode="tailnet", host="100.64.0.7", allowed_hosts=["100.64.0.7"])
    assert "http://localhost:8787" not in _origins(remote) or (
        "http://localhost:8787" in remote.allowed_origins
    )
