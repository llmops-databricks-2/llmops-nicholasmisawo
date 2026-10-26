"""Rewrite a user's question into what search needs: precise search text and metadata filters.

Users ask "how solvent was Wilton Re last year?". Search works better with the terms FCRs actually use
("BSCR ratio", "enhanced capital requirement") and with the insurer and year applied as filters, not
left in the text where they'd dilute the embedding.
"""

from dataclasses import dataclass
from datetime import date

from fcr_curator.gateway import Chat, chat_json
from fcr_curator.search import SearchFilters

INSTRUCTIONS = """You turn a question about Bermuda insurers' Financial Condition Reports (FCRs) into a search request.
Reply with JSON only: {{"search_text": string, "institution": string or null, "reporting_year": integer or null}}
- search_text: the question as a precise search phrase in the terms FCRs use, e.g. "BSCR ratio", "eligible capital",
  "enhanced capital requirement". Leave out the insurer's name and the year.
- institution: the insurer's short name without legal suffixes, e.g. "Wilton Re", or null if none is named.
- reporting_year: the year the FCR covers, or null. An FCR is published after its year ends, and today is
  {today}, so "last year" or "latest" means {last_year}."""


@dataclass(frozen=True)
class RewrittenQuery:
    """A question rewritten for search."""

    search_text: str
    filters: SearchFilters


def rewrite_query(question: str, chat: Chat, today: date) -> RewrittenQuery:
    """Return the question's search text and filters, as the chat model reads them.

    Raises ValueError when the reply has no search_text, since searching without one would be a guess.
    """
    instructions = INSTRUCTIONS.format(today=today.isoformat(), last_year=today.year - 1)
    reply = chat_json(chat, instructions, question)
    search_text = (reply.get("search_text") or "").strip()
    if not search_text:
        raise ValueError(f"Query rewrite returned no search_text for {question!r}: {reply}")
    year = reply.get("reporting_year")
    return RewrittenQuery(
        search_text=search_text,
        filters=SearchFilters(institution=reply.get("institution") or None, reporting_year=int(year) if year else None),
    )
