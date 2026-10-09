"""
Golden-file contract tests for _flatten_cms_item().

Source: mrfkit.json_reader._flatten_cms_item
Golden: tests/golden/flatten_cms_item.json

Note: Some test cases use partial matching (rows_unordered, payer_count)
because row order depends on dict insertion order and payer objects are
complex nested structures.
"""

import pytest
from golden_loader import load_golden, golden_ids
from mrfkit.json_reader import _flatten_cms_item, _json_get, _JSON_FIELD_MAP, _JSON_CANONICAL_TO_KEYS

_CASES = load_golden("flatten_cms_item.json")


@pytest.mark.parametrize("case", _CASES, ids=golden_ids(_CASES))
def test_flatten_cms_item(case):
    inp = case["input"]
    expected = case["expected"]

    rows = _flatten_cms_item(inp["obj"])

    if "row_count" in expected:
        assert len(rows) == expected["row_count"], (
            f"Expected {expected['row_count']} rows, got {len(rows)}"
        )

    if "rows_unordered" in expected:
        # Check that each expected partial row matches some actual row
        for exp_partial in expected["rows_unordered"]:
            found = False
            for row in rows:
                if all(row.get(k) == v for k, v in exp_partial.items()):
                    found = True
                    break
            assert found, (
                f"No row matches {exp_partial!r} in {rows!r}"
            )
        return

    if "rows" in expected:
        assert len(rows) == len(expected["rows"]), (
            f"Expected {len(expected['rows'])} rows, got {len(rows)}"
        )
        for i, exp_row in enumerate(expected["rows"]):
            actual = rows[i]
            for key, exp_val in exp_row.items():
                if key == "payer_count":
                    assert len(actual["payers"]) == exp_val, (
                        f"Row {i} payer_count mismatch: got {len(actual['payers'])}, expected {exp_val}"
                    )
                else:
                    assert actual.get(key) == exp_val, (
                        f"Row {i} {key} mismatch: got {actual.get(key)!r}, expected {exp_val!r}"
                    )


# ── _json_get helper tests ───────────────────────────────────────────


class TestJsonGet:
    """Unit tests for _json_get() and _JSON_FIELD_MAP."""

    def test_primary_key(self):
        """Primary key found directly."""
        assert _json_get({'payer_name': 'Aetna'}, 'payer_name') == 'Aetna'

    def test_alternate_key(self):
        """Alternate key resolves to canonical."""
        assert _json_get({'standard_charge_dollar': 150}, 'negotiated_rate') == 150

    def test_modifier_code_alias(self):
        """modifier_code maps to modifiers canonical."""
        assert _json_get({'modifier_code': ['26']}, 'modifiers') == ['26']

    def test_first_wins(self):
        """When multiple keys present, first in _JSON_FIELD_MAP order wins."""
        obj = {'standard_charge_dollar': 100, 'negotiated_dollar': 200}
        assert _json_get(obj, 'negotiated_rate') == 100

    def test_missing_returns_default(self):
        """Missing key returns default."""
        assert _json_get({}, 'payer_name') is None
        assert _json_get({}, 'payer_name', 'fallback') == 'fallback'

    def test_none_value_skipped(self):
        """Explicit None value skipped, falls through to alternate."""
        obj = {'standard_charge_dollar': None, 'negotiated_dollar': 50}
        assert _json_get(obj, 'negotiated_rate') == 50

    def test_reverse_map_complete(self):
        """Every _JSON_FIELD_MAP value has a corresponding reverse entry."""
        for json_key, canonical in _JSON_FIELD_MAP.items():
            assert json_key in _JSON_CANONICAL_TO_KEYS[canonical]

    def test_payer_stat_fields_mapped(self):
        """median_amount, pct_10, pct_90, claim_count are in the map."""
        assert 'median_amount' in _JSON_CANONICAL_TO_KEYS
        assert 'pct_10' in _JSON_CANONICAL_TO_KEYS
        assert 'pct_90' in _JSON_CANONICAL_TO_KEYS
        assert 'claim_count' in _JSON_CANONICAL_TO_KEYS

    def test_discounted_cash_variants(self):
        """Both discounted_cash and discounted_cash_price map correctly."""
        assert _json_get({'discounted_cash': 100}, 'discounted_cash_price') == 100
        assert _json_get({'discounted_cash_price': 200}, 'discounted_cash_price') == 200
