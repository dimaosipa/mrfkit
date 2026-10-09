"""
Tests for JSON byte-level sanitization in the ingestion pipeline.

Covers: sanitize(), open_json(sanitize=True)(), open_json()
Source: mrfkit.files.py
"""

import gzip
import io
import json
import tempfile
from pathlib import Path

import pytest

import re

from mrfkit.files import (
    JsonSanitizingReader,
    Utf8SanitizingReader,
    _detect_utf16_encoding,
    _transcode_to_utf8,
    open_json,
)


def sanitize(data: bytes) -> bytes:
    """Run *data* through the repair stack open_json(sanitize=True) applies."""
    return JsonSanitizingReader(Utf8SanitizingReader(io.BytesIO(data))).read()


def fix_utf8(data: bytes) -> bytes:
    """Run *data* through the UTF-8 repair open_json applies in both modes."""
    return Utf8SanitizingReader(io.BytesIO(data)).read()


# Independent whole-buffer implementation of the same repairs. The streaming
# reader is checked against it with tiny chunk sizes to shake out boundary bugs.
_JSON_CTRL_CHAR_RE = re.compile(b'[\x00-\x08\x0b\x0c\x0e-\x1f]')


def _fix_invalid_utf8_reference(data: bytes) -> bytes:
    return data.decode('utf-8', errors='replace').encode('utf-8')


def _reference_sanitize(data: bytes) -> bytes:
    """Fix common JSON byte-level errors in a complete JSON byte string.

    This is **string-aware**: structural comma fixes (double commas,
    trailing commas) are only applied outside quoted JSON strings.
    Control characters are stripped globally (they are never valid in JSON).

    Must be called on the *complete* file content - not on arbitrary
    chunks - because quote tracking requires seeing the full stream.
    """
    # Transcode UTF-16 to UTF-8 first (e.g. Ernest Health files).
    data = _transcode_to_utf8(data)
    # Strip UTF-8 BOM - valid UTF-8 but not valid JSON.
    if data[:3] == b'\xef\xbb\xbf':
        data = data[3:]
    # Fix invalid UTF-8 sequences (e.g. Windows-1252 stray bytes).
    data = _fix_invalid_utf8_reference(data)
    # Strip control characters globally (never valid in JSON).
    data = _JSON_CTRL_CHAR_RE.sub(b'', data)

    # Fast path: nothing to fix if no commas at all.
    if b',' not in data:
        return data

    out = bytearray()
    in_string = False
    escaped = False
    i = 0
    n = len(data)

    while i < n:
        bch = data[i]

        if in_string:
            out.append(bch)
            if escaped:
                escaped = False
            else:
                if bch == 0x5C:   # backslash
                    escaped = True
                elif bch == 0x22: # double quote
                    in_string = False
            i += 1
            continue

        # Outside of strings.
        if bch == 0x22:  # opening quote
            in_string = True
            out.append(bch)
            i += 1
            continue

        if bch == 0x2C:  # comma
            # Look ahead past whitespace to see what follows.
            j = i + 1
            while j < n and data[j] in b' \t\r\n':
                j += 1
            # Check for duplicate commas (,,) possibly separated by whitespace.
            if j < n and data[j] == 0x2C:
                # Collapse run of extra commas (and their whitespace).
                while j < n and data[j] == 0x2C:
                    j += 1
                    while j < n and data[j] in b' \t\r\n':
                        j += 1
                # After collapsing, if next char is ] or }, drop comma entirely.
                if j < n and data[j] in (0x5D, 0x7D):  # ] or }
                    # Emit the whitespace between original comma pos and the
                    # closing bracket, preserving formatting, then skip comma.
                    i = j
                    continue
                # Emit single comma + everything from (i+1) to start of the
                # duplicate comma run was whitespace - preserve it.
                out.append(0x2C)
                # Re-emit any whitespace that was between the first comma and
                # the second (now-removed) comma to preserve formatting.
                k = i + 1
                while k < n and data[k] in b' \t\r\n':
                    out.append(data[k])
                    k += 1
                i = j
                continue
            # Single comma: check for trailing comma (comma followed by ] or }).
            if j < n and data[j] in (0x5D, 0x7D):  # ] or }
                # Trailing comma - drop the comma, preserve whitespace.
                for k in range(i + 1, j):
                    out.append(data[k])
                i = j
                continue
            # Normal comma - emit it and all following whitespace as-is.
            out.append(bch)
            i += 1
            continue

        out.append(bch)
        i += 1

    return bytes(out)


# ============================================================================
# _sanitize_json_bytes - unit tests
# ============================================================================

