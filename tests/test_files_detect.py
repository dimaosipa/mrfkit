"""
Tests for detect_file_format() and _detect_format_from_content().

Covers extension-based detection, content-based fallback (magic bytes),
gzip-compressed files, ZIP archives, JSON, CSV, BOM handling,
and empty files.

Source: mrfkit.files :: detect_file_format, _detect_format_from_content
"""

import gzip
import tempfile
import zipfile
from pathlib import Path

import pytest

from mrfkit.files import (
    detect_file_format,
    _detect_format_from_content,
    _zip_single_member,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_tmp(content: bytes, suffix: str = "") -> Path:
    """Write *content* to a temporary file with the given suffix and return its path."""
    f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    f.write(content)
    f.close()
    return Path(f.name)


def _write_gzip(content: bytes, suffix: str = ".gz") -> Path:
    """Create a gzip-compressed temp file containing *content*."""
    f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    f.close()
    with gzip.open(f.name, "wb") as gz:
        gz.write(content)
    return Path(f.name)


def _write_zip(inner_name: str, content: bytes, suffix: str = ".zip") -> Path:
    """Create a ZIP archive with a single file inside."""
    f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    f.close()
    with zipfile.ZipFile(f.name, "w") as zf:
        zf.writestr(inner_name, content)
    return Path(f.name)


# ===========================================================================
# A) Extension-based detection (existing behaviour, regression tests)
# ===========================================================================

class TestDetectFileFormatByExtension:
    """detect_file_format uses the file extension when it's recognised."""

    def test_csv(self, tmp_path):
        p = tmp_path / "data.csv"
        p.write_text("a,b\n1,2\n")
        assert detect_file_format(p) == ("csv", None)

    def test_csv_gz(self, tmp_path):
        p = tmp_path / "data.csv.gz"
        p.write_bytes(gzip.compress(b"a,b\n1,2\n"))
        assert detect_file_format(p) == ("csv", "gz")

    def test_json(self, tmp_path):
        p = tmp_path / "data.json"
        p.write_text('{"key": "val"}')
        assert detect_file_format(p) == ("json", None)

    def test_json_gz(self, tmp_path):
        p = tmp_path / "data.json.gz"
        p.write_bytes(gzip.compress(b'{"key": "val"}'))
        assert detect_file_format(p) == ("json", "gz")

    def test_zip_csv(self, tmp_path):
        p = tmp_path / "data.zip"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("inner.csv", "a,b\n")
        assert detect_file_format(p) == ("csv", "zip")

    def test_zip_json(self, tmp_path):
        p = tmp_path / "data.zip"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("inner.json", '{"k": 1}')
        assert detect_file_format(p) == ("json", "zip")

    def test_zip_txt_csv_content(self, tmp_path):
        """ZIP with .txt inner file containing CSV data."""
        p = tmp_path / "data.zip"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("hospital_standardcharges.txt",
                         "code,description,price\n1,Test,100\n")
        assert detect_file_format(p) == ("csv", "zip")

    def test_zip_txt_json_content(self, tmp_path):
        """ZIP with .txt inner file containing JSON data."""
        p = tmp_path / "data.zip"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("hospital_standardcharges.txt",
                         '{"hospital": "Test", "charges": []}')
        assert detect_file_format(p) == ("json", "zip")

    def test_zip_txt_bom_csv(self, tmp_path):
        """ZIP with .txt inner file containing BOM + CSV data (Partners Healthcare pattern)."""
        p = tmp_path / "042768256_hospital_standardcharges.zip"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("042768256_hospital_standardcharges.txt",
                         "\ufeffhospital_name,last_updated\nTest Hospital,2025\n")
        assert detect_file_format(p) == ("csv", "zip")

    def test_zip_nested_gzip_raises(self, tmp_path):
        """ZIP containing a gzip-compressed inner file should raise, not misdetect."""
        p = tmp_path / "data.zip"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("data.csv.gz", gzip.compress(b"a,b\n1,2\n"))
        with pytest.raises(ValueError, match="gzip-compressed"):
            detect_file_format(p)

    def test_zip_nested_zip_raises(self, tmp_path):
        """ZIP containing another ZIP archive should raise, not misdetect."""
        import io
        inner_buf = io.BytesIO()
        with zipfile.ZipFile(inner_buf, "w") as inner_zf:
            inner_zf.writestr("data.csv", "a,b\n1,2\n")
        p = tmp_path / "data.zip"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("inner.zip", inner_buf.getvalue())
        # inner.zip has a recognised .zip extension but detect_format_from_zip
        # doesn't recurse - it checks extension only; however for an
        # unrecognised extension like .dat wrapping a ZIP, it should reject:
        p2 = tmp_path / "data2.zip"
        with zipfile.ZipFile(p2, "w") as zf:
            zf.writestr("inner.dat", inner_buf.getvalue())
        with pytest.raises(ValueError, match="another ZIP"):
            detect_file_format(p2)

    def test_case_insensitive(self, tmp_path):
        p = tmp_path / "DATA.CSV.GZ"
        p.write_bytes(gzip.compress(b"a,b\n1,2\n"))
        assert detect_file_format(p) == ("csv", "gz")


# ===========================================================================
# A1) macOS-zipped archives (__MACOSX / .DS_Store sidecars)
# ===========================================================================

class TestDetectFileFormatMacOSZip:
    """Finder/``ditto`` ZIPs bundle the real file with __MACOSX resource
    forks and .DS_Store entries. Those sidecars must be ignored so a
    single-CSV/JSON archive is not rejected as multi-file (the
    ``*.csv.zip`` ingest incident)."""

    def test_zip_csv_with_macosx_sidecar(self, tmp_path):
        p = tmp_path / "262332250_hospital_standardcharges.csv.zip"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("standardcharges.csv", "code,price\n1,100\n")
            zf.writestr("__MACOSX/._standardcharges.csv", b"\x00\x05\x16\x07resourcefork")
        assert detect_file_format(p) == ("csv", "zip")

    def test_zip_json_with_macosx_sidecar(self, tmp_path):
        p = tmp_path / "844845184_hospital_standardcharges.json.zip"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("standardcharges.json", '{"k": 1}')
            zf.writestr("__MACOSX/._standardcharges.json", b"\x00\x05\x16\x07resourcefork")
        assert detect_file_format(p) == ("json", "zip")

    def test_zip_csv_with_ds_store(self, tmp_path):
        p = tmp_path / "data.zip"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("inner.csv", "a,b\n1,2\n")
            zf.writestr(".DS_Store", b"\x00\x00\x00\x01Bud1")
        assert detect_file_format(p) == ("csv", "zip")

    def test_zip_with_directory_entry(self, tmp_path):
        """An explicit directory entry must not be counted as a data file."""
        p = tmp_path / "data.zip"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("payload/", b"")  # directory entry
            zf.writestr("payload/inner.csv", "a,b\n1,2\n")
        assert detect_file_format(p) == ("csv", "zip")

    def test_zip_two_real_files_still_rejected(self, tmp_path):
        """Two genuine data members remain an error - we must not guess."""
        p = tmp_path / "data.zip"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("one.csv", "a,b\n1,2\n")
            zf.writestr("two.csv", "c,d\n3,4\n")
        with pytest.raises(ValueError, match="exactly one file"):
            detect_file_format(p)

    def test_zip_only_sidecars_rejected(self, tmp_path):
        """An archive with no real data member is rejected, not silently empty."""
        p = tmp_path / "data.zip"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("__MACOSX/._ghost.csv", b"resourcefork")
        with pytest.raises(ValueError, match="exactly one file"):
            detect_file_format(p)

    def test_read_path_selects_real_member_when_sidecar_first(self, tmp_path):
        """The read helper must pick the genuine member regardless of order -
        a sidecar listed first must not be opened as the data file."""
        p = tmp_path / "data.zip"
        with zipfile.ZipFile(p, "w") as zf:
            # Write the sidecar first so namelist()[0] would have been wrong.
            zf.writestr("__MACOSX/._real.csv", b"resourcefork")
            zf.writestr("real.csv", "a,b\n1,2\n")
        with zipfile.ZipFile(p, "r") as zf:
            assert _zip_single_member(zf, p.name) == "real.csv"


# ===========================================================================
# A2) Extension-content mismatch correction
# ===========================================================================

class TestDetectFileFormatMismatch:
    """detect_file_format trusts content over a wrong extension."""

    def test_json_extension_csv_content(self, tmp_path):
        """The Marin General Hospital bug: .json file that's actually CSV."""
        p = tmp_path / "hospital_standardcharges.json"
        p.write_bytes(b"\xef\xbb\xbfhospital_name,last_updated\nMarin,2025\n")
        assert detect_file_format(p) == ("csv", None)

    def test_csv_extension_json_content(self, tmp_path):
        """Reverse mismatch: .csv file that's actually JSON."""
        p = tmp_path / "charges.csv"
        p.write_bytes(b'{"hospital": "Test", "charges": []}')
        assert detect_file_format(p) == ("json", None)

    def test_json_extension_gzip_csv_content(self, tmp_path):
        """A .json file that's actually a gzipped CSV."""
        p = tmp_path / "charges.json"
        p.write_bytes(gzip.compress(b"a,b\n1,2\n"))
        assert detect_file_format(p) == ("csv", "gz")

    def test_csv_extension_gzip_json_content(self, tmp_path):
        """A .csv file that's actually a gzipped JSON."""
        p = tmp_path / "charges.csv"
        p.write_bytes(gzip.compress(b'{"a": 1}'))
        assert detect_file_format(p) == ("json", "gz")

    def test_correct_json_extension_not_changed(self, tmp_path):
        """A .json file with actual JSON content stays json."""
        p = tmp_path / "charges.json"
        p.write_bytes(b'{"hospital": "Test"}')
        assert detect_file_format(p) == ("json", None)

    def test_correct_csv_extension_not_changed(self, tmp_path):
        """A .csv file with actual CSV content stays csv."""
        p = tmp_path / "charges.csv"
        p.write_text("a,b\n1,2\n")
        assert detect_file_format(p) == ("csv", None)

    def test_empty_json_file_trusts_extension(self, tmp_path):
        """An empty .json file - can't sniff, trust the extension."""
        p = tmp_path / "charges.json"
        p.write_bytes(b"")
        assert detect_file_format(p) == ("json", None)

    def test_empty_csv_file_trusts_extension(self, tmp_path):
        """An empty .csv file - can't sniff, trust the extension."""
        p = tmp_path / "charges.csv"
        p.write_bytes(b"")
        assert detect_file_format(p) == ("csv", None)

    def test_craneware_marin_exact_scenario(self, tmp_path):
        """Exact reproduction of the Marin General Hospital failure:
        file_path_hint gives .json extension, content is BOM+CSV."""
        p = tmp_path / "marinhealth-medical-center_standardcharges.json"
        content = (
            b"\xef\xbb\xbf"
            b"hospital_name,last_updated_on,hospital_location,"
            b"hospital_address,license_number | CMS Certification Number\r\n"
            b"Marin General Hospital,01/01/2025,Greenbrae,"
            b"\"250 Bon Air Rd, Greenbrae, CA 94904\",110000361\r\n"
        )
        p.write_bytes(content)
        assert detect_file_format(p) == ("csv", None)


# ===========================================================================
# B) Content-based fallback (no recognised extension)
# ===========================================================================

class TestDetectFileFormatByContent:
    """detect_file_format falls back to content sniffing for unknown extensions."""

    def test_plain_csv_no_extension(self):
        p = _write_tmp(b"code,description,price\nCPT1,Test,100\n", suffix="")
        assert detect_file_format(p) == ("csv", None)

    def test_plain_json_no_extension(self):
        p = _write_tmp(b'{"hospital": "Test"}', suffix="")
        assert detect_file_format(p) == ("json", None)

    def test_json_array_no_extension(self):
        p = _write_tmp(b'[{"a":1}]', suffix="")
        assert detect_file_format(p) == ("json", None)

    def test_gzip_csv_no_extension(self):
        p = _write_gzip(b"code,description\nCPT,Test\n", suffix="")
        assert detect_file_format(p) == ("csv", "gz")

    def test_gzip_json_no_extension(self):
        p = _write_gzip(b'{"hospital":"Test"}', suffix="")
        assert detect_file_format(p) == ("json", "gz")

    def test_zip_csv_no_extension(self):
        p = _write_zip("charges.csv", b"a,b\n1,2\n", suffix="")
        assert detect_file_format(p) == ("csv", "zip")

    def test_zip_json_no_extension(self):
        p = _write_zip("charges.json", b'{"k":1}', suffix="")
        assert detect_file_format(p) == ("json", "zip")

    def test_zip_txt_inner_no_extension(self):
        """ZIP (no outer extension) with .txt inner file - sniff content."""
        p = _write_zip("hospital_charges.txt", b"code,desc,price\n1,2,3\n", suffix="")
        assert detect_file_format(p) == ("csv", "zip")

    def test_unrecognised_extension_falls_back(self):
        """A file with extension '.dat' should be sniffed by content."""
        p = _write_tmp(b'{"data": true}', suffix=".dat")
        assert detect_file_format(p) == ("json", None)

    def test_mrf_extension_falls_back(self):
        """Mimics the Craneware URL that ends in /mrf with no extension."""
        p = _write_tmp(b"code,description,price\n1,2,3\n", suffix=".mrf")
        # .mrf is not a recognized extension - should sniff as CSV
        assert detect_file_format(p) == ("csv", None)


# ===========================================================================
# C) _detect_format_from_content directly
# ===========================================================================

class TestDetectFormatFromContent:
    """Direct tests for the content-sniffing function."""

    def test_plain_csv(self):
        p = _write_tmp(b"a,b,c\n1,2,3\n")
        assert _detect_format_from_content(p) == ("csv", None)

    def test_plain_json_object(self):
        p = _write_tmp(b'  {"key": "value"}')
        assert _detect_format_from_content(p) == ("json", None)

    def test_plain_json_array(self):
        p = _write_tmp(b'  [1, 2, 3]')
        assert _detect_format_from_content(p) == ("json", None)

    def test_csv_with_bom(self):
        p = _write_tmp(b"\xef\xbb\xbfcode,description\n")
        assert _detect_format_from_content(p) == ("csv", None)

    def test_json_with_bom(self):
        p = _write_tmp(b"\xef\xbb\xbf{\"hospital\": \"Test\"}")
        assert _detect_format_from_content(p) == ("json", None)

    def test_csv_with_leading_whitespace(self):
        p = _write_tmp(b"   \n  code,desc\n1,test\n")
        assert _detect_format_from_content(p) == ("csv", None)

    def test_json_with_leading_whitespace(self):
        p = _write_tmp(b"  \n\t {\"a\": 1}")
        assert _detect_format_from_content(p) == ("json", None)

    def test_gzip_csv(self):
        p = _write_gzip(b"a,b\n1,2\n")
        assert _detect_format_from_content(p) == ("csv", "gz")

    def test_gzip_json(self):
        p = _write_gzip(b'{"key": "val"}')
        assert _detect_format_from_content(p) == ("json", "gz")

    def test_gzip_csv_with_bom(self):
        p = _write_gzip(b"\xef\xbb\xbfa,b\n1,2\n")
        assert _detect_format_from_content(p) == ("csv", "gz")

    def test_gzip_json_with_bom(self):
        p = _write_gzip(b"\xef\xbb\xbf{\"a\": 1}")
        assert _detect_format_from_content(p) == ("json", "gz")

    def test_zip_csv(self):
        p = _write_zip("data.csv", b"a,b\n")
        assert _detect_format_from_content(p) == ("csv", "zip")

    def test_zip_json(self):
        p = _write_zip("data.json", b'{"a":1}')
        assert _detect_format_from_content(p) == ("json", "zip")

    def test_empty_file_raises(self):
        p = _write_tmp(b"")
        with pytest.raises(ValueError, match="empty"):
            _detect_format_from_content(p)

    def test_zip_txt_csv_content(self):
        """ZIP containing a .txt file with CSV content (Brigham & Women's Faulkner)."""
        p = _write_zip("hospital_standardcharges.txt", b"code,description,price\n1,Test,100\n")
        assert _detect_format_from_content(p) == ("csv", "zip")

    def test_zip_txt_json_content(self):
        """ZIP containing a .txt file with JSON content."""
        p = _write_zip("hospital_standardcharges.txt", b'{"hospital": "Test", "charges": []}')
        assert _detect_format_from_content(p) == ("json", "zip")

    def test_zip_txt_empty_raises(self):
        """ZIP containing an empty .txt file."""
        p = _write_zip("empty.txt", b"")
        with pytest.raises(ValueError, match="empty"):
            _detect_format_from_content(p)

    def test_craneware_csv_with_bom_and_preamble(self):
        """Simulate the Craneware Marin General Hospital file structure:
        BOM + 3 preamble lines + CSV data."""
        content = (
            b"\xef\xbb\xbf"
            b"Hospital Name,Marin General Hospital\r\n"
            b"Last Updated On,2025-01-01\r\n"
            b"Version,2.0.0\r\n"
            b"code|1,description|1,setting|1\r\n"
            b"CPT|12345,Test Procedure,ip\r\n"
        )
        p = _write_tmp(content)
        assert _detect_format_from_content(p) == ("csv", None)


# ===========================================================================
# D) ZIP integrity validation (corrupt / truncated archives)
# ===========================================================================

class TestValidateZipIntegrity:
    """Tests for validate_zip_integrity() and BadZipFile propagation."""

    def test_valid_zip_passes(self, tmp_path):
        """A well-formed ZIP should not raise."""
        from mrfkit.files import validate_zip_integrity
        p = tmp_path / "good.zip"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("data.csv", "a,b\n1,2\n")
        validate_zip_integrity(p)  # no exception

    def test_truncated_zip_raises_bad_zip_file(self, tmp_path):
        """A truncated ZIP (missing end-of-central-directory) should raise BadZipFile."""
        from mrfkit.files import validate_zip_integrity
        p = tmp_path / "truncated.zip"
        # Write a valid ZIP then chop it
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("data.csv", "a,b\n" + "1,2\n" * 1000)
        original = p.read_bytes()
        p.write_bytes(original[: len(original) // 2])
        with pytest.raises(zipfile.BadZipFile):
            validate_zip_integrity(p)

    def test_random_bytes_raises_bad_zip_file(self, tmp_path):
        """Random bytes with ZIP magic should raise BadZipFile."""
        from mrfkit.files import validate_zip_integrity
        p = tmp_path / "garbage.zip"
        p.write_bytes(b"PK\x03\x04" + b"\x00" * 100 + b"garbage data here")
        with pytest.raises(zipfile.BadZipFile):
            validate_zip_integrity(p)

    def test_empty_file_raises_bad_zip_file(self, tmp_path):
        """An empty file should raise BadZipFile."""
        from mrfkit.files import validate_zip_integrity
        p = tmp_path / "empty.zip"
        p.write_bytes(b"")
        with pytest.raises(zipfile.BadZipFile):
            validate_zip_integrity(p)

    def test_detect_file_format_rejects_corrupt_zip(self, tmp_path):
        """detect_file_format should propagate BadZipFile for corrupt .zip files."""
        p = tmp_path / "corrupt.zip"
        # Write a valid ZIP then truncate it
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("data.csv", "a,b\n" + "1,2\n" * 500)
        original = p.read_bytes()
        p.write_bytes(original[: len(original) // 3])
        with pytest.raises(zipfile.BadZipFile):
            detect_file_format(p)

    def test_detect_format_from_content_rejects_corrupt_zip(self, tmp_path):
        """Content-sniffed ZIP (no .zip extension) should also reject corrupt archives."""
        p = tmp_path / "data.dat"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("charges.csv", "a,b\n" + "1,2\n" * 500)
        original = p.read_bytes()
        p.write_bytes(original[: len(original) // 3])
        with pytest.raises(zipfile.BadZipFile):
            _detect_format_from_content(p)


# ===========================================================================
# E) UTF-16 encoded files
# ===========================================================================

class TestDetectFormatUTF16:
    """Files encoded in UTF-16 LE/BE should be correctly detected as JSON."""

    def test_utf16_le_bom_json_detected(self):
        """UTF-16 LE JSON with BOM (Ernest Health pattern)."""
        content = '{"hospital_name": "Test"}'.encode('utf-16-le')
        bom = b'\xff\xfe'
        p = _write_tmp(bom + content)
        assert _detect_format_from_content(p) == ("json", None)

    def test_utf16_be_bom_json_detected(self):
        """UTF-16 BE JSON with BOM."""
        content = '{"hospital_name": "Test"}'.encode('utf-16-be')
        bom = b'\xfe\xff'
        p = _write_tmp(bom + content)
        assert _detect_format_from_content(p) == ("json", None)

    def test_utf16_le_no_bom_json_detected(self):
        """UTF-16 LE JSON without BOM - heuristic detection."""
        content = '{"hospital_name": "Test"}'.encode('utf-16-le')
        p = _write_tmp(content)
        assert _detect_format_from_content(p) == ("json", None)

    def test_utf16_be_no_bom_json_detected(self):
        """UTF-16 BE JSON without BOM - heuristic detection."""
        content = '{"hospital_name": "Test"}'.encode('utf-16-be')
        p = _write_tmp(content)
        assert _detect_format_from_content(p) == ("json", None)

    def test_utf16_le_json_extension_not_overridden(self, tmp_path):
        """A .json file with UTF-16 LE content should stay json (not be misdetected as csv)."""
        content = b'\xff\xfe' + '{"hospital_name": "Rehab Hospital"}'.encode('utf-16-le')
        p = tmp_path / "charges.json"
        p.write_bytes(content)
        assert detect_file_format(p) == ("json", None)

    def test_utf16_le_json_array_detected(self):
        """UTF-16 LE JSON array with BOM."""
        content = b'\xff\xfe' + '[{"a": 1}]'.encode('utf-16-le')
        p = _write_tmp(content)
        assert _detect_format_from_content(p) == ("json", None)

    def test_utf16_le_csv_detected(self):
        """UTF-16 LE CSV content should still be detected as csv."""
        content = b'\xff\xfe' + 'code,description,price\n1,Test,100\n'.encode('utf-16-le')
        p = _write_tmp(content)
        assert _detect_format_from_content(p) == ("csv", None)

    def test_utf16_gzip_json_detected(self):
        """Gzip-compressed UTF-16 LE JSON should be detected correctly."""
        content = b'\xff\xfe' + '{"hospital": "Test"}'.encode('utf-16-le')
        p = _write_gzip(content)
        assert _detect_format_from_content(p) == ("json", "gz")


# ===========================================================================
# F) XLSX / XLS rejection
# ===========================================================================

class TestXlsxRejection:
    """XLSX files (which are ZIP archives) must be rejected with a clear message."""

    def test_xlsx_extension_rejected(self, tmp_path):
        """A file with .xlsx extension is rejected before content sniffing."""
        p = tmp_path / "charges.xlsx"
        # Write minimal ZIP bytes so it looks like a real file
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("[Content_Types].xml", "<Types></Types>")
        with pytest.raises(ValueError, match="Excel/XLSX.*not supported"):
            detect_file_format(p)

    def test_xls_extension_rejected(self, tmp_path):
        """A file with .xls extension is rejected."""
        p = tmp_path / "charges.xls"
        p.write_bytes(b"\xd0\xcf\x11\xe0" + b"\x00" * 100)
        with pytest.raises(ValueError, match="Excel/XLSX.*not supported"):
            detect_file_format(p)

    def test_xlsx_extension_case_insensitive(self, tmp_path):
        """XLSX rejection should be case-insensitive."""
        p = tmp_path / "DATA.XLSX"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("[Content_Types].xml", "<Types></Types>")
        with pytest.raises(ValueError, match="Excel/XLSX.*not supported"):
            detect_file_format(p)

    def test_xlsx_content_detected_no_extension(self):
        """A ZIP file (no .xlsx ext) containing XLSX structure is rejected by content sniffing."""
        # Create a minimal XLSX-like ZIP with [Content_Types].xml
        p = _write_tmp(b"", suffix=".dat")
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("[Content_Types].xml", "<Types></Types>")
            zf.writestr("xl/workbook.xml", "<workbook></workbook>")
        # _detect_format_from_content should reject this via _reject_if_xlsx
        with pytest.raises(ValueError, match="Excel/XLSX.*not supported"):
            _detect_format_from_content(p)

    def test_xlsx_xl_directory_detected(self):
        """XLSX with xl/ directory entries (but no [Content_Types].xml) is rejected."""
        p = _write_tmp(b"", suffix=".dat")
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("xl/worksheets/sheet1.xml", "<worksheet></worksheet>")
            zf.writestr("xl/sharedStrings.xml", "<sst></sst>")
        with pytest.raises(ValueError, match="Excel/XLSX.*not supported"):
            _detect_format_from_content(p)

    def test_normal_zip_not_rejected_as_xlsx(self, tmp_path):
        """A normal ZIP with a CSV inside should NOT be rejected as XLSX."""
        p = tmp_path / "data.zip"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("charges.csv", "code,description,price\n1,Test,100\n")
        fmt, comp = detect_file_format(p)
        assert fmt == "csv"
        assert comp == "zip"

    def test_xlsx_error_message_includes_filename(self, tmp_path):
        """Error message should include the filename for debugging."""
        p = tmp_path / "my_hospital_charges.xlsx"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("[Content_Types].xml", "<Types></Types>")
        with pytest.raises(ValueError, match="my_hospital_charges.xlsx"):
            detect_file_format(p)

    def test_xlsx_with_zip_extension_rejected(self, tmp_path):
        """An XLSX file saved with .zip extension is still rejected."""
        p = tmp_path / "hospital_charges.zip"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("[Content_Types].xml", "<Types></Types>")
            zf.writestr("xl/workbook.xml", "<workbook></workbook>")
        with pytest.raises(ValueError, match="Excel/XLSX.*not supported"):
            detect_file_format(p)
