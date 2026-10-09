"""JSON root metadata, array prefix and modifier information, including BOM handling.

Ported from the original parser's tests.
"""

import gzip
import io
import json
import os
import tempfile
import zipfile
from pathlib import Path


from mrfkit.files import Utf8SanitizingReader
from mrfkit.json_reader import (
    _detect_json_array_prefix,
    _extract_json_modifier_information,
    _extract_json_root_metadata,
    _file_metadata,
)

_UTF8_BOM = b"\xef\xbb\xbf"


def _write_json(data: dict, compression=None, bom=False) -> Path:
    """Write JSON dict to temp file, optionally with BOM and/or compression."""
    raw = json.dumps(data).encode('utf-8')
    if bom:
        raw = _UTF8_BOM + raw
    if compression == 'gz':
        fd, path = tempfile.mkstemp(suffix='.json.gz')
        os.close(fd)
        with gzip.open(path, 'wb') as f:
            f.write(raw)
    elif compression == 'zip':
        fd, path = tempfile.mkstemp(suffix='.zip')
        os.close(fd)
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('data.json', raw)
    else:
        fd, path = tempfile.mkstemp(suffix='.json')
        os.close(fd)
        with open(path, 'wb') as f:
            f.write(raw)
    return Path(path)


def _write_raw(content: bytes, suffix='.json', compression=None) -> Path:
    """Write raw bytes to temp file."""
    if compression == 'gz':
        fd, path = tempfile.mkstemp(suffix=suffix + '.gz')
        os.close(fd)
        with gzip.open(path, 'wb') as f:
            f.write(content)
    elif compression == 'zip':
        fd, path = tempfile.mkstemp(suffix='.zip')
        os.close(fd)
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('data' + suffix, content)
    else:
        fd, path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        with open(path, 'wb') as f:
            f.write(content)
    return Path(path)


# ============================================================================
# Fix 1: _retry_on_lock_error
# ============================================================================


class TestUtf8BomSanitizingReader:
    """Utf8SanitizingReader._detect_and_strip_bom() should strip UTF-8 BOM."""

    def test_utf8_bom_stripped_from_stream(self):
        """UTF-8 BOM at start of stream should be silently stripped."""
        data = _UTF8_BOM + b'{"key": "value"}'
        reader = Utf8SanitizingReader(io.BytesIO(data))
        result = reader.read()
        assert result == b'{"key": "value"}'

    def test_utf8_bom_stripped_chunked_reads(self):
        """UTF-8 BOM should be stripped even with small chunk reads."""
        data = _UTF8_BOM + b'{"hospital": "Test"}'
        reader = Utf8SanitizingReader(io.BytesIO(data), chunk_size=8)
        chunks = []
        while True:
            chunk = reader.read(10)
            if not chunk:
                break
            chunks.append(chunk)
        result = b''.join(chunks)
        assert result == b'{"hospital": "Test"}'

    def test_utf8_bom_json_parseable(self):
        """After BOM stripping, the output should be valid JSON."""
        import ijson
        data = _UTF8_BOM + b'{"hospital_name": "Saint Lukes"}'
        reader = Utf8SanitizingReader(io.BytesIO(data))
        events = list(ijson.parse(reader))
        keys = [v for _, e, v in events if e == 'map_key']
        assert 'hospital_name' in keys

    def test_no_bom_unchanged(self):
        """Data without BOM should pass through unchanged."""
        data = b'{"key": "value"}'
        reader = Utf8SanitizingReader(io.BytesIO(data))
        result = reader.read()
        assert result == data


class TestDetectJsonArrayPrefixBom:
    """_detect_json_array_prefix should handle UTF-8 BOM."""

    def test_bom_object_root(self):
        """BOM + object-rooted JSON should detect standard_charge_information."""
        data = _UTF8_BOM + json.dumps({
            "hospital_name": "Test",
            "standard_charge_information": [{"code": "99213"}],
        }).encode('utf-8')
        path = _write_raw(data)
        try:
            result = _detect_json_array_prefix(path, None)
            assert result == 'standard_charge_information.item'
        finally:
            os.unlink(path)

    def test_bom_array_root(self):
        """BOM + array-rooted JSON should detect 'item' prefix."""
        data = _UTF8_BOM + b'[{"description":"Test","code_information":[]}]'
        path = _write_raw(data)
        try:
            result = _detect_json_array_prefix(path, None)
            assert result == 'item'
        finally:
            os.unlink(path)

    def test_bom_gzip(self):
        """BOM inside gzip file should be handled."""
        data = _UTF8_BOM + json.dumps({
            "hospital_name": "Test",
            "standard_charge_information": [],
        }).encode('utf-8')
        path = _write_raw(data, compression='gz')
        try:
            result = _detect_json_array_prefix(path, 'gz')
            assert result == 'standard_charge_information.item'
        finally:
            os.unlink(path)

    def test_bom_zip(self):
        """BOM inside zip file should be handled."""
        data = _UTF8_BOM + json.dumps({
            "hospital_name": "Test",
            "standard_charge_information": [],
        }).encode('utf-8')
        path = _write_raw(data, compression='zip')
        try:
            result = _detect_json_array_prefix(path, 'zip')
            assert result == 'standard_charge_information.item'
        finally:
            os.unlink(path)