class TestSanitizeJsonBytes:
    """Low-level byte sanitization."""

    def test_clean_data_unchanged(self):
        data = b'{"key": "value", "arr": [1, 2, 3]}'
        assert sanitize(data) == data

    def test_double_comma_removed(self):
        data = b'[{"a":1},,{"b":2}]'
        assert sanitize(data) == b'[{"a":1},{"b":2}]'

    def test_double_comma_with_whitespace(self):
        data = b'[{"a":1}, ,{"b":2}]'
        result = sanitize(data)
        assert b',,' not in result
        assert json.loads(result) == [{"a": 1}, {"b": 2}]

    def test_triple_comma(self):
        """Triple comma should collapse to single."""
        data = b'[1,,,2]'
        result = sanitize(data)
        assert b',,,' not in result
        assert b',,' not in result
        assert json.loads(result) == [1, 2]

    def test_trailing_comma_in_array(self):
        data = b'[1, 2, 3,]'
        assert sanitize(data) == b'[1, 2, 3]'

    def test_trailing_comma_in_object(self):
        data = b'{"a": 1, "b": 2,}'
        assert sanitize(data) == b'{"a": 1, "b": 2}'

    def test_trailing_comma_with_whitespace(self):
        data = b'[1, 2, 3, ]'
        result = sanitize(data)
        assert json.loads(result) == [1, 2, 3]

    def test_control_chars_stripped(self):
        data = b'{"key": "val\x01ue\x02"}'
        assert sanitize(data) == b'{"key": "value"}'

    def test_null_byte_stripped(self):
        data = b'{"key": "val\x00ue"}'
        assert sanitize(data) == b'{"key": "value"}'

    def test_tab_and_newline_preserved(self):
        """Tab (0x09), LF (0x0a), CR (0x0d) are valid JSON whitespace."""
        data = b'{\t"key":\n"value"\r\n}'
        assert sanitize(data) == data

    def test_langley_porter_pattern(self):
        """Pattern from the Langley Porter file (complete JSON context)."""
        data = (b'[{"standard_charge_information":[{"description":'
                b'"Shared Hosp Anc Profees"}]},,{"description":'
                b'"Pr Needle Biop"}]')
        result = sanitize(data)
        assert b',,' not in result
        parsed = json.loads(result)
        assert parsed[0]["standard_charge_information"][0]["description"] == "Shared Hosp Anc Profees"
        assert parsed[1]["description"] == "Pr Needle Biop"

    def test_empty_input(self):
        assert sanitize(b'') == b''

    def test_multiple_issues_combined(self):
        """File with control chars, double commas, and trailing commas."""
        data = b'[{"a": "x\x01y"},,{"b": 2,}]'
        result = sanitize(data)
        assert result == b'[{"a": "xy"},{"b": 2}]'
        parsed = json.loads(result)
        assert parsed == [{"a": "xy"}, {"b": 2}]


# ============================================================================
# String-awareness: commas inside quoted strings must be preserved
# ============================================================================

class TestStringAwareness:
    """Verify that sanitization only touches structural commas, not string content."""

    def test_double_comma_inside_string_preserved(self):
        """A literal ',,' inside a JSON string value must NOT be changed."""
        data = b'{"description": "a,,b"}'
        result = sanitize(data)
        assert result == data
        assert json.loads(result) == {"description": "a,,b"}

    def test_trailing_comma_pattern_inside_string_preserved(self):
        """A literal ',]' inside a string must NOT be treated as trailing comma."""
        data = b'{"note": "values: 1,2,]end"}'
        result = sanitize(data)
        assert result == data
        assert json.loads(result) == {"note": "values: 1,2,]end"}

    def test_structural_double_comma_with_string_containing_commas(self):
        """Fix structural ,, while preserving commas inside strings."""
        data = b'[{"desc": "a,,b"},,{"desc": "c,,d"}]'
        result = sanitize(data)
        parsed = json.loads(result)
        assert parsed == [{"desc": "a,,b"}, {"desc": "c,,d"}]

    def test_escaped_quote_inside_string(self):
        r"""Escaped quotes inside strings must not confuse quote tracking."""
        data = b'{"key": "say \\"hello,, world\\""}'
        result = sanitize(data)
        assert result == data  # no structural commas to fix
        assert json.loads(result) == {"key": 'say "hello,, world"'}

    def test_escaped_backslash_before_quote(self):
        r"""Escaped backslash (\\) before quote should end the string."""
        # In JSON: {"k": "val\\"} means key "k" has value "val\"
        # The \\\\ in the byte literal is \\ in the actual bytes,
        # and then the " after it ends the string.
        data = b'[{"k": "val\\\\"},,{"k2": "v2"}]'
        result = sanitize(data)
        parsed = json.loads(result)
        assert parsed == [{"k": "val\\"}, {"k2": "v2"}]

    def test_no_commas_fast_path(self):
        """Data with no commas should pass through unchanged (fast path)."""
        data = b'{"key": "value"}'
        assert sanitize(data) == data

    def test_comma_inside_string_near_structural_issue(self):
        """Complex interleaving: string commas adjacent to structural issues."""
        data = b'[{"a": "x,y"},,{"b": "p,q",}]'
        result = sanitize(data)
        parsed = json.loads(result)
        assert parsed == [{"a": "x,y"}, {"b": "p,q"}]


# ============================================================================
# open_json(sanitize=True) - integration tests with real files
# ============================================================================

class TestOpenJsonSanitized:
    """Test file-level sanitization with temp files."""

    def _write_temp(self, data: bytes, suffix='.json', compress=False):
        """Write data to a temp file, optionally gzipped."""
        if compress:
            suffix = '.json.gz'
        f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        if compress:
            f.close()
            with gzip.open(f.name, 'wb') as gz:
                gz.write(data)
        else:
            f.write(data)
            f.close()
        return Path(f.name)

    def test_clean_file_not_modified(self):
        data = b'{"items": [1, 2, 3]}'
        path = self._write_temp(data)
        try:
            with open_json(path, None, sanitize=True) as fh:
                buf = io.BytesIO(fh.read())
            assert buf.read() == data
        finally:
            path.unlink()

    def test_dirty_file_is_modified(self):
        data = b'{"items": [1,,2]}'
        path = self._write_temp(data)
        try:
            with open_json(path, None, sanitize=True) as fh:
                buf = io.BytesIO(fh.read())
            result = buf.read()
            assert b',,' not in result
            assert json.loads(result) == {"items": [1, 2]}
        finally:
            path.unlink()

    def test_gzipped_file(self):
        data = b'[{"a":1},,{"b":2}]'
        path = self._write_temp(data, compress=True)
        try:
            with open_json(path, 'gz', sanitize=True) as fh:
                buf = io.BytesIO(fh.read())
            result = buf.read()
            assert json.loads(result) == [{"a": 1}, {"b": 2}]
        finally:
            path.unlink()

    def test_large_file(self):
        """Verify sanitization works on files larger than 1 MB."""
        # Build a ~1.5 MB array with a double comma buried inside.
        # Each item is ~520 bytes: {"description": "xxx...xxx"}
        item = b'{"description": "' + b'x' * 500 + b'"}'
        chunks = [item] * 3000  # ~1.5 MB
        arr = b'[' + b','.join(chunks) + b']'
        # Find a comma between items that's past 1 MB and double it.
        target = 1024 * 1024
        # Search forward from target to find a structural comma
        # (commas between items are right after '}').
        pos = arr.index(b'},', target)
        insert_at = pos + 1  # position of the comma itself
        arr = arr[:insert_at] + b',' + arr[insert_at:]  # ,, at this position
        assert b',,' in arr  # sanity check
        path = self._write_temp(arr)
        try:
            with open_json(path, None, sanitize=True) as fh:
                buf = io.BytesIO(fh.read())
            result = buf.read()
            assert b',,' not in result
            parsed = json.loads(result)
            assert isinstance(parsed, list)
        finally:
            path.unlink()

    def test_string_content_preserved_in_file(self):
        """File-level: commas inside strings must survive sanitization."""
        data = b'[{"desc": "a,,b"},,{"desc": "c,,d"}]'
        path = self._write_temp(data)
        try:
            with open_json(path, None, sanitize=True) as fh:
                buf = io.BytesIO(fh.read())
            result = buf.read()
            parsed = json.loads(result)
            assert parsed[0]["desc"] == "a,,b"
            assert parsed[1]["desc"] == "c,,d"
        finally:
            path.unlink()


