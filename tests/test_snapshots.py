"""End-to-end snapshots: every record mrfkit produces for the fixture MRFs.

The CSV fixtures come from the original parser's test suite, and their
snapshots match its staged rows exactly (checked with a differential run when
the readers were ported). Any change in output fails here.

Regenerate after an intended change, then review the diff:

    MRFKIT_UPDATE_SNAPSHOTS=1 python -m pytest tests/test_snapshots.py
"""

import dataclasses
import json
import os
from pathlib import Path

import pytest

from mrfkit.csv_reader import iter_csv
from mrfkit.files import detect_file_format
from mrfkit.json_reader import iter_json

FIXTURES = Path(__file__).parent / "fixtures" / "mrf"
EXPECTED = FIXTURES / "expected"
CASES = sorted(p for p in FIXTURES.iterdir() if p.is_file())


def _records(path):
    fmt, compression = detect_file_format(path)
    reader = iter_csv if fmt == "csv" else iter_json
    return [{"type": r.TABLE, **dataclasses.asdict(r)} for r in reader(path, compression)]


@pytest.mark.parametrize("path", CASES, ids=[p.name for p in CASES])
def test_snapshot(path):
    actual = _records(path)
    expected_path = EXPECTED / (path.name + ".jsonl")
    if os.environ.get("MRFKIT_UPDATE_SNAPSHOTS"):
        expected_path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in actual))
    expected = [json.loads(line) for line in expected_path.read_text().splitlines()]
    assert actual == expected
