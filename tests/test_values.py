"""Methodology, NPI and null-rate helpers."""

from __future__ import annotations

import pytest

from mrfkit.values import (
    _SIMPLE_WIDE_THRESHOLD,
    _clean_methodology,
    _column_looks_like_payer_rates,
    _is_null_rate_token,
    _looks_like_dollar_value,
    _sanitize_npi,
    hoist_numeric_methodology,
    normalize_methodology,
)

# ---------------------------------------------------------------------------
# normalize_methodology
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("Percent of Total Billed Charges", "percent_of_charge"),
    ("PERCENT OF TOTAL BILLED CHARGES", "percent_of_charge"),
    ("percent of total billed charges", "percent_of_charge"),
    ("Percent of Billed Charges", "percent_of_charge"),
    ("Percentage of Billed Charges", "percent_of_charge"),
    ("Percent of Charges", "percent_of_charge"),
    ("Percent of Charge", "percent_of_charge"),
    ("Percent Charge", "percent_of_charge"),
    ("Pct of Charges", "percent_of_charge"),
    ("% of Charges", "percent_of_charge"),
    ("% of Billed Charges", "percent_of_charge"),
    ("Fee Schedule", "fee_schedule"),
    ("fee schedule", "fee_schedule"),
    ("FEE SCHEDULE", "fee_schedule"),
    ("Fee-Schedule", "fee_schedule"),
    ("Fee for Service", "fee_schedule"),
    ("Case Rate", "case_rate"),
    ("case rate", "case_rate"),
    ("Case-Rate", "case_rate"),
    ("Case Based", "case_rate"),
    ("Per Diem", "per_diem"),
    ("per diem", "per_diem"),
    ("Per-Diem", "per_diem"),
    ("Perdiem", "per_diem"),
    ("Other", "other"),
    ("other", "other"),
    ("OTHER", "other"),
])
def test_known_methodology_types(raw, expected):
    assert normalize_methodology(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_null_and_empty_methodology(raw):
    assert normalize_methodology(raw) is None


def test_methodology_whitespace_stripped():
    assert normalize_methodology("  Fee Schedule  ") == "fee_schedule"
    assert normalize_methodology("\tCase Rate\n") == "case_rate"


def test_unknown_methodology_returns_other():
    assert normalize_methodology("some unknown method") == "other"
    assert normalize_methodology("DRG Flat Rate") == "other"


@pytest.mark.parametrize("raw,expected", [
    ("Custom Percent of Something", "percent_of_charge"),
    ("modified percent arrangement", "percent_of_charge"),
    ("20% of charges or something", "percent_of_charge"),
    ("Custom Fee Schedule Plus", "fee_schedule"),
    ("Modified Case Rate Agreement", "case_rate"),
    ("Custom Per Diem Plan", "per_diem"),
])
def test_methodology_substring_fallback(raw, expected):
    assert normalize_methodology(raw) == expected


@pytest.mark.parametrize("raw", [
    "48.41", "77.11", "93.15", "99.09", "38.52",
    "1", "50", "100", "100.00", "  77.46  ",
])
def test_percent_shaped_numbers(raw):
    assert normalize_methodology(raw) == "percent_of_charge"


def test_sub_one_numbers_still_type_as_percent():
    """Fraction-form values (0.43 == 43%) are still percentages by TYPE."""
    assert normalize_methodology("0.43") == "percent_of_charge"
    assert normalize_methodology("0.72") == "percent_of_charge"


@pytest.mark.parametrize("raw", [
    "0", "0.0", "00", "0.00",
    "101", "150", "432.81",
    "18350.15", "142031.00", "178799",
])
def test_zero_and_over_100_stay_other(raw):
    assert normalize_methodology(raw) == "other"


@pytest.mark.parametrize("raw", ["-5", "1,250", "$500", "48.41%"])
def test_non_bare_numbers_unaffected(raw):
    assert normalize_methodology(raw) == "other"


# ---------------------------------------------------------------------------
# hoist_numeric_methodology
# ---------------------------------------------------------------------------


def test_zero_label_dropped_value_untouched():
    assert hoist_numeric_methodology("0", "other", None, None, None) == (None, "other", None)
    assert hoist_numeric_methodology("0.0", "other", None, None, None) == (None, "other", None)
    assert hoist_numeric_methodology("0", "other", 250.0, None, None) == (None, "other", None)


@pytest.mark.parametrize("raw,expected_pct", [
    ("48.41", 48.41), ("1", 1.0), ("100", 100.0), ("77.11", 77.11), ("  50  ", 50.0),
])
def test_percent_hoisted_on_clean_row(raw, expected_pct):
    meth, mtype, pct = hoist_numeric_methodology(raw, "percent_of_charge", None, None, None)
    assert meth is None
    assert mtype == "percent_of_charge"
    assert pct == pytest.approx(expected_pct)


def test_not_hoisted_when_rate_present():
    assert hoist_numeric_methodology("48.41", "other", 100.0, None, None) == ("48.41", "other", None)


def test_not_hoisted_when_percentage_present():
    assert hoist_numeric_methodology("48.41", "percent_of_charge", None, 30.0, None) \
        == ("48.41", "percent_of_charge", 30.0)


def test_not_hoisted_when_algorithm_present():
    assert hoist_numeric_methodology("48.41", "other", None, None, "Percent of Charges") \
        == ("48.41", "other", None)


@pytest.mark.parametrize("raw", ["0.43", "0.72", "150", "18350.15", "142031.00"])
def test_ambiguous_left_intact_for_review(raw):
    assert hoist_numeric_methodology(raw, "other", None, None, None) == (raw, "other", None)


@pytest.mark.parametrize("raw", [None, "Fee Schedule", "150%", "1,250", "$500", ""])
def test_hoist_ignores_non_bare_numbers(raw):
    assert hoist_numeric_methodology(raw, "fee_schedule", None, None, None) == (raw, "fee_schedule", None)


# ---------------------------------------------------------------------------
# _clean_methodology
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dropped", [
    "",
    "   ",
    # per-claim audit notes: unbounded via the embedded claim id
    "Re-evaluated: HLB.35878101; Using historical claim(s); Scope: All "
    "Financial Classes; Case Rate",
    "historical claim(s) weighted average",
    "see https://example.com/methodology",
    "Rate per schedule at https://www.cms.gov/x",
])
def test_methodology_notes_urls_and_blank_dropped(dropped):
    assert _clean_methodology(dropped) is None