# ============================================================================
# open_json - context manager integration test
# ============================================================================

class TestOpenFileBinary:
    """Verify open_json yields sanitized data."""

    def _write_temp(self, data: bytes, suffix='.json'):
        f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        f.write(data)
        f.close()
        return Path(f.name)

    def test_sanitized_output(self):
        """open_json should yield sanitized bytes."""
        data = b'[{"a":1},,{"b":2}]'
        path = self._write_temp(data)
        try:
            with open_json(path, None, sanitize=True) as fh:
                result = fh.read()
            assert json.loads(result) == [{"a": 1}, {"b": 2}]
        finally:
            path.unlink()

    def test_clean_file_passes_through(self):
        """Clean files should pass through unchanged."""
        data = b'{"key": "value"}'
        path = self._write_temp(data)
        try:
            with open_json(path, None, sanitize=True) as fh:
                result = fh.read()
            assert result == data
        finally:
            path.unlink()


# ============================================================================
# open_json() - raw streaming mode
# ============================================================================

class TestOpenFileBinaryRawMode:
    """Verify open_json() streams without loading into memory."""

    def _write_temp(self, data: bytes, suffix='.json', compress=False):
        if compress:
            suffix = '.json.gz'
        f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        if compress:
            f.write(gzip.compress(data))
        else:
            f.write(data)
        f.close()
        return Path(f.name)

    def test_raw_reads_clean_json(self):
        """Raw mode should read well-formed JSON without modification."""
        data = b'{"key": "value", "arr": [1, 2, 3]}'
        path = self._write_temp(data)
        try:
            with open_json(path, None) as fh:
                result = fh.read()
            assert result == data
        finally:
            path.unlink()

    def test_raw_with_gzip(self):
        """Raw mode should decompress gzip files."""
        data = b'[{"a": 1}, {"b": 2}]'
        path = self._write_temp(data, compress=True)
        try:
            with open_json(path, 'gz') as fh:
                result = fh.read()
            assert result == data
        finally:
            path.unlink()

    def test_raw_does_not_sanitize(self):
        """Raw mode should NOT fix malformed JSON - caller handles fallback."""
        data = b'[{"a":1},,{"b":2}]'
        path = self._write_temp(data)
        try:
            with open_json(path, None) as fh:
                result = fh.read()
            # Raw mode returns the original bytes, including the double comma
            assert result == data
        finally:
            path.unlink()

    def test_raw_ijson_parses_clean_json(self):
        """ijson should parse clean JSON in raw mode."""
        import ijson
        data = b'{"standard_charge_information": [{"description": "First"}, {"description": "Second"}]}'
        path = self._write_temp(data)
        try:
            with open_json(path, None) as fh:
                items = list(ijson.items(fh, 'standard_charge_information.item'))
            assert len(items) == 2
            assert items[0]['description'] == 'First'
        finally:
            path.unlink()

    def test_raw_ijson_raises_on_malformed_json(self):
        """ijson should raise JSONError in raw mode for malformed JSON."""
        import ijson
        data = b'{"items": [{"a":1},,{"b":2}]}'
        path = self._write_temp(data)
        try:
            with pytest.raises(ijson.JSONError):
                with open_json(path, None) as fh:
                    list(ijson.items(fh, 'items.item'))
        finally:
            path.unlink()


# ============================================================================
# End-to-end: verify ijson can parse sanitized output
# ============================================================================

class TestIjsonIntegration:
    """Verify that sanitized output is parseable by ijson."""

    def test_ijson_parses_fixed_double_comma(self):
        """ijson should be able to parse data with double commas after sanitization."""
        import ijson

        # Simulate the Langley Porter pattern
        data = b'{"standard_charge_information": [{"description": "First"},, {"description": "Second"}]}'
        path = tempfile.NamedTemporaryFile(suffix='.json', delete=False)
        path.write(data)
        path.close()
        path = Path(path.name)

        try:
            with open_json(path, None, sanitize=True) as fh:
                items = list(ijson.items(fh, 'standard_charge_information.item'))
            assert len(items) == 2
            assert items[0]['description'] == 'First'
            assert items[1]['description'] == 'Second'
        finally:
            path.unlink()

    def test_ijson_parses_fixed_trailing_comma(self):
        """ijson should handle trailing commas after sanitization."""
        import ijson

        data = b'{"items": [{"a": 1}, {"b": 2},]}'
        path = tempfile.NamedTemporaryFile(suffix='.json', delete=False)
        path.write(data)
        path.close()
        path = Path(path.name)

        try:
            with open_json(path, None, sanitize=True) as fh:
                items = list(ijson.items(fh, 'items.item'))
            assert len(items) == 2
        finally:
            path.unlink()

    def test_ijson_with_string_commas_preserved(self):
        """ijson should see original string values after sanitization fixes structural issues."""
        import ijson

        data = b'{"items": [{"desc": "a,,b"},, {"desc": "c,]d",}]}'
        path = tempfile.NamedTemporaryFile(suffix='.json', delete=False)
        path.write(data)
        path.close()
        path = Path(path.name)

        try:
            with open_json(path, None, sanitize=True) as fh:
                items = list(ijson.items(fh, 'items.item'))
            assert len(items) == 2
            assert items[0]['desc'] == 'a,,b'
            assert items[1]['desc'] == 'c,]d'
        finally:
            path.unlink()


