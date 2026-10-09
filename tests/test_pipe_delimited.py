"""
Tests for pipe-delimited CSV parsing support in parse_csv_to_staging().

These tests use a mock staging recorder to verify that pipe-delimited files
are correctly parsed without requiring a database connection.
"""

import io
from unittest.mock import MagicMock


from staging_shim import header_mappings, parse_csv_to_staging
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
            'negotiated_percentage': negotiated_percentage,
            'negotiated_algorithm': negotiated_algorithm,
            'estimated_amount': estimated_amount,
            'footnote': footnote,
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


PIPE_CSV = (
    "facility_id|facility_name|procedure|code_type|code|"
    "min_ip_reimb|max_ip_reimb|min_op_reimb|max_op_reimb|"
    "ndc|rev_code|procedure_description|quantity|"
    "payer|contract|plan|"
    "ip_price|ip_pricing_detail|ip_expected_reimbursement|ip_xr_detail|"
    "op_price|op_pricing_detail|op_expected_reimbursement|op_xr_detail\n"
    "1234|Test Hospital|HIP REPLACEMENT|DRG|MS-DRG V41.0 (FY 2024) 469|"
    "15000.00|45000.00|0|0|"
    "||Total Hip Replacement Surgery|1|"
    "AETNA [1001]|1001|AETNA HMO PLAN [100101]|"
    "32000.50|||"
    "|0||||\n"
    "1234|Test Hospital|BLOOD TEST|EAP|HCPCS C1776|"
    "100.00|500.00|80.00|450.00|"
    "||Cardiovascular stent|1|"
    "CIGNA [2001]|2001|CIGNA PPO [200101]|"
    "0|||"
    "|250.75||200.00||\n"
    "1234|Test Hospital|ASPIRIN|SUP|CPT 99213|"
    "50.00|150.00|40.00|120.00|"
    "12345-6789-01||Aspirin 325mg tablet|10|"
    "<Self-pay>||<Self-pay>|"
    "75.00|||"
    "|0||||\n"
)


class TestPipeDelimitedParsing:
    """Test that pipe-delimited CSV files are correctly parsed."""

    def test_delimiter_detection(self):
        """Verify pipe delimiter is detected and fields are split correctly."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(PIPE_CSV)
        logs = []

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: logs.append(msg))

        # Should detect pipe delimiter
        assert any('pipe-delimited' in msg for msg in logs), (
            f"Expected pipe-delimited detection log, got: {logs}"
        )

    def test_rows_parsed(self):
        """Verify all 3 rows are parsed (not zero)."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(PIPE_CSV)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1, log=lambda msg, **kw: None)

        assert len(mock._items) >= 3, (
            f"Expected at least 3 items, got {len(mock._items)}"
        )

    def test_composite_code_split(self):
        """Verify composite codes like 'HCPCS C1776' are split correctly."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(PIPE_CSV)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1, log=lambda msg, **kw: None)

        # Row 2: code='HCPCS C1776', code_type='EAP'
        # After composite split: code='C1776', code_type='HCPCS'
        codes = {(item['code'], item['code_type']) for item in mock._items}
        assert ('C1776', 'HCPCS') in codes, (
            f"Expected ('C1776', 'HCPCS') in items, got codes: {codes}"
        )

    def test_drg_code_extraction(self):
        """Verify MS-DRG composite code extracts trailing DRG number."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(PIPE_CSV)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1, log=lambda msg, **kw: None)

        # Row 1: code='MS-DRG V41.0 (FY 2024) 469', code_type='DRG'
        # After composite split + normalization: code='469', code_type='MS-DRG'
        codes = {(item['code'], item['code_type']) for item in mock._items}
        assert ('469', 'MS-DRG') in codes, (
            f"Expected ('469', 'MS-DRG') in items, got codes: {codes}"
        )

    def test_cpt_composite_split(self):
        """Verify 'CPT 99213' is split even when code_type is SUP (→CDM)."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(PIPE_CSV)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1, log=lambda msg, **kw: None)

        # Row 3: code='CPT 99213', code_type='SUP' (→CDM, hospital-internal)
        # Since CDM is hospital-internal, composite split fires: code='99213', type='CPT'
        codes = {(item['code'], item['code_type']) for item in mock._items}
        assert ('99213', 'CPT') in codes, (
            f"Expected ('99213', 'CPT') in items, got codes: {codes}"
        )

    def test_payer_rates_have_values(self):
        """Verify payer rates have negotiated_rate values."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(PIPE_CSV)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1, log=lambda msg, **kw: None)

        assert len(mock._payer_rates) >= 2, (
            f"Expected at least 2 payer rates, got {len(mock._payer_rates)}"
        )

        # Row 1: ip_price=32000.50, op_price=0 → use ip_price
        aetna_rates = [r for r in mock._payer_rates if r['payer_name'] and 'Aetna' in r['payer_name']]
        assert len(aetna_rates) >= 1, f"Expected Aetna rate, got payers: {[r['payer_name'] for r in mock._payer_rates]}"
        assert aetna_rates[0]['negotiated_rate'] == 32000.50

    def test_op_price_fallback(self):
        """When ip_price is 0, op_price should be used as fallback."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(PIPE_CSV)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1, log=lambda msg, **kw: None)

        # Row 2: ip_price=0, op_price=250.75 → fallback to op_price
        cigna_rates = [r for r in mock._payer_rates if r['payer_name'] and 'Cigna' in r['payer_name']]
        assert len(cigna_rates) >= 1, f"Expected Cigna rate, got payers: {[r['payer_name'] for r in mock._payer_rates]}"
        assert cigna_rates[0]['negotiated_rate'] == 250.75

    def test_op_estimated_amount_fallback(self):
        """When ip_expected_reimbursement is empty, op_expected_reimbursement should be used."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(PIPE_CSV)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1, log=lambda msg, **kw: None)

        # Row 2: ip_expected_reimbursement='', op_expected_reimbursement=200.00
        cigna_rates = [r for r in mock._payer_rates if r['payer_name'] and 'Cigna' in r['payer_name']]
        assert len(cigna_rates) >= 1
        assert cigna_rates[0]['estimated_amount'] == 200.0

    def test_min_max_rates(self):
        """Verify min/max negotiated rates are correctly mapped from ip_reimb columns."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(PIPE_CSV)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1, log=lambda msg, **kw: None)

        # Check that staged standard entries have min/max rates
        assert len(mock._standards) >= 1

        # Row 1: min_ip_reimb=15000.00, max_ip_reimb=45000.00
        hip_std = [s for s in mock._standards if s['min_rate'] == 15000.0]
        assert len(hip_std) >= 1, f"Expected standard with min_rate=15000, got: {mock._standards}"
        assert hip_std[0]['max_rate'] == 45000.0

    def test_self_pay_parsed(self):
        """Verify <Self-pay> payer is routed to cash price, not payer rate."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(PIPE_CSV)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1, log=lambda msg, **kw: None)

        # Row 3: payer='<Self-pay>' with ip_price=75.00
        # Self-pay should NOT appear as a payer rate, it should be
        # routed to charge_item.discounted_cash_price instead
        all_payers = [r['payer_name'] for r in mock._payer_rates]
        assert 'Self-Pay' not in all_payers, (
            f"Self-pay should not be in payer rates, got: {all_payers}"
        )
        assert '<Self-Pay>' not in all_payers
        assert '<Self-pay>' not in all_payers

        # Should have exactly 2 payer rates (Aetna and Cigna)
        assert len(mock._payer_rates) == 2, (
            f"Expected 2 payer rates (Aetna, Cigna), got {len(mock._payer_rates)}: {all_payers}"
        )

        # Self-pay rate should appear as a cash price in the staging standard
        cash_prices = [s['cash'] for s in mock._standards if s['cash'] is not None]
        assert 75.0 in cash_prices, (
            f"Expected 75.0 as cash price from self-pay row, got standards: {mock._standards}"
        )


class TestUnmappedCellSuppression:
    """Verify that low-value columns are suppressed from unmapped cell tracking."""

    def test_no_unmapped_cells_for_pipe_delimited(self):
        """All 14 unmapped Partners columns should be suppressed."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(PIPE_CSV)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1, log=lambda msg, **kw: None)

        # Every column in PIPE_CSV is either canonical or in _SUPPRESS_UNMAPPED,
        # so no unmapped cells should be staged.
        assert len(mock._unmapped) == 0, (
            f"Expected 0 unmapped cells, got {len(mock._unmapped)}: "
            f"{set(u['source_column'] for u in mock._unmapped)}"
        )

    def test_suppression_log_message(self):
        """Suppression should be logged."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(PIPE_CSV)
        logs = []

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: logs.append(msg))

        assert any('Suppressed' in msg and 'low-value' in msg for msg in logs), (
            f"Expected suppression log message, got: {logs}"
        )

    def test_unknown_columns_still_tracked(self):
        """Columns NOT in _SUPPRESS_UNMAPPED should still produce unmapped cells."""
        csv_with_extra = (
            "code|code_type|description|payer_name|plan_name|"
            "negotiated_rate|custom_hospital_field\n"
            "99213|CPT|Office Visit|Aetna|Aetna HMO|150.00|some_value\n"
        )
        mock = _make_mock_ingestor()
        handle = io.StringIO(csv_with_extra)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1, log=lambda msg, **kw: None)

        # custom_hospital_field is not canonical and not suppressed → unmapped
        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        assert 'custom_hospital_field' in unmapped_cols, (
            f"Expected 'custom_hospital_field' in unmapped, got: {unmapped_cols}"
        )

    def test_comma_csv_unmapped_unaffected(self):
        """Regular comma CSV with unknown columns should still track them."""
        csv_data = (
            "code,code_type,description,payer_name,negotiated_rate,mystery_col\n"
            "99213,CPT,Office Visit,Aetna,150.00,hello\n"
        )
        mock = _make_mock_ingestor()
        handle = io.StringIO(csv_data)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1, log=lambda msg, **kw: None)

        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        assert 'mystery_col' in unmapped_cols


