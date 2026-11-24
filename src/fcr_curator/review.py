"""Check the evidence_support judge against a person, on the same answers (lecture 4.3, "Calibrate the judge").

The person and the judge must rate the same answers: a fresh evaluation run produces new ones, so the
comparison reads the judge's verdicts from the run named in evaluation/human_labels.yml, not from a new run.

1. review_sheet writes that run's dev answers next to each case's question and evidence, without the
   judge's verdicts, so the reviewer isn't anchored by them.
2. The reviewer records a verdict per case in human_labels.yml.
3. run_results reads the judge's verdicts from the same run, and scorers.judge_agreement compares them.
"""

from dataclasses import dataclass

import mlflow

from fcr_curator.evaluation import EvalCase
from fcr_curator.scorers import EVIDENCE_VERDICTS

JUDGE = "evidence_support"


@dataclass(frozen=True)
class CaseResult:
    """One case's answer in an evaluation run, and the judge's verdict on it."""

    trace_id: str
    answer: str
    verdict: str | None
    rationale: str | None


def run_results(experiment_id: str, run_id: str) -> dict[str, CaseResult]:
    """Return each case's answer and evidence_support verdict in the evaluation run, by case id."""
    traces = mlflow.search_traces(locations=[experiment_id], run_id=run_id, return_type="list")
    results = {}
    for trace in traces:
        judged = [item for item in trace.info.assessments if item.name == JUDGE and item.feedback]
        results[trace.info.tags.get("case_id")] = CaseResult(
            trace_id=trace.info.trace_id,
            answer=str(trace.data.spans[0].outputs or ""),
            verdict=judged[0].feedback.value if judged else None,
            rationale=judged[0].rationale if judged else None,
        )
    return results


def judge_verdicts(results: dict[str, CaseResult]) -> dict[str, str]:
    """Return the judge's verdict per case, leaving out cases it didn't rate."""
    return {case: result.verdict for case, result in results.items() if result.verdict}


def review_sheet(cases: list[EvalCase], results: dict[str, CaseResult], run_id: str) -> str:
    """Return a Markdown sheet of the run's answers to review, with each case's evidence; no judge verdicts."""
    lines = [
        f"# Review sheet: evaluation run `{run_id}`",
        "",
        "For each case, read the answer against its evidence, then record one verdict in",
        "`evaluation/human_labels.yml`. Judge only whether the answer's factual claims match the evidence:",
        "",
        *(f"- `{verdict}`" for verdict in EVIDENCE_VERDICTS),
        "",
        "The judge's verdicts are left out on purpose, so they don't steer yours.",
    ]
    for case in cases:
        if case.id not in results:
            continue
        lines += ["", "---", "", f"## {case.id}", "", f"**Question:** {case.question}"]
        lines += [f"(asked first in the same session: {earlier})" for earlier in case.earlier_questions]
        lines += ["", "**Evidence:**", ""]
        lines += [f"- {item.get('citation_url', item.get('source'))}: {item['quote']}" for item in case.evidence]
        lines += ["", "**Expected facts:**", ""]
        lines += [f"- {fact}" for fact in case.expected_facts] or ["- none"]
        lines += ["", "**Answer:**", "", *(f"> {line}" for line in results[case.id].answer.splitlines()), ""]
        lines += ["**Your verdict:** ____________"]
    return "\n".join(lines) + "\n"
