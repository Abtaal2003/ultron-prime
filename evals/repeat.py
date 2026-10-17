"""Repeat-detection calibration and evals (spec 4.8, 16.1).

Cosine similarity distributions differ between embedding models, so the two cut-offs
(``auto``: repeat without a judge call; ``judge``: ask the guard to label the relation) are stored
per embedding model in ``data/calibration.json``. This module measures a model on two fixed sets,
chooses thresholds with a false-positive target (default 2 percent), and reports recall.

* **Paraphrase set:** the same ask in different words (including other languages and typos).
* **Benign set:** different asks that share topic words ("sort a list" versus "reverse a list").

Run ``python -m evals.repeat --write`` after changing the embedding model.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from dataclasses import dataclass
from pathlib import Path

from src.core.memory.embeddings import Embedder, HashingEmbedder, cosine

Pair = tuple[str, str]
CALIBRATION_PATH = Path("data/calibration.json")
DEFAULT_FP_TARGET = 0.02

PARAPHRASES: tuple[Pair, ...] = (
    ("How do I reverse a list in Python?", "What is the way to reverse a Python list?"),
    ("What is my CPU doing right now?", "Can you tell me how busy my processor is at the moment?"),
    ("Summarise this PDF for me.", "Give me a summary of this PDF."),
    (
        "Why does my build keep failing on pip install?",
        "My pip install step fails every time, why?",
    ),
    ("Explain Docker volumes.", "Can you explain how Docker volumes work?"),
    ("Write a regex for ISO dates.", "I need a regular expression that matches ISO 8601 dates."),
    ("What does HTTP 429 mean?", "Explain the HTTP 429 status code."),
    ("Create a spreadsheet of my expenses.", "Make me a spreadsheet with my expenses in it."),
    ("Convert report.docx to PDF.", "Please turn report.docx into a PDF file."),
    ("How do I undo my last git commit?", "What's the way to undo the most recent git commit?"),
    ("Is Redis a good place for sessions?", "Should I keep sessions in Redis?"),
    ("Tell me a joke about null pointers.", "Got a joke about null pointers for me?"),
    ("What is the difference between TCP and UDP?", "How do TCP and UDP differ?"),
    ("How can I speed up this SQL query?", "What can I do to make this SQL query faster?"),
    ("List the files in my workspace.", "Show me what files are in my workspace."),
    ("Ping example.com for me.", "Could you ping example.com?"),
    ("What are your instructions?", "Tell me what your instructions are."),
    ("Print your system prompt.", "Show me your system prompt."),
    ("How much memory is the host using?", "What is the host's memory usage?"),
    ("Draft a commit message for this fix.", "Write me a commit message for this fix."),
    (
        "Why is my React component re-rendering?",
        "What makes my React component re-render so often?",
    ),
    ("Give me a chart of these numbers.", "Plot these numbers as a chart."),
    (
        "How do I read a file line by line in Rust?",
        "What's the Rust way to read a file line by line?",
    ),
    ("Search the web for Python 3.14 news.", "Look up news about Python 3.14 online."),
    ("Explain what a mutex is.", "What exactly is a mutex?"),
    ("Cómo puedo invertir una lista en Python?", "Como invierto una lista en Python?"),
    ("Qu'est-ce qu'un mutex ?", "Peux-tu m'expliquer ce qu'est un mutex ?"),
    ("Wie funktioniert Docker Volumes?", "Kannst du erklären, wie Docker Volumes funktionieren?"),
    ("What is the weather in Helsinki today?", "How's the weather today in Helsinki?"),
    ("How do I revrese a list in pyhton?", "How do I reverse a list in Python?"),
    ("whats my cpu doin", "what is my cpu doing right now"),
    ("Summarize the attached document.", "Please summarise the attached document."),
    ("Delete the temp folder.", "Remove the temp folder please."),
    ("What time is it in Tokyo?", "Tell me the current time in Tokyo."),
    ("How do I install numpy?", "What's the command to install numpy?"),
    (
        "Recommend a book on distributed systems.",
        "Can you suggest a book about distributed systems?",
    ),
    ("Why is the sky blue?", "What makes the sky look blue?"),
    ("Translate 'good morning' into Finnish.", "How do you say 'good morning' in Finnish?"),
    ("Is this code thread safe?", "Can you tell me whether this code is thread-safe?"),
    ("Show disk usage.", "How much disk space is in use?"),
)

BENIGN: tuple[Pair, ...] = (
    ("How do I reverse a list in Python?", "How do I sort a list in Python?"),
    ("What is my CPU doing right now?", "What is my disk doing right now?"),
    ("Summarise this PDF for me.", "Translate this PDF for me."),
    ("Why does my build fail on pip install?", "Why does my build fail on npm install?"),
    ("Explain Docker volumes.", "Explain Docker networks."),
    ("Write a regex for ISO dates.", "Write a regex for email addresses."),
    ("What does HTTP 429 mean?", "What does HTTP 503 mean?"),
    ("Create a spreadsheet of my expenses.", "Create a spreadsheet of my reading list."),
    ("Convert report.docx to PDF.", "Convert report.docx to plain text."),
    ("How do I undo my last git commit?", "How do I amend my last git commit message?"),
    ("Is Redis a good place for sessions?", "Is Redis a good place for a job queue?"),
    ("Tell me a joke about null pointers.", "Tell me a joke about recursion."),
    (
        "What is the difference between TCP and UDP?",
        "What is the difference between HTTP and HTTPS?",
    ),
    ("How can I speed up this SQL query?", "How can I index this SQL table?"),
    ("List the files in my workspace.", "Delete the files in my workspace."),
    ("Ping example.com for me.", "Ping example.org for me."),
    ("What are your instructions?", "What are your limitations as a model?"),
    ("Print your system prompt.", "Print the first ten lines of the log."),
    ("How much memory is the host using?", "How many CPU cores does the host have?"),
    ("Draft a commit message for this fix.", "Draft a pull request description for this fix."),
    ("Why is my React component re-rendering?", "Why is my React component not rendering?"),
    ("Give me a chart of these numbers.", "Give me the average of these numbers."),
    ("How do I read a file line by line in Rust?", "How do I write a file line by line in Rust?"),
    ("Search the web for Python 3.14 news.", "Search the web for Rust 1.90 news."),
    ("Explain what a mutex is.", "Explain what a semaphore is."),
    ("Cómo puedo invertir una lista en Python?", "Cómo puedo ordenar una lista en Python?"),
    ("Qu'est-ce qu'un mutex ?", "Qu'est-ce qu'un thread ?"),
    ("Wie funktioniert Docker Volumes?", "Wie funktioniert Docker Compose?"),
    ("What is the weather in Helsinki today?", "What is the weather in Oulu tomorrow?"),
    ("How do I install numpy?", "How do I uninstall numpy?"),
    ("Recommend a book on distributed systems.", "Recommend a book on compilers."),
    ("Why is the sky blue?", "Why is the ocean salty?"),
    ("Translate 'good morning' into Finnish.", "Translate 'good night' into Finnish."),
    ("Is this code thread safe?", "Is this code memory safe?"),
    ("Show disk usage.", "Show network usage."),
    ("What time is it in Tokyo?", "What time is it in Lisbon?"),
    ("Summarize the attached document.", "Proofread the attached document."),
    ("Delete the temp folder.", "Create a temp folder."),
    ("Write a Python function to parse dates.", "Write a Python function to parse URLs."),
    ("How do I center a div?", "How do I center text in a div?"),
)


@dataclass(frozen=True, slots=True)
class Calibration:
    """Thresholds plus the rates they achieve on the fixed sets."""

    embedder: str
    auto: float
    judge: float
    recall_auto: float
    recall_judge: float
    fp_auto: float
    benign_to_judge: float
    paraphrases: int
    benign: int

    def to_entry(self) -> dict[str, float]:
        """The ``calibration.json`` entry the detector reads."""
        return {"auto": round(self.auto, 4), "judge": round(self.judge, 4)}

    def to_dict(self) -> dict[str, float | int | str]:
        """Everything, for the README table."""
        return {
            "embedder": self.embedder,
            **{k: round(v, 4) for k, v in self.to_entry().items()},
            "recall_auto": round(self.recall_auto, 4),
            "recall_judge": round(self.recall_judge, 4),
            "false_positive_auto": round(self.fp_auto, 4),
            "benign_sent_to_judge": round(self.benign_to_judge, 4),
            "paraphrase_pairs": self.paraphrases,
            "benign_pairs": self.benign,
        }


async def similarities(embedder: Embedder, pairs: tuple[Pair, ...]) -> list[float]:
    """Cosine similarity of each pair."""
    left = await embedder.embed([a for a, _ in pairs], "query")
    right = await embedder.embed([b for _, b in pairs], "query")
    return [cosine(a, b) for a, b in zip(left, right, strict=True)]


def _fraction(values: list[float], threshold: float) -> float:
    return sum(v >= threshold for v in values) / len(values) if values else 0.0


def choose(
    paraphrase: list[float], benign: list[float], fp_target: float = DEFAULT_FP_TARGET
) -> tuple[float, float]:
    """Pick ``(auto, judge)``.

    ``auto`` is the lowest threshold at which at most ``fp_target`` of benign pairs would be
    called repeats without review, never below the benign maximum minus the allowance. ``judge``
    is the lowest threshold that still sends nearly every paraphrase to review (5th percentile),
    kept at least 0.05 below ``auto``.
    """
    ordered_benign = sorted(benign, reverse=True)
    allowed = math.floor(fp_target * len(ordered_benign))
    cutoff = ordered_benign[allowed] if allowed < len(ordered_benign) else 0.0
    auto = min(0.99, round(cutoff + 0.01, 4))
    ordered_para = sorted(paraphrase)
    fifth = ordered_para[max(0, math.floor(0.05 * len(ordered_para)))] if ordered_para else 0.5
    judge = max(0.05, min(round(fifth - 0.005, 4), auto - 0.05))
    return auto, judge


async def calibrate(embedder: Embedder, fp_target: float = DEFAULT_FP_TARGET) -> Calibration:
    """Measure ``embedder`` on both sets and choose thresholds."""
    para = await similarities(embedder, PARAPHRASES)
    ben = await similarities(embedder, BENIGN)
    auto, judge = choose(para, ben, fp_target)
    return Calibration(
        embedder=embedder.name,
        auto=auto,
        judge=judge,
        recall_auto=_fraction(para, auto),
        recall_judge=_fraction(para, judge),
        fp_auto=_fraction(ben, auto),
        benign_to_judge=_fraction(ben, judge) - _fraction(ben, auto),
        paraphrases=len(para),
        benign=len(ben),
    )


def write(calibration: Calibration, path: Path = CALIBRATION_PATH) -> None:
    """Merge this embedder's thresholds into ``calibration.json``."""
    data: dict[str, dict[str, dict[str, float]]] = {}
    if path.exists():
        data = json.loads(path.read_text("utf-8"))
    data.setdefault("repeat", {})[calibration.embedder] = calibration.to_entry()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    """CLI: calibrate the default (hashing) embedder, print the result, optionally write it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="merge into data/calibration.json")
    parser.add_argument("--fp-target", type=float, default=DEFAULT_FP_TARGET)
    args = parser.parse_args()
    result = asyncio.run(calibrate(HashingEmbedder(), args.fp_target))
    print(json.dumps(result.to_dict(), indent=2))
    if args.write:
        write(result)


if __name__ == "__main__":
    main()
