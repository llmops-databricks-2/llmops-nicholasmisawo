"""Tests for the agent: its loop, session history, the trace it leaves, and errors returned to the model."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import mlflow
import pytest
from mlflow.entities import SpanStatusCode, SpanType
from mlflow.tracing.constant import SpanAttributeKey
from mlflow.types.responses import ResponsesAgentRequest, ResponsesAgentResponse

from fcr_curator.agent import FINAL_ANSWER_ATTEMPTS, NO_ANSWER, FCRAgent, ToolLoop, response_text
from fcr_curator.agent_tools import ToolInputError
from fcr_curator.config import AgentSettings
from fcr_curator.mcp import ToolInfo
from fcr_curator.tracing import record_token_usage

SESSION = "session-1"
REQUEST = "request-1"
SYSTEM_PROMPT = "You answer FCR questions."
# Smaller than project_config.yml's limits, so the tests show the configured values are the ones used
SETTINGS = AgentSettings(
    max_tool_rounds=3,
    history_messages=4,
    answer_max_tokens=100,
    default_search_results=2,
    max_search_results=4,
    excerpt_chars=50,
    max_page_chars=200,
)
HISTORY = [{"role": "user", "content": "Who is Wilton Re?"}, {"role": "assistant", "content": "A reinsurer."}]


def tool_call(name: str, arguments: str, call_id: str = "call-1") -> SimpleNamespace:
    """Build a tool call as the OpenAI SDK returns it."""
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def reply(content: str | None = None, tool_calls: list | None = None, usage: object = None) -> SimpleNamespace:
    """Build a chat completion with one message."""
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


class FakeClient:
    """Returns the given replies in turn, recording the messages each request sent."""

    def __init__(self, *replies: SimpleNamespace) -> None:
        self.replies = list(replies)
        self.requests: list[list[dict]] = []
        self.tool_choices: list[object] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **request: object) -> SimpleNamespace:
        """Record a copy of the request's messages and return the next reply."""
        self.requests.append([dict(message) for message in request["messages"]])
        self.tool_choices.append(request.get("tool_choice"))
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


class FakeMemory:
    """Holds sessions in a dict, recording each load."""

    def __init__(self, history: list[dict]) -> None:
        self.sessions = {SESSION: list(history)}
        self.loads: list[tuple[str, int]] = []

    def load_messages(self, session_id: str, limit: int) -> list[dict]:
        """Return the session's last `limit` messages."""
        self.loads.append((session_id, limit))
        return self.sessions.get(session_id, [])[-limit:]

    def save_messages(self, session_id: str, messages: list[dict]) -> None:
        """Append messages to the session."""
        self.sessions.setdefault(session_id, []).extend(messages)


def echo_tool(name: str = "echo") -> ToolInfo:
    """Return a tool echoing its text, refusing an empty one."""

    def echo(text: str) -> str:
        if not text:
            raise ToolInputError("text is required")
        return json.dumps({"echo": text})

    return ToolInfo(name=name, spec={"type": "function", "function": {"name": name}}, exec_fn=echo)


def tool_loop(client: FakeClient) -> ToolLoop:
    """Build a loop with the echo tool."""
    return ToolLoop(client, "chat-model", (echo_tool(),), SETTINGS)


def agent(client: FakeClient, memory: FakeMemory | None = None) -> FCRAgent:
    """Build an agent with the echo tool."""
    return FCRAgent(tool_loop(client), memory or FakeMemory([]), SYSTEM_PROMPT, SETTINGS.history_messages)


def ask(test_agent: FCRAgent, question: str, session_id: str | None = SESSION) -> ResponsesAgentResponse:
    """Send one question as a ResponsesAgentRequest, as notebook 04_tracing/custom_agent.py does."""
    custom_inputs = {"session_id": session_id, "request_id": REQUEST} if session_id else {}
    request = ResponsesAgentRequest(input=[{"role": "user", "content": question}], custom_inputs=custom_inputs)
    return test_agent.predict(request)


def test_predict_runs_the_tool_the_model_calls_then_returns_its_answer() -> None:
    """The tool's result goes back to the model, whose next reply is the answer."""
    client = FakeClient(reply(tool_calls=[tool_call("echo", '{"text": "BSCR"}')]), reply("The BSCR is 245%."))

    response = ask(agent(client), "What is the BSCR?")

    assert response_text(response) == "The BSCR is 245%."
    assert client.requests[1][-1] == {"role": "tool", "tool_call_id": "call-1", "content": '{"echo": "BSCR"}'}


