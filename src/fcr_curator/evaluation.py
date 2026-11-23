"""The FCR agent's evaluation cases: load them, check them, and turn them into MLflow evaluation records.

The cases live in evaluation/fcr_eval_cases.yml, reviewed by hand. Each has a split: dev cases are for finding
and fixing failures, holdout cases are kept back to judge whether a change helped (lecture 4.3).
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import mlflow
import yaml
from mlflow.entities import SpanType
from mlflow.types.responses import ResponsesAgentRequest, ResponsesAgentResponse

from fcr_curator.agent import response_text

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


class Agent(Protocol):
    """What evaluation needs of the agent: to answer a request."""

    def predict(self, request: ResponsesAgentRequest) -> ResponsesAgentResponse:
        """Answer the request."""
        ...


def ask(agent: Agent, question: str, session_id: str) -> str:
    """Ask one question in the session, with a request id of its own, and return the answer's text."""
    request = ResponsesAgentRequest(
        input=[{"role": "user", "content": question}],
        custom_inputs={"session_id": session_id, "request_id": f"req-{uuid4()}"},
    )
    return response_text(agent.predict(request))


def make_predict_fn(agent: Agent) -> Callable[..., str]:
    """Return mlflow.genai.evaluate's predict_fn: each case in a new session, its earlier questions first.

    A fresh session per case keeps one case's history out of another's; a follow-up's earlier questions are
    asked in its session first, so the agent loads them from memory as it would in a real conversation.

    The function is traced, so each case is one trace with the answer as its output and every turn's spans
    beneath it. Untraced, MLflow sometimes takes the agent's own predict trace as the case's, and the scorers
    then read the whole response object rather than the answer.
    """

    @mlflow.trace(span_type=SpanType.CHAIN, name="evaluation_case")
    def predict_fn(question: str, earlier_questions: list[str] | None = None) -> str:
        session_id = f"eval-{uuid4()}"
        for earlier in earlier_questions or []:
            ask(agent, earlier, session_id)
        return ask(agent, question, session_id)

    return predict_fn


def load_human_verdicts(path: str | Path) -> dict[str, str | None]:
    """Return a reviewer's verdicts by case id; None where a case hasn't been reviewed yet."""
    return dict(yaml.safe_load(Path(path).read_text(encoding="utf-8")).get("verdicts") or {})


def load_reviewed_run_id(path: str | Path) -> str:
    """Return the evaluation run whose answers the verdicts are for; ValueError if it isn't set."""
    run_id = (yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}).get("run_id")
    if not run_id:
        raise ValueError(f"Set run_id in {path}: the evaluation run whose answers you reviewed")
    return str(run_id)
