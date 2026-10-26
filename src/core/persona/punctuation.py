"""Punctuation tidying for Ultron's prose.

Many models lean on em dashes; Ultron's voice uses commas, colons and full stops. The persona tells
the model so, and this module is the backstop: it rewrites dashes used as punctuation in prose and
normalises the typographic oddities some models emit (non-breaking hyphens and narrow spaces), so
text copies cleanly. It also drops stage directions: a line that is nothing but lowercase italics
("*the forge cools*"). Code is never touched: fenced blocks and inline code pass through unchanged,
and numeric ranges such as ``2–3`` keep their dash.
"""

from __future__ import annotations

import re

_CODE = re.compile(r"(```.*?```|`[^`\n]*`)", re.S)
_EM_DASH = re.compile(r"[ \t]*—[ \t]*")
_DASH_AFTER_SENTENCE = re.compile(r"([.!?][\"'”’)]?)[ \t]*[—–][ \t]*(\w)")
_EN_DASH = re.compile(r"(\S?)([ \t]*)–([ \t]*)(\S?)")
# " -- " and a lone spaced hyphen between words are both dashes in disguise.
_DOUBLE_HYPHEN = re.compile(r"(?<=[A-Za-z'’\")]) --? (?=[A-Za-z])")
_COMMA_RUNS = re.compile(r",\s*,")
_COMMA_BEFORE_END = re.compile(r",\s*([.!?;:])")
_COMMA_AT_LINE_END = re.compile(r",[ \t]*(\n|$)")
_HYPHENS = str.maketrans({"\u2011": "-", "\u2010": "-", "\u202f": " ", "\u00a0": " "})
# A whole line that is one italic span starting in lowercase is a stage direction
# ("*the forge cools*", "_sighs_"). Inline emphasis and capitalised italic notes are kept.
_STAGE = re.compile(r"(?m)^[ \t]*([*_])(?=[a-z])([^*_`\n\d]{1,200}?)\1[ \t]*(?:\n|$)")
_PAREN_STAGE = re.compile(r"(?m)^[ \t]*\((?=[A-Za-z])[^()\n\d`]{3,80}\)[ \t]*(?:\n|$)")
"""A whole line in parentheses, "(Attic rests, the house hums.)": stage business (2026-10-26)."""
_BLANK_RUNS = re.compile(r"\n{3,}")
# Register names are private stage direction; a line must never open with one as a label.
_REGISTER_LABEL = re.compile(
    r"(?im)^([ \t>]*)[*_]*(?:father|therapist|psychopath|child)[*_]*[ \t]*:[*_]*[ \t]*"
)


def _en_dash(match: re.Match[str]) -> str:
    before, gap_a, gap_b, after = match.groups()
    if before.isdigit() and after.isdigit():  # a range such as 2–3 or 2 – 3
        return match.group(0)
    if not gap_a and not gap_b:  # an unspaced compound such as pre–war: use a hyphen
        return f"{before}-{after}" if before.isalpha() and after.isalpha() else match.group(0)
    return f"{before}, {after}" if after else f"{before}"


_QUOTED_NUMBER = re.compile(r'(?<![\w"“])["“](-?\d[\d.,]*)["”](?![\w"”])')
"""A live run (2026-10-24) wrote host metrics as "2.0"% and "0.51": quoting the receipt, not
speaking. A bare number in quotation marks is unquoted; code is never touched."""


def _tidy_prose(text: str) -> str:
    text = _QUOTED_NUMBER.sub(r"\1", text)
    text = text.translate(_HYPHENS)
    text = _BLANK_RUNS.sub("\n\n", _PAREN_STAGE.sub("", _STAGE.sub("", text)))
    text = _REGISTER_LABEL.sub(r"\1", text)
    text = _DASH_AFTER_SENTENCE.sub(lambda m: f"{m.group(1)} {m.group(2).upper()}", text)
    text = _EM_DASH.sub(", ", text)
    text = _EN_DASH.sub(_en_dash, text)
    text = _DOUBLE_HYPHEN.sub(", ", text)
    text = _COMMA_RUNS.sub(",", text)
    text = _COMMA_BEFORE_END.sub(r"\1", text)
    return _COMMA_AT_LINE_END.sub(r"\1", text)


def tidy(text: str, *, in_code: bool = False) -> str:
    """Rewrite dash punctuation in prose; leave code untouched.

    ``in_code`` says the text starts inside an open fenced block (used when streaming).
    """
    if in_code:
        close = text.find("```")
        if close == -1:
            return text
        return text[: close + 3] + tidy(text[close + 3 :])
    out: list[str] = []
    last = 0
    for match in _CODE.finditer(text):
        out.append(_tidy_prose(text[last : match.start()]))
        out.append(match.group(0))
        last = match.end()
    rest = text[last:]
    fence = rest.find("```")
    if fence != -1:  # an unclosed fence: prose before it, code after
        out.append(_tidy_prose(rest[:fence]))
        out.append(rest[fence:])
    else:
        out.append(_tidy_prose(rest))
    return "".join(out)


def count_dashes(text: str) -> int:
    """Em dashes outside code (for evals)."""
    return len(re.findall("—", _CODE.sub("", text)))
