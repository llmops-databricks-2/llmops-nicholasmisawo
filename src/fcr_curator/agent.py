"""The FCR agent: a traced tool-calling loop over the chat model, each session's history kept in Lakebase.

FCRAgent is an MLflow ResponsesAgent, shaped like the course's ArxivAgent so it can be logged and served:
predict / predict_stream take a ResponsesAgentRequest whose custom_inputs carry session_id and request_id.
from_config builds its parts as ArxivAgent's constructor does: the gateway client, the Lakebase pgvector
tools, the UC function and Genie tools, and the session memory.

Code controls the shape of every turn, and the model makes the decisions within it:

| Code controls | The model decides |
|---|---|
| Loading the session's last history_messages messages | Whether a question needs a tool at all |
| Which tools exist, and checking their arguments | Which tool, with what arguments |
| At most max_tool_rounds rounds of tool calls, then one answer without tools | When it has enough to answer |
| Running tools; returning a bad call's error to the model | How to recover from that error |
| Saving the question and answer, not the tool calls | The answer and its citations |

Each turn is one trace (the course's layout, with two changes):

    predict_stream               AGENT     tagged with the session, request id, git_sha and deployment
    ├─ memory_load               MEMORY    the course uses RETRIEVER, which retrieval judges would read as evidence
    ├─ call_and_run_tools        CHAIN
    │  ├─ call_llm               LLM       with the gateway's token usage; the course's call_llm isn't traced
    │  ├─ <tool name>            TOOL      e.g. search_fcr, with its arguments and result
    │  │  └─ hybrid_search       RETRIEVER the passages found (vector_search.py)
    │  └─ ...                              until the model answers
    └─ memory_save               MEMORY

The loop is ours rather than the course's: the model may call several tools in one round, a bad call's error
goes back to it, and when the rounds run out it answers from what it found instead of "Max iterations reached".
"""

import json
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import Any, Protocol

import mlflow
from databricks.sdk import WorkspaceClient
from loguru import logger
from mlflow.entities import SpanType
from mlflow.pyfunc import ResponsesAgent
from mlflow.tracing.constant import SpanAttributeKey, TokenUsageKey
from mlflow.types.responses import (
    ResponsesAgentRequest,
    ResponsesAgentResponse,
    ResponsesAgentStreamEvent,
    to_chat_completions_input,
)
from openai import OpenAI

from fcr_curator.agent_tools import ToolInputError, build_fcr_tools
from fcr_curator.config import AgentSettings, ProjectConfig
from fcr_curator.gateway import gateway_client, retry_when_rate_limited
from fcr_curator.mcp import ToolInfo
from fcr_curator.memory import LakebaseMemory
from fcr_curator.tracing import release_tags

NO_ANSWER = "I couldn't answer within the tool calls allowed. Please ask a narrower question."
# Sent when the tool rounds run out, so the turn ends with what was found rather than nothing
ANSWER_NOW = "You have used all your tool calls. Answer now from what the tools returned, and say plainly what you couldn't find."
CHAT_ATTEMPTS = 3  # A turn waits at most two minutes for the gateway's rate limit before failing
SESSION_METADATA = "mlflow.trace.session"  # MLflow groups a session's traces by this key
OUTPUT_ITEM_DONE = "response.output_item.done"

Message = dict[str, Any]


class ConversationMemory(Protocol):
    """Where a session's messages are kept between turns."""

    def load_messages(self, session_id: str, limit: int) -> list[Message]:
        """Return the session's last `limit` messages, oldest first."""
        ...

    def save_messages(self, session_id: str, messages: list[Message]) -> None:
        """Append messages to the session."""
        ...


def tool_error(message: str) -> str:
    """Return a tool error as the model sees it."""
    return json.dumps({"error": message})


def assistant_tool_calls(message: Any) -> Message:  # noqa: ANN401 - the OpenAI SDK's ChatCompletionMessage
    """Return the assistant's tool-call turn as a chat message, without fields the gateway rejects."""
    return {
        "role": "assistant",
        "content": message.content,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.function.name, "arguments": call.function.arguments},
            }
            for call in message.tool_calls
        ],
    }


