"""
Tests for CSV row-level rejection behavior in parse_csv_to_staging().

When every (code, code_type) pair on a row is rejected by
`is_rejected_code()`, the entire row must be dropped, even if the row has
a description. Otherwise a description-only fallback would stage a
charge_item with NULL code/code_type, contradicting the intent of
is_rejected_code() to drop corrupt rows entirely.
"""

import io
from unittest.mock import MagicMock


from staging_shim import parse_csv_to_staging


def _make_mock_ingestor():
    mock = MagicMock()
    mock._seen_items = set()
    mock._payer_rate_count = 0
    items = []

    def fake_stage_item(code, code_type, description, billing_class,
                        setting=None, modifiers=None,
                        drug_unit_of_measurement=None,
                        drug_type_of_measurement=None,
                        additional_generic_notes=None):
        items.append({
            'code': code, 'code_type': code_type,
            'description': description,
        })

    mock.stage_charge_item.side_effect = fake_stage_item
    mock.stage_charge_standard.side_effect = lambda *a, **kw: None
    mock.stage_payer_rate.side_effect = lambda *a, **kw: None
    mock.stage_unmapped.side_effect = lambda *a, **kw: None
    mock.flush_all_staging.return_value = None
    mock.insert_header_mappings.return_value = None
    mock.update_header_mapping.return_value = None
    mock._items = items
    return mock


# Two rows:
#   1. code=300 code_type=5 (decimal MS-DRG with numeric code_type), rejected
#   2. code=99213 code_type=CPT, valid
# Both rows have a description present. Rejection must drop row 1 entirely
# rather than staging a NULL-coded charge_item from the description fallback.
REJECT_CSV = (
    "code|code_type|description|gross_charge\n"
    "300|5|Corrupt row description|100.00\n"
    "99213|CPT|Valid office visit|150.00\n"
)


class TestRejectedRowDropsEntireRow:
    def test_corrupt_row_not_staged_with_null_code(self):
        """
        A row whose only code input is rejected MUST NOT be staged with
        (None, None), even when description is present.
        """
        mock = _make_mock_ingestor()
        handle = io.StringIO(REJECT_CSV)

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
        )

        # Valid row should be present
        codes = [(it['code'], it['code_type']) for it in mock._items]
        assert ('99213', 'CPT') in codes, (
            f"Expected valid row to be staged; got {codes}"
        )

        # No (None, None) item from description-only fallback on rejected row
        null_coded = [it for it in mock._items
                      if it['code'] is None and it['code_type'] is None]
        assert null_coded == [], (
            f"Rejected row was incorrectly staged with NULL code/code_type: "
            f"{null_coded}"
        )

        # And the corrupt description should not have leaked in as a row
        descriptions = [it['description'] for it in mock._items]
        assert 'Corrupt row description' not in descriptions, (
            f"Rejected row's description leaked into staged items: "
            f"{descriptions}"
        )

    def test_description_only_row_still_staged(self):
        """
        A row with no code input at all but a description should still
        stage with (None, None), that path is unaffected by the fix.
        """
        csv_text = (
            "code|code_type|description|gross_charge\n"
            "||Description-only row|50.00\n"
        )
        mock = _make_mock_ingestor()
        handle = io.StringIO(csv_text)

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
        )

        null_coded = [it for it in mock._items
                      if it['code'] is None and it['code_type'] is None]
        assert len(null_coded) == 1, (
            f"Expected description-only row to stage with NULL codes; "
            f"got {mock._items}"
        )
        assert null_coded[0]['description'] == 'Description-only row'