class TestExtractJsonRootMetadataBom:
    """_extract_json_root_metadata should handle UTF-8 BOM."""

    SAMPLE = {
        "hospital_name": "Saint Lukes Hospital",
        "last_updated_on": "2025-07-01",
        "version": "2.0.0",
        "standard_charge_information": [
            {"description": "X-ray", "code": "70553"}
        ],
    }

    def test_bom_plain_json(self):
        """Metadata should be extracted from BOM-prefixed plain JSON."""
        path = _write_json(self.SAMPLE, bom=True)
        try:
            meta, warning = _extract_json_root_metadata(path, None)
            assert meta.get('hospital_name') == "Saint Lukes Hospital"
            assert meta.get('last_updated_on') == "2025-07-01"
            assert warning is None
        finally:
            os.unlink(path)

    def test_bom_gzip_json(self):
        """Metadata should be extracted from BOM-prefixed gzipped JSON."""
        path = _write_json(self.SAMPLE, compression='gz', bom=True)
        try:
            meta, warning = _extract_json_root_metadata(path, 'gz')
            assert meta.get('hospital_name') == "Saint Lukes Hospital"
            assert warning is None
        finally:
            os.unlink(path)

    def test_bom_zip_json(self):
        """Metadata should be extracted from BOM-prefixed JSON inside zip."""
        path = _write_json(self.SAMPLE, compression='zip', bom=True)
        try:
            meta, warning = _extract_json_root_metadata(path, 'zip')
            assert meta.get('hospital_name') == "Saint Lukes Hospital"
            assert warning is None
        finally:
            os.unlink(path)

    def test_no_bom_still_works(self):
        """No BOM → should still parse correctly (regression check)."""
        path = _write_json(self.SAMPLE, bom=False)
        try:
            meta, warning = _extract_json_root_metadata(path, None)
            assert meta.get('hospital_name') == "Saint Lukes Hospital"
            assert warning is None
        finally:
            os.unlink(path)


class TestExtractJsonModifierInfoBom:
    """_extract_json_modifier_information should handle UTF-8 BOM."""

    SAMPLE = {
        "hospital_name": "Test Hospital",
        "modifier_information": [
            {
                "code": "50",
                "description": "Bilateral procedure",
                "modifier_payer_information": [
                    {"payer_name": "Test Payer", "plan_name": "PPO",
                     "description": "150% adjustment"},
                ],
            },
        ],
        "standard_charge_information": [],
    }

    def test_bom_plain_json(self):
        """Modifiers should be extracted from BOM-prefixed plain JSON."""
        path = _write_json(self.SAMPLE, bom=True)
        try:
            result = _extract_json_modifier_information(path, None)
            assert len(result) == 1
            assert result[0]['code'] == '50'
        finally:
            os.unlink(path)

    def test_bom_gzip_json(self):
        """Modifiers should be extracted from BOM-prefixed gzipped JSON."""
        path = _write_json(self.SAMPLE, compression='gz', bom=True)
        try:
            result = _extract_json_modifier_information(path, 'gz')
            assert len(result) == 1
            assert result[0]['code'] == '50'
        finally:
            os.unlink(path)

    def test_bom_zip_json(self):
        """Modifiers should be extracted from BOM-prefixed JSON inside zip."""
        path = _write_json(self.SAMPLE, compression='zip', bom=True)
        try:
            result = _extract_json_modifier_information(path, 'zip')
            assert len(result) == 1
            assert result[0]['code'] == '50'
        finally:
            os.unlink(path)


