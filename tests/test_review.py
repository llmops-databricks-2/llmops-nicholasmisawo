"""Tests for the judge review: a blind sheet of one run's answers, and that run's judge verdicts."""

from fcr_curator.evaluation import EvalCase
from fcr_curator.review import CaseResult, judge_verdicts, review_sheet

CASE = EvalCase(
    id="wilton_capital_vs_msm",
    split="dev",
    category="known_failure",
    question="How did Wilton Re's capital compare with its MSM in 2024?",
    expected_facts=("Wilton Re's MSM was 377.0",),
    evidence=({"citation_url": "https://www.wiltonre.bm/fcr-2024.pdf#page=16", "quote": "MSM 377.0"},),
)
RESULT = CaseResult(trace_id="tr-1", answer="About 4.66 times.\nMSM 377.0.", verdict="supported", rationale="Matches.")


def test_review_sheet_shows_the_answer_and_evidence_but_not_the_judges_verdict() -> None:
    """The reviewer sees what they need to judge, and nothing that would anchor them."""
    sheet = review_sheet([CASE], {CASE.id: RESULT}, "run-1")

    assert "> About 4.66 times." in sheet
    assert "MSM 377.0" in sheet
    assert "Matches." not in sheet
    assert "`run-1`" in sheet


def test_review_sheet_leaves_out_cases_the_run_didnt_answer() -> None:
    """Only the run's own answers are reviewed."""
    assert "## wilton_capital_vs_msm" not in review_sheet([CASE], {}, "run-1")


def test_judge_verdicts_leave_out_cases_the_judge_didnt_rate() -> None:
    """A case the judge skipped can't be compared, so it isn't counted."""
    skipped = CaseResult(trace_id="tr-2", answer="", verdict=None, rationale=None)

    assert judge_verdicts({"wilton": RESULT, "argo": skipped}) == {"wilton": "supported"}