def tool_call_items(call: Any, result: str) -> list[Message]:  # noqa: ANN401 - the OpenAI SDK's tool call
    """Return a tool call and its result as Responses output items, as a ResponsesAgent reports them."""
    return [
        ResponsesAgent.create_function_call_item(call.id, call.id, call.function.name, call.function.arguments),
        ResponsesAgent.create_function_call_output_item(call.id, result),
    ]


def record_token_usage(usage: Any) -> None:  # noqa: ANN401 - the OpenAI SDK's CompletionUsage, or None
    """Record the gateway's token counts on the current LLM span; missing usage is left missing, not zero."""
    span = mlflow.get_current_active_span()
    if usage is None or span is None:
        return
    span.set_attribute(
        SpanAttributeKey.CHAT_USAGE,
        {
            TokenUsageKey.INPUT_TOKENS: usage.prompt_tokens,
            TokenUsageKey.OUTPUT_TOKENS: usage.completion_tokens,
            TokenUsageKey.TOTAL_TOKENS: usage.total_tokens,
        },
    )


def response_text(response: ResponsesAgentResponse) -> str:
    """Return the answer's text from a response, without the tool calls before it."""
    return "\n".join(
        part["text"]
        for item in response.model_dump()["output"]
        if item.get("type") == "message"
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    )


@dataclass(frozen=True)
class TurnResult:
    """What one turn produced: the answer, and every output item (tool calls, their results, the answer)."""

    answer: str
    output_items: tuple[Message, ...]


@dataclass(frozen=True)
class ToolLoop:
    """Lets the model call tools until it answers, within the configured number of rounds."""

    client: OpenAI
    model: str  # The Unity Gateway chat model service
    tools: tuple[ToolInfo, ...]
    settings: AgentSettings
    sleep: Callable[[float], None] = time.sleep

    def run(self, messages: list[Message]) -> TurnResult:
        """Return the turn's answer; after max_tool_rounds rounds of tool calls, one given without tools."""
        items: list[Message] = []
        for _ in range(self.settings.max_tool_rounds):
            message = self.call_llm(messages)
            if not message.tool_calls:
                return self.answered(message.content or "", items)
            messages.append(assistant_tool_calls(message))
            for call in message.tool_calls:
                result = self.run_tool(call.function.name, call.function.arguments)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
                items.extend(tool_call_items(call, result))
        logger.warning(
            f"No answer after {self.settings.max_tool_rounds} rounds of tool calls; asking for one without tools"
        )
        final = self.call_llm([*messages, {"role": "system", "content": ANSWER_NOW}], allow_tools=False)
        return self.answered(final.content or NO_ANSWER, items)

    @staticmethod
    def answered(answer: str, items: list[Message]) -> TurnResult:
        """Return the turn's result, its answer the last output item."""
        message = ResponsesAgent.create_text_output_item(answer, f"msg_{len(items)}")
        return TurnResult(answer, (*items, message))

    @mlflow.trace(span_type=SpanType.LLM, name="call_llm")
    def call_llm(self, messages: list[Message], allow_tools: bool = True) -> Any:  # noqa: ANN401 - the OpenAI SDK's ChatCompletionMessage
        """Return the model's next message, recording its token usage on this LLM span."""
        response = retry_when_rate_limited(
            lambda: self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=[tool.spec for tool in self.tools],
                tool_choice="auto" if allow_tools else "none",
                max_tokens=self.settings.answer_max_tokens,
            ),
            self.model,
            CHAT_ATTEMPTS,
            self.sleep,
        )
        record_token_usage(getattr(response, "usage", None))
        return response.choices[0].message

    @property
    def tools_by_name(self) -> dict[str, ToolInfo]:
        """Return the tools keyed by the name the model calls them by."""
        return {tool.name: tool for tool in self.tools}

    def run_tool(self, name: str, arguments: str) -> str:
        """Run one tool call and return its result, or an error the model can act on.

        Errors from the model's own arguments go back to it; anything else (an outage, a bug) propagates.
        """
        if name not in self.tools_by_name:
            return tool_error(f"Unknown tool {name!r}; use one of {', '.join(self.tools_by_name)}")
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return tool_error(f"Arguments for {name} aren't valid JSON: {arguments!r}")
        logger.info(f"Tool {name}({args})")
        try:
            return self.execute_tool(name, args)
        except (ToolInputError, LookupError, TypeError) as error:
            logger.warning(f"Tool {name} rejected {args}: {error}")
            return tool_error(str(error))

    def execute_tool(self, tool_name: str, args: dict[str, Any]) -> str:
        """Run the tool in a TOOL span named after it, so the trace reads search_fcr rather than execute_tool.

        A tool that raises leaves its span marked as an error, with the exception recorded.
        """
        with mlflow.start_span(name=tool_name, span_type=SpanType.TOOL) as span:
            span.set_inputs(args)
            result = str(self.tools_by_name[tool_name].exec_fn(**args))
            span.set_outputs(result)
            return result