# ============================================================================
# _fix_invalid_utf8 - unit tests
# ============================================================================

class TestFixInvalidUtf8:
    """Test the low-level UTF-8 fix function."""

    REPLACEMENT = '\ufffd'  # U+FFFD replacement character
    REPLACEMENT_BYTES = REPLACEMENT.encode('utf-8')  # b'\xef\xbf\xbd'

    def test_valid_utf8_unchanged(self):
        data = b'{"description": "hello world"}'
        assert fix_utf8(data) == data

    def test_valid_utf8_with_multibyte(self):
        """Valid multi-byte UTF-8 chars should pass through."""
        data = 'résumé \u2014 done'.encode('utf-8')
        assert fix_utf8(data) == data

    def test_windows_1252_nbsp_replaced(self):
        """\\xa0 (Windows-1252 non-breaking space) should be replaced."""
        data = b'COIL EMBO SOFT\xa0CMPLX'
        result = fix_utf8(data)
        assert b'\xa0' not in result
        assert self.REPLACEMENT_BYTES in result

    def test_windows_1252_endash_replaced(self):
        """\\x96 (Windows-1252 en-dash) should be replaced."""
        data = b'CHIP CANC FRZ DRY1.7\x9610MM'
        result = fix_utf8(data)
        assert b'\x96' not in result
        assert self.REPLACEMENT_BYTES in result

    def test_multiple_invalid_bytes(self):
        """Multiple invalid bytes at different positions."""
        data = b'abc\x80def\xff ghi'
        result = fix_utf8(data)
        assert b'\x80' not in result
        assert b'\xff' not in result
        text = result.decode('utf-8')
        assert 'abc' in text
        assert 'def' in text
        assert 'ghi' in text

    def test_empty_input(self):
        assert fix_utf8(b'') == b''

    def test_all_ascii(self):
        data = b'hello world 123'
        assert fix_utf8(data) == data

    def test_result_is_valid_utf8(self):
        """Output must always be valid UTF-8."""
        # Feed in a mix of valid and invalid bytes
        data = b'\xc0\xaf\xe0\x80\xbf\xff\xfe valid text \xa0 end'
        result = fix_utf8(data)
        # This must not raise
        result.decode('utf-8', errors='strict')

    def test_nyt_presbyterian_pattern(self):
        """The exact pattern from the NewYork-Presbyterian error."""
        data = (b'"description":"COIL EMBO SOFT\xa0CMPLX 10CMX4MM",'
                b'"code_informat')
        result = fix_utf8(data)
        result.decode('utf-8', errors='strict')  # must not raise

    def test_chip_canc_pattern(self):
        """The exact pattern from the second NYP error."""
        data = (b'iption":"CHIP CANC FRZ DRY1.7\x9610MM 60CC",'
                b'"code_information":')
        result = fix_utf8(data)
        result.decode('utf-8', errors='strict')  # must not raise


# ============================================================================
# _sanitize_json_bytes - UTF-8 handling
# ============================================================================

class TestSanitizeJsonBytesUtf8:
    """Verify _sanitize_json_bytes also fixes invalid UTF-8."""

    def test_invalid_utf8_in_json_value(self):
        """Invalid UTF-8 in a JSON string value should be replaced, not crash."""
        data = b'{"description": "COIL EMBO SOFT\xa0CMPLX"}'
        result = sanitize(data)
        parsed = json.loads(result)
        assert 'COIL EMBO SOFT' in parsed['description']
        assert 'CMPLX' in parsed['description']

    def test_invalid_utf8_plus_double_comma(self):
        """Both issues in one file: invalid UTF-8 AND structural double comma."""
        data = b'[{"desc": "a\xa0b"},,{"desc": "c"}]'
        result = sanitize(data)
        parsed = json.loads(result)
        assert len(parsed) == 2
        assert 'a' in parsed[0]['desc']
        assert 'b' in parsed[0]['desc']
        assert parsed[1]['desc'] == 'c'

    def test_invalid_utf8_plus_trailing_comma(self):
        data = b'{"desc": "val\x96ue",}'
        result = sanitize(data)
        parsed = json.loads(result)
        assert 'val' in parsed['desc']


# ============================================================================
# Utf8SanitizingReader - unit tests
# ============================================================================