def test_predict_reports_each_tool_call_and_its_result_before_the_answer() -> None:
    """The output lists the call, its result and then the answer, as a ResponsesAgent's output items."""
    client = FakeClient(reply(tool_calls=[tool_call("echo", '{"text": "BSCR"}')]), reply("The BSCR is 245%."))

    output = ask(agent(client), "What is the BSCR?").model_dump()["output"]

    assert [item["type"] for item in output] == ["function_call", "function_call_output", "message"]
    assert (output[0]["name"], output[1]["output"]) == ("echo", '{"echo": "BSCR"}')


def test_predict_starts_from_the_sessions_history_and_saves_only_the_exchange() -> None:
    """History comes before the question; the question and answer are saved, not the tool calls."""
    memory = FakeMemory(HISTORY)
    client = FakeClient(reply(tool_calls=[tool_call("echo", '{"text": "x"}')]), reply("Its 2024 FCR."))

    ask(agent(client, memory), "And its latest FCR?")

    assert client.requests[0][1:] == [*HISTORY, {"role": "user", "content": "And its latest FCR?"}]
    assert memory.sessions[SESSION][2:] == [
        {"role": "user", "content": "And its latest FCR?"},
        {"role": "assistant", "content": "Its 2024 FCR."},
    ]
    assert memory.loads == [(SESSION, SETTINGS.history_messages)]


def test_predict_without_a_session_neither_loads_nor_saves_history() -> None:
    """A request with no session_id is answered on its own and leaves nothing in memory."""
    memory = FakeMemory(HISTORY)

    ask(agent(FakeClient(reply("A reinsurer.")), memory), "Who is Wilton Re?", session_id=None)

    assert (memory.loads, memory.sessions[SESSION]) == ([], HISTORY)


def test_predict_sends_the_system_prompt_first_unchanged() -> None:
    """The configured system prompt, which carries the tool guidance, opens every request as it is."""
    client = FakeClient(reply("The BSCR is 245%."))

    ask(agent(client), "What is the BSCR?")

    assert client.requests[0][0] == {"role": "system", "content": SYSTEM_PROMPT}


def test_predict_tags_the_trace_with_the_session_request_and_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """The trace carries the session id, the caller's request id and the code version it came from."""
    monkeypatch.setenv("GIT_SHA", "f16aa9a")

    with patch("fcr_curator.agent.mlflow.update_current_trace") as update_trace:
        ask(agent(FakeClient(reply("A reinsurer."))), "Who is Wilton Re?")

    tags = update_trace.call_args.kwargs
    assert (tags["metadata"], tags["client_request_id"], tags["tags"]["git_sha"]) == (
        {"mlflow.trace.session": SESSION},
        REQUEST,
        "f16aa9a",
    )


def test_loop_asks_for_an_answer_without_tools_once_the_rounds_run_out() -> None:
    """After max_tool_rounds rounds of tool calls, one more request forbids tools, so the turn ends with an answer."""
    looping = [reply(tool_calls=[tool_call("echo", '{"text": "again"}')]) for _ in range(SETTINGS.max_tool_rounds)]
    client = FakeClient(*looping, reply("From what I found: 245%."))

    assert tool_loop(client).run([{"role": "user", "content": "Loop forever"}]).answer == "From what I found: 245%."
    assert client.tool_choices == ["auto"] * SETTINGS.max_tool_rounds + ["none"]


def test_loop_falls_back_to_a_fixed_reply_when_even_the_final_request_is_empty() -> None:
    """A model that returns nothing without tools still gives the user a clear reply."""
    client = FakeClient(reply(tool_calls=[tool_call("echo", '{"text": "again"}')]))

    assert tool_loop(client).run([{"role": "user", "content": "Loop forever"}]).answer == NO_ANSWER


def test_loop_asks_again_without_tools_when_the_model_replies_with_nothing() -> None:
    """An empty reply isn't an answer: the model is asked to answer in text, and that answer is returned."""
    client = FakeClient(
        reply(tool_calls=[tool_call("echo", '{"text": "ECR"}')]),
        reply(content=""),
        reply(content="Argo Re's ECR ratio was 142%."),
    )

    result = tool_loop(client).run([{"role": "user", "content": "Argo Re's ECR ratio?"}])

    assert result.answer == "Argo Re's ECR ratio was 142%."
    assert client.tool_choices == ["auto", "auto", "none"]


def test_loop_gives_the_fixed_reply_when_the_model_stays_empty() -> None:
    """After FINAL_ANSWER_ATTEMPTS empty replies without tools, the user gets a clear reply, not an empty one."""
    client = FakeClient(reply(content=None), reply(content="  "), reply(content=""))

    result = tool_loop(client).run([{"role": "user", "content": "Arch's 2024 FCR?"}])

    assert result.answer == NO_ANSWER
    assert client.tool_choices == ["auto"] + ["none"] * FINAL_ANSWER_ATTEMPTS


