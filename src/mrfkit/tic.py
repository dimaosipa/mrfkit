"""Read insurer Transparency in Coverage (TiC) files.

Two kinds of file are read:

- An in-network rates file: ``provider_references`` lists who (provider
  groups: a TIN and its NPIs) and ``in_network`` lists what each billing code
  pays them. :func:`iter_tic_in_network` reads it.
- A table of contents: ``reporting_structure`` lists plans and the URLs of
  their in-network files. :func:`iter_tic_index` reads it.

Allowed-amount and prescription-drug files are not supported.

Licensed text: an in-network item's ``name``, ``description`` and
``covered_services``, and a bundled code's ``description``, are payer or AMA
CPT descriptor text. No record ever carries them. An unknown key is counted in
:attr:`ParseStats.unmapped_paths` by its JSON path only, never with its
value, so a payer's own free-text field cannot leak descriptor text through
that door either.
"""

from __future__ import annotations

import itertools
import re
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional, Set, Union

import ijson

from .codes import normalize_code
from .files import detect_file_format, open_json
from .records import TicFileMetadata, TicIndexEntry, TicProviderGroup, TicRate
from .reference import ParseStats

# TiC's own "does not expire" date, also used when a price gives none.
NEVER_EXPIRES = "9999-12-31"
NPI_MIN = 1_000_000_000
NPI_MAX = 9_999_999_999
# Values at or past these are data errors, not prices.
MAX_ABS_RATE = 1e10
MAX_ABS_PERCENTAGE = 1e5
# Distinct prices held for one billing code before they are emitted. One
# item can carry tens of thousands of negotiated_rates entries, so memory is
# bounded by this rather than by the largest item in the file. Emitting
# early only means a price can appear in two records, each with its own
# provider groups.
MAX_HELD_RATES = 50_000

KNOWN_KEYS = {
    "$": {"reporting_entity_name", "reporting_entity_type", "last_updated_on",
          "version", "provider_references", "in_network"},
    "$.provider_references[]": {"provider_group_id", "provider_groups",
                                "network_name", "location"},
    "$.provider_references[].provider_groups[]": {"npi", "tin"},
    "$.provider_references[].provider_groups[].tin": {"type", "value", "business_name"},
    "$.in_network[]": {"negotiation_arrangement", "name", "billing_code_type",
                       "billing_code_type_version", "billing_code", "description",
                       "negotiated_rates", "bundled_codes",
                       # TiC 2.0, for DRG codes. Read past, not emitted.
                       "severity_of_illness"},
    "$.in_network[].negotiated_rates[]": {"provider_references", "negotiated_prices",
                                          "provider_groups"},
    "$.in_network[].negotiated_rates[].provider_groups[]": {"npi", "tin"},
    "$.in_network[].negotiated_rates[].provider_groups[].tin": {"type", "value",
                                                                 "business_name"},
    "$.in_network[].negotiated_rates[].negotiated_prices[]": {
        "negotiated_type", "negotiated_rate", "expiration_date", "service_code",
        "billing_class", "billing_code_modifier", "setting", "additional_information",
    },
    "$.in_network[].bundled_codes[]": {"billing_code_type", "billing_code_type_version",
                                       "billing_code", "description"},
}

# Descriptor text, dropped on purpose: never emitted and never reported.
IGNORED_ITEM_KEYS = frozenset({"name", "description", "covered_services"})
IGNORED_BUNDLED_KEYS = frozenset({"description"})

NEGOTIATED_TYPES = frozenset({"negotiated", "fee schedule", "percentage", "per diem", "derived"})
# Per diem and derived prices may carry no value at all.
_TYPES_REQUIRING_A_VALUE = frozenset({"negotiated", "fee schedule", "percentage"})
# "both": the rate applies to the professional and the institutional claim.
BILLING_CLASSES = frozenset({"professional", "institutional", "both"})
SETTINGS = frozenset({"both", "outpatient", "inpatient"})

_PRICES = "$.in_network[].negotiated_rates[].negotiated_prices[]"
_POS_CODE_RE = re.compile(r"^\d{2}$")
_NON_DIGIT_RE = re.compile(r"[^0-9]")


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------

