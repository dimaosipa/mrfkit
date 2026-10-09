"""
Regression tests for concatenated multi-section CSVs where several tables are
stacked under a single header row.

Real-world trigger: HCA "Medical City" North-Texas files (observed 2026-07),
served from core.secure.ehc.com.  The file is a normal chargemaster table
followed by dozens of per-payer blocks, each introduced by its OWN sub-header:

    Procedure ID,HCPCS/CPT Code,Description,Gross Charge,Discounted Cash Price
    071270,071270,XR CHEST MIN 3 VIEWS,270.00,202.50        <- real chargemaster
    ...
    Accountable Health Plans,,                               <- payer marker
    Service Description,Coding,Rate                          <- foreign sub-header
    "CPT/HCPC Ambulance service ...","<TAB>A0021",$43.74     <- payer rows

csv.DictReader keeps only the FIRST line as fieldnames, so every payer row was
misparsed positionally against the chargemaster schema: the rate ("$43.74")
landed in the Description column and codes lost their leading alpha
(A0021 -> 0021, then mistyped CPT).  On prod this produced ~161K
"$"-in-description rows across 9 hospitals.

The fix: when a row carries no price in any canonical price column AND looks
like a re-declared header (a cell that maps to the `description` field), it is
a foreign section boundary, parsing stops there.  The real chargemaster above
it is already staged; the per-payer blocks below are redundant with the
hospital's clean CMS-3.0 JSON sibling.

Source: mrfkit.csv_reader, through tests/staging_shim.py
and _row_is_foreign_section_header().
"""

import io
from unittest.mock import MagicMock


def _make_mock_ingestor():
    """Mock staging recorder with the minimum interface parse_csv_to_staging
    touches.  All DB-touching methods are auto-mocked no-ops."""
    mock = MagicMock()
    mock._seen_items = set()
    mock._payer_rate_count = 0
    return mock


# A chargemaster table (3 real rows) followed by two per-payer blocks, each
# with its own "Service Description,Coding,Rate" sub-header and rows whose
# rate text would land in the Description column under the first schema.
_MULTISECTION_CSV = (
    "Procedure ID,HCPCS/CPT Code,Description,Gross Charge,"
    "Discounted Cash Price (Gross Charges)\n"
    "071270,071270,XR CHEST MIN 3 VIEWS,270.00,202.50\n"
    "0C1776,0C1776,LENS IO IMPLANT,1044.00,1044.00\n"
    "0A4263,0A4263,ELECTRODE PATCH,50.00,40.00\n"
    "Accountable Health Plans,,\n"
    "Service Description,Coding,Rate\n"
    '"CPT/HCPC Ambulance service outside state per mile","\tA0021",$43.74\n'
    '"CPT/HCPC Non-emergency transport per mile","\tA0160",$2.32\n'
    "Blue Cross Blue Shield,,\n"
    "Service Description,Coding,Rate\n"
    '"CPT/HCPC BLS mileage per mile","\tA0380",65% of Billable Gross Charges\n'
)


def _staged_items(mock_ingestor):
    """Return {(code, code_type, description)} across all stage_charge_item calls."""
    return {
        (c.args[0], c.args[1], c.args[2])
        for c in mock_ingestor.stage_charge_item.call_args_list
    }


class TestMultiSectionCsv:
    def test_chargemaster_rows_staged(self):
        """The real first-section chargemaster rows ingest normally."""
        from staging_shim import parse_csv_to_staging

        mock_ingestor = _make_mock_ingestor()
        parse_csv_to_staging(
            file_handle=io.StringIO(_MULTISECTION_CSV),
            ingestor=mock_ingestor,
            hospital_id=1,
            run_id=1,
            log=lambda *a, **kw: None,
        )

        descriptions = {item[2] for item in _staged_items(mock_ingestor)}
        assert "XR CHEST MIN 3 VIEWS" in descriptions
        assert "LENS IO IMPLANT" in descriptions
        assert "ELECTRODE PATCH" in descriptions

    def test_no_price_string_descriptions(self):
        """No charge_item is staged with a rate/price text as its description
        (the core corruption symptom)."""
        from staging_shim import parse_csv_to_staging

        mock_ingestor = _make_mock_ingestor()
        parse_csv_to_staging(
            file_handle=io.StringIO(_MULTISECTION_CSV),
            ingestor=mock_ingestor,
            hospital_id=1,
            run_id=1,
            log=lambda *a, **kw: None,
        )

        for _code, _ct, description in _staged_items(mock_ingestor):
            desc = (description or "").strip()
            assert not desc.startswith("$"), f"price-string description leaked: {desc!r}"
            assert "Billable Gross Charges" not in desc
            assert not desc.startswith("Service Description")

    def test_payer_block_rows_not_staged(self):
        """The per-payer blocks below the first foreign sub-header are dropped
        entirely, none of their descriptions/codes reach staging."""
        from staging_shim import parse_csv_to_staging

        mock_ingestor = _make_mock_ingestor()
        parse_csv_to_staging(
            file_handle=io.StringIO(_MULTISECTION_CSV),
            ingestor=mock_ingestor,
            hospital_id=1,
            run_id=1,
            log=lambda *a, **kw: None,
        )

        staged = _staged_items(mock_ingestor)
        # Exactly the 3 chargemaster rows, nothing from the payer blocks.
        assert len(staged) == 3, f"expected 3 staged items, got {staged}"
        joined = " ".join(
            f"{c} {ct} {d}" for c, ct, d in staged
        )
        assert "Ambulance" not in joined
        assert "mileage" not in joined

    def test_warning_recorded(self):
        """Truncation is surfaced as a parse warning (visible in ingest logs)."""
        from staging_shim import parse_csv_to_staging

        logs = []
        mock_ingestor = _make_mock_ingestor()
        warnings = parse_csv_to_staging(
            file_handle=io.StringIO(_MULTISECTION_CSV),
            ingestor=mock_ingestor,
            hospital_id=1,
            run_id=1,
            log=lambda *a, **kw: logs.append(" ".join(str(x) for x in a)),
        )

        blob = " ".join((warnings or []) + logs)
        assert "Multi-section" in blob or "foreign section" in blob


