"""Read hospital MRF CSV files into records."""

from __future__ import annotations

import csv
import io
import itertools
import logging
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, TextIO, Union

from .codes import _MAX_CODE_TYPE_LEN, _RE_VALID_CODE_TYPE_SHAPE
from .files import detect_file_format, open_text
from .headers import map_header
from .records import FileMetadata
from .reference import ParseStats, ReferenceData
from .tabular import PEEK_ROWS, SectionBoundary, TableLayout
from .values import _STATE_NAME_TO_ABBR, _US_STATES, _sanitize_npi

log = logging.getLogger(__name__)

# Real MRF cells (long notes, stuffed code lists) exceed the csv default of
# 128 KB. Raised once at import, so a caller can still lower it.
FIELD_SIZE_LIMIT = 10 * 1024 * 1024
csv.field_size_limit(max(csv.field_size_limit(), FIELD_SIZE_LIMIT))

PREAMBLE_LINES = 10
QUOTING_SAMPLE_ROWS = 200
_WARN_SKIPPED_ROWS = 5

# "Key: Value" preamble lines, as some non-CMS files write their metadata.
_KV_PREAMBLE_KEYS = {
    'hospital name': 'hospital_name',
    'hospital location': 'hospital_location',
    'hospital address': 'hospital_address',
    'price effective date': 'last_updated_on',
    'last updated on': 'last_updated_on',
    'cms certification number': 'CMS Certification Number',
    'version': 'version',
    'npi': 'type_2_npi',
    'type 2 npi': 'type_2_npi',
    'ein': 'ein',
    'attestation': 'attestation',
    'attester name': 'attester_name',
}


def iter_csv(
    source: Union[str, Path, TextIO],
    compression: Optional[str] = None,
    *,
    extra_synonyms: Optional[Mapping[str, str]] = None,
    header_overrides: Optional[Mapping[str, str]] = None,
    code_extraction: Optional[Mapping[str, Any]] = None,
    ref: Optional[ReferenceData] = None,
    stats: Optional[ParseStats] = None,
) -> Iterator[Any]:
    """Yield the records in a hospital MRF CSV file.

    Order: one :class:`FileMetadata` (when the file has a metadata preamble),
    one :class:`HeaderMapping` per column, then charge items, standard
    charges, payer rates and unmapped cells as rows are read.

    *source* is a path (compression detected unless given) or an open text
    stream. Pass a :class:`ParseStats` to collect counts and warnings.
    """
    options = dict(extra_synonyms=extra_synonyms, header_overrides=header_overrides,
                   code_extraction=code_extraction, ref=ref,
                   stats=stats if stats is not None else ParseStats())
    if hasattr(source, 'read'):
        yield from _read(source, **options)
        return
    path = Path(source)
    if compression is None:
        _, compression = detect_file_format(path)
    with open_text(path, compression) as fh:
        yield from _read(fh, **options)


def _read(fh: TextIO, *, stats: ParseStats, **layout_options) -> Iterator[Any]:
    preamble = list(itertools.islice(fh, PREAMBLE_LINES))
    if not preamble:
        raise ValueError("CSV file is empty")

    header_idx = _find_header_row(preamble)
    delimiter = _sniff_delimiter(preamble[header_idx])
    if delimiter != ',':
        log.info("Detected %s-delimited format", {'|': 'pipe', '\t': 'tab'}[delimiter])

    metadata = _read_metadata(preamble, header_idx, delimiter)
    if metadata is not None:
        yield metadata

    sample = list(itertools.islice(fh, QUOTING_SAMPLE_ROWS))
    quoting = _detect_csv_quoting(preamble[header_idx:], sample, delimiter)

    lines = itertools.chain(preamble[header_idx:], sample, fh)
    reader = csv.DictReader(lines, delimiter=delimiter, quoting=quoting)
    if not reader.fieldnames:
        raise ValueError("CSV has no headers")

    peeked = []
    for _ in range(PEEK_ROWS):
        try:
            peeked.append(next(reader))
        except StopIteration:
            break
        except csv.Error as exc:
            _skip_row(stats, len(peeked) + 1, exc)
            break

    layout = TableLayout(reader.fieldnames, peeked, stats=stats, **layout_options)
    yield from layout.header_mappings()

    rows = itertools.chain(peeked, reader)
    row_number = 0
    while True:
        try:
            row = next(rows)
        except StopIteration:
            break
        except csv.Error as exc:
            row_number += 1
            _skip_row(stats, row_number, exc)
            continue
        row_number += 1
        stats.rows_read += 1
        try:
            yield from layout.row_to_records(row, first_row=row_number == 1, check_sections=True)
        except SectionBoundary:
            # A stacked multi-table file: later sections repeat the data under
            # narrower per-payer headers and cannot be read against this one.
            dropped = sum(1 for _ in _drain(rows))
            stats.rows_skipped += 1
            stats.warn(f"Multi-section CSV: stopped at row ~{row_number} on a later section "
                       f"header; dropped ~{dropped} trailing rows")
            break

    if stats.rows_skipped:
        stats.warnings.insert(0, f"Skipped {stats.rows_skipped} CSV rows due to parse errors "
                                 f"(e.g. oversized fields)")


