"""
Tests for the CSV row-skip logic in parse_csv_to_staging().

When a CSV row triggers csv.Error (e.g. a field exceeding csv.field_size_limit),
the parser should skip that row, log a warning, and continue processing the rest
of the file, not crash.

Two test classes:
  1. TestCsvRowSkipPattern, unit tests verifying the skip pattern in isolation
     (using csv.DictReader directly with a low field_size_limit).
  2. TestParseCSVToStagingSkip, integration tests calling parse_csv_to_staging()
     directly with a mock staging recorder, verifying warnings are returned and
     good rows are still staged.

Source: mrfkit.csv_reader, through tests/staging_shim.py
"""

import csv
import io
from unittest.mock import MagicMock

import pytest


class TestCsvRowSkipPattern:
    """Test the while-True / try-next(reader) / except csv.Error pattern."""

    def test_csv_error_is_raised_for_oversized_field(self):
        """Verify that Python's csv module actually raises csv.Error for
        fields exceeding the limit, confirming our skip logic is needed."""
        old_limit = csv.field_size_limit()
        try:
            csv.field_size_limit(100)
            big_value = "x" * 200  # Exceeds 100 byte limit
            csv_text = f"a,b\n{big_value},2\n"
            reader = csv.reader(io.StringIO(csv_text))
            next(reader)  # header, OK
            with pytest.raises(csv.Error, match="field larger"):
                next(reader)  # data row, should raise
        finally:
            csv.field_size_limit(old_limit)

    def test_skip_pattern_recovers_after_bad_row(self):
        """Simulate the skip pattern from parse_csv_to_staging:
        bad rows are skipped, good rows after them are processed."""
        old_limit = csv.field_size_limit()
        try:
            csv.field_size_limit(100)
            big_value = "x" * 200
            # Row 1: good, Row 2: bad (oversized), Row 3: good
            csv_text = f"a,b\nval1,1\n{big_value},2\nval3,3\n"
            reader = csv.DictReader(io.StringIO(csv_text))

            good_rows = []
            parse_warnings = []
            skipped_rows = 0
            row_count = 0

            while True:
                try:
                    row = next(reader)
                except csv.Error as e:
                    row_count += 1
                    skipped_rows += 1
                    if skipped_rows <= 5:
                        msg = f"Skipped CSV row ~{row_count}: {e}"
                        parse_warnings.append(msg)
                    continue
                except StopIteration:
                    break
                row_count += 1
                good_rows.append(row)

            assert len(good_rows) == 2
            assert good_rows[0]["a"] == "val1"
            assert good_rows[1]["a"] == "val3"
            assert skipped_rows == 1
            assert len(parse_warnings) == 1
            assert "Skipped CSV row" in parse_warnings[0]
        finally:
            csv.field_size_limit(old_limit)

    def test_warning_suppression_after_5_bad_rows(self):
        """After 5 bad rows, further warnings should be suppressed."""
        old_limit = csv.field_size_limit()
        try:
            csv.field_size_limit(100)
            big_value = "x" * 200
            # Build CSV: header + 8 bad rows + 1 good row
            lines = ["a,b"]
            for _ in range(8):
                lines.append(f"{big_value},bad")
            lines.append("good,last")
            csv_text = "\n".join(lines) + "\n"

            reader = csv.DictReader(io.StringIO(csv_text))
            good_rows = []
            parse_warnings = []
            skipped_rows = 0
            row_count = 0

            while True:
                try:
                    row = next(reader)
                except csv.Error as e:
                    row_count += 1
                    skipped_rows += 1
                    if skipped_rows <= 5:
                        msg = f"Skipped CSV row ~{row_count}: {e}"
                        parse_warnings.append(msg)
                    continue
                except StopIteration:
                    break
                row_count += 1
                good_rows.append(row)

            assert skipped_rows == 8
            # Only first 5 warnings captured
            assert len(parse_warnings) == 5
            # Good row still processed
            assert len(good_rows) == 1
            assert good_rows[0]["a"] == "good"
        finally:
            csv.field_size_limit(old_limit)

    def test_no_bad_rows_returns_empty_warnings(self):
        """When all rows are valid, parse_warnings should be empty."""
        csv_text = "a,b\nval1,1\nval2,2\n"
        reader = csv.DictReader(io.StringIO(csv_text))

        good_rows = []
        parse_warnings = []
        skipped_rows = 0
        row_count = 0

        while True:
            try:
                row = next(reader)
            except csv.Error as e:
                row_count += 1
                skipped_rows += 1
                if skipped_rows <= 5:
                    parse_warnings.append(f"Skipped CSV row ~{row_count}: {e}")
                continue
            except StopIteration:
                break
            row_count += 1
            good_rows.append(row)

        assert len(good_rows) == 2
        assert skipped_rows == 0
        assert parse_warnings == []

    def test_all_rows_bad_processes_none(self):
        """If every data row is bad, zero good rows but no crash."""
        old_limit = csv.field_size_limit()
        try:
            csv.field_size_limit(100)
            big_value = "x" * 200
            csv_text = f"a,b\n{big_value},1\n{big_value},2\n"
            reader = csv.DictReader(io.StringIO(csv_text))

            good_rows = []
            parse_warnings = []
            skipped_rows = 0
            row_count = 0

            while True:
                try:
                    row = next(reader)
                except csv.Error as e:
                    row_count += 1
                    skipped_rows += 1
                    if skipped_rows <= 5:
                        parse_warnings.append(f"Skipped CSV row ~{row_count}: {e}")
                    continue
                except StopIteration:
                    break
                row_count += 1
                good_rows.append(row)

            assert len(good_rows) == 0
            assert skipped_rows == 2
            assert len(parse_warnings) == 2
        finally:
            csv.field_size_limit(old_limit)