class TestPatternBasedSuppression:
    """Regex-based suppression patterns in _SUPPRESS_UNMAPPED_PATTERNS."""

    def _run(self, csv_data):
        mock = _make_mock_ingestor()
        handle = io.StringIO(csv_data)
        logs = []
        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: logs.append(msg))
        return mock, logs

    def test_internal_suffix_suppressed(self):
        """Headers ending in _internal should be suppressed (Alameda/Philip Health pattern)."""
        csv_data = (
            "code,code_type,description,negotiated_rate,"
            "widget_internal,foo_bar_internal,new_future_internal\n"
            "99213,CPT,Office Visit,150.00,x,y,z\n"
        )
        mock, logs = self._run(csv_data)
        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        assert 'widget_internal' not in unmapped_cols
        assert 'foo_bar_internal' not in unmapped_cols
        assert 'new_future_internal' not in unmapped_cols

    def test_column_numeric_suppressed(self):
        """Generic column1..columnN headers should be suppressed (Watsonville pattern)."""
        csv_data = (
            "code,code_type,description,negotiated_rate,"
            "column1,column2,column15,column999\n"
            "99213,CPT,Office Visit,150.00,a,b,c,d\n"
        )
        mock, logs = self._run(csv_data)
        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        assert 'column1' not in unmapped_cols
        assert 'column2' not in unmapped_cols
        assert 'column15' not in unmapped_cols
        assert 'column999' not in unmapped_cols

    def test_bare_digit_headers_suppressed(self):
        """Bare numeric headers (data-row-as-header mis-parse) should be suppressed."""
        csv_data = (
            "code,code_type,description,negotiated_rate,1,3,42\n"
            "99213,CPT,Office Visit,150.00,a,b,c\n"
        )
        mock, logs = self._run(csv_data)
        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        assert '1' not in unmapped_cols
        assert '3' not in unmapped_cols
        assert '42' not in unmapped_cols

    def test_last_updated_suppressed(self):
        """last_updated timestamp column should be suppressed."""
        csv_data = (
            "code,code_type,description,negotiated_rate,last_updated\n"
            "99213,CPT,Office Visit,150.00,2025-01-01\n"
        )
        mock, logs = self._run(csv_data)
        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        assert 'last_updated' not in unmapped_cols

    def test_legit_column_not_suppressed(self):
        """Ensure patterns do not over-match real columns."""
        csv_data = (
            "code,code_type,description,negotiated_rate,"
            "column,internal,internally,column_foo,foo_column1\n"
            "99213,CPT,Office Visit,150.00,a,b,c,d,e\n"
        )
        mock, logs = self._run(csv_data)
        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        # None of these match the regex patterns, so they should remain unmapped
        assert 'column' in unmapped_cols
        assert 'internal' in unmapped_cols
        assert 'internally' in unmapped_cols
        assert 'column_foo' in unmapped_cols
        assert 'foo_column1' in unmapped_cols


class TestSharedSuppressionHelper:
    """Direct tests for the module-level _is_unmapped_header_suppressed()
    helper used by both CSV and JSON ingest paths.  Ensures the two code
    paths stay in sync and noisy keys do not reintroduce unmapped_cell
    volume via the JSON parser."""

    def test_exact_match_set(self):
        from mrfkit.headers import _is_unmapped_header_suppressed
        # Sample of the exact-match set, must be suppressed
        for header in [
            'op_price', 'ip_xr_detail', 'facility_id', 'cdm',
            'department', 'tabname', 'last_updated', '',
            'count_of_compared_rates', 'additional_generic_notes1',
            'revenue_code', 'allowed_amounts', 'billing_code_type_version',
            'financial_aid_policy', 'rev', 'charge_number', 'ivnum',
            'mins_per_unit',
        ]:
            assert _is_unmapped_header_suppressed(header), \
                f"expected suppression for {header!r}"

    def test_regex_patterns(self):
        from mrfkit.headers import _is_unmapped_header_suppressed
        for header in [
            'anything_internal', 'foo_bar_internal', 'widget_internal',
            'column1', 'column42', 'column999',
            '1', '42', '123456',
        ]:
            assert _is_unmapped_header_suppressed(header), \
                f"expected suppression for {header!r}"

    def test_non_matching(self):
        from mrfkit.headers import _is_unmapped_header_suppressed
        # These must NOT be suppressed so real headers keep flowing
        for header in [
            'code', 'description', 'negotiated_rate', 'gross_charge',
            'column', 'internal', 'internally', 'column_foo',
            'foo_column1', 'internal_code', 'last_updated_at',
        ]:
            assert not _is_unmapped_header_suppressed(header), \
                f"should not suppress {header!r}"



class TestProdUnmappedPatterns:
    """Regression tests for high-volume prod unmapped_cell patterns."""

    def test_ascension_estimate_columns_map_to_canonical_fields(self):
        expected = {
            'Gross_Charges_Estimate': 'gross_charge',
            'Cash_Price_Estimate': 'discounted_cash_price',
            'Insurance_Estimate': 'estimated_amount',
        }
        for header, canonical in expected.items():
            assert SYNONYM_MAP.get(normalize_header(header)) == canonical

    def test_standard_charge_estimated_discounted_cash_maps(self):
        assert SYNONYM_MAP.get(
            normalize_header('standard_charge | estimated_discounted_cash')
        ) == 'discounted_cash_price'

    def test_wide_payer_typos_are_consumed_not_unmapped(self):
        csv = (
            'code,code_type,description,gross_charge,'
            '"standard_charged|Aetna|negotiated_percentage",'
            '"standard_charged|Aetna|negotiated_algorithm",'
            '"standard_charges | Cigna|Commercial|negotiated rate_dollar"\n'
            '99213,CPT,Office Visit,100,95,contracted rate,81.50\n'
        )
        mock = _make_mock_ingestor()

        parse_csv_to_staging(
            io.StringIO(csv), mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
        )

        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        assert not unmapped_cols
        assert len(mock._payer_rates) == 2

        aetna = [r for r in mock._payer_rates if r['payer_name'] == 'Aetna'][0]
        assert aetna['negotiated_percentage'] == 95.0
        assert aetna['negotiated_algorithm'] == 'contracted rate'

        cigna = [r for r in mock._payer_rates if r['payer_name'] == 'Cigna'][0]
        assert cigna['plan_name']
        assert cigna['negotiated_rate'] == 81.5


class TestCommaDelimitedUnchanged:
    """Verify that regular comma-delimited CSV still works correctly."""

    COMMA_CSV = (
        "code,code_type,description,payer_name,plan_name,"
        "negotiated_rate,min_negotiated_rate,max_negotiated_rate\n"
        "99213,CPT,Office Visit,Aetna,Aetna HMO,"
        "150.00,100.00,200.00\n"
    )

    def test_comma_delimiter(self):
        """Regular comma CSV should not trigger pipe detection."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.COMMA_CSV)
        logs = []

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: logs.append(msg))

        assert not any('pipe-delimited' in msg for msg in logs)
        assert len(mock._items) >= 1
        codes = {(item['code'], item['code_type']) for item in mock._items}
        assert ('99213', 'CPT') in codes


class TestTabDelimited:
    """Verify tab-delimited CSV is detected and parsed."""

    TAB_CSV = (
        "code\tcode_type\tdescription\tpayer_name\tnegotiated_rate\n"
        "99213\tCPT\tOffice Visit\tAetna\t150.00\n"
    )

    def test_tab_delimiter_detection(self):
        """Tab delimiter should be detected."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.TAB_CSV)
        logs = []

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: logs.append(msg))

        assert any('tab-delimited' in msg for msg in logs)
        assert len(mock._items) >= 1