def _drain(rows):
    while True:
        try:
            yield next(rows)
        except StopIteration:
            return
        except csv.Error:
            continue


def _skip_row(stats: ParseStats, row_number: int, exc: Exception) -> None:
    stats.rows_skipped += 1
    if stats.rows_skipped <= _WARN_SKIPPED_ROWS:
        stats.warn(f"Skipped CSV row ~{row_number}: {exc}")


# ---------------------------------------------------------------------------
# Header row, delimiter, quoting
# ---------------------------------------------------------------------------

def _find_header_row(preamble: List[str]) -> int:
    """Return the index of the data header among the first lines.

    Candidates mention ``description`` or ``code|``. Some files glue the data
    header onto the end of the metadata value row, so among candidates the
    one whose fields map best wins, then the shorter line (the clean header
    is a suffix of the glued one), then the earliest.
    """
    def score(line: str):
        try:
            fields = next(csv.reader(io.StringIO(line)))
        except (csv.Error, StopIteration):
            return (-1, 0)
        mapped = sum(1 for f in fields if f.strip() and map_header(f)[1])
        return (mapped, -len(fields))

    candidates = [i for i, line in enumerate(preamble)
                  if 'description' in line.lower() or 'code|' in line.lower()]
    if not candidates:
        log.warning("Could not detect the data header row, using the first row")
        return 0
    return max(candidates, key=lambda i: score(preamble[i]))


def _sniff_delimiter(header_line: str) -> str:
    """Comma unless the header splits into fewer than 3 fields on commas.

    CMS wide headers (``standard_charge|AETNA|PPO|negotiated_dollar``) are
    full of pipes, so counting pipes against commas would pick the pipe.
    """
    if len(next(csv.reader(io.StringIO(header_line)))) >= 3:
        return ','
    pipes, tabs = header_line.count('|'), header_line.count('\t')
    if pipes > tabs and pipes > 0:
        return '|'
    if tabs > 0:
        return '\t'
    return ','


def _score_csv_quoting_mode(header_lines, sample_lines, delimiter, quoting) -> Dict[str, int]:
    """Parse a sample under *quoting* and count the symptoms of a bad choice.

    Lower is better: rows whose width does not match the header, fields that
    swallowed a newline (quote fusion), and code_type values that are not a
    code-type shape (the classic sign of a column shift).
    """
    buf = io.StringIO(''.join(header_lines) + ''.join(sample_lines))
    try:
        reader = csv.DictReader(buf, delimiter=delimiter, quoting=quoting)
        fieldnames = reader.fieldnames or []
    except csv.Error:
        return {'length_mismatches': 10**9, 'malformed_code_types': 10**9,
                'embedded_newlines': 0, 'rows_seen': 0, 'expected_cols': 0}
    ct_col = next((h for h in fieldnames if map_header(h)[1] == 'code_type'), None)
    scores = {'length_mismatches': 0, 'malformed_code_types': 0, 'embedded_newlines': 0,
              'rows_seen': 0, 'expected_cols': len(fieldnames)}
    try:
        for row in reader:
            scores['rows_seen'] += 1
            # DictReader puts overflow under a None key and fills underflow with None.
            if None in row or any(row.get(h) is None for h in fieldnames):
                scores['length_mismatches'] += 1
            # MRF fields never legitimately span lines.
            if any(isinstance(v, str) and '\n' in v for v in row.values()):
                scores['embedded_newlines'] += 1
            if ct_col:
                ct = (row.get(ct_col) or '').strip()
                if ct and (len(ct) > _MAX_CODE_TYPE_LEN
                           or not _RE_VALID_CODE_TYPE_SHAPE.fullmatch(ct.upper())):
                    scores['malformed_code_types'] += 1
    except csv.Error:
        scores['length_mismatches'] += 10**6
    return scores