class TestCms30LicenseStateExtraction:
    """_extract_json_root_metadata should extract license_information.state."""

    CMS30_SAMPLE = {
        "hospital_name": "Providence Health & Services - Montana",
        "version": "3.0.0",
        "location_name": [
            "St Patrick Hospital (Broadway Campus)",
            "St Patrick Hospital (Orange Campus)",
        ],
        "hospital_address": [
            "500 W Broadway, Missoula, MT 59806",
            "902 N Orange St, Missoula, MT 59802",
        ],
        "license_information": {
            "license_number": "13200|13200",
            "state": "MT",
        },
        "attestation": {
            "attestation": "We attest that this data is accurate.",
            "confirm_attestation": True,
            "attester_name": "John Smith",
        },
        "standard_charge_information": [],
    }

    def test_license_state_extracted(self):
        """license_information.state should be extracted as license_state."""
        path = _write_json(self.CMS30_SAMPLE)
        try:
            meta, warning = _extract_json_root_metadata(path, None)
            assert meta.get('license_state') == 'MT'
            assert warning is None
        finally:
            os.unlink(path)

    def test_license_number_extracted(self):
        """license_information.license_number should be extracted."""
        path = _write_json(self.CMS30_SAMPLE)
        try:
            meta, warning = _extract_json_root_metadata(path, None)
            assert meta.get('license_number') == '13200|13200'
        finally:
            os.unlink(path)

    def test_license_state_none_when_missing(self):
        """license_state should be None when license_information has no state."""
        data = {
            "hospital_name": "Test Hospital",
            "license_information": {"license_number": "12345"},
            "standard_charge_information": [],
        }
        path = _write_json(data)
        try:
            meta, warning = _extract_json_root_metadata(path, None)
            assert meta.get('license_state') is None
            assert meta.get('license_number') == '12345'
        finally:
            os.unlink(path)

    def test_no_license_information_at_all(self):
        """Missing license_information → no license_state or license_number."""
        data = {
            "hospital_name": "Test Hospital",
            "standard_charge_information": [],
        }
        path = _write_json(data)
        try:
            meta, warning = _extract_json_root_metadata(path, None)
            assert 'license_state' not in meta
            assert 'license_number' not in meta
        finally:
            os.unlink(path)


class TestCms30LocationNameExtraction:
    """_extract_json_root_metadata should map location_name → hospital_location."""

    def test_location_name_mapped_to_hospital_location(self):
        """CMS 3.0 location_name should appear as hospital_location in metadata."""
        data = {
            "hospital_name": "Test Hospital",
            "location_name": ["Campus A", "Campus B"],
            "standard_charge_information": [],
        }
        path = _write_json(data)
        try:
            meta, warning = _extract_json_root_metadata(path, None)
            assert meta.get('hospital_location') == ["Campus A", "Campus B"]
            # location_name should be normalized away
            assert 'location_name' not in meta
        finally:
            os.unlink(path)

    def test_hospital_location_still_works(self):
        """CMS 2.0 hospital_location should still work as before."""
        data = {
            "hospital_name": "Test Hospital",
            "hospital_location": ["Main Campus"],
            "standard_charge_information": [],
        }
        path = _write_json(data)
        try:
            meta, warning = _extract_json_root_metadata(path, None)
            assert meta.get('hospital_location') == ["Main Campus"]
        finally:
            os.unlink(path)

    def test_location_name_preferred_over_hospital_location(self):
        """If both location_name and hospital_location exist, location_name wins."""
        data = {
            "hospital_name": "Test Hospital",
            "hospital_location": ["Old Name"],
            "location_name": ["New Name"],
            "standard_charge_information": [],
        }
        path = _write_json(data)
        try:
            meta, warning = _extract_json_root_metadata(path, None)
            assert meta.get('hospital_location') == ["New Name"]
        finally:
            os.unlink(path)

    def test_neither_location_key_present(self):
        """Neither location_name nor hospital_location → no hospital_location."""
        data = {
            "hospital_name": "Test Hospital",
            "standard_charge_information": [],
        }
        path = _write_json(data)
        try:
            meta, warning = _extract_json_root_metadata(path, None)
            assert 'hospital_location' not in meta
        finally:
            os.unlink(path)