class TestCMSWideCommaDelimited:
    """
    CMS v3 wide-format CSVs are comma-delimited but have column names
    containing pipes (e.g. standard_charge|PAYER|PLAN|negotiated_dollar).
    The pipe count in the header can exceed the comma count, which previously
    caused the delimiter sniffer to misidentify them as pipe-delimited.
    """

    # Minimal CMS v3.0.0 wide format with 2-row preamble + data header + data.
    # Column names like code|1, standard_charge|[PAYER]|[PLAN]|negotiated_dollar
    # have many pipes, but the actual delimiter is comma.
    CMS_WIDE_CSV = (
        'hospital_name,last_updated_on,version,location_name,hospital_address,'
        'license_number|OR,type_2_npi,attestation,attester_name,,,,,,,,,,,,,,\n'
        'KAISER FOUNDATION HOSPITAL - WESTSIDE,2026-02-01,3.0.0,'
        'WESTSIDE MEDICAL CENTER,2875 NW STUCKI AVE HILLSBORO OR 97124,'
        '14-1472 | OR,1891048807,TRUE,Brad Tinnermon,kp.org/mfa/nw,,,,,,,,,,,,,,\n'
        'description,code|1,code|1|type,code|2,code|2|type,setting,'
        'standard_charge|gross,standard_charge|discounted_cash,'
        '"standard_charge|[KAISER FOUNDATION HEALTH PLAN, INC.]|[COMMERCIAL]|negotiated_dollar",'
        '"standard_charge|[KAISER FOUNDATION HEALTH PLAN, INC.]|[COMMERCIAL]|methodology",'
        'standard_charge|min,standard_charge|max\n'
        'HIP REPLACEMENT,469,MS-DRG,27447,CPT,inpatient,'
        '50000.00,40000.00,'
        '35000.00,Per Diem,'
        '35000.00,50000.00\n'
        'OFFICE VISIT,99213,CPT,,,outpatient,'
        '250.00,200.00,'
        '180.00,Fee Schedule,'
        '180.00,250.00\n'
    )

    def test_not_misdetected_as_pipe(self):
        """CMS wide CSV must NOT trigger pipe-delimited detection."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.CMS_WIDE_CSV)
        logs = []

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: logs.append(msg))

        assert not any('pipe-delimited' in msg for msg in logs), (
            f"CMS wide CSV was wrongly detected as pipe-delimited: {logs}"
        )

    def test_rows_parsed(self):
        """Both data rows should be parsed successfully."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.CMS_WIDE_CSV)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        assert len(mock._items) >= 2, (
            f"Expected at least 2 items, got {len(mock._items)}"
        )

    def test_wide_payer_rates_extracted(self):
        """Wide payer columns should produce payer rate rows."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.CMS_WIDE_CSV)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        assert len(mock._payer_rates) >= 2, (
            f"Expected at least 2 payer rates (one per data row), "
            f"got {len(mock._payer_rates)}"
        )
        # Verify the payer name was extracted from the wide column header
        # (may be normalized, e.g. "Kaiser Permanente" from
        # "KAISER FOUNDATION HEALTH PLAN, INC.")
        payer_names = {r['payer_name'] for r in mock._payer_rates}
        assert any('Kaiser' in (p or '') or 'KAISER' in (p or '') for p in payer_names), (
            f"Expected Kaiser payer, got: {payer_names}"
        )

    def test_code_types_correct(self):
        """Multi-code columns (code|1, code|2) should be parsed."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.CMS_WIDE_CSV)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        codes = {(item['code'], item['code_type']) for item in mock._items}
        # First row has code|1=469 (MS-DRG) and code|2=27447 (CPT)
        assert ('469', 'MS-DRG') in codes, f"Expected MS-DRG 469, got: {codes}"
        assert ('99213', 'CPT') in codes, f"Expected CPT 99213, got: {codes}"

    def test_wide_format_detected_in_logs(self):
        """CMS wide format detection should be logged."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.CMS_WIDE_CSV)
        logs = []

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: logs.append(msg))

        assert any('wide format' in msg.lower() for msg in logs), (
            f"Expected 'wide format' in logs, got: {logs}"
        )


class TestLicenseNumberStateAgnostic:
    """Verify license_number metadata extraction works for any state suffix."""

    CMS_CSV_OR = (
        'hospital_name,last_updated_on,license_number|OR\n'
        'TEST HOSPITAL,2026-01-01,14-1472 | OR\n'
        'description,code|1,code|1|type,setting,standard_charge|gross\n'
        'TEST PROCEDURE,99213,CPT,outpatient,100.00\n'
    )

    CMS_CSV_CA = (
        'hospital_name,last_updated_on,license_number|CA\n'
        'CA HOSPITAL,2026-01-01,050454\n'
        'description,code|1,code|1|type,setting,standard_charge|gross\n'
        'TEST PROCEDURE,99213,CPT,outpatient,100.00\n'
    )

    def test_oregon_license_number(self):
        """license_number|OR should be extracted (not just |CA)."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.CMS_CSV_OR)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        # Metadata is emitted with the state taken from the header suffix.
        kwargs = mock.upsert_hospital_locations.call_args.kwargs
        assert kwargs['license_state'] == 'OR'
        assert kwargs['license_number'] == '14-1472'

    def test_california_license_number(self):
        """license_number|CA should still work."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.CMS_CSV_CA)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        kwargs = mock.upsert_hospital_locations.call_args.kwargs
        assert kwargs['license_state'] == 'CA'
        assert kwargs['license_number'] == '050454', (
            "Expected the CA license number in the metadata"
        )


class TestFootnoteTallFormat:
    """Verify footnote column is extracted in tall-format CSV parsing."""

    TALL_CSV_WITH_FOOTNOTE = (
        "code,code_type,description,payer_name,plan_name,"
        "negotiated_rate,footnote\n"
        "99213,CPT,Office Visit,Aetna,Aetna HMO,"
        "150.00,Lesser of charges or contracted rate\n"
        "99214,CPT,Office Visit Extended,BCBS,BCBS PPO,"
        "200.00,Bundled with 99213 when billed same day\n"
        "99215,CPT,Office Visit Complex,Cigna,Cigna HMO,"
        "250.00,\n"
    )

    def test_footnote_passed_to_payer_rate(self):
        """Footnote from tall CSV should flow into stage_payer_rate."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.TALL_CSV_WITH_FOOTNOTE)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        footnotes = [r['footnote'] for r in mock._payer_rates]
        assert any(fn and 'Lesser of charges' in fn for fn in footnotes), (
            f"Expected 'Lesser of charges' footnote, got: {footnotes}"
        )
        assert any(fn and 'Bundled' in fn for fn in footnotes), (
            f"Expected 'Bundled' footnote, got: {footnotes}"
        )

    def test_empty_footnote_is_none(self):
        """Empty footnote cell should yield None, not empty string."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.TALL_CSV_WITH_FOOTNOTE)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        # Third row has empty footnote
        cigna_rates = [r for r in mock._payer_rates if 'Cigna' in (r['payer_name'] or '')]
        assert len(cigna_rates) >= 1, "Expected at least one Cigna rate"
        assert cigna_rates[0]['footnote'] is None, (
            f"Expected None for empty footnote, got: {cigna_rates[0]['footnote']!r}"
        )

    def test_footnote_not_in_unmapped(self):
        """footnote column should be canonical, not tracked as unmapped."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.TALL_CSV_WITH_FOOTNOTE)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        assert 'footnote' not in unmapped_cols, (
            f"footnote should be canonical, not unmapped: {unmapped_cols}"
        )


class TestSuppressedColumnSuppression:
    """Verify suppressed columns (e.g. department) don't appear in unmapped cells."""

    CSV_WITH_DEPARTMENT = (
        "code,code_type,description,payer_name,plan_name,"
        "negotiated_rate,department\n"
        "99213,CPT,Office Visit,Aetna,Aetna HMO,"
        "150.00,Radiology\n"
    )

    def test_department_suppressed(self):
        """department should NOT appear in unmapped cells."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.CSV_WITH_DEPARTMENT)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        assert 'department' not in unmapped_cols, (
            f"department should be suppressed, "
            f"got unmapped: {unmapped_cols}"
        )

    def test_suppression_logged(self):
        """Suppression of department should be logged."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.CSV_WITH_DEPARTMENT)
        logs = []

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: logs.append(msg))

        assert any(
            'department' in msg.lower()
            and ('suppressed' in msg.lower() or 'low-value' in msg.lower())
            for msg in logs
        ), (
            f"Expected department suppression log entry, got: {logs}"
        )


