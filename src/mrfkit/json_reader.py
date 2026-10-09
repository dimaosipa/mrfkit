"""Read hospital MRF JSON files into records.

Two shapes exist. The CMS template nests everything: an item has
``code_information``, ``standard_charges`` and, inside those,
``payers_information``. Older and vendor files are flat: each item is a row
of key/value pairs, read exactly like a CSV row by
:class:`mrfkit.tabular.TableLayout`.
"""

from __future__ import annotations

import itertools
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple, Union

import ijson

from .codes import merge_modifier_into_field, normalize_code
from .files import detect_file_format, open_json
from .payers import is_self_pay_payer, normalize_payer_name
from .records import FileMetadata, HeaderMapping, ModifierInfo, StandardCharge
from .reference import ParseStats, ReferenceData
from .tabular import PEEK_ROWS, RecordBuilder, TableLayout
from .values import _STATE_NAME_TO_ABBR, _US_STATES, _safe_count, _safe_float, _sanitize_npi

log = logging.getLogger(__name__)

# Root keys that hold the item array, most likely first.
DATA_KEYS = ('standard_charge_information', 'items', 'data')

# Root arrays that are metadata, never charge data.
METADATA_ARRAY_KEYS = frozenset({
    'hospital_location', 'hospital_address', 'financial_aid_policy',
    'license_information', 'affirmation', 'modifier_information',
    'general_contract_provisions', 'location_name', 'type_2_npi',
})

_PREFIX_SNIFF_BYTES = 65536

# How the CMS nested fields are read, for HeaderMapping records.
_CMS_HEADER_MAPPINGS = [
    ('description', 'description', 'description'),
    ('code_information.code', 'code', 'code'),
    ('code_information.type', 'code_type', 'code_type'),
    ('standard_charges.gross_charge', 'gross_charge', 'gross_charge'),
    ('standard_charges.discounted_cash', 'discounted_cash_price', 'discounted_cash_price'),
    ('standard_charges.minimum', 'min_negotiated_rate', 'min_negotiated_rate'),
    ('standard_charges.maximum', 'max_negotiated_rate', 'max_negotiated_rate'),
    ('standard_charges.billing_class', 'billing_class', 'billing_class'),
    ('standard_charges.setting', 'setting', 'setting'),
    ('standard_charges.modifiers', 'modifiers', 'modifiers'),
    ('drug_information.unit', 'drug_unit_of_measurement', 'drug_unit_of_measurement'),
    ('drug_information.type', 'drug_type_of_measurement', 'drug_type_of_measurement'),
    ('standard_charges.payers_information', 'payers_information', 'wide_payer:cms_nested'),
]

# CMS JSON field names for each canonical field. Several spellings exist
# across template versions; the first non-null one wins.
_JSON_FIELD_MAP = {
    # standard_charges level
    'gross_charge': 'gross_charge',
    'discounted_cash': 'discounted_cash_price',
    'discounted_cash_price': 'discounted_cash_price',
    'minimum': 'min_negotiated_rate',
    'min': 'min_negotiated_rate',
    'maximum': 'max_negotiated_rate',
    'max': 'max_negotiated_rate',
    'billing_class': 'billing_class',
    'setting': 'setting',
    'modifiers': 'modifiers',
    'modifier_code': 'modifiers',       # CMS 3.0
    'additional_generic_notes': 'additional_generic_notes',
    'payers_information': 'payers_information',
    # payers_information entries
    'payer_name': 'payer_name',
    'plan_name': 'plan_name',
    'standard_charge_dollar': 'negotiated_rate',
    'negotiated_dollar': 'negotiated_rate',
    'standard_charge_percentage': 'negotiated_percentage',
    'standard_charge_percent': 'negotiated_percentage',
    'negotiated_percentage': 'negotiated_percentage',
    'negotiated_percent': 'negotiated_percentage',
    'standard_charge_algorithm': 'negotiated_algorithm',
    'negotiated_algorithm': 'negotiated_algorithm',
    'methodology': 'methodology',
    'estimated_amount': 'estimated_amount',
    'additional_payer_notes': 'additional_notes',
    'footnote': 'footnote',
    'median_amount': 'median_amount',
    'median_allowed_amount': 'median_amount',
    '10th_percentile': 'pct_10',
    '90th_percentile': 'pct_90',
    'count': 'claim_count',
}

