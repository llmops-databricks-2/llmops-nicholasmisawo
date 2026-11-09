"""MCP (Model Context Protocol) integration utilities.

DatabricksMCPClient's synchronous methods call asyncio.run, which fails inside an event loop that's
already running, as a notebook's is. outside_event_loop runs them on a worker thread there instead, so
the tools work the same from a laptop, a notebook or a job, without patching asyncio (nest_asyncio).
"""

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from databricks.sdk import WorkspaceClient
from databricks_mcp import DatabricksMCPClient
from pydantic import BaseModel, ConfigDict


def outside_event_loop[T](call: Callable[[], T]) -> T:
    """Return call(), run on a worker thread when this thread already runs an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return call()
    with ThreadPoolExecutor(max_workers=1) as worker:
        return worker.submit(call).result()


class ToolInfo(BaseModel):
    """Tool information for agent integration.

    Attributes:
        name: Tool name
        spec: JSON description of the tool (OpenAI Responses format)
        exec_fn: Function that implements the tool logic

    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    spec: dict
    exec_fn: Callable


def create_managed_exec_fn(server_url: str, tool_name: str, w: WorkspaceClient) -> Callable:
    """Create an execution function for an MCP tool.

    Args:
        server_url: MCP server URL
        tool_name: Name of the tool
        w: Databricks workspace client

    Returns:
        Callable that executes the tool

    """

    def exec_fn(**kwargs: Any) -> str:  # noqa: ANN401 - arguments follow the MCP tool's own schema
        client = DatabricksMCPClient(server_url=server_url, workspace_client=w)
        response = outside_event_loop(lambda: client.call_tool(tool_name, kwargs))
        return "".join([c.text for c in response.content])

    return exec_fn


def genie_mcp_url(host: str, space_id: str) -> str:
    """Return the managed MCP server URL of a Genie space."""
    return f"{host.rstrip('/')}/api/2.0/mcp/genie/{space_id}"


def mcp_server_tools(w: WorkspaceClient, server_url: str) -> list[ToolInfo]:
    """Return the tools one MCP server offers, each calling the server when run.

    Works inside a notebook's running event loop too (see outside_event_loop).
    """
    tools = []
    for mcp_tool in outside_event_loop(DatabricksMCPClient(server_url=server_url, workspace_client=w).list_tools):
        input_schema = mcp_tool.inputSchema.copy() if mcp_tool.inputSchema else {}
        tool_spec = {
            "type": "function",
            "function": {
                "name": mcp_tool.name,
                "parameters": input_schema,
                "description": mcp_tool.description or f"Tool: {mcp_tool.name}",
            },
        }
        exec_fn = create_managed_exec_fn(server_url, mcp_tool.name, w)
        tools.append(ToolInfo(name=mcp_tool.name, spec=tool_spec, exec_fn=exec_fn))
    return tools


async def create_mcp_tools(w: WorkspaceClient, url_list: list[str]) -> list[ToolInfo]:
    """Create tools from MCP servers (the course's async interface; see mcp_server_tools).

    Args:
        w: Databricks workspace client
        url_list: List of MCP server URLs

    Returns:
        List of ToolInfo objects

    """
    return [tool for server_url in url_list for tool in mcp_server_tools(w, server_url)]
