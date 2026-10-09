"""Self-pay detection, payer-name validity and the payer match-fold."""

from __future__ import annotations

import pytest

from mrfkit.payers import (
    _JUNK_PAYER_NAMES,
    _SELF_PAY_KEYWORD_SUBSTRINGS,
    _SELF_PAY_NAMES,
    _contains_self_pay_keyword,
    _singularize_token,
    is_self_pay_payer,
    is_valid_payer_name,
    normalize_payer_name,
    payer_match_key,
)
from mrfkit.reference import ReferenceData

# ---------------------------------------------------------------------------
# is_self_pay_payer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "<Self-Pay>", "<Self-pay>", "<SELF-PAY>", "<Self Pay>", "[Self-Pay]", "[SELF-PAY]",
    "Self-Pay", "Self Pay", "SELF PAY", "SELF-PAY", "self pay", "self-pay",
    "Selfpay", "SELFPAY", "Cash", "CASH", "cash", "Cash Pay", "CASH PAY",
    "Cash Price", "Cash Price Estimate", "CASH PRICE ESTIMATE", "Cash Discount",
    "Patient", "Patient Pay", "PATIENT PAY", "Uninsured", "UNINSURED",
    "Uninsured/Self-Pay", "UNINSURED/SELF-PAY", "Self-Pay/Uninsured", "SELF-PAY/UNINSURED",
    "No Insurance", "NO INSURANCE", "Self", "SELF", "Gross Charges", "GROSS CHARGES",
    "Gross Charge", "GROSS CHARGE", "CDM Default", "CDM DEFAULT", "CDM", "cdm",
    "Charge Master", "CHARGE MASTER", "Chargemaster", "CHARGEMASTER",
    "  Self Pay  ",   # leading/trailing whitespace
    "SELF_PAY",       # underscore separator
    "<SELF_PAY>",     # angle brackets + underscore
    "Self  Pay",      # double space
])
def test_self_pay_positive(name):
    assert is_self_pay_payer(name)


@pytest.mark.parametrize("name", [
    "Aetna", "Blue Cross Blue Shield", "United Healthcare",
    "Aetna Sutter Health Self Insured",   # self-insured is NOT self-pay
    "Medicare", "Cigna", "", None, "   ",
    "Cash America Insurance",             # real insurer with "Cash"
    "BlueCash PPO",                       # "Cash" inside a token
    "Cash Advance Bank",                  # "Cash" alone shouldn't trigger
])
def test_self_pay_negative(name):
    assert not is_self_pay_payer(name)


@pytest.mark.parametrize("name", [
    "Self Pay Rate", "Cash/Self Pay Price", "Uninsured Patient Estimate",
    "Self-Pay (no insurance)", "Self Pay Discount", "SELF PAY ESTIMATE",
    "Selfpay Cash Rate", "Uninsured Discount", "Estimated Uninsured Patient Cost",
    "Self-Pay Plus Plan",
])
def test_self_pay_keyword_substrings(name):
    """Any name containing SELF PAY / SELF-PAY / SELFPAY / UNINSURED folds into cash."""
    assert is_self_pay_payer(name)


@pytest.mark.parametrize("name", [
    "Cash America Insurance", "BlueCash PPO", "Cash Plus Network",
    "Aetna Sutter Health Self Insured",
    "Self Service Health Plan",   # SELF but not SELF PAY
    "Uninsurable Risk Pool",      # UNINSUR prefix but not UNINSURED
])
def test_self_pay_keywords_do_not_mispromote_real_insurers(name):
    assert not is_self_pay_payer(name)


def test_self_pay_keyword_helper_pinned():
    assert set(_SELF_PAY_KEYWORD_SUBSTRINGS) == {"SELF PAY", "SELF-PAY", "SELFPAY", "UNINSURED"}
    # Caller must pass uppercase; the helper does not lowercase.
    assert _contains_self_pay_keyword("FOO SELF PAY BAR") is True
    assert _contains_self_pay_keyword("foo self pay bar") is False
    # CASH alone is not a substring trigger.
    assert _contains_self_pay_keyword("BLUE CASH PPO") is False


