"""Tests for reading the entity list and naming FCRs after the insurers on it."""

import pytest

from fcr_curator.reference import ReferenceEntity, entities_from_rows, entity_resolver

RENRE_DOMAIN = "renre.com"
RENAISSANCE_RE = ReferenceEntity("Renaissance Reinsurance Ltd.", search_priority=1, domains=(RENRE_DOMAIN,))
RENRE_EUROPE = ReferenceEntity("RenaissanceRe Europe AG", search_priority=3, domains=(RENRE_DOMAIN,))
RENRE_GROUP = ReferenceEntity("RenaissanceRe Group", search_priority=4, is_group=True, domains=(RENRE_DOMAIN,))
ANTARES = ReferenceEntity("Antares Reinsurance Company Limited", domains=("antaresglobal.com",), aliases=("Qatar Re",))
WILTON = ReferenceEntity("Wilton Re International Limited", search_priority=2)
LIST = [RENAISSANCE_RE, RENRE_EUROPE, RENRE_GROUP, ANTARES, WILTON]


def row(name: str, **columns: object) -> dict:
    """Build a stg_fcr_entities row, as Spark returns it: arrays as lists, other columns as given."""
    defaults = {"fcr_search_priority": 1, "group_assets_bn_usd": None, "domains": [], "aliases": [], "fcr_pages": []}
    return {"name": name, **defaults, **columns}


def resolve(**document: str) -> str | None:
    """Name a curated document's insurer with the test list."""
    return entity_resolver(LIST)(document)


# --- Reading the list ---------------------------------------------------------------------------


def test_entities_come_in_search_order_class_first_then_larger_group_then_name() -> None:
    """Class 4 before Long Term E; within a class, the larger group first; then alphabetically."""
    entities = entities_from_rows(
        [
            row("Wilton Re International Limited", fcr_search_priority=2, group_assets_bn_usd=40.0),
            row("Small Re Ltd.", fcr_search_priority=1),
            row("Arch Reinsurance Ltd.", fcr_search_priority=1, group_assets_bn_usd=28.7),
            row("Argo Re Ltd.", fcr_search_priority=1),
        ]
    )

    assert [entity.name for entity in entities] == [
        "Arch Reinsurance Ltd.",
        "Argo Re Ltd.",
        "Small Re Ltd.",
        "Wilton Re International Limited",
    ]


def test_aliases_are_searched_names_and_domains_are_lower_cased() -> None:
    """An entity's names are its own then its aliases; domains match whatever case a URL uses."""
    [entity] = entities_from_rows([row("Somers Re Ltd.", domains=["SomersGroup.com"], aliases=["Watford Re"])])

    assert entity.names == ("Somers Re Ltd.", "Watford Re")
    assert entity.domains == ("somersgroup.com",)


def test_multi_value_columns_may_also_be_semicolon_separated_text() -> None:
    """Rows typed by hand or read from a seed separate values with ';', and stray spaces don't matter."""
    [entity] = entities_from_rows([row("Example Re Ltd.", domains=" example-re.com ; example-re.bm ", aliases="")])

    assert (entity.domains, entity.aliases) == (("example-re.com", "example-re.bm"), ())


@pytest.mark.parametrize(
    ("bad_row", "message"),
    [
        (row(""), "no name"),
        (row("Example Re Ltd.", domains=["example-re.com/fcrs"]), "hosts only"),
        (row("Example Re Ltd.", fcr_pages=["example-re.com/fcrs"]), "full URLs"),
    ],
)
def test_an_unusable_row_fails_naming_the_problem(bad_row: dict, message: str) -> None:
    """A nameless row, a domain with a path, or a page that isn't a URL would silently never match."""
    with pytest.raises(ValueError, match=message):
        entities_from_rows([bad_row])


def test_an_alias_that_repeats_another_insurers_name_fails() -> None:
    """Two insurers under one name would be searched twice, and an FCR under it couldn't be told apart."""
    with pytest.raises(ValueError, match="repeats"):
        entities_from_rows([row("Antares Reinsurance Company Limited", aliases=["Qatar Re"]), row("Qatar Re Ltd.")])


def test_an_insurer_and_its_group_may_share_a_name() -> None:
    """The BMA lists some groups under their insurer's name plus "(Group)"; that isn't a duplicate."""
    entities = entities_from_rows(
        [
            row("Nissan Global Reinsurance, Ltd.", business="general"),
            row("Nissan Global Reinsurance, Ltd. (Group)", fcr_search_priority=4, business="group"),
        ]
    )

    assert entity_resolver(entities)({"institution": "Nissan Global Reinsurance"}) == "Nissan Global Reinsurance, Ltd."


# --- Naming FCRs --------------------------------------------------------------------------------


def test_sister_companies_on_one_domain_are_told_apart_by_name() -> None:
    """A group's companies share its website, so the FCR's own name decides whose it is."""
    assert resolve(domain=RENRE_DOMAIN, institution="RenaissanceRe Europe AG") == "RenaissanceRe Europe AG"
    assert resolve(domain=RENRE_DOMAIN, institution="Renaissance Reinsurance Ltd") == "Renaissance Reinsurance Ltd."


def test_a_groups_own_fcr_is_the_groups_not_its_largest_subsidiarys() -> None:
    """An exact name beats a longer one that merely starts the same: the group's FCR is the group's."""
    assert resolve(domain=RENRE_DOMAIN, institution="RenaissanceRe Group") == "RenaissanceRe Group"


def test_an_fcr_on_a_shared_domain_that_names_no_sister_is_left_out() -> None:
    """Rather than guess which sister company filed it, an unnamed FCR on a shared domain gets no name."""
    assert resolve(domain=RENRE_DOMAIN, institution="Annual Disclosures") is None


def test_an_fcr_on_a_single_companys_domain_is_that_companys() -> None:
    """When only one listed insurer uses a domain, the domain alone is enough."""
    assert resolve(domain="www.antaresglobal.com", title="View") == "Antares Reinsurance Company Limited"


def test_an_fcr_under_an_alias_gets_the_insurers_name() -> None:
    """A report filed under a former name is named as the insurer is now."""
    assert resolve(domain="q4cdn.com", institution="Qatar Re") == "Antares Reinsurance Company Limited"


def test_an_fcr_off_any_listed_domain_is_matched_by_its_institutions_name() -> None:
    """A report hosted elsewhere is still the insurer's if it names it, in full or shortened."""
    assert resolve(domain="cdn.example.com", institution="Wilton Re International Ltd") == WILTON.name
    assert resolve(domain="cdn.example.com", institution="Wilton Re") == WILTON.name


def test_a_foreign_subsidiarys_report_elsewhere_isnt_taken_for_its_groups() -> None:
    """Off the group's website, a longer name is a more specific company, such as an Irish subsidiary."""
    assert resolve(domain="centralbank.ie", institution="Antares Reinsurance Company Europe SE") is None


def test_an_fcr_naming_no_listed_insurer_gets_no_name() -> None:
    """Curation leaves out FCRs of insurers not on the list."""
    assert resolve(domain="steamshipmutual.com", institution="Steamship Mutual") is None