_JSON_CANONICAL_TO_KEYS: Dict[str, List[str]] = {}
for _key, _field in _JSON_FIELD_MAP.items():
    _JSON_CANONICAL_TO_KEYS.setdefault(_field, []).append(_key)

# Preferred code when an item lists several: CPT, then HCPCS, RC, CDM, NDC.
_CODE_PRIORITY = {'CPT': 0, 'HCPCS': 1, 'RC': 2, 'CDM': 3, 'NDC': 4}


def iter_json(
    path: Union[str, Path],
    compression: Optional[str] = None,
    *,
    extra_synonyms: Optional[Mapping[str, str]] = None,
    header_overrides: Optional[Mapping[str, str]] = None,
    code_extraction: Optional[Mapping[str, Any]] = None,
    ref: Optional[ReferenceData] = None,
    stats: Optional[ParseStats] = None,
) -> Iterator[Any]:
    """Yield the records in a hospital MRF JSON file.

    Order: :class:`FileMetadata` (when the root has any),
    :class:`ModifierInfo` records, :class:`HeaderMapping` records, then the
    data records. The file is opened several times (metadata, modifier
    information, items), so *path* must be a file, not a stream.
    """
    path = Path(path)
    stats = stats if stats is not None else ParseStats()
    if compression is None:
        _, compression = detect_file_format(path)

    metadata, warning = _extract_json_root_metadata(path, compression)
    if warning:
        stats.warn(warning)
    record = _file_metadata(metadata)
    if record is not None:
        yield record

    # ponytail: modifier_information usually follows the big item array, so
    # this is a second full read of the file. One pass with a push parser
    # would save it if read time ever matters.
    for entry in _extract_json_modifier_information(path, compression):
        yield from _modifier_records(entry, ref)

    prefix = _detect_json_array_prefix(path, compression)
    if prefix is None:
        key = _find_data_array_key(path, compression)
        if key is None:
            stats.warn("No data array found in the JSON root object")
            return
        log.warning("No known data array; reading the first root array %r", key)
        prefix = f'{key}.item'

    yield from records_from_items(
        _iter_items(path, compression, prefix, stats),
        extra_synonyms=extra_synonyms, header_overrides=header_overrides,
        code_extraction=code_extraction, ref=ref, stats=stats,
    )


def records_from_items(
    items: Iterable[Any],
    *,
    extra_synonyms: Optional[Mapping[str, str]] = None,
    header_overrides: Optional[Mapping[str, str]] = None,
    code_extraction: Optional[Mapping[str, Any]] = None,
    ref: Optional[ReferenceData] = None,
    stats: Optional[ParseStats] = None,
) -> Iterator[Any]:
    """Turn parsed JSON items (dicts) into records.

    The first item decides the shape: CMS nested, or flat rows.
    """
    stats = stats if stats is not None else ParseStats()
    dicts = (obj for obj in items if isinstance(obj, dict))
    first = next(dicts, None)
    if first is None:
        return

    if _is_cms_format(first):
        log.info("Detected CMS nested format")
        for source, normalized, mapped in _CMS_HEADER_MAPPINGS:
            yield HeaderMapping(source, normalized, mapped)
        builder = RecordBuilder(code_extraction=code_extraction, ref=ref, stats=stats)
        for obj in itertools.chain([first], dicts):
            for row in _flatten_cms_item(obj):
                stats.rows_read += 1
                yield from _cms_row_records(row, builder)
        return

    peek = [first] + list(itertools.islice(dicts, PEEK_ROWS - 1))
    layout = TableLayout(list(first.keys()), peek, extra_synonyms=extra_synonyms,
                         header_overrides=header_overrides, code_extraction=code_extraction,
                         ref=ref, stats=stats)
    yield from layout.header_mappings()
    for obj in itertools.chain(peek, dicts):
        stats.rows_read += 1
        yield from layout.row_to_records(obj)


# ---------------------------------------------------------------------------
# Streaming items
# ---------------------------------------------------------------------------

