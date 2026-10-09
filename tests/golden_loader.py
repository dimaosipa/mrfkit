"""Load golden-file cases.

Golden files live in tests/golden/*.json. Each holds a JSON array of cases:

    {
        "id": "descriptive_snake_case_id",
        "description": "Human-readable explanation",
        "input": { ... },
        "expected": { ... }
    }

The tests are thin parametrized wrappers, so the same files can pin the
behaviour of any other implementation.
"""

import json
from pathlib import Path
from typing import Any, Dict, List

GOLDEN_DIR = Path(__file__).parent / "golden"


def load_golden(filename: str) -> List[Dict[str, Any]]:
    """Load a golden file and return its test cases."""
    with open(GOLDEN_DIR / filename, "r", encoding="utf-8") as f:
        cases = json.load(f)
    assert isinstance(cases, list), f"{filename} must contain a JSON array"
    assert len(cases) > 0, f"{filename} must not be empty"
    return cases


def golden_ids(cases: List[Dict[str, Any]]) -> List[str]:
    """Extract test IDs from golden cases for pytest parametrize."""
    return [c["id"] for c in cases]
