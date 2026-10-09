"""
Regression tests for malformed CMS "tall" CSVs where the metadata-VALUE row
is glued onto the data-header row with no newline between them.

Real-world trigger: AnMed Health Cannon (observed 2026-06).  Its file looks like:

    hospital_name,last_updated_on,version,...,attester_name,attestation
    AnMed Health Cannon,2026-03-06,...,Stephen Grigsby,TRUE,description,code|1,...
    description,code|1,code|1|type,...            <- clean header on next line
    Office Visit,99213,CPT,...                    <- data rows

The old header detector picked the FIRST preamble line containing
'description'/'code|', which is the glued metadata-value line.  That prepended
the 10 metadata values as phantom columns, shifting every data column right ,
producing 13.8M unmapped_cell rows and corrupt charge_item rows (codes holding
dollar amounts, empty descriptions/prices).

The fix scores candidate header rows by how cleanly their fields map to
canonical charge fields and prefers the clean header (a strict suffix of the
glued line).

Source: mrfkit.csv_reader, through tests/staging_shim.py
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


# Metadata header row (10 columns), then the malformed glued line
# (10 metadata values, address quoted with an embedded comma, + the 7-column
# data header), then the clean 7-column data header, then two data rows.
_GLUED_CSV = (
    "hospital_name,last_updated_on,version,location_name,hospital_address,"
    "financial_aid_policy,license_number|SC,type_2_npi,attester_name,attestation\n"
    'AnMed Health Cannon,2026-03-06,3.0.0,AnMed Health Cannon,'
    '"123 WG Acker Drive, Pickens SC 29671",,HTL-0076,1497744254,Stephen Grigsby,'
    "TRUE,description,code|1,code|1|type,billing_class,setting,"
    "standard_charge|gross,standard_charge|discounted_cash\n"
    "description,code|1,code|1|type,billing_class,setting,"
    "standard_charge|gross,standard_charge|discounted_cash\n"
    "Office Visit,99213,CPT,professional,outpatient,150.00,120.00\n"
    "Comprehensive Visit,99215,CPT,professional,outpatient,300.00,250.00\n"
)


class TestGluedMetadataHeaderRow:
    def test_clean_header_is_chosen_not_glued_line(self):
        """Header mappings should reflect the clean data header, not the
        metadata-value phantoms from the glued line."""
        from staging_shim import parse_csv_to_staging

        mock_ingestor = _make_mock_ingestor()
        parse_csv_to_staging(
            file_handle=io.StringIO(_GLUED_CSV),
            ingestor=mock_ingestor,
            hospital_id=1,
            run_id=1,
            log=lambda *a, **kw: None,
        )

        assert mock_ingestor.insert_header_mappings.call_count == 1
        _run_id, mappings = mock_ingestor.insert_header_mappings.call_args.args
        source_headers = {m[0] for m in mappings}

        # The real header columns are present...
        assert "description" in source_headers
        assert "code|1" in source_headers
        assert "standard_charge|gross" in source_headers
        # ...and none of the metadata VALUES leaked in as phantom columns.
        for phantom in (
            "AnMed Health Cannon",
            "2026-03-06",
            "3.0.0",
            "HTL-0076",
            "1497744254",
            "Stephen Grigsby",
            "TRUE",
        ):
            assert phantom not in source_headers, f"phantom column leaked: {phantom!r}"

    def test_data_columns_are_aligned(self):
        """Charge items must be staged with codes/descriptions in the right
        columns, not shifted by the metadata prefix."""
        from staging_shim import parse_csv_to_staging

        mock_ingestor = _make_mock_ingestor()
        parse_csv_to_staging(
            file_handle=io.StringIO(_GLUED_CSV),
            ingestor=mock_ingestor,
            hospital_id=1,
            run_id=1,
            log=lambda *a, **kw: None,
        )

        # Two data rows -> two charge_item stagings (the clean header line is
        # consumed as the header, not as a data row).
        calls = mock_ingestor.stage_charge_item.call_args_list
        staged = {
            (c.args[0], c.args[1], c.args[2])  # code, code_type, description
            for c in calls
        }
        assert ("99213", "CPT", "Office Visit") in staged
        assert ("99215", "CPT", "Comprehensive Visit") in staged

    def test_attestation_text_containing_description_not_chosen(self):
        """A well-formed CMS file whose metadata-VALUE row carries an
        attestation sentence containing the word 'descriptions' (Altus
        Lumberton Hospital) must still resolve to the real header line, the
        substring match on the value row must not win.  This case previously
        failed ingestion outright."""
        from staging_shim import parse_csv_to_staging

        attestation = (
            "This hospital does not have a universal discounted cash price. "
            "For all other descriptions, self-pay patients are charged gross."
        )
        csv_text = (
            "hospital_name,last_updated_on,version,attestation\n"
            f'Altus Lumberton Hospital,10/14/2025,2.0.0,"{attestation}"\n'
            "description,code|1,code|1|type,standard_charge|gross\n"
            "ANESTH BLEPHAROPLASTY,103,CPT,500.00\n"
        )
        mock_ingestor = _make_mock_ingestor()
        parse_csv_to_staging(
            file_handle=io.StringIO(csv_text),
            ingestor=mock_ingestor,
            hospital_id=1,
            run_id=1,
            log=lambda *a, **kw: None,
        )

        _run_id, mappings = mock_ingestor.insert_header_mappings.call_args.args
        source_headers = {m[0] for m in mappings}
        assert source_headers == {
            "description", "code|1", "code|1|type", "standard_charge|gross",
        }
        calls = mock_ingestor.stage_charge_item.call_args_list
        assert any(
            c.args[0] == "103" and c.args[2] == "ANESTH BLEPHAROPLASTY"
            for c in calls
        )

    def test_well_formed_file_unaffected(self):
        """A normal CMS tall file (metadata header / values / header / data)
        still resolves to its single real header row."""
        from staging_shim import parse_csv_to_staging

        well_formed = (
            "hospital_name,last_updated_on,version\n"
            "Example Hospital,2026-01-01,3.0.0\n"
            "description,code|1,code|1|type,standard_charge|gross\n"
            "Office Visit,99213,CPT,150.00\n"
        )
        mock_ingestor = _make_mock_ingestor()
        parse_csv_to_staging(
            file_handle=io.StringIO(well_formed),
            ingestor=mock_ingestor,
            hospital_id=1,
            run_id=1,
            log=lambda *a, **kw: None,
        )

        _run_id, mappings = mock_ingestor.insert_header_mappings.call_args.args
        source_headers = {m[0] for m in mappings}
        assert source_headers == {
            "description", "code|1", "code|1|type", "standard_charge|gross",
        }
        calls = mock_ingestor.stage_charge_item.call_args_list
        assert any(c.args[0] == "99213" for c in calls)