@pytest.mark.parametrize("name", [
    "30% Self Pay/Uninsured Discount", "30% SELF PAY/UNINSURED DISCOUNT",
    "30% Self-Pay/Uninsured", "50% Self Pay", "50% Cash", "10% Uninsured",
    "25.5% Self-Pay", "100% Cash Price", "75 % Self Pay", "<30% Self-Pay>",
])
def test_percent_prefix_self_pay_detected(name):
    """A leading NN% is stripped before matching, so the cash price is not lost."""
    assert is_self_pay_payer(name)


@pytest.mark.parametrize("name", ["95% Anthem Blue Cross", "100% Aetna", "85% Cigna PPO", "100% All Other"])
def test_percent_prefix_real_insurer_not_self_pay(name):
    assert not is_self_pay_payer(name)


@pytest.mark.parametrize("name", [
    "30% Self Pay/Uninsured Discount", "50% Self Pay", "10% Uninsured",
    "100% Cash", "25.5% Self-Pay/Uninsured",
])
def test_self_pay_matchers_agree_on_percent_prefix(name):
    """For %-prefixed self-pay rows, is_self_pay_payer says True AND
    normalize_payer_name discards the payer, so the row is folded into cash
    and not also kept as a payer rate."""
    canonical, _ = normalize_payer_name(name)
    assert is_self_pay_payer(name) is True
    assert canonical is None


def test_self_pay_names_are_uppercase():
    for name in _SELF_PAY_NAMES:
        assert name == name.upper()


@pytest.mark.parametrize("stored_name", [
    "<Self-Pay>", "Self-Pay", "Self Pay", "Cash", "Cash Price", "Gross Charges",
    "Gross Charge", "CDM Default", "Chargemaster", "Uninsured", "Patient",
])
def test_normalized_self_pay_forms_still_detected(stored_name):
    """The names normalize_payer_name produces for self-pay input are still self-pay."""
    assert is_self_pay_payer(stored_name)


# ---------------------------------------------------------------------------
# is_valid_payer_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "", "   ",
    "0.05", "0.10", "27.07", "147.05", '"147.05"',   # rate-like
    "#", "--", ".", "##",                          # punctuation-only
    "1199", "0", "999",                            # bare integers
])
def test_junk_payer_names_invalid(name):
    assert is_valid_payer_name(name) is False


def test_none_payer_name_invalid():
    assert is_valid_payer_name(None) is False


@pytest.mark.parametrize("name", sorted(_JUNK_PAYER_NAMES - {''}))
def test_junk_set_members_invalid(name):
    assert is_valid_payer_name(name) is False


@pytest.mark.parametrize("name", [
    "1199 SEIU", "Imagine 360", "Advantage 360", "Plan 65",
    "Aetna", "Cigna", "Blue Cross Blue Shield", "United Healthcare",
    "Some Regional Payer",
])
def test_valid_payer_names(name):
    assert is_valid_payer_name(name) is True


# ---------------------------------------------------------------------------
# Payer match-fold
# ---------------------------------------------------------------------------