class FCRAgent(ResponsesAgent):
    """Answers questions about FCRs with tools, continuing each session from its saved history."""

    def __init__(self, loop: ToolLoop, memory: ConversationMemory, system_prompt: str, history_messages: int) -> None:
        """Answer with loop, keeping sessions in memory; build one for an environment with from_config."""
        self.loop = loop
        self.memory = memory
        self.system_prompt = system_prompt  # prompts/fcr_curator.txt, its tool guidance included
        self.history_messages = history_messages

    @classmethod
    def from_config(
        cls,
        config: ProjectConfig,
        env: str,
        genie_space_id: str | None = None,
        workspace: WorkspaceClient | None = None,
    ) -> "FCRAgent":
        """Return the agent for env, with the Lakebase pgvector, UC function and (given its id) Genie tools.

        Raises RuntimeError if the get_fcr_details UC function isn't registered (agent_tools.build_fcr_tools).
        """
        workspace = workspace or WorkspaceClient()
        tools = build_fcr_tools(workspace, config, env, genie_space_id)
        loop = ToolLoop(gateway_client(workspace), config.llm_endpoint, tuple(tools), config.agent)
        memory = LakebaseMemory(workspace, config, env)
        return cls(loop, memory, config.system_prompt, config.agent.history_messages)

    @mlflow.trace(span_type=SpanType.MEMORY, name="memory_load")
    def load_memory(self, session_id: str) -> list[Message]:
        """Return the session's latest messages from Lakebase."""
        return self.memory.load_messages(session_id, self.history_messages)

    @mlflow.trace(span_type=SpanType.MEMORY, name="memory_save")
    def save_memory(self, session_id: str, messages: list[Message]) -> None:
        """Append the turn's question and answer to the session."""
        self.memory.save_messages(session_id, messages)

    @mlflow.trace(span_type=SpanType.CHAIN)
    def call_and_run_tools(
        self,
        request_input: list[Message],
        previous_messages: list[Message],
        request_id: str | None = None,
        session_id: str | None = None,
    ) -> list[Message]:
        """Tag the trace, answer the request after the session's history, and save the question and answer."""
        mlflow.update_current_trace(
            tags=release_tags(),
            metadata={SESSION_METADATA: session_id} if session_id else {},
            client_request_id=request_id,
        )
        system_turn = {"role": "system", "content": self.system_prompt}
        turn = self.loop.run([system_turn, *previous_messages, *request_input])
        if session_id:
            self.save_memory(session_id, [*request_input, {"role": "assistant", "content": turn.answer}])
        return list(turn.output_items)

    def predict(self, request: ResponsesAgentRequest) -> ResponsesAgentResponse:
        """Answer the request; custom_inputs may carry session_id (to continue a session) and request_id."""
        events = list(self.predict_stream(request))
        return ResponsesAgentResponse(output=[event.item for event in events], custom_outputs=request.custom_inputs)

    @mlflow.trace(span_type=SpanType.AGENT)
    def predict_stream(self, request: ResponsesAgentRequest) -> Generator[ResponsesAgentStreamEvent, None, None]:
        """Yield the turn's output items once it's done; the loop isn't streamed token by token."""
        custom_inputs = request.custom_inputs or {}
        session_id = custom_inputs.get("session_id")
        previous_messages = self.load_memory(session_id) if session_id else []
        request_input = to_chat_completions_input([item.model_dump() for item in request.input])
        items = self.call_and_run_tools(
            request_input, previous_messages, request_id=custom_inputs.get("request_id"), session_id=session_id
        )
        for item in items:
            yield ResponsesAgentStreamEvent(type=OUTPUT_ITEM_DONE, item=item)