class TestSingleSectionUnaffected:
    def test_well_formed_file_fully_ingested(self):
        """A normal single-section CMS file is not truncated, every data row
        (including price-less rows) still ingests."""
        from staging_shim import parse_csv_to_staging

        well_formed = (
            "description,code|1,code|1|type,standard_charge|gross\n"
            "Office Visit,99213,CPT,150.00\n"
            "Comprehensive Visit,99215,CPT,300.00\n"
            "Followup Visit,99214,CPT,0.00\n"
        )
        mock_ingestor = _make_mock_ingestor()
        parse_csv_to_staging(
            file_handle=io.StringIO(well_formed),
            ingestor=mock_ingestor,
            hospital_id=1,
            run_id=1,
            log=lambda *a, **kw: None,
        )

        codes = {item[0] for item in _staged_items(mock_ingestor)}
        assert {"99213", "99215", "99214"}.issubset(codes)

    def test_priceless_data_row_not_mistaken_for_boundary(self):
        """A legitimate price-less chargemaster row whose description is real
        text must NOT trigger the boundary guard."""
        from staging_shim import parse_csv_to_staging

        csv_text = (
            "description,code|1,code|1|type,standard_charge|gross\n"
            "Office Visit,99213,CPT,150.00\n"
            "Nursing Assessment,99999,CPT,\n"   # real row, no price
            "Comprehensive Visit,99215,CPT,300.00\n"
        )
        mock_ingestor = _make_mock_ingestor()
        parse_csv_to_staging(
            file_handle=io.StringIO(csv_text),
            ingestor=mock_ingestor,
            hospital_id=1,
            run_id=1,
            log=lambda *a, **kw: None,
        )

        codes = {item[0] for item in _staged_items(mock_ingestor)}
        # The price-less middle row must not truncate the file.
        assert "99215" in codes, "boundary guard truncated a legit price-less row"
        assert "99999" in codes

    def test_over_wide_price_less_row_does_not_crash_or_truncate(self):
        """A data row WIDER than the header (surplus fields collected under the
        csv.DictReader None restkey as a list) and carrying no price must not
        crash the guard's header scan, nor truncate the file.  Regression for
        the AttributeError('list'.strip) crash on the mainstream ingest path."""
        from staging_shim import parse_csv_to_staging

        csv_text = (
            "description,code|1,code|1|type,standard_charge|gross\n"
            "Office Visit,99213,CPT,150.00\n"
            # blank gross + 2 trailing surplus columns (unescaped commas)
            "Nursing assessment,88888,HCPCS,,see note, addl\n"
            "Comprehensive Visit,99215,CPT,300.00\n"
        )
        mock_ingestor = _make_mock_ingestor()
        # Must not raise.
        parse_csv_to_staging(
            file_handle=io.StringIO(csv_text),
            ingestor=mock_ingestor,
            hospital_id=1,
            run_id=1,
            log=lambda *a, **kw: None,
        )
        codes = {item[0] for item in _staged_items(mock_ingestor)}
        assert {"99213", "88888", "99215"}.issubset(codes), (
            f"over-wide row truncated the file: {codes}")

    def test_repeated_full_header_not_truncated(self):
        """A file with two same-schema sections (a repeated FULL header) must
        not be truncated, rows in the second section still ingest.  The
        repeated header carries the price-column header text as a truthy value
        in the price column, so the price-absence gate skips it."""
        from staging_shim import parse_csv_to_staging

        csv_text = (
            "description,code|1,code|1|type,standard_charge|gross\n"
            "Office Visit,99213,CPT,150.00\n"
            "description,code|1,code|1|type,standard_charge|gross\n"  # repeated
            "Comprehensive Visit,99215,CPT,300.00\n"
        )
        mock_ingestor = _make_mock_ingestor()
        parse_csv_to_staging(
            file_handle=io.StringIO(csv_text),
            ingestor=mock_ingestor,
            hospital_id=1,
            run_id=1,
            log=lambda *a, **kw: None,
        )
        codes = {item[0] for item in _staged_items(mock_ingestor)}
        assert "99215" in codes, "repeated full header falsely truncated the file"

    def test_full_width_priceless_header_like_description_not_truncated(self):
        """A FULL-WIDTH price-less row whose description cell is a header-like
        token must NOT truncate: the boundary guard additionally requires a
        missing (None) trailing cell, the structural hallmark of a narrower
        sub-header.  Documents the narrowed false-positive surface."""
        from staging_shim import parse_csv_to_staging

        csv_text = (
            "description,code|1,code|1|type,standard_charge|gross\n"
            "Office Visit,99213,CPT,150.00\n"
            "Item Description,77777,CPT,\n"   # header-like desc, but full width + no None
            "Comprehensive Visit,99215,CPT,300.00\n"
        )
        mock_ingestor = _make_mock_ingestor()
        parse_csv_to_staging(
            file_handle=io.StringIO(csv_text),
            ingestor=mock_ingestor,
            hospital_id=1,
            run_id=1,
            log=lambda *a, **kw: None,
        )
        codes = {item[0] for item in _staged_items(mock_ingestor)}
        assert "99215" in codes, "full-width header-like row falsely truncated the file"