def _detect_csv_quoting(header_lines, sample_lines, delimiter) -> int:
    """Pick QUOTE_MINIMAL or QUOTE_NONE for this file.

    A stray ``"`` at the start of a field (``|"needle 6 inch``) makes
    QUOTE_MINIMAL swallow delimiters and newlines until the next quote,
    fusing rows. Parse a sample both ways and keep the cleaner one; ties go
    to QUOTE_MINIMAL.
    """
    if not sample_lines:
        return csv.QUOTE_MINIMAL

    def total(s):
        return s['length_mismatches'] * 10 + s['embedded_newlines'] * 10 + s['malformed_code_types']

    minimal = total(_score_csv_quoting_mode(header_lines, sample_lines, delimiter, csv.QUOTE_MINIMAL))
    none = total(_score_csv_quoting_mode(header_lines, sample_lines, delimiter, csv.QUOTE_NONE))
    if none < minimal:
        log.info("CSV quoting: QUOTE_NONE (minimal=%d, none=%d)", minimal, none)
        return csv.QUOTE_NONE
    return csv.QUOTE_MINIMAL


# ---------------------------------------------------------------------------
# Metadata preamble
# ---------------------------------------------------------------------------

def _read_metadata(preamble: List[str], header_idx: int, delimiter: str) -> Optional[FileMetadata]:
    meta = _try_parse_kv_preamble(preamble, header_idx)
    if meta is None and header_idx >= 2:
        rows = list(csv.reader(io.StringIO(''.join(preamble[:header_idx])), delimiter=delimiter))
        if len(rows) >= 2:
            keys, values = rows[0], rows[1]
            meta = {keys[i].strip().lstrip('﻿'): values[i].strip()
                    for i in range(min(len(keys), len(values))) if keys[i].strip()}
    if meta is None:
        return None

    def get(key):
        value = (meta.get(key) or '').strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1].strip()
        return value or None

    license_number, license_state = None, None
    for key, value in meta.items():
        if key.lower().strip().startswith('license_number'):
            # The header carries the state: license_number|CA
            if '|' in key:
                state = key.split('|', 1)[1].strip().upper()
                if len(state) > 2:
                    state = _STATE_NAME_TO_ABBR.get(state, state[:2])
                license_state = state if state in _US_STATES else None
            license_number = value.split('|')[0].strip().strip('"').strip() or None
            break

    return FileMetadata(
        hospital_name=get('hospital_name'),
        hospital_location=get('hospital_location'),
        hospital_address=get('hospital_address'),
        cms_certification_number=get('CMS Certification Number'),
        license_number=license_number,
        license_state=license_state,
        type_2_npi=_sanitize_npi((meta.get('type_2_npi') or '').strip()),
        ein=get('ein'),
        last_updated_on=get('last_updated_on'),
        version=get('version'),
        attestation=get('attestation'),
        attester_name=get('attester_name'),
    )


def _try_parse_kv_preamble(preamble_lines: List[str], header_row_idx: int) -> Optional[Dict[str, str]]:
    """Parse "Hospital Name: Acme,,,," style lines, or return None."""
    if header_row_idx < 1:
        return None
    meta = {}
    for line in preamble_lines[:header_row_idx]:
        stripped = line.strip().rstrip(',').strip()
        if ':' not in stripped:
            continue
        key, _, value = stripped.partition(':')
        canonical = _KV_PREAMBLE_KEYS.get(key.strip().lower().lstrip('﻿'))
        if canonical:
            meta[canonical] = value.strip().rstrip(',').strip()
    return meta or None