class TestFootnoteWideFormat:
    """Verify footnote flows through CMS wide-format CSV."""

    CMS_WIDE_WITH_FOOTNOTE = (
        'hospital_name,last_updated_on\n'
        'TEST HOSPITAL,2026-01-01\n'
        'description,code|1,code|1|type,setting,'
        'standard_charge|gross,standard_charge|discounted_cash,'
        '"standard_charge|[AETNA]|[HMO PLAN]|negotiated_dollar",'
        '"standard_charge|[AETNA]|[HMO PLAN]|methodology",'
        'footnote\n'
        'OFFICE VISIT,99213,CPT,outpatient,'
        '250.00,200.00,'
        '180.00,Fee Schedule,'
        'Lesser of charges or contracted rate\n'
    )

    def test_wide_format_footnote_passed(self):
        """Row-level footnote should propagate to wide-format payer rates."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.CMS_WIDE_WITH_FOOTNOTE)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        assert len(mock._payer_rates) >= 1, "Expected at least 1 payer rate"
        footnotes = [r['footnote'] for r in mock._payer_rates]
        assert any(fn and 'Lesser of charges' in fn for fn in footnotes), (
            f"Expected footnote fallback to wide payer, got: {footnotes}"
        )


# ===================================================================
# Header overrides (ingest_config)
# ===================================================================

class TestHeaderOverrides:
    """Verify ingest_config header_overrides remap unmapped headers."""

    CSV_WITH_TYPE = (
        "code,description,Type,gross_charge,discounted_cash_price\n"
        "99213,Office Visit,CPT,250.00,200.00\n"
        "469,Hip Replacement,DRG,50000.00,40000.00\n"
        "J0171,Adrenalin Injection,HCPCS,75.00,60.00\n"
    )

    def test_type_override_to_code_type(self):
        """Type column remapped to code_type via header_overrides."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.CSV_WITH_TYPE)

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
            ingest_config={"header_overrides": {"Type": "code_type"}},
        )

        code_types = {(item['code'], item['code_type']) for item in mock._items}
        # Type column says CPT for 99213
        assert ('99213', 'CPT') in code_types, f"Expected CPT code_type, got: {code_types}"
        # Type column says DRG for 469
        assert any(ct == 'DRG' or ct == 'MS-DRG' for c, ct in code_types if c == '469'), (
            f"Expected DRG code_type for 469, got: {code_types}"
        )

    def test_type_not_in_unmapped(self):
        """Type column should NOT appear as unmapped when overridden."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.CSV_WITH_TYPE)

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
            ingest_config={"header_overrides": {"Type": "code_type"}},
        )

        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        assert 'Type' not in unmapped_cols, (
            f"Type should be overridden, not unmapped: {unmapped_cols}"
        )

    def test_override_logged(self):
        """Header override application should be logged."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.CSV_WITH_TYPE)
        logs = []

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: logs.append(msg),
            ingest_config={"header_overrides": {"Type": "code_type"}},
        )

        assert any('override' in msg.lower() and 'Type' in msg for msg in logs), (
            f"Expected override log mentioning 'Type', got: {logs}"
        )

    def test_without_override_type_is_unmapped(self):
        """Without header_overrides, Type column should be unmapped."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.CSV_WITH_TYPE)

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
        )

        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        assert 'Type' in unmapped_cols, (
            f"Expected 'Type' in unmapped without override, got: {unmapped_cols}"
        )

    def test_invalid_target_ignored(self):
        """header_overrides with invalid canonical target should be ignored."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.CSV_WITH_TYPE)
        logs = []

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: logs.append(msg),
            ingest_config={"header_overrides": {"Type": "not_a_real_field"}},
        )

        # Type should still be unmapped since the target is invalid
        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        assert 'Type' in unmapped_cols, (
            f"Type should remain unmapped with invalid target: {unmapped_cols}"
        )
        # Should log a warning
        assert any('WARNING' in msg and 'not_a_real_field' in msg for msg in logs), (
            f"Expected warning about invalid target, got: {logs}"
        )

    def test_override_updates_header_mapping(self):
        """Header overrides should update header_mapping audit trail."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.CSV_WITH_TYPE)

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
            ingest_config={"header_overrides": {"Type": "code_type"}},
        )

        assert header_mappings(mock)['Type'] == 'code_type'


# ===================================================================
# DB-backed synonym map (db_synonyms parameter)
# ===================================================================

class TestDbSynonyms:
    """Verify db_synonyms parameter remaps unmapped headers during ingestion."""

    CSV_WITH_CUSTOM = (
        "code,description,charge_amount,self_pay_rate\n"
        "99213,Office Visit,250.00,200.00\n"
        "99214,Extended Visit,350.00,280.00\n"
    )

    def test_db_synonym_maps_header(self):
        """DB synonym maps charge_amount -> gross_charge, which goes to stage_charge_standard."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.CSV_WITH_CUSTOM)

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
            db_synonyms={"charge_amount": "gross_charge",
                         "self_pay_rate": "discounted_cash_price"},
        )

        # gross_charge goes into stage_charge_standard as `gross`
        assert mock.stage_charge_standard.called, "stage_charge_standard should have been called"
        for std in mock._standards:
            assert std['gross'] is not None, (
                f"Expected gross from db_synonym charge_amount, got: {std}")

    def test_db_synonym_not_in_unmapped(self):
        """Headers mapped via db_synonyms should not appear in unmapped."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.CSV_WITH_CUSTOM)

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
            db_synonyms={"charge_amount": "gross_charge",
                         "self_pay_rate": "discounted_cash_price"},
        )

        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        assert "charge_amount" not in unmapped_cols
        assert "self_pay_rate" not in unmapped_cols

    def test_db_synonym_updates_header_mapping(self):
        """DB synonym application calls update_header_mapping for audit."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.CSV_WITH_CUSTOM)

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
            db_synonyms={"charge_amount": "gross_charge"},
        )

        assert header_mappings(mock)["charge_amount"] == "gross_charge"

    def test_db_synonym_logged(self):
        """DB synonym application should be logged."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.CSV_WITH_CUSTOM)
        logs = []

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: logs.append(msg),
            db_synonyms={"charge_amount": "gross_charge"},
        )

        log_text = "\n".join(str(l) for l in logs)
        assert "charge_amount" in log_text, f"Expected charge_amount in logs, got: {log_text[:500]}"

    def test_header_override_takes_precedence(self):
        """Source header_overrides should take precedence over db_synonyms."""
        mock = _make_mock_ingestor()
        # CSV with a "Type" column that's unmapped by SYNONYM_MAP
        csv = (
            "code,description,Type,gross_charge\n"
            "99213,Office Visit,CPT,250.00\n"
        )
        handle = io.StringIO(csv)

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
            ingest_config={"header_overrides": {"Type": "code_type"}},
            db_synonyms={"type": "description"},  # lower precedence
        )

        # Override should win: Type -> code_type (not description)
        code_types = {item.get('code_type') for item in mock._items}
        assert 'CPT' in code_types, f"Expected code_type=CPT from override, got: {code_types}"

    def test_without_db_synonyms_stays_unmapped(self):
        """Without db_synonyms, custom headers remain unmapped."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.CSV_WITH_CUSTOM)

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
        )

        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        assert "charge_amount" in unmapped_cols

    def test_invalid_canonical_field_ignored(self):
        """DB synonym with invalid canonical field is silently ignored."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.CSV_WITH_CUSTOM)
        logs = []

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: logs.append(msg),
            db_synonyms={"charge_amount": "not_a_real_field"},
        )

        # charge_amount should still be unmapped
        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        assert "charge_amount" in unmapped_cols

    def test_empty_db_synonyms_is_noop(self):
        """Empty db_synonyms dict doesn't cause errors."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.CSV_WITH_CUSTOM)

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
            db_synonyms={},
        )

        # Should behave same as no db_synonyms
        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        assert "charge_amount" in unmapped_cols


# ===================================================================
# Simple wide payer auto-detection
# ===================================================================

class TestSimpleWidePayer:
    """Verify auto-detection of simple wide-format payer columns."""

    # South Texas-style: code, description, Type, gross/cash, then payer columns
    SIMPLE_WIDE_CSV = (
        "code,description,gross_charge,discounted_cash_price,"
        "AETNA COMMERCIAL,CIGNA PPO,BCBS HMO,UNITED HEALTHCARE\n"
        "99213,Office Visit,250.00,200.00,"
        "$180.00,$175.00,$190.00,$185.00\n"
        "99214,Office Visit Extended,350.00,280.00,"
        "$250.00,$245.00,$260.00,$255.00\n"
        "99215,Office Visit Complex,450.00,360.00,"
        "$320.00,Packaged,$340.00,$-\n"
    )

    def test_payer_columns_detected(self):
        """Payer columns with $-values should be auto-detected."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.SIMPLE_WIDE_CSV)
        logs = []

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: logs.append(msg),
        )

        assert any('auto-detected' in msg.lower() for msg in logs), (
            f"Expected auto-detection log, got: {logs}"
        )

    def test_payer_rates_created(self):
        """Each payer column x data row should produce a payer rate."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.SIMPLE_WIDE_CSV)

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
        )

        # 4 payers x 3 rows = 12 potential rates, but $- and Packaged
        # produce None rates which may or may not be staged
        assert len(mock._payer_rates) >= 8, (
            f"Expected at least 8 payer rates (valid $-values), "
            f"got {len(mock._payer_rates)}"
        )

    def test_payer_names_from_headers(self):
        """Payer names should come from column headers."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.SIMPLE_WIDE_CSV)

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
        )

        payer_names = {r['payer_name'] for r in mock._payer_rates}
        # normalize_payer_name may normalize these, but check at least some match
        assert len(payer_names) >= 3, (
            f"Expected at least 3 distinct payer names, got: {payer_names}"
        )

    def test_dollar_values_parsed(self):
        """$180.00 should be parsed to 180.0."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.SIMPLE_WIDE_CSV)

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
        )

        rates = [r['negotiated_rate'] for r in mock._payer_rates
                 if r['negotiated_rate'] is not None]
        assert 180.0 in rates, f"Expected 180.0 in rates, got: {rates}"

    def test_packaged_and_dash_produce_no_rate(self):
        """'Packaged' and '$-' should not produce numeric rates."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.SIMPLE_WIDE_CSV)

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
        )

        # Row 3: CIGNA PPO = Packaged, UNITED = $-
        # These should either not produce payer rates or produce None rates
        rates = [r['negotiated_rate'] for r in mock._payer_rates]
        # None values are acceptable (null rate), but numeric rates should
        # NOT include values from 'Packaged' or '$-'
        numeric_rates = [r for r in rates if r is not None]
        for rate in numeric_rates:
            assert rate > 0, f"Expected positive rates, got: {rate}"

    def test_payer_columns_not_in_unmapped(self):
        """Auto-detected payer columns should NOT appear as unmapped."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.SIMPLE_WIDE_CSV)

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
        )

        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        assert 'AETNA COMMERCIAL' not in unmapped_cols, (
            f"Payer cols should not be unmapped: {unmapped_cols}"
        )
        assert 'CIGNA PPO' not in unmapped_cols

    def test_no_detection_with_too_few_payer_cols(self):
        """If fewer than 3 columns have dollar values, don't auto-detect."""
        csv_few = (
            "code,description,gross_charge,discounted_cash_price,"
            "AETNA,MYSTERY\n"
            "99213,Office Visit,250.00,200.00,"
            "$180.00,not_a_price\n"
        )
        mock = _make_mock_ingestor()
        handle = io.StringIO(csv_few)
        logs = []

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: logs.append(msg),
        )

        assert not any('auto-detected' in msg.lower() for msg in logs), (
            f"Should NOT auto-detect with <3 payer columns: {logs}"
        )

    def test_no_detection_when_tall_format(self):
        """If payer_name column exists, don't trigger auto-detection."""
        csv_tall = (
            "code,description,payer_name,plan_name,negotiated_rate,"
            "EXTRA_COL_A,EXTRA_COL_B,EXTRA_COL_C\n"
            "99213,Office Visit,Aetna,HMO,150.00,"
            "$10,$20,$30\n"
        )
        mock = _make_mock_ingestor()
        handle = io.StringIO(csv_tall)
        logs = []

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: logs.append(msg),
        )

        assert not any('auto-detected' in msg.lower() for msg in logs), (
            f"Should NOT auto-detect when payer_name column exists: {logs}"
        )

    def test_auto_detection_updates_header_mapping(self):
        """Auto-detected payer columns should update header_mapping audit trail."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.SIMPLE_WIDE_CSV)

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
        )

        mapped = header_mappings(mock)
        for header in ('AETNA COMMERCIAL', 'CIGNA PPO', 'BCBS HMO', 'UNITED HEALTHCARE'):
            assert mapped[header] == 'wide_payer:negotiated_rate'


# ===================================================================
# Suppression of cdm and procedure_id
# ===================================================================

class TestSuppressNewColumns:
    """Verify cdm and procedure_id columns are suppressed from unmapped tracking."""

    CSV_WITH_CDM_PROCID = (
        "code,code_type,description,gross_charge,CDM,Procedure ID\n"
        "99213,CPT,Office Visit,250.00,12345,PROC-001\n"
        "99214,CPT,Office Visit Ext,350.00,67890,PROC-002\n"
    )

    def test_cdm_suppressed(self):
        """CDM column should not appear in unmapped cells."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.CSV_WITH_CDM_PROCID)

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
        )

        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        assert 'CDM' not in unmapped_cols, (
            f"CDM should be suppressed, got unmapped: {unmapped_cols}"
        )

    def test_procedure_id_suppressed(self):
        """Procedure ID column should not appear in unmapped cells."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.CSV_WITH_CDM_PROCID)

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
        )

        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        assert 'Procedure ID' not in unmapped_cols, (
            f"Procedure ID should be suppressed, got unmapped: {unmapped_cols}"
        )

    def test_suppression_logged(self):
        """Suppression of cdm/procedure_id should appear in log."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.CSV_WITH_CDM_PROCID)
        logs = []

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: logs.append(msg),
        )

        assert any('cdm' in msg.lower() or 'procedure_id' in msg.lower()
                    for msg in logs if 'Suppressed' in msg or 'suppressed' in msg), (
            f"Expected suppression log for cdm/procedure_id, got: {logs}"
        )