class TestUtf8SanitizingReader:
    """Test the streaming UTF-8 sanitizer."""

    def test_valid_utf8_passthrough(self):
        data = b'{"key": "value", "num": 42}'
        reader = Utf8SanitizingReader(io.BytesIO(data))
        assert reader.read() == data

    def test_invalid_byte_replaced_in_read(self):
        data = b'hello\xa0world'
        reader = Utf8SanitizingReader(io.BytesIO(data))
        result = reader.read()
        assert b'\xa0' not in result
        result.decode('utf-8', errors='strict')  # must not raise

    def test_read_with_size(self):
        """Reading in fixed-size chunks should still produce valid UTF-8
        and each chunk must be at most n bytes (the read(n) contract)."""
        data = b'{"desc": "COIL\xa0CMPLX", "code": "abc\x96def"}'
        reader = Utf8SanitizingReader(io.BytesIO(data))
        chunks = []
        while True:
            chunk = reader.read(10)
            if not chunk:
                break
            assert len(chunk) <= 10, f"read(10) returned {len(chunk)} bytes"
            chunks.append(chunk)
        result = b''.join(chunks)
        result.decode('utf-8', errors='strict')  # must not raise
        assert b'\xa0' not in result
        assert b'\x96' not in result

    def test_readline(self):
        data = b'line1\xa0val\nline2\x96val\n'
        reader = Utf8SanitizingReader(io.BytesIO(data))
        line1 = reader.readline()
        line2 = reader.readline()
        assert b'\xa0' not in line1
        assert b'\x96' not in line2
        line1.decode('utf-8', errors='strict')
        line2.decode('utf-8', errors='strict')

    def test_iter(self):
        data = b'line1\xa0\nline2\x96\n'
        reader = Utf8SanitizingReader(io.BytesIO(data))
        lines = list(reader)
        assert len(lines) == 2
        for line in lines:
            line.decode('utf-8', errors='strict')

    def test_empty_input(self):
        reader = Utf8SanitizingReader(io.BytesIO(b''))
        assert reader.read() == b''

    def test_multibyte_utf8_boundary(self):
        """A valid multi-byte UTF-8 char split across read boundaries.

        Uses small read(n) calls to force the incremental decoder to
        handle multi-byte sequences that straddle chunk boundaries.
        """
        # é = b'\xc3\xa9' in UTF-8
        data = 'café résumé'.encode('utf-8')
        reader = Utf8SanitizingReader(io.BytesIO(data), chunk_size=4)
        # Drive with small reads - some will land mid-sequence.
        chunks = []
        while True:
            chunk = reader.read(3)
            if not chunk:
                break
            assert len(chunk) <= 3, f"read(3) returned {len(chunk)} bytes"
            chunks.append(chunk)
        result = b''.join(chunks)
        assert result == data

    def test_seekable_and_readable(self):
        reader = Utf8SanitizingReader(io.BytesIO(b''))
        assert reader.seekable() is False
        assert reader.readable() is True


# ============================================================================
# open_json() - UTF-8 sanitization in raw mode
# ============================================================================

class TestOpenFileBinaryUtf8Raw:
    """Verify raw streaming mode sanitizes invalid UTF-8."""

    def _write_temp(self, data: bytes, suffix='.json', compress=False):
        if compress:
            suffix = '.json.gz'
        f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        if compress:
            f.write(gzip.compress(data))
        else:
            f.write(data)
        f.close()
        return Path(f.name)

    def test_raw_fixes_invalid_utf8(self):
        """Raw mode should now fix invalid UTF-8 bytes."""
        data = b'{"desc": "COIL\xa0CMPLX"}'
        path = self._write_temp(data)
        try:
            with open_json(path, None) as fh:
                result = fh.read()
            assert b'\xa0' not in result
            result.decode('utf-8', errors='strict')
        finally:
            path.unlink()

    def test_raw_gzip_fixes_invalid_utf8(self):
        """Raw gzip mode should fix invalid UTF-8."""
        data = b'{"desc": "CHIP\x9610MM"}'
        path = self._write_temp(data, compress=True)
        try:
            with open_json(path, 'gz') as fh:
                result = fh.read()
            assert b'\x96' not in result
            result.decode('utf-8', errors='strict')
        finally:
            path.unlink()

    def test_raw_ijson_parses_invalid_utf8(self):
        """ijson should now parse files with invalid UTF-8 in raw mode."""
        import ijson
        data = (b'{"items": [{"description": "COIL\xa0CMPLX"},'
                b' {"description": "CHIP\x9610MM"}]}')
        path = self._write_temp(data)
        try:
            with open_json(path, None) as fh:
                items = list(ijson.items(fh, 'items.item'))
            assert len(items) == 2
            assert 'COIL' in items[0]['description']
            assert 'CMPLX' in items[0]['description']
            assert 'CHIP' in items[1]['description']
            assert '10MM' in items[1]['description']
        finally:
            path.unlink()

    def test_raw_valid_utf8_unchanged(self):
        """Valid UTF-8 should pass through raw mode unchanged."""
        data = b'{"key": "value", "arr": [1, 2, 3]}'
        path = self._write_temp(data)
        try:
            with open_json(path, None) as fh:
                result = fh.read()
            assert result == data
        finally:
            path.unlink()

    def test_nyp_realistic_json(self):
        """Realistic NewYork-Presbyterian-style JSON with invalid UTF-8 bytes."""
        import ijson
        # Simulate a CMS-format JSON with invalid bytes in descriptions
        data = json.dumps({
            "standard_charge_information": [
                {"description": "NORMAL ITEM", "code": "12345"},
            ]
        }).encode('utf-8')
        # Inject invalid bytes into the description
        data = data.replace(b'NORMAL ITEM', b'COIL EMBO SOFT\xa0CMPLX 10CMX4MM')
        path = self._write_temp(data)
        try:
            with open_json(path, None) as fh:
                items = list(ijson.items(fh, 'standard_charge_information.item'))
            assert len(items) == 1
            assert 'COIL EMBO SOFT' in items[0]['description']
            assert 'CMPLX 10CMX4MM' in items[0]['description']
        finally:
            path.unlink()


# ============================================================================
# _detect_utf16_encoding - unit tests
# ============================================================================

