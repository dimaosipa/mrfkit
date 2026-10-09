"""Guard: no AMA CPT descriptor text or AMA copyright notice in the repo.

CPT code numbers in hospital files are fine, they are published under the
price transparency rule. The AMA's own descriptors and its data file are
licensed and must never ship here. Test data uses hospital-style text such
as "Office Visit Level 3" instead.

The phrases are split so this file does not match itself.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".venv", "venv", "build", "dist", "__pycache__", ".pytest_cache", ".ruff_cache"}
PHRASES = [
    "office/outpatient" + " visit",          # AMA E/M short descriptor wording
    "ofc/outpt" + " visit",
    "american medical" + " association",
    "cpt" + " copyright",
    "current procedural terminology" + " (cpt) is copyright",
]


def _text_files():
    for path in ROOT.rglob("*"):
        if path.is_file() and not SKIP_DIRS.intersection(path.relative_to(ROOT).parts):
            try:
                yield path, path.read_text(encoding="utf-8").lower()
            except (UnicodeDecodeError, OSError):
                continue  # binary fixture


def test_no_ama_descriptor_text():
    hits = [f"{path.relative_to(ROOT)}: {phrase!r}"
            for path, text in _text_files() for phrase in PHRASES if phrase in text]
    assert not hits, "AMA-licensed text found:\n" + "\n".join(hits)
