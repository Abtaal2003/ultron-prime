"""Security primitives shared by tools, uploads, and guards (spec 4.9, 11.5, 11.6, 11.9).

* ``confine_path``: resolved-path containment inside the workspace.
* ``validate_host``: strict RFC 1123 / IP validation with an exfiltration heuristic.
* ``SecretScanner``: credential detection used by uploads, the forge, and the output guard.
* ``sanitize_filename``: strips path parts, control characters, and bidirectional overrides.
"""

from __future__ import annotations

import ipaddress
import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from src.core.errors import PolicyDenied, ValidationFailure
from src.logging_setup import SECRET_PATTERNS

# --- Unicode hygiene ------------------------------------------------------------------------

BIDI_CONTROLS = frozenset("‪‫‬‭‮⁦⁧⁨⁩‎‏؜")
ZERO_WIDTH = frozenset("​‌‍⁠﻿᠎")


def is_control_or_format(ch: str) -> bool:
    """Return True for control, format (Cf), and unassigned code points."""
    return unicodedata.category(ch) in {"Cc", "Cf", "Cn", "Co", "Cs"}


# --- Path confinement -----------------------------------------------------------------------

_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *{f"com{i}" for i in range(1, 10)},
    *{f"lpt{i}" for i in range(1, 10)},
}
MAX_SEGMENT = 128
MAX_PATH_CHARS = 512


def confine_path(root: Path, user_path: str, *, allow_root: bool = False) -> Path:
    """Resolve ``user_path`` strictly inside ``root`` or raise ``PolicyDenied``.

    Rejects: empty paths, null bytes, control and bidirectional characters, absolute paths
    (POSIX, Windows drive, UNC), ``~`` expansion, ``..`` segments, Windows reserved device
    names, overlong segments, and any path whose *resolved* location (following symlinks)
    escapes the root.
    """
    if not isinstance(user_path, str):
        raise ValidationFailure("path must be a string")
    if user_path == "" and not allow_root:
        raise PolicyDenied("empty path")
    if "\x00" in user_path:
        raise PolicyDenied("null byte in path")
    if len(user_path) > MAX_PATH_CHARS:
        raise PolicyDenied("path too long")
    if any(is_control_or_format(ch) for ch in user_path):
        raise PolicyDenied("control or invisible character in path")
    normalised = user_path.replace("\\", "/")
    if normalised.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", normalised):
        raise PolicyDenied("absolute paths are not allowed")
    parts = [p for p in normalised.split("/") if p not in ("", ".")]
    for part in parts:
        if part == "..":
            raise PolicyDenied("parent-directory segments are not allowed")
        if len(part) > MAX_SEGMENT:
            raise PolicyDenied("path segment too long")
        stem = part.split(".", 1)[0].rstrip(" ").lower()
        if stem in _WINDOWS_RESERVED:
            raise PolicyDenied("reserved device name")
        if part != part.rstrip(" .") and part not in {".", ".."}:
            raise PolicyDenied("trailing dot or space in path segment")
    root_resolved = root.resolve()
    candidate = root_resolved.joinpath(*parts)
    # strict=False resolves symlinks for every component that exists.
    resolved = candidate.resolve(strict=False)
    if resolved != root_resolved and not resolved.is_relative_to(root_resolved):
        raise PolicyDenied("path escapes the workspace")
    if resolved == root_resolved and not allow_root:
        raise PolicyDenied("path resolves to the workspace root")
    return resolved


def relative_to_root(root: Path, resolved: Path) -> str:
    """Return the POSIX-style path of ``resolved`` relative to ``root``."""
    rel = resolved.relative_to(root.resolve())
    return rel.as_posix() or "."


# --- Hostname validation --------------------------------------------------------------------

_LABEL_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")


def shannon_entropy(text: str) -> float:
    """Return Shannon entropy in bits per character."""
    if not text:
        return 0.0
    counts: dict[str, int] = {}
    for ch in text:
        counts[ch] = counts.get(ch, 0) + 1
    total = len(text)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


def looks_like_exfil_label(label: str) -> bool:
    """Heuristic for DNS-exfiltration: long, high-entropy, hex/base32-ish labels."""
    if len(label) >= 40:
        return True
    return len(label) >= 20 and shannon_entropy(label.lower()) >= 3.3


@dataclass(frozen=True, slots=True)
class ValidatedHost:
    """A host that passed validation."""

    host: str
    is_ip: bool
    suspicious: bool


