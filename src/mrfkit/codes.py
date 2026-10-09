"""Normalize billing codes and code types.

Hospitals label the same code system many ways ("CPT4", "HCPCS/CPT", "REV",
"AP-DRG"), file standard codes under their own chargemaster buckets, wrap
codes in prefixes ("HCPCS C1776", "DRG100") and bake modifiers into the code
("73721TC"). ``normalize_code`` sorts all of that out; the helpers around it
reject rows that are too broken to keep and infer the billing class when the
file does not say.

Nothing here touches a database. A few guards get sharper when you pass a
``ReferenceData`` with published code lists; without one they fall back to the
shape-only rules.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Dict, Optional, Tuple

if TYPE_CHECKING:
    from mrfkit.reference import ParseStats, ReferenceData

# ============================================================================
# CODE & CODE_TYPE NORMALIZATION
# ============================================================================
# Standard US medical code systems and their formats:
#   CPT   - 5 digits (Level I HCPCS), e.g. 99213, 27447
#   HCPCS - letter + 4 digits (Level II HCPCS), e.g. J0690, C1776, A4550
#           Also includes Category III: 5 digits + 'T', e.g. 0237T
#   CDT   - Current Dental Terminology, D + 4 digits, e.g. D0120, D7509
#   RC    - Revenue Code, 4 digits zero-padded (UB-04), e.g. 0360, 0750
#   MS-DRG - 3 digits, e.g. 469, 470
#   APC   - Ambulatory Payment Classification, 4 digits or 'N'+3 digits
#   NDC   - National Drug Code, 10-11 digits with dashes, e.g. 12345-6789-01
#   CDM   - Charge Description Master (hospital-internal), non-standard formats
#   LOCAL - Hospital-internal codes that don't match any standard system

# code_type synonyms: normalize various source labels to canonical types
_CODE_TYPE_NORMALIZE = {
    'CPT': 'CPT',
    'CPT4': 'CPT',
    'CPT-4': 'CPT',
    'HCPCS': 'HCPCS',
    'HCPCS LEVEL II': 'HCPCS',
    'HCPCS II': 'HCPCS',
    'HCPCS2': 'HCPCS',
    # Hedge labels used by hospitals that don't know whether a code is
    # CPT (Level I) or HCPCS (Level II).  Normalize to HCPCS; Step 5 will
    # reclassify 5-digit numeric codes to CPT based on shape.
    'CPT/HCPCS': 'HCPCS',
    'HCPCS/CPT': 'HCPCS',
    'CPT / HCPCS': 'HCPCS',
    'HCPCS / CPT': 'HCPCS',
    'CPT-HCPCS': 'HCPCS',
    'HCPCS-CPT': 'HCPCS',
    'CPTHCPCS': 'HCPCS',
    'HCPCSCPT': 'HCPCS',
    'RC': 'RC',
    'REV': 'RC',
    'REVENUE': 'RC',
    'REVENUE CODE': 'RC',
    'REVCODE': 'RC',
    'MS-DRG': 'MS-DRG',
    'MSDRG': 'MS-DRG',
    'DRG': 'MS-DRG',
    'TRIS-DRG': 'MS-DRG',  # Tenet Revenue Integrity System - same codes as MS-DRG
    'APR-DRG': 'APR-DRG',  # All Patient Refined DRG (3M) - distinct from MS-DRG
    'APRDRG': 'APR-DRG',
    'APR DRG': 'APR-DRG',
    'APR_DRG': 'APR-DRG',
    'AP-DRG': 'APR-DRG',   # All Patient DRG variant: same code set
    'APC': 'APC',
    'CMG': 'CMG',   # Case Mix Group (CMS inpatient rehab grouper)
    'NDC': 'NDC',
    'CDT': 'CDT',
    'DENTAL': 'CDT',
    'CDM': 'CDM',
    'CHARGEMASTER': 'CDM',
    'LOCAL': 'LOCAL',
    # Partners Healthcare internal types
    'SUP': 'CDM',    # supplies
    'EAP': 'CDM',    # enterprise-assigned procedure
    'ERX': 'CDM',    # enterprise pharmacy
    # Compound NDC+system types: disambiguated in normalize_code step 2A
    'NDCHCPCS': 'NDCHCPCS',
    'NDCCPT': 'NDCCPT',
}

# Non-system junk code_type labels that some hospitals emit.  When
# the canonicalized code_type is one of these we discard it (treat as missing)
# and re-derive from code shape, exactly as for a blank code_type.
_JUNK_CODE_TYPES = frozenset({'TYPE', 'MODIFIER', 'MODIFIERS', 'OBS', 'DOT', 'UN'})

# Canonical validity regexes for controlled-vocabulary numeric types.
# Applied in two places: cross-system reclassify (step 6A) and residue gating
# (step 12).  APR-DRG base-severity form (775-1) and CMG group-tier form
# (1902-D) are both canonical - do NOT gate them.
_RE_VALID_RC = re.compile(r'^\d{3,4}$')
_RE_VALID_MS_DRG = re.compile(r'^\d{1,3}$')
_RE_VALID_APR_DRG = re.compile(r'^\d{1,4}(-\d{1,2})?$')
_RE_VALID_APC = re.compile(r'^(?:\d{4,5}|[Nn]\d{3,4})$')
_RE_VALID_CMG = re.compile(r'^\d{4}(-[A-Z])?$')
_RE_VALID_EAPG = re.compile(r'^\d{3,5}$')

_CONTROLLED_VOCAB_VALIDATORS = {
    'RC': _RE_VALID_RC,
    'MS-DRG': _RE_VALID_MS_DRG,
    'APR-DRG': _RE_VALID_APR_DRG,
    'APC': _RE_VALID_APC,
    'CMG': _RE_VALID_CMG,
    'EAPG': _RE_VALID_EAPG,
}

# Revenue codes are 1-4 digit numbers in the range 0001-0999.
# Some hospitals report them under code_type='HCPCS' using the
# 3-digit UB-04 revenue center number (e.g., 272 for sterile supply).
_KNOWN_REVENUE_CODES = {
    # Commonly misclassified as HCPCS
    '250', '0250', '255', '0255', '258', '0258',   # Pharmacy
    '270', '0270', '271', '0271', '272', '0272',    # Supplies
    '275', '0275', '276', '0276', '278', '0278',      # Implants, supplies
    '300', '0300', '301', '0301', '302', '0302',    # Lab
    '305', '0305', '306', '0306', '309', '0309',
    '310', '0310', '311', '0311', '312', '0312',
    '320', '0320', '323', '0323', '333', '0333',    # Radiology
    '335', '0335', '341', '0341', '342', '0342',
    '343', '0343', '350', '0350', '351', '0351',
    '352', '0352', '360', '0360', '361', '0361',    # OR
    '370', '0370', '390', '0390',                    # Anesthesia
    '402', '0402', '403', '0403', '404', '0404',    # Other imaging
    '410', '0410', '412', '0412', '420', '0420',    # Physical therapy
    '424', '0424', '430', '0430', '440', '0440',
    '450', '0450', '460', '0460', '470', '0470',    # ER, ambulance
    '471', '0471', '480', '0480', '481', '0481',
    '483', '0483', '489', '0489', '510', '0510',
    '521', '0521', '610', '0610', '611', '0611',
    '615', '0615', '618', '0618', '636', '0636',    # Pharmacy IV
    '637', '0637', '710', '0710', '721', '0721',
    '722', '0722', '730', '0730', '731', '0731',
    '740', '0740', '750', '0750', '760', '0760',
    '812', '0812', '906', '0906', '916', '0916',
    '918', '0918', '920', '0920', '921', '0921',
    '922', '0922', '940', '0940', '942', '0942',
    '949', '0949', '987', '0987', '998', '0998',
}


def _strip_non_ascii(s: str) -> str:
    """Remove non-ASCII characters (mojibake, control chars) from a string."""
    return ''.join(c for c in s if ord(c) < 128)


# Regex patterns for valid code formats
_RE_CPT = re.compile(r'\d{5}')              # 5 digits: 99213
_RE_CPT_PLA = re.compile(r'\d{4}[A-Z]')     # PLA codes: 0202U, 0003M
_RE_HCPCS = re.compile(r'[A-Z]\d{4}')       # letter + 4 digits: J0591
_RE_CAT3 = re.compile(r'\d{4}T')            # Category III: 0237T

# ── NDC normalization ─────────────────────────────────────────────────
# Produces canonical 11-digit no-dash NDCs directly.
_RE_NDC_LEADING_ALPHA = re.compile(r'^[A-Za-z]')
_RE_NDC_CANON_11 = re.compile(r'^\d{11}$')
_RE_NDC_NINE_DIGIT = re.compile(r'^\d{9}$')
# Bare dash-less 10-digit NDC10 → NDC11 via leading '0'.
_RE_NDC_TEN_DIGIT = re.compile(r'^\d{10}$')
_RE_NDC_SETTING_SUFFIX = re.compile(
    r'^(\d{10,11})_(?:ip|op)$', re.IGNORECASE
)
_RE_NDC_TRAILING_LETTER = re.compile(r'^(\d{10,11})[A-Za-z]$')
_RE_NDC_PKG_DIGIT_SUFFIX = re.compile(r'^(\d{10,11})_\d+$')
# Package + setting double suffix, e.g. "39822105505_4_ip".
_RE_NDC_PKG_SETTING_SUFFIX = re.compile(
    r'^(\d{10,11})_\d+_(?:ip|op)$', re.IGNORECASE
)
# 3-segment dashed NDC, optionally followed by an alpha packaging suffix
# (e.g. "RL1", "OSPT") or "-<digits>" extra tail (e.g. "-50"). Used for
# both shape detection and canonicalization.
_RE_NDC_3SEG = re.compile(
    r'^(\d{4,5})-(\d{3,5})-(\d{1,2})(?:[A-Za-z][A-Za-z0-9]*|-\d+)?$'
)


def _ndc_canonicalize_base(base: str) -> Optional[str]:
    """10/11-digit numeric base → canonical 11-digit string. None if neither."""
    n = len(base)
    if n == 11:
        return base
    if n == 10:
        return '0' + base
    return None


def _normalize_ndc(
    code: Optional[str],
) -> Tuple[Optional[str], str]:
    """Normalize an NDC code to canonical 11-digit no-dash form.

    Returns ``(canonical_code, code_type)`` where ``code_type`` is either
    ``'NDC'`` (default) or ``'LOCAL'`` (when the value starts with an
    ASCII letter: those are charge-master codes, not NDCs).

    Rules, first match wins:

    1. ``None`` → ``(None, 'NDC')``.
    2. Empty after strip → ``(raw, 'NDC')``.
    3. Leading ASCII letter → ``(raw, 'LOCAL')``.
    4. Already canonical 11-digit → ``(raw, 'NDC')``.
    5. 9-digit all-numeric → ``('00' + raw, 'NDC')``.
    6. Bare 10-digit all-numeric → ``('0' + raw, 'NDC')``.
    7. 10/11-digit base + ``_ip``/``_op`` setting suffix → canonical base.
    8. 10/11-digit base + single trailing ASCII letter → canonical base.
    9. 10/11-digit base + ``_<digits>`` package suffix → canonical base.
    10. 10/11-digit base + ``_<digits>_<ip|op>`` package+setting double
        suffix → canonical base.
    11. 3-segment dashed shape (with optional alpha/dash packaging
        suffix): strip non-digits; 11 or 10 digits canonicalize, else
        leave as-is.
    12. Anything else → ``(raw, 'NDC')`` unchanged.
    """
    if code is None:
        return None, 'NDC'
    s = code.strip()
    if not s:
        return code, 'NDC'

    # 3. Leading-letter codes are local charge-master codes, not NDCs.
    if _RE_NDC_LEADING_ALPHA.match(s):
        return code, 'LOCAL'

    # 4. Already canonical.
    if _RE_NDC_CANON_11.match(s):
        return s, 'NDC'

    # 5. 9-digit pad with '00'.
    if _RE_NDC_NINE_DIGIT.match(s):
        return '00' + s, 'NDC'

    # 6. Bare 10-digit pad with '0'.
    if _RE_NDC_TEN_DIGIT.match(s):
        return '0' + s, 'NDC'

    # 7-10. 10/11-digit base with various trailing suffixes (setting,
    #       letter, package, and package+setting).
    for rx in (
        _RE_NDC_SETTING_SUFFIX,
        _RE_NDC_TRAILING_LETTER,
        _RE_NDC_PKG_DIGIT_SUFFIX,
        _RE_NDC_PKG_SETTING_SUFFIX,
    ):
        m = rx.match(s)
        if m:
            canon = _ndc_canonicalize_base(m.group(1))
            if canon is not None:
                return canon, 'NDC'

    # 9. 3-segment dashed shape with optional packaging suffix
    #    (alpha tail like "RL1" or "-50"). Canonicalize via segment
    #    alignment: seg1→5, seg2→4, seg3→2. The packaging
    #    suffix is intentionally discarded - it is not part of the
    #    canonical NDC product code. Requires seg1≤5, seg2≤4,
    #    seg3∈[1,2] digits so total = 11; otherwise leave alone.
    m = _RE_NDC_3SEG.match(s)
    if m:
        seg1, seg2, seg3 = m.group(1), m.group(2), m.group(3)
        if len(seg1) <= 5 and len(seg2) <= 4 and len(seg3) <= 2:
            canon = seg1.zfill(5) + seg2.zfill(4) + seg3.zfill(2)
            if len(canon) == 11:
                return canon, 'NDC'
        # Doesn't fit canonical layout (e.g. shifted-dash 5-5-1):
        # leave as-is rather than guess.
        return s, 'NDC'

    # 10. Anything else: leave untouched.
    return s, 'NDC'


# Placeholder/test patterns that should be classified as LOCAL
_RE_PLACEHOLDER = re.compile(
    r'^(?:X{3,}|TEST\b|N/?A$|NONE$|TBD$|UNKNOWN$)',
    re.IGNORECASE,
)

# Composite code prefixes: code values like "HCPCS C1776" or "CPT 87339"
# embed the code type as a prefix.  Keys are uppercase prefixes that can
# appear at the start of a code value (followed by a space).
_COMPOSITE_CODE_PREFIXES = {
    'HCPCS', 'CPT', 'CPT4', 'MS-DRG', 'MSDRG', 'DRG',
    'NDC', 'REV', 'RC', 'REVENUE', 'REVENUE CODE', 'REVCODE', 'APC', 'CDM',
    'CDT', 'APR-DRG', 'APRDRG', 'APR DRG', 'APR_DRG', 'CMG',
}

_COMPOSITE_CODE_PREFIXES_BY_LENGTH = sorted(
    _COMPOSITE_CODE_PREFIXES,
    key=len,
    reverse=True,
)

# code_types whose numeric codes may arrive with a spurious trailing
# ".0" due to Excel / ETL float coercion (e.g. "320.0" → "320").  The strip
# is scoped to these numeric controlled-vocabulary types only - ICD codes like
# "250.0" are valid decimal-coded ICD-9 entries and must NOT be altered.
_FLOAT_COERCE_STRIP_TYPES = frozenset({'RC', 'MS-DRG', 'APR-DRG', 'APC', 'CMG', 'EAPG'})


def _valid_prefixed_code_remainder(rest: str, norm_type: str) -> bool:
    """Return whether a composite prefix leaves a plausible code value."""
    s = rest.strip()
    if norm_type == 'CDT':
        return bool(re.fullmatch(r'[Dd]\d{4}', s))
    if norm_type == 'APR-DRG':
        return bool(re.match(r'\d{3}\s*-\s*\d', s) or re.fullmatch(r'\d{3,4}', s))
    if norm_type == 'APC':
        return bool(re.fullmatch(r'(?:\d{3,4}|[Nn]\d{3})', s))
    if norm_type == 'RC':
        return bool(re.fullmatch(r'\d{1,4}', s))
    if norm_type == 'CMG':
        # 4-digit group number, optionally followed by '-' + comorbidity tier letter
        return bool(re.fullmatch(r'\d{4}(-[A-Z])?', s))
    return True

# MS-DRG composite: "MS-DRG V41.0 (FY 2024) 155" - the actual DRG number
# is the last group of 1-3 digits in the string.
_RE_DRG_TRAILING_NUM = re.compile(r'(\d{1,3})\s*$')


def _split_attached_composite_code(
    code: str,
    code_type: Optional[str],
) -> Tuple[str, Optional[str]]:
    """Split tightly attached code-system prefixes at the start of a value.

    This is deliberately narrower than the space-delimited composite splitter:
    the full code value must be just the prefix plus a structurally valid code.
    That lets us recover values like ``DRG100`` while avoiding infix false
    positives such as ``SUP-D224DRG`` or free-text markers like ``NEED CPT``.
    """
    stripped = code.strip()

    rules = (
        ('MS-DRG', r'(?:MS-DRG|MSDRG|DRG)(\d{1,3})'),
        # "MS-012" abbreviated form (MS- + 1-3 digits, no 'DRG' suffix).
        # Distinct from the full "MS-DRG470" pattern above.  Only split when the
        # declared code_type is MS-DRG-family, CDM, LOCAL, or absent - never
        # override a conflicting standard type (same guard as the other rules).
        ('MS-DRG', r'MS-(\d{1,3})'),
        ('APR-DRG', r'(?:APR-DRG|APRDRG)(\d{3}(?:-\d|\d)?)'),
        ('APC', r'APC((?:\d{3,4}|[Nn]\d{3}))'),
        ('RC', r'RC(\d{1,4})'),
        ('NDC', r'NDC(\d{9,11}|\d{4,5}-\d{3,5}-\d{1,2}(?:[A-Za-z][A-Za-z0-9]*|-\d+)?)'),
        ('CPT', r'CPT(\d{5}|\d{4}[FTUftu])'),
        ('HCPCS', r'HCPCS([A-CE-Va-ce-v]\d{4})'),
        ('CDT', r'CDT([Dd]\d{4})'),
        # "CMG-1902-D" dash-attached form: CMG + dash + 4-digit group
        # + optional comorbidity tier letter.  The space form ("CMG 1902-D") is
        # handled by the space-prefix splitter; this rule covers the dash form.
        ('CMG', r'CMG-(\d{4}(?:-[A-Z])?)'),
    )

    for prefix_type, pattern in rules:
        m = re.fullmatch(pattern, stripped, flags=re.IGNORECASE)
        if not m:
            continue

        if code_type:
            norm_ct = _CODE_TYPE_NORMALIZE.get(code_type.strip().upper())
            norm_prefix = _CODE_TYPE_NORMALIZE.get(prefix_type, prefix_type)
            if norm_ct and norm_ct not in ('CDM', 'LOCAL', norm_prefix):
                return code, code_type

        return m.group(1), prefix_type

    return code, code_type


def _split_composite_code(
    code: str,
    code_type: Optional[str],
) -> Tuple[str, Optional[str]]:
    """
    Split composite code values where code_type is embedded as a prefix.

    Examples:
        "HCPCS C1776"  → code="C1776", code_type="HCPCS"
        "CPT 87339"    → code="87339", code_type="CPT"
        "MS-DRG V41.0 (FY 2024) 155" → code="155", code_type="MS-DRG"
        "HCPCS 25009999" → code="25009999", code_type="HCPCS"

    Only splits when:
    - The code contains a space
    - The part before the first space is a known code type prefix
    - There is no separately declared code_type, OR the declared code_type
      is a non-standard value (not in _CODE_TYPE_NORMALIZE)

    Returns (code, code_type) - possibly unchanged.
    """
    stripped = code.strip()
    if ' ' not in stripped:
        return _split_attached_composite_code(code, code_type)

    def _match_composite_prefix(value: str) -> Tuple[Optional[str], Optional[str]]:
        upper_value = value.upper()
        for known_prefix in _COMPOSITE_CODE_PREFIXES_BY_LENGTH:
            if upper_value.startswith(known_prefix + ' '):
                prefix_text = value[:len(known_prefix)].upper().rstrip('®')
                rest_text = value[len(known_prefix) + 1:].strip()
                return prefix_text, rest_text
        return None, None

    # Don't override a valid, recognized code_type - with exceptions
    if code_type:
        ct_upper = code_type.strip().upper()
        norm_ct = _CODE_TYPE_NORMALIZE.get(ct_upper)
        if norm_ct:
            prefix, rest = _match_composite_prefix(stripped)
            if not prefix:
                first_space = stripped.index(' ')
                prefix = stripped[:first_space].upper().rstrip('®')
                rest = stripped[first_space + 1:].strip()
            # If code_type is hospital-internal (CDM, LOCAL) but the code
            # starts with a standard code type prefix, prefer the standard type
            if norm_ct in ('CDM', 'LOCAL') and prefix in _COMPOSITE_CODE_PREFIXES:
                pass  # fall through to split logic below
            # If the prefix matches the declared code_type (redundant), strip it
            elif prefix == ct_upper or _CODE_TYPE_NORMALIZE.get(prefix) == norm_ct:
                if rest:
                    if not _valid_prefixed_code_remainder(rest, norm_ct):
                        return code, code_type
                    # For DRG-family types, extract the trailing DRG number
                    if norm_ct == 'MS-DRG':
                        m = _RE_DRG_TRAILING_NUM.search(rest)
                        if m:
                            return m.group(1), code_type
                        return code, code_type
                    return rest, code_type
                return code, code_type
            else:
                # Code type is standard (CPT, HCPCS, etc.) and prefix doesn't
                # match - don't split (the space is part of the code/description)
                return code, code_type

    prefix, rest = _match_composite_prefix(stripped)
    if not prefix:
        return code, code_type

    if not rest:
        return code, code_type

    # For DRG-family prefixes, the actual DRG number is the trailing digits
    # (e.g. "V41.0 (FY 2024) 155" → "155")
    norm_prefix = _CODE_TYPE_NORMALIZE.get(prefix, prefix)
    if not _valid_prefixed_code_remainder(rest, norm_prefix):
        return code, code_type
    if norm_prefix == 'MS-DRG':
        m = _RE_DRG_TRAILING_NUM.search(rest)
        if m:
            return m.group(1), prefix
        return code, code_type

    return rest, prefix


# ── Split modifiers baked into the code field ──────────────────────────
#
# Hospitals sometimes fuse a modifier onto the procedure code in their
# MRF: `73721TC`, `87077QW`, `36415CP`, `82274SC`. These don't parse as a
# valid 5-char CPT, so they land under `code_type='CDM'` or `'LOCAL'` and
# the modifier is trapped in the code string.
#
# The helper takes the known-modifier set as a parameter so its rules can
# be tested on their own; `apply_baked_modifier_split` below wires in the
# curated set.

# Allowed prefix shapes the splitter recognizes - must be structurally
# valid CPT or HCPCS Level II. Anything else (ICD-10-PCS 7-char, NDC,
# legitimate 6-char numerics) MUST NOT be touched.
#
# Why these patterns specifically:
#   * `\d{5}`    - plain CPT (73721, 87077, 36415, 82274)
#   * `\d{4}[FTU]` - CPT Cat-II/III/PLA (`0001F`, `0202U`, `0237T`).
#     Currently the trailing letter is part of the code, not a modifier;
#     but a baked-modifier composite like `0001FTC` is theoretically
#     possible - exclude for now to avoid ambiguity.
#   * `[A-CE-V]\d{4}` - HCPCS Level II (J0591, A4253). Excludes D
#     (CDT - different code system) and W/X/Y/Z (not assigned).
#
# Modifier suffix shape: `[A-Z0-9]{2}` matches the 2-char atomic-modifier
# format that covers every curated atomic modifier (TC, SG, QW, LT, RT,
# JW, JZ, 22, 26, 50, etc.). The actual must-be-a-known-modifier check is
# the caller's responsibility: that's what guards against false-positive
# splits.
_RE_BAKED_MODIFIER_CPT5 = re.compile(r'^(\d{5})([A-Z0-9]{2})$')
_RE_BAKED_MODIFIER_HCPCS = re.compile(r'^([A-CE-V]\d{4})([A-Z0-9]{2})$')


def _split_attached_modifier_code(
    code: str,
    code_type: Optional[str],
    known_modifiers: frozenset,
    canonical_prefixes_by_type: Optional[Dict[str, frozenset]] = None,
    digit_modifiers: Optional[frozenset] = None,
    modifier_validity_by_code: Optional[Dict[str, frozenset]] = None,
    descriptions_by_code: Optional[Dict[str, str]] = None,
    row_description: Optional[str] = None,
) -> Tuple[str, Optional[str], Optional[str]]:
    """Detect a `<structurally-valid CPT/HCPCS><known atomic modifier>`
    cell and split it.

    Returns ``(clean_code, new_code_type, extracted_modifier)``:
      * On a successful split (`73721TC` with `code_type='CDM'` and `TC`
        in ``known_modifiers``): returns ``('73721', 'CPT', 'TC')``.
      * On any guard failure (unrecognized suffix, wrong source
        ``code_type``, NDC/ICD shape, already-classified-as-CPT): returns
        ``(code, code_type, None)`` - caller treats the cell as-is.

    Guards (in priority order):

    1. ``code_type`` must be one of `CDM`, `LOCAL`, or `None` - codes
       already filed under a real coding system are not in scope. A
       `73721TC` already filed as `CPT` would mean the hospital encoded
       it as a 7-char CPT, which is invalid; that's a separate bug.
    2. The cell must match `_RE_BAKED_MODIFIER_CPT5` or
       `_RE_BAKED_MODIFIER_HCPCS` exactly. ICD-10-PCS (7-char
       alphanumeric like `02573ZZ` where `ZZ` is a legitimate part of
       the code) does NOT match these regexes because the prefix shape
       requires `\\d{5}` or `[A-CE-V]\\d{4}` - ICD codes use a
       different layout. NDC codes are 9–11+ digits with no letter
       suffix, also no match.
    3. The 2-char suffix must be in ``known_modifiers``: the caller
       passes the curated modifier set.
    4. If ``canonical_prefixes_by_type`` is provided, the extracted prefix
       must be in the canonical set for the resolved ``target_type``. Kills
       the over-match where 7-digit hospital CDM IDs (`4954052`
       "azithromycin tab") factor structurally as `<5-digit><2-char
       modifier>` but the 5-digit prefix isn't a real published CPT/HCPCS,
       only a coincidence. When ``None``, this guard is skipped.
    5. Digit-suffix gate: if ``digit_modifiers`` is provided and the suffix
       is in that set (i.e. it is a digit-only modifier token), then
       ``modifier_validity_by_code`` is consulted as the final guard.
       - ``modifier_validity_by_code`` is ``None`` → default-deny.
       - ``modifier_validity_by_code`` is a dict but the (prefix, suffix)
         pair is absent → reject (not CMS-validated).
       - ``modifier_validity_by_code`` has the prefix and suffix in its
         frozenset → accept.
       Letter suffixes never enter this branch.
    6. Description-consistency guard: applies ONLY inside the digit-suffix
       branch, AFTER Guard 5 passes.
       - ``descriptions_by_code`` is ``None`` → skip the guard.
       - ``descriptions_by_code`` is a dict → look up the reference
         description for ``prefix`` and require
         ``_description_matches(row_description, canonical_desc)`` → True.
         If no reference description is found, or the descriptions don't
         match → reject.
       Letter suffixes are NEVER subject to Guard 6.

    Casing: the regexes accept uppercase only. Callers should already
    have uppercased the cell via the existing pipeline (`_strip_non_ascii`
    + the `_CODE_TYPE_NORMALIZE` upper).
    """
    if not code:
        return code, code_type, None

    # Guard 1: source code_type must be a hospital-internal bucket.
    if code_type not in (None, 'CDM', 'LOCAL'):
        return code, code_type, None

    stripped = code.strip()
    if not stripped:
        return code, code_type, None

    # Guard 2: structural match. Try CPT first (more common at the
    # prevalence we measured), then HCPCS.
    m = _RE_BAKED_MODIFIER_CPT5.match(stripped)
    target_type: Optional[str] = None
    if m:
        target_type = 'CPT'
    else:
        m = _RE_BAKED_MODIFIER_HCPCS.match(stripped)
        if m:
            target_type = 'HCPCS'

    if m is None:
        return code, code_type, None

    prefix, suffix = m.group(1), m.group(2)

    # Guard 3: the suffix must be a real modifier per the caller's
    # curated set.
    if suffix not in known_modifiers:
        return code, code_type, None

    # Guard 4: the prefix must be a canonical published code. Skipped when
    # the caller passes None.
    if canonical_prefixes_by_type is not None:
        prefixes = canonical_prefixes_by_type.get(target_type)
        if not prefixes or prefix not in prefixes:
            return code, code_type, None

    # Guard 5: digit-suffix CMS-validity gate.
    # Only digit tokens enter this branch; letter suffixes are unaffected.
    # `modifier_validity_by_code` is keyed by bare CPT code only, so HCPCS
    # digit composites have no entries and fall through to default-deny
    # below. The frozensets hold only CMS-valid modifiers, so membership
    # here IS the validity check.
    if digit_modifiers and suffix in digit_modifiers:
        # Default-deny when no validity matrix is provided.
        if modifier_validity_by_code is None:
            return code, code_type, None
        valid = modifier_validity_by_code.get(prefix)
        if not valid or suffix not in valid:
            return code, code_type, None

        # Guard 6: description-consistency guard. Skipped when
        # descriptions_by_code is None. When provided, the row's free-text
        # description must match the reference description via
        # `_description_matches`: prevents coincidental CDM IDs (drugs,
        # devices, generics) from splitting just because their 5-digit
        # prefix happens to be a valid CPT.
        if descriptions_by_code is not None:
            canonical_desc = descriptions_by_code.get(prefix)
            if not _description_matches(row_description, canonical_desc):
                return code, code_type, None

    return prefix, target_type, suffix


# ---------------------------------------------------------------------------
# The curated modifier set `apply_baked_modifier_split` splits on.
#
# Only LETTER suffixes split on shape alone. Digit suffixes are far riskier:
# 7-digit hospital CDM IDs often factor as `<5-digit><2-digit>` by
# coincidence (`4954052` "azithromycin tab" looks like `49540` + `52`), and
# splitting them on shape alone produced a flood of false positives. So the
# digit tokens split only when a CMS (code, modifier) validity matrix is
# supplied and confirms the pair (PC/TC indicator, bilateral indicator,
# CLIA-waived list).
#
# Other common baked modifiers (`SG`, `CL`, `AS`, `80-82`, `62/66`,
# `53/73/74`) are deliberately not in the set yet: the set is kept equal to
# the curated modifier dictionary so every split modifier is one the rest of
# a pipeline knows how to classify.
_BAKED_MODIFIER_LETTER_KEYS = frozenset({
    # ------- component -------
    "TC",
    # ------- drug_supply -------
    "JW", "JZ", "TB", "SL",
    # ------- distinct -------
    "XE", "XP", "XS", "XU",
    # ------- enhancement / oversight -------
    "QW",
    # ------- anatomy / supply -------
    "LT", "RT",
    # ------- admin_noise -------
    "GY", "FY", "GO", "GN", "GP", "NU", "PO",
})

# Numeric-suffix tokens. Split only when a CMS validity matrix confirms the
# (CPT, modifier) pair.
_BAKED_MODIFIER_DIGIT_KEYS = frozenset({
    # ------- component -------
    "26",
    # ------- surgical_phase -------
    "54", "55", "56", "58", "78", "79", "24",
    # ------- repeat / distinct -------
    "76", "77", "91", "59",
    # ------- enhancement / oversight -------
    "22", "25", "50", "52", "90", "95",
})

_BAKED_MODIFIER_SPLIT_KEYS = _BAKED_MODIFIER_LETTER_KEYS | _BAKED_MODIFIER_DIGIT_KEYS


# ---------------------------------------------------------------------------
# Description-consistency helper.
#
# Decides whether a CDM row's free-text description is consistent with a
# reference description for the code (``ReferenceData.code_descriptions``).
# Both strings are normalized before comparison:
#
#   1. Uppercase; strip surrounding quotes; replace non-alphanumeric runs
#      with spaces; collapse whitespace.
#   2. Expand a small abbreviation map so common radiography short-forms
#      align with the reference descriptions.
#   3. Reject on a denylist of generic/non-specific descriptions that match
#      virtually anything (e.g. "OTHER OUTPATIENT", "MISC").
#   4. Tokenize both sides; drop 1-char tokens (noise).  Compute overlap of
#      the canonical token set C against the row token set R.
#      Match condition:  |C ∩ R| / |C| >= 0.5  AND  |C ∩ R| >= 1
#      AND at least one shared token has len >= 4 (avoids matching on tiny
#      words like "OF", "OR", "BY", "MG" only).
#
# Rationale for the 0.5 threshold: reference short descriptions are often
# brief (2-4 meaningful tokens); requiring only 50 % coverage admits slight synonymy
# while blocking completely unrelated descriptions (drugs, devices).
# ---------------------------------------------------------------------------

# Small abbreviation expansion map.
# Applied in two passes:
#   Pass 1 (pre-normalization): whole-string regex substitutions that must
#     fire BEFORE the general non-alphanumeric → space replacement.  Handles
#     hyphenated forms like "X-RAY" → "XRAY" which would otherwise be split
#     into "X" + "RAY" by the normalizer.
#   Pass 2 (post-normalization): token-level lookup on the already-uppercased,
#     whitespace-collapsed string.  Keys are standalone uppercase tokens.
# Keep the map small and well-commented - it's a precision tuning knob.

# Pass 1: pre-normalization whole-word substitutions (case-insensitive regex).
# Each entry is (pattern, replacement) where pattern is matched against the
# uppercased raw string before non-alpha stripping.
_DESC_PRENORM_SUBS: list = [
    # "X-RAY" / "X-RAYS" → "XRAY" / "XRAYS" (hyphen removed before normalization
    # strips all non-alphanumeric, so the compound becomes a single token).
    (re.compile(r"\bX-RAYS?\b"), "XRAY"),
]

# Pass 2: post-normalization token → canonical expansion.
# Applied after uppercasing + non-alpha strip + collapse.
_DESC_ABBREV: Dict[str, str] = {
    "XR":       "XRAY",     # "XR FEMUR" → "XRAY FEMUR"
    "BIL":      "BILATERAL",
    "BILAT":    "BILATERAL",
    "W":        "WITH",     # "W/" becomes "W" after non-alpha strip
    "WO":       "WITHOUT",  # "W/O" becomes "WO" after non-alpha strip
    "BX":       "BIOPSY",
}

# Normalized forms of the generic denylist (applied after normalization step).
_DESC_GENERIC_DENYLIST: frozenset = frozenset({
    "OTHER OUTPATIENT",
    "OUTPATIENT",
    "OTHER",
    "MISC",
    "MISCELLANEOUS",
})

# Non-discriminating filler words. Removed from BOTH token sets before the
# coverage calc so they don't dilute the canonical denominator: short
# reference descriptions often carry "AND"/"OF", which would otherwise drop
# a real composite below the threshold.
# Dropping them never lowers precision (they carry no clinical signal).
_DESC_STOPWORDS: frozenset = frozenset({
    "AND", "OF", "OR", "THE", "WITH", "WITHOUT", "FOR", "TO", "IN", "ON",
    "BY", "A", "AN",
})

_RE_DESC_NONALNUM = re.compile(r"[^A-Z0-9]+")


def _normalize_desc(s: str) -> str:
    """Normalize a description for comparison:
      upper-case → pre-norm substitutions → strip surrounding quotes →
      replace non-alphanumeric with spaces → collapse whitespace.
    """
    s = s.upper().strip()
    # Pass 1: pre-normalization substitutions (hyphenated compounds).
    for pattern, repl in _DESC_PRENORM_SUBS:
        s = pattern.sub(repl, s)
    s = s.strip('"').strip("'")
    s = _RE_DESC_NONALNUM.sub(" ", s).strip()
    return s


def _description_matches(
    row_desc: Optional[str],
    canonical_desc: Optional[str],
) -> bool:
    """Return True when the CDM row description is consistent with the
    reference description for the code.

    Both strings must be non-empty after normalization; empty-after-normalize
    → False (default-deny).  Generic/non-specific row descriptions → False.

    Matching rule (token-set overlap):
      Let C = canonical token set (tokens len >= 2), R = row token set
      (tokens len >= 2).
      Match when:
        |C ∩ R| / |C| >= 0.5  (at least half of canonical tokens covered)
        AND |C ∩ R| >= 1
        AND at least one shared token has len >= 4  (no trivial-word match)
    """
    if not row_desc or not canonical_desc:
        return False

    row_norm = _normalize_desc(row_desc)
    can_norm = _normalize_desc(canonical_desc)

    if not row_norm or not can_norm:
        return False

    # Reject generic row descriptions outright.
    if row_norm in _DESC_GENERIC_DENYLIST:
        return False

    # Pass 2: token-level abbreviation expansion.
    def _expand(text: str) -> str:
        tokens = text.split()
        return " ".join(_DESC_ABBREV.get(t, t) for t in tokens)

    row_norm = _expand(row_norm)
    can_norm = _expand(can_norm)

    # Tokenize; drop 1-char noise tokens and non-discriminating stop-words
    # (stop-words in the reference description would otherwise inflate the
    # coverage denominator and reject real composites).
    row_tokens = {t for t in row_norm.split()
                  if len(t) >= 2 and t not in _DESC_STOPWORDS}
    can_tokens = {t for t in can_norm.split()
                  if len(t) >= 2 and t not in _DESC_STOPWORDS}

    if not can_tokens:
        return False

    overlap = can_tokens & row_tokens
    if not overlap:
        return False

    # Coverage: at least half of canonical tokens must be present.
    coverage = len(overlap) / len(can_tokens)
    if coverage < 0.5:
        return False

    # At least one shared token must be "substantial" (len >= 4) so two
    # descriptions that share only tiny words ("OF", "MG", "BY") don't match.
    if not any(len(t) >= 4 for t in overlap):
        return False

    return True


def apply_baked_modifier_split(
    code: Optional[str],
    code_type: Optional[str],
    description: Optional[str] = None,
    *,
    ref: Optional[ReferenceData] = None,
    stats: Optional[ParseStats] = None,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Split a modifier baked into the code, using the curated modifier set.

    Returns ``(code, code_type, baked_modifier)``:
      * Successful split (`'73721TC' + 'CDM'` → `('73721', 'CPT', 'TC')`):
        caller updates code/code_type AND merges ``baked_modifier`` into
        the row's `modifiers` field (via `merge_modifier_into_field`).
      * No split (any guard failure): returns the original ``(code,
        code_type, None)`` unchanged. Caller proceeds as before.

    Safe to call with ``code = None`` or ``code = ''`` (returns inputs
    unchanged with ``baked_modifier = None``).

    Letter suffixes split on shape alone. With ``ref``:
      * ``ref.code_prefixes`` makes the prefix a published CPT/HCPCS code
        (Guard 4);
      * ``ref.modifier_validity`` lets digit suffixes split when CMS marks
        the (code, modifier) pair valid (Guard 5). Without it digit
        suffixes never split;
      * ``ref.code_descriptions`` additionally requires the row
        description to match the code's reference description before a
        digit suffix splits (Guard 6).

    ``stats``, when given, counts each split.
    """
    if not code:
        return code, code_type, None
    result = _split_attached_modifier_code(
        code, code_type,
        _BAKED_MODIFIER_SPLIT_KEYS,
        canonical_prefixes_by_type=ref.code_prefixes if ref else None,
        digit_modifiers=_BAKED_MODIFIER_DIGIT_KEYS,
        modifier_validity_by_code=ref.modifier_validity if ref else None,
        descriptions_by_code=ref.code_descriptions if ref else None,
        row_description=description,
    )
    if stats is not None and result[2]:
        stats.record_baked_modifier_split(result[2], code_type)
    return result


