"""Tests for the reference list of (re)insurers: loading it, and naming FCRs after its entities."""

import csv
from pathlib import Path

import pytest

from fcr_curator.reference import ReferenceEntity, entities_from_rows, entity_resolver

SEED_FILE = Path(__file__).resolve().parents[1] / "dbt/seeds/bermuda_entities.csv"


def load_reference_entities(path: Path) -> list[ReferenceEntity]:
    """Load entities from a seed CSV, as the pipeline loads the seed table's rows."""
    with open(path, newline="", encoding="utf-8") as file:
        return entities_from_rows(csv.DictReader(file))


def csv_file(tmp_path: Path, rows: str) -> Path:
    """Write a reference CSV with the standard header and the given rows."""
    path = tmp_path / "entities.csv"
    path.write_text(f"name,assets_bn_usd\n{rows}")
    return path


def test_entities_come_largest_assets_first_whatever_the_file_order() -> None:
    """Search priority follows assets, so the biggest insurers are searched before the budget runs out."""
    entities = load_reference_entities(SEED_FILE)

    assets = [entity.assets_bn_usd for entity in entities]
    assert assets == sorted(assets, reverse=True)


def test_file_order_does_not_set_priority(tmp_path: Path) -> None:
    """A smaller insurer listed first still comes after a larger one."""
    entities = load_reference_entities(csv_file(tmp_path, "Small Re,1.5\nLarge Re,30\n"))

    assert [entity.name for entity in entities] == ["Large Re", "Small Re"]


def test_every_row_of_the_shipped_list_loads() -> None:
    """The seed list loads in full (one entity per data row), starting with the largest."""
    path = SEED_FILE
    data_rows = len(path.read_text().strip().splitlines()) - 1

    entities = load_reference_entities(path)

    assert len(entities) == data_rows
    assert (entities[0].name, entities[0].assets_bn_usd) == ("RenaissanceRe", 30.5)


@pytest.mark.parametrize(
    ("rows", "message"),
    [(" ,3.0\n", "Line 2: no name"), ("Example Re,lots\n", "Line 2 .*assets_bn_usd")],
)
def test_bad_rows_fail_naming_the_line(tmp_path: Path, rows: str, message: str) -> None:
    """A row with no name or non-numeric assets is an error that says where it is."""
    with pytest.raises(ValueError, match=message):
        load_reference_entities(csv_file(tmp_path, rows))


def test_a_name_repeated_in_another_form_fails(tmp_path: Path) -> None:
    """'Example Re Ltd.' and 'example re' are one insurer, which would otherwise be searched twice."""
    with pytest.raises(ValueError, match="repeats"):
        load_reference_entities(csv_file(tmp_path, "Example Re Ltd.,2\nexample re,3\n"))


def test_domains_are_optional_and_split_on_semicolons(tmp_path: Path) -> None:
    """A domains column lists an insurer's websites; leaving it out or blank means none."""
    path = tmp_path / "entities.csv"
    path.write_text("name,assets_bn_usd,domains\nExample Re,2, example-re.com ; Example-Re.bm \nOther Re,1,\n")

    entities = load_reference_entities(path)

    assert [entity.domains for entity in entities] == [("example-re.com", "example-re.bm"), ()]


ENTITIES = [
    ReferenceEntity("Arch Capital Group", 28.7, ("archgroup.com",)),
    ReferenceEntity("Arch", 1.0),
    ReferenceEntity("Wilton Re", 5.0, ("wiltonre.bm",)),
]
RESOLVE = entity_resolver(ENTITIES)


def document(institution: str | None = None, domain: str = "example.com", title: str = "FCR 2024") -> dict:
    """Build the curated fields the resolver reads."""
    return {"institution": institution, "domain": domain, "title": title}


def test_resolver_names_a_document_on_an_insurers_domain_whatever_its_title() -> None:
    """A messy title name is overridden by the domain the FCR is published on, subdomains included."""
    assert RESOLVE(document("wiltonre.bm", domain="wiltonre.bm")) == "Wilton Re"
    assert RESOLVE(document("Group", domain="investors.archgroup.com")) == "Arch Capital Group"


def test_resolver_falls_back_to_the_name_and_prefers_the_most_specific() -> None:
    """Off any listed domain, the name decides, and the longest matching reference name wins."""
    assert RESOLVE(document("Arch Capital Group Ltd.")) == "Arch Capital Group"
    assert RESOLVE(document("Arch Reinsurance Ltd.")) == "Arch"


def test_resolver_reads_the_title_when_the_institution_is_unhelpful() -> None:
    """An FCR whose institution came out generic is still named from a title that starts with the insurer."""
    assert RESOLVE(document("Group", title="Wilton Re Ltd Financial Condition Report 2024")) == "Wilton Re"


def test_resolver_leaves_insurers_not_on_the_list_unnamed() -> None:
    """No domain or name match means no registered name, rather than a guess."""
    assert RESOLVE(document("Steamship Mutual", domain="steamshipmutual.com")) is None


def test_previous_names_are_read_and_an_fcr_under_one_gets_the_current_name(tmp_path: Path) -> None:
    """An FCR filed before a rebrand is named after the company's current name."""
    path = tmp_path / "entities.csv"
    path.write_text("name,assets_bn_usd,domains,previous_name\nAntares Re,10.5,,Qatar Re; Qatar Reinsurance\n")

    [entity] = load_reference_entities(path)

    assert entity.names == ("Antares Re", "Qatar Re", "Qatar Reinsurance")
    assert entity_resolver([entity])(document("Qatar Re Ltd.")) == "Antares Re"


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ("Fidelis,16.1,pelagos.com, Pelagos,extra\n", "more fields than the header"),
        ("Antares Re,10.5,antaresglobal.com/antares-re,\n", "hosts only"),
    ],
)
def test_malformed_columns_fail_naming_the_line(tmp_path: Path, rows: str, message: str) -> None:
    """A ',' used where ';' belongs, or a domain with a path, is an error rather than silently ignored."""
    path = tmp_path / "entities.csv"
    path.write_text(f"name,assets_bn_usd,domains,previous_name\n{rows}")

    with pytest.raises(ValueError, match=f"Line 2 .*{message}"):
        load_reference_entities(path)


def test_a_previous_name_that_repeats_another_entity_fails(tmp_path: Path) -> None:
    """A previous name already used by another row would name the FCR after the wrong company."""
    path = tmp_path / "entities.csv"
    path.write_text("name,assets_bn_usd,domains,previous_name\nAntares Re,10.5,,Qatar Re\nQatar Re,1,,\n")

    with pytest.raises(ValueError, match="repeats"):
        load_reference_entities(path)


def test_rows_from_the_seed_table_load_like_the_csv() -> None:
    """Table rows give empty cells as None and assets as numbers; they load the same as CSV rows."""
    rows = [
        {"name": "Small Re", "assets_bn_usd": 1.5, "domains": None, "previous_name": None, "fcr_pages": None},
        {
            "name": "Large Re",
            "assets_bn_usd": 30.0,
            "domains": "large-re.com",
            "previous_name": None,
            "fcr_pages": None,
        },
    ]

    entities = entities_from_rows(rows)

    assert [(entity.name, entity.domains) for entity in entities] == [("Large Re", ("large-re.com",)), ("Small Re", ())]