# Real spelling variants of the same payer. Each pair must share a match key.
_VARIANT_PAIRS = [
    ("Assured Benefit Administrators", "Assured Benefits Administrators"),
    ("Buckeye Community Health Plans", "Buckeye Community Health Plan"),
    ("Wisconsin Physician Service", "Wisconsin Physicians Services"),
    ("Mental Health Consultants Inc", "Mental Health Consultants, Inc"),
    ("Provider Partners Health Plans", "Provider Partners Health Plan"),
    ("Municipal Health Benefits Fund", "Municipal Health Benefit Fund"),
    ("Imagine Health Network, Inc", "Imagine Health Network, Inc."),
    ("Georgia Workers’ Compensation", "Georgia Workers Compensation"),
    ("Community Partners Health Plan", "Community Partners Health Plans"),
    ("Multiplan, Inc. (Dba Claritev)", "Multiplan, Inc. (Dba Claritev"),
    ("Community First Health Plans", "Community First Health Plan"),
    ("Midwest- Operating Engineers", "Midwest Operating Engineers"),
    ("Carolina Complete Health Inc", "Carolina Complete Health Inc."),
    ("Health Alliance Medical Plan", "Health Alliance Medical Plans"),
    ("Texas Childrens Health Plan", "Texas Childrens Health Plans"),
    ("St. Francis Medical Center", "St Francis Medical Center"),
    ("Childrens Medical Service", "Childrens Medical Services"),
    ("Employee Benefits Systems", "Employee Benefit Systems"),
    ("Amerigroup Tennessee, Inc", "Amerigroup Tennessee Inc"),
    ("Indian Health Services", "Indian Health Service"),
    ("Enterprise Group Planning/Employee Benefit Plan", "Enterprise Group Planning Employee Benefit Plan"),
    ("Vantage Health Plan Inc", "Vantage Health Plan Inc."),
    ("Florida Healthcare Plan", "Florida Healthcare Plans"),
]

# Pairs that must STAY distinct.
_DISTINCT_PAIRS = [
    ("Blue Cross of California", "Blue Cross of Idaho"),
    ("Aetna", "Cigna"),
    ("Wisconsin Physicians Service", "Wisconsin Physicians Insurance"),
    ("Community First Health Plan", "Community Health Plan"),
    ("El Paso First Health Plan", "El Paso Second Health Plan"),
    ("Johns Hopkins Health Plan", "Johns Hopkins Hospital"),
]


@pytest.mark.parametrize("a,b", _VARIANT_PAIRS)
def test_variant_pairs_share_match_key(a, b):
    assert payer_match_key(a) == payer_match_key(b)
    assert payer_match_key(a)


@pytest.mark.parametrize("a,b", _DISTINCT_PAIRS)
def test_distinct_pairs_keep_separate_match_keys(a, b):
    assert payer_match_key(a) != payer_match_key(b)


def test_match_key_examples():
    assert payer_match_key("Buckeye Community Health Plans") == "BUCKEYE COMMUNITY HEALTH PLAN"
    assert payer_match_key("Wisconsin Physicians Service") == "WISCONSIN PHYSICIAN SERVICE"
    assert payer_match_key("Georgia Workers’ Compensation") == "GEORGIA WORKER COMPENSATION"
    assert payer_match_key("") == ""
    assert payer_match_key(None) == ""


def test_singularize_token_is_conservative():
    assert _singularize_token("plans") == "plan"
    assert _singularize_token("services") == "service"
    assert _singularize_token("administrators") == "administrator"
    assert _singularize_token("express") == "express"
    assert _singularize_token("wellness") == "wellness"
    assert _singularize_token("hms") == "hms"
    assert _singularize_token("of") == "of"


def test_fold_is_noop_without_index():
    a, _ = normalize_payer_name("Buckeye Community Health Plans")
    b, _ = normalize_payer_name("Buckeye Community Health Plan")
    assert a == "Buckeye Community Health Plans"
    assert b == "Buckeye Community Health Plan"


def test_fold_redirects_variant_onto_winner():
    winner = "Buckeye Community Health Plan"
    ref = ReferenceData(payer_match_index={payer_match_key(winner): winner})
    folded, _ = normalize_payer_name("Buckeye Community Health Plans", ref=ref)
    assert folded == winner


def test_fold_collapses_new_payer_variants_within_one_run():
    """First spelling seen wins; later variants with the same ref fold onto it."""
    ref = ReferenceData(payer_match_index={"__seed__": "seed"})
    first, _ = normalize_payer_name("Brand New Health Plans", ref=ref)
    second, _ = normalize_payer_name("Brand New Health Plan", ref=ref)
    assert first == "Brand New Health Plans"
    assert second == first


def test_fold_does_not_touch_curated_canonical_map_hits():
    ref = ReferenceData(payer_match_index={payer_match_key("Aetna"): "WRONG"})
    name, _ = normalize_payer_name("Aetna", ref=ref)
    assert name == "Aetna"
