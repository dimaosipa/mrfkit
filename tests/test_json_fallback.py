"""Finding the item array when the 64 KB sniff fails, and HTML error pages.

Ported from the original parser's tests. The original scanned the stream it
was parsing and gave up when it could not rewind; mrfkit scans the file on its
own handle, so an unusual array name is always found.
"""

import json

import ijson

from mrfkit.files import looks_like_html as _looks_like_html_error_page
from mrfkit.json_reader import _find_data_array_key


def _iter_json_items_dynamic(doc, tmp_path):
    """Items of the array _find_data_array_key picks for *doc*."""
    path = tmp_path / "doc.json"
    path.write_text(json.dumps(doc))
    key = _find_data_array_key(path, None)
    if key is None:
        return []
    with open(path, "rb") as fh:
        return list(ijson.items(fh, f"{key}.item"))


class TestJsonFallbackDataKeyPreference:
    """The previous fallback picked the first non-skip root array, so
    CMS 3.0 files that list location_name before standard_charge_information
    yielded string names instead of charge items, resulting in 0 rows."""

    def test_prefers_standard_charge_information(self, tmp_path):
        doc = {
            "hospital_name": "Overlake",
            # Metadata array appears first in document order
            "location_name": ["Overlake Medical Center", "Overlake ASC"],
            # Real data comes later
            "standard_charge_information": [
                {"code": "99213", "description": "Office visit"},
                {"code": "99214", "description": "Office visit level 4"},
            ],
        }
        items = _iter_json_items_dynamic(doc, tmp_path)
        assert len(items) == 2
        assert items[0]["code"] == "99213"

    def test_prefers_items_key(self, tmp_path):
        doc = {
            "hospital_name": "X",
            "location_name": ["Campus A"],
            "items": [{"code": "A"}, {"code": "B"}],
        }
        items = _iter_json_items_dynamic(doc, tmp_path)
        assert [i["code"] for i in items] == ["A", "B"]

    def test_prefers_data_key(self, tmp_path):
        doc = {
            "hospital_name": "X",
            "type_2_npi": ["1234567890"],
            "data": [{"code": "A"}, {"code": "B"}],
        }
        items = _iter_json_items_dynamic(doc, tmp_path)
        assert [i["code"] for i in items] == ["A", "B"]

    def test_skips_location_name_metadata(self, tmp_path):
        """If only location_name (metadata) is present, the scanner
        must not emit those string values as charge items."""
        doc = {
            "hospital_name": "X",
            "location_name": ["Campus A", "Campus B"],
        }
        items = _iter_json_items_dynamic(doc, tmp_path)
        assert items == []

    def test_skips_type_2_npi_metadata(self, tmp_path):
        doc = {"hospital_name": "X", "type_2_npi": ["1234567890"]}
        items = _iter_json_items_dynamic(doc, tmp_path)
        assert items == []

    def test_falls_back_to_empty_when_only_metadata(self, tmp_path):
        """A file containing only known metadata arrays and no data array
        must yield zero items rather than erroring out."""
        doc = {
            "hospital_name": "X",
            "hospital_location": [{"address": "1 Main"}],
            "license_information": [{"number": "ABC"}],
        }
        items = _iter_json_items_dynamic(doc, tmp_path)
        assert items == []

    def test_falls_back_to_unknown_key_when_seekable(self, tmp_path):
        """If no known data key exists but the stream is seekable, the scanner
        rewinds and consumes the first non-skip root-level array. Preserves
        pre-fix behavior for files with non-CMS-standard root keys."""
        doc = {
            "hospital_name": "X",
            # Only an unknown (non-skip, non-known) key present:
            "chargemaster_rows": [{"code": "A"}, {"code": "B"}],
        }
        items = _iter_json_items_dynamic(doc, tmp_path)
        assert [i["code"] for i in items] == ["A", "B"]

    def test_unknown_key_found_without_rewinding(self, tmp_path):
        """The original yielded nothing here when its stream could not rewind."""
        doc = {"hospital_name": "X", "chargemaster_rows": [{"code": "A"}]}
        assert _iter_json_items_dynamic(doc, tmp_path) == [{"code": "A"}]


