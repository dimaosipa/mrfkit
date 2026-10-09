"""
Tests for JSON non-CMS wide-format detection and processing.

Covers:
  - Atrium wide format detection in JSON path
  - Setting-specific charge_item/charge_standard emission
  - Min/Max indicator routing
  - Suppression of low-value unmapped columns (JSON path)
  - Middlesex synonym mappings (cpthcpcs_code, gross_charge_per_cdm, etc.)

Run:
    cd mrf-parser && .venv/bin/python -m pytest tests/test_json_wide_format.py -v
"""

from unittest.mock import MagicMock

import pytest

from staging_shim import _process_json_items_to_staging
from mrfkit.headers import SYNONYM_MAP, normalize_header


def _make_mock_ingestor():
    """Create a mock staging recorder that records all staged data."""
    mock = MagicMock()
    mock._seen_items = set()
    mock._payer_rate_count = 0

    items = []
    standards = []
    payer_rates = []
    unmapped = []

    def fake_stage_item(code, code_type, description, billing_class,
                        setting=None, modifiers=None,
                        drug_unit_of_measurement=None,
                        drug_type_of_measurement=None,
                        additional_generic_notes=None):
        key = (code, code_type, billing_class or '', setting or '')
        mock._seen_items.add(key)
        items.append({
            'code': code, 'code_type': code_type,
            'description': description, 'billing_class': billing_class,
            'setting': setting,
        })

    def fake_stage_std(code, code_type, description, billing_class,
                       setting, gross, cash, min_rate, max_rate,
                       modifiers=None):
        standards.append({
            'code': code, 'code_type': code_type,
            'setting': setting,
            'gross': gross, 'cash': cash,
            'min_rate': min_rate, 'max_rate': max_rate,
        })

    def fake_stage_payer(code, code_type, description, billing_class,
                         payer_name, raw_payer_name, plan_category,
                         plan_network, plan_name, item_setting=None,
                         negotiated_rate=None, negotiated_percentage=None,
                         negotiated_algorithm=None, methodology=None,
                         estimated_amount=None, rate_billing_class=None,
                         setting=None, additional_notes=None, footnote=None,
                         median_amount=None, pct_10=None, pct_90=None,
                         claim_count=None, item_modifiers=None):
        mock._payer_rate_count += 1
        payer_rates.append({
            'code': code, 'code_type': code_type,
            'payer_name': payer_name, 'plan_name': plan_name,
            'negotiated_rate': negotiated_rate,
            'setting': setting,
        })

    def fake_stage_unmapped(code, code_type, description, billing_class,
                            item_setting, source_column, value_text,
                            item_modifiers=None):
        unmapped.append({
            'source_column': source_column, 'value_text': value_text,
            'item_setting': item_setting,
        })

    mock.stage_charge_item.side_effect = fake_stage_item
    mock.stage_charge_standard.side_effect = fake_stage_std
    mock.stage_payer_rate.side_effect = fake_stage_payer
    mock.stage_unmapped.side_effect = fake_stage_unmapped
    mock.flush_all_staging.return_value = None
    mock.insert_header_mappings.return_value = None
    mock.update_header_mapping.return_value = None

    mock._items = items
    mock._standards = standards
    mock._payer_rates = payer_rates
    mock._unmapped = unmapped

    return mock


# ============================================================================
# Atrium JSON Wide Format
# ============================================================================

def _atrium_json_items():
    """Simulates Atrium Health JSON items with setting-specific columns."""
    return [
        {
            "Code": "99213",
            "Code Type": "CPT",
            "Procedure Description": "Office Visit",
            "Payer": "Aetna",
            "Plan": "Commercial",
            " Inpatient Gross Charge": "150.00",
            " Outpatient Gross Charge": "120.00",
            " Inpatient Negotiated Charge": "100.00",
            " Outpatient Negotiated Charge": "80.00",
            "Min /Max": "MIN",
            "TabName": "Hosp Std. Charges_All Payors",
            "Rev Code": "0510",
            "Procedure": "1100000001",
        },
        {
            "Code": "99213",
            "Code Type": "CPT",
            "Procedure Description": "Office Visit",
            "Payer": "Aetna",
            "Plan": "Commercial",
            " Inpatient Gross Charge": "150.00",
            " Outpatient Gross Charge": "120.00",
            " Inpatient Negotiated Charge": "110.00",
            " Outpatient Negotiated Charge": "90.00",
            "Min /Max": "MAX",
            "TabName": "Hosp Std. Charges_All Payors",
            "Rev Code": "0510",
            "Procedure": "1100000001",
        },
        {
            "Code": "27447",
            "Code Type": "CPT",
            "Procedure Description": "Hip Replacement",
            " Inpatient Gross Charge": "50000.00",
            " Outpatient Gross Charge": "N/A",
            " Inpatient Negotiated Charge": "35000.00",
            " Outpatient Negotiated Charge": "N/A",
            "Min /Max": "MIN",
            "TabName": "Hosp Deidentified Min Max",
            "Rev Code": "0360",
            "Procedure": "1100000002",
        },
    ]


