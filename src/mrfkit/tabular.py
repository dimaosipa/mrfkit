"""Turn table rows into records.

A "table" is a CSV file, or the flat objects of a JSON file that does not use
the CMS nested layout. Both have a header (column names or object keys) and
rows of values, and hospitals use the same layouts in both: tall (one payer
per row), CMS wide (one column group per payer and plan), simple wide (one
column per payer), and a few vendor-specific shapes.

:class:`TableLayout` reads the header once and decides how to read every row.
:meth:`TableLayout.rows_to_records` then turns each row into records.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from .codes import (
    apply_baked_modifier_split,
    apply_code_extraction,
    infer_billing_class,
    is_rejected_code,
    merge_modifier_into_field,
    normalize_code,
)
from .headers import (
    ATRIUM_SETTING_COLUMNS,
    CANONICAL_FIELDS,
    HAWAII_SETTING_COLUMNS,
    _ATRIUM_DETECT_COLUMNS,
    _HAWAII_DETECT_COLUMNS,
    _is_unmapped_header_suppressed,
    map_header,
    normalize_header,
    parse_hawaii_payer_header,
    parse_wide_payer_header,
)
from .payers import is_self_pay_payer, is_valid_payer_name, normalize_payer_name, normalize_plan_name
from .records import ChargeItem, HeaderMapping, PayerRate, StandardCharge, UnmappedCell
from .reference import ParseStats, ReferenceData
from .values import (
    _SIMPLE_WIDE_PEEK_ROWS,
    _clean_methodology,
    _column_looks_like_payer_rates,
    _is_null_rate_token,
    _safe_count,
    _safe_float,
    hoist_numeric_methodology,
    normalize_methodology,
)

log = logging.getLogger(__name__)

# At most this many unmapped cells are kept per column. A mis-detected column
# would otherwise repeat on every row of a multi-million-row file.
UNMAPPED_CELLS_PER_COLUMN_CAP = 2000

# Simple wide format needs at least this many payer-looking columns.
SIMPLE_WIDE_MIN_PAYER_COLS = 3
PEEK_ROWS = _SIMPLE_WIDE_PEEK_ROWS

# Price-bearing fields. A row with any of them is data, never a section header.
_PRICE_FIELDS = frozenset({
    'gross_charge', 'discounted_cash_price', 'negotiated_rate',
    'min_negotiated_rate', 'max_negotiated_rate', 'estimated_amount',
    'median_amount',
})

# Partners Healthcare style files: ip_* columns map to fields, op_* columns
# fill in when the ip_* value is empty or zero.
_OP_FALLBACK_MAP = {
    'negotiated_rate': ['op_price'],
    'min_negotiated_rate': ['min_op_reimb'],
    'max_negotiated_rate': ['max_op_reimb'],
    'estimated_amount': ['op_expected_reimbursement'],
}

_ZERO = ('', '0', '0.00')


class SectionBoundary(Exception):
    """A later section of a stacked multi-table CSV starts at this row."""


def cell(row: Mapping[str, Any], key: Optional[str]) -> str:
    """Return a cell as stripped text: '' for missing, JSON for nested values."""
    if key is None:
        return ''
    value = row.get(key)
    if value is None:
        return ''
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return str(value).strip()


def _number(text: str) -> Optional[float]:
    """Parse a price cell, treating empty and N/A as missing."""
    if not text or text.upper() == 'N/A':
        return None
    return _safe_float(text)


def _strip_nuls(row: Dict[str, Any]) -> Dict[str, Any]:
    """Drop NUL characters: they are never meaningful and break most consumers."""
    if not any(isinstance(v, str) and '\x00' in v for v in row.values()):
        return row
    return {k: v.replace('\x00', '') if isinstance(v, str) else v for k, v in row.items()}


def _normalize_billing_class(value: Optional[str]) -> str:
    value = (value or '').strip().lower()
    return value if value in ('facility', 'professional') else ''


class RecordBuilder:
    """Shared record building: item dedup, code cleanup, payer rate cleanup.

    One instance per file, so a charge item is emitted once per file.
    """

    def __init__(self, *, code_extraction: Optional[Mapping[str, Any]] = None,
                 ref: Optional[ReferenceData] = None, stats: Optional[ParseStats] = None):
        self.ref = ref
        self.stats = stats
        self._code_config = {'code_extraction': dict(code_extraction)} if code_extraction else None
        self._seen_items: set = set()

    def clean_code(self, code, code_type, description):
        """Run one (code, code_type) through extraction, normalization and the
        baked-modifier split. Return ``(code, code_type, baked_modifier)``, or
        None when the pair is too corrupt to keep."""
        code, code_type = apply_code_extraction(code or None, code_type or None, self._code_config)
        code, code_type = normalize_code(code or None, code_type or None)
        code, code_type, baked = apply_baked_modifier_split(
            code, code_type, description=description, ref=self.ref, stats=self.stats)
        if is_rejected_code(code, code_type, stats=self.stats):
            return None
        return code, code_type, baked

    def effective(self, code, code_type, description, billing_class):
        """Apply billing-class inference, which can also fix the code and type."""
        inferred_bc, normalized_code, inferred_ct = infer_billing_class(
            code, code_type, description, billing_class)
        return (normalized_code if normalized_code != code else code,
                inferred_ct if inferred_ct != code_type else code_type,
                billing_class or inferred_bc or '')

    def item(self, code, code_type, description, billing_class, setting, modifiers,
             data) -> Iterator[ChargeItem]:
        """Yield the charge item unless this file already produced its key."""
        key = (code, code_type, billing_class or '', setting or '', modifiers or '')
        if key in self._seen_items:
            return
        self._seen_items.add(key)
        yield ChargeItem(
            code, code_type, description, billing_class or '', setting or '', modifiers,
            drug_unit_of_measurement=data.get('drug_unit_of_measurement'),
            drug_type_of_measurement=data.get('drug_type_of_measurement'),
            additional_generic_notes=data.get('additional_generic_notes'),
        )

    def rate(self, code, code_type, description, billing_class, setting, modifiers, *,
             payer, plan, rate_billing_class, rate_setting, methodology=None,
             negotiated_rate=None, negotiated_percentage=None, negotiated_algorithm=None,
             estimated_amount=None, additional_notes=None, footnote=None,
             median_amount=None, pct_10=None, pct_90=None, claim_count=None,
             ) -> Iterator[PayerRate]:
        """Yield a payer rate with normalized payer, plan and methodology."""
        payer_name, plan_name = normalize_payer_name(payer, plan, ref=self.ref)
        if not payer_name:
            return
        plan_category, plan_network, plan_name = normalize_plan_name(plan_name, payer_name)
        methodology_type = normalize_methodology(methodology, ref=self.ref)
        methodology = _clean_methodology(methodology)
        methodology, methodology_type, negotiated_percentage = hoist_numeric_methodology(
            methodology, methodology_type, negotiated_rate, negotiated_percentage,
            negotiated_algorithm,
        )
        yield PayerRate(
            code, code_type, description, billing_class or '', setting or '', modifiers,
            payer_name=payer_name, raw_payer_name=payer, plan_name=plan_name,
            plan_category=plan_category or 'Other', plan_network=plan_network,
            negotiated_rate=negotiated_rate, negotiated_percentage=negotiated_percentage,
            negotiated_algorithm=negotiated_algorithm, methodology=methodology,
            methodology_type=methodology_type, estimated_amount=estimated_amount,
            rate_billing_class=rate_billing_class or '', rate_setting=rate_setting or '',
            additional_notes=additional_notes, footnote=footnote,
            median_amount=median_amount, pct_10=pct_10, pct_90=pct_90, claim_count=claim_count,
        )


class TableLayout(RecordBuilder):
    """How to read the rows of one table, decided from its header.

    *peek_rows* are the first data rows. They are only used to spot the
    simple wide format; the caller still passes them to
    :meth:`rows_to_records`.
    """

    def __init__(
        self,
        headers: Sequence[str],
        peek_rows: Sequence[Mapping[str, Any]] = (),
        *,
        extra_synonyms: Optional[Mapping[str, str]] = None,
        header_overrides: Optional[Mapping[str, str]] = None,
        code_extraction: Optional[Mapping[str, Any]] = None,
        ref: Optional[ReferenceData] = None,
        stats: Optional[ParseStats] = None,
    ):
        super().__init__(code_extraction=code_extraction, ref=ref, stats=stats)
        self.headers = list(headers)
        self.mappings: Dict[str, HeaderMapping] = {}

        canonical_map: Dict[str, str] = {}
        unmapped: List[str] = []
        self.wide_payer_groups: Dict[Tuple[str, str], Dict[str, str]] = {}

        for header in self.headers:
            wide = parse_wide_payer_header(header)
            if wide:
                payer, plan, rate_field = wide
                self.wide_payer_groups.setdefault((payer, plan), {})[rate_field] = header
                self._map(header, f'wide_payer:{rate_field}')
                continue
            _normalized, field = map_header(header)
            self._map(header, field)
            if field:
                canonical_map[header] = field
            else:
                unmapped.append(header)

        # Caller overrides beat synonyms: exact header first, then normalized.
        for source, lookup in (('override', header_overrides or {}),
                               ('synonym', extra_synonyms or {})):
            if not lookup or not unmapped:
                continue
            still, applied = [], []
            for header in unmapped:
                key = header if source == 'override' else normalize_header(header)
                target = lookup.get(key)
                if target in CANONICAL_FIELDS:
                    canonical_map[header] = target
                    self._map(header, target)
                    applied.append(f"{header} -> {target}")
                else:
                    if target:
                        log.warning("Ignoring %s for %r: %r is not a canonical field",
                                    source, header, target)
                    still.append(header)
            unmapped = still
            if applied:
                log.info("Header %ss applied: %s", source, applied)

        self.has_payer_column = 'payer_name' in canonical_map.values()

        # ---- code columns: one pair per code|N column, or a single pair ----
        code_headers = [h for h, f in canonical_map.items() if f == 'code']
        code_type_headers = [h for h, f in canonical_map.items() if f == 'code_type']
        self.fields = {h: f for h, f in canonical_map.items() if f not in ('code', 'code_type')}
        self.code_pairs: List[Tuple[Optional[str], Optional[str]]] = []
        if len(code_headers) > 1:
            log.info("Multi-code detected: %d code columns", len(code_headers))
            type_by_index = {}
            for h in code_type_headers:
                parts = [p.strip() for p in h.split('|')]
                if len(parts) >= 3:
                    type_by_index[parts[1]] = h
            for h in code_headers:
                parts = [p.strip() for p in h.split('|')]
                if len(parts) >= 2:
                    self.code_pairs.append((h, type_by_index.get(parts[1])))
                else:
                    self.code_pairs.append((h, code_type_headers[0] if code_type_headers else None))
        elif code_headers or code_type_headers:
            self.code_pairs.append((code_headers[0] if code_headers else None,
                                    code_type_headers[0] if code_type_headers else None))

        # ---- outpatient fallback columns ----
        mapped_fields = set(canonical_map.values())
        unmapped_by_norm = {normalize_header(h): h for h in unmapped}
        self.op_fallbacks: Dict[str, str] = {}
        for field, candidates in _OP_FALLBACK_MAP.items():
            if field in mapped_fields:
                for candidate in candidates:
                    if candidate in unmapped_by_norm:
                        self.op_fallbacks[field] = unmapped_by_norm[candidate]
                        break

        # ---- drop low-value columns from unmapped tracking ----
        suppressed = sorted(normalize_header(h) for h in unmapped
                            if _is_unmapped_header_suppressed(normalize_header(h)))
        if suppressed:
            unmapped = [h for h in unmapped if not _is_unmapped_header_suppressed(normalize_header(h))]
            log.info("Suppressed %d low-value unmapped columns from cell tracking: %s",
                     len(suppressed), suppressed)

        # ---- Hawaii: setting-specific charge columns, payer_setting_method columns ----
        norm_set = {normalize_header(h) for h in unmapped}
        self.hawaii = _HAWAII_DETECT_COLUMNS.issubset(norm_set)
        self.hawaii_settings: Dict[str, Dict[str, str]] = {}
        self.hawaii_payers: List[Tuple[str, str, str, str]] = []
        if self.hawaii:
            by_norm = {normalize_header(h): h for h in unmapped}
            consumed = set()
            for norm, (field, setting) in HAWAII_SETTING_COLUMNS.items():
                if norm in by_norm:
                    self.hawaii_settings.setdefault(setting, {})[field] = by_norm[norm]
                    consumed.add(by_norm[norm])
                    self._map(by_norm[norm], f'hawaii_wide:{field}:{setting}')
            for h in unmapped:
                if h in consumed:
                    continue
                parsed = parse_hawaii_payer_header(h)
                if parsed:
                    payer, setting, methodology = parsed
                    self.hawaii_payers.append((h, payer, setting, methodology))
                    consumed.add(h)
                    self._map(h, f'hawaii_payer:{setting}:{methodology}')
            unmapped = [h for h in unmapped if h not in consumed]
            log.info("Hawaii wide format detected: %d settings, %d payer columns",
                     len(self.hawaii_settings), len(self.hawaii_payers))

        # ---- Atrium: setting-specific charge columns plus a Min/Max flag ----
        self.atrium = not self.hawaii and _ATRIUM_DETECT_COLUMNS.issubset(norm_set)
        self.atrium_settings: Dict[str, Dict[str, str]] = {}
        self.atrium_minmax: Optional[str] = None
        if self.atrium:
            by_norm = {normalize_header(h): h for h in unmapped}
            consumed = set()
            for norm, (field, setting) in ATRIUM_SETTING_COLUMNS.items():
                if norm in by_norm:
                    self.atrium_settings.setdefault(setting, {})[field] = by_norm[norm]
                    consumed.add(by_norm[norm])
                    self._map(by_norm[norm], f'atrium_wide:{field}:{setting}')
            if 'min_max' in by_norm:
                self.atrium_minmax = by_norm['min_max']
                consumed.add(self.atrium_minmax)
                self._map(self.atrium_minmax, 'atrium_wide:min_max_indicator')
            unmapped = [h for h in unmapped if h not in consumed]
            norm_set = {normalize_header(h) for h in unmapped}
            log.info("Atrium wide format detected: %d settings, min/max=%s",
                     len(self.atrium_settings), 'yes' if self.atrium_minmax else 'no')

        # ---- Paris style: an extra outpatient gross price column ----
        self.outpatient_price: Optional[str] = None
        if not self.hawaii and not self.atrium and 'outpatient_price' in norm_set:
            self.outpatient_price = {normalize_header(h): h for h in unmapped}['outpatient_price']
            unmapped = [h for h in unmapped if h != self.outpatient_price]
            self._map(self.outpatient_price, 'outpatient_gross:gross_charge:outpatient')
            log.info("Outpatient price column detected: %r", self.outpatient_price)

        # ---- simple wide: one plain column per payer, values are dollars ----
        if (not self.wide_payer_groups and not self.has_payer_column
                and len(unmapped) >= SIMPLE_WIDE_MIN_PAYER_COLS and peek_rows):
            payer_cols = [h for h in unmapped
                          if _column_looks_like_payer_rates(cell(r, h) for r in peek_rows)]
            if len(payer_cols) >= SIMPLE_WIDE_MIN_PAYER_COLS:
                for h in payer_cols:
                    if is_valid_payer_name(h.strip()):
                        self.wide_payer_groups[(h.strip(), '')] = {'negotiated_rate': h}
                        self._map(h, 'wide_payer:negotiated_rate')
                taken = set(payer_cols)
                unmapped = [h for h in unmapped if h not in taken]
                log.info("Simple wide format: %d payer columns auto-detected", len(payer_cols))

        self.wide_self_pay = frozenset(k for k in self.wide_payer_groups if is_self_pay_payer(k[0]))
        if self.wide_payer_groups:
            log.info("CMS wide format: %d payer-plan combinations detected",
                     len(self.wide_payer_groups))
        self.unmapped = unmapped
        self._unmapped_counts: Dict[str, int] = {}

    # ------------------------------------------------------------------

    def _map(self, header: str, mapped_to: Optional[str]) -> None:
        self.mappings[header] = HeaderMapping(header, normalize_header(header), mapped_to)

    def header_mappings(self) -> List[HeaderMapping]:
        """One record per source column, saying what it was read as."""
        return [self.mappings[h] for h in self.headers if h in self.mappings]

    def _unmapped_cells(self, row, code, code_type, description, billing_class, setting,
                        modifiers) -> Iterator[UnmappedCell]:
        for header in self.unmapped:
            value = cell(row, header)
            if not value or _is_null_rate_token(value):
                continue
            n = self._unmapped_counts.get(header, 0)
            if n >= UNMAPPED_CELLS_PER_COLUMN_CAP:
                continue
            self._unmapped_counts[header] = n + 1
            yield UnmappedCell(code, code_type, description, billing_class or '', setting or '',
                               modifiers, source_column=header, value=value)

    # ------------------------------------------------------------------

    def _code_pairs(self, row, description) -> Optional[List[Tuple[Any, Any, Any]]]:
        """Return the row's (code, code_type, baked_modifier) pairs, or None to drop the row."""
        pairs = []
        had_input = False
        for code_src, type_src in self.code_pairs:
            code, code_type = cell(row, code_src), cell(row, type_src)
            if not code and not code_type:
                continue
            had_input = True
            cleaned = self.clean_code(code, code_type, description)
            if cleaned is not None:
                pairs.append(cleaned)
        if pairs:
            return pairs
        if had_input or not description:
            # Every code was rejected (do not fall back to a code-less item),
            # or there is nothing to identify the item by.
            return None
        return [(None, None, None)]

    def row_to_records(self, row: Dict[str, Any], *, first_row: bool = False,
                       check_sections: bool = False) -> Iterator[Any]:
        """Yield the records for one data row.

        Raises :class:`SectionBoundary` when *check_sections* is set and the
        row turns out to be the header of a later stacked section.
        """
        row = _strip_nuls(row)
        data: Dict[str, str] = {}
        for header, field in self.fields.items():
            value = cell(row, header)
            if value:
                data[field] = value
        for field, op_header in self.op_fallbacks.items():
            if data.get(field, '') in _ZERO:
                fallback = cell(row, op_header)
                if fallback not in _ZERO:
                    data[field] = fallback
        description = data.get('description')

        if check_sections and not first_row and self._is_section_header(row, data):
            raise SectionBoundary(row)

        pairs = self._code_pairs(row, description)
        if pairs is None:
            return
        billing_class = _normalize_billing_class(data.get('billing_class'))
        setting = (data.get('setting') or '').strip().lower()

        if self.hawaii:
            yield from self._hawaii_row(row, data, pairs, description, billing_class)
        elif self.atrium:
            yield from self._atrium_row(row, data, pairs, description, billing_class)
        else:
            yield from self._tall_or_wide_row(row, data, pairs, description, billing_class, setting)

    def _is_section_header(self, row, data) -> bool:
        """A narrower row with no price whose cell re-declares the description column."""
        values = list(row.values())
        if not any(v is None for v in values):
            return False
        if any(data.get(f) for f in _PRICE_FIELDS):
            return False
        return any(isinstance(v, str) and v and map_header(v)[1] == 'description' for v in values)

    def _tall_or_wide_row(self, row, data, pairs, description, billing_class, setting):
        gross = _safe_float(data.get('gross_charge'))
        cash = _safe_float(data.get('discounted_cash_price'))
        min_rate = _safe_float(data.get('min_negotiated_rate'))
        max_rate = _safe_float(data.get('max_negotiated_rate'))
        raw_payer = data.get('payer_name')
        raw_plan = data.get('plan_name')

        # Self-pay is the cash price, not a payer rate.
        is_self_pay = bool(self.has_payer_column and raw_payer and is_self_pay_payer(raw_payer))
        if is_self_pay and cash is None:
            cash = _safe_float(data.get('negotiated_rate')) or _safe_float(data.get('estimated_amount'))
        if self.wide_self_pay and cash is None:
            for key in self.wide_self_pay:
                cols = self.wide_payer_groups[key]
                rate = _safe_float(cell(row, cols.get('negotiated_rate')) or None)
                if rate is None:
                    rate = _safe_float(cell(row, cols.get('estimated_amount')) or None)
                if rate is not None:
                    cash = rate
                    break
        has_charge = any(v is not None for v in (gross, cash, min_rate, max_rate))

        first = None
        for code, code_type, baked in pairs:
            if not any([code, code_type, description]):
                continue
            modifiers = merge_modifier_into_field(data.get('modifiers'), baked)
            code, code_type, bc = self.effective(code, code_type, description, billing_class)
            key = (code, code_type, description, bc, setting, modifiers)
            if first is None:
                first = key
            yield from self.item(code, code_type, description, bc, setting, modifiers, data)
            if has_charge:
                yield StandardCharge(code, code_type, description, bc, setting, modifiers,
                                     gross, cash, min_rate, max_rate)

            if self.outpatient_price:
                op_gross = _safe_float(cell(row, self.outpatient_price) or None)
                if op_gross is not None:
                    yield from self.item(code, code_type, description, bc, 'outpatient',
                                          modifiers, data)
                    yield StandardCharge(code, code_type, description, bc, 'outpatient',
                                         modifiers, op_gross)

            if self.has_payer_column and raw_payer and not is_self_pay:
                yield from self.rate(
                    code, code_type, description, bc, setting, modifiers,
                    payer=raw_payer, plan=raw_plan,
                    rate_billing_class=data.get('billing_class'),
                    rate_setting=data.get('setting'),
                    negotiated_rate=_safe_float(data.get('negotiated_rate')),
                    negotiated_percentage=_safe_float(data.get('negotiated_percentage')),
                    negotiated_algorithm=data.get('negotiated_algorithm'),
                    methodology=data.get('methodology'),
                    estimated_amount=_safe_float(data.get('estimated_amount')),
                    additional_notes=data.get('additional_notes'),
                    footnote=data.get('footnote'),
                    median_amount=_safe_float(data.get('median_amount')),
                    pct_10=_safe_float(data.get('pct_10')),
                    pct_90=_safe_float(data.get('pct_90')),
                    claim_count=_safe_count(data.get('claim_count')),
                )

            for (payer, plan), cols in self.wide_payer_groups.items():
                if (payer, plan) in self.wide_self_pay:
                    continue

                def text(field, cols=cols):
                    return cell(row, cols.get(field)) or None

                values = dict(
                    negotiated_rate=_safe_float(text('negotiated_rate')),
                    negotiated_percentage=_safe_float(text('negotiated_percentage')),
                    negotiated_algorithm=text('negotiated_algorithm'),
                    methodology=text('methodology'),
                    estimated_amount=_safe_float(text('estimated_amount')),
                    median_amount=_safe_float(text('median_amount')),
                    pct_10=_safe_float(text('pct_10')),
                    pct_90=_safe_float(text('pct_90')),
                    claim_count=_safe_count(text('claim_count')),
                )
                if not any(values.values()):
                    continue
                yield from self.rate(
                    code, code_type, description, bc, setting, modifiers,
                    payer=payer, plan=plan, rate_billing_class=bc, rate_setting=setting,
                    additional_notes=text('additional_notes'),
                    footnote=text('footnote') or data.get('footnote'),
                    **values,
                )

        if first is not None:
            yield from self._unmapped_cells(row, *first)

    def _hawaii_row(self, row, data, pairs, description, billing_class):
        min_rate = _safe_float(data.get('min_negotiated_rate'))
        max_rate = _safe_float(data.get('max_negotiated_rate'))
        first = None
        first_setting = None
        for code, code_type, baked in pairs:
            if not any([code, code_type, description]):
                continue
            modifiers = merge_modifier_into_field(data.get('modifiers'), baked)
            code, code_type, bc = self.effective(code, code_type, description, billing_class)
            if first is None:
                first = (code, code_type, description, bc, modifiers)
            emitted = set()
            for setting, cols in self.hawaii_settings.items():
                gross = _number(cell(row, cols.get('gross_charge')))
                cash = _number(cell(row, cols.get('discounted_cash_price')))
                # min/max are shared across settings and do not keep an
                # otherwise empty setting alive.
                if gross is None and cash is None:
                    continue
                emitted.add(setting)
                if first_setting is None:
                    first_setting = setting
                yield from self.item(code, code_type, description, bc, setting, modifiers, data)
                yield StandardCharge(code, code_type, description, bc, setting, modifiers,
                                     gross, cash, min_rate, max_rate)
            for header, payer, setting, methodology in self.hawaii_payers:
                if setting not in emitted:
                    continue
                rate = _number(cell(row, header))
                if rate is None:
                    continue
                percentage = None
                if methodology == 'percent_of_charge':
                    # Some files write 0.85 for 85%.
                    percentage, rate = (rate * 100.0 if 0 < rate <= 1 else rate), None
                yield from self.rate(
                    code, code_type, description, bc, setting, modifiers,
                    payer=payer, plan=None, rate_billing_class=bc, rate_setting=setting,
                    methodology=methodology, negotiated_rate=rate,
                    negotiated_percentage=percentage,
                )
        if first is not None and first_setting is not None:
            code, code_type, description, bc, modifiers = first
            yield from self._unmapped_cells(row, code, code_type, description, bc,
                                            first_setting, modifiers)

    def _atrium_row(self, row, data, pairs, description, billing_class):
        flag = cell(row, self.atrium_minmax).upper()
        raw_payer = data.get('payer_name')
        first = None
        first_setting = None
        for code, code_type, baked in pairs:
            if not any([code, code_type, description]):
                continue
            modifiers = merge_modifier_into_field(data.get('modifiers'), baked)
            code, code_type, bc = self.effective(code, code_type, description, billing_class)
            if first is None:
                first = (code, code_type, description, bc, modifiers)
            for setting, cols in self.atrium_settings.items():
                gross = _number(cell(row, cols.get('gross_charge')))
                cash = _number(cell(row, cols.get('discounted_cash_price')))
                negotiated = _number(cell(row, cols.get('negotiated_charge')))
                # The Min/Max column says whether the negotiated charge is a
                # min or a max. Without it, the value is a payer rate.
                min_rate = negotiated if flag == 'MIN' else None
                max_rate = negotiated if flag == 'MAX' else None
                is_self_pay = bool(raw_payer and is_self_pay_payer(raw_payer))
                if is_self_pay and cash is None and negotiated is not None:
                    cash, negotiated = negotiated, None
                if all(v is None for v in (gross, cash, min_rate, max_rate, negotiated)):
                    continue
                if first_setting is None:
                    first_setting = setting
                yield from self.item(code, code_type, description, bc, setting, modifiers, data)
                yield StandardCharge(code, code_type, description, bc, setting, modifiers,
                                     gross, cash, min_rate, max_rate)
                if raw_payer and not is_self_pay and negotiated is not None and flag not in ('MIN', 'MAX'):
                    yield from self.rate(
                        code, code_type, description, bc, setting, modifiers,
                        payer=raw_payer, plan=data.get('plan_name'),
                        rate_billing_class=bc, rate_setting=setting,
                        methodology=data.get('methodology'), negotiated_rate=negotiated,
                        additional_notes=data.get('additional_notes'),
                        footnote=data.get('footnote'),
                    )
        if first is not None and first_setting is not None:
            code, code_type, description, bc, modifiers = first
            yield from self._unmapped_cells(row, code, code_type, description, bc,
                                            first_setting, modifiers)