def test_run_tool_returns_a_bad_argument_as_an_error_for_the_model() -> None:
    """A tool's ToolInputError goes back to the model as JSON, so it can correct the call."""
    result = tool_loop(FakeClient(reply("unused"))).run_tool("echo", '{"text": ""}')

    assert json.loads(result) == {"error": "text is required"}


def test_run_tool_names_the_tools_when_the_model_calls_one_that_doesnt_exist() -> None:
    """An unknown tool's error lists the tools that do exist."""
    result = tool_loop(FakeClient(reply("unused"))).run_tool("search_web", "{}")

    assert "echo" in json.loads(result)["error"]


def test_run_tool_reports_arguments_that_arent_json_or_dont_fit() -> None:
    """Malformed JSON and unexpected argument names both come back as errors, not exceptions."""
    loop = tool_loop(FakeClient(reply("unused")))

    assert "valid JSON" in json.loads(loop.run_tool("echo", "{text"))["error"]
    assert "error" in json.loads(loop.run_tool("echo", '{"query": "x"}'))


class FakeSpan:
    """Records the attributes set on it."""

    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}

    def set_attribute(self, key: str, value: object) -> None:
        """Record the attribute."""
        self.attributes[key] = value


def test_record_token_usage_puts_the_gateways_counts_on_the_llm_span() -> None:
    """Prompt and completion tokens become MLflow's input and output token usage."""
    span = FakeSpan()
    usage = SimpleNamespace(prompt_tokens=120, completion_tokens=30, total_tokens=150)

    with patch("fcr_curator.tracing.mlflow.get_current_active_span", return_value=span):
        record_token_usage(usage)

    assert span.attributes[SpanAttributeKey.CHAT_USAGE] == {
        "input_tokens": 120,
        "output_tokens": 30,
        "total_tokens": 150,
    }


def test_record_token_usage_leaves_missing_usage_missing_rather_than_zero() -> None:
    """A reply without usage records nothing, so it can't be read as zero tokens."""
    span = FakeSpan()

    with patch("fcr_curator.tracing.mlflow.get_current_active_span", return_value=span):
        record_token_usage(None)

    assert span.attributes == {}


@pytest.fixture
def tracing_on() -> None:
    """Trace into the session's temporary store (conftest.py), so the trace tree can be read back."""
    mlflow.tracing.enable()
    yield
    mlflow.tracing.disable()


@pytest.mark.usefixtures("tracing_on")
def test_predict_traces_the_turn_as_the_course_lays_it_out_with_memory_and_llm_spans() -> None:
    """One trace: memory_load, the CHAIN (its LLM and TOOL spans) and memory_save, under the AGENT spans.

    ResponsesAgent traces predict itself, so predict_stream's AGENT span sits under predict's, as in the course.
    """
    client = FakeClient(reply(tool_calls=[tool_call("echo", '{"text": "BSCR"}')]), reply("The BSCR is 245%."))

    ask(agent(client, FakeMemory(HISTORY)), "What is the BSCR?")

    spans = mlflow.get_trace(mlflow.get_last_active_trace_id(), flush=True).data.spans
    names = {span.span_id: span.name for span in spans}
    tree = sorted((span.name, span.span_type, names.get(span.parent_id)) for span in spans)
    assert tree == [
        ("call_and_run_tools", SpanType.CHAIN, "predict_stream"),
        ("call_llm", SpanType.LLM, "call_and_run_tools"),
        ("call_llm", SpanType.LLM, "call_and_run_tools"),
        ("echo", SpanType.TOOL, "call_and_run_tools"),
        ("memory_load", SpanType.MEMORY, "predict_stream"),
        ("memory_save", SpanType.MEMORY, "call_and_run_tools"),
        ("predict", SpanType.AGENT, None),
        ("predict_stream", SpanType.AGENT, "predict"),
    ]


@pytest.mark.usefixtures("tracing_on")
def test_a_refused_tool_call_marks_its_span_as_an_error_while_the_turn_succeeds() -> None:
    """The trace shows which step failed and why, though the model recovered and answered."""
    client = FakeClient(reply(tool_calls=[tool_call("echo", '{"text": ""}')]), reply("Please give a longer name."))

    ask(agent(client), "Which FCRs has AXA published?", SESSION)

    trace = mlflow.get_trace(mlflow.get_last_active_trace_id(), flush=True)
    [tool_span] = [span for span in trace.data.spans if span.span_type == SpanType.TOOL]
    [exception] = [event for event in tool_span.events if event.name == "exception"]
    assert (str(trace.info.state), tool_span.status.status_code) == ("OK", SpanStatusCode.ERROR)
    assert exception.attributes["exception.message"] == "text is required"
