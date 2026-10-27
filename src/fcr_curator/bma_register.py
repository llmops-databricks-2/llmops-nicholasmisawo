"""Turn a hand-copied extract of the BMA's regulated-entities list into the dbt seed of licensed insurers.

The BMA's robots.txt blocks automated access, so the pipeline never fetches bma.bm. Instead, someone
filters the regulated-entities page to the Insurance sector in a browser, copies the table into a text
file, and runs this module:

    uv run python -m fcr_curator.bma_register <extract.txt> dbt/seeds/bma_insurance_register.csv

The extract is the page's table pasted as text: a header row, then one record per licence, starting
with a "<name><TAB><sector><TAB>..." line and followed by its address and contact lines and labelled
lines such as "License/Registration Type: Class 4". Only the name, the licence types and any
liquidation status are kept: addresses and contact people aren't needed, and the extract itself
stays out of git. An insurer with several licences (a composite) has one record per licence, so
records are combined by name. Names the BMA lists in capitals are put in proper case; the seed keeps
the BMA's spelling too (bma_name), which other seeds join on.
"""

import argparse
import csv
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

INSURANCE_SECTOR = "Insurance"
RECORD_START = re.compile(r"^(?P<name>[^\t]+)\t(?P<sector>[^\t]+)\t")
LICENCE_TYPE = re.compile(r"License/Registration Type:\s*(?P<value>.+?)\s*$")
LIQUIDATION_STATUS = re.compile(r"Liquidation Status:\s*(?P<value>.+?)\s*$")
SEED_COLUMNS = ("name", "bma_name", "licence_types", "liquidation_status", "extracted_on")

# Words a capitalised name keeps lower case after its first word, and ones it keeps in capitals
LOWER_CASE_WORDS = frozenset({"a", "an", "and", "at", "for", "in", "of", "on", "the", "to"})
UPPER_CASE_WORDS = frozenset({"AG", "AIG", "AXA", "LLC", "NV", "PLC", "SA", "SE", "UK", "US", "USA", "XL"})
WORD = re.compile(r"[A-Za-z0-9']+")
INITIALS = re.compile(r"^(?:[A-Z]\.)+[A-Z]?\.?$")  # "R.V.I." or "P." stay as they are


@dataclass(frozen=True)
class LicensedInsurer:
    """One insurer on the BMA's list, with every licence it holds."""

    bma_name: str  # As the BMA lists it, e.g. "ARCH REINSURANCE LTD."
    licence_types: tuple[str, ...]  # Sorted, e.g. ("Class 4", "Long Term Class C") for a composite
    liquidation_status: str | None  # e.g. "PROVISIONAL LIQ"; None when not in liquidation

    @property
    def name(self) -> str:
        """The name to show: the BMA's, in proper case if the BMA wrote it in capitals."""
        return proper_case(self.bma_name)


def _proper_case_word(word: str, first: bool) -> str:
    if word in UPPER_CASE_WORDS or any(character.isdigit() for character in word):
        return word
    if not first and word.lower() in LOWER_CASE_WORDS:
        return word.lower()
    return word[0] + word[1:].lower()  # Not str.title(): "GEORGE'S" -> "George's", not "George'S"


def proper_case(name: str) -> str:
    """Return a name the BMA wrote in capitals in proper case; a name with any lower case is kept as is.

    "EVEREST REINSURANCE (BERMUDA), LTD." -> "Everest Reinsurance (Bermuda), Ltd.". Initials ("R.V.I.")
    and known acronyms ("AXA", "XL") stay in capitals. A brand's own casing, such as DaVinci, can't be
    guessed from capitals; fcr_entity_details.csv can give a display name instead.
    """
    if any(character.islower() for character in name):
        return name
    tokens = name.split(" ")
    cased = []
    for index, token in enumerate(tokens):
        if INITIALS.match(token.strip("(),&")):
            cased.append(token)
            continue
        cased.append(WORD.sub(lambda word, first=(index == 0): _proper_case_word(word.group(), first), token))
    return " ".join(cased)


def _records(lines: list[str]) -> Iterable[list[str]]:
    starts = [index for index, line in enumerate(lines) if RECORD_START.match(line)]
    for start, end in zip(starts, [*starts[1:], len(lines)], strict=True):
        yield lines[start:end]


def _labelled_values(record: list[str], label: re.Pattern) -> list[str]:
    return [match["value"] for line in record if (match := label.search(line))]


def parse_register(extract: str) -> list[LicensedInsurer]:
    """Return every Insurance-sector entity in the extract, one per name, sorted by name.

    Fails if an insurance record has no licence type, since that means the extract was cut or its
    layout changed, and a silently missing licence would hide an FCR filer.
    """
    lines = extract.replace("\r\n", "\n").split("\n")[1:]  # The first line is the table's header
    licences: dict[str, set[str]] = {}
    liquidations: dict[str, str] = {}

    for record in _records(lines):
        start = RECORD_START.match(record[0])
        if start["sector"].strip() != INSURANCE_SECTOR:
            continue
        name = start["name"].strip()
        types = _labelled_values(record, LICENCE_TYPE)
        if not types:
            raise ValueError(f"No licence type for {name!r}: the extract may be cut or its layout changed")
        licences.setdefault(name, set()).update(types)
        if status := _labelled_values(record, LIQUIDATION_STATUS):
            liquidations[name] = status[0]

    return [LicensedInsurer(name, tuple(sorted(licences[name])), liquidations.get(name)) for name in sorted(licences)]


def seed_rows(insurers: Iterable[LicensedInsurer], extracted_on: date) -> list[dict]:
    """Return the seed's rows: multi-value licence types joined with ';', like the other seeds."""
    return [
        {
            "name": insurer.name,
            "bma_name": insurer.bma_name,
            "licence_types": ";".join(insurer.licence_types),
            "liquidation_status": insurer.liquidation_status or "",
            "extracted_on": extracted_on.isoformat(),
        }
        for insurer in insurers
    ]


def main() -> None:
    """Write the seed CSV from an extract; the extract's date defaults to when the file was saved."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("extract", type=Path, help="The regulated-entities table, copied as text")
    parser.add_argument("seed", type=Path, help="Where to write the seed, e.g. dbt/seeds/bma_insurance_register.csv")
    parser.add_argument("--extracted-on", type=date.fromisoformat, help="When the list was copied (YYYY-MM-DD)")
    args = parser.parse_args()

    extracted_on = args.extracted_on or datetime.fromtimestamp(args.extract.stat().st_mtime, UTC).date()
    insurers = parse_register(args.extract.read_text(encoding="utf-8"))
    with args.seed.open("w", newline="", encoding="utf-8") as seed:
        writer = csv.DictWriter(seed, fieldnames=SEED_COLUMNS)
        writer.writeheader()
        writer.writerows(seed_rows(insurers, extracted_on))
    print(f"Wrote {len(insurers)} licensed insurers to {args.seed}")


if __name__ == "__main__":
    main()