class TestCms30AttestationExtraction:
    """_extract_json_root_metadata should handle nested CMS 3.0 attestation."""

    def test_nested_attestation_extracted(self):
        """CMS 3.0 nested attestation object should be flattened into metadata."""
        data = {
            "hospital_name": "Test Hospital",
            "attestation": {
                "attestation": "We attest this is accurate.",
                "confirm_attestation": True,
                "attester_name": "Jane Doe",
            },
            "standard_charge_information": [],
        }
        path = _write_json(data)
        try:
            meta, warning = _extract_json_root_metadata(path, None)
            assert meta.get('attestation') == "We attest this is accurate."
            assert meta.get('confirm_attestation') is True
            assert meta.get('attester_name') == "Jane Doe"
        finally:
            os.unlink(path)

    def test_scalar_attestation_still_works(self):
        """CMS 2.0 scalar attestation should still be captured."""
        data = {
            "hospital_name": "Test Hospital",
            "attestation": "Yes",
            "attester_name": "John Smith",
            "confirm_attestation": True,
            "standard_charge_information": [],
        }
        path = _write_json(data)
        try:
            meta, warning = _extract_json_root_metadata(path, None)
            assert meta.get('attestation') == "Yes"
            assert meta.get('attester_name') == "John Smith"
            assert meta.get('confirm_attestation') is True
        finally:
            os.unlink(path)


class TestCms30FullSample:
    """End-to-end test with a full CMS 3.0 JSON structure."""

    CMS30_FULL = {
        "hospital_name": "Providence Health & Services - Montana",
        "version": "3.0.0",
        "last_updated_on": "01/15/2026",
        "location_name": [
            "St Patrick Hospital (Broadway Campus)",
            "St Patrick Hospital (Orange Campus)",
        ],
        "hospital_address": [
            "500 W Broadway, Missoula, MT 59806",
            "902 N Orange St, Missoula, MT 59802",
        ],
        "license_information": {
            "license_number": "13200|13200",
            "state": "MT",
        },
        "attestation": {
            "attestation": "We attest that the information is accurate.",
            "confirm_attestation": True,
            "attester_name": "CFO Name",
        },
        "standard_charge_information": [],
    }

    def test_all_metadata_extracted(self):
        """All CMS 3.0 metadata should be extracted correctly."""
        path = _write_json(self.CMS30_FULL)
        try:
            meta, warning = _extract_json_root_metadata(path, None)
            assert warning is None
            # Basic metadata
            assert meta['hospital_name'] == "Providence Health & Services - Montana"
            assert meta['version'] == "3.0.0"
            assert meta['last_updated_on'] == "01/15/2026"
            # Locations (mapped from location_name)
            assert meta['hospital_location'] == [
                "St Patrick Hospital (Broadway Campus)",
                "St Patrick Hospital (Orange Campus)",
            ]
            assert 'location_name' not in meta
            # Addresses
            assert meta['hospital_address'] == [
                "500 W Broadway, Missoula, MT 59806",
                "902 N Orange St, Missoula, MT 59802",
            ]
            # License
            assert meta['license_number'] == "13200|13200"
            assert meta['license_state'] == "MT"
            # Attestation (flattened from nested object)
            assert meta['attestation'] == "We attest that the information is accurate."
            assert meta['confirm_attestation'] is True
            assert meta['attester_name'] == "CFO Name"
        finally:
            os.unlink(path)

    def test_all_metadata_extracted_gzip(self):
        """Same test but with gzip compression."""
        path = _write_json(self.CMS30_FULL, compression='gz')
        try:
            meta, warning = _extract_json_root_metadata(path, 'gz')
            assert warning is None
            assert meta['hospital_name'] == "Providence Health & Services - Montana"
            assert meta['license_state'] == "MT"
            assert meta['hospital_location'] == [
                "St Patrick Hospital (Broadway Campus)",
                "St Patrick Hospital (Orange Campus)",
            ]
            assert meta['attestation'] == "We attest that the information is accurate."
        finally:
            os.unlink(path)


class TestLicenseStateValidation:
    """License state extraction should validate against known US states."""

    def _state(self, **meta):
        return _file_metadata({"hospital_name": "X", **meta}).license_state

    def test_json_valid_state_accepted(self):
        """CMS 3.0 license_information.state 'MT' is used directly."""
        assert self._state(license_state="MT", license_number="13200|13200") == "MT"

    def test_json_full_state_name_normalized(self):
        """A full state name becomes its abbreviation."""
        assert self._state(license_state="Montana", license_number="13200") == "MT"

    def test_json_garbage_state_rejected(self):
        """A state that is not a US state is dropped."""
        assert self._state(license_state="XX", license_number="13200") is None

    def test_state_from_pipe_in_license_number(self):
        """Older files write the state after a pipe in the license number."""
        meta = _file_metadata({"hospital_name": "X", "license_number": "12345|CA"})
        assert (meta.license_number, meta.license_state) == ("12345", "CA")
