"""Tests for the agent's loop: tool calls, errors returned to the model, and session history."""

import json
from types import SimpleNamespace

from fcr_curator.agent import HISTORY_MESSAGES, MAX_TOOL_ROUNDS, NO_ANSWER, TOOL_GUIDANCE, FCRAgent
from fcr_curator.agent_tools import ToolInputError
from fcr_curator.mcp import ToolInfo

SESSION = "session-1"
HISTORY = [{"role": "user", "content": "Who is Wilton Re?"}, {"role": "assistant", "content": "A reinsurer."}]


def tool_call(name: str, arguments: str, call_id: str = "call-1") -> SimpleNamespace:
    """Build a tool call as the OpenAI SDK returns it."""
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def reply(content: str | None = None, tool_calls: list | None = None) -> SimpleNamespace:
    """Build a chat completion with one message."""
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))])


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
    """Holds sessions in a dict."""

    def __init__(self, history: list[dict]) -> None:
        self.sessions = {SESSION: list(history)}
        self.limits: list[int] = []

    def load_messages(self, session_id: str, limit: int) -> list[dict]:
        """Return the session's last `limit` messages."""
        self.limits.append(limit)
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


def agent(client: FakeClient, memory: FakeMemory | None = None) -> FCRAgent:
    """Build an agent with the echo tool."""
    return FCRAgent(client, "chat-model", "You answer FCR questions.", (echo_tool(),), memory or FakeMemory([]))


def test_answer_runs_the_tool_the_model_calls_then_returns_its_answer() -> None:
    """The tool's result goes back to the model, whose next reply is the answer."""
    client = FakeClient(reply(tool_calls=[tool_call("echo", '{"text": "BSCR"}')]), reply("The BSCR is 245%."))

    answer = agent(client).answer("What is the BSCR?", SESSION)

    assert answer == "The BSCR is 245%."
    assert client.requests[1][-1] == {"role": "tool", "tool_call_id": "call-1", "content": '{"echo": "BSCR"}'}


def test_answer_starts_from_the_sessions_history_and_saves_only_the_exchange() -> None:
    """History comes before the question; the question and answer are saved, not the tool calls."""
    memory = FakeMemory(HISTORY)
    client = FakeClient(reply(tool_calls=[tool_call("echo", '{"text": "x"}')]), reply("Its 2024 FCR."))

    agent(client, memory).answer("And its latest FCR?", SESSION)

    assert client.requests[0][1:] == [*HISTORY, {"role": "user", "content": "And its latest FCR?"}]
    assert memory.sessions[SESSION][2:] == [
        {"role": "user", "content": "And its latest FCR?"},
        {"role": "assistant", "content": "Its 2024 FCR."},
    ]
    assert memory.limits == [HISTORY_MESSAGES]


def test_answer_asks_for_an_answer_without_tools_once_the_rounds_run_out() -> None:
    """After MAX_TOOL_ROUNDS rounds of tool calls, one more request forbids tools, so the turn ends with an answer."""
    looping = [reply(tool_calls=[tool_call("echo", '{"text": "again"}')]) for _ in range(MAX_TOOL_ROUNDS)]
    client = FakeClient(*looping, reply("From what I found: 245%."))

    assert agent(client).answer("Loop forever", SESSION) == "From what I found: 245%."
    assert client.tool_choices == ["auto"] * MAX_TOOL_ROUNDS + ["none"]


def test_answer_falls_back_to_a_fixed_reply_when_even_the_final_request_is_empty() -> None:
    """A model that returns nothing without tools still gives the user a clear reply."""
    client = FakeClient(reply(tool_calls=[tool_call("echo", '{"text": "again"}')]))

    assert agent(client).answer("Loop forever", SESSION) == NO_ANSWER


def test_answer_adds_the_tool_guidance_to_the_system_prompt() -> None:
    """The model is told how FCRs report solvency and how to use the tools, after the project's prompt."""
    client = FakeClient(reply("An answer."))

    agent(client).answer("What is the BSCR?", SESSION)

    assert client.requests[0][0]["content"] == f"You answer FCR questions.\n\n{TOOL_GUIDANCE}"


def test_run_tool_returns_a_bad_argument_as_an_error_for_the_model() -> None:
    """A tool's ToolInputError goes back to the model as JSON, so it can correct the call."""
    result = agent(FakeClient(reply("unused"))).run_tool("echo", '{"text": ""}')

    assert json.loads(result) == {"error": "text is required"}


def test_run_tool_names_the_tools_when_the_model_calls_one_that_doesnt_exist() -> None:
    """An unknown tool's error lists the tools that do exist."""
    result = agent(FakeClient(reply("unused"))).run_tool("search_web", "{}")

    assert "echo" in json.loads(result)["error"]


def test_run_tool_reports_arguments_that_arent_json_or_dont_fit() -> None:
    """Malformed JSON and unexpected argument names both come back as errors, not exceptions."""
    test_agent = agent(FakeClient(reply("unused")))

    assert "valid JSON" in json.loads(test_agent.run_tool("echo", "{text"))["error"]
    assert "error" in json.loads(test_agent.run_tool("echo", '{"query": "x"}'))