def _iter_items(path: Path, compression: Optional[str], prefix: str,
                stats: ParseStats) -> Iterator[Any]:
    """Stream the items under *prefix*, repairing the JSON only if needed.

    The repair pass (control characters, double and trailing commas) is slow
    pure Python, so the file is first read as is. On a syntax error it is
    reopened with repair on, and the items already yielded are skipped:
    repair only touches damaged bytes, and every item before the damage
    parsed cleanly.
    """
    done = 0
    try:
        with open_json(path, compression) as fh:
            for obj in ijson.items(fh, prefix):
                yield obj
                done += 1
        return
    except ijson.JSONError as exc:
        stats.warn(f"JSON syntax error after {done} items ({exc}); retrying with repair")
    with open_json(path, compression, sanitize=True) as fh:
        for obj in itertools.islice(ijson.items(fh, prefix), done, None):
            yield obj


def _detect_json_array_prefix(path: Path, compression: Optional[str]) -> Optional[str]:
    """Return the ijson prefix of the item array, from the first 64 KB.

    ``item`` for a root array, ``<key>.item`` when a known data key shows up
    early, or None when the root is an object without one.
    """
    with open_json(path, compression) as fh:
        head = fh.read(_PREFIX_SNIFF_BYTES).decode('utf-8', errors='replace').strip()
    if head.startswith('['):
        return 'item'
    for key in DATA_KEYS:
        if f'"{key}"' in head:
            return f'{key}.item'
    return None


def _find_data_array_key(path: Path, compression: Optional[str]) -> Optional[str]:
    """Scan the root object for the array that holds the items.

    A known data key wins wherever it appears; otherwise the first root
    array that is not metadata. Used when the item array starts later than
    the first 64 KB or has an unusual name.
    """
    first_candidate = None
    with open_json(path, compression) as fh:
        for prefix, event, _value in ijson.parse(fh):
            if event != 'start_array' or not prefix or '.' in prefix:
                continue
            if prefix in METADATA_ARRAY_KEYS:
                continue
            if prefix in DATA_KEYS:
                return prefix
            if first_candidate is None:
                first_candidate = prefix
    return first_candidate


def _is_cms_format(obj: Mapping[str, Any]) -> bool:
    """True for the CMS nested item shape."""
    return 'code_information' in obj or 'standard_charges' in obj


# ---------------------------------------------------------------------------
# Root metadata and modifier information
# ---------------------------------------------------------------------------