# ===================================================================
# Suppression of mnemonic / neumonic columns
# ===================================================================

class TestSuppressMnemonic:
    """Verify mnemonic/neumonic columns are suppressed from unmapped tracking."""

    CSV_WITH_NEUMONIC = (
        "Facility,NEUMONIC,description,Gross Price\n"
        "Cornerstone,300880182,Office Visit,250.00\n"
        "Cornerstone,1606867,Office Visit Ext,350.00\n"
    )

    CSV_WITH_MNEMONIC = (
        "code,code_type,description,gross_charge,mnemonic\n"
        "99213,CPT,Office Visit,250.00,ABC123\n"
        "99214,CPT,Office Visit Ext,350.00,DEF456\n"
    )

    def test_neumonic_suppressed(self):
        """NEUMONIC column (misspelling from Cornerstone) should be suppressed."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.CSV_WITH_NEUMONIC)

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
        )

        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        assert 'NEUMONIC' not in unmapped_cols, (
            f"NEUMONIC should be suppressed, got unmapped: {unmapped_cols}"
        )

    def test_mnemonic_suppressed(self):
        """Correctly-spelled mnemonic column should also be suppressed."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.CSV_WITH_MNEMONIC)

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
        )

        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        assert 'mnemonic' not in unmapped_cols, (
            f"mnemonic should be suppressed, got unmapped: {unmapped_cols}"
        )


# ===================================================================
# Combined South Texas scenario
# ===================================================================

class TestSouthTexasCombined:
    """Combined test: header_overrides + simple wide payer + suppression."""

    SOUTH_TEXAS_CSV = (
        "Facility,CDM,description,code,Type,gross_charge,discounted_cash_price,"
        "AETNA COMMERCIAL,CIGNA PPO,BCBS HMO,UNITED HEALTHCARE\n"
        "South Texas Medical,12345,Office Visit,99213,CPT,250.00,200.00,"
        "$180.00,$175.00,$190.00,$185.00\n"
        "South Texas Medical,67890,Hip Replacement,469,DRG,50000.00,40000.00,"
        "$35000.00,$32000.00,$38000.00,$36000.00\n"
    )

    def test_combined_scenario(self):
        """Override Type, suppress CDM, auto-detect payer columns."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.SOUTH_TEXAS_CSV)
        logs = []

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: logs.append(msg),
            ingest_config={"header_overrides": {"Type": "code_type"}},
        )

        # 1. code_type should come from Type column
        code_types = {(item['code'], item['code_type']) for item in mock._items}
        assert ('99213', 'CPT') in code_types, f"Expected CPT, got: {code_types}"

        # 2. CDM should be suppressed
        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        assert 'CDM' not in unmapped_cols, f"CDM should be suppressed: {unmapped_cols}"

        # 3. Payer rates should be created
        assert len(mock._payer_rates) >= 6, (
            f"Expected at least 6 payer rates (4 payers x 2 rows minus nulls), "
            f"got {len(mock._payer_rates)}"
        )

        # 4. Payer columns should not be unmapped
        assert 'AETNA COMMERCIAL' not in unmapped_cols
        assert 'CIGNA PPO' not in unmapped_cols

        # 5. Type should not be unmapped (was overridden)
        assert 'Type' not in unmapped_cols

    def test_combined_payer_values(self):
        """Verify actual dollar values are parsed correctly in combined scenario."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.SOUTH_TEXAS_CSV)

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
            ingest_config={"header_overrides": {"Type": "code_type"}},
        )

        rates = [r['negotiated_rate'] for r in mock._payer_rates
                 if r['negotiated_rate'] is not None]
        assert 180.0 in rates, f"Expected $180.00 parsed as 180.0, got: {rates}"
        assert 35000.0 in rates, f"Expected $35000.00 parsed as 35000.0, got: {rates}"


# ===================================================================
# Hawaii wide format (setting-specific columns + payer headers)
# ===================================================================

