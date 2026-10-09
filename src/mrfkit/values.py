"""Normalize field values: methodology, identifiers, addresses and numbers.

Small pure helpers the readers apply cell by cell: methodology labels to a
handful of types, EINs and NPIs to their compact forms, addresses to
street/city/state/zip, price and count strings to numbers, and the textual
"no rate" placeholders hospitals put in rate columns.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Dict, Optional

if TYPE_CHECKING:
    from mrfkit.reference import ReferenceData

# ============================================================================
# METHODOLOGY NORMALIZATION
# ============================================================================
# Normalizes raw methodology strings from hospital MRF files into a clean
# enum-like value.  Hospitals report methodology in varying casing and
# phrasing (e.g. "Percent of Total Billed Charges", "PERCENT OF BILLED
# CHARGES", "percent of total billed charges").  This mapping collapses
# them into a small set of canonical types used to decide how to interpret
# negotiated_rate / negotiated_percentage.
#
# Canonical values:
#   percent_of_charge  - rate is a percentage of gross charge
#   fee_schedule       - rate is a fixed dollar amount from a fee schedule
#   case_rate          - flat per-case reimbursement
#   per_diem           - flat per-day reimbursement
#   other              - methodology reported but doesn't fit above categories
#
# The function first checks caller-supplied aliases
# (``ReferenceData.methodology_aliases``), then falls back to deterministic
# pattern matching.

METHODOLOGY_MAP: Dict[str, str] = {
    # percent_of_charge variants
    'percent of total billed charges': 'percent_of_charge',
    'percent of billed charges': 'percent_of_charge',
    'percentage of billed charges': 'percent_of_charge',
    'percent of charges': 'percent_of_charge',
    'percent of charge': 'percent_of_charge',
    'percent charge': 'percent_of_charge',
    'pct of charges': 'percent_of_charge',
    '% of charges': 'percent_of_charge',
    '% of billed charges': 'percent_of_charge',
    # fee_schedule variants
    'fee schedule': 'fee_schedule',
    'fee-schedule': 'fee_schedule',
    'fee for service': 'fee_schedule',
    # case_rate variants
    'case rate': 'case_rate',
    'case-rate': 'case_rate',
    'case based': 'case_rate',
    # per_diem variants
    'per diem': 'per_diem',
    'per-diem': 'per_diem',
    'perdiem': 'per_diem',
    # other
    'other': 'other',
}


# Bare-number methodologies.
# A handful of MRF sources (a single "machine-readable-files.com" CSV vendor,
# observed in Abbeville Area Medical Center + Self Regional Healthcare) drop a
# bare number straight into the methodology column instead of a description.
# A value in (0, 100] is a percentage-of-charge, so classify its *type* as such
# rather than the catch-all 'other'. A bare 0, negatives, and values > 100 stay
# 'other' because they are ambiguous: a > 100 value may be a flat dollar amount
# OR a percent-of-benchmark such as "150% of Medicare", and there is no
# dollar-rate methodology type. This classifies the TYPE only; it does NOT move
# the number into negotiated_percentage / negotiated_rate (see
# hoist_numeric_methodology for the cases that are safe to move).
_BARE_NUMBER_RE = re.compile(r'^[0-9]+(?:\.[0-9]+)?$')


def normalize_methodology(
    raw: Optional[str],
    *,
    ref: Optional[ReferenceData] = None,
) -> Optional[str]:
    """
    Normalize a raw methodology string into a canonical type.

    Resolution order:
      1. NULL / empty → None
      2. ``ref.methodology_aliases`` (uppercased raw value → type)
      3. Exact match in METHODOLOGY_MAP (lowercased)
      4. Bare number in (0, 100] → percent_of_charge (dropped-in percentage)
      5. Pattern-based fallback ('percent' → percent_of_charge, etc.)
      6. Unknown → 'other'
    """
    if not raw or not raw.strip():
        return None

    cleaned = raw.strip()
    upper = cleaned.upper()

    # 1. Check caller-supplied aliases first
    aliases: Dict[str, str] = ref.methodology_aliases if ref is not None else {}
    if upper in aliases:
        return aliases[upper]

    # 2. Exact match in static map
    lower = cleaned.lower()
    if lower in METHODOLOGY_MAP:
        return METHODOLOGY_MAP[lower]

    # 3. Bare numeric methodology (no words, no % sign) - a percentage a hospital
    #    dropped straight into the methodology column. Only (0, 100] is an
    #    unambiguous percent; 0 / negatives / > 100 fall through to 'other'.
    if _BARE_NUMBER_RE.match(cleaned):
        try:
            value = float(cleaned)
        except ValueError:
            value = None
        if value is not None and 0 < value <= 100:
            return 'percent_of_charge'

    # 4. Pattern-based fallback
    if 'percent' in lower or '% of' in lower:
        return 'percent_of_charge'
    if 'fee' in lower and 'schedule' in lower:
        return 'fee_schedule'
    if 'case' in lower and 'rate' in lower:
        return 'case_rate'
    if 'per' in lower and 'diem' in lower:
        return 'per_diem'

    # 5. Unknown methodology - classify as 'other'
    return 'other'


def hoist_numeric_methodology(
    methodology: Optional[str],
    methodology_type: Optional[str],
    negotiated_rate: Optional[float],
    negotiated_percentage: Optional[float],
    negotiated_algorithm: Optional[str],
):
    """Recover a bare-number methodology into the rate columns.

    A scrambled-column MRF vendor drops the negotiated value straight into the
    methodology column. Recover the common, unambiguous cases here so the value
    lands in the right column and the junk label is dropped:

      * bare "0"  -> drop the label. 0 is never a methodology or a real price;
                     any existing rate/percentage on the row is left untouched.
      * bare number in [1, 100] on a row with no other price signal
                  -> hoist it into negotiated_percentage, set methodology_type
                     to percent_of_charge, and drop the label. Guarded on
                     rate/percentage/algorithm all being NULL so we never
                     fabricate a percentage on a row that already has a price.

    Ambiguous cases are left INTACT for a human to resolve with the item's
    context:
      * value < 1    -- fraction form (0.5 == 50%) vs a literal 0.5%
      * value > 100  -- flat dollar amount vs percent-of-benchmark (150% of Medicare)

    Returns the (possibly updated) tuple
    (methodology, methodology_type, negotiated_percentage).
    """
    if methodology is None:
        return methodology, methodology_type, negotiated_percentage
    stripped = methodology.strip()
    if not _BARE_NUMBER_RE.match(stripped):
        return methodology, methodology_type, negotiated_percentage
    try:
        value = float(stripped)
    except ValueError:
        return methodology, methodology_type, negotiated_percentage

    # Junk "0" - drop the label; nothing to recover.
    if value == 0:
        return None, methodology_type, negotiated_percentage

    # Clean percentage dropped into the methodology column - recover it, but
    # only on a row with no competing price signal (conflicts go to review).
    if (1.0 <= value <= 100.0
            and negotiated_rate is None
            and negotiated_percentage is None
            and negotiated_algorithm is None):
        return None, 'percent_of_charge', value

    # < 1 and > 100 are ambiguous: leave the label for human review.
    return methodology, methodology_type, negotiated_percentage



# Keep the methodology text without the noise, and without discarding
# salvageable data. The column is a free-text dumping ground, but most of what
# lands there is real: hospitals routinely put the negotiated price or
# percentage straight into the methodology field ("48.41", "18350.15"), which
# is recoverable data, so we KEEP it. What we drop is the one genuinely
# non-methodology, unbounded vector: the per-claim audit notes one source
# stuffs into the column ("Re-evaluated: HLB.<claim-id>; Using historical
# claim(s); ..."), each unique via its embedded claim id, plus URLs. Neither is
# a methodology or a price. The canonical methodology_type is derived from the
# ORIGINAL value before this gate, so classification/pricing is unaffected.
_METHODOLOGY_JUNK_RE = re.compile(r're-evaluated|historical claim|https?://', re.IGNORECASE)


def _clean_methodology(value: Optional[str]) -> Optional[str]:
    """Drop only per-claim audit notes and URLs; keep the rest.

    Numeric values (prices/percentages hospitals put in the methodology field)
    are kept: they are salvageable data that belongs in the rate columns. Returns None for None / blank and for note/URL strings; otherwise
    the value unchanged. The canonical methodology_type is derived by the caller
    before this runs and is unaffected.
    """
    if value is None:
        return None
    if not value.strip():
        return None
    if _METHODOLOGY_JUNK_RE.search(value):
        return None
    return value



def normalize_ein(raw: Optional[str]) -> Optional[str]:
    """
    Normalize a hospital EIN (Employer Identification Number).
    
    Strips dashes and surrounding whitespace so both ``68-0396600`` and
    ``680396600`` become the same compact form (``680396600``).  Returns
    *None* for blank / whitespace-only input; otherwise returns the
    cleaned string without performing length or digit validation.
    """
    if not raw:
        return None
    cleaned = raw.strip().replace('-', '')
    return cleaned if cleaned else None



def parse_filename_metadata(filename: str) -> Dict[str, Optional[str]]:
    """
    Extract EIN and NPI from standardized MRF filenames.
    
    Supported patterns:
      {EIN}-{NPI}_{name}_standardcharges.{ext}     (e.g., 940562680-1295181477_alta-bates...csv)
      {EIN-WITH-DASH}-{NPI}_{name}_standard...     (e.g., 68-0396600-1801821376_John-Muir...zip)
      {EIN}_{name}_standardcharges.{ext}            (e.g., 946174066_stanford-health...json)
    
    Returns dict with 'ein' and 'npi' (may be None).
    
    NOTE: The second number in the filename is the NPI (National Provider
    Identifier, always 10 digits), NOT the CMS Certification Number.  The CMS
    cert lives inside the file.  Treating this number as the CMS cert makes
    the same hospital look like two different ones.
    """
    # Strip extensions (.csv, .json, .csv.gz, .zip, etc.)
    base = filename
    # Strip the `subset_` prefix carried by test subsets of real files
    if base.startswith('subset_'):
        base = base[len('subset_'):]
    for ext in ('.csv.gz', '.json.gz', '.csv', '.json', '.zip'):
        if base.lower().endswith(ext):
            base = base[:-len(ext)]
            break
    
    # Split on underscore - first segment contains EIN (and possibly NPI)
    parts = base.split('_', 1)
    if not parts:
        return {'ein': None, 'npi': None}
    
    id_part = parts[0]  # e.g., "940562680-1295181477" or "68-0396600-1801821376" or "946174066"
    
    # Try to extract EIN and NPI from the id_part
    # EIN formats: "940562680" (9 digits) or "68-0396600" (XX-XXXXXXX)
    ein = None
    npi = None
    
    # Pattern: digits-only with possible embedded dash for EIN format
    # Split on hyphens and reconstruct
    segments = id_part.split('-')
    
    if len(segments) == 1:
        # Just EIN, no NPI: "946174066"
        # Validate: EIN must be 9 digits (compact format)
        if segments[0].isdigit() and len(segments[0]) == 9:
            ein = segments[0]
    elif len(segments) == 2:
        # Could be "EIN-NPI" (940562680-1295181477) or "EIN_PREFIX-EIN_SUFFIX" (68-0396600)
        # If second part is 7 digits and first is 2 digits, it's XX-XXXXXXX EIN format
        if len(segments[0]) == 2 and segments[0].isdigit() and len(segments[1]) == 7 and segments[1].isdigit():
            # EIN in XX-XXXXXXX format, no NPI
            ein = f"{segments[0]}-{segments[1]}"
        elif segments[0].isdigit() and len(segments[0]) == 9:
            # EIN-NPI: "940562680-1295181477"
            ein = segments[0]
            if segments[1].isdigit():
                npi = segments[1]
    elif len(segments) == 3:
        # "68-0396600-1801821376" -> EIN = "68-0396600", NPI = "1801821376"
        if len(segments[0]) == 2 and segments[0].isdigit() and segments[1].isdigit():
            ein = f"{segments[0]}-{segments[1]}"
            if segments[2].isdigit():
                npi = segments[2]
        elif segments[0].isdigit() and len(segments[0]) == 9:
            ein = segments[0]
            if segments[2].isdigit():
                npi = segments[2]
    
    return {
        'ein': normalize_ein(ein) if ein else None,
        'npi': npi if npi else None,
    }


# ============================================================================
# ADDRESS PARSING -- extract city / state from raw address strings
# ============================================================================

# US state abbreviations (two-letter)
_US_STATES = {
    'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN',
    'IA','KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV',
    'NH','NJ','NM','NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN',
    'TX','UT','VT','VA','WA','WV','WI','WY','DC','PR','VI','GU','AS','MP',
}

# Full state name → 2-letter abbreviation (upper-case keys for case-insensitive match)
_STATE_NAME_TO_ABBR = {
    'ALABAMA': 'AL', 'ALASKA': 'AK', 'ARIZONA': 'AZ', 'ARKANSAS': 'AR',
    'CALIFORNIA': 'CA', 'COLORADO': 'CO', 'CONNECTICUT': 'CT', 'DELAWARE': 'DE',
    'FLORIDA': 'FL', 'GEORGIA': 'GA', 'HAWAII': 'HI', 'IDAHO': 'ID',
    'ILLINOIS': 'IL', 'INDIANA': 'IN', 'IOWA': 'IA', 'KANSAS': 'KS',
    'KENTUCKY': 'KY', 'LOUISIANA': 'LA', 'MAINE': 'ME', 'MARYLAND': 'MD',
    'MASSACHUSETTS': 'MA', 'MICHIGAN': 'MI', 'MINNESOTA': 'MN',
    'MISSISSIPPI': 'MS', 'MISSOURI': 'MO', 'MONTANA': 'MT', 'NEBRASKA': 'NE',
    'NEVADA': 'NV', 'NEW HAMPSHIRE': 'NH', 'NEW JERSEY': 'NJ',
    'NEW MEXICO': 'NM', 'NEW YORK': 'NY', 'NORTH CAROLINA': 'NC',
    'NORTH DAKOTA': 'ND', 'OHIO': 'OH', 'OKLAHOMA': 'OK', 'OREGON': 'OR',
    'PENNSYLVANIA': 'PA', 'RHODE ISLAND': 'RI', 'SOUTH CAROLINA': 'SC',
    'SOUTH DAKOTA': 'SD', 'TENNESSEE': 'TN', 'TEXAS': 'TX', 'UTAH': 'UT',
    'VERMONT': 'VT', 'VIRGINIA': 'VA', 'WASHINGTON': 'WA',
    'WEST VIRGINIA': 'WV', 'WISCONSIN': 'WI', 'WYOMING': 'WY',
    'DISTRICT OF COLUMBIA': 'DC', 'PUERTO RICO': 'PR',
    'VIRGIN ISLANDS': 'VI', 'GUAM': 'GU', 'AMERICAN SAMOA': 'AS',
    'NORTHERN MARIANA ISLANDS': 'MP',
}

# Pattern: STATE ZIP at end, with optional preceding comma/space
_STATE_ZIP_RE = re.compile(
    r'[,\s]+([A-Z]{2})[,\s]+(\d{5}(?:-\d{4})?)$'
)

# Fallback: STATE at end without ZIP
_STATE_ONLY_RE = re.compile(
    r'[,\s]+([A-Z]{2})$'
)

# Fallback: full state name + ZIP at end  (e.g. ", CALIFORNIA 94110-3518")
_FULL_STATE_ZIP_RE = re.compile(
    r'[,\s]+([A-Z][A-Z ]+?)\s+(\d{5}(?:-\d{4})?)$'
)

# Fallback: ZIP before 2-letter state at end (e.g. "MODESTO 95356 CA")
_ZIP_STATE_RE = re.compile(
    r'\s+(\d{5}(?:-\d{4})?)\s+([A-Z]{2})$'
)

# Inverted, pipe-delimited form where the WHOLE left segment is just a
# "STATE ZIP" prefix, e.g. "MS 38827 | 351 Peoples Dr Pontotoc".  Matched against
# the left side of the first '|' only (anchored ^...$), so a normal address can
# never trip it.
_LEADING_STATE_ZIP_RE = re.compile(
    r'^([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$'
)

def parse_address(raw: Optional[str]) -> Dict[str, Optional[str]]:
    """
    Parse a raw address string into components.
    Returns dict with keys: street, city, state, zip.
    All values may be None if parsing fails.

    Handles common US address formats:
      - "170 Alameda de las Pulgas, Redwood City, CA 94062"
      - "250 Bon Air Rd,Greenbrae,CA,94904"  (no spaces after commas)
      - "505 Parnassus Ave, Box 0296, San Francisco, CA 94143"
      - "2000 Mowry Ave Fremont CA 94538"  (no commas)
      - "1001 POTRERO AVE, SAN FRANCISCO, CALIFORNIA 94110-3518"  (full state name)
      - "4601 DALE RD MODESTO 95356 CA"  (ZIP before state, no commas)
    """
    result = {'street': None, 'city': None, 'state': None, 'zip': None}
    if not raw or not raw.strip():
        return result

    addr = raw.strip()
    # Normalize: strip outer quotes
    if addr.startswith('"') and addr.endswith('"'):
        addr = addr[1:-1].strip()

    # Some multi-hospital system files emit an inverted, pipe-delimited
    # address where a "STATE ZIP" prefix precedes the street/city,
    # e.g. "MS 38827 | 351 Peoples Dr Pontotoc".  Left as-is the whole string
    # falls through to the raw-as-street fallback.  If the
    # segment before the first '|' is EXACTLY a state + ZIP, move it to the end
    # ("351 Peoples Dr Pontotoc, MS 38827") so the standard parsing below recovers
    # street/city/state/zip.  The ^...$ anchor means a normal address (which never
    # has a bare "ST ZIP" before a pipe) can't trip this.
    if '|' in addr:
        left, _, right = addr.partition('|')
        right = right.strip()
        lm = _LEADING_STATE_ZIP_RE.match(left.strip().upper())
        if lm and lm.group(1) in _US_STATES and right:
            addr = f"{right}, {lm.group(1)} {lm.group(2)}"

    upper = addr.upper()

    # Try matching STATE ZIP at the end
    m = _STATE_ZIP_RE.search(upper)
    if m and m.group(1) in _US_STATES:
        state = m.group(1)
        zipcode = m.group(2)
        before = addr[:m.start()].strip().rstrip(',').strip()
        result['state'] = state
        result['zip'] = zipcode

        # Split remaining into street + city on last comma
        # e.g. "170 Alameda de las Pulgas, Redwood City"
        parts = [p.strip() for p in before.rsplit(',', 1)]
        if len(parts) == 2:
            result['street'] = parts[0]
            result['city'] = parts[1]
        elif len(parts) == 1 and parts[0]:
            # No comma - try splitting on last space-delimited word as city
            # "2000 Mowry Ave Fremont" - not reliable, store as street only
            result['street'] = parts[0]
        return result

    # Fallback: full state name + ZIP at end
    # e.g. "1001 POTRERO AVE, SAN FRANCISCO, CALIFORNIA 94110-3518"
    m = _FULL_STATE_ZIP_RE.search(upper)
    if m:
        state_name = m.group(1).strip()
        abbr = _STATE_NAME_TO_ABBR.get(state_name)
        if abbr:
            zipcode = m.group(2)
            before = addr[:m.start()].strip().rstrip(',').strip()
            result['state'] = abbr
            result['zip'] = zipcode
            parts = [p.strip() for p in before.rsplit(',', 1)]
            if len(parts) == 2:
                result['street'] = parts[0]
                result['city'] = parts[1]
            elif len(parts) == 1 and parts[0]:
                result['street'] = parts[0]
            return result

    # Fallback: ZIP before 2-letter state at end (no commas)
    # e.g. "4601 DALE RD MODESTO 95356 CA"
    # Must come before STATE_ONLY to avoid matching just the trailing "CA"
    m = _ZIP_STATE_RE.search(upper)
    if m and m.group(2) in _US_STATES:
        zipcode = m.group(1)
        state = m.group(2)
        before = addr[:m.start()].strip().rstrip(',').strip()
        result['state'] = state
        result['zip'] = zipcode
        # No commas - store remainder as street (city not reliably extractable)
        if before:
            result['street'] = before
        return result

    # Fallback: STATE only at end (no ZIP)
    m = _STATE_ONLY_RE.search(upper)
    if m and m.group(1) in _US_STATES:
        state = m.group(1)
        before = addr[:m.start()].strip().rstrip(',').strip()
        result['state'] = state
        parts = [p.strip() for p in before.rsplit(',', 1)]
        if len(parts) == 2:
            result['street'] = parts[0]
            result['city'] = parts[1]
        elif len(parts) == 1 and parts[0]:
            result['street'] = parts[0]
        return result

    # Could not parse - return raw as street
    result['street'] = addr
    return result




# ============================================================================
# UTILITIES
# ============================================================================

def _safe_float(value) -> Optional[float]:
    """Safely convert string or number to float, return None if invalid."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return None
    try:
        # Remove common currency symbols and commas
        clean = str(value).replace('$', '').replace(',', '').strip()
        return float(clean)
    except (ValueError, TypeError):
        return None


