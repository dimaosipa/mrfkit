"""
Tests for CSV quoting auto-detection in mrfkit.csv_reader.

Source of bug: some source CSVs contain unescaped quote marks at the
*start* of a description field (e.g. `"needle 6 inch gauge 22"` written
as `"needle 6 inch gauge 22`).  Under csv.DictReader's default
QUOTE_MINIMAL, a leading `"` opens a quoted field that never closes
until the next `"` or EOF, swallowing every subsequent delimiter and
fusing rows together.

Note: Python's csv module only treats `"` as a quote opener when it
appears at the *very start* of a field.  Mid-field `"` is kept literal
(so `6" needle` parses cleanly).

`_detect_csv_quoting()` samples a small window of data lines, scores
each quoting mode by column-alignment + structural sanity of the
code_type column, and picks the mode with fewer errors.  Ties favour
QUOTE_MINIMAL (RFC 4180 compliant default).
"""

import csv


from mrfkit.csv_reader import _detect_csv_quoting, _score_csv_quoting_mode


# ---- Helpers --------------------------------------------------------------

HEADER_LINE = "code|code_type|description|gross_charge\n"


def _header_lines():
    return [HEADER_LINE]


# ---- Well-formed files stay on QUOTE_MINIMAL -----------------------------

class TestWellFormedCsv:
    def test_empty_sample_defaults_to_minimal(self):
        assert _detect_csv_quoting(_header_lines(), [], '|') == csv.QUOTE_MINIMAL

    def test_clean_rows_pick_minimal(self):
        sample = [
            "99213|CPT|Office visit|150.00\n",
            "99214|CPT|Office visit complex|225.00\n",
            "J0878|HCPCS|Daptomycin injection|120.00\n",
        ]
        assert _detect_csv_quoting(_header_lines(), sample, '|') == csv.QUOTE_MINIMAL

    def test_mid_field_inch_mark_still_picks_minimal(self):
        # Mid-field `"` is handled fine by QUOTE_MINIMAL, python's csv
        # module only treats `"` as a quote opener when it's the first
        # char of a field.  Both modes parse this cleanly, tie goes to
        # QUOTE_MINIMAL.
        sample = [
            '12345|HCPCS|6" needle|5.00\n',
            '12346|HCPCS|8" needle|6.00\n',
        ]
        assert _detect_csv_quoting(_header_lines(), sample, '|') == csv.QUOTE_MINIMAL

    def test_legitimately_quoted_field_with_delimiter_picks_minimal(self):
        # QUOTE_MINIMAL correctly handles an embedded delimiter inside a
        # properly-quoted field; QUOTE_NONE would split on the inner `,`
        # and produce length mismatches + malformed code_type.
        comma_header = ["code,code_type,description,gross_charge\n"]
        comma_sample = [
            '99213,CPT,"Office visit, brief",150.00\n',
            '99214,CPT,"Office visit, complex",225.00\n',
            '99215,CPT,"Office visit, detailed",275.00\n',
        ]
        assert _detect_csv_quoting(comma_header, comma_sample, ',') == csv.QUOTE_MINIMAL


# ---- Leading-quote shift flips to QUOTE_NONE -----------------------------

class TestLeadingQuoteShift:
    def test_leading_quote_in_description_picks_quote_none(self):
        # Under QUOTE_MINIMAL, the leading `"` in the description field
        # opens a quoted field that fuses the next rows together until
        # another `"` appears, causing length mismatches.  Under
        # QUOTE_NONE the `"` is literal and each row parses cleanly.
        sample = [
            '12345|HCPCS|"needle 6 inch gauge 22|5.00\n',
            '12346|HCPCS|"needle 8 inch gauge 20|6.00\n',
            '12347|HCPCS|"catheter 10 french|7.00\n',
            '12348|HCPCS|"drain 14 french|8.00\n',
            '12349|HCPCS|Sterile gauze|2.00\n',
        ]
        assert _detect_csv_quoting(_header_lines(), sample, '|') == csv.QUOTE_NONE

    def test_mixed_but_mostly_bad_picks_quote_none(self):
        sample = [
            '99213|CPT|Office visit|150.00\n',
            '12345|HCPCS|"catheter foley|5.00\n',
            '12346|HCPCS|"drain jackson|6.00\n',
            '12347|HCPCS|"tube nasogastric|7.00\n',
        ]
        assert _detect_csv_quoting(_header_lines(), sample, '|') == csv.QUOTE_NONE


# ---- Scoring -------------------------------------------------------------

class TestScoring:
    def test_scoring_detects_shift_on_minimal(self):
        sample = [
            '12345|HCPCS|"needle 6 gauge 22|5.00\n',
            '12346|HCPCS|"needle 8 gauge 20|6.00\n',
            '12347|HCPCS|"needle 10 gauge 18|7.00\n',
        ]
        minimal = _score_csv_quoting_mode(_header_lines(), sample, '|', csv.QUOTE_MINIMAL)
        none = _score_csv_quoting_mode(_header_lines(), sample, '|', csv.QUOTE_NONE)
        assert (minimal['length_mismatches'] + minimal['malformed_code_types']) > 0
        assert none['length_mismatches'] == 0
        assert none['malformed_code_types'] == 0

    def test_scoring_clean_file_zero_errors(self):
        sample = [
            '99213|CPT|Visit|150.00\n',
            '99214|CPT|Visit|225.00\n',
        ]
        minimal = _score_csv_quoting_mode(_header_lines(), sample, '|', csv.QUOTE_MINIMAL)
        assert minimal['length_mismatches'] == 0
        assert minimal['malformed_code_types'] == 0
        assert minimal['rows_seen'] == 2

    def test_scoring_malformed_code_type_detected(self):
        # Column-shifted rows land prose in code_type; the structural
        # regex in mrfkit.codes flags that as malformed.
        sample = [
            "99213|some long prose that cannot be a code type|desc|150.00\n",
        ]
        minimal = _score_csv_quoting_mode(_header_lines(), sample, '|', csv.QUOTE_MINIMAL)
        assert minimal['malformed_code_types'] >= 1

    def test_score_has_all_keys_on_csv_error_path(self):
        # If csv.DictReader construction raises csv.Error, the early-return
        # dict must still contain every key that _detect_csv_quoting's
        # _total() reads, otherwise detection crashes with KeyError and
        # aborts ingestion.
        from unittest.mock import patch
        with patch('mrfkit.csv_reader.csv.DictReader', side_effect=csv.Error("boom")):
            result = _score_csv_quoting_mode(
                _header_lines(), ["foo\n"], '|', csv.QUOTE_MINIMAL)
        for key in ('length_mismatches', 'malformed_code_types',
                    'embedded_newlines', 'rows_seen', 'expected_cols'):
            assert key in result, f"missing key {key} in error-path score"

    def test_detect_does_not_crash_when_both_modes_error(self):
        # End-to-end: even if both modes raise csv.Error during
        # construction, _detect_csv_quoting should return a mode (not
        # KeyError out).
        from unittest.mock import patch
        with patch('mrfkit.csv_reader.csv.DictReader', side_effect=csv.Error("boom")):
            mode = _detect_csv_quoting(_header_lines(), ["foo\n"], '|')
        assert mode in (csv.QUOTE_MINIMAL, csv.QUOTE_NONE)