class TestDetectUtf16Encoding:
    """Test shared UTF-16 encoding detection helper."""

    def test_utf16_le_bom(self):
        assert _detect_utf16_encoding(b'\xff\xfe{\x00') == 'utf-16-le'

    def test_utf16_be_bom(self):
        assert _detect_utf16_encoding(b'\xfe\xff\x00{') == 'utf-16-be'

    def test_utf16_le_no_bom(self):
        data = '{"a":1}'.encode('utf-16-le')
        assert _detect_utf16_encoding(data) == 'utf-16-le'

    def test_utf16_be_no_bom(self):
        data = '{"a":1}'.encode('utf-16-be')
        assert _detect_utf16_encoding(data) == 'utf-16-be'

    def test_utf8_returns_none(self):
        assert _detect_utf16_encoding(b'{"a":1}') is None

    def test_empty_returns_none(self):
        assert _detect_utf16_encoding(b'') is None

    def test_single_byte_returns_none(self):
        assert _detect_utf16_encoding(b'{') is None


# ============================================================================
# _transcode_to_utf8 - unit tests
# ============================================================================

class TestTranscodeToUtf8:
    """Test UTF-16 → UTF-8 transcoding."""

    def test_utf8_unchanged(self):
        data = b'{"key": "value"}'
        assert _transcode_to_utf8(data) == data

    def test_utf16_le_bom(self):
        """UTF-16 LE with BOM should be transcoded to UTF-8."""
        text = '{"hospital": "Test"}'
        data = b'\xff\xfe' + text.encode('utf-16-le')
        result = _transcode_to_utf8(data)
        assert result == text.encode('utf-8')

    def test_utf16_be_bom(self):
        """UTF-16 BE with BOM should be transcoded to UTF-8."""
        text = '{"hospital": "Test"}'
        data = b'\xfe\xff' + text.encode('utf-16-be')
        result = _transcode_to_utf8(data)
        assert result == text.encode('utf-8')

    def test_utf16_le_no_bom_heuristic(self):
        """UTF-16 LE without BOM, detected by NUL-byte heuristic."""
        text = '{"hospital": "Test Hospital"}'
        data = text.encode('utf-16-le')
        result = _transcode_to_utf8(data)
        assert result == text.encode('utf-8')

    def test_utf16_be_no_bom_heuristic(self):
        """UTF-16 BE without BOM, detected by NUL-byte heuristic."""
        text = '{"hospital": "Test Hospital"}'
        data = text.encode('utf-16-be')
        result = _transcode_to_utf8(data)
        assert result == text.encode('utf-8')

    def test_empty_input(self):
        assert _transcode_to_utf8(b'') == b''

    def test_single_byte(self):
        assert _transcode_to_utf8(b'{') == b'{'

    def test_real_ernest_health_pattern(self):
        """Exact pattern from the Ernest Health file that triggered the bug."""
        # UTF-16 LE BOM + JSON object
        text = '{\r\n  "hospital_name": "Rehabilitation Hospital of Southern California"}'
        data = b'\xff\xfe' + text.encode('utf-16-le')
        result = _transcode_to_utf8(data)
        assert result == text.encode('utf-8')
        assert json.loads(result)['hospital_name'] == "Rehabilitation Hospital of Southern California"


# ============================================================================
# _sanitize_json_bytes with UTF-16 input
# ============================================================================

class TestSanitizeJsonBytesUtf16:
    """Verify _sanitize_json_bytes handles UTF-16 encoded input."""

    def test_utf16_le_sanitized(self):
        """UTF-16 LE JSON should be transcoded and sanitized."""
        text = '{"key": "value"}'
        data = b'\xff\xfe' + text.encode('utf-16-le')
        result = sanitize(data)
        assert json.loads(result) == {"key": "value"}

    def test_utf16_le_with_structural_issues(self):
        """UTF-16 LE JSON with double commas should be fixed."""
        text = '[{"a": 1},, {"b": 2}]'
        data = b'\xff\xfe' + text.encode('utf-16-le')
        result = sanitize(data)
        parsed = json.loads(result)
        assert parsed == [{"a": 1}, {"b": 2}]


# ============================================================================
# Utf8SanitizingReader with UTF-16 input
# ============================================================================

class TestUtf8SanitizingReaderUtf16:
    """Test streaming UTF-16 detection in Utf8SanitizingReader."""

    def test_utf16_le_bom_streaming(self):
        """UTF-16 LE BOM should be detected and transcoded during streaming."""
        text = '{"hospital": "Test"}'
        data = b'\xff\xfe' + text.encode('utf-16-le')
        reader = Utf8SanitizingReader(io.BytesIO(data))
        result = reader.read()
        assert result == text.encode('utf-8')

    def test_utf16_be_bom_streaming(self):
        """UTF-16 BE BOM should be detected and transcoded during streaming."""
        text = '{"hospital": "Test"}'
        data = b'\xfe\xff' + text.encode('utf-16-be')
        reader = Utf8SanitizingReader(io.BytesIO(data))
        result = reader.read()
        assert result == text.encode('utf-8')

    def test_utf16_le_chunked_read(self):
        """UTF-16 LE should work with small chunked reads."""
        text = '{"hospital": "Test Hospital Name"}'
        data = b'\xff\xfe' + text.encode('utf-16-le')
        reader = Utf8SanitizingReader(io.BytesIO(data), chunk_size=16)
        chunks = []
        while True:
            chunk = reader.read(10)
            if not chunk:
                break
            chunks.append(chunk)
        result = b''.join(chunks)
        assert result == text.encode('utf-8')

    def test_utf16_le_readline(self):
        """UTF-16 LE should work with readline."""
        text = '{"hospital": "Test"}\n{"other": "data"}\n'
        data = b'\xff\xfe' + text.encode('utf-16-le')
        reader = Utf8SanitizingReader(io.BytesIO(data))
        line1 = reader.readline()
        line2 = reader.readline()
        assert line1 == b'{"hospital": "Test"}\n'
        assert line2 == b'{"other": "data"}\n'


# ============================================================================
# open_json with UTF-16 files
# ============================================================================

