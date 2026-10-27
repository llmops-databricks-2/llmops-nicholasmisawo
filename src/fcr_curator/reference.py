"""The reference list of insurers and reinsurers: who the search task looks for, and the names FCRs get.

The list is a dbt seed (dbt/seeds/bermuda_entities.csv), loaded as the table ref_bermuda_entities,
which the pipeline reads. Its columns:

- `name`: the insurer's name, as searched for and as shown on its FCRs and chunks (`registered_name`)
- `assets_bn_usd`: total assets; insurers are searched largest first, so when a run's search budget runs
  out, it's the smallest that wait for the next run
- `domains` (optional): websites the insurer publishes on, separated by `;`. Matching on a domain beats
  matching on a name, since FCR titles often name a subsidiary or nothing at all. Give hosts only, no
  paths, and don't list shared hosts such as q4cdn.com, which serve many companies.
- `previous_name` (optional): names before a rebrand, separated by `;`. FCRs filed under one are named
  after the current `name`, and previous names are searched too, since earlier reports used them.
- `fcr_pages` (optional): full URLs of pages that link to the company's FCRs, separated by `;`. The search
  task reads them directly (sources/pages.py), finding FCRs without spending a search.

Swap in a fuller register (such as the BMA's) by editing the seed; no code changes.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from fcr_curator.curation import EntityResolver, institution_key, on_domain, same_institution

REFERENCE_TABLE = "ref_bermuda_entities"  # The seed's table, in the project's schema


@dataclass(frozen=True)
class ReferenceEntity:
    """An insurer to search for and to name FCRs after."""

    name: str
    assets_bn_usd: float
    domains: tuple[str, ...] = ()
    previous_names: tuple[str, ...] = ()
    fcr_pages: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        """The current name, then any previous ones."""
        return (self.name, *self.previous_names)


def _split(value: str | None) -> tuple[str, ...]:
    return tuple(part.strip() for part in (value or "").split(";") if part.strip())


def _parse(row: Mapping, line: int) -> ReferenceEntity:
    name = (row.get("name") or "").strip()
    if not name:
        raise ValueError(f"Line {line}: no name")
    if None in row:  # csv.DictReader puts fields beyond the header under None
        raise ValueError(f"Line {line} ({name}): more fields than the header; use ';' between values, not ','")
    domains = tuple(domain.lower() for domain in _split(row.get("domains")))
    if any("/" in domain for domain in domains):
        raise ValueError(f"Line {line} ({name}): domains are hosts only, without a path: {domains}")
    fcr_pages = _split(row.get("fcr_pages"))
    if any(not page.startswith(("https://", "http://")) for page in fcr_pages):
        raise ValueError(f"Line {line} ({name}): fcr_pages must be full URLs starting https://: {fcr_pages}")
    try:
        return ReferenceEntity(name, float(row["assets_bn_usd"]), domains, _split(row.get("previous_name")), fcr_pages)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Line {line} ({name}): assets_bn_usd must be a number") from error


def entities_from_rows(rows: Iterable[Mapping]) -> list[ReferenceEntity]:
    """Return the entities in rows (from the seed table), largest assets first.

    Raises ValueError, naming the row, for a row with no name or no numeric assets, and for a name that
    repeats another once case, punctuation and legal suffixes are ignored (it would be searched twice).
    Rows are numbered as CSV lines are, so line 2 is the first company.
    """
    entities = [_parse(row, line) for line, row in enumerate(rows, start=2)]

    seen: dict[str, str] = {}
    for entity in entities:
        for name in entity.names:
            key = institution_key(name)
            if key in seen:
                raise ValueError(f"{name!r} repeats {seen[key]!r}")
            seen[key] = name

    return sorted(entities, key=lambda entity: entity.assets_bn_usd, reverse=True)


def entity_resolver(entities: list[ReferenceEntity]) -> EntityResolver:
    """Return a function that names a curated document's insurer as the list has it, or None.

    A document on one of an insurer's domains is that insurer's. Otherwise its institution or title is
    matched against each insurer's current and previous names (see curation.same_institution), and if
    several match, the longest, most specific name wins: "Arch Capital Group" over "Arch". Either way the
    document gets the current name.
    """

    def resolve(document: dict) -> str | None:
        for entity in entities:
            if any(on_domain(document.get("domain"), domain) for domain in entity.domains):
                return entity.name
        document_names = [document.get("institution") or "", document.get("title") or ""]
        matches = [
            (len(institution_key(name)), entity.name)
            for entity in entities
            for name in entity.names
            if any(same_institution(name, document_name) for document_name in document_names)
        ]
        return max(matches)[1] if matches else None

    return resolve
