"""Write records to disk: one file per record type, in CSV or Parquet."""

from __future__ import annotations

import csv
import dataclasses
import operator
import typing
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple, Type


def _columns(record_type: type) -> Tuple[List[str], Callable[[Any], tuple]]:
    names = [f.name for f in dataclasses.fields(record_type)]
    getter = operator.attrgetter(*names)
    if len(names) == 1:
        return names, lambda r: (getter(r),)
    return names, getter


class CsvSink:
    """Write each record type to ``<out_dir>/<TABLE>.csv``. None becomes an empty cell."""

    def __init__(self, out_dir: Path):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._open: Dict[type, Tuple[Any, Any, Callable]] = {}

    def write(self, record: Any) -> None:
        entry = self._open.get(type(record))
        if entry is None:
            names, getter = _columns(type(record))
            fh = open(self.out_dir / f"{record.TABLE}.csv", "w", newline="", encoding="utf-8")
            writer = csv.writer(fh)
            writer.writerow(names)
            entry = self._open[type(record)] = (fh, writer, getter)
        entry[1].writerow(entry[2](record))

    def close(self) -> None:
        for fh, _, _ in self._open.values():
            fh.close()
        self._open.clear()

    def __enter__(self) -> "CsvSink":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class ParquetSink:
    """Write each record type to ``<out_dir>/<TABLE>.parquet``, in row groups of *batch_size*."""

    def __init__(self, out_dir: Path, batch_size: int = 50_000):
        try:
            import pyarrow
            import pyarrow.parquet
        except ImportError as exc:
            raise ImportError('Parquet output needs pyarrow: pip install "mrfkit[parquet]"') from exc
        self._pa = pyarrow
        self._pq = pyarrow.parquet
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.batch_size = batch_size
        self._rows: Dict[type, list] = {}
        self._writers: Dict[type, Any] = {}
        self._layout: Dict[type, Tuple[List[str], Callable, Any]] = {}

    def write(self, record: Any) -> None:
        record_type = type(record)
        if record_type not in self._layout:
            names, getter = _columns(record_type)
            self._layout[record_type] = (names, getter, self._schema(record_type, names))
            self._rows[record_type] = []
        rows = self._rows[record_type]
        rows.append(self._layout[record_type][1](record))
        if len(rows) >= self.batch_size:
            self._flush(record_type)

    def _schema(self, record_type: type, names: List[str]):
        pa = self._pa
        hints = typing.get_type_hints(record_type)
        by_type = {str: pa.string(), float: pa.float64(), int: pa.int64(), bool: pa.bool_()}
        fields = []
        for name in names:
            args = [a for a in typing.get_args(hints[name]) if a is not type(None)] or [hints[name]]
            fields.append(pa.field(name, by_type[args[0]]))
        return pa.schema(fields)

    def _flush(self, record_type: Type) -> None:
        rows = self._rows[record_type]
        if not rows:
            return
        names, _, schema = self._layout[record_type]
        table = self._pa.Table.from_arrays(
            [self._pa.array(col, type=schema.field(i).type) for i, col in enumerate(zip(*rows, strict=True))],
            schema=schema,
        )
        writer = self._writers.get(record_type)
        if writer is None:
            path = self.out_dir / f"{record_type.TABLE}.parquet"
            writer = self._writers[record_type] = self._pq.ParquetWriter(path, schema)
        writer.write_table(table)
        rows.clear()

    def close(self) -> None:
        for record_type in list(self._rows):
            self._flush(record_type)
        for writer in self._writers.values():
            writer.close()
        self._writers.clear()

    def __enter__(self) -> "ParquetSink":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def open_sink(out_dir: Path, fmt: str = "csv"):
    """Return a CSV or Parquet sink for *out_dir*."""
    if fmt == "csv":
        return CsvSink(out_dir)
    if fmt == "parquet":
        return ParquetSink(out_dir)
    raise ValueError(f"Unknown output format {fmt!r}: use 'csv' or 'parquet'")