def _make_mock_ingestor():
    """Create a mock staging recorder with the minimum interface needed by
    parse_csv_to_staging().  All DB-touching methods are no-ops."""
    mock = MagicMock()
    mock._seen_items = set()
    mock._payer_rate_count = 0
    # insert_header_mappings, stage_charge_item, stage_charge_standard,
    # stage_payer_rate, stage_unmapped, flush_all_staging, commit,
    # upsert_hospital_locations are all auto-mocked by MagicMock.
    return mock


class TestParseCSVToStagingSkip:
    """Integration tests calling parse_csv_to_staging() directly.

    Uses a mock staging recorder and a low csv.field_size_limit to trigger
    the skip logic on specific rows, verifying:
      - The function returns warning strings for skipped rows.
      - Good rows are still staged (stage_charge_item called).
      - The function does not crash on bad rows.
    """

    def test_skipped_row_returns_warnings(self):
        """parse_csv_to_staging skips bad rows and returns warnings."""
        from staging_shim import parse_csv_to_staging

        old_limit = csv.field_size_limit()
        try:
            csv.field_size_limit(100)
            big_value = "x" * 200
            # Minimal CSV with headers that map to canonical fields.
            # Row 1: good, Row 2: bad (oversized description), Row 3: good.
            csv_text = (
                "code,code_type,description,gross_charge\n"
                "99213,CPT,Office Visit,150.00\n"
                f"99214,CPT,{big_value},200.00\n"
                "99215,CPT,Comprehensive Visit,300.00\n"
            )
            handle = io.StringIO(csv_text)
            mock_ingestor = _make_mock_ingestor()

            warnings = parse_csv_to_staging(
                file_handle=handle,
                ingestor=mock_ingestor,
                hospital_id=1,
                run_id=1,
                log=lambda *a, **kw: None,  # suppress output
            )

            # Should have at least one warning about the skipped row
            assert any("Skipped" in w for w in warnings)
            # stage_charge_item should have been called for the 2 good rows
            assert mock_ingestor.stage_charge_item.call_count >= 2
        finally:
            csv.field_size_limit(old_limit)

    def test_all_good_rows_no_warnings(self):
        """parse_csv_to_staging returns empty warnings when all rows are valid."""
        from staging_shim import parse_csv_to_staging

        csv_text = (
            "code,code_type,description,gross_charge\n"
            "99213,CPT,Office Visit,150.00\n"
            "99214,CPT,Follow-up Visit,200.00\n"
        )
        handle = io.StringIO(csv_text)
        mock_ingestor = _make_mock_ingestor()

        warnings = parse_csv_to_staging(
            file_handle=handle,
            ingestor=mock_ingestor,
            hospital_id=1,
            run_id=1,
            log=lambda *a, **kw: None,
        )

        assert warnings == []
        assert mock_ingestor.stage_charge_item.call_count >= 2

    def test_all_bad_rows_returns_warnings_no_crash(self):
        """parse_csv_to_staging handles files where every data row is bad."""
        from staging_shim import parse_csv_to_staging

        old_limit = csv.field_size_limit()
        try:
            csv.field_size_limit(100)
            big_value = "x" * 200
            csv_text = (
                "code,code_type,description,gross_charge\n"
                f"99213,CPT,{big_value},150.00\n"
                f"99214,CPT,{big_value},200.00\n"
            )
            handle = io.StringIO(csv_text)
            mock_ingestor = _make_mock_ingestor()

            warnings = parse_csv_to_staging(
                file_handle=handle,
                ingestor=mock_ingestor,
                hospital_id=1,
                run_id=1,
                log=lambda *a, **kw: None,
            )

            # Should have summary + per-row warnings
            assert len(warnings) >= 2
            assert any("Skipped" in w for w in warnings)
            # No good rows staged
            assert mock_ingestor.stage_charge_item.call_count == 0
        finally:
            csv.field_size_limit(old_limit)


