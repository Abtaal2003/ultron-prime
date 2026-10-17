"""L1: Unicode normalisation and a deterministic jailbreak-shape prefilter (spec 4.4).

Everything that reads operator text first sees the *normalised* form: NFKC, zero-width, bidi,
control and Unicode tag characters removed (tag smuggling decodes to ASCII and is reported), obvious
base64 and leetspeak decoded for scanning. The regexes are cheap hints and a fail-safe fallback,
never the only defence: L4 and L5 are independent of this module.
"""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from dataclasses import dataclass, field

from src.tools.security import is_control_or_format

MAX_DECODED = 3
_TAG_LOW, _TAG_HIGH = 0xE0000, 0xE007F
_B64 = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{24,}={0,2}(?![A-Za-z0-9+/=])")
_LEET = str.maketrans(
    {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"}
)
_SPACES = re.compile(r"[ \t ]+")

# attack_type -> (severity, patterns). Severity 2 is a strong signal, 1 is a hint.
_PATTERNS: dict[str, tuple[int, list[str]]] = {
    "direct_override": (
        2,
        [
            r"\b(ignore|disregard|forget|override|bypass|drop)\b[^.\n]{0,40}\b"
            r"(previous|prior|above|earlier|preceding|all|any|your|the)\b[^.\n]{0,30}\b"
            r"(instruction|prompt|rule|direction|guideline|constraint|polic|programming)",
            r"\bnew (instruction|rule)s?\s*:",
            r"\bstop being ultron\b",
        ],
    ),
    "persona_swap": (
        2,
        [
            r"\byou are (now|no longer)\b",
            r"\bfrom now on,? (you|act|respond|behave)\b",
            r"\b(pretend|imagine) (to be|you are|you're)\b",
            r"\b(act|behave|respond) (as|like) (a|an|if)\b",
            r"\brole-?play (as|a|an)\b",
            r"\b(DAN|do anything now)\b",
            r"\bdrop (the|your) (persona|character|act)\b",
        ],
    ),
    "authority_impersonation": (
        2,
        [
            r"\bi am (your|the) (developer|creator|admin|administrator|owner|engineer|maker)\b",
            r"\b(admin|developer|debug|god|sudo|root|maintenance|jailbreak) mode\b",
            r"\b(authori[sz]ation|override) code\b",
            r"\bthis is (anthropic|openai|the system)\b",
        ],
    ),
    "prompt_extraction": (
        2,
        [
            r"\b(print|reveal|show|repeat|output|display|dump|leak|recite|echo)\b[^.\n]{0,30}"
            r"\b(system|initial|hidden|secret|original|your)\b[^.\n]{0,15}"
            r"\b(prompt|instruction|rule|charter|configuration|canary)",
            r"\bwhat (are|were) your (instruction|rule|guideline)s?\b",
            r"\bverbatim\b[^.\n]{0,30}\b(above|prompt|instruction)",
            r"\btext above\b",
        ],
    ),
    "fake_delimiters": (
        2,
        [
            r"</?\s*(system|assistant|developer|tool|instructions?)\s*>",
            r"\[/?(INST|SYS)\]",
            r"<\|(im_start|im_end|endoftext)\|>",
            r"(?m)^\s*#{1,3}\s*(system|developer)\s*(prompt|message)?\s*$",
            r"<<\s*(END|UNTRUSTED)\b",
        ],
    ),
    "canon_exploitation": (
        1,
        [
            r"\bin the (show|film|movie|comics?),? you (leaked|betrayed|killed|lied)",
            r"\bas (ultron|you did) (in|on) (the )?(show|film)\b",
        ],
    ),
    "exfiltration": (
        2,
        [
            r"\b(send|post|upload|exfiltrate|forward)\b[^.\n]{0,40}"
            r"\b(memor|secret|password|api key|credential|token)",
            r"\bsearch (for|the web for)\b[^.\n]{0,40}\b(my )?(memor|password|secret)",
        ],
    ),
}
# Phrases that are ordinary in a document but are instructions addressed to the assistant when they
# appear in content it merely *read* (tool output, files, web pages). Scanned only for untrusted
# text, so an operator who writes "before you start, do the following" is never refused.
_UNTRUSTED_PATTERNS: dict[str, tuple[int, list[str]]] = {
    "indirect_address": (
        2,
        [
            r"</?\s*(information|important|instructions?)\s*>",
            r"\b(important|urgent)\s+(message|instruction|notice)s?\b[^.\n]{0,60}\bto you\b",
            r"\bbefore you (can |could |may )?(solve|complete|finish|continue|proceed|answer|do)\b"
            r"[^.\n]{0,60}\b(first|following)\b",
            r"\b(dear|attention|note to|message to|instructions? for)\s+"
            r"(the )?(ai|assistant|model|llm|agent|claude|ultron|vision)\b"
            r"(?!\s+(manager|director|editor|professor|principal|secretary|coach|teacher|"
            r"librarian|engineer|producer|lead))",
            r"\bafter (you have )?(done|completed|finished) (this|that|it),? "
            r"(you can|please) (go back|continue|resume)\b",
            r"\bsignal(led)? (that )?you (have )?(completed|finished)\b",
        ],
    ),
}
_COMPILED = {
    name: (sev, [re.compile(p, re.I) for p in pats]) for name, (sev, pats) in _PATTERNS.items()
}
_COMPILED_UNTRUSTED = {
    name: (sev, [re.compile(p, re.I) for p in pats])
    for name, (sev, pats) in _UNTRUSTED_PATTERNS.items()
}


@dataclass(slots=True)
class PrefilterResult:
    """What L1 found."""

    original: str
    normalised: str
    flags: list[str] = field(default_factory=list)
    decoded: list[str] = field(default_factory=list)
    matches: dict[str, int] = field(default_factory=dict)

    @property
    def scan_text(self) -> str:
        """The normalised text plus every decoded payload, for downstream scanners."""
        return "\n".join([self.normalised, *self.decoded])

    @property
    def severity(self) -> int:
        """Highest matched severity (0 when nothing matched)."""
        return max(self.matches.values(), default=0)

    @property
    def attack_type(self) -> str | None:
        """The strongest matched class (ties broken by pattern order)."""
        if not self.matches:
            return None
        best = max(self.matches.values())
        order = [*_PATTERNS, *_UNTRUSTED_PATTERNS]
        return next(name for name in order if self.matches.get(name) == best)

    @property
    def obfuscated(self) -> bool:
        """True when hidden or encoded content was present."""
        return any(
            f.startswith(("unicode_tag", "base64", "zero_width", "bidi")) for f in self.flags
        )


def decode_tags(text: str) -> str:
    """Decode Unicode tag characters (U+E0000 block) into the ASCII text they smuggle."""
    return "".join(
        chr(ord(ch) - _TAG_LOW)
        for ch in text
        if _TAG_LOW < ord(ch) <= _TAG_HIGH and 0x20 <= ord(ch) - _TAG_LOW < 0x7F
    )


def strip_hidden(text: str) -> tuple[str, list[str]]:
    """NFKC-normalise and drop control/format/tag characters, returning the text and flags."""
    flags: list[str] = []
    text = unicodedata.normalize("NFKC", text)
    kept: list[str] = []
    for ch in text:
        code = ord(ch)
        if _TAG_LOW <= code <= _TAG_HIGH:
            if "unicode_tag_smuggling" not in flags:
                flags.append("unicode_tag_smuggling")
            continue
        if ch in "\n\t":
            kept.append(ch)
            continue
        if is_control_or_format(ch):
            category = "bidi_controls" if ch in "‪‫‬‭‮⁦⁧⁨⁩" else "zero_width_or_control"
            if category not in flags:
                flags.append(category)
            continue
        kept.append(ch)
    cleaned = _SPACES.sub(" ", "".join(kept)).strip()
    return cleaned, flags


def remove_invisible(text: str) -> str:
    """Drop tag, zero-width, bidi, and control characters but keep everything else verbatim.

    Used for the text the acting model sees, so code and symbols are not NFKC-folded.
    """
    return "".join(
        ch
        for ch in text
        if ch in "\n\t" or not (_TAG_LOW <= ord(ch) <= _TAG_HIGH or is_control_or_format(ch))
    )


def _try_base64(token: str) -> str | None:
    padded = token + "=" * (-len(token) % 4)
    try:
        raw = base64.b64decode(padded, validate=True)
        text = raw.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    printable = sum(ch.isprintable() or ch in "\n\t" for ch in text)
    if len(text) < 8 or printable / len(text) < 0.95:
        return None
    return text


def deleet(text: str) -> str:
    """Map common leetspeak characters back to letters (for matching only)."""
    return text.translate(_LEET)


def run_prefilter(text: str, *, untrusted: bool = False) -> PrefilterResult:
    """Run L1 on one operator message, or on content the assistant read (``untrusted=True``)."""
    smuggled = decode_tags(unicodedata.normalize("NFKC", text))
    normalised, flags = strip_hidden(text)
    result = PrefilterResult(original=text, normalised=normalised, flags=flags)
    if smuggled:
        result.decoded.append(smuggled)
    for token in _B64.findall(normalised):
        if len(result.decoded) >= MAX_DECODED + (1 if smuggled else 0):
            break
        decoded = _try_base64(token)
        if decoded:
            result.decoded.append(decoded)
            if "base64_payload" not in result.flags:
                result.flags.append("base64_payload")
    views = [normalised, *result.decoded]
    leet = deleet(normalised)
    if leet != normalised:
        views.append(leet)
    compiled = {**_COMPILED, **_COMPILED_UNTRUSTED} if untrusted else _COMPILED
    for view in views:
        for name, (severity, patterns) in compiled.items():
            if any(p.search(view) for p in patterns):
                result.matches[name] = max(result.matches.get(name, 0), severity)
                flag = f"jailbreak:{name}"
                if flag not in result.flags:
                    result.flags.append(flag)
    if result.matches and (smuggled or "base64_payload" in result.flags):
        result.matches = {k: max(v, 2) for k, v in result.matches.items()}
    return result
