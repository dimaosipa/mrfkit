"""Tests for open_text, open_json and Deflate64 zip members."""

import gzip
import struct
import sys
import zipfile
from pathlib import Path

import ijson
import pytest

from mrfkit.files import detect_file_format, open_json, open_text, validate_zip_integrity

FIXTURES = Path(__file__).parent / "fixtures"
sys.path.insert(0, str(FIXTURES))
from make_deflate64 import charges_csv  # noqa: E402

DEFLATE64_ZIP = FIXTURES / "deflate64.zip"


def _corrupt(tmp_path: Path, offset_in_data: int) -> Path:
    """Copy the Deflate64 fixture with one compressed byte flipped."""
    data = bytearray(DEFLATE64_ZIP.read_bytes())
    name_len, extra_len = struct.unpack("<HH", data[26:30])
    data[30 + name_len + extra_len + offset_in_data] ^= 0xFF
    path = tmp_path / "bad.zip"
    path.write_bytes(bytes(data))
    return path


class TestDeflate64:
    def test_fixture_really_is_deflate64(self):
        with zipfile.ZipFile(DEFLATE64_ZIP) as zf:
            assert zf.infolist()[0].compress_type == 9
            with pytest.raises(NotImplementedError):
                zf.read("charges.txt")  # the stdlib alone cannot read it

    def test_detect_sniffs_inner_txt(self):
        assert detect_file_format(DEFLATE64_ZIP) == ("csv", "zip")

    def test_open_text_round_trip(self):
        with open_text(DEFLATE64_ZIP, "zip") as fh:
            assert fh.read().encode() == charges_csv()

    def test_open_text_line_iteration(self):
        with open_text(DEFLATE64_ZIP, "zip") as fh:
            lines = list(fh)
        assert len(lines) == 20001
        assert lines[0].startswith("description,code")

    def test_corrupt_block_header_is_bad_zip(self, tmp_path):
        # inflate64 raises ValueError here; detect_file_format would read that
        # as "empty file" and trust the extension, so it must be BadZipFile.
        with pytest.raises(zipfile.BadZipFile):
            validate_zip_integrity(_corrupt(tmp_path, 0))

    def test_crc_mismatch_detected(self, tmp_path):
        # Intact data, wrong CRC in the central directory (where zipfile reads it).
        data = bytearray(DEFLATE64_ZIP.read_bytes())
        crc_at = data.index(b"PK\x01\x02") + 16
        data[crc_at] ^= 0xFF
        path = tmp_path / "bad.zip"
        path.write_bytes(bytes(data))
        with pytest.raises(zipfile.BadZipFile, match="CRC"):
            validate_zip_integrity(path)

    def test_corrupt_zip_named_csv_is_not_trusted(self, tmp_path):
        path = _corrupt(tmp_path, 0).rename(tmp_path / "charges.csv")
        with pytest.raises(zipfile.BadZipFile):
            detect_file_format(path)


class TestOpenText:
    def test_utf16_le_with_bom(self, tmp_path):
        path = tmp_path / "f.csv"
        path.write_bytes(b"\xff\xfe" + "a,b\n1,é\n".encode("utf-16-le"))
        with open_text(path, None) as fh:
            # The BOM survives as U+FEFF, as it does for UTF-8; header
            # normalization strips it.
            assert fh.read() == "﻿a,b\n1,é\n"

    def test_utf16_be_without_bom(self, tmp_path):
        path = tmp_path / "f.csv"
        path.write_bytes("code,description\n99213,Visit\n".encode("utf-16-be"))
        with open_text(path, None) as fh:
            assert fh.readline() == "code,description\n"

    def test_invalid_utf8_replaced(self, tmp_path):
        path = tmp_path / "f.csv"
        path.write_bytes(b"a,b\n1,caf\xe9 \x96 bar\n")  # Windows-1252 bytes
        with open_text(path, None) as fh:
            assert fh.read() == "a,b\n1,caf� � bar\n"

    def test_gzip(self, tmp_path):
        path = tmp_path / "f.csv.gz"
        path.write_bytes(gzip.compress(b"a,b\n1,2\n"))
        with open_text(path, "gz") as fh:
            assert fh.read() == "a,b\n1,2\n"

    def test_gzip_utf16(self, tmp_path):
        path = tmp_path / "f.csv.gz"
        path.write_bytes(gzip.compress(b"\xff\xfe" + "a,b\n".encode("utf-16-le")))
        with open_text(path, "gz") as fh:
            assert fh.read() == "﻿a,b\n"

    def test_zip_stored_and_deflated(self, tmp_path):
        for method in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            path = tmp_path / f"f{method}.zip"
            with zipfile.ZipFile(path, "w", compression=method) as zf:
                zf.writestr("data.csv", "a,b\n1,2\n")
            with open_text(path, "zip") as fh:
                assert fh.read() == "a,b\n1,2\n"

    def test_underlying_file_closed(self, tmp_path):
        path = tmp_path / "f.csv"
        path.write_bytes(b"a\n")
        with open_text(path, None) as fh:
            buffer = fh.buffer
        assert buffer.closed


class TestOpenJson:
    def test_raw_mode_parses_multi_chunk_file(self, tmp_path):
        item = '{"code": "99213", "description": "' + "x" * 200 + '"}'
        path = tmp_path / "f.json"
        path.write_text('{"items": [' + ",".join([item] * 5000) + "]}")  # ~1.1 MB
        with open_json(path, None) as fh:
            assert sum(1 for _ in ijson.items(fh, "items.item")) == 5000

    def test_sanitize_fixes_comma_damage_across_chunk_boundaries(self, tmp_path):
        # Over 1 MB so the double comma sits past several 256 KB chunks.
        body = ",".join(['{"d": "' + "y" * 500 + '"}'] * 2500)
        cut = body.index("},", 1_100_000) + 1
        body = body[:cut] + "," + body[cut:] + ","  # double comma + trailing comma
        path = tmp_path / "f.json"
        path.write_text("[" + body + "]")
        with open_json(path, None) as fh, pytest.raises(ijson.JSONError):
            list(ijson.items(fh, "item"))
        with open_json(path, None, sanitize=True) as fh:
            assert sum(1 for _ in ijson.items(fh, "item")) == 2500

    def test_deflate64_json(self, tmp_path):
        # Same Deflate64 path for JSON: the fixture is CSV, so only check it streams.
        with open_json(DEFLATE64_ZIP, "zip") as fh:
            assert fh.read(11) == b"description"