class TestHawaiiWideFormat:
    """Verify Hawaii denormalized wide format is pivoted to tall format."""

    HAWAII_CSV = (
        "Description,ProcedureCode,code_type,"
        "InpatientGrossCharge,OutpatientGrossCharge,EmergencyRoomGrossCharge,"
        "DiscountedCashPriceInpatient,DiscountedCashPriceOutpatient,"
        "DiscountedCashPriceEmergencyRoom,"
        "MinimumNegotiatedCharge,MaximumNegotiatedCharge,"
        "SomePayer_Inpatient_FeeSchedule,"
        "SomePayer_Outpatient_PercentofCharges,"
        "SomePayer_EmergencyRoom_PerDiem,"
        "RecordType,FileInformation\n"
        "Hip Replacement,27447,CPT,"
        "50000.00,35000.00,45000.00,"
        "40000.00,28000.00,36000.00,"
        "15000.00,55000.00,"
        "32000.50,0.85,1200.00,"
        "5,N/A\n"
        "Blood Test,85025,CPT,"
        "100.00,N/A,N/A,"
        "80.00,N/A,N/A,"
        "50.00,120.00,"
        "N/A,N/A,N/A,"
        "5,N/A\n"
    )

    def test_hawaii_format_detected(self):
        """Hawaii wide format should be detected via setting-specific columns."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.HAWAII_CSV)
        logs = []

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: logs.append(msg))

        assert any('hawaii wide format' in msg.lower() for msg in logs), (
            f"Expected Hawaii detection log, got: {logs}"
        )

    def test_multiple_settings_emitted(self):
        """Row 1 has all 3 settings non-N/A, should produce 3 charge_items."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.HAWAII_CSV)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        # Row 1 has 3 non-N/A settings; Row 2 has only inpatient
        settings = [item['setting'] for item in mock._items]
        assert 'inpatient' in settings, f"Expected inpatient, got: {settings}"
        assert 'outpatient' in settings, f"Expected outpatient, got: {settings}"
        assert 'emergency' in settings, f"Expected emergency, got: {settings}"

    def test_na_settings_skipped(self):
        """Row 2 has N/A for outpatient/emergency, should only emit inpatient."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.HAWAII_CSV)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        # Row 2 (Blood Test) should only have inpatient setting
        blood_items = [item for item in mock._items if item['code'] == '85025']
        blood_settings = {item['setting'] for item in blood_items}
        assert blood_settings == {'inpatient'}, (
            f"Blood Test should only have inpatient, got: {blood_settings}"
        )

    def test_charge_standards_per_setting(self):
        """Each setting should get its own staged standard with correct gross/cash."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.HAWAII_CSV)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        # Row 1: 3 settings, each with different gross/cash
        assert len(mock._standards) >= 3, (
            f"Expected at least 3 staged standards, got {len(mock._standards)}"
        )

        gross_values = sorted([s['gross'] for s in mock._standards if s['gross'] is not None])
        # Should have 50000 (inpatient), 35000 (outpatient), 45000 (emergency),
        # 100 (blood test inpatient)
        assert 50000.0 in gross_values, f"Expected 50000.0, got: {gross_values}"
        assert 35000.0 in gross_values, f"Expected 35000.0, got: {gross_values}"
        assert 45000.0 in gross_values, f"Expected 45000.0, got: {gross_values}"

    def test_shared_min_max_rates(self):
        """min/max negotiated rates are shared across all settings."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.HAWAII_CSV)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        # All staged standards for row 1 should have same min/max
        hip_stds = [s for s in mock._standards if s['min_rate'] == 15000.0]
        assert len(hip_stds) >= 3, (
            f"Expected 3 standards with min_rate=15000 (one per setting), "
            f"got {len(hip_stds)}"
        )
        for s in hip_stds:
            assert s['max_rate'] == 55000.0

    def test_payer_rates_with_setting(self):
        """Hawaii payer columns should produce payer rates with correct settings."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.HAWAII_CSV)

        # Extend mock to capture item_setting and methodology
        payer_rates_extended = []
        orig_fake = mock.stage_payer_rate.side_effect

        def extended_fake(*args, **kwargs):
            orig_fake(*args, **kwargs)
            payer_rates_extended.append({
                'code': args[0],
                'item_setting': kwargs.get('item_setting'),
                'methodology': kwargs.get('methodology'),
                'negotiated_rate': kwargs.get('negotiated_rate'),
                'negotiated_percentage': kwargs.get('negotiated_percentage'),
            })

        mock.stage_payer_rate.side_effect = extended_fake

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        # Row 1 has 3 payer columns, all non-N/A → 3 payer rates
        assert len(payer_rates_extended) >= 3, (
            f"Expected at least 3 payer rates, got {len(payer_rates_extended)}"
        )

        settings = {r['item_setting'] for r in payer_rates_extended}
        assert 'inpatient' in settings
        assert 'outpatient' in settings
        assert 'emergency' in settings

    def test_percent_of_charges_methodology(self):
        """PercentofCharges columns should set negotiated_percentage, not rate."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.HAWAII_CSV)

        payer_rates_extended = []
        orig_fake = mock.stage_payer_rate.side_effect

        def extended_fake(*args, **kwargs):
            orig_fake(*args, **kwargs)
            payer_rates_extended.append({
                'item_setting': kwargs.get('item_setting'),
                'methodology': kwargs.get('methodology'),
                'negotiated_rate': kwargs.get('negotiated_rate'),
                'negotiated_percentage': kwargs.get('negotiated_percentage'),
            })

        mock.stage_payer_rate.side_effect = extended_fake

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        pct_rates = [r for r in payer_rates_extended
                     if r['methodology'] == 'percent_of_charge']
        assert len(pct_rates) >= 1, (
            f"Expected percent_of_charge rates, got methodologies: "
            f"{[r['methodology'] for r in payer_rates_extended]}"
        )
        for r in pct_rates:
            assert r['negotiated_percentage'] is not None, (
                "percent_of_charge should set negotiated_percentage"
            )
            assert r['negotiated_rate'] is None, (
                "percent_of_charge should NOT set negotiated_rate"
            )
            # 0.85 in source → normalized to 85.0 (0-100 range)
            assert r['negotiated_percentage'] == 85.0, (
                f"Expected 85.0 (normalized from 0.85), got {r['negotiated_percentage']}"
            )

    def test_na_payer_values_skipped(self):
        """N/A payer values should not produce payer rate rows."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.HAWAII_CSV)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        # Row 2 (Blood Test) has all N/A payer columns → no payer rates
        blood_rates = [r for r in mock._payer_rates if r['code'] == '85025']
        assert len(blood_rates) == 0, (
            f"Blood Test should have no payer rates (all N/A), got: {blood_rates}"
        )

    def test_setting_columns_not_unmapped(self):
        """Hawaii setting-specific columns should not appear as unmapped."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.HAWAII_CSV)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        assert 'InpatientGrossCharge' not in unmapped_cols
        assert 'OutpatientGrossCharge' not in unmapped_cols
        assert 'EmergencyRoomGrossCharge' not in unmapped_cols
        assert 'DiscountedCashPriceInpatient' not in unmapped_cols

    def test_payer_columns_not_unmapped(self):
        """Hawaii payer columns should not appear as unmapped."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.HAWAII_CSV)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        assert 'SomePayer_Inpatient_FeeSchedule' not in unmapped_cols
        assert 'SomePayer_Outpatient_PercentofCharges' not in unmapped_cols
        assert 'SomePayer_EmergencyRoom_PerDiem' not in unmapped_cols

    def test_na_unmapped_skipped(self):
        """N/A values in remaining unmapped columns should not be staged."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.HAWAII_CSV)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        # FileInformation is N/A in both rows → should not appear in unmapped
        na_unmapped = [u for u in mock._unmapped
                       if u['source_column'] == 'FileInformation']
        assert len(na_unmapped) == 0, (
            f"N/A values should not be staged as unmapped: {na_unmapped}"
        )

    def test_procedure_code_mapped(self):
        """ProcedureCode should be mapped to code via SYNONYM_MAP."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.HAWAII_CSV)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        codes = {item['code'] for item in mock._items}
        assert '27447' in codes, f"Expected code 27447 from ProcedureCode, got: {codes}"
        assert '85025' in codes, f"Expected code 85025 from ProcedureCode, got: {codes}"

    def test_unmapped_cells_use_emitted_setting(self):
        """Unmapped cells should use the first emitted setting, not empty string."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.HAWAII_CSV)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        for u in mock._unmapped:
            assert u['item_setting'] != '', (
                f"Unmapped cell should have a real setting, got empty for "
                f"column={u['source_column']}"
            )
            assert u['item_setting'] in ('inpatient', 'outpatient', 'emergency'), (
                f"Unexpected unmapped setting: {u['item_setting']}"
            )

    def test_payer_rates_only_for_emitted_settings(self):
        """Payer rates should only be emitted for settings with charge_items."""
        mock = _make_mock_ingestor()
        # Row with only inpatient data (outpatient/emergency N/A)
        csv = (
            "Description,ProcedureCode,code_type,"
            "InpatientGrossCharge,OutpatientGrossCharge,EmergencyRoomGrossCharge,"
            "DiscountedCashPriceInpatient,DiscountedCashPriceOutpatient,"
            "DiscountedCashPriceEmergencyRoom,"
            "MinimumNegotiatedCharge,MaximumNegotiatedCharge,"
            "SomePayer_Inpatient_FeeSchedule,"
            "SomePayer_Outpatient_FeeSchedule,"
            "SomePayer_EmergencyRoom_FeeSchedule\n"
            "Test,12345,CPT,"
            "100.00,N/A,N/A,"
            "80.00,N/A,N/A,"
            "50.00,120.00,"
            "90.00,95.00,110.00\n"
        )
        handle = io.StringIO(csv)

        payer_settings = []
        orig_fake = mock.stage_payer_rate.side_effect

        def capture_fake(*args, **kwargs):
            orig_fake(*args, **kwargs)
            payer_settings.append(kwargs.get('item_setting'))

        mock.stage_payer_rate.side_effect = capture_fake

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        # Only inpatient charge_item was emitted, so only inpatient
        # payer rate should exist (outpatient/emergency have non-N/A payer
        # values but no charge_item to attach to).
        assert set(payer_settings) == {'inpatient'}, (
            f"Payer rates should only be for inpatient, got: {payer_settings}"
        )


# ===================================================================
# Paris outpatient price column
# ===================================================================

class TestParisOutpatientPrice:
    """Verify OutPatient Price creates a second charge_item with setting=outpatient."""

    PARIS_CSV = (
        "HCPCS,description,Gross Price,OutPatient Price\n"
        "99213,Office Visit,250.00,200.00\n"
        "99214,Office Visit Ext,350.00,280.00\n"
        "99215,Office Visit Complex,450.00,\n"
    )

    def test_outpatient_price_detected(self):
        """OutPatient Price column should be detected in logs."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.PARIS_CSV)
        logs = []

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: logs.append(msg))

        assert any('outpatient price' in msg.lower() for msg in logs), (
            f"Expected outpatient price detection log, got: {logs}"
        )

    def test_outpatient_items_created(self):
        """Rows with OutPatient Price should produce items with setting=outpatient."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.PARIS_CSV)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        op_items = [item for item in mock._items if item['setting'] == 'outpatient']
        assert len(op_items) >= 2, (
            f"Expected at least 2 outpatient items (rows 1 & 2), got {len(op_items)}"
        )

    def test_empty_outpatient_no_item(self):
        """Row 3 with empty OutPatient Price should not produce outpatient item."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.PARIS_CSV)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        # Row 3 (99215) has empty outpatient price
        op_items = [item for item in mock._items
                    if item['setting'] == 'outpatient' and item['code'] == '99215']
        assert len(op_items) == 0, (
            f"99215 should not have outpatient item (empty price): {op_items}"
        )

    def test_outpatient_charge_standard(self):
        """Outpatient items should have staged standard with outpatient gross."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.PARIS_CSV)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        # Should have standards with gross=200.00 and gross=280.00 (outpatient)
        gross_values = [s['gross'] for s in mock._standards if s['gross'] is not None]
        assert 200.0 in gross_values, f"Expected 200.0 outpatient gross, got: {gross_values}"
        assert 280.0 in gross_values, f"Expected 280.0 outpatient gross, got: {gross_values}"

    def test_outpatient_price_not_unmapped(self):
        """OutPatient Price should not appear as unmapped."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.PARIS_CSV)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        assert 'OutPatient Price' not in unmapped_cols, (
            f"OutPatient Price should be consumed, not unmapped: {unmapped_cols}"
        )

    def test_original_gross_preserved(self):
        """Original Gross Price should still produce items without outpatient setting."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.PARIS_CSV)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        # Original items should have empty or no setting (just gross_charge)
        non_op_items = [item for item in mock._items if item['setting'] != 'outpatient']
        assert len(non_op_items) >= 3, (
            f"Expected at least 3 original items (without outpatient setting), "
            f"got {len(non_op_items)}"
        )

    def test_hcpcs_code_mapped(self):
        """HCPCS column should be mapped to code via Part 1 synonym."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.PARIS_CSV)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        codes = {item['code'] for item in mock._items}
        assert '99213' in codes, f"Expected 99213 from HCPCS column, got: {codes}"


