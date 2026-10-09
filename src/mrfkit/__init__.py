"""Parse US hospital price transparency files (MRFs) into clean, normalized rows.

    import mrfkit

    for record in mrfkit.iter_records("hospital_standardcharges.csv"):
        ...

See :mod:`mrfkit.records` for what comes out.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, Optional, Union

from .csv_reader import iter_csv
from .files import detect_file_format
from .json_reader import iter_json
from .records import (
    ChargeItem,
    FileMetadata,
    HeaderMapping,
    ModifierInfo,
    PayerRate,
    StandardCharge,
    UnmappedCell,
)
from .reference import ParseStats, ReferenceData
from .sinks import CsvSink, ParquetSink, open_sink

__version__ = "0.1.0"

__all__ = [
    "ChargeItem", "CsvSink", "FileMetadata", "HeaderMapping", "ModifierInfo", "ParquetSink",
    "ParseStats", "PayerRate", "ReferenceData", "StandardCharge", "UnmappedCell",
    "detect_file_format", "iter_csv", "iter_json", "iter_records", "open_sink",
]


def iter_records(path: Union[str, Path], compression: Optional[str] = None, **options: Any) -> Iterator[Any]:
    """Yield every record in a hospital MRF, CSV or JSON, plain or compressed.

    *options* go to :func:`iter_csv` / :func:`iter_json`: ``stats``,
    ``extra_synonyms``, ``header_overrides``, ``code_extraction``, ``ref``.
    """
    fmt, detected = detect_file_format(Path(path))
    reader = iter_csv if fmt == "csv" else iter_json
    return reader(path, compression or detected, **options)
