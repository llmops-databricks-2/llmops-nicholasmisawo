"""The FCR agent: a tool-calling loop over the chat model, with each session's history kept in Lakebase.

Code controls the shape of every turn, and the model makes the decisions within it:

| Code controls | The model decides |
|---|---|
| Loading the session's last HISTORY_MESSAGES messages | Whether a question needs a tool at all |
| Which tools exist, and checking their arguments | Which tool, with what arguments |
| At most MAX_TOOL_ROUNDS rounds of tool calls, then one answer without tools | When it has enough to answer |
| Running tools; returning a bad call's error to the model | How to recover from that error |
| Saving the question and answer, not the tool calls | The answer and its citations |

Only questions and answers are saved: tool results are large, and the next turn can look facts up again.
"""

import json
from dataclasses import dataclass
from typing import Any, Protocol

from loguru import logger
from openai import OpenAI

from fcr_curator.agent_tools import ToolInputError
from fcr_curator.mcp import ToolInfo

MAX_TOOL_ROUNDS = 8  # A question needs 2 or 3; more means the model is going in circles
HISTORY_MESSAGES = 20  # The last 10 exchanges: enough for follow-ups, without crowding the context
ANSWER_MAX_TOKENS = 4000  # Room for a reasoning model to think, then cite several FCRs
NO_ANSWER = "I couldn't answer within the tool calls allowed. Please ask a narrower question."

# How to use the tools, appended to the system prompt. FCRs often give the figures a ratio is worked out
# from rather than the ratio: without this, a model asked for a "solvency ratio" kept searching for one
TOOL_GUIDANCE = """HOW TO USE THE TOOLS
- FCRs usually report the figures a ratio is worked out from rather than the ratio: eligible capital,
  the enhanced capital requirement (ECR) and the minimum margin of solvency (MSM). When no ratio is
  stated, work it out (eligible capital / ECR) and say that you did.
- After a search, read the most relevant page in full with extract_fcr_content before searching again.
  Don't repeat a search with only reworded queries.
- For counts and lists across insurers, ask the Genie tool (query_space_...) a plain-English question,
  not SQL: Genie knows the tables and writes the SQL itself.
- Cite every figure with the insurer, reporting year, page and citation_url."""
# Sent when the tool rounds run out, so the turn ends with what was found rather than nothing
ANSWER_NOW = "You have used all your tool calls. Answer now from what the tools returned, and say plainly what you couldn't find."

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


@dataclass(frozen=True)
class FCRAgent:
    """Answers questions about FCRs with tools, continuing each session from its saved history."""

    client: OpenAI
    model: str  # The Unity Gateway chat model service
    system_prompt: str
    tools: tuple[ToolInfo, ...]
    memory: ConversationMemory

    def answer(self, question: str, session_id: str) -> str:
        """Answer question in the session, and save the exchange for the session's next turn."""
        user_turn = {"role": "user", "content": question}
        history = self.memory.load_messages(session_id, HISTORY_MESSAGES)
        system_turn = {"role": "system", "content": f"{self.system_prompt}\n\n{TOOL_GUIDANCE}"}
        reply = self._converse([system_turn, *history, user_turn])
        self.memory.save_messages(session_id, [user_turn, {"role": "assistant", "content": reply}])
        return reply

    def _converse(self, messages: list[Message]) -> str:
        """Let the model call tools until it answers; after MAX_TOOL_ROUNDS rounds, have it answer without."""
        for _ in range(MAX_TOOL_ROUNDS):
            message = self._complete(messages)
            if not message.tool_calls:
                return message.content or ""
            messages.append(assistant_tool_calls(message))
            messages.extend(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": self.run_tool(call.function.name, call.function.arguments),
                }
                for call in message.tool_calls
            )
        logger.warning(f"No answer after {MAX_TOOL_ROUNDS} rounds of tool calls; asking for one without tools")
        final = self._complete([*messages, {"role": "system", "content": ANSWER_NOW}], allow_tools=False)
        return final.content or NO_ANSWER

    def _complete(self, messages: list[Message], allow_tools: bool = True) -> Any:  # noqa: ANN401 - the OpenAI SDK's ChatCompletionMessage
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=[tool.spec for tool in self.tools],
            tool_choice="auto" if allow_tools else "none",
            max_tokens=ANSWER_MAX_TOKENS,
        )
        return response.choices[0].message

    def run_tool(self, name: str, arguments: str) -> str:
        """Run one tool call and return its result, or an error the model can act on.

        Errors from the model's own arguments go back to it; anything else (an outage, a bug) propagates.
        """
        tools = {tool.name: tool for tool in self.tools}
        if name not in tools:
            return tool_error(f"Unknown tool {name!r}; use one of {', '.join(tools)}")
        try:
            kwargs = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return tool_error(f"Arguments for {name} aren't valid JSON: {arguments!r}")
        logger.info(f"Tool {name}({kwargs})")
        try:
            return str(tools[name].exec_fn(**kwargs))
        except (ToolInputError, LookupError, TypeError) as error:
            logger.warning(f"Tool {name} rejected {kwargs}: {error}")
            return tool_error(str(error))
