"""The FCR agent's tools: what each does, when the model should use it, and how its input is checked.

| Tool | Technique | Answers |
|---|---|---|
| search_fcr | Lakebase pgvector (hybrid search) | Which passages discuss a topic, optionally for one insurer, year or class |
| get_fcr_details | Unity Catalog function, through managed MCP | Which FCRs an insurer has: years, licence class, PDF links |
| extract_fcr_content | Custom Python function | The full text of one cited page, to quote its figures |
| Genie (optional) | Genie space, through managed MCP | Counts and lists over the register and entity list |

They don't overlap: search finds passages, details lists reports, extract reads a page. Each returns JSON.
A tool given arguments it can't use raises ToolInputError, and one asked for a page that isn't there
raises LookupError; the agent passes the message to the model so it can correct itself.

Importing this module loads psycopg. On Databricks, set PSYCOPG_IMPL=python before importing it (notebooks/03_agent do), since
psycopg's binary build aborts Python on serverless compute; its pure-Python build works there.
"""

import json
from collections.abc import Callable
from typing import Protocol

from databricks.sdk import WorkspaceClient

from fcr_curator.config import ProjectConfig
from fcr_curator.curation import MIN_NAME_KEY_CHARS, institution_key
from fcr_curator.mcp import ToolInfo, genie_mcp_url, mcp_server_tools
from fcr_curator.search import FCR_CLASSES, SearchFilters, SearchHit
from fcr_curator.uc_functions import GET_FCR_DETAILS, uc_function_mcp_url
from fcr_curator.vector_search import LakebasePgVectorManager

SEARCH_FCR = "search_fcr"
EXTRACT_FCR_CONTENT = "extract_fcr_content"
DEFAULT_SEARCH_RESULTS = 5
MAX_SEARCH_RESULTS = 10  # More passages crowd the context without adding answers
EXCERPT_CHARS = 800  # Enough to judge a passage; extract_fcr_content gives the whole page
MAX_PAGE_CHARS = 12_000  # About 3,000 tokens: the longest FCR pages, with room for the rest of the turn
EARLIEST_REPORTING_YEAR = 2000  # Before any Bermuda FCR, so only typos are rejected
LATEST_REPORTING_YEAR = 2100

SEARCH_FCR_SPEC = {
    "type": "function",
    "function": {
        "name": SEARCH_FCR,
        "description": (
            "Search the text of Bermuda insurers' Financial Condition Reports (FCRs) by meaning and keywords. "
            "Use it to find passages about a topic, such as solvency ratios, capital requirements, risk "
            "profile or governance. Returns passages with the insurer, reporting year, page, a citation URL "
            "and an excerpt. Pass the insurer, year and licence class as filters, not in the query."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to find, in the terms FCRs use, e.g. 'BSCR ratio enhanced capital requirement'",
                },
                "insurer": {
                    "type": "string",
                    "description": "Limit to one insurer or reinsurer by short name, e.g. 'Wilton Re'",
                },
                "reporting_year": {"type": "integer", "description": "Limit to FCRs for this year, e.g. 2024"},
                "fcr_class": {
                    "type": "string",
                    "enum": list(FCR_CLASSES),
                    "description": "Limit to insurers holding this BMA licence class; only when the question names one",
                },
                "num_results": {
                    "type": "integer",
                    "description": f"Passages to return, 1 to {MAX_SEARCH_RESULTS} (default {DEFAULT_SEARCH_RESULTS})",
                },
            },
            "required": ["query"],
        },
    },
}

GET_FCR_DETAILS_SPEC = {
    "type": "function",
    "function": {
        "name": GET_FCR_DETAILS,
        "description": (
            "List the Financial Condition Reports registered for one insurer or reinsurer, newest first: "
            "reporting year, registered name, BMA licence class, line of business, PDF link and whether it "
            "was downloaded. Use it to check which years exist before searching, or to give a report's link."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "insurer": {
                    "type": "string",
                    "description": "The insurer's short name without legal suffixes, e.g. 'Wilton Re'",
                },
            },
            "required": ["insurer"],
        },
    },
}

EXTRACT_FCR_CONTENT_SPEC = {
    "type": "function",
    "function": {
        "name": EXTRACT_FCR_CONTENT,
        "description": (
            "Return the full text of one page of an FCR, in reading order, including its tables. Use it "
            "after search_fcr when an excerpt is cut short or a figure needs quoting exactly. Take file_url "
            "and page from a search_fcr result, or file_url from get_fcr_details."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_url": {"type": "string", "description": "The FCR PDF's URL, without any #page= part"},
                "page": {"type": "integer", "description": "The page number as printed in the PDF, from 1"},
            },
            "required": ["file_url", "page"],
        },
    },
}


class ToolInputError(ValueError):
    """A tool was called with arguments it can't use; the message says what to change."""


class ChunkStore(Protocol):
    """What the search and extract tools need from the Lakebase knowledge base."""

    def hybrid_search(self, query: str, filters: SearchFilters, limit: int) -> list[SearchHit]:
        """Return passages matching query by meaning and keywords."""
        ...

    def page(self, citation_url: str) -> list[SearchHit]:
        """Return one cited page's chunks in reading order."""
        ...


def required_text(value: str | None, name: str) -> str:
    """Return value stripped, or raise ToolInputError if it's missing or blank."""
    if not value or not value.strip():
        raise ToolInputError(f"{name} is required")
    return value.strip()


def checked_year(year: int | None) -> int | None:
    """Return year if it's a plausible FCR reporting year (or None), else raise ToolInputError."""
    if year is not None and not EARLIEST_REPORTING_YEAR <= year <= LATEST_REPORTING_YEAR:
        raise ToolInputError(f"reporting_year {year} isn't a plausible FCR year")
    return year


