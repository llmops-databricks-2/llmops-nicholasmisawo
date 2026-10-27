"""The entity list: the insurers the search task looks for, and the names their FCRs get.

The list is dbt's stg_fcr_entities: the BMA-licensed insurers in search scope (dbt/seeds/
bma_licence_classes.csv), with what fcr_entity_details.csv adds. Each row has:

- `name`: the insurer's name in proper case, as searched for and as shown on its FCRs and chunks
  (`registered_name`)
- `business`: general, long_term or group. An insurer and its group may share a name, as the BMA lists
  "Nissan Global Reinsurance, Ltd." and "... (Group)"; an FCR either could have filed goes to the insurer
- `fcr_search_priority`: its licence class's search order (1 is Class 4), so when a run's search budget
  runs out, the less important classes wait; within a class, larger groups (`group_assets_bn_usd`) go first
- `domains`: hosts its group publishes on. Sister companies share them, so a domain narrows an FCR down
  to a group and the name tells the companies apart
- `aliases`: former names and brands. They're searched too, and an FCR under one gets the insurer's name
- `fcr_pages`: pages that link to its FCRs, which the search task reads without spending a search
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from fcr_curator.curation import (
    MIN_NAME_KEY_CHARS,
    EntityResolver,
    institution_key,
    on_domain,
    same_institution,
    title_subject,
)

REFERENCE_TABLE = "stg_fcr_entities"  # The entity list's view, in the project's schema


@dataclass(frozen=True)
class ReferenceEntity:
    """An insurer to search for and to name FCRs after."""

    name: str
    search_priority: int = 1
    is_group: bool = False
    group_assets_bn_usd: float | None = None
    domains: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    fcr_pages: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        """Its name, then its aliases."""
        return (self.name, *self.aliases)

    @property
    def search_order(self) -> tuple:
        """Most important licence class first, then the larger group, then by name."""
        return (self.search_priority, -(self.group_assets_bn_usd or 0.0), self.name)


def _values(value: str | Iterable[str] | None) -> tuple[str, ...]:
    parts = value.split(";") if isinstance(value, str) else (value or ())
    return tuple(part.strip() for part in parts if part and part.strip())


def _parse(row: Mapping) -> ReferenceEntity:
    name = (row.get("name") or "").strip()
    if not name:
        raise ValueError(f"An entity has no name: {dict(row)}")
    domains = tuple(domain.lower() for domain in _values(row.get("domains")))
    if any("/" in domain for domain in domains):
        raise ValueError(f"{name}: domains are hosts only, without a path: {domains}")
    fcr_pages = _values(row.get("fcr_pages"))
    if any(not page.startswith(("https://", "http://")) for page in fcr_pages):
        raise ValueError(f"{name}: fcr_pages must be full URLs starting https://: {fcr_pages}")
    assets = row.get("group_assets_bn_usd")
    return ReferenceEntity(
        name=name,
        search_priority=int(row.get("fcr_search_priority") or 1),
        is_group=row.get("business") == "group",
        group_assets_bn_usd=float(assets) if assets is not None else None,
        domains=domains,
        aliases=_values(row.get("aliases")),
        fcr_pages=fcr_pages,
    )


def entities_from_rows(rows: Iterable[Mapping]) -> list[ReferenceEntity]:
    """Return the entities in rows (from stg_fcr_entities), in search order.

    Raises ValueError for a row with no name, a domain with a path or a page that isn't a URL, and for
    a name or alias that repeats another once case, punctuation and legal suffixes are ignored (it would
    be searched twice, and an FCR under it couldn't be told apart). Only an insurer and a group may
    share a name, as the BMA lists some groups under their insurer's name.
    """
    entities = [_parse(row) for row in rows]

    seen: dict[str, tuple[str, ReferenceEntity]] = {}
    for entity in entities:
        for name in entity.names:
            key = institution_key(name)
            if key in seen and not _insurer_and_its_group(seen[key][1], entity):
                raise ValueError(f"{name!r} repeats {seen[key][0]!r}")
            seen.setdefault(key, (name, entity))

    return sorted(entities, key=lambda entity: entity.search_order)


def _insurer_and_its_group(entity: ReferenceEntity, other: ReferenceEntity) -> bool:
    return entity.is_group != other.is_group


def _names_it_or_shortens_it(name: str, document_name: str) -> bool:
    # "Everest Re" names "Everest Reinsurance (Bermuda), Ltd.", but "Partner Reinsurance Europe SE" is a
    # more specific company than "PartnerRe Group" (an Irish one, not on the list)
    key, document_key = institution_key(name), institution_key(document_name)
    return len(document_key) >= MIN_NAME_KEY_CHARS and key.startswith(document_key)


def _best_name_match(
    candidates: Iterable[ReferenceEntity], document_names: list[str], *, on_its_domain: bool
) -> str | None:
    # On the insurer's own website a name only has to start the same; elsewhere the document must name
    # the insurer or shorten its name, so a foreign subsidiary's report isn't taken for its group's.
    # An exact name beats a longer one that merely starts the same: "RenaissanceRe Group"'s own FCR is
    # the group's, not "Renaissance Reinsurance"'s. Among the rest, the longest, most specific wins, and
    # between an insurer and a group of the same name, the insurer.
    matches_name = same_institution if on_its_domain else _names_it_or_shortens_it
    document_keys = {institution_key(document_name) for document_name in document_names}
    matches = [
        (institution_key(name) in document_keys, len(institution_key(name)), not entity.is_group, entity.name)
        for entity in candidates
        for name in entity.names
        if any(matches_name(name, document_name) for document_name in document_names)
    ]
    return max(matches)[-1] if matches else None


def entity_resolver(entities: list[ReferenceEntity]) -> EntityResolver:
    """Return a function that names a curated document's insurer as the list has it, or None.

    A document on a group's domain is one of that group's companies: the one its institution or title
    names, or the only one if the group has just one. Elsewhere it's the listed insurer its institution
    (or the name after the report's in its title) names, in full or shortened ("Everest Re"); a longer,
    more specific name is another company, such as a group's foreign subsidiary. A document naming no listed insurer gets None, which leaves it out of
    the register, and so does one on a shared domain that doesn't say which sister company it is.
    """

    def resolve(document: dict) -> str | None:
        subject = title_subject(document.get("title"))
        document_names = [name for name in (document.get("institution"), subject, document.get("title")) if name]
        group_on_domain = [
            entity for entity in entities if any(on_domain(document.get("domain"), domain) for domain in entity.domains)
        ]
        if group_on_domain:
            if match := _best_name_match(group_on_domain, document_names, on_its_domain=True):
                return match
            if len(group_on_domain) == 1:
                return group_on_domain[0].name
        named = [name for name in (document.get("institution"), subject) if name]
        return _best_name_match(entities, named, on_its_domain=False)

    return resolve