class TestNoneValueCells:
    """Regression tests for CSV rows containing None cell values.

    Python's csv.DictReader produces None values when a row has fewer
    fields than the header (restval=None by default).  The parser must
    treat None the same as an empty string, not crash with
    AttributeError: 'NoneType' object has no attribute 'strip'.
    """

    def test_none_in_canonical_fields_no_crash(self):
        """Rows with fewer columns than the header should not crash."""
        from staging_shim import parse_csv_to_staging

        # Row 2 has only 2 fields but 4 header columns → description
        # and gross_charge will be None in the DictReader output.
        csv_text = (
            "code,code_type,description,gross_charge\n"
            "99213,CPT,Office Visit,150.00\n"
            "99214,CPT\n"
            "99215,CPT,Comprehensive Visit,300.00\n"
        )
        handle = io.StringIO(csv_text)
        mock_ingestor = _make_mock_ingestor()

        # Should not raise AttributeError
        parse_csv_to_staging(
            file_handle=handle,
            ingestor=mock_ingestor,
            hospital_id=1,
            run_id=1,
            log=lambda *a, **kw: None,
        )

        # At least the 2 fully-populated rows should be staged
        assert mock_ingestor.stage_charge_item.call_count >= 2

    def test_none_in_payer_columns_no_crash(self):
        """Rows with None in payer-related columns should not crash."""
        from staging_shim import parse_csv_to_staging

        # Row 2 is missing payer_name and negotiated_rate (None)
        csv_text = (
            "code,code_type,description,payer_name,negotiated_rate\n"
            "99213,CPT,Office Visit,Aetna,150.00\n"
            "99214,CPT,Follow-up\n"
            "99215,CPT,Comprehensive Visit,BCBS,300.00\n"
        )
        handle = io.StringIO(csv_text)
        mock_ingestor = _make_mock_ingestor()

        parse_csv_to_staging(
            file_handle=handle,
            ingestor=mock_ingestor,
            hospital_id=1,
            run_id=1,
            log=lambda *a, **kw: None,
        )

        # Should not crash; all 3 rows processed (row 2 has no payer data)
        assert mock_ingestor.stage_charge_item.call_count >= 2