class TestOpenFileBinaryUtf16:
    """End-to-end: open_json with UTF-16 encoded JSON files."""

    def _write_temp(self, data: bytes, suffix='.json'):
        f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        f.write(data)
        f.close()
        return Path(f.name)

    def test_sanitized_mode_utf16(self):
        """Sanitized mode should transcode UTF-16 LE to parseable UTF-8."""
        import ijson
        text = '{"items": [{"description": "Test"}]}'
        data = b'\xff\xfe' + text.encode('utf-16-le')
        path = self._write_temp(data)
        try:
            with open_json(path, None, sanitize=True) as fh:
                items = list(ijson.items(fh, 'items.item'))
            assert len(items) == 1
            assert items[0]['description'] == 'Test'
        finally:
            path.unlink()

    def test_raw_mode_utf16(self):
        """Raw mode should detect UTF-16 BOM and transcode to UTF-8."""
        import ijson
        text = '{"items": [{"description": "Test"}]}'
        data = b'\xff\xfe' + text.encode('utf-16-le')
        path = self._write_temp(data)
        try:
            with open_json(path, None) as fh:
                items = list(ijson.items(fh, 'items.item'))
            assert len(items) == 1
            assert items[0]['description'] == 'Test'
        finally:
            path.unlink()


# ============================================================================
# JsonSanitizingReader - streaming sanitizer
# ============================================================================

class TestJsonSanitizingReader:
    """Test the streaming JSON sanitizer matches _sanitize_json_bytes output."""

    def _stream_result(self, data: bytes, chunk_size: int = 256 * 1024) -> bytes:
        """Run data through JsonSanitizingReader and return all output."""
        reader = JsonSanitizingReader(io.BytesIO(data), chunk_size=chunk_size)
        return reader.read()

    def _batch_result(self, data: bytes) -> bytes:
        """Run data through _sanitize_json_bytes for comparison."""
        return _reference_sanitize(data)

    # -- Basic parity with _sanitize_json_bytes --

    def test_clean_data_unchanged(self):
        data = b'{"key": "value", "arr": [1, 2, 3]}'
        assert self._stream_result(data) == data

    def test_double_comma(self):
        data = b'[{"a":1},,{"b":2}]'
        assert self._stream_result(data) == self._batch_result(data)

    def test_triple_comma(self):
        data = b'[1,,,2,,,3]'
        assert self._stream_result(data) == self._batch_result(data)

    def test_trailing_comma_array(self):
        data = b'[1, 2, 3,]'
        assert self._stream_result(data) == self._batch_result(data)

    def test_trailing_comma_object(self):
        data = b'{"a": 1, "b": 2,}'
        assert self._stream_result(data) == self._batch_result(data)

    def test_double_comma_with_whitespace(self):
        data = b'[1, , 2]'
        assert self._stream_result(data) == self._batch_result(data)

    def test_control_chars_stripped(self):
        data = b'{"key": "val\x01ue\x02"}'
        assert self._stream_result(data) == self._batch_result(data)

    def test_comma_inside_string_preserved(self):
        data = b'{"key": "a,,b,,"}'
        assert self._stream_result(data) == data  # no change inside strings

    def test_trailing_comma_inside_string_preserved(self):
        data = b'{"key": "trailing,}"}'
        assert self._stream_result(data) == data

    def test_empty_input(self):
        assert self._stream_result(b'') == b''

    def test_no_commas(self):
        data = b'{"key": "value"}'
        assert self._stream_result(data) == data

    def test_escaped_quote_in_string(self):
        """Escaped quotes should not break string tracking."""
        data = b'{"key": "val\\"ue", "arr": [1,,2]}'
        assert self._stream_result(data) == self._batch_result(data)

    def test_nested_structure(self):
        data = b'{"a": {"b": [1,,2,]}, "c": [3, ,4,]}'
        assert self._stream_result(data) == self._batch_result(data)

    def test_double_comma_before_close(self):
        data = b'[1,,]'
        assert self._stream_result(data) == self._batch_result(data)

    # -- Chunk boundary tests (small chunk sizes) --

    def test_comma_at_chunk_boundary(self):
        """Comma at exact chunk boundary needs lookahead into next chunk."""
        data = b'[1, 2, 3]'
        # Put chunk boundary right after a comma
        for chunk_size in (3, 4, 5, 6):
            result = self._stream_result(data, chunk_size=chunk_size)
            assert result == data, f"chunk_size={chunk_size}"

    def test_double_comma_spans_chunks(self):
        """Double comma split across chunk boundary."""
        data = b'[1,,2]'
        for chunk_size in (2, 3, 4):
            result = self._stream_result(data, chunk_size=chunk_size)
            expected = self._batch_result(data)
            assert result == expected, f"chunk_size={chunk_size}"

    def test_trailing_comma_spans_chunks(self):
        """Trailing comma + bracket split across chunks."""
        data = b'[1, 2,]'
        for chunk_size in (2, 3, 4, 5, 6):
            result = self._stream_result(data, chunk_size=chunk_size)
            expected = self._batch_result(data)
            assert result == expected, f"chunk_size={chunk_size}"

    def test_string_spans_chunks(self):
        """String literal spanning chunk boundary with internal commas."""
        data = b'{"key": "a,,b", "other": [1,,2]}'
        for chunk_size in (3, 5, 7, 11):
            result = self._stream_result(data, chunk_size=chunk_size)
            expected = self._batch_result(data)
            assert result == expected, f"chunk_size={chunk_size}"

    def test_escaped_quote_at_chunk_boundary(self):
        """Escaped quote at chunk boundary should not break state tracking."""
        data = b'{"k": "a\\"b", "arr": [1,,2]}'
        for chunk_size in (3, 5, 8):
            result = self._stream_result(data, chunk_size=chunk_size)
            expected = self._batch_result(data)
            assert result == expected, f"chunk_size={chunk_size}"

    def test_large_whitespace_gap(self):
        """Comma followed by lots of whitespace before next token."""
        data = b'[1,    \n   \n   2]'
        assert self._stream_result(data) == data  # normal comma, no fix needed

    def test_large_whitespace_double_comma(self):
        data = b'[1,  \n  ,  \n  2]'
        assert self._stream_result(data) == self._batch_result(data)

    # -- read(n) interface --

    def test_read_n_bytes(self):
        """read(n) should return at most n bytes."""
        data = b'[1,,2,,3,]'
        expected = self._batch_result(data)
        reader = JsonSanitizingReader(io.BytesIO(data), chunk_size=4)
        result = b''
        while True:
            chunk = reader.read(3)
            if not chunk:
                break
            assert len(chunk) <= 3
            result += chunk
        assert result == expected

    def test_readline(self):
        """readline should return one line at a time."""
        data = b'[1,,2]\n[3,,4]\n'
        expected = self._batch_result(data)
        reader = JsonSanitizingReader(io.BytesIO(data), chunk_size=5)
        line1 = reader.readline()
        line2 = reader.readline()
        line3 = reader.readline()
        assert line1 + line2 == expected
        assert line3 == b''

    def test_iterator(self):
        """Iterator should yield lines."""
        data = b'{"a": 1}\n{"b": 2}\n'
        reader = JsonSanitizingReader(io.BytesIO(data), chunk_size=8)
        lines = list(reader)
        assert lines == [b'{"a": 1}\n', b'{"b": 2}\n']

    # -- Parity with batch sanitizer on complex real-world patterns --

    def test_complex_hospital_json(self):
        """Simulate a malformed hospital JSON pattern."""
        data = (
            b'{"hospital_name": "Test Hospital",\n'
            b' "standard_charge_information": [\n'
            b'   {"code": "99213",, "description": "Office visit",},\n'
            b'   {"code": "99214", "description": "Extended visit",,},\n'
            b'   ,\n'
            b'   {"code": "99215", "description": "Complex visit"}\n'
            b' ]}\n'
        )
        expected = self._batch_result(data)
        # Test with several chunk sizes
        for chunk_size in (16, 32, 64, 128):
            result = self._stream_result(data, chunk_size=chunk_size)
            assert result == expected, f"chunk_size={chunk_size}"

    def test_control_chars_in_string_and_outside(self):
        """Control chars should be stripped both inside and outside strings."""
        data = b'{"key":\x01 "val\x02ue"}'
        expected = self._batch_result(data)
        assert self._stream_result(data) == expected

    def test_properties(self):
        """Check seekable/readable."""
        reader = JsonSanitizingReader(io.BytesIO(b'{}'))
        assert reader.seekable() is False
        assert reader.readable() is True

    def test_close_cascades(self):
        """close() should cascade to the underlying file handle."""
        inner = io.BytesIO(b'{}')
        reader = JsonSanitizingReader(inner)
        reader.close()
        assert inner.closed


