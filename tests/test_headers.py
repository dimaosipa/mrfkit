"""Header synonyms, wide-payer suffixes and unmapped-header suppression."""

from __future__ import annotations

import pytest

from mrfkit.headers import (
    SYNONYM_MAP,
    _is_unmapped_header_suppressed,
    map_header,
    normalize_header,
    parse_wide_payer_header,
)

# ---------------------------------------------------------------------------
# Synonyms seen in real files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("header,expected", [
    # CVH ships `standard_charge | negotiated_percent` (singular, spaces around the pipe)
    ("standard_charge | negotiated_percent", "negotiated_percentage"),
    ("standard_charge|negotiated_percent", "negotiated_percentage"),
    ("negotiated_percent", "negotiated_percentage"),
    ("standard_charge | negotiated_percentage", "negotiated_percentage"),
    ("negotiated_percentage", "negotiated_percentage"),
    # Ascension / CVH shoppables triplet
    ("Cash_Price_Estimate", "discounted_cash_price"),
    ("Gross_Charges_Estimate", "gross_charge"),
    ("Insurance_Estimate", "estimated_amount"),
    ("cash_price_estimate", "discounted_cash_price"),
    ("gross_charges_estimate", "gross_charge"),
    ("insurance_estimate", "estimated_amount"),
    # Bear Lake Memorial chargemaster legacy headers
    ("IVCPTCD", "code"),
    ("ivcptcd", "code"),
    ("IvCptCd", "code"),
    ("IVDESC", "description"),
    ("IVPRICE1", "gross_charge"),
    # Middlesex Hospital
    ("CPT/HCPCS Code", "code"),
    ("Gross Charge per CDM", "gross_charge"),
    ("Rate Methodology", "methodology"),
    ("Billing Code Description", "description"),
    # Atrium sections
    ("Plan(s)", "plan_name"),
    ("Product", "plan_name"),
    ("Codes", "code"),
    ("Min Negotiated Charge", "min_negotiated_rate"),
    ("Max Negotiated Charge", "max_negotiated_rate"),
    (" Minimum Reimbursement ", "min_negotiated_rate"),
    (" Maximum Reimbursement ", "max_negotiated_rate"),
    ("Procedure Modifier", "modifiers"),
    ("Price", "gross_charge"),
    ("Procedure External ID", "code"),
])
def test_real_world_synonyms(header, expected):
    assert map_header(header)[1] == expected


def test_bear_lake_normalized_form():
    assert map_header("IVCPTCD") == ("ivcptcd", "code")


def test_ivnum_stays_unmapped():
    """IVNUM is an internal item id, not a canonical field."""
    assert map_header("IVNUM")[1] is None


@pytest.mark.parametrize("header,expected", [
    ("Gross_Charges_Estimate", "gross_charge"),
    ("Cash_Price_Estimate", "discounted_cash_price"),
    ("Insurance_Estimate", "estimated_amount"),
    ("standard_charge | estimated_discounted_cash", "discounted_cash_price"),
])
def test_synonym_map_lookup_by_normalized_header(header, expected):
    assert SYNONYM_MAP.get(normalize_header(header)) == expected


@pytest.mark.parametrize("header,expected_payer", [
    ("standard_charge|Aetna|negotiated_percent", "Aetna"),
    ("standard_charge|Blue Cross|PPO|negotiated_percent", "Blue Cross"),
    ("standard_charge|Aetna|negotiated_percentage", "Aetna"),
])
def test_wide_payer_singular_negotiated_percent(header, expected_payer):
    parsed = parse_wide_payer_header(header)
    assert parsed is not None
    payer, _plan, field = parsed
    assert payer == expected_payer
    assert field == "negotiated_percentage"


# ---------------------------------------------------------------------------
# extra_synonyms
# ---------------------------------------------------------------------------


def test_extra_synonyms_map_unknown_headers():
    extra = {"average_charge_per_unit": "gross_charge"}
    assert map_header("Average Charge Per Unit") == ("average_charge_per_unit", None)
    assert map_header("Average Charge Per Unit", extra_synonyms=extra) == (
        "average_charge_per_unit", "gross_charge",
    )


def test_extra_synonyms_never_override_built_in():
    assert map_header("Gross Charge", extra_synonyms={"gross_charge": "description"})[1] == "gross_charge"


def test_extra_synonyms_ignore_non_canonical_targets():
    assert map_header("Mystery", extra_synonyms={"mystery": "not_a_field"})[1] is None


# ---------------------------------------------------------------------------
# Unmapped-header suppression
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw_header", [
    "Item#", "Item #", "Item No", "Item No.", "item", "item_no",
    "code[2]", "code[3]", "code[4]", "code[5]", "code[6]", "code[7]", "code[8]", "code[9]",
    "code[10]", "code[2]type", "code[4]type", "code[9]type",
    "additional_generic_notes_payor_reimbursement",
    "Item ID", "item id", "ITEM ID", " Item ID ",
    "Dept #", "dept #", "DEPT #",
    "Dept Name", "dept name", "DEPT NAME",
])
def test_metadata_columns_are_suppressed(raw_header):
    assert _is_unmapped_header_suppressed(normalize_header(raw_header))


@pytest.mark.parametrize("raw_header", [
    "code", "billing_code", "code_type", "items",
    # A primary code column labelled code1 / code[1] is a real mapping
    # problem to surface, not silence.
    "code1", "code[1]", "code1type", "code[1]type",
    # Real price/value columns
    "Average Charge Per Unit", "Amount", "possible_amount",
    "Gross Charge", "Discounted Cash Price",
])
def test_real_columns_are_not_suppressed(raw_header):
    assert not _is_unmapped_header_suppressed(normalize_header(raw_header))


def test_item_and_bracket_normalization():
    """The suppression rules depend on these normalized forms."""
    assert normalize_header("Item#") == "item"
    assert normalize_header("Item #") == "item"
    assert normalize_header("Item No") == "item_no"
    assert normalize_header("Item No.") == "item_no"
    assert normalize_header("code[4]") == "code4"
    assert normalize_header("code[4]type") == "code4type"
    assert normalize_header("code[10]") == "code10"


@pytest.mark.parametrize("header", [
    'op_price', 'ip_xr_detail', 'facility_id', 'cdm', 'department', 'tabname',
    'last_updated', '', 'count_of_compared_rates', 'additional_generic_notes1',
    'revenue_code', 'allowed_amounts', 'billing_code_type_version',
    'financial_aid_policy', 'rev', 'charge_number', 'ivnum', 'mins_per_unit',
    # patterns
    'anything_internal', 'foo_bar_internal', 'widget_internal',
    'column1', 'column42', 'column999', '1', '42', '123456',
])
def test_suppressed_normalized_headers(header):
    assert _is_unmapped_header_suppressed(header)


@pytest.mark.parametrize("header", [
    'code', 'description', 'negotiated_rate', 'gross_charge',
    'column', 'internal', 'internally', 'column_foo',
    'foo_column1', 'internal_code', 'last_updated_at',
])
def test_not_suppressed_normalized_headers(header):
    assert not _is_unmapped_header_suppressed(header)