def checked_class(fcr_class: str | None) -> str | None:
    """Return fcr_class if it's one of FCR_CLASSES (or None), else raise ToolInputError."""
    if fcr_class is not None and fcr_class not in FCR_CLASSES:
        raise ToolInputError(f"fcr_class must be one of {', '.join(FCR_CLASSES)}, not {fcr_class!r}")
    return fcr_class


def checked_insurer(insurer: str | None) -> str | None:
    """Return insurer if its name is specific enough to match (or None), else raise ToolInputError."""
    if insurer is not None and len(institution_key(insurer)) < MIN_NAME_KEY_CHARS:
        raise ToolInputError(f"insurer {insurer!r} is too short to match one company; give more of its name")
    return insurer


def checked_result_count(num_results: int) -> int:
    """Return num_results if it's between 1 and MAX_SEARCH_RESULTS, else raise ToolInputError."""
    if not 1 <= num_results <= MAX_SEARCH_RESULTS:
        raise ToolInputError(f"num_results must be between 1 and {MAX_SEARCH_RESULTS}")
    return num_results


def file_url_and_page(hit: SearchHit) -> tuple[str | None, int | None]:
    """Return the PDF URL a hit's citation opens and its page, as extract_fcr_content takes them."""
    file_url = hit.citation_url.split("#", 1)[0] if hit.citation_url else None
    return file_url, hit.page


def passage(hit: SearchHit) -> dict:
    """Return a search hit as the model sees it: where it's from, how to cite it, and an excerpt."""
    file_url, page = file_url_and_page(hit)
    return {
        "insurer": hit.company,
        "reporting_year": hit.reporting_year,
        "fcr_class": hit.fcr_class,
        "file_url": file_url,
        "page": page,
        "citation_url": hit.citation_url,
        "score": round(hit.score, 4) if hit.score is not None else None,
        "excerpt": hit.text[:EXCERPT_CHARS],
    }


def search_fcr_tool(store: ChunkStore) -> ToolInfo:
    """Return search_fcr: hybrid search of the knowledge base in Lakebase, with optional filters."""

    def search_fcr(
        query: str,
        insurer: str | None = None,
        reporting_year: int | None = None,
        fcr_class: str | None = None,
        num_results: int = DEFAULT_SEARCH_RESULTS,
    ) -> str:
        filters = SearchFilters(
            institution=checked_insurer(insurer),
            reporting_year=checked_year(reporting_year),
            fcr_class=checked_class(fcr_class),
        )
        hits = store.hybrid_search(required_text(query, "query"), filters, checked_result_count(num_results))
        return json.dumps([passage(hit) for hit in hits])

    return ToolInfo(name=SEARCH_FCR, spec=SEARCH_FCR_SPEC, exec_fn=search_fcr)


def extract_fcr_content_tool(store: ChunkStore) -> ToolInfo:
    """Return extract_fcr_content: one page's full text, read from the knowledge base in Lakebase."""

    def extract_fcr_content(file_url: str, page: int) -> str:
        url = required_text(file_url, "file_url")
        if not url.startswith(("https://", "http://")) or "#" in url:
            raise ToolInputError("file_url must be the PDF's http(s) URL, without a #page= part")
        if page < 1:
            raise ToolInputError("page counts from 1")
        citation_url = f"{url}#page={page}"
        chunks = store.page(citation_url)
        if not chunks:
            raise LookupError(f"No text for page {page} of {url}; take file_url and page from search_fcr")
        text = "\n\n".join(chunk.text for chunk in chunks)
        first = chunks[0]
        return json.dumps(
            {
                "insurer": first.company,
                "reporting_year": first.reporting_year,
                "fcr_class": first.fcr_class,
                "page": page,
                "citation_url": citation_url,
                "text": text[:MAX_PAGE_CHARS],
                "truncated": len(text) > MAX_PAGE_CHARS,
            }
        )

    return ToolInfo(name=EXTRACT_FCR_CONTENT, spec=EXTRACT_FCR_CONTENT_SPEC, exec_fn=extract_fcr_content)


def get_fcr_details_tool(call_uc_function: Callable[..., str]) -> ToolInfo:
    """Return get_fcr_details: checks the insurer's name, then calls the UC function (through MCP)."""

    def get_fcr_details(insurer: str) -> str:
        name = checked_insurer(required_text(insurer, "insurer"))
        return call_uc_function(insurer=name)

    return ToolInfo(name=GET_FCR_DETAILS, spec=GET_FCR_DETAILS_SPEC, exec_fn=get_fcr_details)


def build_fcr_tools(workspace: WorkspaceClient, config: ProjectConfig, env: str) -> list[ToolInfo]:
    """Return the agent's tools for env; Genie's only when config names a Genie space.

    Raises RuntimeError if the get_fcr_details UC function isn't registered (uc_functions.py).
    """
    host = workspace.config.host
    store = LakebasePgVectorManager(config, env, workspace)
    uc_tools = mcp_server_tools(workspace, uc_function_mcp_url(host, config, GET_FCR_DETAILS))
    if len(uc_tools) != 1:
        raise RuntimeError(f"Expected the {GET_FCR_DETAILS} UC function as one MCP tool, got {len(uc_tools)}")
    tools = [search_fcr_tool(store), get_fcr_details_tool(uc_tools[0].exec_fn), extract_fcr_content_tool(store)]
    if config.genie_space_id:
        tools.extend(mcp_server_tools(workspace, genie_mcp_url(host, config.genie_space_id)))
    return tools