def _normalize_tin(raw: Any) -> Optional[str]:
    """Digits only. Some plans write an EIN as ``30-0318970`` and others as
    ``300318970``; one form must win or one provider group splits in two."""
    digits = _NON_DIGIT_RE.sub("", str(raw)) if raw is not None else ""
    return digits or None


def _split_modifiers(raw: Any) -> list:
    """Sorted, distinct modifiers. Some plans pack several into one string ("52,53")."""
    out = set()
    for m in raw or []:
        if m is None:
            continue
        for part in str(m).split(","):
            part = part.strip()
            if part:
                out.add(part)
    return sorted(out)


def _valid_npi(raw: Any) -> Optional[int]:
    """A 10-digit NPI, or None for anything non-numeric or out of NPI range."""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return n if NPI_MIN <= n <= NPI_MAX else None


def _is_pos_code(value: Any) -> bool:
    return isinstance(value, str) and bool(_POS_CODE_RE.match(value))


def _parse_expiration(raw: Any, stats: ParseStats) -> str:
    """ISO expiration date. Missing means "does not expire" and is not
    reported; a present but unparseable date is reported, then defaulted,
    and the price is kept."""
    if not raw:
        return NEVER_EXPIRES
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
    except (ValueError, TypeError):
        stats.record_unmapped_path(f"{_PRICES}.expiration_date=unparseable")
        return NEVER_EXPIRES


def _normalize_tic_code(code: Any, code_type: Any):
    """CPT and HCPCS go through :func:`normalize_code`. Every other type (RC,
    MS-DRG, payer-custom ``CSTM-*`` ...) is kept exactly as the file declares
    it: those are not the standard-shape codes that normalizer gates."""
    if isinstance(code_type, str) and code_type.upper() in ("CPT", "HCPCS"):
        return normalize_code(code, code_type)
    return code, code_type