def merge_modifier_into_field(
    existing: Optional[str],
    baked: Optional[str],
) -> Optional[str]:
    """Merge a modifier split out of the code into a row's modifiers field.

    Used after ``apply_baked_modifier_split`` returns a non-None
    ``baked_modifier``: the caller adds it to whatever the MRF row already
    has in its `modifiers` column. Token-set semantics: order is
    preserved (existing tokens first, baked appended last) but a duplicate
    is dropped so a fixture row with both `'73721TC'` AND a `'TC'` in its
    modifiers column doesn't end up with `'TC,TC'`.

    Tokens are split on `[|, ;]+`, the delimiter set modifier fields use
    in the wild. The output uses a single comma separator so it
    round-trips through the same tokenizer cleanly.

    Returns the merged string, or ``None`` if both inputs are empty.
    """
    if not baked:
        return existing
    baked = baked.strip()
    if not baked:
        return existing
    if not existing or not existing.strip():
        return baked

    # Use the same delimiter regex readers split on so we don't invent a
    # new token boundary here.
    tokens = [t for t in re.split(r"[|, ;]+", existing) if t]
    if baked in tokens:
        # Already present - return canonicalized form (comma-joined, no
        # leading/trailing whitespace) so we don't drift the field shape.
        return ",".join(tokens)
    tokens.append(baked)
    return ",".join(tokens)


