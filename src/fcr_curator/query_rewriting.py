"""Rewrite a user's question into what search needs: precise search text and metadata filters.

Users ask "how solvent was Wilton Re last year?". Search works better with the terms FCRs actually use
("BSCR ratio", "enhanced capital requirement") and with the (re)insurer, year and licence class applied
as filters, not left in the text where they'd dilute the embedding.
"""

from dataclasses import dataclass
from datetime import date

from fcr_curator.gateway import Chat, chat_json
from fcr_curator.search import FCR_CLASSES, SearchFilters

INSTRUCTIONS = """You turn a question about the Financial Condition Reports (FCRs) of Bermuda insurers and reinsurers
into a search request.
Reply with JSON only: {{"search_text": string, "institution": string or null, "reporting_year": integer or null,
"fcr_class": string or null}}
- search_text: the question as a precise search phrase in the terms FCRs use, e.g. "BSCR ratio", "eligible capital",
  "enhanced capital requirement". Leave out the company's name and the year.
- institution: the insurer's or reinsurer's short name without legal suffixes, e.g. "Wilton Re", or null if
  none is named.
- reporting_year: the year the FCR covers, or null. An FCR is published after its year ends, and today is
  {today}, so "last year" or "latest" means {last_year}.
- fcr_class: the BMA licence class the question limits itself to, exactly one of {classes}, or null.
  "Class 4 reinsurers" is "Class 4"; "long-term insurers" or "life reinsurers" is "Long Term Class E";
  "insurance groups" is "Group". Leave out the class from search_text."""


@dataclass(frozen=True)
class RewrittenQuery:
    """A question rewritten for search."""

    search_text: str
    filters: SearchFilters


def rewrite_query(question: str, chat: Chat, today: date) -> RewrittenQuery:
    """Return the question's search text and filters, as the chat model reads them.

    Raises ValueError when the reply has no search_text, since searching without one would be a guess. A
    class that isn't one of FCR_CLASSES is dropped: it would filter out every chunk.
    """
    instructions = INSTRUCTIONS.format(
        today=today.isoformat(), last_year=today.year - 1, classes=", ".join(FCR_CLASSES)
    )
    reply = chat_json(chat, instructions, question)
    search_text = (reply.get("search_text") or "").strip()
    if not search_text:
        raise ValueError(f"Query rewrite returned no search_text for {question!r}: {reply}")
    year = reply.get("reporting_year")
    fcr_class = reply.get("fcr_class") if reply.get("fcr_class") in FCR_CLASSES else None
    return RewrittenQuery(
        search_text=search_text,
        filters=SearchFilters(
            institution=reply.get("institution") or None,
            reporting_year=int(year) if year else None,
            fcr_class=fcr_class,
        ),
    )