def _text(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def _check_unknown_keys(stats: ParseStats, path: str, obj: Any,
                        ignored: frozenset = frozenset()) -> None:
    """Count every key of *obj* not known at *path*. Path only, never the value."""
    if not isinstance(obj, dict):
        return
    known = KNOWN_KEYS.get(path, ())
    for key in obj:
        if key not in known and key not in ignored:
            stats.record_unmapped_path(f"{path}.{key}")


# ---------------------------------------------------------------------------
# Reading the file
# ---------------------------------------------------------------------------

# Some payer files carry bare backslashes inside strings ("\WAKE SPECIALTY
# PHYSICIANS LLC"), which strict JSON rejects. A valid escape is \" \\ \/ \b
# \f \n \r \t \uXXXX; anything else gets its backslash doubled so the string
# keeps the backslash as a literal character.
_BAD_ESCAPE_RE = re.compile(rb'\\\\|\\(?=[^"\\/bfnrtu])')


def _repair_escapes(data: bytes) -> tuple:
    """Return (repaired bytes, number of bad escapes doubled)."""
    repairs = 0

    def fix(m):
        nonlocal repairs
        if m.group(0) == b"\\\\":
            return m.group(0)
        repairs += 1
        return b"\\\\"
    return _BAD_ESCAPE_RE.sub(fix, data), repairs


class _EscapeRepairingStream:
    """Wrap a binary stream and repair invalid JSON escapes on the fly.

    A trailing backslash that could start an escape or a pair is carried into
    the next read, and a read never returns empty bytes before the underlying
    stream is exhausted (empty means EOF to the parser). ``repairs`` counts
    the substitutions.
    """

    def __init__(self, fh):
        self._fh = fh
        self._carry = b""
        self.repairs = 0

    def read(self, size=-1):
        while True:
            chunk = self._fh.read(size)
            data = self._carry + chunk
            self._carry = b""
            if not chunk:
                out, n = _repair_escapes(data)
                self.repairs += n
                return out
            trailing = len(data) - len(data.rstrip(b"\\"))
            if trailing % 2 == 1:
                self._carry = b"\\"
                data = data[:-1]
            out, n = _repair_escapes(data)
            self.repairs += n
            if out:
                return out


@contextmanager
def _open(path: Path, compression: Optional[str]) -> Iterator[_EscapeRepairingStream]:
    with open_json(path, compression) as fh:
        yield _EscapeRepairingStream(fh)


def _bounded_array_events(fh, array_key: str):
    """Parse events up to and including *array_key*'s closing bracket, then
    stop, so streaming that one top-level array never reads the rest of a
    multi-gigabyte file. ``ijson.items`` accepts these events in place of a
    file."""
    for prefix, event, value in ijson.parse(fh):
        yield prefix, event, value
        if prefix == array_key and event == "end_array":
            return


def detect_tic_file(path: Union[str, Path], compression: Optional[str] = None) -> Optional[str]:
    """``'in_network'`` or ``'index'`` for a TiC file, None for anything else.

    Looks at the root keys in the first events of the file only, so a
    hospital file costs a few kilobytes of parsing.
    """
    path = Path(path)
    if compression is None:
        _, compression = detect_file_format(path)
    try:
        with _open(path, compression) as fh:
            for prefix, event, value in itertools.islice(ijson.parse(fh), 1000):
                if event != "map_key":
                    continue
                if prefix == "" and value in ("in_network", "provider_references"):
                    return "in_network"
                # A table of contents is sometimes wrapped in a one-element list.
                if prefix in ("", "item") and value == "reporting_structure":
                    return "index"
    except ijson.JSONError:
        pass
    return None


# ---------------------------------------------------------------------------
# In-network rates files
# ---------------------------------------------------------------------------

def iter_tic_in_network(
    path: Union[str, Path],
    compression: Optional[str] = None,
    *,
    stats: Optional[ParseStats] = None,
) -> Iterator[Any]:
    """Yield the records in a TiC in-network rates file.

    Order: :class:`TicFileMetadata`, every :class:`TicProviderGroup` from
    ``provider_references``, then :class:`TicRate` records (with a
    :class:`TicProviderGroup` for a group listed inline on a rate just
    before the first rate that uses it).

    Two passes over separate handles, so *path* must be a file: the first
    reads ``provider_references`` and stops at its closing bracket, the
    second streams ``in_network``. Memory holds the reference ids, not the
    file.

    *stats*: ``rows_read`` counts negotiated prices read, ``rows_skipped``
    the provider groups, items and prices dropped as invalid or unmappable,
    and ``unmapped_paths`` names each problem by JSON path.
    """
    path = Path(path)
    stats = stats if stats is not None else ParseStats()
    if compression is None:
        _, compression = detect_file_format(path)

    metadata = _read_metadata(path, compression, stats)
    if any((metadata.reporting_entity_name, metadata.reporting_entity_type,
            metadata.last_updated_on, metadata.version, metadata.network_name)):
        yield metadata
    resolved: Set[Any] = set()
    yield from _provider_reference_groups(path, compression, stats, resolved)
    yield from _rates(path, compression, stats, resolved)


def _read_metadata(path: Path, compression: Optional[str], stats: ParseStats) -> TicFileMetadata:
    """Root fields and the first reference's network name, then stop.

    Payers write the root scalars before the two big arrays and repeat one
    network label on every reference, so this reads at most one reference.
    """
    fields: Dict[str, str] = {}
    root_keys: Dict[str, None] = {}
    with _open(path, compression) as fh:
        for prefix, event, value in ijson.parse(fh):
            if prefix == "" and event == "map_key":
                root_keys[value] = None
                if value == "in_network":
                    break
            elif prefix in ("reporting_entity_name", "reporting_entity_type",
                            "last_updated_on", "version") and event in ("string", "number"):
                fields[prefix] = str(value)
            elif prefix == "provider_references.item.network_name.item" and event == "string":
                fields["network_name"] = value
                break
            elif ((prefix == "provider_references.item" and event == "end_map")
                  or (prefix == "provider_references" and event == "end_array")):
                break
    _check_unknown_keys(stats, "$", root_keys)
    return TicFileMetadata(**fields)


def _provider_group(pg: Any, path: str, stats: ParseStats) -> Optional[tuple]:
    """(tin_type, tin, npis, business_name) for a valid provider group, else None.

    A group needs a TIN and at least one valid NPI. Invalid NPIs are dropped
    one by one and reported; the group keeps its valid ones.
    """
    _check_unknown_keys(stats, path, pg)
    tin_obj = pg.get("tin") or {}
    _check_unknown_keys(stats, f"{path}.tin", tin_obj)
    tin_type = tin_obj.get("type")
    if tin_type != "ein":
        stats.record_unmapped_path(f"{path}.tin.type={tin_type}")

    tin = _normalize_tin(tin_obj.get("value"))
    if not tin:
        stats.record_unmapped_path(f"{path}.tin.value=missing")
        stats.rows_skipped += 1
        return None

    npis = set()
    any_invalid = False
    for raw in pg.get("npi") or []:
        npi = _valid_npi(raw)
        if npi is None:
            any_invalid = True
            kind = "out_of_range" if str(raw).lstrip("-").isdigit() else "non_numeric"
            stats.record_unmapped_path(f"{path}.npi={kind}")
            stats.rows_skipped += 1
            continue
        npis.add(npi)
    if not npis:
        if not any_invalid:
            stats.record_unmapped_path(f"{path}.npi=empty")
        return None
    return (_text(tin_type), tin, "|".join(str(n) for n in sorted(npis)),
            _text(tin_obj.get("business_name")))


def _provider_reference_groups(path: Path, compression: Optional[str], stats: ParseStats,
                               resolved: Set[Any]) -> Iterator[TicProviderGroup]:
    """Pass 1: one record per (reference id, provider group).

    Adds every reference id with at least one valid group to *resolved*; a
    rate pointing only at other ids has nobody to pay and is skipped.
    """
    # ponytail: one key per distinct group row stays in memory to drop a
    # reference repeated in the file; spill to disk if a file ever has
    # tens of millions of groups.
    seen = set()
    path_groups = "$.provider_references[].provider_groups[]"
    with _open(path, compression) as fh:
        events = _bounded_array_events(fh, "provider_references")
        for ref in ijson.items(events, "provider_references.item"):
            _check_unknown_keys(stats, "$.provider_references[]", ref)
            ref_id = ref.get("provider_group_id")
            if ref.get("location") and not ref.get("provider_groups"):
                # The groups live in a separate file this reader does not fetch.
                stats.record_unmapped_path("$.provider_references[].location=unsupported")
            for pg in ref.get("provider_groups") or []:
                group = _provider_group(pg, path_groups, stats)
                if group is None:
                    continue
                resolved.add(ref_id)
                key = (ref_id, group[1], group[2])
                if key not in seen:
                    seen.add(key)
                    yield TicProviderGroup(str(ref_id), *group)


def _id_order(group_id: Any) -> tuple:
    # Reference ids are numbers; inline ids are strings and sort after them.
    return (isinstance(group_id, str), group_id)


def _rates(path: Path, compression: Optional[str], stats: ParseStats,
           resolved: Set[Any]) -> Iterator[Any]:
    """Pass 2: stream ``in_network`` into rate records."""
    inline_ids: Dict[tuple, str] = {}
    path_inline = "$.in_network[].negotiated_rates[].provider_groups[]"
    with _open(path, compression) as fh:
        for item in ijson.items(fh, "in_network.item"):
            _check_unknown_keys(stats, "$.in_network[]", item, IGNORED_ITEM_KEYS)
            for bc in item.get("bundled_codes") or []:
                _check_unknown_keys(stats, "$.in_network[].bundled_codes[]", bc,
                                    IGNORED_BUNDLED_KEYS)

            raw_code = item.get("billing_code")
            raw_code_type = item.get("billing_code_type")
            if not raw_code or not raw_code_type:
                stats.record_unmapped_path(
                    "$.in_network[].billing_code=missing" if not raw_code
                    else "$.in_network[].billing_code_type=missing")
                stats.rows_skipped += max(1, sum(
                    len(nr.get("negotiated_prices") or [])
                    for nr in item.get("negotiated_rates") or []))
                continue
            code, code_type = _normalize_tic_code(raw_code, raw_code_type)

            # Distinct price -> every provider group that has it, for this
            # item. Some payers list one provider per negotiated_rates entry,
            # so identical prices repeat many times per code; one record per
            # distinct price says the same thing in far fewer rows.
            held: Dict[tuple, set] = {}
            for nr in item.get("negotiated_rates") or []:
                _check_unknown_keys(stats, "$.in_network[].negotiated_rates[]", nr)
                group_ids = set()
                unknown_ref = False
                for ref_id in nr.get("provider_references") or []:
                    if ref_id in resolved:
                        group_ids.add(ref_id)
                    else:
                        unknown_ref = True
                if unknown_ref:
                    stats.record_unmapped_path(
                        "$.in_network[].negotiated_rates[].provider_references=unknown")
                for pg in nr.get("provider_groups") or []:
                    group = _provider_group(pg, path_inline, stats)
                    if group is None:
                        continue
                    group_id = inline_ids.get(group[1:3])
                    if group_id is None:
                        group_id = inline_ids[group[1:3]] = f"inline-{len(inline_ids) + 1}"
                        yield TicProviderGroup(group_id, *group)
                    group_ids.add(group_id)
                if not group_ids:
                    continue

                for price in nr.get("negotiated_prices") or []:
                    key = _price_key(price, stats)
                    if key is None:
                        continue
                    merged = held.get(key)
                    if merged is None:
                        held[key] = set(group_ids)
                    else:
                        merged |= group_ids
                    if len(held) >= MAX_HELD_RATES:
                        yield from _rate_records(item, code, code_type, held)
            yield from _rate_records(item, code, code_type, held)
        if fh.repairs:
            stats.warn(f"Repaired {fh.repairs:,} invalid JSON escape(s)")


def _price_key(price: Any, stats: ParseStats) -> Optional[tuple]:
    """The merge key for one negotiated price, or None when it is skipped."""
    _check_unknown_keys(stats, _PRICES, price)
    stats.rows_read += 1

    negotiated_type = price.get("negotiated_type")
    if negotiated_type not in NEGOTIATED_TYPES:
        stats.record_unmapped_path(f"{_PRICES}.negotiated_type={negotiated_type}")
        stats.rows_skipped += 1
        return None
    billing_class = price.get("billing_class")
    if billing_class not in BILLING_CLASSES:
        stats.record_unmapped_path(f"{_PRICES}.billing_class={billing_class}")
        stats.rows_skipped += 1
        return None
    setting = price.get("setting")
    if setting is not None and setting not in SETTINGS:
        stats.record_unmapped_path(f"{_PRICES}.setting={setting}")

    value = price.get("negotiated_rate")
    if value is None and negotiated_type in _TYPES_REQUIRING_A_VALUE:
        stats.record_unmapped_path(f"{_PRICES}.negotiated_rate=missing")
        stats.rows_skipped += 1
        return None
    if value is not None:
        limit = MAX_ABS_PERCENTAGE if negotiated_type == "percentage" else MAX_ABS_RATE
        try:
            out_of_range = abs(float(value)) >= limit
        except (TypeError, ValueError):
            out_of_range = True
        if out_of_range:
            stats.record_unmapped_path(f"{_PRICES}.negotiated_rate=out_of_range")
            stats.rows_skipped += 1
            return None

    service_codes = sorted(price.get("service_code") or [])
    for sc in service_codes:
        if not _is_pos_code(sc):
            stats.record_unmapped_path(f"{_PRICES}.service_code={sc}")
    return (billing_class, setting if setting in SETTINGS else "", negotiated_type, value,
            tuple(service_codes), tuple(_split_modifiers(price.get("billing_code_modifier"))),
            _parse_expiration(price.get("expiration_date"), stats))


def _rate_records(item: dict, code: Any, code_type: Any, held: Dict[tuple, set]) -> Iterator[TicRate]:
    """One record per distinct price held for *item*, then clear *held*."""
    for (billing_class, setting, negotiated_type, value, service_codes, modifiers,
         expiration), group_ids in held.items():
        value = None if value is None else float(value)
        is_percentage = negotiated_type == "percentage"
        yield TicRate(
            billing_code=_text(code),
            billing_code_type=_text(code_type),
            billing_code_type_version=_text(item.get("billing_code_type_version")),
            negotiation_arrangement=_text(item.get("negotiation_arrangement")),
            negotiated_type=negotiated_type,
            negotiated_rate=None if is_percentage else value,
            negotiated_percentage=value if is_percentage else None,
            expiration_date=expiration,
            billing_class=billing_class,
            setting=setting,
            service_codes="|".join(str(s) for s in service_codes) or None,
            modifiers="|".join(modifiers) or None,
            provider_group_ids="|".join(str(g) for g in sorted(group_ids, key=_id_order)),
        )
    held.clear()


# ---------------------------------------------------------------------------
# Table of contents (index) files
# ---------------------------------------------------------------------------

def iter_tic_index(
    path: Union[str, Path],
    compression: Optional[str] = None,
    *,
    stats: Optional[ParseStats] = None,
) -> Iterator[TicIndexEntry]:
    """Yield one :class:`TicIndexEntry` per (reporting plan, in-network file)
    pair of every ``reporting_structure`` in a table-of-contents file.

    Streamed: one structure is in memory at a time, so a 10 GB index is
    fine. A root wrapped in a list is accepted. An in-network file with no
    ``location`` is skipped and counted in *stats*.

    Raises ``ValueError`` when the root is neither an object nor a list.
    """
    path = Path(path)
    stats = stats if stats is not None else ParseStats()
    if compression is None:
        _, compression = detect_file_format(path)

    root: Dict[str, str] = {}
    with _open(path, compression) as fh:
        events = ijson.parse(fh)
        first = next(events, None)
        if first is None or first[1] not in ("start_map", "start_array"):
            raise ValueError(f"{path.name}: a table of contents must be a JSON object "
                             f"(or a list holding one)")
        base = "" if first[1] == "start_map" else "item."

        def capture_root():
            # Root fields stream past before reporting_structure; keep them.
            yield first
            for prefix, event, value in events:
                if (prefix in (f"{base}reporting_entity_name", f"{base}reporting_entity_type")
                        and event == "string"):
                    root[prefix[len(base):]] = value
                yield prefix, event, value

        for structure in ijson.items(capture_root(), f"{base}reporting_structure.item"):
            plans = structure.get("reporting_plans") or [{}]
            allowed = (structure.get("allowed_amount_file") or {}).get("location")
            for entry in structure.get("in_network_files") or []:
                location = entry.get("location")
                if not location:
                    stats.record_unmapped_path(
                        "$.reporting_structure[].in_network_files[].location=missing")
                    stats.rows_skipped += 1
                    continue
                stats.rows_read += 1
                for plan in plans:
                    yield TicIndexEntry(
                        reporting_entity_name=root.get("reporting_entity_name"),
                        reporting_entity_type=root.get("reporting_entity_type"),
                        plan_name=_text(plan.get("plan_name")),
                        plan_id=_text(plan.get("plan_id")),
                        plan_id_type=_text(plan.get("plan_id_type")),
                        plan_market_type=_text(plan.get("plan_market_type")),
                        in_network_location=str(location),
                        in_network_description=_text(entry.get("description")),
                        allowed_amount_location=_text(allowed),
                    )


# "2026-09-01_KPIC-MA_in-network-rates" -> "KPIC-MA_in-network-rates". File
# names usually lead with their publication date, which would make every
# monthly index a new set of networks.
_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}(?:-\d{2})?_")