def normalize_code(
    code: Optional[str],
    code_type: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    """
    Normalize code and code_type to standard formats.

    Returns: (normalized_code, normalized_code_type)

    Rules applied:
    1. Strip non-ASCII characters (mojibake, encoding artifacts)
    2. Uppercase and canonicalize code_type via _CODE_TYPE_NORMALIZE
    2A. NDCHCPCS / NDCCPT disambiguation: pick real type from code shape
    2B. Junk code_type discard (TYPE/MODIFIER/MODIFIERS/OBS/DOT/UN → treat
        as missing, re-derive from code shape)
    3. Detect hospital-internal codes (SUP-*, PX-*, RX-*) → CDM
    4. Detect misclassified Revenue Codes stored as HCPCS → RC
    4.5. Reclassify unambiguous standard code shapes mislabeled CDM/LOCAL
    5. Normalize HCPCS Level I → CPT for 5-digit numeric codes
    5.5. Reclassify D-prefix codes (CDT dental) → CDT
    5.6. HCPCS Level II forward fix: letter-prefix [A-CE-V]\\d{4} that
         hospitals labeled CPT → HCPCS (excludes D handled by 5.5)
    5.7. CPT Cat II/III/PLA reverse fix: \\d{4}[FTU] that hospitals
         labeled HCPCS → CPT
    6. Zero-pad Revenue Codes to 4 digits
    6A. Cross-system reclassify: controlled-vocab numeric type holding a
        standard-shape code (e.g. CPT 99232 declared as RC) → reclassify
        to correct standard type before residue gating.
    7. Normalize NDC to canonical 11-digit no-dash form (see
       _normalize_ndc): leading-letter NDCs reclassify to LOCAL;
       3-segment dashed forms with optional packaging suffix collapse to
       11 digits; 9-digit numeric pads to 11 with '00'; 10/11-digit
       bases with _ip/_op, trailing letter, or _<digits> package suffix
       canonicalize to 11.
    8. Infer code_type from code format when code_type is missing (then
       re-apply NDC normalization if NDC was inferred)
    9. Validate code against declared code_type format; extract valid code
       from garbage wrapping when possible (e.g. XJ0591X → J0591/HCPCS)
    10. Classify placeholder/test codes as LOCAL
    11. APR-DRG qualifier stripping
    12. Residue gating: controlled-vocab numeric type with a code that still
        fails the canonical regex → reclassify to LOCAL (code preserved).
    """
    if not code and not code_type:
        return code, code_type

    # --- Step 1: Strip non-ASCII characters ---
    if code:
        cleaned = _strip_non_ascii(code.strip())
        if cleaned != code.strip():
            code = cleaned
        if not code:
            return code, code_type

    # --- Step 1.5: Split composite code values ---
    # Some hospitals (e.g. Partners Healthcare / Brigham & Women's) embed the
    # code type as a prefix in the code column: "HCPCS C1776", "CPT 87339",
    # "MS-DRG V41.0 (FY 2024) 155".  Split these so the code type flows into
    # Step 2 for normalization.
    if code:
        code, code_type = _split_composite_code(code, code_type)

    # --- Step 2: Normalize code_type ---
    norm_type = None
    if code_type:
        raw_upper = code_type.strip().upper()
        norm_type = _CODE_TYPE_NORMALIZE.get(raw_upper, raw_upper)

    # --- Step 2A: NDCHCPCS / NDCCPT disambiguation ---
    # Some CMS/hospital data formats emit compound types like "NDCHCPCS" or
    # "NDCCPT" to indicate the code column may contain either an NDC or a
    # procedure code.  Pick the real type by code shape:
    #   - NDC shape → normalize through _normalize_ndc() → NDC or LOCAL
    #   - [A-CE-V]\d{4} → HCPCS (Level II letter-prefix)
    #   - \d{5} or \d{4}[FTU] → CPT (for NDCCPT; also acceptable for NDCHCPCS)
    #   - else → LOCAL
    if code and norm_type in ('NDCHCPCS', 'NDCCPT'):
        s = code.strip()
        # NDC shape: 11-digit, 9-digit, 10-digit, or 3-segment dashed form
        if (re.fullmatch(r'\d{9,11}', s)
                or re.fullmatch(r'\d{4,5}-\d{3,4}-\d{1,2}', s)):
            new_code, new_type = _normalize_ndc(code)
            return new_code, new_type
        elif re.fullmatch(r'[A-CE-Va-ce-v]\d{4}', s):
            norm_type = 'HCPCS'
        elif (re.fullmatch(r'\d{5}', s)
              or re.fullmatch(r'\d{4}[FTUftu]', s)):
            norm_type = 'CPT'
        else:
            norm_type = 'LOCAL'

    # --- Step 2B: Junk code_type discard ---
    # Non-system labels (TYPE, MODIFIER, MODIFIERS, OBS, DOT, UN) carry no
    # system information.  Discard them so the blank-type path (steps 4.5,
    # 5, 5.5–5.7, 8) can re-derive the type from code shape.
    if norm_type in _JUNK_CODE_TYPES:
        norm_type = None

    # --- Step 2.5: Strip trailing float-coercion artifact (.0) ---
    # Excel / ETL tools sometimes coerce integer revenue codes and DRG numbers
    # to floats, writing "320.0" instead of "320".  Strip a trailing "\.0+$"
    # ONLY for numeric controlled-vocabulary types where the decimal is always
    # spurious.  MUST NOT apply to ICD (250.0 is a valid ICD-9 code), CPT,
    # HCPCS, NDC, CDM, LOCAL, or any other type.
    if code and norm_type in _FLOAT_COERCE_STRIP_TYPES:
        stripped_code = code.strip()
        stripped_code = re.sub(r'\.0+$', '', stripped_code)
        if stripped_code != code.strip():
            code = stripped_code

    # --- Step 3: Detect hospital-internal codes by prefix ---
    if code:
        code_upper = code.strip()
        # SUP- (supplies), PX- (procedures), RX- (pharmacy) are CDM conventions
        if code_upper.startswith(('SUP-', 'PX-', 'RX-')):
            return code_upper, 'CDM'

    # --- Step 4: Detect misclassified codes ---
    # Some hospitals report RC values under code_type='HCPCS'
    if code and norm_type in ('HCPCS', None) and code.strip() in _KNOWN_REVENUE_CODES:
        norm_type = 'RC'
    # Numeric codes > 5 digits labeled as CPT/HCPCS are really CDM codes
    elif (code and norm_type in ('CPT', 'HCPCS')
          and code.strip().isdigit() and len(code.strip()) > 5):
        norm_type = 'CDM'

    # --- Step 4.5: Recover standard codes mislabeled as CDM/LOCAL ---
    # CDM/LOCAL are hospital-internal buckets, but many files put standard
    # codes there. Only reclassify shapes that are unambiguous. Bare 3-digit
    # DRGs and bare 4-digit APCs are intentionally not inferred because they
    # collide with revenue codes and hospital-local identifiers. Prefixed
    # values such as "MS-DRG 470" and "APC 5191" are handled by Step 1.5.
    if code and norm_type in ('CDM', 'LOCAL'):
        stripped = code.strip()
        if re.fullmatch(r'[Dd]\d{4}', stripped):
            norm_type = 'CDT'
        elif re.fullmatch(r'[A-CE-Va-ce-v]\d{4}', stripped):
            norm_type = 'HCPCS'
        elif (re.fullmatch(r'\d{5}', stripped)
              or re.fullmatch(r'\d{4}[FTUftu]', stripped)):
            norm_type = 'CPT'

    # --- Step 5: Normalize HCPCS Level I → CPT ---
    # Many hospitals label all procedure codes as 'HCPCS' even when they are
    # 5-digit numeric CPT codes.  HCPCS Level I ≡ CPT, so normalize them.
    # True HCPCS Level II codes have a letter prefix (A0000-V9999) and are
    # already correctly classified.
    if (code and norm_type == 'HCPCS'
            and code.strip().isdigit() and len(code.strip()) == 5):
        norm_type = 'CPT'

    # --- Step 5.5: Reclassify D-prefix codes as CDT ---
    # CDT dental codes (D0120, D7509, etc.) are often misreported as HCPCS or
    # CPT by hospitals.  Reclassify them so dental codes have a consistent type.
    if (code and norm_type in ('HCPCS', 'CPT')
            and re.fullmatch(r'[Dd]\d{4}', code.strip())):
        norm_type = 'CDT'

    # --- Step 5.6: HCPCS Level II forward fix ---
    # Letter-prefix Level II codes (A0000-V9999, excluding D) are HCPCS by
    # definition. Some hospital MRFs misclassify them as CPT (e.g. Q5128
    # under code_type='CPT'). CPT codes are unambiguously 5-digit numerics
    # or `\d{4}[FTU]` Cat II/III/PLA codes, never letter-prefix.
    # Excludes D (CDT) which Step 5.5 already handled.
    if (code and norm_type == 'CPT'
            and re.fullmatch(r'[A-CE-Va-ce-v]\d{4}', code.strip())):
        norm_type = 'HCPCS'

    # --- Step 5.7: CPT Cat II/III/PLA reverse fix ---
    # Cat II (\d{4}F), Cat III (\d{4}T), and PLA (\d{4}U) codes are CPT
    # by definition (HCPCS Level II never has a trailing F/T/U). Some
    # hospital MRFs put these under code_type='HCPCS', and some omit the
    # type entirely; normalize both cases to CPT.
    if (code and norm_type in ('HCPCS', None)
            and re.fullmatch(r'\d{4}[FTUftu]', code.strip())):
        norm_type = 'CPT'

    # --- Step 6A: Cross-system reclassify ---
    # A controlled-vocab numeric type (RC / MS-DRG / APR-DRG / APC / CMG /
    # EAPG) may hold a standard-shape procedure code that a hospital
    # misrouted (e.g. CPT 99232 declared as RC).  If the code fails the
    # type's canonical regex AND matches a standard shape, reclassify:
    #   \d{5} / \d{4}[FTU] → CPT
    #   [A-CE-V]\d{4}       → HCPCS
    #   D\d{4}              → CDT
    # Run this BEFORE residue gating (step 12) so reclassified codes never
    # reach the LOCAL fallback.  RC zero-padding (step 6) runs after this so
    # we don't accidentally treat a 5-digit CPT as a 5-digit revenue code.
    if code and norm_type in _CONTROLLED_VOCAB_VALIDATORS:
        validator = _CONTROLLED_VOCAB_VALIDATORS[norm_type]
        stripped = code.strip()
        if not validator.fullmatch(stripped):
            if re.fullmatch(r'\d{5}', stripped):
                norm_type = 'CPT'
            elif re.fullmatch(r'\d{4}[FTUftu]', stripped):
                norm_type = 'CPT'
            elif re.fullmatch(r'[A-CE-Va-ce-v]\d{4}', stripped):
                norm_type = 'HCPCS'
            elif re.fullmatch(r'[Dd]\d{4}', stripped):
                norm_type = 'CDT'

    # --- Step 6: Zero-pad Revenue Codes ---
    if code and norm_type == 'RC':
        stripped = code.strip()
        if stripped.isdigit():
            code = stripped.zfill(4)
            return code, norm_type

    # --- Step 7: Normalize NDC to canonical 11-digit no-dash form ---
    # See _normalize_ndc(). Handles leading-letter→LOCAL reclassification,
    # 9- and 10-digit padding, setting/letter/package (incl. double)
    # suffixes, and 3-segment dashed shapes.
    if code and norm_type == 'NDC':
        new_code, new_type = _normalize_ndc(code)
        return new_code, new_type

    # --- Step 8: Infer code_type from code format when missing ---
    if code and not norm_type:
        stripped = code.strip()
        # CDT dental codes: D + 4 digits (D0120, D7509, etc.)
        if re.fullmatch(r'[Dd]\d{4}', stripped):
            norm_type = 'CDT'
        # HCPCS Level II: letter + 4 digits (J0690, C1776, A4550, etc.)
        elif re.fullmatch(r'[A-Za-z]\d{4}', stripped):
            norm_type = 'HCPCS'
        # CPT: exactly 5 digits
        elif re.fullmatch(r'\d{5}', stripped):
            norm_type = 'CPT'
        # CPT Category II/III/PLA: 4 digits + F/T/U (1036F, 0237T, 0016U)
        elif re.fullmatch(r'\d{4}[FTUftu]', stripped):
            norm_type = 'CPT'
        # NDC: digits with dashes (10-11 digit segments). Run the full
        # NDC normalizer so the inferred NDC is also canonicalized.
        elif re.fullmatch(r'\d{4,5}-\d{3,4}-\d{1,2}', stripped):
            new_code, new_type = _normalize_ndc(code)
            return new_code, new_type
        # MS-DRG: exactly 3 digits
        elif re.fullmatch(r'\d{3}', stripped):
            # Ambiguous: could be RC or MS-DRG. Don't guess - leave as-is.
            pass

    # --- Step 9: Validate code against declared code_type ---
    # If the code doesn't match the expected format for its declared type,
    # try to extract a valid code from the raw value.  Handles cases like
    # "XJ0591X" labeled as CPT → extract "J0591" and reclassify as HCPCS.
    if code and norm_type in ('CPT', 'HCPCS'):
        stripped = code.strip().upper()
        is_valid = (
            _RE_CPT.fullmatch(stripped)       # CPT: 99213
            or _RE_CPT_PLA.fullmatch(stripped) # PLA: 0202U, 0003M
            or _RE_HCPCS.fullmatch(stripped)   # HCPCS: J0591
            or _RE_CAT3.fullmatch(stripped)    # Category III: 0237T
        )
        if not is_valid:
            # Try to extract a valid code from the garbage
            extracted_code, extracted_type = _extract_code_from_garbage(stripped)
            if extracted_code:
                code = extracted_code
                norm_type = extracted_type
            else:
                # Can't salvage - downgrade to LOCAL
                norm_type = 'LOCAL'

    # --- Step 10: Classify placeholder/test codes ---
    if code and norm_type not in ('RC', 'NDC', 'CDM', 'MS-DRG', 'APC'):
        stripped = code.strip()
        if _RE_PLACEHOLDER.match(stripped):
            norm_type = 'LOCAL'

    # --- Step 11: APR-DRG qualifier stripping ---
    # APR-DRG codes are formatted as 'NNN-S' where NNN is 3-digit DRG and S is
    # a 1-digit severity (0-4).  Many hospitals append qualifier text:
    #   '001-1 Short Stay'             -> '001-1'
    #   '001-1- IP LOS Greater Than 50' -> '001-1'
    #   '001 - 1'                       -> '001-1' (normalise whitespace)
    if code and norm_type == 'APR-DRG':
        code = _strip_apr_drg_qualifier(code)

    if code:
        code = code.strip()

    # --- Step 12: Residue gating → LOCAL ---
    # After all recovery/reclassify steps above, if the final code_type is a
    # controlled-vocab numeric system and the final code still fails that
    # system's canonical regex, route code_type to LOCAL.  The code value is
    # preserved unchanged - this is a classification fix, not a deletion.
    # Examples that land here: MS-DRG '1622' (4-digit, valid range is 1-3
    # digits), EAPG wrong-length numeric, miscellaneous numeric hospital IDs.
    # Do NOT gate CPT or HCPCS: validating those needs the licensed code
    # lists, and shape checks already ran in steps 5-9.
    #
    # Scope: only numeric-looking codes (all-digit, or digit-dash for
    # APR-DRG/CMG subtypes). Non-numeric or mixed values (e.g. 'MSCODE',
    # 'MS-012', 'NONE' under RC) are preserved in their declared type -
    # they are hospital chargemaster labels that cannot be safely reclassified.
    if code and norm_type in _CONTROLLED_VOCAB_VALIDATORS:
        validator = _CONTROLLED_VOCAB_VALIDATORS[norm_type]
        if not validator.fullmatch(code):
            # Only gate codes that are purely numeric (or digit+dash, which is
            # the canonical form for APR-DRG/CMG). Letter-prefix or mixed codes
            # stay in their declared type.
            if re.fullmatch(r'[\d-]+', code):
                norm_type = 'LOCAL'

    return code, norm_type


# Regex used by _strip_apr_drg_qualifier: extracts the NNN-S base from the
# start of an APR-DRG code, tolerating whitespace and dashes.
_RE_APR_DRG_BASE = re.compile(r'^\s*(\d{3})\s*-\s*(\d)')

# Period-separated form: '48.4' → '048-4', '532.2' → '532-2'
# Must precede _RE_APR_DRG_NNNS/base fallback to avoid '48.4' → '048'.
_RE_APR_DRG_PERIOD = re.compile(r'^\s*(\d{1,3})\.(\d)\s*$')

# Verbose 'APRnnn SOI s' form (case-insensitive; double-space tolerated).
# Example: 'APR052  SOI 1' → '052-1'.
_RE_APR_DRG_VERBOSE = re.compile(r'^\s*APR(\d{3})\s+SOI\s+(\d)\s*$', re.IGNORECASE)

# Matches the legacy bare 4-digit NNNS form (no separator). Only applied
# when no separator is present at all, so it can't misfire on codes like
# '0012 Short Stay' (those are handled by _RE_APR_DRG_BASE first).
_RE_APR_DRG_NNNS = re.compile(r'^\s*(\d{3})(\d)\s*$')

# Short leading-zero hyphen form: '48-3' → '048-3', '5-2' → '005-2'
# Matches only 1-2 digit base with a single severity digit (no ambiguity with
# the canonical NNN-S form handled by _RE_APR_DRG_BASE).
_RE_APR_DRG_SHORT_HYPHEN = re.compile(r'^\s*(\d{1,2})-(\d)\s*$')

# Bare short base (no severity): '48' → '048', '5' → '005'.
# Base-only without severity; passes _RE_VALID_APR_DRG which allows ^\d{1,4}(-\d{1,2})?$.
_RE_APR_DRG_SHORT_BASE = re.compile(r'^\s*(\d{1,2})\s*$')

# Compact 'APRnnnns' form (case-insensitive): 'APR0011' → '001-1'.
# DISABLED: the only known source of this form is a single file that lists
# 1,340 all-distinct values, which looks more like an enumeration dump than
# real APR-DRG codes. Gated behind _APR_COMPACT_ENABLED until that is
# confirmed either way.
_RE_APR_DRG_COMPACT = re.compile(r'^\s*APR(\d{3})(\d)\s*$', re.IGNORECASE)
_APR_COMPACT_ENABLED = False

# Helper: zero-pad a string to 3 digits.
_pad3 = lambda s: s.zfill(3)


def _strip_apr_drg_qualifier(code: str) -> str:
    """Return the canonical APR-DRG form of *code*.

    Handles the following source formats, tested in order (ORDER IS CORRECTNESS):

    1. 'NNN-S' with optional qualifier text  - '001-1 Short Stay' → '001-1'
       '001 - 1' (whitespace-padded dash)    → '001-1'
    2. Period-separated  'N.S' / 'NN.S' / 'NNN.S' - '48.4'→'048-4', '532.2'→'532-2'
       MUST precede bare-3-digit fallback to avoid eating the severity digit.
    3. Verbose 'APRnnn SOI s'               - 'APR052  SOI 1' → '052-1'
    4. Compact 'APRnnns' (DARK/GATED)       - 'APR0011' → '001-1'
       Disabled: see _APR_COMPACT_ENABLED.
    5. Bare 4-digit NNNS                    - '0011' → '001-1'
    6. Short leading-zero hyphen 'NN-S'/'N-S' - '48-3'→'048-3', '5-2'→'005-2'
    7. Short bare base (no severity)         - '48'→'048', '5'→'005'
       Base-only is intentional; passes _RE_VALID_APR_DRG.
    8. Bare 3-digit fallback (no severity)   - '139 Unspecified' → '139'

    If none match, return the original value unchanged.
    """
    if not code:
        return code

    # 1. Canonical NNN-S (with optional qualifier / spacing).
    m = _RE_APR_DRG_BASE.match(code)
    if m:
        return f"{m.group(1)}-{m.group(2)}"

    # 2. Period-separated: NNN.S (MUST be before bare-3-digit fallback).
    m = _RE_APR_DRG_PERIOD.match(code)
    if m:
        return f"{_pad3(m.group(1))}-{m.group(2)}"

    # 3. Verbose 'APRnnn SOI s'.
    m = _RE_APR_DRG_VERBOSE.match(code)
    if m:
        return f"{m.group(1)}-{m.group(2)}"

    # 4. Compact 'APRnnns' - DARK until precheck confirms non-enumeration source.
    if _APR_COMPACT_ENABLED:
        m = _RE_APR_DRG_COMPACT.match(code)
        if m:
            return f"{m.group(1)}-{m.group(2)}"

    # 5. Legacy bare 4-digit NNNS form: '0011' → '001-1'.
    m = _RE_APR_DRG_NNNS.match(code)
    if m:
        return f"{m.group(1)}-{m.group(2)}"

    # 6. Short leading-zero hyphen: '48-3' → '048-3', '5-2' → '005-2'.
    m = _RE_APR_DRG_SHORT_HYPHEN.match(code)
    if m:
        return f"{_pad3(m.group(1))}-{m.group(2)}"

    # 7. Short bare base (no severity): '48' → '048', '5' → '005'.
    m = _RE_APR_DRG_SHORT_BASE.match(code)
    if m:
        return _pad3(m.group(1))

    # 8. Last resort: extract leading 3-digit base (partial/unknown qualifier text).
    m = re.match(r'^\s*(\d{3})', code)
    if m:
        return m.group(1)

    return code


# ============================================================================
# CODE REJECTION: detect corrupt rows that should be skipped
# ============================================================================
# After normalize_code() has done its best, some rows are still irreparably
# corrupt and should be dropped.  Examples seen in real files:
#   - code_type is a bare numeric literal like '1' or '2' (column mis-parse)
#   - MS-DRG rows with 'N.NNNN' code values (decimal DRG, not a real format)
# Return a short machine-readable reason string, or None if the row is OK.


_RE_NUMERIC_ONLY = re.compile(r'^\d+$')
_RE_MS_DRG_DECIMAL = re.compile(r'^\d+\.\d+$')
# Structurally valid code_types are short uppercase tokens composed of
# letters, digits, '-' and '/' only.  Real-world examples span CPT, HCPCS,
# MS-DRG, APR-DRG, TRIS-DRG, CPT/HCPCS, CDM, NDC, ICD, APC, EAPG, RC, CDT,
# CMG, R-DRG, LOCAL, HIPPS.  We allow unknown-but-plausible tokens through
# (e.g. a future 'XYZ-DRG') while rejecting obvious column-shift garbage
# like stray descriptions, decimals, or sentence fragments.
_RE_VALID_CODE_TYPE_SHAPE = re.compile(r'^[A-Z][A-Z0-9]*(?:[-/][A-Z0-9]+)*$')
_MAX_CODE_TYPE_LEN = 16
# When code_type itself matches a HCPCS/CDT Level II pattern (letter +
# 4 digits), the columns have almost certainly shifted: what looks like
# the 'type' is actually a code.  5-digit numeric values (e.g. a CPT
# leaking into the type column) are caught separately by the numeric-only
# rule.  Seen in the wild: code='CDM', code_type='C1887' - where CDM is
# the real type and C1887 is the real code.
_RE_CODE_TYPE_LOOKS_LIKE_HCPCS = re.compile(r'^[A-Z]\d{4}$')


def is_rejected_code(
    code: Optional[str],
    code_type: Optional[str],
    *,
    stats: Optional[ParseStats] = None,
) -> Optional[str]:
    """Decide whether a (code, code_type) pair is too corrupt to keep.

    Returns a short reason string (e.g. 'numeric_code_type',
    'malformed_code_type', 'ms_drg_decimal') when the row must be
    rejected, or None when the row is acceptable.  Callers should drop
    rejected rows rather than emit them.

    This is intentionally conservative: only clearly broken inputs are
    rejected; everything borderline is accepted and classified as LOCAL by
    normalize_code() so a human can still review it downstream.

    ``stats``, when given, counts each rejection.
    """
    reason = _rejection_reason(code, code_type)
    if reason and stats is not None:
        stats.record_rejected_code(code, code_type)
    return reason


def _rejection_reason(
    code: Optional[str],
    code_type: Optional[str],
) -> Optional[str]:
    # Empty rows are not "corrupt", just uninteresting - let callers decide.
    if not code and not code_type:
        return None

    # code_type is purely numeric (e.g. '1', '2', '12') - almost always a
    # column mis-parse where a price or count leaked into the type column.
    if code_type:
        ct_stripped = code_type.strip()
        if ct_stripped and _RE_NUMERIC_ONLY.fullmatch(ct_stripped):
            return 'numeric_code_type'

        # HCPCS/CDT Level II lookalike: a single uppercase letter followed by
        # exactly 4 digits (e.g. 'C1887', 'J0591', 'D9999') is a HCPCS code,
        # not a code_type.  Seeing this in the code_type column means the
        # columns have shifted and the real type sits elsewhere.
        if ct_stripped and _RE_CODE_TYPE_LOOKS_LIKE_HCPCS.fullmatch(ct_stripped.upper()):
            return 'malformed_code_type'

        # Structural sanity: real code_types are short token-like values.
        # Anything containing whitespace, decimals, or punctuation other
        # than '-' / '/' is almost certainly a CSV column-shift artefact
        # (e.g. an unescaped inch-mark in a description causes
        # csv.DictReader to swallow the separator and shift every
        # subsequent column by one).  We validate the uppercased form, so
        # lowercase-only tokens are allowed if uppercasing yields a valid
        # shape, while prose like 'as remittances do not itemize...' is
        # rejected.
        if ct_stripped and len(ct_stripped) <= _MAX_CODE_TYPE_LEN:
            if not _RE_VALID_CODE_TYPE_SHAPE.fullmatch(ct_stripped.upper()):
                return 'malformed_code_type'
        elif ct_stripped:
            return 'malformed_code_type'

    # MS-DRG with a decimal value (e.g. '0.1234') is not a valid DRG format.
    # Real MS-DRGs are 3-digit integers.  These rows came from weird
    # column-swap bugs in some chargemasters.
    if code and code_type and code_type.upper() == 'MS-DRG':
        if _RE_MS_DRG_DECIMAL.fullmatch(code.strip()):
            return 'ms_drg_decimal'

    return None


def _extract_code_from_garbage(raw: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Try to extract a valid CPT/HCPCS/CDT code from a malformed code string.

    Handles patterns like:
    - XJ0591X → J0591 (HCPCS wrapped in garbage)
    - AD9999 → D9999 (CDT dental code with A-prefix)
    - A29999.41 → 29999 (A-prefix CPT with decimal sub-code)
    - A0237T → 0237T (A-prefix Category III)

    Returns: (extracted_code, code_type) or (None, None) if nothing found.
    """
    # Pattern 1: A-prefix + Category III code (A0237T → 0237T)
    # Must check before HCPCS extraction to avoid matching the A as HCPCS prefix
    m = re.fullmatch(r'A(\d{4}T)', raw)
    if m:
        return m.group(1), 'HCPCS'

    # Pattern 2: A-prefix + 5-digit CPT with optional decimal (A29999.41 → 29999)
    # Stanford convention: A = facility component prefix on CPT codes
    m = re.fullmatch(r'A(\d{5})(?:\.\d+)?', raw)
    if m:
        return m.group(1), 'CPT'

    # Pattern 3: A-prefix + HCPCS with optional decimal (A4649.0099 → A4649)
    # Real HCPCS code A4649 with sub-code suffix
    m = re.fullmatch(r'([A-Z]\d{4})\.\d+', raw)
    if m:
        return m.group(1), 'HCPCS'

    # Pattern 4: Embedded HCPCS/CDT code in garbage (XJ0591X → J0591, AD9999 → D9999)
    m = re.search(r'(?<!\d)([A-Z]\d{4})(?!\d)', raw)
    if m:
        extracted = m.group(1)
        # D-prefix codes are CDT (dental), all others are HCPCS Level II
        ext_type = 'CDT' if extracted[0] == 'D' else 'HCPCS'
        return extracted, ext_type

    # Pattern 5: Embedded CPT code in garbage
    m = re.search(r'(?<![A-Z\d])(\d{5})(?!\d)', raw)
    if m:
        return m.group(1), 'CPT'

    # Pattern 6: Embedded PLA code in garbage (digits + letter suffix)
    m = re.search(r'(?<!\d)(\d{4}[A-Z])(?![A-Z\d])', raw)
    if m:
        return m.group(1), 'CPT'

    return None, None


def apply_code_extraction(
    code: Optional[str],
    code_type: Optional[str],
    config: Optional[Dict],
) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract standard codes from composite code strings based on config rules.

    Some hospitals (e.g., UCLA) encode multiple fields into a single composite
    "code" value like ``RRUCLA-7616710100-1000-67101-0761-8580-Y`` where a
    real CPT code (67101) is embedded at a known position.

    This function splits such composite codes and extracts the embedded
    standard code. The caller should then pass the result through
    ``normalize_code()`` for classification.

    Config format (the per-source config dict)::

        {
          "code_extraction": {
            "pattern": "^RRUCLA",      # regex - only codes matching this are processed
            "separator": "-",           # split character
            "code_position": 3,         # 0-indexed segment with the standard code
            "fallback_position": 1      # segment to use as CDM code when code_position is empty
          }
        }

    Returns: (extracted_code, extracted_code_type)
        - If extraction succeeds: the embedded code with code_type=None
          (caller should pass through ``normalize_code()`` for classification)
        - If code_position is empty: (parts[fallback_position], 'CDM')
        - If no config or no match: (code, code_type) unchanged
    """
    if not config or not code:
        return code, code_type

    extraction = config.get('code_extraction')
    if not extraction:
        return code, code_type

    pattern = extraction.get('pattern')
    if not pattern:
        return code, code_type

    code_stripped = code.strip()
    if not re.search(pattern, code_stripped):
        return code, code_type

    separator = extraction.get('separator', '-')
    parts = code_stripped.split(separator)
    pos = extraction.get('code_position', 3)

    # Extract the standard code from the expected position
    if pos < len(parts) and parts[pos]:
        return parts[pos], None

    # Fallback: use CDM ID from another position
    fb_pos = extraction.get('fallback_position', 1)
    if fb_pos < len(parts) and parts[fb_pos]:
        return parts[fb_pos], 'CDM'

    return code, code_type


def infer_billing_class(
    code: Optional[str],
    code_type: Optional[str],
    description: Optional[str],
    billing_class: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Infer billing_class and normalize code when not explicitly provided.
    
    Returns: (billing_class, normalized_code, code_type)
    
    Heuristic rules (applied only when billing_class is None):
    1. A-prefix on CPT/HCPCS codes: 'A' + a full 5-digit CPT = facility
       component. Strip the 'A' prefix, set billing_class = 'facility', fix
       code_type. Does NOT fire on 'A' + 4 digits - that shape is a genuine
       HCPCS Level II code, not a prefixed CPT.
    2. Description prefix 'PR ': professional component.
    3. Description prefix 'HC ' on RC items: facility component.
    4. Revenue codes (code_type='RC'): facility by definition.
    """
    normalized_code = code
    norm_type = code_type
    
    # If billing_class already set (from source data), don't override
    if billing_class:
        return billing_class, normalized_code, norm_type
    
    if not code and not description:
        return None, normalized_code, norm_type
    
    # Rule 1: A-prefix on CPT/HCPCS codes → facility, strip prefix
    # e.g., A19325 (HCPCS) → code=19325, code_type=CPT, billing_class=facility
    # e.g., A0237T (HCPCS) → code=0237T, code_type=HCPCS, billing_class=facility
    # The SUFFIX, not the whole code, has to be a valid CPT. A genuine HCPCS
    # Level II code is exactly letter + 4 digits (length 5), so the old
    # `len(code) >= 5` guard swallowed every A-prefixed Level II supply code:
    # A4322 (irrigation syringe) became CPT '4322', which is not a valid CPT
    # at all. Nothing re-ran normalize_code() on the result, so the corrupt
    # pair went straight into the output.
    if (code and code_type in ('CPT', 'HCPCS', None)
            and len(code) >= 6 and code[0] == 'A'):
        suffix = code[1:]
        if re.fullmatch(r'\d{5}', suffix):
            # A + 5-digit CPT → CPT (strip A, reclassify)
            return 'facility', suffix, 'CPT'
        if re.fullmatch(r'\d{4}T', suffix):
            # A + Category III → HCPCS (strip A, keep HCPCS)
            return 'facility', suffix, 'HCPCS'
    
    # Rule 2: Description starts with 'PR ' → professional
    # Common at O'Connor, John Muir: "PR CT Thorax Diag W Con"
    if description and description.startswith('PR '):
        return 'professional', normalized_code, norm_type
    
    # Rule 3: Revenue codes → facility (they are inherently facility charges)
    if code_type == 'RC':
        return 'facility', normalized_code, norm_type
    
    # Rule 4: Description starts with 'HC ' on non-RC items → facility
    # (RC items already caught by Rule 3)
    if description and description.startswith('HC '):
        return 'facility', normalized_code, norm_type
    
    return None, normalized_code, norm_type

