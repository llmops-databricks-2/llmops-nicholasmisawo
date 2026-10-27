"""Tests for turning a hand-copied BMA regulated-entities extract into the licensed-insurer seed."""

from datetime import date

import pytest

from fcr_curator.bma_register import LicensedInsurer, parse_register, proper_case, seed_rows

HEADER = "Company Name\tSector\tAddress\tContact Information\tDetails"


def record(name: str, sector: str, *details: str) -> str:
    """Build one record as the page's table pastes: name, sector and address, contacts, then labelled details."""
    return "\n".join(
        [
            f"{name}\t{sector}\tPrincipal Address",
            "1 Front Street",
            "Hamilton HM 11",
            "Bermuda\t",
            "Contact Person: A Person",
            "---",
            *details,
        ]
    )


def extract(*records: str) -> str:
    """Join records under the header, with the Windows line endings a copied table has."""
    return "\r\n".join([HEADER, *records])


def test_an_insurer_is_read_with_its_licence_type() -> None:
    """Name and licence type are kept; addresses and contact people are not."""
    insurers = parse_register(extract(record("Example Re Ltd.", "Insurance", "License/Registration Type: Class 4")))

    assert insurers == [LicensedInsurer("Example Re Ltd.", ("Class 4",), None)]


def test_a_composite_with_one_record_per_licence_becomes_one_insurer_with_both() -> None:
    """The list repeats an insurer once per licence; the seed has one row holding every licence, sorted."""
    insurers = parse_register(
        extract(
            record("Example Life Ltd.", "Insurance", "License/Registration Type: Long Term Class C"),
            record("Example Life Ltd.", "Insurance", "License/Registration Type: Class 3A"),
        )
    )

    assert insurers == [LicensedInsurer("Example Life Ltd.", ("Class 3A", "Long Term Class C"), None)]


def test_other_sectors_are_left_out() -> None:
    """Only the Insurance sector is kept, even if the copy included other sectors."""
    insurers = parse_register(
        extract(
            record("Example Trust Ltd.", "Trust Business", "License/Registration Type: Licensed Trust"),
            record("Example Re Ltd.", "Insurance", "License/Registration Type: Class 4"),
        )
    )

    assert [insurer.name for insurer in insurers] == ["Example Re Ltd."]


def test_a_liquidation_status_is_kept() -> None:
    """An insurer in liquidation is flagged, so its FCRs aren't searched for."""
    [insurer] = parse_register(
        extract(
            record(
                "Wound Up Re Ltd.",
                "Insurance",
                "License/Registration Type: Class 3B",
                "Liquidation Status: PROVISIONAL LIQ",
            )
        )
    )

    assert insurer.liquidation_status == "PROVISIONAL LIQ"


def test_an_insurance_record_without_a_licence_type_fails_naming_it() -> None:
    """A record missing its licence means a cut or changed extract, so it stops the run instead of hiding a filer."""
    with pytest.raises(ValueError, match="Cut Off Re"):
        parse_register(extract(record("Cut Off Re Ltd.", "Insurance")))


def test_seed_rows_join_licence_types_and_leave_no_liquidation_empty() -> None:
    """The seed uses ';' between values like the other seeds, and an empty status when not in liquidation."""
    rows = seed_rows(
        [LicensedInsurer("Example Life Ltd.", ("Class 3A", "Long Term Class C"), None)], date(2026, 10, 27)
    )

    assert rows == [
        {
            "name": "Example Life Ltd.",
            "bma_name": "Example Life Ltd.",
            "licence_types": "Class 3A;Long Term Class C",
            "liquidation_status": "",
            "extracted_on": "2026-10-27",
        }
    ]


def test_a_name_in_capitals_gets_proper_case_and_keeps_the_bmas_spelling() -> None:
    """The seed shows a clean name, and keeps the BMA's spelling to join on."""
    [insurer] = parse_register(
        extract(record("EVEREST REINSURANCE (BERMUDA), LTD.", "Insurance", "License/Registration Type: Class 4"))
    )

    assert (insurer.name, insurer.bma_name) == (
        "Everest Reinsurance (Bermuda), Ltd.",
        "EVEREST REINSURANCE (BERMUDA), LTD.",
    )


@pytest.mark.parametrize(
    ("bma_name", "expected"),
    [
        ("ST. GEORGE'S LIMITED", "St. George's Limited"),  # An apostrophe doesn't start a word
        ("UNDERWRITERS AT LLOYDS", "Underwriters at Lloyds"),  # Small words stay lower case after the first
        ("GARD P. & I. (BERMUDA) LTD.", "Gard P. & I. (Bermuda) Ltd."),  # Initials stay in capitals
        ("AXA XL RE LTD", "AXA XL Re Ltd"),  # So do known acronyms
    ],
)
def test_proper_case_handles_apostrophes_small_words_initials_and_acronyms(bma_name: str, expected: str) -> None:
    """Proper case reads like the company's own name, not str.title()'s."""
    assert proper_case(bma_name) == expected


def test_a_name_with_lower_case_is_left_as_the_bma_wrote_it() -> None:
    """Mixed case is the company's own styling, such as PartnerRe, so it isn't touched."""
    assert proper_case("PartnerRe Insurance Solutions Bermuda Ltd.") == "PartnerRe Insurance Solutions Bermuda Ltd."
