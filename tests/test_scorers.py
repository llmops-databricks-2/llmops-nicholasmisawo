"""Tests for the code scorers and the judge-agreement check, from what each must catch."""

import pytest

from fcr_curator.agent import NO_ANSWER
from fcr_curator.scorers import (
    answered,
    cites_page_urls,
    figures_in,
    figures_stated,
    judge_agreement,
    no_tool_markup,
    skip_without,
)

WILTON = {
    "expected_facts": [
        "Wilton Re's total eligible capital at 31 December 2024 was 1,757.3 ($ millions)",
        "Its enhanced capital requirement (ECR) was 717.1 ($ millions)",
        "Eligible capital was about 2.45 times the ECR",
    ],
    "evidence": [{"citation_url": "https://www.wiltonre.bm/fcr-2024.pdf#page=15", "quote": "1,757.3"}],
}


def test_figures_in_reads_printed_figures_but_not_dates_years_or_class_numbers() -> None:
    """Dates, years and 'Class 4' aren't figures; thousands separators and percentages are."""
    text = "At 31 December 2024, Class 4 capital was 1,757.3 against $1,156 million, a ratio of 142%."

    assert figures_in(text) == {1757.3, 1156.0, 1.42}


def test_figures_stated_counts_a_percentage_as_the_same_ratio() -> None:
    """'245%' states the expected 'about 2.45 times'."""
    feedback = figures_stated(outputs="1,757.3 against 717.1, about 245%", expectations=WILTON)

    assert feedback.value == 1.0


def test_figures_stated_names_the_figures_an_answer_leaves_out() -> None:
    """A partial answer scores the share it states, naming what's missing."""
    feedback = figures_stated(outputs="Eligible capital was 1,757.3.", expectations=WILTON)

    assert feedback.value == 0.33
    assert "717.1" in feedback.rationale


def test_figures_stated_skips_a_case_expecting_no_figures() -> None:
    """An out-of-scope case has nothing to count, so it isn't scored at all."""
    assert figures_stated(outputs="I can't advise on that.", expectations={"guidelines": ["Decline"]}) is None


def test_cites_page_urls_wants_a_link_to_an_fcr_page() -> None:
    """A page link passes; a bare document link or a tool-style marker doesn't."""
    assert cites_page_urls(outputs="See https://www.wiltonre.bm/fcr-2024.pdf#page=15", expectations=WILTON).value
    assert not cites_page_urls(outputs="See 【extract_fcr_content†page=15】", expectations=WILTON).value


def test_cites_page_urls_skips_a_case_whose_evidence_is_the_register() -> None:
    """Register answers come from Genie, with no page to link."""
    register_case = {"evidence": [{"source": "dim_fcr_entities", "quote": "6 rows"}]}

    assert cites_page_urls(outputs="6 insurers", expectations=register_case) is None


@pytest.mark.parametrize(
    ("output", "expected"),
    [("1,757.3 against 717.1", True), ("", False), ("   ", False), (NO_ANSWER, False)],
)
def test_answered_fails_an_empty_reply_and_the_no_answer_fallback(output: str, expected: bool) -> None:
    """Only a real answer counts as one."""
    assert answered(outputs=output) is expected


def test_no_tool_markup_catches_copied_tool_citations() -> None:
    """The markers the model copied from tool output (lecture 3's finding) fail; links pass."""
    assert not no_tool_markup(outputs="245% 【search_fcr†2024‑ECR】")
    assert no_tool_markup(outputs="245% (https://www.wiltonre.bm/fcr-2024.pdf#page=16)")


def test_judge_agreement_compares_only_the_cases_a_person_reviewed() -> None:
    """Unreviewed cases are left out; each disagreement names the case and both verdicts."""
    judge = {"wilton": "supported", "argo": "unsupported", "axa": "supported"}
    human = {"wilton": "supported", "argo": "supported", "axa": None}

    agreement = judge_agreement(judge, human)

    assert (agreement.compared, agreement.agreed, agreement.rate) == (2, 1, 0.5)
    assert agreement.disagreements == ("argo: judge unsupported, human supported",)


def test_judge_agreement_has_no_rate_before_any_review() -> None:
    """With nothing reviewed there's no rate, rather than a misleading 0 or 100%."""
    assert judge_agreement({"wilton": "supported"}, {"wilton": None}).rate is None


def test_judge_agreement_refuses_a_verdict_it_doesnt_know() -> None:
    """A typo in the labels file is caught rather than counted as a disagreement."""
    with pytest.raises(ValueError, match="supportd"):
        judge_agreement({"wilton": "supported"}, {"wilton": "supportd"})


class CountingJudge:
    """Stands in for a built-in judge, counting the cases it's asked to judge."""

    name = "correctness"

    def __init__(self) -> None:
        self.judged = 0

    def __call__(self, inputs: dict, outputs: str, expectations: dict) -> bool:
        """Judge every case it's given as passing."""
        self.judged += 1
        return True


def test_skip_without_leaves_out_a_case_missing_the_judges_expectation() -> None:
    """An out-of-scope case has no expected facts, so correctness skips it rather than failing on it."""
    judge = CountingJudge()
    wrapped = skip_without("expected_facts", judge)

    skipped = wrapped(inputs={"question": "Buy Arch?"}, outputs="I can't advise.", expectations={"guidelines": ["x"]})
    judged = wrapped(inputs={"question": "ECR?"}, outputs="717.1", expectations=WILTON)

    assert (skipped, judged, judge.judged) == (None, True, 1)
    assert wrapped.name == "correctness"
