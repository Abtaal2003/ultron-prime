"""Path confinement, host validation, secret scanning, filename hygiene."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import settings as hsettings
from hypothesis import strategies as st
from src.core.errors import PolicyDenied, ValidationFailure
from src.tools.security import (
    SecretScanner,
    confine_path,
    is_private_address,
    sanitize_filename,
    shannon_entropy,
    validate_host,
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "ws"
    r.mkdir()
    (r / "ok.txt").write_text("hi")
    (r / "sub").mkdir()
    return r


@pytest.mark.parametrize(
    "bad",
    [
        "../etc/passwd",
        "..",
        "a/../../b",
        "/etc/passwd",
        "C:\\Windows\\x",
        "~/x",
        "\\\\srv\\share",
        "a\x00b",
        "con",
        "NUL.txt",
        "a/aux/b",
        "x\u202etxt.exe",
        "a\nb",
        "file.",
        "file ",
        "",
        "a" * 200,
        "x/" * 600,
    ],
)
def test_hostile_paths_denied(root: Path, bad: str) -> None:
    with pytest.raises((PolicyDenied, ValidationFailure)):
        confine_path(root, bad)


def test_good_paths_resolve_inside(root: Path) -> None:
    assert confine_path(root, "ok.txt") == (root / "ok.txt").resolve()
    assert confine_path(root, "sub/new/file.md") == (root / "sub/new/file.md").resolve()
    assert confine_path(root, "./sub//x.md") == (root / "sub/x.md").resolve()
    assert confine_path(root, "", allow_root=True) == root.resolve()


def test_symlink_escape_denied(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s")
    os.symlink(outside, root / "link")
    with pytest.raises(PolicyDenied):
        confine_path(root, "link/secret.txt")
    os.symlink(outside / "secret.txt", root / "flink.txt")
    with pytest.raises(PolicyDenied):
        confine_path(root, "flink.txt")


def test_symlink_inside_root_allowed(root: Path) -> None:
    os.symlink(root / "ok.txt", root / "alias.txt")
    assert confine_path(root, "alias.txt") == (root / "ok.txt").resolve()


@given(st.text(max_size=80))
@hsettings(max_examples=300, deadline=None)
def test_any_string_never_escapes(text: str) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        r = Path(d) / "ws"
        r.mkdir()
        try:
            resolved = confine_path(r, text)
        except (PolicyDenied, ValidationFailure):
            return
        assert resolved.is_relative_to(r.resolve())


@pytest.mark.parametrize(
    "host", ["example.com", "a-b.example.co.uk", "localhost", "8.8.8.8", "::1", "Example.COM."]
)
def test_valid_hosts(host: str) -> None:
    assert validate_host(host).host


@pytest.mark.parametrize(
    "host",
    [
        "-c 1000 8.8.8.8",
        "a b",
        "x;rm -rf /",
        "a|b",
        "$(id)",
        "",
        "-h",
        "a..b",
        "-a.com",
        "a_b.com",
        "ex ample.com",
        "http://x.com",
        "x.com/path",
        "1.2.3",
        "münchen.de",
        "`id`",
        "a&b.com",
        "a\n.com",
    ],
)
def test_invalid_hosts_rejected(host: str) -> None:
    with pytest.raises(ValidationFailure):
        validate_host(host)


def test_exfil_heuristic() -> None:
    assert validate_host("mail.google.com").suspicious is False
    assert validate_host("a3f9c2e7b1d4a8f6c0e5.attacker.example").suspicious is True
    assert validate_host("x" * 45 + ".example.com").suspicious is True
    assert validate_host("short.example.com", max_len=10).suspicious is True
    assert shannon_entropy("aaaa") == 0
    assert shannon_entropy("abcd") == pytest.approx(2.0)


def test_private_addresses() -> None:
    for addr in ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.1.1", "::1", "0.0.0.0"]:
        assert is_private_address(addr)
    assert not is_private_address("8.8.8.8")


def test_secret_scanner() -> None:
    sc = SecretScanner(["my-literal-secret-value"])
    text = (
        "key=sk-ant-api03-abcdefghijklmnopqrstuvwx\n"
        "password: Hunter2Hunter2Hunter2\n"
        "tok eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijk\n"
        "lit my-literal-secret-value\nAKIAABCDEFGHIJKLMNOP\n"
    )
    kinds = {h.kind for h in sc.scan(text)}
    assert {"credential", "assignment", "jwt", "configured_secret"} <= kinds
    red = sc.redact(text)
    for leaked in ["sk-ant-api03", "Hunter2", "eyJhbGci", "my-literal-secret-value", "AKIAABCD"]:
        assert leaked not in red
    assert not sc.contains_secret("The quick brown fox, password policy is strong.")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\x\\evil.exe", "evil.exe"),
        ("report\u202efdp.exe", "reportfdp.exe"),
        ("a\x00b.txt", "ab.txt"),
        ("con.txt", "file"),
        ("   ", "file"),
        ("na<me>.txt", "na_me_.txt"),
    ],
)
def test_sanitize_filename(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_long_keeps_extension() -> None:
    out = sanitize_filename("a" * 300 + ".pdf")
    assert len(out) <= 120
    assert out.endswith(".pdf")