def _safe_count(value) -> Optional[int]:
    """Safely convert a count value to int, returning None if invalid.

    Handles plain integers, floats, and CMS privacy-obfuscated ranges
    like "1 through 10" by taking the upper bound.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    s = str(value).strip()
    if not s:
        return None
    # Plain numeric
    if s.replace('.', '', 1).replace('-', '', 1).isdigit():
        return int(float(s))
    # Range pattern: "1 through 10", "1-10", "1 to 10"
    import re
    m = re.search(r'(\d+)\s*(?:through|thru|to|-)\s*(\d+)', s, re.IGNORECASE)
    if m:
        return int(m.group(2))  # upper bound
    # Try to extract any trailing number
    m = re.search(r'(\d+)\s*$', s)
    if m:
        return int(m.group(1))
    return None


def _sanitize_npi(raw: Optional[str]) -> Optional[str]:
    """Extract the first valid 10-digit NPI from a raw string.

    Hospital MRF files sometimes store multiple pipe-separated NPIs in the
    type_2_npi preamble field (e.g. ``1023187416| 1124017140| 1154415024``).
    An NPI is exactly 10 digits, so the raw multi-value string is not one.

    Returns the first 10-digit value found, or None.
    """
    if not raw:
        return None
    # Split on pipe (with optional surrounding whitespace)
    for part in raw.split('|'):
        # Strip whitespace, quotes, and other non-digit noise that
        # may wrap an otherwise valid NPI (e.g. '"1023187416"').
        digits_only = ''.join(ch for ch in part if ch.isdigit())
        if len(digits_only) == 10:
            return digits_only
    return None


_SIMPLE_WIDE_PEEK_ROWS = 50
# Fraction of non-empty values that must look like dollar amounts
_SIMPLE_WIDE_THRESHOLD = 0.5


# Textual "no rate on this payer/item" placeholders.  In a wide-payer file
# these appear in payer-rate columns interchangeably with $- / blank / 0 and
# carry no analytical value.  Treated as *missing* (not as text) by the simple
# wide-format payer detector, and should be kept out of unmapped cells so a
# payer column that is mostly `N/A` cannot masquerade as an unmappable text
# column (see _looks_like_dollar_value).  '\u2014' is the em dash character.
_NULL_RATE_TOKENS = frozenset({
    'n/a', 'n/a.', 'na', '#n/a', 'null', 'none', 'nil', '-', '--', '\u2014',
})


def _is_null_rate_token(v: str) -> bool:
    """Return True if *v* is a textual null-rate placeholder (e.g. ``N/A``)."""
    return v.strip().lower() in _NULL_RATE_TOKENS


def _looks_like_dollar_value(v: str) -> bool:
    """Check if a value looks like a dollar amount, 'Packaged', or null-rate indicator.

    Matches patterns found in simple wide-format hospital MRF files:
      $5, $123.45, $10,002, $-, $0.00, Packaged, 123.45, 0
    """
    if not v:
        return False
    low = v.lower()
    if low == 'packaged':
        return True
    s = v.lstrip('$').replace(',', '').strip()
    if not s or s == '-':
        return True   # $- is a null-rate indicator
    try:
        float(s)
        return True
    except ValueError:
        return False


def _column_looks_like_payer_rates(values) -> bool:
    """Return True if a sampled column looks like a wide-payer rate column.

    Blanks and textual null-rate placeholders (``N/A``, ``null``, ``-`` ...)
    are treated as *missing* - the column is judged only on the fraction of its
    meaningful values that look like dollar amounts.  This is what lets a payer
    column that is mostly ``N/A`` with a minority of real dollar amounts still
    be detected; without it every cell (``N/A`` included) floods the unmapped
    cells and the real negotiated rates are dropped entirely.

    ``values`` is an iterable of raw cell strings (or ``None``) sampled from the
    column's data rows.
    """
    non_empty = [s for s in ((v or '').strip() for v in values)
                 if s and not _is_null_rate_token(s)]
    if not non_empty:
        return False
    matching = sum(1 for v in non_empty if _looks_like_dollar_value(v))
    return matching / len(non_empty) >= _SIMPLE_WIDE_THRESHOLD