class TestHtmlPreflight:
    """Defense-in-depth guard that inspects the first 512 bytes of a file
    and returns True for obvious HTML error pages (ASP.NET viewstate stub,
    Cloudflare challenge, generic <html>/<!doctype>). Legitimate MRFs
    (CSV, JSON, gzip, zip) must return False so we never block real data.
    """

    def _write(self, tmp_path, name: str, data: bytes):
        p = tmp_path / name
        p.write_bytes(data)
        return p

    # --- Positive cases: HTML is correctly detected --------------------------

    def test_detects_doctype_html(self, tmp_path):
        p = self._write(tmp_path, "err.html",
                        b"<!DOCTYPE html><html><body>Not found</body></html>")
        assert _looks_like_html_error_page(p) is True

    def test_detects_html_tag_without_doctype(self, tmp_path):
        p = self._write(tmp_path, "err.html", b"<html><head></head></html>")
        assert _looks_like_html_error_page(p) is True

    def test_detects_cloudflare_challenge(self, tmp_path):
        body = b"<title>Attention Required! | Cloudflare</title>"
        p = self._write(tmp_path, "cf.html", body)
        assert _looks_like_html_error_page(p) is True

    def test_detects_aspnet_viewstate_stub(self, tmp_path):
        # Real ASP.NET download.aspx error page starts with markup including
        # __VIEWSTATE hidden input.
        body = (
            b"<form>"
            b"<input type=\"hidden\" name=\"__VIEWSTATE\" value=\"abc\" />"
            b"</form>"
        )
        p = self._write(tmp_path, "download.aspx.json", body)
        assert _looks_like_html_error_page(p) is True

    def test_detects_html_with_utf8_bom(self, tmp_path):
        p = self._write(tmp_path, "bom.html",
                        b"\xef\xbb\xbf<!doctype html><html></html>")
        assert _looks_like_html_error_page(p) is True

    def test_detects_html_with_leading_whitespace(self, tmp_path):
        p = self._write(tmp_path, "ws.html", b"   \n\r\n<html></html>")
        assert _looks_like_html_error_page(p) is True

    # --- Negative cases: real MRFs must NOT be flagged -----------------------

    def test_allows_json_object(self, tmp_path):
        p = self._write(tmp_path, "mrf.json",
                        b'{"hospital_name": "Test", "items": []}')
        assert _looks_like_html_error_page(p) is False

    def test_allows_json_array(self, tmp_path):
        p = self._write(tmp_path, "mrf.json", b'[{"code": "99213"}]')
        assert _looks_like_html_error_page(p) is False

    def test_allows_json_with_utf8_bom(self, tmp_path):
        p = self._write(tmp_path, "bom.json", b'\xef\xbb\xbf{"x":1}')
        assert _looks_like_html_error_page(p) is False

    def test_allows_csv(self, tmp_path):
        p = self._write(tmp_path, "mrf.csv",
                        b"code,description,gross_charge\n99213,Visit,150\n")
        assert _looks_like_html_error_page(p) is False

    def test_allows_gzip_magic(self, tmp_path):
        # gzip member header starts with 1f 8b; we must not try to pattern
        # match compressed payload against HTML markers.
        p = self._write(tmp_path, "mrf.csv.gz", b"\x1f\x8b\x08\x00" + b"\x00" * 60)
        assert _looks_like_html_error_page(p) is False

    def test_allows_zip_magic(self, tmp_path):
        p = self._write(tmp_path, "mrf.zip", b"PK\x03\x04" + b"\x00" * 60)
        assert _looks_like_html_error_page(p) is False

    def test_allows_empty_file(self, tmp_path):
        # Empty isn't HTML, a separate zero-rows guard catches this later.
        p = self._write(tmp_path, "empty.csv", b"")
        assert _looks_like_html_error_page(p) is False

    def test_missing_file_returns_false(self, tmp_path):
        # Must not raise, existence is already checked upstream.
        assert _looks_like_html_error_page(tmp_path / "does-not-exist") is False