@pytest.mark.parametrize("kept", [
    "fee schedule", "Percent of total billed charges", "case rate", "per diem",
    "APR-DRG", "café",
    # prices / percentages put directly in the methodology field are kept
    "3865", "573.77", "48.41", "18350.15", "142031.00", "48%",
])
def test_real_and_numeric_methodology_preserved(kept):
    assert _clean_methodology(kept) == kept


def test_clean_methodology_none():
    assert _clean_methodology(None) is None


# ---------------------------------------------------------------------------
# _sanitize_npi
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("1023187416", "1023187416"),
    ("1023187416| 1124017140| 1154415024", "1023187416"),
    ("1023187416|1124017140", "1023187416"),
    (None, None),
    ("", None),
    ("   ", None),
    ("not-an-npi", None),
    ("12345", None),
    ("bad| 1124017140", "1124017140"),
    ("short|also-short|toolongvalue1", None),
    ('"1023187416"', "1023187416"),
    ("NPI:1023187416", "1023187416"),
])
def test_sanitize_npi(raw, expected):
    assert _sanitize_npi(raw) == expected


# ---------------------------------------------------------------------------
# Null-rate tokens and the simple wide-format payer detector
# ---------------------------------------------------------------------------


def _na_heavy_payer_column(n_na=67, n_dollar=32, n_zero=1):
    """Distribution measured on a real wide file: ~2/3 N/A, ~1/3 dollars."""
    return ['N/A'] * n_na + ['10045.74'] * n_dollar + ['0'] * n_zero


@pytest.mark.parametrize('value', [
    'N/A', 'n/a', ' N/A ', 'NA', '#N/A', 'null', 'NULL', 'None', 'nil', '-', '--', '\u2014',
])
def test_null_rate_token_recognized(value):
    assert _is_null_rate_token(value)


@pytest.mark.parametrize('value', ['0', '0.00', '10045.74', '$1,234.50', 'Aetna', 'Packaged', 'CDM'])
def test_not_a_null_rate_token(value):
    assert not _is_null_rate_token(value)


@pytest.mark.parametrize('value', ['0', '0.00', '123.45', '$10,002', '$-', 'Packaged'])
def test_dollar_like(value):
    assert _looks_like_dollar_value(value)


@pytest.mark.parametrize('value', ['N/A', 'CDM', 'IP', 'Aetna', ''])
def test_not_dollar_like(value):
    """N/A is still NOT a dollar value: the detector excludes it instead."""
    assert not _looks_like_dollar_value(value)


def test_na_heavy_payer_column_is_detected():
    assert _column_looks_like_payer_rates(_na_heavy_payer_column())


def test_counting_na_would_miss_the_column():
    col = _na_heavy_payer_column()
    ratio = sum(_looks_like_dollar_value(v) for v in col) / len(col)
    assert ratio < _SIMPLE_WIDE_THRESHOLD


def test_clean_payer_column_detected():
    assert _column_looks_like_payer_rates(['1234.50', '0', '$99.00'] * 10)


@pytest.mark.parametrize('values', [
    ['CDM', 'DRG'] * 25,
    ['IP', 'OP'] * 25,
    ['7/10/2023'] * 50,
    ['Charge Description ' + str(i) for i in range(50)],
])
def test_metadata_columns_not_detected(values):
    assert not _column_looks_like_payer_rates(values)


def test_all_null_column_not_detected():
    assert not _column_looks_like_payer_rates(['N/A'] * 50)
    assert not _column_looks_like_payer_rates([None, '', '  '] * 10)


def test_mostly_na_with_a_few_dollars_detected():
    assert _column_looks_like_payer_rates(['N/A'] * 49 + ['500.00'])