def _extract_json_root_metadata(path: Path, compression: Optional[str]) -> Tuple[Dict[str, Any], Optional[str]]:
    """Read the root-level metadata, stopping at the item array.

    Returns ``(metadata, warning)``. Keys: hospital_name, hospital_location
    and hospital_address (lists), license_number, license_state,
    last_updated_on, version, attestation, attester_name,
    confirm_attestation, type_2_npi (list). CMS 3.0 ``location_name`` is
    reported as hospital_location.
    """
    scalar_keys = {'hospital_name', 'last_updated_on', 'version',
                   'attestation', 'attester_name', 'confirm_attestation'}
    array_keys = {'hospital_location', 'hospital_address', 'type_2_npi', 'location_name'}
    metadata: Dict[str, Any] = {}
    warning = None
    try:
        with open_json(path, compression) as fh:
            current = None
            array_key, array_values = None, []
            in_license, license_info, license_key = False, {}, None
            in_attestation, attestation_info, attestation_key = False, {}, None
            for prefix, event, value in ijson.parse(fh):
                if prefix == '' and event == 'map_key':
                    # A new root key: finish whatever was being collected.
                    if array_key and array_values:
                        metadata[array_key] = array_values
                        array_key, array_values = None, []
                    if in_license and license_info:
                        metadata['license_number'] = license_info.get('license_number')
                        metadata['license_state'] = license_info.get('state')
                        in_license, license_info = False, {}
                    if in_attestation and attestation_info:
                        metadata.update(attestation_info)
                        in_attestation, attestation_info = False, {}
                    current = value
                    if current in DATA_KEYS:
                        break  # do not read the item array
                elif prefix in scalar_keys and event in ('string', 'boolean', 'number'):
                    metadata[prefix] = value
                elif current in array_keys:
                    if event == 'start_array':
                        array_key, array_values = current, []
                    elif event in ('string', 'number') and array_key:
                        array_values.append(str(value))
                    elif event == 'end_array' and array_key:
                        metadata[array_key] = array_values
                        array_key, array_values = None, []
                elif current == 'license_information':
                    # An object, or an array of them.
                    if event == 'start_map':
                        in_license, license_info = True, {}
                    elif event == 'map_key' and in_license:
                        license_key = value
                    elif event in ('string', 'number') and in_license and license_key:
                        license_info[license_key] = value
                        license_key = None
                    elif event == 'end_map' and in_license:
                        metadata['license_number'] = license_info.get('license_number')
                        metadata['license_state'] = license_info.get('state')
                        in_license = False
                elif current == 'attestation' and not in_attestation:
                    # CMS 3.0 nests attestation in an object; a CMS 2.0
                    # scalar is caught by the scalar branch above.
                    if event == 'start_map':
                        in_attestation, attestation_info = True, {}
                elif in_attestation:
                    if event == 'map_key':
                        attestation_key = value
                    elif event in ('string', 'boolean', 'number') and attestation_key:
                        attestation_info[attestation_key] = value
                        attestation_key = None
                    elif event == 'end_map':
                        metadata.update(attestation_info)
                        in_attestation = False
    except Exception as exc:  # metadata is optional: never fail the file on it
        warning = f"Could not extract JSON root metadata: {exc}"
    if 'location_name' in metadata:
        metadata['hospital_location'] = metadata.pop('location_name')
    return metadata, warning


def _extract_json_modifier_information(path: Path, compression: Optional[str]) -> List[Dict[str, Any]]:
    """Read the CMS 3.0 root ``modifier_information`` array.

    Returns dicts with code, description, setting and
    modifier_payer_information (payer_name, plan_name, description).
    Entries without a code are dropped. Errors give an empty list.
    """
    results = []
    try:
        with open_json(path, compression) as fh:
            for obj in ijson.items(fh, 'modifier_information.item'):
                if not isinstance(obj, dict) or not obj.get('code', ''):
                    continue
                results.append({
                    'code': obj.get('code', ''),
                    'description': obj.get('description'),
                    'setting': obj.get('setting'),
                    'modifier_payer_information': [
                        {'payer_name': p.get('payer_name', ''),
                         'plan_name': p.get('plan_name', ''),
                         'description': p.get('description')}
                        for p in (obj.get('modifier_payer_information') or [])
                        if isinstance(p, dict)
                    ],
                })
    except Exception as exc:  # optional metadata
        log.warning("Could not extract modifier_information: %s", exc)
    return results


def _file_metadata(meta: Dict[str, Any]) -> Optional[FileMetadata]:
    if not meta:
        return None

    def joined(key):
        values = [v for v in (meta.get(key) or []) if v]
        return '|'.join(values) or None

    def text(key):
        value = meta.get(key)
        return str(value) if value is not None and str(value) != '' else None

    license_number = text('license_number')
    license_state = meta.get('license_state')
    if license_state:
        license_state = str(license_state).strip().upper()
        if len(license_state) > 2:
            license_state = _STATE_NAME_TO_ABBR.get(license_state)
        if license_state not in _US_STATES:
            license_state = None
    # Older files write "12345|CA" in the license number itself.
    if not license_state and license_number and '|' in license_number:
        number, _, state = license_number.partition('|')
        license_number = number.strip()
        state = state.strip().upper()[:2]
        license_state = state if state in _US_STATES else None

    npis = [n for n in (_sanitize_npi(str(v)) for v in (meta.get('type_2_npi') or [])) if n]
    confirm = meta.get('confirm_attestation')
    return FileMetadata(
        hospital_name=text('hospital_name'),
        hospital_location=joined('hospital_location'),
        hospital_address=joined('hospital_address'),
        license_number=license_number,
        license_state=license_state,
        type_2_npi='|'.join(npis) or None,
        last_updated_on=text('last_updated_on'),
        version=text('version'),
        attestation=text('attestation'),
        attester_name=text('attester_name'),
        confirm_attestation=bool(confirm) if confirm is not None else None,
    )


