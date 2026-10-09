"""Golden-file contract tests: one parametrized test per golden file."""

import pytest
from golden_loader import golden_ids, load_golden

from mrfkit.codes import (
    _split_composite_code,
    apply_code_extraction,
    infer_billing_class,
    is_rejected_code,
    normalize_code,
)
from mrfkit.headers import (
    map_header,
    normalize_header,
    parse_hawaii_payer_header,
    parse_wide_payer_header,
)
from mrfkit.payers import normalize_payer_name, normalize_plan_name, plan_name_to_cat_net
from mrfkit.values import (
    _safe_count,
    _safe_float,
    normalize_ein,
    parse_address,
    parse_filename_metadata,
)


def _cases(filename):
    cases = load_golden(filename)
    params = [
        pytest.param(
            c,
            marks=[pytest.mark.xfail(reason=c["xfail"], strict=True)] if "xfail" in c else [],
        )
        for c in cases
    ]
    return pytest.mark.parametrize("case", params, ids=golden_ids(cases))


# --- codes -----------------------------------------------------------------


@_cases("normalize_code.json")
def test_normalize_code(case):
    inp, expected = case["input"], case["expected"]
    code, code_type = normalize_code(inp["code"], inp["code_type"])
    assert code == expected["code"]
    assert code_type == expected["code_type"]


@_cases("split_composite_code.json")
def test_split_composite_code(case):
    inp, expected = case["input"], case["expected"]
    code, code_type = _split_composite_code(inp["code"], inp["code_type"])
    assert code == expected["code"]
    assert code_type == expected["code_type"]


@_cases("is_rejected_code.json")
def test_is_rejected_code(case):
    inp, expected = case["input"], case["expected"]
    assert is_rejected_code(inp["code"], inp["code_type"]) == expected["reason"]


@_cases("apply_code_extraction.json")
def test_apply_code_extraction(case):
    inp, expected = case["input"], case["expected"]
    code, code_type = apply_code_extraction(inp["code"], inp["code_type"], inp["config"])
    assert code == expected["code"]
    assert code_type == expected["code_type"]


@_cases("pipeline_extract_then_normalize.json")
def test_pipeline_extract_then_normalize(case):
    inp, expected = case["input"], case["expected"]
    ext_code, ext_type = apply_code_extraction(inp["code"], inp["code_type"], inp["config"])
    assert ext_code == expected["extracted_code"]
    assert ext_type == expected["extracted_code_type"]
    final_code, final_type = normalize_code(ext_code, ext_type)
    assert final_code == expected["final_code"]
    assert final_type == expected["final_code_type"]


@_cases("infer_billing_class.json")
def test_infer_billing_class(case):
    inp, expected = case["input"], case["expected"]
    billing_class, normalized_code, code_type = infer_billing_class(
        inp["code"], inp["code_type"], inp["description"], inp["billing_class"],
    )
    assert billing_class == expected["billing_class"]
    assert normalized_code == expected["normalized_code"]
    assert code_type == expected.get("code_type", inp["code_type"])


# --- headers ---------------------------------------------------------------


@_cases("normalize_header.json")
def test_normalize_header(case):
    assert normalize_header(case["input"]["header"]) == case["expected"]["normalized"]


@_cases("map_header.json")
def test_map_header(case):
    inp, expected = case["input"], case["expected"]
    normalized, mapped_field = map_header(inp["source_header"])
    assert normalized == expected["normalized"]
    assert mapped_field == expected["mapped_field"]


@_cases("parse_wide_payer_header.json")
def test_parse_wide_payer_header(case):
    expected = case["expected"]
    result = parse_wide_payer_header(case["input"]["source_header"])
    if expected is None:
        assert result is None
    else:
        assert result == (expected["payer"], expected["plan"], expected["field"])


@_cases("parse_hawaii_payer_header.json")
def test_parse_hawaii_payer_header(case):
    expected = case["expected"]
    result = parse_hawaii_payer_header(case["input"]["source_header"])
    if expected is None:
        assert result is None
    else:
        assert result == (expected["payer"], expected["setting"], expected["methodology"])


# --- payers ----------------------------------------------------------------


@_cases("normalize_payer_name.json")
def test_normalize_payer_name(case):
    inp, expected = case["input"], case["expected"]
    canonical, plan = normalize_payer_name(inp["raw_payer"], inp.get("raw_plan"))
    assert canonical == expected["canonical"]
    assert plan == expected["plan"]


@_cases("normalize_plan_name.json")
def test_normalize_plan_name(case):
    inp, expected_plan = case["input"], case["expected"]
    plan_category, plan_network, plan_name = normalize_plan_name(
        inp["raw_plan"], inp["payer_canonical"],
    )
    assert plan_name == expected_plan
    default_cat, default_net = plan_name_to_cat_net(expected_plan)
    assert plan_category == case.get("expected_category", default_cat)
    assert plan_network == case.get("expected_network", default_net)


# --- values ----------------------------------------------------------------


@_cases("normalize_ein.json")
def test_normalize_ein(case):
    assert normalize_ein(case["input"]["raw"]) == case["expected"]


@_cases("parse_filename_metadata.json")
def test_parse_filename_metadata(case):
    expected = case["expected"]
    result = parse_filename_metadata(case["input"]["filename"])
    assert result.get("ein") == expected["ein"]
    assert result.get("npi") == expected["npi"]


@_cases("parse_address.json")
def test_parse_address(case):
    expected = case["expected"]
    result = parse_address(case["input"]["raw"])
    for key in ("street", "city", "state", "zip"):
        assert result.get(key) == expected[key], key


@_cases("safe_float.json")
def test_safe_float(case):
    expected = case["expected"]["result"]
    result = _safe_float(case["input"]["value"])
    if expected is None:
        assert result is None
    else:
        assert result is not None
        assert abs(result - expected) < 1e-9


@_cases("safe_count.json")
def test_safe_count(case):
    assert _safe_count(case["input"]["value"]) == case["expected"]["result"]