def _file_name(location: str) -> str:
    return location.split("?", 1)[0].rsplit("/", 1)[-1]


def tic_network_key(location: str) -> str:
    """A stable key for the network an in-network file belongs to.

    The file's base name without query string, extension or a leading
    publication date, so signed URLs and next month's copy keep the key.
    """
    return _DATE_PREFIX_RE.sub("", _file_name(location).split(".", 1)[0])


def group_tic_networks(entries: Iterable[TicIndexEntry]) -> Dict[str, Dict[str, Any]]:
    """Group index entries into networks, keyed by :func:`tic_network_key`.

    Capitation (``cap-rates``) and allowed-amount files are left out. Each
    group gives ``file_name`` and ``location`` (the lexicographically first
    file name), ``market_type`` (the most common ``plan_market_type`` over
    the plans that reference it) and ``file_count`` (distinct file names).
    Memory is per distinct file, not per entry: an index can repeat one file
    across millions of plans.
    """
    groups: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        file_name = _file_name(entry.in_network_location)
        lower = file_name.lower()
        if "cap-rates" in lower or "allowed" in lower:
            continue
        key = tic_network_key(file_name)
        group = groups.get(key)
        if group is None:
            group = groups[key] = {"file_name": file_name, "location": entry.in_network_location,
                                   "names": set(), "market_types": Counter()}
        elif file_name < group["file_name"]:
            group["file_name"], group["location"] = file_name, entry.in_network_location
        group["names"].add(file_name)
        if entry.plan_market_type:
            group["market_types"][entry.plan_market_type] += 1

    for group in groups.values():
        modal = group.pop("market_types").most_common(1)
        group["market_type"] = modal[0][0] if modal else None
        group["file_count"] = len(group.pop("names"))
    return groups
