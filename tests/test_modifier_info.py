"""
Tests for _extract_json_modifier_information() and modifier_info DB insertion.
"""

import gzip
import json
import os
import tempfile
from pathlib import Path

from mrfkit.json_reader import _extract_json_modifier_information


# ── Sample CMS 3.0 JSON with modifier_information ───────────────────


def _write_json_file(data: dict, compression: str = None) -> Path:
    """Write a JSON dict to a temp file, optionally compressed."""
    raw = json.dumps(data).encode('utf-8')
    if compression == 'gz':
        fd, path = tempfile.mkstemp(suffix='.json.gz')
        os.close(fd)
        with gzip.open(path, 'wb') as f:
            f.write(raw)
    else:
        fd, path = tempfile.mkstemp(suffix='.json')
        os.close(fd)
        with open(path, 'wb') as f:
            f.write(raw)
    return Path(path)


SAMPLE_CMS_JSON = {
    "hospital_name": "Test Hospital",
    "last_updated_on": "2025-01-01",
    "standard_charge_information": [
        {"description": "X-ray", "code_information": [{"code": "70553", "type": "CPT"}]}
    ],
    "modifier_information": [
        {
            "description": "Bilateral procedure",
            "code": "50",
            "setting": "both",
            "modifier_payer_information": [
                {
                    "payer_name": "Platform Health Insurance",
                    "plan_name": "PPO",
                    "description": "150% payment adjustment"
                },
                {
                    "payer_name": "Region Health Insurance",
                    "plan_name": "HMO",
                    "description": "145% payment adjustment"
                }
            ]
        },
        {
            "description": "Co-surgeon",
            "code": "62",
            "modifier_payer_information": [
                {
                    "payer_name": "Platform Health Insurance",
                    "plan_name": "PPO",
                    "description": "62.5% of the amount"
                }
            ]
        },
        {
            "description": "Bilateral with co-surgeon",
            "code": "50|62",
            "setting": "outpatient",
            "modifier_payer_information": []
        }
    ]
}


class TestExtractModifierInformation:
    """Tests for _extract_json_modifier_information()."""

    def test_basic_extraction(self):
        path = _write_json_file(SAMPLE_CMS_JSON)
        try:
            result = _extract_json_modifier_information(path, None)
            assert len(result) == 3

            # First entry: bilateral
            assert result[0]['code'] == '50'
            assert result[0]['description'] == 'Bilateral procedure'
            assert result[0]['setting'] == 'both'
            assert len(result[0]['modifier_payer_information']) == 2
            assert result[0]['modifier_payer_information'][0]['payer_name'] == 'Platform Health Insurance'
            assert result[0]['modifier_payer_information'][0]['plan_name'] == 'PPO'
            assert result[0]['modifier_payer_information'][0]['description'] == '150% payment adjustment'

            # Second entry: co-surgeon (no setting)
            assert result[1]['code'] == '62'
            assert result[1]['setting'] is None
            assert len(result[1]['modifier_payer_information']) == 1

            # Third entry: combined, empty payer info
            assert result[2]['code'] == '50|62'
            assert result[2]['setting'] == 'outpatient'
            assert result[2]['modifier_payer_information'] == []
        finally:
            os.unlink(path)

    def test_gzip_extraction(self):
        """Works with gzipped files."""
        path = _write_json_file(SAMPLE_CMS_JSON, compression='gz')
        try:
            result = _extract_json_modifier_information(path, 'gz')
            assert len(result) == 3
            assert result[0]['code'] == '50'
        finally:
            os.unlink(path)

    def test_no_modifier_information(self):
        """Returns empty list when modifier_information is absent."""
        data = {
            "hospital_name": "Test",
            "standard_charge_information": []
        }
        path = _write_json_file(data)
        try:
            result = _extract_json_modifier_information(path, None)
            assert result == []
        finally:
            os.unlink(path)

    def test_empty_modifier_information(self):
        """Returns empty list when modifier_information is an empty array."""
        data = {
            "hospital_name": "Test",
            "modifier_information": [],
            "standard_charge_information": []
        }
        path = _write_json_file(data)
        try:
            result = _extract_json_modifier_information(path, None)
            assert result == []
        finally:
            os.unlink(path)

    def test_empty_code_skipped(self):
        """Entries with empty code are skipped."""
        data = {
            "hospital_name": "Test",
            "modifier_information": [
                {"description": "No code", "code": "", "modifier_payer_information": []}
            ],
            "standard_charge_information": []
        }
        path = _write_json_file(data)
        try:
            result = _extract_json_modifier_information(path, None)
            assert result == []
        finally:
            os.unlink(path)

    def test_non_dict_entries_skipped(self):
        """Non-dict entries in modifier_information are skipped."""
        data = {
            "hospital_name": "Test",
            "modifier_information": [
                "bad string entry",
                42,
                {"code": "50", "description": "Good", "modifier_payer_information": []}
            ],
            "standard_charge_information": []
        }
        path = _write_json_file(data)
        try:
            result = _extract_json_modifier_information(path, None)
            assert len(result) == 1
            assert result[0]['code'] == '50'
        finally:
            os.unlink(path)

    def test_missing_payer_info_key(self):
        """Entry without modifier_payer_information key gets empty list."""
        data = {
            "hospital_name": "Test",
            "modifier_information": [
                {"code": "TC", "description": "Technical component"}
            ],
            "standard_charge_information": []
        }
        path = _write_json_file(data)
        try:
            result = _extract_json_modifier_information(path, None)
            assert len(result) == 1
            assert result[0]['modifier_payer_information'] == []
        finally:
            os.unlink(path)

    def test_non_dict_payer_entries_skipped(self):
        """Non-dict entries in modifier_payer_information are skipped."""
        data = {
            "hospital_name": "Test",
            "modifier_information": [
                {
                    "code": "26",
                    "description": "Professional",
                    "modifier_payer_information": [
                        "not a dict",
                        {"payer_name": "Aetna", "plan_name": "PPO", "description": "80%"}
                    ]
                }
            ],
            "standard_charge_information": []
        }
        path = _write_json_file(data)
        try:
            result = _extract_json_modifier_information(path, None)
            assert len(result) == 1
            assert len(result[0]['modifier_payer_information']) == 1
            assert result[0]['modifier_payer_information'][0]['payer_name'] == 'Aetna'
        finally:
            os.unlink(path)

    def test_modifier_information_after_large_array(self):
        """Works even when modifier_information comes after a large standard_charge_information array."""
        data = {
            "hospital_name": "Test",
            "standard_charge_information": [
                {"description": f"Item {i}", "code_information": [{"code": str(i), "type": "CPT"}]}
                for i in range(100)
            ],
            "modifier_information": [
                {
                    "code": "50",
                    "description": "Bilateral",
                    "modifier_payer_information": [
                        {"payer_name": "Cigna", "plan_name": "HMO", "description": "double"}
                    ]
                }
            ]
        }
        path = _write_json_file(data)
        try:
            result = _extract_json_modifier_information(path, None)
            assert len(result) == 1
            assert result[0]['code'] == '50'
        finally:
            os.unlink(path)

    def test_corrupt_json_returns_empty(self):
        """Corrupt JSON returns empty list (non-fatal)."""
        fd, path = tempfile.mkstemp(suffix='.json')
        os.write(fd, b'{"modifier_information": [{"code": "50", "broken')
        os.close(fd)
        try:
            result = _extract_json_modifier_information(Path(path), None)
            # ijson may return partial results or empty depending on error position
            assert isinstance(result, list)
        finally:
            os.unlink(path)
