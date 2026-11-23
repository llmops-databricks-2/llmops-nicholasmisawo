"""Tests for the evaluation cases: the reviewed file is well formed, covers what it must, and converts for MLflow."""

from pathlib import Path

import mlflow
import pytest
from mlflow.pyfunc import ResponsesAgent
from mlflow.types.responses import ResponsesAgentRequest, ResponsesAgentResponse

from fcr_curator.evaluation import (
    EvalCase,
    load_cases,
    load_human_verdicts,
    load_reviewed_run_id,
    make_predict_fn,
    split_records,
)

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


class RecordingAgent:
    """Answers every question with its own text, recording each request's session and question."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def predict(self, request: ResponsesAgentRequest) -> ResponsesAgentResponse:
        """Echo the question back as the answer."""
        question = request.input[0].content
        self.calls.append((request.custom_inputs["session_id"], question))
        item = ResponsesAgent.create_text_output_item(f"Answer to: {question}", "msg_0")
        return ResponsesAgentResponse(output=[item])


def test_predict_fn_asks_a_follow_ups_earlier_questions_first_in_the_same_session() -> None:
    """The follow-up is answered in the session its earlier question was asked in, and its answer returned."""
    agent = RecordingAgent()

    answer = make_predict_fn(agent)(question="And the year before?", earlier_questions=["Wilton Re's 2024 ECR?"])

    assert [question for _, question in agent.calls] == ["Wilton Re's 2024 ECR?", "And the year before?"]
    assert len({session for session, _ in agent.calls}) == 1
    assert answer == "Answer to: And the year before?"


def test_predict_fn_gives_each_case_a_session_of_its_own() -> None:
    """One case's history never reaches another's."""
    agent = RecordingAgent()
    predict_fn = make_predict_fn(agent)

    predict_fn(question="First case")
    predict_fn(question="Second case")

    assert agent.calls[0][0] != agent.calls[1][0]


def test_load_human_verdicts_reads_the_reviewed_and_unreviewed_cases(tmp_path: Path) -> None:
    """Unreviewed cases come back as None, so they're left out of the comparison."""
    path = tmp_path / "labels.yml"
    path.write_text("verdicts:\n  wilton: supported\n  argo: null\n")

    assert load_human_verdicts(path) == {"wilton": "supported", "argo": None}


@pytest.mark.usefixtures("tracing_on")
def test_predict_fn_traces_each_case_with_the_answer_as_its_output() -> None:
    """One trace per case, its output the answer text, so scorers read the answer and not a response object."""
    make_predict_fn(RecordingAgent())(question="Wilton Re's 2024 ECR?")

    trace = mlflow.get_trace(mlflow.get_last_active_trace_id(), flush=True)
    assert trace.data.spans[0].name == "evaluation_case"
    assert trace.data.spans[0].outputs == "Answer to: Wilton Re's 2024 ECR?"


@pytest.fixture
def tracing_on() -> None:
    """Trace into the session's temporary store (conftest.py), so the trace can be read back."""
    mlflow.tracing.enable()
    yield
    mlflow.tracing.disable()


def test_load_reviewed_run_id_refuses_labels_without_the_run_they_review(tmp_path: Path) -> None:
    """Verdicts compared against another run's answers would measure nothing, so a missing run_id fails."""
    path = tmp_path / "labels.yml"
    path.write_text("run_id: null\nverdicts:\n  wilton: supported\n")

    with pytest.raises(ValueError, match="run_id"):
        load_reviewed_run_id(path)


def test_load_reviewed_run_id_returns_the_named_run(tmp_path: Path) -> None:
    """The run named in the labels file is the one compared."""
    path = tmp_path / "labels.yml"
    path.write_text("run_id: e788b88f\nverdicts: {}\n")

    assert load_reviewed_run_id(path) == "e788b88f"
