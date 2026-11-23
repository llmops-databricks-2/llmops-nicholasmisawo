"""Tests for the evaluation cases: the reviewed file is well formed, covers what it must, and converts for MLflow."""

from pathlib import Path

import pytest

from fcr_curator.evaluation import EvalCase, load_cases, split_records

CASES_FILE = Path(__file__).resolve().parents[1] / "evaluation/fcr_eval_cases.yml"
# What both splits must cover, so a change is judged on the same kinds of question it was tuned on
REQUIRED_IN_EACH_SPLIT = {"single_insurer", "follow_up", "count_genie", "missing_evidence", "out_of_scope"}


@pytest.fixture(scope="module")
def cases() -> tuple[EvalCase, ...]:
    """Return the reviewed cases."""
    return load_cases(CASES_FILE)


def test_the_reviewed_cases_load_with_a_dev_set_and_a_smaller_holdout(cases: tuple[EvalCase, ...]) -> None:
    """Between 10 and 20 cases, as the lecture suggests, most of them for development."""
    dev = [case for case in cases if case.split == "dev"]
    holdout = [case for case in cases if case.split == "holdout"]

    assert 10 <= len(cases) <= 20
    assert len(dev) > len(holdout) > 0


@pytest.mark.parametrize("split", ["dev", "holdout"])
def test_each_split_covers_every_required_kind_of_question(cases: tuple[EvalCase, ...], split: str) -> None:
    """Both splits hold normal, follow-up, count, missing-evidence and out-of-scope questions."""
    categories = {case.category for case in cases if case.split == split}

    assert categories >= REQUIRED_IN_EACH_SPLIT


def test_every_follow_up_has_the_questions_it_follows(cases: tuple[EvalCase, ...]) -> None:
    """A follow-up only makes sense after its earlier questions."""
    assert all(case.earlier_questions for case in cases if case.category == "follow_up")


def test_every_fact_based_case_cites_its_evidence(cases: tuple[EvalCase, ...]) -> None:
    """A case expecting facts names where they're from, so a judge can check claims against the source."""
    assert all(case.evidence for case in cases if case.expected_facts)


def test_split_records_give_mlflow_the_inputs_expectations_and_case_id(cases: tuple[EvalCase, ...]) -> None:
    """Records carry the question, its earlier questions, the expectations, and tags to read results by."""
    record = next(
        record for record in split_records(cases, "dev") if record["tags"]["case_id"] == "wilton_followup_previous_year"
    )

    assert record["inputs"]["earlier_questions"] == [
        "How did Wilton Re's solvency compare with its enhanced capital requirement in 2024?"
    ]
    assert set(record["expectations"]) == {"expected_facts", "guidelines", "evidence"}
    assert record["tags"]["category"] == "follow_up"


def test_a_case_with_nothing_to_score_against_is_refused() -> None:
    """A case without expected facts or guidelines can't be scored, so it fails at load."""
    with pytest.raises(ValueError, match="needs expected_facts or guidelines"):
        EvalCase(id="empty", split="dev", category="single_insurer", question="What is the BSCR?")


def test_two_cases_with_one_id_are_refused(tmp_path: Path) -> None:
    """Results are read by case id, so ids must be unique."""
    entry = "  - {id: twice, split: dev, category: out_of_scope, inputs: {question: Hi}, expectations: {guidelines: [Be polite]}}\n"
    path = tmp_path / "cases.yml"
    path.write_text("cases:\n" + entry + entry)

    with pytest.raises(ValueError, match="twice"):
        load_cases(path)
