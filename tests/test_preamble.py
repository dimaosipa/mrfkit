"""
Unit tests for preamble parsing helpers in mrfkit.csv_reader.

Tests _sanitize_npi() and _try_parse_kv_preamble(), pure functions
that do not require a database connection.

Run:
    cd mrf-parser && .venv/bin/python -m pytest tests/test_preamble.py -v
"""

from mrfkit.csv_reader import _try_parse_kv_preamble
from mrfkit.values import _sanitize_npi


# ── _sanitize_npi() tests ────────────────────────────────────

class TestSanitizeNpi:
    """Unit tests for _sanitize_npi(), pipe-separated NPI handling."""

    def test_single_valid_npi(self):
        """Single 10-digit NPI returned as-is."""
        assert _sanitize_npi("1023187416") == "1023187416"

    def test_multiple_pipe_separated(self):
        """Returns first valid NPI from pipe-separated list."""
        assert _sanitize_npi("1023187416| 1124017140| 1154415024") == "1023187416"

    def test_no_spaces(self):
        """Handles pipes without surrounding spaces."""
        assert _sanitize_npi("1023187416|1124017140") == "1023187416"

    def test_none_input(self):
        """Returns None for None input."""
        assert _sanitize_npi(None) is None

    def test_empty_string(self):
        """Returns None for empty string."""
        assert _sanitize_npi("") is None

    def test_whitespace_only(self):
        """Returns None for whitespace-only string."""
        assert _sanitize_npi("   ") is None

    def test_non_digit(self):
        """Returns None for non-digit string."""
        assert _sanitize_npi("not-an-npi") is None

    def test_wrong_length(self):
        """Returns None for digit string with wrong length."""
        assert _sanitize_npi("12345") is None

    def test_first_invalid_second_valid(self):
        """Skips invalid parts and returns first valid NPI."""
        assert _sanitize_npi("bad| 1124017140") == "1124017140"

    def test_all_invalid(self):
        """Returns None when no valid 10-digit NPI found."""
        assert _sanitize_npi("short|also-short|toolongvalue1") is None

    def test_quoted_npi(self):
        """Strips quotes around a valid NPI."""
        assert _sanitize_npi('"1023187416"') == "1023187416"

    def test_npi_with_stray_characters(self):
        """Extracts digits from NPI with non-digit noise."""
        assert _sanitize_npi("NPI:1023187416") == "1023187416"


# ── _try_parse_kv_preamble() tests ───────────────────────────

class TestKVPreambleParsing:
    """Unit tests for _try_parse_kv_preamble(), colon-separated format."""

    def test_parses_hospital_name(self):
        """Should extract hospital_name from 'Hospital Name: ...' line."""
        lines = [
            "Hospital Name:  South Texas Health System,,,,,\n",
            "Price Effective Date:  6/1/2021,,,,,\n",
            ",,,,,\n",
            "Facility,CDM,Type,Code,Description,Gross Charge\n",
        ]
        result = _try_parse_kv_preamble(lines, header_row_idx=3)
        assert result is not None
        assert result['hospital_name'] == 'South Texas Health System'

    def test_parses_last_updated_on(self):
        """Should map 'Price Effective Date' to 'last_updated_on'."""
        lines = [
            "Hospital Name:  Test Hospital,,,\n",
            "Price Effective Date:  6/1/2021,,,\n",
            ",,,\n",
            "Description,Code,Gross Charge\n",
        ]
        result = _try_parse_kv_preamble(lines, header_row_idx=3)
        assert result is not None
        assert result['last_updated_on'] == '6/1/2021'

    def test_returns_none_for_standard_format(self):
        """Should return None for standard CMS 2-row table preamble."""
        lines = [
            "hospital_name,hospital_address,last_updated_on\n",
            '"Acme Hospital","123 Main St","01/01/2025"\n',
            "description,code,code_type\n",
        ]
        result = _try_parse_kv_preamble(lines, header_row_idx=2)
        assert result is None

    def test_returns_none_for_no_preamble(self):
        """Should return None when header_row_idx is 0."""
        lines = [
            "description,code,code_type\n",
        ]
        result = _try_parse_kv_preamble(lines, header_row_idx=0)
        assert result is None

    def test_strips_trailing_commas(self):
        """Should strip trailing CSV padding commas from values."""
        lines = [
            "Hospital Name:  Test Hospital,,,,,,,,\n",
            ",,,,,\n",
            "Description,Code\n",
        ]
        result = _try_parse_kv_preamble(lines, header_row_idx=2)
        assert result is not None
        assert result['hospital_name'] == 'Test Hospital'

    def test_multiple_kv_keys(self):
        """Should parse multiple known keys from separate lines."""
        lines = [
            "Hospital Name:  Test Hospital\n",
            "Hospital Address:  123 Main St\n",
            "CMS Certification Number:  450123\n",
            "Last Updated On:  2025-01-01\n",
            "Description,Code\n",
        ]
        result = _try_parse_kv_preamble(lines, header_row_idx=4)
        assert result is not None
        assert result['hospital_name'] == 'Test Hospital'
        assert result['hospital_address'] == '123 Main St'
        assert result['CMS Certification Number'] == '450123'
        assert result['last_updated_on'] == '2025-01-01'

    def test_ignores_unknown_keys(self):
        """Lines with unknown keys should not be included."""
        lines = [
            "Hospital Name:  Test Hospital\n",
            "Random Field:  some value\n",
            "Description,Code\n",
        ]
        result = _try_parse_kv_preamble(lines, header_row_idx=2)
        assert result is not None
        assert len(result) == 1
        assert 'hospital_name' in result

    def test_bom_handling(self):
        """Should handle BOM character in first line."""
        lines = [
            "\ufeffHospital Name:  BOM Hospital,,,\n",
            "Description,Code\n",
        ]
        result = _try_parse_kv_preamble(lines, header_row_idx=1)
        assert result is not None
        assert result['hospital_name'] == 'BOM Hospital'
