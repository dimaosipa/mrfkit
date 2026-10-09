"""``mrfkit FILE...``: turn hospital and insurer MRFs into CSV or Parquet tables."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import List, Optional

from . import __version__, iter_records
from .reference import ParseStats
from .sinks import open_sink


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mrfkit",
        description="Turn US price transparency files (hospital CSV or JSON, insurer "
                    "Transparency in Coverage JSON; plain, gzip or zip) into clean tables: "
                    "one file per record type.")
    p.add_argument("files", nargs="+", type=Path, help="MRF files to read")
    p.add_argument("-o", "--out", type=Path, default=Path("mrfkit-out"),
                   help="output directory (default: mrfkit-out). With several input "
                        "files, each gets its own subdirectory")
    p.add_argument("-f", "--format", choices=["csv", "parquet"], default="csv",
                   help="output format (parquet needs: pip install \"mrfkit[parquet]\")")
    p.add_argument("--synonyms", type=Path,
                   help="JSON file mapping extra header names to fields, "
                        "e.g. {\"charge_amt\": \"gross_charge\"}")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="log layout detection and list unmapped JSON paths")
    p.add_argument("--version", action="version", version=f"mrfkit {__version__}")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.format == "parquet":
        try:
            import pyarrow  # noqa: F401
        except ImportError:
            parser.error('Parquet output needs pyarrow: pip install "mrfkit[parquet]"')
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")
    synonyms = json.loads(args.synonyms.read_text()) if args.synonyms else None

    failed = 0
    for path in args.files:
        out_dir = args.out if len(args.files) == 1 else args.out / path.name.split(".")[0]
        stats = ParseStats()
        counts: Counter = Counter()
        try:
            with open_sink(out_dir, args.format) as sink:
                for record in iter_records(path, stats=stats, extra_synonyms=synonyms):
                    sink.write(record)
                    counts[record.TABLE] += 1
        except (OSError, ValueError) as exc:
            failed += 1
            print(f"{path}: error: {exc}", file=sys.stderr)
            continue
        summary = ", ".join(f"{n:,} {table}" for table, n in sorted(counts.items()))
        print(f"{path} -> {out_dir}: {stats.rows_read:,} rows read; {summary or 'no records'}",
              file=sys.stderr)
        if stats.rejected_codes:
            print(f"  {stats.rejected_codes:,} rows dropped for corrupt codes", file=sys.stderr)
        for warning in stats.warnings:
            print(f"  warning: {warning}", file=sys.stderr)
        if stats.unmapped_paths:
            n_paths = len(stats.unmapped_paths)
            print(f"  {n_paths:,} unmapped JSON path{'' if n_paths == 1 else 's'}"
                  + ("" if args.verbose else " (-v lists them)"), file=sys.stderr)
            if args.verbose:
                for json_path, n in sorted(stats.unmapped_paths.items()):
                    print(f"    {json_path}: {n:,}", file=sys.stderr)
    return 1 if failed else 0