def _atrium_json_with_payer_rate():
    """Atrium JSON item with no MIN/MAX → should produce payer rate."""
    return [
        {
            "Code": "99214",
            "Code Type": "CPT",
            "Procedure Description": "Office Visit Level 4",
            "Payer": "Cigna",
            "Plan": "PPO",
            " Inpatient Gross Charge": "200.00",
            " Outpatient Gross Charge": "180.00",
            " Inpatient Negotiated Charge": "160.00",
            " Outpatient Negotiated Charge": "140.00",
            "Min /Max": "",
            "TabName": "Hosp Std. Charges_All Payors",
        },
    ]


class TestAtriumJsonWideFormat:
    """Verify Atrium Health wide format detection in JSON path."""

    def test_atrium_json_detected(self):
        """Atrium wide format should be detected via setting-specific keys."""
        mock = _make_mock_ingestor()
        logs = []

        _process_json_items_to_staging(
            _atrium_json_items(), mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: logs.append(msg),
        )

        assert any('atrium wide format' in msg.lower() for msg in logs), (
            f"Expected Atrium detection log, got: {logs}"
        )

    def test_multiple_settings_emitted(self):
        """Row with both IP/OP non-N/A should produce 2 settings."""
        mock = _make_mock_ingestor()

        _process_json_items_to_staging(
            _atrium_json_items(), mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
        )

        settings = {item['setting'] for item in mock._items}
        assert 'inpatient' in settings, f"Expected inpatient, got: {settings}"
        assert 'outpatient' in settings, f"Expected outpatient, got: {settings}"

    def test_na_setting_skipped(self):
        """Hip Replacement has N/A for outpatient → only inpatient emitted."""
        mock = _make_mock_ingestor()

        _process_json_items_to_staging(
            _atrium_json_items(), mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
        )

        hip_items = [i for i in mock._items if i['code'] == '27447']
        hip_settings = {i['setting'] for i in hip_items}
        assert hip_settings == {'inpatient'}, (
            f"Hip Replacement should only have inpatient, got: {hip_settings}"
        )

    def test_charge_standards_per_setting(self):
        """Each setting gets correct gross charge values."""
        mock = _make_mock_ingestor()

        _process_json_items_to_staging(
            _atrium_json_items(), mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
        )

        ip_stds = [s for s in mock._standards
                    if s['setting'] == 'inpatient' and s['code'] == '99213']
        op_stds = [s for s in mock._standards
                    if s['setting'] == 'outpatient' and s['code'] == '99213']

        assert ip_stds, "Expected inpatient charge standards for 99213"
        assert op_stds, "Expected outpatient charge standards for 99213"

        # Inpatient gross should be 150.00
        assert ip_stds[0]['gross'] == 150.0
        # Outpatient gross should be 120.00
        assert op_stds[0]['gross'] == 120.0

    def test_min_max_routing(self):
        """MIN/MAX indicator routes negotiated charge to min/max fields."""
        mock = _make_mock_ingestor()

        _process_json_items_to_staging(
            _atrium_json_items(), mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
        )

        # First row: MIN → min_rate populated, max_rate None
        ip_stds_99213 = [s for s in mock._standards
                         if s['setting'] == 'inpatient' and s['code'] == '99213']
        min_rows = [s for s in ip_stds_99213 if s['min_rate'] is not None]
        max_rows = [s for s in ip_stds_99213 if s['max_rate'] is not None]
        assert min_rows, "Expected min_rate from MIN row"
        assert max_rows, "Expected max_rate from MAX row"
        assert min_rows[0]['min_rate'] == 100.0
        assert max_rows[0]['max_rate'] == 110.0

    def test_payer_rate_emitted_without_minmax(self):
        """When Min/Max is empty, negotiated charge → payer rate."""
        mock = _make_mock_ingestor()

        _process_json_items_to_staging(
            _atrium_json_with_payer_rate(), mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
        )

        assert mock._payer_rates, "Expected payer rates for non-MIN/MAX row"
        # Should have rates for both settings
        rate_settings = {r['setting'] for r in mock._payer_rates}
        assert 'inpatient' in rate_settings
        assert 'outpatient' in rate_settings

    def test_payer_rate_not_emitted_for_min_rows(self):
        """MIN/MAX rows should NOT produce payer rates."""
        mock = _make_mock_ingestor()

        _process_json_items_to_staging(
            _atrium_json_items(), mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
        )

        assert not mock._payer_rates, (
            f"MIN/MAX rows should not produce payer rates, got: "
            f"{mock._payer_rates}"
        )

    def test_tabname_suppressed(self):
        """TabName, Rev Code, Procedure should be suppressed (not in unmapped)."""
        mock = _make_mock_ingestor()

        _process_json_items_to_staging(
            _atrium_json_items(), mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
        )

        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        for col in ('TabName', 'Rev Code', 'Procedure'):
            assert col not in unmapped_cols, (
                f"{col} should be suppressed, found in unmapped: {unmapped_cols}"
            )

    def test_min_max_column_consumed(self):
        """Min /Max column should be consumed by Atrium detection."""
        mock = _make_mock_ingestor()

        _process_json_items_to_staging(
            _atrium_json_items(), mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
        )

        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        assert 'Min /Max' not in unmapped_cols, (
            "Min /Max should be consumed by Atrium detection, found in unmapped"
        )

    def test_empty_and_na_unmapped_values_skipped(self):
        """Empty strings and N/A values should not produce unmapped cells."""
        items = [
            {
                "code": "99213",
                "description": "Office Visit",
                "code_type": "CPT",
                " Inpatient Gross Charge": "100.00",
                " Outpatient Gross Charge": "80.00",
                "Min /Max": "",
                "custom_field": "",
                "custom_field2": "N/A",
                "custom_field3": "  ",
                "custom_field4": "real value",
            },
        ]
        mock = _make_mock_ingestor()

        _process_json_items_to_staging(
            items, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
        )

        unmapped_vals = {u['source_column']: u['value_text']
                        for u in mock._unmapped}
        assert 'custom_field' not in unmapped_vals, "Empty string should be skipped"
        assert 'custom_field2' not in unmapped_vals, "N/A should be skipped"
        assert 'custom_field3' not in unmapped_vals, "Whitespace should be skipped"
        assert 'custom_field4' in unmapped_vals, "Real values should be preserved"
        assert unmapped_vals['custom_field4'] == 'real value'