class TestAtriumWideFormat:
    """Verify Atrium Health denormalized wide format with setting-specific columns."""

    # Simulates the Hospital "All Payors" section, tall payer format with
    # setting-specific charge columns and a Min/Max indicator.
    ATRIUM_HOSP_ALL_PAYORS = (
        "Code,Code Type,Procedure Description,Payer,Plan,"
        " Inpatient Gross Charge , Outpatient Gross Charge ,"
        " Inpatient Negotiated Charge , Outpatient Negotiated Charge ,"
        "Min /Max,TabName,Rev Code,Procedure\n"
        "99213,CPT,Office Visit,Aetna,Commercial,"
        "150.00,120.00,"
        "100.00,80.00,"
        "MIN,Hosp Std. Charges_All Payors,0510,1100000001\n"
        "99213,CPT,Office Visit,Aetna,Commercial,"
        "150.00,120.00,"
        "110.00,90.00,"
        "MAX,Hosp Std. Charges_All Payors,0510,1100000001\n"
        "27447,CPT,Hip Replacement,BCBS,PPO,"
        "50000.00,N/A,"
        "35000.00,N/A,"
        "MIN,Hosp Std. Charges_All Payors,0360,1100000002\n"
    )

    # Simulates the Hospital "Discounted Cash Price" section, self-pay rows
    # with Inpatient/Outpatient Discounted Charge columns.
    ATRIUM_HOSP_CASH = (
        "Code,Code Type,Procedure Description,Payer,Plan,"
        " Inpatient Gross Charge , Outpatient Gross Charge ,"
        " Inpatient Discounted Charge , Outpatient Discounted Charge ,"
        "Min /Max,TabName\n"
        "99213,CPT,Office Visit,Self Pay,Self Pay,"
        "150.00,120.00,"
        "130.00,100.00,"
        ",Hosp Discounted Cash Price\n"
    )

    # Simulates the Hospital "Deidentified Min Max" section, no payer, just
    # setting-specific negotiated charges with Min/Max indicator.
    ATRIUM_HOSP_MINMAX = (
        "Code,Code Type,Procedure Description,"
        " Inpatient Gross Charge , Outpatient Gross Charge ,"
        " Inpatient Negotiated Charge , Outpatient Negotiated Charge ,"
        "Min /Max,TabName\n"
        "99213,CPT,Office Visit,"
        "150.00,120.00,"
        "90.00,75.00,"
        "MIN,Hosp Deidentified Payor Min Max\n"
        "99213,CPT,Office Visit,"
        "150.00,120.00,"
        "115.00,95.00,"
        "MAX,Hosp Deidentified Payor Min Max\n"
    )

    # Simulates the Professional section with Facility/Non-Facility columns.
    ATRIUM_PROF_ALL_PAYORS = (
        "Code,Code Type,Procedure Description,Payer,Product,"
        " Gross Charge - Facility , Gross Charge - Non-Facility ,"
        " Negotiated Charge - Facility , Negotiated Charge - Non-Facility ,"
        " Inpatient Gross Charge , Outpatient Gross Charge ,"
        "TabName\n"
        "99213,CPT,Office Visit,Aetna,HMO,"
        "200.00,150.00,"
        "160.00,120.00,"
        "N/A,N/A,"
        "Prof Std Charges_All Payors\n"
    )

    def test_atrium_format_detected(self):
        """Atrium wide format should be detected via setting-specific columns."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.ATRIUM_HOSP_ALL_PAYORS)
        logs = []

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: logs.append(msg))

        assert any('atrium wide format' in msg.lower() for msg in logs), (
            f"Expected Atrium detection log, got: {logs}"
        )

    def test_not_detected_as_hawaii(self):
        """Atrium format should NOT trigger Hawaii detection."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.ATRIUM_HOSP_ALL_PAYORS)
        logs = []

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: logs.append(msg))

        assert not any('hawaii wide format' in msg.lower() for msg in logs), (
            f"Atrium should not trigger Hawaii detection, got: {logs}"
        )

    def test_multiple_settings_emitted(self):
        """Row with both IP/OP non-N/A should produce 2 charge_items."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.ATRIUM_HOSP_ALL_PAYORS)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        settings = {item['setting'] for item in mock._items}
        assert 'inpatient' in settings, f"Expected inpatient, got: {settings}"
        assert 'outpatient' in settings, f"Expected outpatient, got: {settings}"

    def test_na_setting_skipped(self):
        """Row 3 (Hip Replacement) has N/A for outpatient → only inpatient."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.ATRIUM_HOSP_ALL_PAYORS)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        hip_items = [item for item in mock._items if item['code'] == '27447']
        hip_settings = {item['setting'] for item in hip_items}
        assert hip_settings == {'inpatient'}, (
            f"Hip Replacement should only have inpatient, got: {hip_settings}"
        )

    def test_charge_standards_per_setting(self):
        """Each setting should get its own charge standard with correct gross."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.ATRIUM_HOSP_ALL_PAYORS)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        gross_values = [s['gross'] for s in mock._standards if s['gross'] is not None]
        assert 150.0 in gross_values, f"Expected 150.0 (inpatient gross), got: {gross_values}"
        assert 120.0 in gross_values, f"Expected 120.0 (outpatient gross), got: {gross_values}"

    def test_min_max_routing(self):
        """Min/Max column should route negotiated charge to min_rate or max_rate."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.ATRIUM_HOSP_MINMAX)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        # MIN row: inpatient=90, outpatient=75 → min_rate
        min_stds = [s for s in mock._standards if s['min_rate'] is not None]
        assert len(min_stds) >= 2, (
            f"Expected at least 2 standards with min_rate (IP+OP), got: {min_stds}"
        )
        min_rates = sorted([s['min_rate'] for s in min_stds])
        assert 90.0 in min_rates, f"Expected 90.0 (IP min), got: {min_rates}"
        assert 75.0 in min_rates, f"Expected 75.0 (OP min), got: {min_rates}"

        # MAX row: inpatient=115, outpatient=95 → max_rate
        max_stds = [s for s in mock._standards if s['max_rate'] is not None]
        assert len(max_stds) >= 2, (
            f"Expected at least 2 standards with max_rate (IP+OP), got: {max_stds}"
        )
        max_rates = sorted([s['max_rate'] for s in max_stds])
        assert 115.0 in max_rates, f"Expected 115.0 (IP max), got: {max_rates}"
        assert 95.0 in max_rates, f"Expected 95.0 (OP max), got: {max_rates}"

    def test_min_max_rows_no_payer_rates(self):
        """Rows marked MIN/MAX should NOT produce payer_rate records."""
        # CSV with payer + Min/Max indicator, negotiated charge should go
        # to charge_standard min/max, NOT to payer_rate.
        csv_data = (
            "Code,Code Type,Procedure Description,Payer,Plan,"
            " Inpatient Gross Charge , Outpatient Gross Charge ,"
            " Inpatient Negotiated Charge , Outpatient Negotiated Charge ,"
            "Min /Max,TabName\n"
            "99213,CPT,Office Visit,Aetna,PPO,"
            "150.00,120.00,"
            "90.00,75.00,"
            "MIN,Hosp All Payors\n"
            "99213,CPT,Office Visit,Aetna,PPO,"
            "150.00,120.00,"
            "115.00,95.00,"
            "MAX,Hosp All Payors\n"
        )
        mock = _make_mock_ingestor()
        handle = io.StringIO(csv_data)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        # Should have charge_standard rows with min/max
        assert len(mock._standards) >= 2

        # Should NOT have payer rates, MIN/MAX rows are aggregate data
        assert len(mock._payer_rates) == 0, (
            f"MIN/MAX rows should not produce payer rates, got: {mock._payer_rates}"
        )

    def test_payer_rates_from_tall_format(self):
        """Payer/Plan rows WITHOUT Min/Max should produce payer rates."""
        # Rows without Min/Max indicator have true negotiated rates
        csv_data = (
            "Code,Code Type,Procedure Description,Payer,Plan,"
            " Inpatient Gross Charge , Outpatient Gross Charge ,"
            " Inpatient Negotiated Charge , Outpatient Negotiated Charge ,"
            "TabName\n"
            "99213,CPT,Office Visit,Aetna,Commercial,"
            "150.00,120.00,"
            "100.00,80.00,"
            "Hosp Std. Charges_All Payors\n"
            "27447,CPT,Hip Replacement,BCBS,PPO,"
            "50000.00,N/A,"
            "35000.00,N/A,"
            "Hosp Std. Charges_All Payors\n"
        )
        mock = _make_mock_ingestor()
        handle = io.StringIO(csv_data)

        payer_rates_ext = []
        orig_fake = mock.stage_payer_rate.side_effect

        def ext_fake(*args, **kwargs):
            orig_fake(*args, **kwargs)
            payer_rates_ext.append({
                'code': args[0],
                'payer_name': kwargs.get('payer_name'),
                'plan_name': kwargs.get('plan_name'),
                'item_setting': kwargs.get('item_setting'),
                'negotiated_rate': kwargs.get('negotiated_rate'),
            })

        mock.stage_payer_rate.side_effect = ext_fake

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        # Should have payer rates from Aetna and BCBS
        payer_names = {r['payer_name'] for r in payer_rates_ext}
        assert len(payer_names) >= 1, (
            "Expected payer rates, got none"
        )

        # Check that rates are associated with the correct setting
        settings = {r['item_setting'] for r in payer_rates_ext}
        assert len(settings) >= 1, (
            f"Expected rates with settings, got: {settings}"
        )

    def test_self_pay_folded_to_cash(self):
        """Self-pay rows should have negotiated charges folded to cash price."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.ATRIUM_HOSP_CASH)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        # Should have charge items with cash prices
        cash_stds = [s for s in mock._standards if s['cash'] is not None]
        assert len(cash_stds) >= 2, (
            f"Expected at least 2 standards with cash price (IP+OP), got: {cash_stds}"
        )
        cash_values = sorted([s['cash'] for s in cash_stds])
        assert 130.0 in cash_values, f"Expected 130.0 (IP cash), got: {cash_values}"
        assert 100.0 in cash_values, f"Expected 100.0 (OP cash), got: {cash_values}"

        # Should NOT have payer rates (self-pay)
        assert len(mock._payer_rates) == 0, (
            f"Self-pay should not produce payer rates, got: {mock._payer_rates}"
        )

    def test_facility_nonfacility_settings(self):
        """Professional Facility→inpatient, Non-Facility→outpatient (CMS MPFS)."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.ATRIUM_PROF_ALL_PAYORS)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        settings = {item['setting'] for item in mock._items}
        assert 'inpatient' in settings, f"Expected inpatient (facility), got: {settings}"
        assert 'outpatient' in settings, f"Expected outpatient (non-facility), got: {settings}"

    def test_setting_columns_not_unmapped(self):
        """Atrium setting-specific columns should not appear as unmapped."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.ATRIUM_HOSP_ALL_PAYORS)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        assert ' Inpatient Gross Charge ' not in unmapped_cols
        assert ' Outpatient Gross Charge ' not in unmapped_cols
        assert ' Inpatient Negotiated Charge ' not in unmapped_cols
        assert ' Outpatient Negotiated Charge ' not in unmapped_cols
        assert 'Min /Max' not in unmapped_cols
        assert 'TabName' not in unmapped_cols, (
            "TabName should be suppressed (in _SUPPRESS_UNMAPPED)"
        )

    def test_unmapped_cells_use_emitted_setting(self):
        """Unmapped cells should use the first emitted setting."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.ATRIUM_HOSP_ALL_PAYORS)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        for u in mock._unmapped:
            assert u['item_setting'] != '', (
                f"Unmapped cell should have a real setting, got empty for "
                f"column={u['source_column']}"
            )

    def test_code_mapping(self):
        """Code and Code Type columns should map correctly."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.ATRIUM_HOSP_ALL_PAYORS)

        parse_csv_to_staging(handle, mock, hospital_id=1, run_id=1,
                             log=lambda msg, **kw: None)

        codes = {item['code'] for item in mock._items}
        assert '99213' in codes, f"Expected 99213, got: {codes}"
        assert '27447' in codes, f"Expected 27447, got: {codes}"

    def test_synonym_plan_s(self):
        """Plan(s) header should map to plan_name via SYNONYM_MAP."""
        from mrfkit.headers import map_header
        norm, mapped = map_header('Plan(s)')
        assert mapped == 'plan_name', (
            f"Plan(s) should map to plan_name, got: {mapped}"
        )

    def test_synonym_product(self):
        """Product header should map to plan_name via SYNONYM_MAP."""
        from mrfkit.headers import map_header
        norm, mapped = map_header('Product')
        assert mapped == 'plan_name', (
            f"Product should map to plan_name, got: {mapped}"
        )

    def test_synonym_codes(self):
        """Codes header should map to code via SYNONYM_MAP."""
        from mrfkit.headers import map_header
        norm, mapped = map_header('Codes')
        assert mapped == 'code', f"Codes should map to code, got: {mapped}"

    def test_synonym_min_negotiated_charge(self):
        """Min Negotiated Charge should map to min_negotiated_rate."""
        from mrfkit.headers import map_header
        norm, mapped = map_header('Min Negotiated Charge')
        assert mapped == 'min_negotiated_rate', (
            f"Min Negotiated Charge should map to min_negotiated_rate, got: {mapped}"
        )

    def test_synonym_max_negotiated_charge(self):
        """Max Negotiated Charge should map to max_negotiated_rate."""
        from mrfkit.headers import map_header
        norm, mapped = map_header('Max Negotiated Charge')
        assert mapped == 'max_negotiated_rate', (
            f"Max Negotiated Charge should map to max_negotiated_rate, got: {mapped}"
        )

    def test_synonym_minimum_reimbursement(self):
        """Minimum Reimbursement should map to min_negotiated_rate."""
        from mrfkit.headers import map_header
        norm, mapped = map_header(' Minimum Reimbursement ')
        assert mapped == 'min_negotiated_rate', (
            f"Minimum Reimbursement should map to min_negotiated_rate, got: {mapped}"
        )

    def test_synonym_maximum_reimbursement(self):
        """Maximum Reimbursement should map to max_negotiated_rate."""
        from mrfkit.headers import map_header
        norm, mapped = map_header(' Maximum Reimbursement ')
        assert mapped == 'max_negotiated_rate', (
            f"Maximum Reimbursement should map to max_negotiated_rate, got: {mapped}"
        )

    def test_synonym_procedure_modifier(self):
        """Procedure Modifier should map to modifiers."""
        from mrfkit.headers import map_header
        norm, mapped = map_header('Procedure Modifier')
        assert mapped == 'modifiers', (
            f"Procedure Modifier should map to modifiers, got: {mapped}"
        )

    def test_synonym_price(self):
        """Price header should map to gross_charge."""
        from mrfkit.headers import map_header
        norm, mapped = map_header('Price')
        assert mapped == 'gross_charge', (
            f"Price should map to gross_charge, got: {mapped}"
        )

    def test_synonym_procedure_external_id(self):
        """Procedure External ID should map to code."""
        from mrfkit.headers import map_header
        norm, mapped = map_header('Procedure External ID')
        assert mapped == 'code', (
            f"Procedure External ID should map to code, got: {mapped}"
        )


# ===================================================================
# Suppression of KU Great Bend billing-specific columns
# ===================================================================

class TestSuppressKUBillingColumns:
    """Verify KU Great Bend HospitalBilling/ProfessionalBilling columns are suppressed."""

    CSV_WITH_KU_BILLING = (
        "code,code_type,description,gross_charge,"
        "HospitalBilling_Inpatient_MIN_PRICE,HospitalBilling_Inpatient_MAX_PRICE,"
        "HospitalBilling_Outpatient_MIN_PRICE,HospitalBilling_Outpatient_MAX_PRICE,"
        "ProfessionalBilling_Inpatient_MIN_PRICE,ProfessionalBilling_Inpatient_MAX_PRICE,"
        "ProfessionalBilling_Outpatient_MIN_PRICE,ProfessionalBilling_Outpatient_MAX_PRICE\n"
        "99213,CPT,Office Visit,250.00,,,,,,,,,\n"
        "99214,CPT,Office Visit Ext,350.00,74676.10,,,,,,,,\n"
    )

    def test_all_ku_billing_columns_suppressed(self):
        """All 8 KU billing columns should be suppressed from unmapped cells."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.CSV_WITH_KU_BILLING)

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: None,
        )

        unmapped_cols = {u['source_column'] for u in mock._unmapped}
        ku_headers = {
            'HospitalBilling_Inpatient_MIN_PRICE',
            'HospitalBilling_Inpatient_MAX_PRICE',
            'HospitalBilling_Outpatient_MIN_PRICE',
            'HospitalBilling_Outpatient_MAX_PRICE',
            'ProfessionalBilling_Inpatient_MIN_PRICE',
            'ProfessionalBilling_Inpatient_MAX_PRICE',
            'ProfessionalBilling_Outpatient_MIN_PRICE',
            'ProfessionalBilling_Outpatient_MAX_PRICE',
        }
        leaked = ku_headers & unmapped_cols
        assert not leaked, (
            f"KU billing columns should be suppressed, but found: {leaked}"
        )

    def test_suppression_logged(self):
        """Suppression of KU billing columns should appear in log."""
        mock = _make_mock_ingestor()
        handle = io.StringIO(self.CSV_WITH_KU_BILLING)
        logs = []

        parse_csv_to_staging(
            handle, mock, hospital_id=1, run_id=1,
            log=lambda msg, **kw: logs.append(msg),
        )

        suppression_logs = [m for m in logs if 'Suppressed' in m or 'suppressed' in m]
        assert any('hospitalbilling' in m.lower() for m in suppression_logs), (
            f"Expected suppression log mentioning hospitalbilling, got: {suppression_logs}"
        )