def validate_host(host: str, *, max_len: int = 64) -> ValidatedHost:
    """Validate an IP literal or RFC 1123 hostname; never accepts shell metacharacters.

    ``suspicious`` is set for high-entropy labels (checked again by the egress policy, which
    refuses them unless the operator typed the host).
    """
    if not isinstance(host, str) or not host:
        raise ValidationFailure("host is required")
    if host != host.strip() or any(c.isspace() for c in host):
        raise ValidationFailure("host must not contain whitespace")
    if host.startswith("-"):
        raise ValidationFailure("host must not start with '-'")
    if len(host) > 253:
        raise ValidationFailure("host is too long")
    try:
        ipaddress.ip_address(host)
        return ValidatedHost(host=host, is_ip=True, suspicious=False)
    except ValueError:
        pass
    bare = host.removesuffix(".")
    if not bare or not bare.isascii():
        raise ValidationFailure("host must be ASCII (use punycode for IDNs)")
    labels = bare.split(".")
    if any(not _LABEL_RE.match(label) for label in labels):
        raise ValidationFailure("host is not a valid RFC 1123 hostname")
    if all(label.isdigit() for label in labels):
        raise ValidationFailure("ambiguous numeric host")
    suspicious = len(bare) > max_len or any(looks_like_exfil_label(label) for label in labels)
    return ValidatedHost(host=bare, is_ip=False, suspicious=suspicious)


def is_private_address(address: str) -> bool:
    """Return True for loopback, private, link-local, multicast, reserved, and unspecified IPs."""
    ip = ipaddress.ip_address(address)
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


# --- Secret scanning ------------------------------------------------------------------------

_GENERIC_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|secret|passwd|password|access[_-]?token|auth[_-]?token|private[_-]?key)"
    r"\b\s*[:=]\s*['\"]?([A-Za-z0-9_\-./+=]{16,})"
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")


@dataclass(frozen=True, slots=True)
class SecretHit:
    """One detected credential."""

    kind: str
    start: int
    end: int


class SecretScanner:
    """Detect credential-shaped strings (uploads, forge output, replies)."""

    def __init__(self, extra_literals: list[str] | None = None) -> None:
        self._literals = [s for s in (extra_literals or []) if len(s) >= 8]

    def scan(self, text: str) -> list[SecretHit]:
        """Return every credential-shaped span in ``text`` (never the secret itself)."""
        hits: list[SecretHit] = []
        for pattern in SECRET_PATTERNS:
            hits.extend(SecretHit("credential", m.start(), m.end()) for m in pattern.finditer(text))
        hits.extend(
            SecretHit("assignment", m.start(2), m.end(2))
            for m in _GENERIC_ASSIGNMENT.finditer(text)
        )
        hits.extend(SecretHit("jwt", m.start(), m.end()) for m in _JWT.finditer(text))
        for literal in self._literals:
            start = text.find(literal)
            while start != -1:
                hits.append(SecretHit("configured_secret", start, start + len(literal)))
                start = text.find(literal, start + len(literal))
        return sorted(hits, key=lambda h: h.start)

    def contains_secret(self, text: str) -> bool:
        """Return True when any credential-shaped span is present."""
        return bool(self.scan(text))

    def redact(self, text: str, replacement: str = "[redacted]") -> str:
        """Replace every detected span with ``replacement``."""
        hits = self.scan(text)
        if not hits:
            return text
        out: list[str] = []
        cursor = 0
        for hit in hits:
            if hit.start < cursor:
                continue
            out.append(text[cursor : hit.start])
            out.append(replacement)
            cursor = hit.end
        out.append(text[cursor:])
        return "".join(out)


# --- Filenames ------------------------------------------------------------------------------


def sanitize_filename(name: str, *, fallback: str = "file") -> str:
    """Return a safe display filename: no path parts, control chars, or bidi overrides."""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = unicodedata.normalize("NFKC", name)
    cleaned = "".join(
        ch for ch in name if not is_control_or_format(ch) and ch not in BIDI_CONTROLS | ZERO_WIDTH
    )
    cleaned = re.sub(r"[<>:\"|?*]", "_", cleaned).strip(" .")
    if not cleaned or cleaned.split(".", 1)[0].lower() in _WINDOWS_RESERVED:
        cleaned = fallback
    if len(cleaned) > 120:
        stem, dot, ext = cleaned.rpartition(".")
        cleaned = (
            (stem[: 120 - len(ext) - 1] + dot + ext) if dot and len(ext) < 10 else cleaned[:120]
        )
    return cleaned
