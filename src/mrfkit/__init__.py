"""Parse US price transparency files (MRFs) into clean, normalized rows.

    import mrfkit

    for record in mrfkit.iter_records("hospital_standardcharges.csv"):
        ...

Hospital files (CSV or JSON) and insurer Transparency in Coverage files
(in-network rates and tables of contents) are both read. See
:mod:`mrfkit.records` for what comes out.
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
    TicFileMetadata,
    TicIndexEntry,
    TicProviderGroup,
    TicRate,
    UnmappedCell,
)
from .reference import ParseStats, ReferenceData
from .sinks import CsvSink, ParquetSink, open_sink
from .tic import detect_tic_file, group_tic_networks, iter_tic_in_network, iter_tic_index, tic_network_key

__version__ = "0.1.0"

__all__ = [
    "ChargeItem", "CsvSink", "FileMetadata", "HeaderMapping", "ModifierInfo", "ParquetSink",
    "ParseStats", "PayerRate", "ReferenceData", "StandardCharge", "TicFileMetadata",
    "TicIndexEntry", "TicProviderGroup", "TicRate", "UnmappedCell", "detect_file_format",
    "group_tic_networks", "iter_csv", "iter_json", "iter_records", "iter_tic_in_network",
    "iter_tic_index", "open_sink", "tic_network_key",
]


def iter_records(path: Union[str, Path], compression: Optional[str] = None, **options: Any) -> Iterator[Any]:
    """Yield every record in an MRF, CSV or JSON, plain or compressed.

    A JSON file whose root has ``in_network`` or ``provider_references`` is
    read by :func:`iter_tic_in_network`, one with ``reporting_structure`` by
    :func:`iter_tic_index`, and every other file as a hospital MRF.

    *options* go to :func:`iter_csv` / :func:`iter_json`: ``stats``,
    ``extra_synonyms``, ``header_overrides``, ``code_extraction``, ``ref``.
    The TiC readers take only ``stats``.
    """
    fmt, detected = detect_file_format(Path(path))
    compression = compression or detected
    if fmt == "json":
        kind = detect_tic_file(path, compression)
        if kind is not None:
            reader = iter_tic_in_network if kind == "in_network" else iter_tic_index
            return reader(path, compression, stats=options.get("stats"))
    reader = iter_csv if fmt == "csv" else iter_json
    return reader(path, compression, **options)