# ============================================================================
# JSON Suppression (non-Atrium)
# ============================================================================

class TestJsonSuppression:
    """Verify low-value columns are suppressed in JSON non-CMS path."""

    def test_generic_suppressed_columns(self):
        """Generic low-value JSON columns should be suppressed."""
        items = [
            {
                "Code": "99213",
                "Description": "Office Visit",
                "Code Type": "CPT",
                "Gross Charge": "150.00",
                "rev_code": "0510",
                "ndc": "12345-6789-01",
                "department": "Cardiology",
                "procedure_id": "PROC001",
            },
        ]
        mock = _make_mock_ingestor()

        _process_json_items_to_staging(
            items, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
        )

        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        for col in ('rev_code', 'ndc', 'department', 'procedure_id'):
            assert col not in unmapped_cols, (
                f"{col} should be suppressed, found in unmapped: {unmapped_cols}"
            )

    def test_middlesex_columns_suppressed(self):
        """Middlesex-specific internal columns should be suppressed."""
        items = [
            {
                "Code": "99213",
                "Description": "Office Visit",
                "Code Type": "CPT",
                "Revenue Code Description": "General",
                "FscRptCat3Name": "Cat3",
                "FscName": "Fiscal",
                "FscCategory": "FiscalCat",
                "Script Type": "TypeA",
                "Service Area": "Area1",
            },
        ]
        mock = _make_mock_ingestor()

        _process_json_items_to_staging(
            items, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
        )

        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        for col in ('Revenue Code Description', 'FscRptCat3Name', 'FscName',
                     'FscCategory', 'Script Type', 'Service Area'):
            assert col not in unmapped_cols, (
                f"{col} should be suppressed, found in unmapped: {unmapped_cols}"
            )


# ============================================================================
# Middlesex Synonym Mappings
# ============================================================================

class TestMiddlesexSynonyms:
    """Verify new SYNONYM_MAP entries for Middlesex Hospital headers."""

    @pytest.mark.parametrize("source_header,expected_field", [
        ("CPT/HCPCS Code", "code"),
        ("Gross Charge per CDM", "gross_charge"),
        ("Rate Methodology", "methodology"),
        ("Billing Code Description", "description"),
    ])
    def test_synonym_mapping(self, source_header, expected_field):
        """Middlesex headers should map to correct canonical fields."""
        normalized = normalize_header(source_header)
        mapped = SYNONYM_MAP.get(normalized)
        assert mapped == expected_field, (
            f"Header {source_header!r} (norm: {normalized!r}) should map to "
            f"{expected_field!r}, got {mapped!r}"
        )

    def test_middlesex_json_produces_items(self):
        """Simulated Middlesex JSON data should produce charge items."""
        items = [
            {
                "CPT/HCPCS Code": "99213",
                "Billing Code Description": "Office Visit",
                "Gross Charge per CDM": "150.00",
                "Rate Methodology": "Fee Schedule",
                "Revenue Code Description": "General",
                "FscRptCat3Name": "Cat3",
            },
        ]
        mock = _make_mock_ingestor()

        _process_json_items_to_staging(
            items, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
        )

        assert mock._items, "Expected charge items from Middlesex data"
        assert mock._items[0]['code'] == '99213'
        assert mock._items[0]['description'] == 'Office Visit'
        assert mock._standards, "Expected charge standards from Middlesex data"
        assert mock._standards[0]['gross'] == 150.0