# ============================================================================
# open_json(sanitize=True) - end-to-end with files
# ============================================================================

class TestOpenJsonSanitizedStreaming:
    """Test the streaming sanitizer with actual files on disk."""

    def _write_temp(self, data: bytes, suffix='.json'):
        f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        f.write(data)
        f.close()
        return Path(f.name)

    def test_plain_json(self):
        """Plain JSON file should be readable."""
        import ijson
        data = b'{"items": [{"code": "99213"}, {"code": "99214"}]}'
        path = self._write_temp(data)
        try:
            with open_json(path, None, sanitize=True) as reader:
                items = list(ijson.items(reader, 'items.item'))
            assert len(items) == 2
            assert items[0]['code'] == '99213'
        finally:
            path.unlink()

    def test_gzip_json(self):
        """Gzipped JSON file should be readable."""
        import ijson
        data = b'[{"code": "99213"}, {"code": "99214"}]'
        path = self._write_temp(b'', suffix='.json.gz')
        with gzip.open(path, 'wb') as f:
            f.write(data)
        try:
            with open_json(path, 'gz', sanitize=True) as reader:
                items = list(ijson.items(reader, 'item'))
            assert len(items) == 2
        finally:
            path.unlink()

    def test_malformed_json(self):
        """Malformed JSON with double commas should be fixed."""
        import ijson
        data = b'[{"code": "99213"},,{"code": "99214"}]'
        path = self._write_temp(data)
        try:
            with open_json(path, None, sanitize=True) as reader:
                items = list(ijson.items(reader, 'item'))
            assert len(items) == 2
        finally:
            path.unlink()

    def test_control_chars_stripped(self):
        """Control characters should be stripped."""
        import ijson
        data = b'{"items": [{"code": "992\x0113"}]}'
        path = self._write_temp(data)
        try:
            with open_json(path, None, sanitize=True) as reader:
                items = list(ijson.items(reader, 'items.item'))
            assert len(items) == 1
            assert items[0]['code'] == '99213'
        finally:
            path.unlink()

    def test_zip_json(self):
        """ZIP-compressed JSON should be readable."""
        import ijson
        import zipfile as zf_mod
        data = b'[{"code": "99213"}]'
        path = self._write_temp(b'', suffix='.zip')
        with zf_mod.ZipFile(path, 'w') as zf:
            zf.writestr('data.json', data)
        try:
            with open_json(path, 'zip', sanitize=True) as reader:
                items = list(ijson.items(reader, 'item'))
            assert len(items) == 1
        finally:
            path.unlink()

    def test_utf8_bom_stripped(self):
        """UTF-8 BOM should be stripped."""
        import ijson
        data = b'\xef\xbb\xbf{"items": [{"code": "99213"}]}'
        path = self._write_temp(data)
        try:
            with open_json(path, None, sanitize=True) as reader:
                items = list(ijson.items(reader, 'items.item'))
            assert len(items) == 1
        finally:
            path.unlink()