def _modifier_records(entry: Dict[str, Any], ref: Optional[ReferenceData]) -> Iterator[ModifierInfo]:
    """One general record per modifier, then one per payer that defines it."""
    yield ModifierInfo(entry['code'], entry.get('description'), entry.get('setting'))
    for info in entry['modifier_payer_information']:
        payer_name, _plan = normalize_payer_name(info.get('payer_name', ''), ref=ref)
        if not payer_name:
            continue
        yield ModifierInfo(
            entry['code'], entry.get('description'), entry.get('setting'),
            payer_name=payer_name, raw_payer_name=info.get('payer_name') or None,
            plan_name=info.get('plan_name') or None, payer_description=info.get('description'),
        )


# ---------------------------------------------------------------------------
# CMS nested items
# ---------------------------------------------------------------------------

def _json_get(obj: Mapping[str, Any], canonical: str, default=None):
    """Return the first non-null value among the JSON spellings of *canonical*."""
    for key in _JSON_CANONICAL_TO_KEYS.get(canonical, ()):
        value = obj.get(key)
        if value is not None:
            return value
    return default


def _flatten_cms_item(obj: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Flatten one CMS item into one row per (billing_class, setting, modifiers).

    The item's best code is used (CPT, then HCPCS, RC, CDM, NDC). Charges
    sharing a group are merged, and their payers collected under 'payers'.
    CMS 3.0 ``billing_class: both`` becomes a facility row and a
    professional row with the same prices.
    """
    description = obj.get('description', '')
    code_info = obj.get('code_information', [])
    charges = obj.get('standard_charges', [])
    drug_info = obj.get('drug_information', {})
    if not code_info and not charges:
        return []

    drug_unit = str(drug_info.get('unit', '')) if drug_info else None
    drug_type = str(drug_info.get('type', '')) if drug_info else None
    drug_unit = drug_unit if drug_unit and drug_unit.strip() else None
    drug_type = drug_type if drug_type and drug_type.strip() else None

    best_code = best_type = None
    best_priority = 99
    for ci in code_info:
        ctype = str(ci.get('type', '')).upper()
        priority = _CODE_PRIORITY.get(ctype, 10)
        if priority < best_priority:
            best_code, best_type, best_priority = str(ci.get('code', '')), ctype, priority
    best_code, best_type = normalize_code(best_code, best_type)

    def row(bc, setting, modifiers, pricing):
        def text(v):
            return str(v) if v is not None else None
        return {
            'description': description,
            'code': best_code,
            'code_type': best_type,
            'gross_charge': text(pricing['gross']),
            'discounted_cash_price': text(pricing['cash']),
            'min_negotiated_rate': text(pricing['min_rate']),
            'max_negotiated_rate': text(pricing['max_rate']),
            'billing_class': bc,
            'setting': setting,
            'modifiers': modifiers,
            'drug_unit_of_measurement': drug_unit,
            'drug_type_of_measurement': drug_type,
            'additional_generic_notes': pricing.get('additional_generic_notes'),
            'payers': pricing['payers'],
        }

    if not charges:
        empty = {'gross': None, 'cash': None, 'min_rate': None, 'max_rate': None,
                 'additional_generic_notes': None, 'payers': []}
        return [row('', '', None, empty)]

    groups: Dict[Tuple[str, str, Optional[str]], Dict[str, Any]] = {}
    for c in charges:
        bc = (_json_get(c, 'billing_class') or '').strip().lower()
        bc = bc if bc in ('facility', 'professional', 'both') else ''
        setting = (_json_get(c, 'setting') or '').strip().lower()
        modifiers = _json_get(c, 'modifiers')
        if isinstance(modifiers, list):
            modifiers = ','.join(s for s in (str(m).strip() for m in modifiers) if s) or None
        elif modifiers:
            modifiers = str(modifiers).strip() or None
        else:
            modifiers = None
        notes = _json_get(c, 'additional_generic_notes')
        notes = notes.strip() or None if isinstance(notes, str) else None
        payers = _json_get(c, 'payers_information') or []

        key = (bc, setting, modifiers)
        if key not in groups:
            groups[key] = {
                'gross': _json_get(c, 'gross_charge'),
                'cash': _json_get(c, 'discounted_cash_price'),
                'min_rate': _json_get(c, 'min_negotiated_rate'),
                'max_rate': _json_get(c, 'max_negotiated_rate'),
                'additional_generic_notes': notes,
                'payers': list(payers),
            }
        else:
            g = groups[key]
            g['gross'] = g['gross'] or _json_get(c, 'gross_charge')
            g['cash'] = g['cash'] or _json_get(c, 'discounted_cash_price')
            g['min_rate'] = g['min_rate'] or _json_get(c, 'min_negotiated_rate')
            g['max_rate'] = g['max_rate'] or _json_get(c, 'max_negotiated_rate')
            g['additional_generic_notes'] = g['additional_generic_notes'] or notes
            g['payers'].extend(payers)

    rows = []
    for (bc, setting, modifiers), pricing in groups.items():
        for expanded in (('facility', 'professional') if bc == 'both' else (bc,)):
            rows.append(row(expanded, setting, modifiers, pricing))
    return rows


def _cms_row_records(row: Dict[str, Any], builder: RecordBuilder) -> Iterator[Any]:
    """Records for one flattened CMS row."""
    code, code_type, description = row.get('code'), row.get('code_type'), row.get('description')
    if not any([code, code_type, description]):
        return
    cleaned = builder.clean_code(code, code_type, description)
    if cleaned is None:
        return
    code, code_type, baked = cleaned
    billing_class = row.get('billing_class')
    setting = row.get('setting')
    modifiers = merge_modifier_into_field(row.get('modifiers'), baked)
    code, code_type, billing_class = builder.effective(code, code_type, description, billing_class)

    yield from builder.item(code, code_type, description, billing_class, setting, modifiers, row)

    gross = _safe_float(row.get('gross_charge'))
    cash = _safe_float(row.get('discounted_cash_price'))
    min_rate = _safe_float(row.get('min_negotiated_rate'))
    max_rate = _safe_float(row.get('max_negotiated_rate'))
    payers = row.get('payers', [])
    # Self-pay entries are the cash price: keep the lowest.
    for entry in payers:
        raw = (_json_get(entry, 'payer_name') or '').strip()
        if raw and is_self_pay_payer(raw):
            rate = _safe_float(_json_get(entry, 'negotiated_rate') or _json_get(entry, 'estimated_amount'))
            if rate is not None and (cash is None or rate < cash):
                cash = rate
    if any(v is not None for v in (gross, cash, min_rate, max_rate)):
        yield StandardCharge(code, code_type, description, billing_class or '', setting or '',
                             modifiers, gross, cash, min_rate, max_rate)

    for entry in payers:
        raw = (_json_get(entry, 'payer_name') or '').strip()
        if not raw or is_self_pay_payer(raw):
            continue
        values = dict(
            negotiated_rate=_safe_float(_json_get(entry, 'negotiated_rate')),
            negotiated_percentage=_safe_float(_json_get(entry, 'negotiated_percentage')),
            negotiated_algorithm=_json_get(entry, 'negotiated_algorithm') or None,
            methodology=_json_get(entry, 'methodology') or None,
            estimated_amount=_safe_float(_json_get(entry, 'estimated_amount')),
        )
        if not any(values.values()):
            continue
        yield from builder.rate(
            code, code_type, description, billing_class, setting, modifiers,
            payer=raw, plan=(_json_get(entry, 'plan_name') or '').strip() or None,
            rate_billing_class=billing_class, rate_setting=setting,
            additional_notes=_json_get(entry, 'additional_notes') or None,
            footnote=_json_get(entry, 'footnote') or None,
            median_amount=_safe_float(_json_get(entry, 'median_amount')),
            pct_10=_safe_float(_json_get(entry, 'pct_10')),
            pct_90=_safe_float(_json_get(entry, 'pct_90')),
            claim_count=_safe_count(_json_get(entry, 'claim_count')),
            **values,
        )
