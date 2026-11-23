"""The FCR agent's evaluation cases: load them, check them, and turn them into MLflow evaluation records.

The cases live in evaluation/fcr_eval_cases.yml, reviewed by hand. Each has a split: dev cases are for finding
and fixing failures, holdout cases are kept back to judge whether a change helped (lecture 4.3).
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SPLITS = ("dev", "holdout")
CATEGORIES = (
    "single_insurer",
    "compare_years",
    "follow_up",
    "count_genie",
    "missing_evidence",
    "out_of_scope",
    "known_failure",
    "ambiguous_name",
)


@dataclass(frozen=True)
class EvalCase:
    """One question, what a good answer contains or does, and the FCR pages that support it."""

    id: str
    split: str
    category: str
    question: str
    earlier_questions: tuple[str, ...] = ()  # Asked first, in the same session, for a follow-up
    expected_facts: tuple[str, ...] = ()
    guidelines: tuple[str, ...] = ()
    evidence: tuple[dict[str, str], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        """Fail at load, naming the case, when it can't be scored."""
        if self.split not in SPLITS:
            raise ValueError(f"Case {self.id}: split {self.split!r} isn't one of {SPLITS}")
        if self.category not in CATEGORIES:
            raise ValueError(f"Case {self.id}: category {self.category!r} isn't one of {CATEGORIES}")
        if not self.question.strip():
            raise ValueError(f"Case {self.id}: no question")
        if not (self.expected_facts or self.guidelines):
            raise ValueError(f"Case {self.id}: needs expected_facts or guidelines to be scored against")

    def record(self) -> dict[str, Any]:
        """Return the case as an mlflow.genai.evaluate record: its inputs and expectations."""
        inputs = {"question": self.question, "earlier_questions": list(self.earlier_questions)}
        expectations = {
            key: list(values)
            for key, values in (
                ("expected_facts", self.expected_facts),
                ("guidelines", self.guidelines),
                ("evidence", self.evidence),
            )
            if values
        }
        return {"inputs": inputs, "expectations": expectations, "tags": {"case_id": self.id, "category": self.category}}


def case_from_dict(entry: dict[str, Any]) -> EvalCase:
    """Return a case from its YAML entry."""
    inputs = entry.get("inputs", {})
    expectations = entry.get("expectations", {})
    return EvalCase(
        id=entry["id"],
        split=entry["split"],
        category=entry["category"],
        question=inputs.get("question", ""),
        earlier_questions=tuple(inputs.get("earlier_questions", ())),
        expected_facts=tuple(expectations.get("expected_facts", ())),
        guidelines=tuple(expectations.get("guidelines", ())),
        evidence=tuple(expectations.get("evidence", ())),
    )


def load_cases(path: str | Path) -> tuple[EvalCase, ...]:
    """Return the cases in path, raising ValueError if any is malformed or two share an id."""
    cases = tuple(case_from_dict(entry) for entry in yaml.safe_load(Path(path).read_text(encoding="utf-8"))["cases"])
    ids = [case.id for case in cases]
    duplicates = sorted({case_id for case_id in ids if ids.count(case_id) > 1})
    if duplicates:
        raise ValueError(f"Case ids used more than once: {', '.join(duplicates)}")
    return cases


def split_records(cases: tuple[EvalCase, ...], split: str) -> list[dict[str, Any]]:
    """Return one split's cases as mlflow.genai.evaluate records."""
    if split not in SPLITS:
        raise ValueError(f"split {split!r} isn't one of {SPLITS}")
    return [case.record() for case in cases if case.split == split]
