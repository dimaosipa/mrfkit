
"""Normalize payer and plan names.

The same insurer shows up as "AETNA HMO [1001103]", "JMPN AETNA", "Aetna Inc."
and "95% Aetna". ``normalize_payer_name`` reduces those to one canonical name
(plus any plan type it found in the payer string), ``is_self_pay_payer`` spots
the cash / uninsured price published as if it were a payer, and
``normalize_plan_name`` sorts free-text plan names into a small taxonomy.

Everything here is pure. Pass a ``ReferenceData`` to add your own payer
aliases or a match index of payers you already know.
"""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING, Dict, Optional, Tuple

if TYPE_CHECKING:
    from mrfkit.reference import ReferenceData

# ============================================================================
# PAYER NAME NORMALIZATION
# ============================================================================
# Deterministic rules to clean raw payer_name values from hospital MRF files.
# Problems addressed:
#   1. Embedded contract IDs:  "AETNA HMO [1001103]" -> "Aetna"
#   2. Hospital-specific prefixes: "JMPN AETNA [...]" -> "Aetna"
#   3. Product-line suffixes mixed into name: "BLUE SHIELD PPO" -> "Blue Shield"
#      (product line preserved as plan_type)
#   4. Junk entries: "#N/A", "GENERIC HMO", etc. -> filtered out
#   5. Inconsistent casing/spacing

# Contract ID pattern: [digits] or [digits+letters] at end of string
_CONTRACT_ID_RE = re.compile(r'\s*\[[\w]+\]\s*$')

# Bracket-wrapped name pattern: entire payer name in matching square or angle brackets
# e.g., "[Kaiser Foundation Health Plan, Inc.]" or "<Self-Pay>"
# Uses alternation to enforce matching pairs (no [Aetna> mismatches)
_BRACKET_WRAPPED_RE = re.compile(r'^\[(.+)\]$|^<(.+)>$')

# Self-pay / cash-pay payer names.  When a hospital publishes a rate with
# one of these as the "payer", it is really the cash / uninsured price and
# belongs in the item's discounted_cash_price, not in a payer rate.
_SELF_PAY_NAMES = frozenset({
    'SELF PAY',
    'SELF-PAY',
    'SELFPAY',
    'SELF PAYSELF-PAY',   # seen in Partners Healthcare data
    'CASH',
    'CASH PAY',
    'CASH PRICE',
    'CASH PRICE ESTIMATE',
    'CASH DISCOUNT',
    'PATIENT',
    'PATIENT PAY',
    'UNINSURED',
    'UNINSURED/SELF-PAY',
    'SELF-PAY/UNINSURED',
    'NO INSURANCE',
    'SELF',
    'GROSS CHARGES',      # some MRFs publish gross as a "payer"
    'GROSS CHARGE',
    'CDM DEFAULT',        # Charge Description Master - hospital list price
    'CDM',
    'CHARGE MASTER',
    'CHARGEMASTER',
    'SELF PAY/UNINSURED DISCOUNT',  # seen in William Bee Ririe Hospital
})


# Keyword substrings that indicate a self-pay / uninsured row even when the
# full label isn't in _SELF_PAY_NAMES.  Required so novel labels like
# "Self Pay Rate", "Cash/Self Pay Price", "Uninsured Patient Estimate",
# "Self-Pay (no insurance)" fold into discounted_cash_price instead of
# being stored as fake payers with plan_category='Other'.
#
# Conservative on purpose:
#   * SELF[ -]?PAY and UNINSURED are strong, unambiguous signals: no
#     legitimate insurer name contains them.
#   * CASH is NOT included as a substring (would mis-promote real names
#     like "Cash America Insurance", "BlueCash PPO").  Stand-alone CASH
#     and common CASH * combos are already covered by the exact-set above.
#
# Substrings are checked against the normalized uppercase ``working``
# value (after html-unescape, quote-strip, bracket-unwrap, underscore to
# space, whitespace collapse), so case and minor punctuation variations
# don't matter.
_SELF_PAY_KEYWORD_SUBSTRINGS = (
    'SELF PAY',
    'SELF-PAY',
    'SELFPAY',
    'UNINSURED',
)


def _contains_self_pay_keyword(upper_name: str) -> bool:
    """Helper used by both ``is_self_pay_payer`` and (transitively by)
    ``normalize_payer_name``'s ``%``-prefix branch to keep the two
    self-pay matchers in sync."""
    return any(kw in upper_name for kw in _SELF_PAY_KEYWORD_SUBSTRINGS)


def is_self_pay_payer(raw_payer: Optional[str]) -> bool:
    """Return True if the raw payer name represents a self-pay / cash price.

    The check is case-insensitive and handles bracket/angle-bracket wrapping.

    Strips a leading ``NN%`` (or ``NN.N%``) prefix before the exact-set /
    keyword check so this matcher agrees with :func:`normalize_payer_name`,
    which strips the same prefix. Without the strip, a row labeled e.g.
    ``"30% Cash"`` was silently lost: the fold-to-cash step gates on
    ``is_self_pay_payer`` (False because of the unstripped ``30%``), and
    ``normalize_payer_name`` then returned None (discard).

    In addition to the exact-set match against ``_SELF_PAY_NAMES``, accepts
    any name containing the keyword substrings ``SELF PAY``, ``SELF-PAY``,
    ``SELFPAY``, or ``UNINSURED``. Catches novel labels that aren't in the
    exact set (e.g. ``"Self Pay Rate"``, ``"Uninsured Patient Estimate"``)
    so they fold into ``discounted_cash_price`` rather than being stored as
    fake payers.  ``CASH`` is intentionally NOT a substring trigger: real
    insurer names like ``"Cash America Insurance"`` / ``"BlueCash PPO"``
    would otherwise mis-promote.  Stand-alone CASH and the common CASH *
    combos remain in the exact set.
    """
    if not raw_payer or not raw_payer.strip():
        return False
    working = html.unescape(str(raw_payer)).strip()
    working = working.strip('"\'\u201c\u201d\u2018\u2019`')
    if working.startswith('<') and working.endswith('>'):
        working = working[1:-1].strip()

    if _NUMERIC_PAYER_RE.match(working):
        return False
    # Unwrap brackets: [Self-Pay] or <Self-Pay>
    m = _BRACKET_WRAPPED_RE.match(working)
    if m:
        working = (m.group(1) or m.group(2)).strip()
    # Normalize underscores and collapse whitespace
    working = working.replace('_', ' ')
    working = re.sub(r'\s+', ' ', working).strip()
    # Strip a leading "NN%" / "NN.N%" so percentage-prefixed self-pay rows
    # match the same _SELF_PAY_NAMES set as unprefixed rows.  Mirrors
    # normalize_payer_name(); see _PCT_PREFIX_RE.
    pct_match = _PCT_PREFIX_RE.match(working)
    if pct_match:
        working = pct_match.group(2).strip()
    upper = working.upper()
    if upper in _SELF_PAY_NAMES:
        return True
    # Broaden detection to keyword substrings so novel self-pay labels
    # also fold into cash.
    return _contains_self_pay_keyword(upper)

# Hospital-specific prefixes to strip (case-insensitive, checked in order)
# Longer prefixes first to avoid partial matches
_HOSPITAL_PREFIXES = [
    'SHCA COMM ',       # Sutter Health Care Alliance - Commercial
    'HILLS ',           # Hills Physicians variant prefix
    'PSYCH ',           # Psychiatric variant prefix (e.g., "PSYCH BLUE SHIELD...")
    'JMPN ',            # John Muir Physician Network
    'NODE ',            # AdventHealth/CHS internal system prefix
    'MC GENERIC ',      # Plan-code prefix leaked into payer names
    'TUH ',             # Temple University Hospital source prefix
    'JNS ',             # Jefferson/Temple source prefix
    'CHH ',             # Catholic Health/Hospice source prefix
    'FCOD ',            # Facility-code prefix in NY payer extracts
    'NACC ',            # Facility-code prefix in NY payer extracts
    'JUP ',             # Facility-code prefix in NY payer extracts
    'STOD ',            # Facility-code prefix in NY payer extracts
    'PAYER ',           # Literal field-label prefix from malformed extracts
    'PRO ',             # Provider-system prefix, e.g. "PRO UHC Commercial"
]

# Product-line / network suffixes to extract as plan_type
# Order matters: longer patterns first to avoid partial matches
_PLAN_TYPE_SUFFIXES = [
    ' MEDICARE ADVANTAGE',
    ' MEDICARE ADV',
    ' MEDICARE A/B REBILL',
    ' MCR ADV - ALL PLANS',
    ' MCR ADV - ALL OTHER PLANS',
    ' MCR ADV',
    ' COMM - ALL OTHER PLANS',
    ' PPO/HPN - ALL OTHER PLANS',
    ' PPO/HMO-ALL OTHER PLANS',
    ' PPO-ALL OTHER PLANS',
    ' PPO - ALL OTHER PLANS',
    ' HMO-ALL OTHER PLANS',
    ' HMO - ALL OTHER PLANS',
    '-ALL OTHER PLANS',
    '-ALL PLANS',
    ' - ALL OTHER PLANS',
    ' - ALL PLANS',
    ' ALTERNATE PAYER',
    ' ALTERNATE PAYOR',
    ' ALT PAYOR',
    ' ALT PAYER',
    '-NETWORK MCARE',
    '-NETWORK',
    ' NETWORK SELECT',
    ' DOCTORS PLAN PPO/EP',
    ' DOCTORS PLAN',
    ' SELECT/NAVIGATE/CORE',
    ' TAILORED NETWORK',
    ' ENHANCEDCARE',
    ' CHOICE CARE',
    ' FEDERAL EMPLOYEES',
    ' NEW BUS OAP',
    ' MCARE',
    ' MEDICARE',
    ' HMO/IFP',
    ' HMO/PPO',
    ' HMO/POS',
    ' PPO/EPO',
    ' HMO',
    ' PPO',
    ' EPPO',
    ' EPO',
    ' POS',
    ' INDEMNITY',
    ' LIFE',
    ' MEDI-CAL',
]

# Junk payer names to discard entirely
_JUNK_PAYER_NAMES = {
    '#N/A',
    'N/A',
    '',
    'GENERIC COMMERCIAL/INDEMNITY',
    'GENERIC COMMERCIAL',
    'GENERIC HMO',
    'GENERIC PPO',
    'MEDICARE ADV GENERIC',
    'MEDI-CAL',         # bare "Medi-Cal" is a program, not a payer
}

# ---------- Regex-based junk / prefix patterns ----------

# Decimal/rate-like payer names: "27.07", "147.05", "0.80" - these are raw rates, not payers
# Matches strings that are purely a decimal number (possibly quoted).
_RATE_LIKE_PAYER_RE = re.compile(r'^"?\d+(?:\.\d+)?"?$')

# Punctuation-only payer names: "#", ".", "--", etc.
_PUNCT_ONLY_PAYER_RE = re.compile(r'^[^A-Za-z0-9]+$')

# Numeric-only payer names that are NOT legitimate union/local payer identifiers.
# Legitimate numeric payers (e.g. "1199 SEIU", "Local 104") contain non-digit words.
# This pattern matches strings that are *only* an integer with no alphabetic content.
_INTEGER_ONLY_PAYER_RE = re.compile(r'^\d+$')

# Percentage-prefixed payer names: "95% Anthem Blue Cross" -> payer="Anthem Blue Cross"
# The percentage is the negotiated rate as % of charges. These are real payers with
# a pricing methodology baked into the name.
_PCT_PREFIX_RE = re.compile(r'^(\d+(?:\.\d+)?)\s*%\s*(.+)$')

# Per-diem rate values used as payer names: "Abbhh 14/Day", "Abbhh 22.37/Day"
# These are rate tiers from Alexian Brothers Behavioral Health Hospital, not real payers.
_PER_DIEM_PAYER_RE = re.compile(r'^.+\s+\d+(?:\.\d+)?/DAY$', re.IGNORECASE)

# Embedded 8-digit date pattern: "Cofinity/Ppom 20120101 (St Mary)" -> "Cofinity/Ppom"
# Dates are contract effective dates baked into the payer name by some hospital systems.
# Also strips optional trailing " (St Mary)", " (St. Mary)", " Anderson", " Tlk ..."
_EMBEDDED_DATE_RE = re.compile(
    r'\s+\d{8}'                    # 8-digit date (e.g. 20120101)
    r'(?:\s+\(.*?\))?'            # optional (St Mary) / (St. Mary) suffix
    r'(?:\s+(?:Anderson|Tlk)\b.*)?'  # optional Anderson / Tlk trailing text
    r'\s*$',
    re.IGNORECASE,
)

# Trailing numeric IDs from hospital billing systems: "Fidelis 5155" -> "Fidelis"
# Matches 2-5 trailing digits that aren't part of a legitimate name.
# Negative lookbehind prevents stripping from union locals, product names.
_TRAILING_ID_RE = re.compile(
    r'\s+\d{2,5}$'                 # trailing 2-5 digits preceded by whitespace
)

# Names to EXCLUDE from trailing-ID stripping (legitimate numbers in name)
_TRAILING_ID_PRESERVE = frozenset({
    'IMAGINE 360',
    'ADVANTAGE 360',
    'PLAN 80',
    'PLAN 65',
    'PLAN 1000',
})

# Compound payer IDs: "Highmark Blue Cross Blue Shield Of Western Ny Medicaid 1702, ..."
# These are multi-ID strings from NY hospital systems. Just strip trailing ", NNNN" parts.
_COMPOUND_ID_RE = re.compile(
    r'(?:,\s*\S+\s+\d{3,5})+$'    # one or more ", Name NNNN" suffixes
)

# Workers Comp prefix pattern: "Wc Aetna" -> parent payer "Aetna", plan_category="Workers Comp"
_WC_PREFIX_RE = re.compile(r'^WC\s+(.+)$', re.IGNORECASE)

_NUMERIC_PAYER_RE = re.compile(r'^"?\d+(?:\.\d+)?"?$')

_PAYER_CONTRACT_STATUS_SUFFIX_RE = re.compile(
    r'\s+(?:NON[-\s]?CONTRACT(?:ED)?|CONTRACTED)$', re.IGNORECASE)

_PAYER_TRAILING_PLAN_SUFFIXES = [
    (' MEDICARE ADVANTAGE CONTRACTED', 'Medicare Advantage'),
    (' MEDICARE ADVANTAGE NON-CONTRACTED', 'Medicare Advantage'),
    (' MEDICARE MANAGED CARE', 'Medicare Advantage'),
    (' MEDICARE ADVANTAGE', 'Medicare Advantage'),
    (' MEDICARE CONTRACTED', 'Medicare'),
    (' MEDICARE NON-CONTRACTED', 'Medicare'),
    (' MEDICARE', 'Medicare'),
    (' MCARE', 'Medicare'),
    (' MCR', 'Medicare'),
    (' MEDICAID MANAGED CARE', 'Medicaid Managed Care'),
    (' MEDICAID CONTRACTED', 'Medicaid'),
    (' MEDICAID NON-CONTRACTED', 'Medicaid'),
    (' MEDICAID', 'Medicaid'),
    (' MEDI-CAL', 'Medi-Cal'),
    (' MCAL', 'Medi-Cal'),
    (' MCD', 'Medicaid'),
    (' COMMERCIAL CONTRACTED', 'Commercial'),
    (' COMMERCIAL NON-CONTRACTED', 'Commercial'),
    (' COMMERCIAL', 'Commercial'),
    (' HMO', 'HMO'),
    (' PPO', 'PPO'),
]

_CONCAT_PAYER_PLAN_PATTERNS = [
    ('unitedhealthcareoxford', 'United Healthcare'),
    ('unitedhealthcarecommty', 'United Healthcare'),
    ('unitedhealthcare', 'United Healthcare'),
    ('uhc', 'United Healthcare'),
    ('emblemhealthhip', 'EmblemHealth'),
    ('emblemhealthghi', 'EmblemHealth'),
    ('emblemhealth', 'EmblemHealth'),
    ('metroplushealth', 'MetroPlus'),
    ('metroplus', 'MetroPlus'),
    ('bcbstx', 'Blue Cross Blue Shield of Texas'),
    ('bcbsnc', 'Blue Cross Blue Shield of North Carolina'),
    ('bcbsmn', 'Blue Cross Blue Shield of Minnesota'),
    ('bluecrossblueshield', 'Blue Cross Blue Shield'),
    ('bluecross', 'Blue Cross Blue Shield'),
    ('blueshield', 'Blue Shield of California'),
    ('healthlink', 'HealthLink'),
    ('healthsmart', 'HealthSmart'),
    ('wellcare', 'WellCare'),
    ('care1st', 'Care 1st Health Plan'),
    ('carefirst', 'CareFirst Blue Cross Blue Shield'),
    ('aetna', 'Aetna'),
    ('anthem', 'Anthem Blue Cross'),
    ('cigna', 'Cigna'),
    ('humana', 'Humana'),
    ('kaiser', 'Kaiser Permanente'),
    ('medica', 'Medica'),
    ('moda', 'Moda Health'),
    ('mclaren', 'McLaren Health Plan'),
    ('meridian', 'Meridian'),
    ('amerigroup', 'Amerigroup'),
    ('optum', 'Optum'),
    ('ubh', 'United Behavioral Health'),
]

_CONCAT_PLAN_SUFFIXES = [
    ('medicareadvantage', 'Medicare Advantage'),
    ('medicare', 'Medicare'),
    ('mcare', 'Medicare'),
    ('mcr', 'Medicare'),
    ('medicaid', 'Medicaid'),
    ('mcaid', 'Medicaid'),
    ('mcal', 'Medi-Cal'),
    ('mcd', 'Medicaid'),
    ('commercial', 'Commercial'),
    ('comm', 'Commercial'),
    ('hmo', 'HMO'),
    ('ppo', 'PPO'),
]


def _clean_payer_key(value: Optional[str]) -> str:
    """Return the uppercase lookup key used for payer alias matching."""
    if not value:
        return ''
    cleaned = html.unescape(str(value)).strip()
    cleaned = cleaned.strip('"\'\u201c\u201d\u2018\u2019`')
    cleaned = cleaned.strip('<>')
    cleaned = cleaned.replace('_', ' ')
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned.upper()


# ---------------------------------------------------------------------------
# Payer-name match-fold: dedup of normalization variants.
#
# normalize_payer_name() resolves curated names via the payer aliases and
# _PAYER_CANONICAL_MAP, but anything not in those maps falls through to the
# Step-5 title-case fallback, which preserves trivial spelling variants:
# singular/plural ("Health Plan" vs "Health Plans"), trailing punctuation
# ("Inc" vs "Inc."), smart-quotes, stray whitespace/hyphens, truncated
# parens.  Two hospitals spelling the SAME payer slightly differently would
# each produce a distinct payer name.
#
# Fix: compute a conservative match key (case/punctuation/whitespace-folded,
# each token singularized) and, when that key already maps to an established
# payer in ``ReferenceData.payer_match_index``, fold the fallback name onto
# that payer's canonical name. The key is deliberately narrow: it folds
# plural/punctuation noise but never drops distinguishing words (state
# names, brands), so it cannot merge "Blue Cross of California" with
# "Blue Cross of Idaho".  Only the uncurated fallback path is folded;
# alias / canonical-map hits are trusted as-is.
# ---------------------------------------------------------------------------

_PAYER_MATCH_PUNCT_RE = re.compile(r"[^a-z0-9\s]")
_PAYER_MATCH_SPACE_RE = re.compile(r"\s+")


def _singularize_token(tok: str) -> str:
    """Strip a single trailing plural 's' from one token.

    Conservative: leaves ``ss`` words ("Express", "Wellness") and very short
    tokens (<=3 chars) untouched, so it folds "Plans"->"Plan",
    "Services"->"Service", "Benefits"->"Benefit", "Administrators"->
    "Administrator" without mangling distinct words.
    """
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def payer_match_key(name: Optional[str]) -> str:
    """Normalized key for collapsing payer-name spelling variants.

    Lowercases, replaces punctuation with spaces, collapses whitespace,
    singularizes each token, and uppercases.  Two names that differ ONLY by
    case, punctuation, whitespace/hyphens, or singular/plural map to the same
    key; names that differ by any real word (state, brand) do not.

        "Buckeye Community Health Plans" -> "BUCKEYE COMMUNITY HEALTH PLAN"
        "Buckeye Community Health Plan"  -> "BUCKEYE COMMUNITY HEALTH PLAN"
        "Wisconsin Physicians Service"   -> "WISCONSIN PHYSICIAN SERVICE"
        "Georgia Workers' Compensation"  -> "GEORGIA WORKER COMPENSATION"
    """
    if not name:
        return ""
    lowered = html.unescape(str(name)).lower()
    no_punct = _PAYER_MATCH_PUNCT_RE.sub(" ", lowered)
    collapsed = _PAYER_MATCH_SPACE_RE.sub(" ", no_punct).strip()
    if not collapsed:
        return ""
    singular = [_singularize_token(t) for t in collapsed.split(" ") if t]
    return " ".join(singular).upper()


def _fold_canonical_via_match(
    canonical: Optional[str],
    ref: Optional[ReferenceData] = None,
) -> Optional[str]:
    """Fold a fallback-derived canonical name onto an established payer.

    Returns the winning canonical name when this name's match key is
    already in ``ref.payer_match_index``; otherwise returns the input
    unchanged and records it in that index so later variants of a brand-new
    payer within the same run also collapse (first-seen is a stable winner).
    No-op when the index is empty, keeping normalize_payer_name() pure by
    default.
    """
    index = ref.payer_match_index if ref is not None else None
    if not canonical or not index:
        return canonical
    mk = payer_match_key(canonical)
    if not mk:
        return canonical
    winner = index.get(mk)
    if winner is not None:
        return winner
    index[mk] = canonical
    return canonical


# Canonical name mapping: normalized uppercase key -> canonical display name.
# Built from the payer names seen across thousands of real hospital files.
_PAYER_CANONICAL_MAP = {
    # ---- Major national insurers ----
    'AETNA': 'Aetna',
    'AETNA BETTER HEALTH DR': 'Aetna',
    'AETNA BETTERHEALTH VA MCD': 'Aetna',
    'AETNA CHOICE POS II SHC': 'Aetna',
    'AETNA GLOBAL BENEFITS': 'Aetna',
    'AETNA HEALTH OF CALIFORNIA INC.': 'Aetna',
    'AETNA HILL PHYSICIANS': 'Aetna',
    'AETNA HMO OTHER': 'Aetna',
    'AETNA INTERNATIONAL': 'Aetna',
    'AETNA LEWER MARK STUDENT INSURANCE': 'Aetna',
    'AETNA MANAGED CHOICE': 'Aetna',
    'AETNA MED ADV': 'Aetna',
    'AETNA MEDICARE SUPPLEMENT': 'Aetna',
    'AETNA NON CONTRACTED': 'Aetna',
    'AETNA OTHER': 'Aetna',
    'AETNA PAMF MPD': 'Aetna',
    'AETNA PPO': 'Aetna',
    'AETNA SENIOR': 'Aetna',
    'AETNA SENIOR HEALTH PLAN': 'Aetna',
    'AETNA STUDENT HEALTH': 'Aetna',
    'AETNA SUTTER HEALTH': 'Aetna',
    'AETNA SUTTER HEALTH SELF INSURED': 'Aetna',
    'AFFINITY MED GRP AETNA': 'Aetna',
    'ALT AETNA COMM AETNA': 'Aetna',
    'BROWN AND TOLAND MED GRP AETNA': 'Aetna',
    'BROWN AND TOLAND MG AETNA': 'Aetna',
    'DIGNITY HEALTHCARE AETNA': 'Aetna',
    'HILL PHYS MED GRP AETNA': 'Aetna',
    'JOHN MUIR HLTH NTWK AETNA': 'Aetna',
    'MARKSREIN SALES COMPANY AETNA': 'Aetna',
    'TRUSTMARK AETNA': 'Aetna',
    'VIRGINIA TOTAL CARE DSNP': 'Aetna',
    'VIRGINIA TOTAL CARE MCRA': 'Aetna',

    'AMBETTER': 'Ambetter',
    'AMBETTER CASCADE COMPLETE': 'Ambetter',
    'AMBETTER HEALTH': 'Ambetter',
    'AMBETTER HEALTH ATTN CLAIMS': 'Ambetter',

    '26056 BLUE CROSS SR MEDICARE ADVANTAGE P': 'Anthem Blue Cross',
    'ALT BLUE CROSS COMM': 'Anthem Blue Cross',
    'ANTHEM': 'Anthem Blue Cross',
    'ANTHEM AFFILIATES': 'Anthem Blue Cross',
    'ANTHEM BC': 'Anthem Blue Cross',
    'ANTHEM BC VA DUAL': 'Anthem Blue Cross',
    'ANTHEM BC VA EXCHANGE': 'Anthem Blue Cross',
    'ANTHEM BLUE CONNECTION': 'Anthem Blue Cross',
    'ANTHEM BLUE CROSS': 'Anthem Blue Cross',
    'ANTHEM BLUE CROSS BLUE SHIELD': 'Anthem Blue Cross',
    'ANTHEM BLUE CROSS HILL PHYSICIANS': 'Anthem Blue Cross',
    'ANTHEM BLUE CROSS MED ADV': 'Anthem Blue Cross',
    'ANTHEM BLUE CROSS MEDI-CAL DELEGATED': 'Anthem Blue Cross',
    'ANTHEM BLUE CROSS MEDI-CAL FFS': 'Anthem Blue Cross',
    'ANTHEM BLUE CROSS SPHS': 'Anthem Blue Cross',
    'ANTHEM MEDICAID': 'Anthem Blue Cross',
    'ANTHEM SSI': 'Anthem Blue Cross',
    'BC BS OF MA': 'Blue Cross Blue Shield of Massachusetts',
    'BC OF MA EXCHANGE': 'Blue Cross Blue Shield of Massachusetts',
    'BCBS - ANTHEM': 'Anthem Blue Cross',
    'BCMA BLUE BENEFIT ADMIN': 'Anthem Blue Cross',
    'BLUE CARD (OUT OF STATE)': 'Blue Cross Blue Shield',
    'BLUE CROSS': 'Blue Cross Blue Shield',
    'BLUE CROSS/BLUE SHIELD': 'Blue Cross Blue Shield',
    'BLUE CROSS BLUE SHIELD ASSOCIATION': 'Blue Cross Blue Shield',
    'BLUE CROSS ALTERNATE PAYOR': 'Blue Cross Blue Shield',
    'BLUE CROSS ANTHEM': 'Anthem Blue Cross',
    'BLUE CROSS CAL PERS SELECT': 'Anthem Blue Cross',
    'BLUE CROSS HEALTH PERFORMANCE NETWORK': 'Anthem Blue Cross',
    'BLUE CROSS HMO OTHER': 'Blue Cross Blue Shield',
    'BLUE CROSS MEDI CAL': 'Anthem Blue Cross',
    'BLUE CROSS MEDI-CAL': 'Anthem Blue Cross',
    'BLUE CROSS MEDICARE SUPPLEMENT': 'Blue Cross Blue Shield',
    'BLUE CROSS MR': 'Blue Cross Blue Shield',
    'BLUE CROSS NON CONTRACTED': 'Blue Cross Blue Shield',
    'BLUE CROSS OF CALIFORNIA': 'Anthem Blue Cross',
    'BLUE CROSS OF CALIFORNIA, DBA ANTHEM BLUE CROSS AND ITS AFFILIATES': 'Anthem Blue Cross',
    'BLUE CROSS OF MA': 'Blue Cross Blue Shield of Massachusetts',
    'BLUE CROSS ONOFF EXCHANGE': 'Blue Cross Blue Shield',
    'BLUE CROSS PPO': 'Blue Cross Blue Shield',
    'BLUE CROSS SEBMF': 'Anthem Blue Cross',
    'BLUE CROSS SELECT': 'Blue Cross Blue Shield',
    'BLUE CROSS SR IMPERIAL HEALTH PLAN': 'Anthem Blue Cross',
    'BLUE CROSS STUDENT HEALTH': 'Blue Cross Blue Shield',
    'BLUE CROSSNORTH EAST MEDICAL SERVICES': 'Anthem Blue Cross',
    'BLUE MEDI-CAL RIVER CITY MEDICAL GROUP': 'Anthem Blue Cross',
    'NEMS BLUE CROSS MCAL': 'Anthem Blue Cross',
    'VIVANT BLUE CROSS': 'Anthem Blue Cross',

    'BCBS': 'Blue Cross Blue Shield',
    'BCBS MA EXCHANGE': 'Blue Cross Blue Shield of Massachusetts',
    'BCBS OF MASS': 'Blue Cross Blue Shield of Massachusetts',
    'BLUE CROSS BLUE SHIELD': 'Blue Cross Blue Shield',
    'VERMONT BLUE ADVANTAGE': 'Blue Cross Blue Shield',

    'ALT BLUE SHIELD COMM': 'Blue Shield of California',
    'AMERICAN SPECIALTY': 'Blue Shield of California',
    'AMERICAN SPECIALTY HEALTH PLAN': 'Blue Shield of California',
    'AMERICAN SPECIALTY HEALTH PLAN ALTERNATE PAYER': 'Blue Shield of California',
    'BLUE CARD (OUT OF STATE) - BS': 'Blue Shield of California',
    'BLUE SHIELD': 'Blue Shield of California',
    'BLUE SHIELD ACO': 'Blue Shield of California',
    'BLUE SHIELD ALTERNATE PAYOR': 'Blue Shield of California',
    'BLUE SHIELD CA': 'Blue Shield of California',
    'BLUE SHIELD CAL PERS': 'Blue Shield of California',
    'BLUE SHIELD CALIFORNIA': 'Blue Shield of California',
    'BLUE SHIELD CALPERS SUPPLEMENT': 'Blue Shield of California',
    'BLUE SHIELD CCSF BTMG': 'Blue Shield of California',
    'BLUE SHIELD COMMERCIAL AND EPN': 'Blue Shield of California',
    'BLUE SHIELD COVER CA': 'Blue Shield of California',
    'BLUE SHIELD COVERED CA': 'Blue Shield of California',
    'BLUE SHIELD CPIC/EPO': 'Blue Shield of California',
    'BLUE SHIELD EPPO': 'Blue Shield of California',
    'BLUE SHIELD FEDERAL EMPLOYEE PROGRAM': 'Blue Shield of California',
    'BLUE SHIELD HMO OTHER': 'Blue Shield of California',
    'BLUE SHIELD HMO POS PAMF MPD': 'Blue Shield of California',
    'BLUE SHIELD LEASED NETWORK': 'Blue Shield of California',
    'BLUE SHIELD MCR ADVANTAGE': 'Blue Shield of California',
    'BLUE SHIELD MEDICARE SUPPLEMENT': 'Blue Shield of California',
    'BLUE SHIELD MR': 'Blue Shield of California',
    'BLUE SHIELD OF CALIFORNIA': 'Blue Shield of California',
    'BLUE SHIELD OUT OF STATE': 'Blue Shield of California',
    'BLUE SHIELD PPO': 'Blue Shield of California',
    'BLUE SHIELD SENIOR BROWN & TOLAND MG': 'Blue Shield of California',
    'BLUE SHIELD SENIOR OTHER': 'Blue Shield of California',
    'BLUE SHIELD SENIOR PLAN': 'Blue Shield of California',
    'BLUE SHIELD SFHSS NON-TRIO PAMF': 'Blue Shield of California',
    'BLUE SHIELD SFHSS NON-TRIO SWBM': 'Blue Shield of California',
    'BLUE SHIELD SR ALL CARE IPA': 'Blue Shield of California',
    'BLUE SHIELD SR PAMF': 'Blue Shield of California',
    'BLUE SHIELD STUDENT HEALTH': 'Blue Shield of California',
    'BLUE SHIELD TANDEM': 'Blue Shield of California',
    'BLUE SHIELD TRIO ABMG': 'Blue Shield of California',
    'BLUE SHIELD TRIO BROWN AND TOLAND': 'Blue Shield of California',
    'BLUE SHIELD UC DAVIS': 'Blue Shield of California',
    'BS OUT OF STATE HIGH PERFORMANCE': 'Blue Shield of California',
    'CALIFORNIA PHYSICIANS\' SERVICE, DBA BLUE SHIELD OF CALIFORNIA': 'Blue Shield of California',
    'SB SELECT IPA BLUE SHIELD': 'Blue Shield of California',
    'SFHSS BLUE SHIELD NON-TRIO': 'Blue Shield of California',

    'CENTENE': 'Centene',

    'ALT CIGNA COMM': 'Cigna',
    'CIGNA': 'Cigna',
    'CIGNA BEHAVIORAL HEALTH': 'Cigna',
    'CIGNA BH': 'Cigna',
    'CIGNA HEALTHCARE OF CALIFORNIA, INC. AND CIGNA HEALTH AND LIFE INSURANCE COMPANY': 'Cigna',
    'CIGNA HILL PHYSICIANS': 'Cigna',
    'CIGNA HMO OTHER': 'Cigna',
    'CIGNA INDEMNITY': 'Cigna',
    'CIGNA INTERNATIONAL': 'Cigna',
    'CIGNA LEASED NETWORK': 'Cigna',
    'CIGNA NON CONTRACTED': 'Cigna',
    'CIGNA OTHER': 'Cigna',
    'CIGNA PPO BEHAVIORAL HEALTH': 'Cigna',
    'CIGNAKAISER HAWAII': 'Cigna',

    'CORVEL': 'CorVel',
    'CORVEL CORPORATION': 'CorVel',
    'CORVEL HEALTHCARE CORPORATION': 'CorVel',

    'COVENTRY': 'Coventry',
    'COVENTRY FIRST HEALTH': 'Coventry',
    'COVENTRY HEALTH CARE NATIONAL NETWORK': 'Coventry',
    'COVENTRY HEALTHCARE': 'Coventry',
    'COVENTRY HEALTHCARE WC': 'Coventry',
    'ENLYTE/GENEX/COVENTRY': 'Coventry',

    'EMBLEM': 'EmblemHealth',
    'EMBLEM HEALTH': 'EmblemHealth',
    'EMBLEMHEALTH': 'EmblemHealth',

    'FIRST HEALTH': 'First Health',
    'FIRST HEALTH BROADSHIRE CCN CONCENTRA COVENTRY': 'First Health',
    'FIRST HEALTH MEDICAL RENTAL': 'First Health',
    'FIRST HEALTH/COVENTRY': 'First Health',

    'ALT HEALTH NET COMM': 'Health Net',
    'CA HEALTH AND WELLNESS': 'Health Net',
    'CA HEALTH AND WELLNESS COMMERCIAL': 'Health Net',
    'CA HEALTH AND WELLNESS FOLLOWS HEALTHNET DR': 'Health Net',
    'CA HEALTH AND WELLNESS MR': 'Health Net',
    'CALIFORNIA HEALTH WELLNESS': 'Health Net',
    'FOLLOWS HEALTHNET DR': 'Health Net',
    'HEALTH NET': 'Health Net',
    'HEALTH NET CENTENE IFP': 'Health Net',
    'HEALTH NET CENTENE PURECARE': 'Health Net',
    'HEALTH NET COMMERCIAL': 'Health Net',
    'HEALTH NET HMO OTHER': 'Health Net',
    'HEALTH NET MEDI CAL': 'Health Net',
    'HEALTH NET MEDI-CAL DMC': 'Health Net',
    'HEALTH NET OTHER': 'Health Net',
    'HEALTH NET PAMF MPD': 'Health Net',
    'HEALTH NET SMARTCARE': 'Health Net',
    'HEALTH NET SR PLUS': 'Health Net',
    'HEALTH NET SWBMG': 'Health Net',
    'HEALTH NET WELLCARE': 'Health Net',
    'HEALTH NET WELLCARE SR OTHER': 'Health Net',
    'HEALTH NET/HEALTHY FAMILY/AIM': 'Health Net',
    'HEALTHNET': 'Health Net',
    'HEALTHNET ENHANCEDCARE': 'Health Net',
    'HEALTHNET HMO/PPO': 'Health Net',
    'HEALTHNET HMO/PPO HILL PHYSICIANS': 'Health Net',
    'HEALTHNET OF CALIFORNIA, INC.': 'Health Net',
    'HEALTHNET TAILORED NETWORK': 'Health Net',

    'HEALTH LINK': 'HealthLink',
    'HEALTHLINK': 'HealthLink',
    'HEALTHLINK CONTRACTED': 'HealthLink',
    'HEALTHLINK INC': 'HealthLink',

    'HAP': 'HAP (Health Alliance Plan)',
    'HAP-HEALTH ALLIANCE PLAN': 'HAP (Health Alliance Plan)',
    'HEALTH ALLIANCE PLAN': 'HAP (Health Alliance Plan)',

    'HUMANA': 'Humana',
    'HUMANA CHOICE CARE': 'Humana',
    'HUMANA CHOICECARE': 'Humana',
    'HUMANA MEDICAID': 'Humana',
    'HUMANA MEDICARE SUPPLEMENT': 'Humana',
    'HUMANA NON-CONTRACTED': 'Humana',

    'KAISER': 'Kaiser Permanente',
    'KAISER FOUNDATION HEALTH PLAN': 'Kaiser Permanente',
    'KAISER FOUNDATION HEALTH PLAN OF WASHINGTON': 'Kaiser Permanente',
    'KAISER FOUNDATION HEALTH PLAN, INC.': 'Kaiser Permanente',
    'KAISER FOUNDATION HOSPITALS': 'Kaiser Permanente',
    'KAISER HMO NON-CONTRACTED': 'Kaiser Permanente',
    'KAISER MEDI CAL': 'Kaiser Permanente',
    'KAISER MEDI-CAL': 'Kaiser Permanente',
    'KAISER MEDI-CAL FFS': 'Kaiser Permanente',
    'KAISER MEDICAL': 'Kaiser Permanente',
    'KAISER NORTHERN CA': 'Kaiser Permanente',
    'KAISER OUT OF AREA': 'Kaiser Permanente',
    'KAISER PERMANENTE': 'Kaiser Permanente',
    'KAISER PERMANENTE SOUTHERN CA': 'Kaiser Permanente',

    'MAGELLAN': 'Magellan Health',
    'MAGELLAN BEHAVIORAL HEALTH SERVICES': 'Magellan Health',
    'MAGELLAN BH': 'Magellan Health',
    'MAGELLAN HEALTH': 'Magellan Health',

    'MOLINA': 'Molina Healthcare',
    'MOLINA COMMERCIAL': 'Molina Healthcare',
    'MOLINA HEALTHCARE': 'Molina Healthcare',
    'MOLINA HEALTHCARE INC': 'Molina Healthcare',
    'MOLINA HEALTHCARE OF CALIFORNIA': 'Molina Healthcare',
    'MOLINA HEALTHCARE OF TEXAS (CLAIMS ONLY)': 'Molina Healthcare',
    'MOLINA MEDI CAL': 'Molina Healthcare',
    'MOLINA NV': 'Molina Healthcare',

    'BEECH ST': 'MultiPlan',
    'BEECH STREET': 'MultiPlan',
    'BEECH STREET CORPORATION, F/K/A CAPP CARE, INC.': 'MultiPlan',
    'CLARITEV': 'MultiPlan',
    'MULTIPLAN': 'MultiPlan',
    'MULTIPLAN COMPLIMENTARY': 'MultiPlan',
    'MULTIPLAN COMPLIMENTARY MULTIPLAN VALUE POINT': 'MultiPlan',
    'MULTIPLAN COMPLIMENTARY VALUE POINT': 'MultiPlan',
    'MULTIPLAN PHCS NETWORK PHCS SAVILITY HEALTHEOS NETWORK': 'MultiPlan',
    'MULTIPLAN PPO': 'MultiPlan',
    'MULTIPLAN WC': 'MultiPlan',
    'MULTIPLAN WORK COMP': 'MultiPlan',
    'MULTIPLAN WORKERS COMP VALUE POINT': 'MultiPlan',
    'MULTIPLAN/PHCS': 'MultiPlan',
    'PHCS': 'MultiPlan',
    'PHCS MULTIPLAN': 'MultiPlan',
    'PHCS-MULTIPLAN': 'MultiPlan',
    'PHCS/PRIVATE HEALTHCARE SYSTEMS': 'MultiPlan',
    'PRIVATE HEALTHCARE SYS (PHCS)': 'MultiPlan',
    'PRIVATE HEALTH CARE SYSTEMS': 'MultiPlan',
    'PRIVATE HEALTHCARE SYSTEM': 'MultiPlan',
    'PRIVATE HEALTHCARE SYSTEMS': 'MultiPlan',
    'PRIVATE HEALTHCARE SYSTEMS PHCS': 'MultiPlan',

    'OPTUM': 'Optum',
    'OPTUM HEALTH CARE SOLUTIONS': 'Optum',
    'OPTUM HEALTH CARE SOLUTIONS, LLC': 'Optum',
    'OPTUM TRANSPLANTS OON': 'Optum',
    'OPTUMCARE': 'Optum',

    'OSCAR HEALTH': 'Oscar Health',
    'OSCAR HEALTH PLAN': 'Oscar Health',
    'OSCAR SELECT': 'Oscar Health',

    'UNITED BEHAVIORAL HEALTH': 'United Behavioral Health',
    'UNITED BH': 'United Behavioral Health',

    'AARP MEDICARE ADVANTAGE CHOICE': 'United Healthcare',
    'AARP MEDICARE COMPLETE UHC WEST': 'United Healthcare',
    'ALL SAVERS': 'United Healthcare',
    'ALT UNITED HEALTHCARE COMM': 'United Healthcare',
    'SB SELECT IPA UNITED HEALTHCARE': 'United Healthcare',
    'SIERRA HEALTH SERVICES': 'United Healthcare',
    'SIERRA HEALTH SERVICESUHC': 'United Healthcare',
    'UHC': 'United Healthcare',
    'UHC BTMG EAST (ABMG) HMO CAP': 'United Healthcare',
    'UHC CHOICE': 'United Healthcare',
    'UHC DOCTORS PLAN': 'United Healthcare',
    'UHC GLOBAL': 'United Healthcare',
    'UHC GOULD MG': 'United Healthcare',
    'UHC HMO OTHER MEDICARE SOLUTIONS': 'United Healthcare',
    'UHC NON-PARTICIPATING': 'United Healthcare',
    'UHC OF CALIFORNIA, DBA UNITEDHEALTHCARE OF CALIFORNIA AND FKA PACIFICCARE OF CALIFORNIA': 'United Healthcare',
    'UHC OTHER': 'United Healthcare',
    'UHC OXFORD HEALTH PLAN': 'United Healthcare',
    'UHC SR AFFINITY MG': 'United Healthcare',
    'UHC SR ALLCARE': 'United Healthcare',
    'UHC SR BTMG EAST (ABMG)': 'United Healthcare',
    'UHC SR HPMG': 'United Healthcare',
    'UHC SR PAMF MG': 'United Healthcare',
    'UHC SUREST': 'United Healthcare',
    'UHC SVALLIANCE': 'United Healthcare',
    'UHC VA CCN': 'United Healthcare',
    'UHC WEST COMM': 'United Healthcare',
    'UHP HEALTHCARE': 'United Healthcare',
    'UNITED': 'United Healthcare',
    'UNITED HEALTH CARE': 'United Healthcare',
    'UNITED HEALTH CARE PACIFICARE': 'United Healthcare',
    'UNITED HEALTHCARE': 'United Healthcare',
    'UNITED HEALTHCARE EMPIRE PLAN': 'United Healthcare',
    'UNITED HEALTHCARE EXCHANG': 'United Healthcare',
    'UNITED HEALTHCARE GLOBAL': 'United Healthcare',
    'UNITED HEALTHCARE GROUP': 'United Healthcare',
    'UNITED HEALTHCARE MCD': 'United Healthcare',
    'UNITED HEALTHCARE MEDICAR': 'United Healthcare',
    'UNITED HEALTHCARE NON CONTRACTED': 'United Healthcare',
    'UNITED HEALTHCARE OTHER': 'United Healthcare',
    'UNITED HEALTHCARE SUREST': 'United Healthcare',
    'UNITED HEATHCARE GLOBAL': 'United Healthcare',
    'UNITED SCO AND ONE CARE': 'United Healthcare',
    'UNITEDHEALTHCARE': 'United Healthcare',
    'UNITEDHEALTHCARE HMO HILL PHYSICIANS': 'United Healthcare',
    'UNITEDHEALTHCARE WEST': 'United Healthcare',

    # ---- Government programs ----
    'CA PRISON HEALTH CARE SERVICES': 'CA Prison Health Care Services',

    'CHAMPVA': 'CHAMPVA',

    'CALIFORNIA CORRECTIONAL HEALTH': 'Department of Corrections',
    'CALIFORNIA CORRECTIONAL HEALTH CARE HEALTH NET FEDERAL SERVICES': 'Department of Corrections',
    'DEPARTMENT OF CORRECTIONS': 'Department of Corrections',
    'DEPARTMENT OF CORRECTIONS NETWORK PROVIDER': 'Department of Corrections',
    'DEPT OF CORRECTION': 'Department of Corrections',
    'DEPT OF CORRECTIONS NETWORK PROV LLC': 'Department of Corrections',

    'FEDERAL CORRECTION INSTITUTE': 'Federal Correction Institute',

    'MEDI CAL': 'Medi-Cal',
    'MEDI-CAL NEMS PACE': 'Medi-Cal',
    'MEDI-CAL OP': 'Medi-Cal',
    'MEDICAL': 'Medi-Cal',
    'MEDICAL SHARE OF COST': 'Medi-Cal',

    'MEDICAID OUT OF STATE': 'Medicaid',

    'MEDICARE': 'Medicare',
    'MEDICARE ADVANTAGE OTHER': 'Medicare',
    'MEDICARE PART B ONLY': 'Medicare',

    'TRICARE': 'TRICARE',
    'TRICARE FOR': 'TRICARE',
    'TRICARE HEALTHNET': 'TRICARE',
    'TRICARE RESERVE': 'TRICARE',
    'TRICARE SELECT': 'TRICARE',
    'TRICARE WEST': 'TRICARE',
    'TRICARE WEST HEALTHNET': 'TRICARE',
    'TRIWEST COMMUNITY CARE NETWORK': 'TRICARE',
    'TRIWEST COMMUNITY CARE NETWORK COMMERCIAL': 'TRICARE',
    'TRIWEST HEALTHCARE ALLIANCE': 'TRICARE',

    'DEPARTMENT OF VETERANS AFFAIRS MI': 'Veterans Administration',
    'DEPARTMENT OF VETERANS AFFAIRS': 'Department of Veterans Affairs',
    'PGBA VA COMMUNITY CARE PROGRAM': 'Veterans Administration',
    'VA (TRIWEST CCN)': 'Veterans Administration',
    'VA ADMINISTRATION': 'Veterans Administration',
    'VA COMMUNITY CARE NETWORK': 'Veterans Administration',
    'VETERANS ADMINISTRATION': 'Veterans Administration',

    'ADVA NET WC': 'Workers\' Comp',
    'ADVA NET WC PARADIGM SPECIALTY NETWORK': 'Workers\' Comp',
    'ADVA-NET WORK COMP': 'Workers\' Comp',
    'CAREWORKS WORK COMP': 'Workers\' Comp',
    'WORKERS COMP': 'Workers\' Comp',
    'WORKER\'S COMP': 'Workers\' Comp',
    'WORKERS COMPENSATION': 'Workers\' Comp',
    'WORKERS\' COMP': 'Workers\' Comp',

    # ---- Regional / local / specialty plans ----
    'ALT': 'ALT Medicare',
    'ALT MEDICARE': 'ALT Medicare',
    'ALT MEDICARE A/B REBILL': 'ALT Medicare',
    'APS HEALTHCARE': 'APS Healthcare',
    'APS HEALTHCARE BETHESDA PSYCH': 'APS Healthcare',
    'ADVENTIST HEALTH': 'Adventist Health',
    'ADVENTIST HEALTH PLAN': 'Adventist Health',
    'AFFINITY MED GRP BLUE CROSS': 'Affinity Medical Group',
    'AFFINITY MED GRP BLUE SHIELD': 'Affinity Medical Group',
    'AFFINITY MED GRP HEALTH NET': 'Affinity Medical Group',
    'AFFINITY MED GRP UNITED HEALTHCARE': 'Affinity Medical Group',
    'AFFINITY MEDICAL GROUP': 'Affinity Medical Group',
    'AFFINITY MEDICAL GROUP COMM': 'Affinity Medical Group',
    'AFFINITY MG UC BLUE AND GOLD HEALTH NET': 'Affinity Medical Group',
    'ALT AFFINITY MED GRP': 'Affinity Medical Group',
    'ALT AFFINITY MEDICAL GRP COMM HMO BILLABLE': 'Affinity Medical Group',

    'AITHER HEALTH': 'Aither Health',
    'ALAMEDA ALLIANCE': 'Alameda Alliance',
    'CHCN ALAMEDA ALLIANCE': 'Alameda Alliance',
    'CHCN ALAMEDA ALLIANCE GROUP CARE': 'Alameda Alliance',
    'ALIGNMENT': 'Alignment Health Plan',
    'ALIGNMENT HEALTH PLAN': 'Alignment Health Plan',
    'ALIGNMENT MCARE ADV': 'Alignment Health Plan',
    'ALL CARE IPA ALIGNMENT MCARE ADV': 'AllCare IPA',
    'ALL CARE IPA BLS TRIO': 'AllCare IPA',
    'ALL CARE IPA HUMANA': 'AllCare IPA',
    'ALLCARE': 'AllCare IPA',
    'ALLCARE CCO': 'AllCare IPA',
    'ALLCARE IPA': 'AllCare IPA',
    'ALLCARE MG BLUE CROSS': 'AllCare IPA',
    'ALLCARE MG UNITED HEALTHCARE': 'AllCare IPA',
    'ALT ALL CARE IPA': 'AllCare IPA',
    'ALT ALL CARE IPA COMM': 'AllCare IPA',

    'ALLIANZ': 'Allianz',
    'ALLIED': 'Allied Benefit Systems',
    'ALLIED AFFORDABLE CARE PLAN': 'Allied Benefit Systems',
    'ALLIED BENEFIT SYSTEM, IN': 'Allied Benefit Systems',
    'ALLIED BENEFITS SYSTEM': 'Allied Benefit Systems',
    'ALLIED PACIFIC OF CALIFORNIA': 'Allied Benefit Systems',

    'ALLYALIGN HEALTH': 'AllyAlign Health',
    'ALPHA CARE': 'Alpha Care Medical Group',
    'ALPHA CARE MEDICAL GROUP': 'Alpha Care Medical Group',
    'ALTA BATES MEDICAL GRP ALT': 'Alta Bates Medical Group',
    'ALTAMED': 'AltaMed',
    'ALTAMED HEALTH SERVICES': 'AltaMed',
    'AMERIHEALTH': 'AmeriHealth',
    'AMERICAS CHOICE PROVIDER NETWORK': 'Americas Choice Provider Network',
    'AMERICAS CHOICE PROVIDER NETWORK AUTO': 'Americas Choice Provider Network',
    'AMERICAS CHOICE PROVIDER NETWORK MR': 'Americas Choice Provider Network',
    'AMERICAS CHOICE PROVIDER NETWORK WC': 'Americas Choice Provider Network',
    'AMERICAS CHOUCE PROVIDER NETWORK': 'Americas Choice Provider Network',

    'ASPIRE': 'Aspire Health Plan',
    'ASTIVA HEALTH': 'Astiva Health',
    'ASTIVA HEALTH SR': 'Astiva Health',
    'BMC HEALTHNET': 'BMC HealthNet',
    'BMC HEALTHNET EXCH SILVER': 'BMC HealthNet',
    'BMC HEALTHNET EXCHANGE': 'BMC HealthNet',
    'BOSTON MEDICAL CTR DUAL': 'BMC HealthNet',
    'BOSTON MEDICAL MEDICAID': 'BMC HealthNet',

    'BRMS': 'BRMS',
    'BANNER': 'Banner Health',
    'BEACON HEALTH OPTIONS': 'Beacon Health Strategies',
    'BEACON HEALTH STRATEGIES': 'Beacon Health Strategies',
    'BORREGO COMMUNITY HEALTH': 'Borrego Community Health',
    'BRAND NEW DAY': 'Brand New Day',
    'BRAND NEW DAY MR': 'Brand New Day',
    'BRANDMAN CENTERS FOR SENIOR CARE': 'Brandman Centers for Senior Care',
    'BRIGHT HEALTH': 'Bright Health Plan',
    'BRIGHT HEALTH PHYSICIANS OF PIH': 'Bright Health Plan',
    'BRIGHT HEALTH PLAN': 'Bright Health Plan',
    'ALT BROWN & TOLAND MG': 'Brown & Toland',
    'ALT BROWN AND TOLAND MED GRP COMM': 'Brown & Toland',
    'ALT BROWN AND TOLAND MED GRP MCAL': 'Brown & Toland',
    'ALT BROWN TOLAND MG SR HEALTH SERVICES': 'Brown & Toland',
    'ALT BTMG EAST (ABMG) COMM': 'Brown & Toland',
    'ALT SR BTMG EAST ABMG': 'Brown & Toland',
    'BROWN & TOLAND': 'Brown & Toland',
    'BROWN & TOLAND HEALTH SERVICES': 'Brown & Toland',
    'BROWN & TOLAND MED GRP BC': 'Brown & Toland',
    'BROWN & TOLAND MG ALT': 'Brown & Toland',
    'BROWN &TOLAND UC BLUE AND GOLD HEALTH NE': 'Brown & Toland',
    'BROWN AND TOLAN': 'Brown & Toland',
    'BROWN AND TOLAND': 'Brown & Toland',
    'BROWN AND TOLAND BLUE CROSS': 'Brown & Toland',
    'BROWN AND TOLAND EAST (ABMG) BLS': 'Brown & Toland',
    'BROWN AND TOLAND EAST (ABMG) BLS CPRS': 'Brown & Toland',
    'BROWN AND TOLAND MED GRP BLS': 'Brown & Toland',
    'BROWN AND TOLAND MED GRP BLS TRIO': 'Brown & Toland',
    'BROWN AND TOLAND MED GRP CIGNA': 'Brown & Toland',
    'BROWN AND TOLAND MED GRP HEALTH NET': 'Brown & Toland',
    'BROWN AND TOLAND MED GRP WELLCARE': 'Brown & Toland',
    'BROWN AND TOLAND MG ALIGNMENT MCARE ADV': 'Brown & Toland',
    'BROWN AND TOLAND MG BLS CAL PERS': 'Brown & Toland',
    'BROWN AND TOLAND UNITED HEALTHCARE': 'Brown & Toland',
    'BROWN TOLAND': 'Brown & Toland',
    'BTMG EAST (ABMG) BLUE CROSS': 'Brown & Toland',
    'BTMG EAST (ABMG) UC BLUE/GOLD HEALTH NET': 'Brown & Toland',
    'BTMG EAST (ABMG) UNITED HEALTHCARE': 'Brown & Toland',

    'ALT CCHCA-AAMG': 'CCHCA-AAMG',
    'ALT CCHCA-AAMG MCAL': 'CCHCA-AAMG',
    'CCHCA-AAMG BLUE CROSS': 'CCHCA-AAMG',
    'CCHP CHARITY': 'CCHP Charity',
    'CNA': 'CNA',
    'CNA (MAILHANDLERS)': 'CNA',
    'COVID-19 HRSA UNINSURED FUND': 'COVID-19 HRSA Uninsured Fund',
    'COVID19 HRSA UNINSURED TESTING AND TREATMENT FUND': 'COVID-19 HRSA Uninsured Fund',
    'CALOPTIMA': 'CalOptima',
    'CALVIVA': 'CalViva Health',
    'CALVIVA HEALTH': 'CalViva Health',
    'CALVOS SELECTCARE': 'CalVos SelectCare',
    'CALIFORNIA CHILDRENS SERVICES': 'California Childrens Services',
    'CA TRANSPLANT DONOR NETWORK': 'California Transplant Donor Network',
    'CALIFORNIA TRANSPLANT DONOR COMM': 'California Transplant Donor Network',
    'ALT CANOPY HEALTH': 'Canopy Health',
    'CANOPY': 'Canopy Health',
    'CANOPY HEALTH': 'Canopy Health',
    'CANOPY HILL PHYSICIANS': 'Canopy Health',

    'CANOPY HN': 'Canopy Health (Health Net)',
    'CANOPY UHC': 'Canopy Health (United Healthcare)',
    'CANOPY UNITED HEALTHCARE': 'Canopy Health (United Healthcare)',
    'CAPNET IPA': 'CapNet IPA',
    'CARE 1ST': 'Care 1st Health Plan',
    'CARE 1ST HEALTH PLAN': 'Care 1st Health Plan',
    'CARE1ST HEALTH PLAN': 'Care 1st Health Plan',
    'CARE ADVANTAGE': 'Care Advantage',
    'CARE ADVANTAGE SR PLAN': 'Care Advantage',
    'CARECENTRIX': 'CareCentrix',
    'CAREMORE': 'CareMore',
    'CARENET MEDICAID': 'CareNet',
    'FAMILY CARENET': 'CareNet',
    'CARELON BEHAVIORAL HEALTH NONCONTRACTED': 'Carelon Behavioral Health',
    'CENTER FOR ELDERS INDEPENDENCE': 'Center for Elders Independence',
    'CCAH': 'Central California Alliance for Health',
    'CENTRAL CA ALLIANCE FOR HEALTH DR': 'Central California Alliance for Health',
    'CENTRAL CA PHYS PARTNERS HEALTH NET': 'Central California Alliance for Health',
    'CENTRAL CALIFORNIA ALLIANCE FOR HEALTH': 'Central California Alliance for Health',

    'ALT CENTRAL HEALTH MEDICARE PLAN': 'Central Health Plan',
    'CENTAL HEALTH PLAN': 'Central Health Plan',
    'CENTRAL HEALTH MEDICARE PLAN': 'Central Health Plan',
    'CENTRAL HEALTH PLAN': 'Central Health Plan',
    'CENTRAL HEALTH PLAN OF CALIFORNIA': 'Central Health Plan',
    'CENTRAL HLTH': 'Central Health Plan',

    'CENTRAL VALLEY MEDICAL': 'Central Valley PACE',
    'CENTRAL VALLEY PACE': 'Central Valley PACE',
    'CHAMPION HEALTH PLAN': 'Champion Health Plan',
    'ALT CHINESE COMMUNITY HEALTH PLAN MCAL': 'Chinese Community Health Plan',
    'ALT JADE MG CHINESE COMMUNITY HP': 'Chinese Community Health Plan',
    'CHINESE COMMUNITY': 'Chinese Community Health Plan',
    'CHINESE COMMUNITY HEALTH PLAN': 'Chinese Community Health Plan',
    'CHINESE COMMUNITY HEALTH PLAN ACTIVE CHO': 'Chinese Community Health Plan',
    'CHINESE COMMUNITY HEALTH PLAN COVER CA H': 'Chinese Community Health Plan',
    'CHINESE COMMUNITY HEALTH PLAN SR JADE': 'Chinese Community Health Plan',
    'CHINESE COMMUNITY HP': 'Chinese Community Health Plan',
    'CHINESE HP': 'Chinese Community Health Plan',
    'CHN SF HEALTH PLAN': 'Chinese Community Health Plan',
    'JADE MG CHINESE COMMUNITY HP SR': 'Chinese Community Health Plan',

    'CHOICECARE NETWORK': 'ChoiceCare Network',
    'CLAREMONT BEHAVIORAL SERVICES': 'Claremont Behavioral Services',
    'CLEVER CARE': 'Clever Care Health Plan',
    'CLOVER INSURANCE CO': 'Clover Health',
    'COMMONWEALTH ALLIANCE MCR': 'Commonwealth Care Alliance',
    'COMMONWEALTH CARE': 'Commonwealth Care Alliance',
    'COMMONWEALTH CARE ALLIANCE': 'Commonwealth Care Alliance',
    'COMMONWEALTH CARE DUAL': 'Commonwealth Care Alliance',
    'COMMONWEALTH CARE MEDICAR': 'Commonwealth Care Alliance',

    'COMMUNITY HEALTH CHOICE': 'Community Health Choice',
    'COMMUNITY HOSPITAL OF MONTEREY PENINSULA': 'Community Hospital of Monterey Peninsula',
    'COMPCARE': 'CompCare',
    'CONCERTOPACE OF LOS ANGELES, LLC': 'ConcertoPACE',
    'CONNECTED CARE': 'Connected Care',
    'CONNECTICARE': 'ConnectiCare',
    'CONTIGO HEALTH': 'Contigo Health',
    'CONTRA COSTA COUNTY JAIL': 'Contra Costa County Jail',
    'CONTRA COSTA': 'Contra Costa Health Plan',
    'CONTRA COSTA COUNTY EMPLOYEES': 'Contra Costa Health Plan',
    'CONTRA COSTA HEALTH PLAN': 'Contra Costa Health Plan',
    'CONTRA COSTA HEALTH PLAN BASIC HEALTH CARE CCHP': 'Contra Costa Health Plan',
    'CONTRA COSTA HEALTH PLAN MEDI CAL': 'Contra Costa Health Plan',
    'CONTRA COSTA HEALTH PLAN MEDICAL': 'Contra Costa Health Plan',

    'COORDINATED CARE': 'Coordinated Care',
    'COUNTY OF SANTA CLARA': 'County of Santa Clara',
    'COVENTRY (CCN/FIRST HEALTH)': 'Coventry (First Health)',
    'DEAN HEALTH PLAN': 'Dean Health Plan',
    'DELTA HEALTH SYSTEM': 'Delta Health Systems',
    'DELTA HEALTH SYSTEMS': 'Delta Health Systems',
    'ALT DIGNITY HEALTHCARE COMM': 'Dignity Health',
    'DIGNITY HEALTH': 'Dignity Health',
    'DIGNITY HEALTH MR': 'Dignity Health',
    'DONOR NETWORK WEST': 'Donor Network West',
    'EMI HEALTH': 'EMI Health',
    'EASY CHOICE': 'Easy Choice Health Plan',
    'EASY CHOICE HEALTH PLAN': 'Easy Choice Health Plan',
    'EMPIRE PLAN': 'Empire Plan',
    'EPIC HEALTH PLAN': 'Epic Health Plan',
    'EPIC HEALTH PLAN IPA': 'Epic Health Plan',
    'ESSENCE HEALTHCARE': 'Essence Healthcare',
    'ETERNAL HEALTHCARE MCR': 'Eternal Healthcare',
    'FALLON COMMUNITY HEALTH': 'Fallon Health',
    'FALLON EXCHANGE': 'Fallon Health',
    'FALLON MEDICAID': 'Fallon Health',
    'FALLON PACE': 'Fallon Health',
    'FALLON SENIOR': 'Fallon Health',
    'FALLON SENIOR PLAN': 'Fallon Health',

    'FORTIFIED PROVIDER NETWORK': 'Fortified Provider Network',
    'GEHA': 'GEHA',
    'GMMI INC': 'GMMI',
    'GALAXY HEALTH NETWORK': 'Galaxy Health Network',
    'GLOBAL EXCEL MANAGEMENT': 'Global Excel Management',
    'GLOBAL EXCEL MANAGEMENT INC': 'Global Excel Management',
    'GLOBAL EXCEL MANGEMENT': 'Global Excel Management',
    'GLOBAL XL MANAGEMENT': 'Global Excel Management',

    'GOLD COAST HEALTH PLAN': 'Gold Coast Health Plan',
    'VENTURA COUNTY MEDI-CAL MANAGED CARE COMMISSION (DBA GOLD COAST HEALTH PLAN)': 'Gold Coast Health Plan',
    'GOLDEN STATE': 'Golden State Medicare',
    'GOLDEN STATE MEDICARE': 'Golden State Medicare',
    'GRAVIE': 'Gravie',
    'GRAVIE ADMINISTRATIVE': 'Gravie',
    'GRAVIE ADMINISTRATIVE SERVICES': 'Gravie',
    'GRAVIE INC': 'Gravie',
    'GRAVIE INS': 'Gravie',
    'GRAVIES ADMINISTRATIVE SERVICES': 'Gravie',

    'HARVARD PILGRIM': 'Harvard Pilgrim Healthcare',
    'HARVARD PILGRIM EXCHANGE': 'Harvard Pilgrim Healthcare',
    'HARVARD PILGRIM HEALTHCAR': 'Harvard Pilgrim Healthcare',
    'HARVARD PILGRIM HEALTHCARE': 'Harvard Pilgrim Healthcare',
    'UNITED HARVARD PILGRAM PC': 'Harvard Pilgrim Healthcare',
    'UNITED HARVARD PILGRIM PC': 'Harvard Pilgrim Healthcare',

    'HEALTH MANAGEMENT NETWORK': 'Health Management Network',
    'HEALTH NEW ENGLAND': 'Health New England',
    'HEALTH NEW ENGLAND EXCHAN': 'Health New England',
    'HEALTH NEW ENGLAND EXCHGE': 'Health New England',
    'HEALTH NEW ENGLAND MEDICA': 'Health New England',
    'HEALTH NEWENGLAND MEDICAI': 'Health New England',

    'HEALTH PLAN OF NEVADA': 'Health Plan of Nevada',
    'HEALTH PLAN OF SAN JOAQUIN': 'Health Plan of San Joaquin',
    'HEALTH PLAN SAN JOAQUIN': 'Health Plan of San Joaquin',
    'HEALTH PLAN SAN JOAQUIN COUNTY': 'Health Plan of San Joaquin',
    'HEALTH PLAN OF SAN MATEO': 'Health Plan of San Mateo',
    'HEALTH PLAN SAN MATEO': 'Health Plan of San Mateo',
    'HPSM': 'Health Plan of San Mateo',
    'SAN MATEO HEALTH COMMISSION': 'Health Plan of San Mateo',

    'HEALTH PLAN INC.': 'Health Plans Inc',
    'HEALTH PLANS INC': 'Health Plans Inc',
    'HEALTH COMP': 'HealthComp',
    'HEALTHCOMP': 'HealthComp',
    'HEALTH SMART INTERPLAN': 'HealthSmart',
    'HEALTHSMART': 'HealthSmart',
    'HEALTHSMART   PKA INTERPLAN': 'HealthSmart',
    'HEALTHSMART - PKA INTERPLAN': 'HealthSmart',
    'HEALTHSMART INTERPLAN': 'HealthSmart',
    'HEALTHSMART PREFERRED NETWORK': 'HealthSmart',

    'HEALTHCARE PARTNERS': 'Healthcare Partners',
    'HEALTHKEEPERS MEDICAID': 'Healthkeepers',
    'HERITAGE': 'Heritage Provider Network',
    'HERITAGE PROVIDER NETWORK': 'Heritage Provider Network',
    'HERITAGE PROVIDER NETWORK, INC.': 'Heritage Provider Network',
    'ALT HILL PHYS MED GRP COMM': 'Hill Physicians',
    'HILL PHYS MED GRP BLUE SHIELD': 'Hill Physicians',
    'HILL PHYS MED GRP CENTRAL HEALTH': 'Hill Physicians',
    'HILL PHYS MED GRP CHINESE COMMUNITY HP': 'Hill Physicians',
    'HILL PHYS MED GRP UNITED HEALTHCARE': 'Hill Physicians',
    'HILL PHYS MG BLUE SHIELD TRIO': 'Hill Physicians',
    'HILL PHYS MG SFHSS BLS SFHSS': 'Hill Physicians',
    'HILL PHYSCN MG UC BLUE & GOLD HEALTH NET': 'Hill Physicians',
    'HILL PHYSICIANS': 'Hill Physicians',
    'HILL PHYSICIANS CARE SOLUTIONS': 'Hill Physicians',
    'HILL PHYSICIANS MG BLUE CROSS': 'Hill Physicians',
    'HILL PHYSICIANS MG BLUE SHIELD': 'Hill Physicians',
    'HILL PHYSICIANS MG HEALTH NET': 'Hill Physicians',

    'HOME STATE HEALTH PLAN': 'Home State Health Plan',
    'HOMETOWN HEALTH': 'Hometown Health',
    'ASSISTED HOSPICE CARE': 'Hospice',
    'BRISTOL HOSPICE': 'Hospice',
    'BUENA VISTA HOSPICE': 'Hospice',
    'HOSPICE OF THE VALLEY': 'Hospice',
    'MISSION HH HOSPICE': 'Hospice',

    'HOSPICE OF EAST BAY': 'Hospice of East Bay',
    'IMAGINE 360 MEDICAL PLAN NETWORK ACCESS': 'Imagine360',
    'IMAGINE360': 'Imagine360',
    'IMPERIAL HEALTH': 'Imperial Health Plan',
    'IEHP': 'Inland Empire Health Plan',
    'INLAND EMPIRE HEALTH PLAN': 'Inland Empire Health Plan',
    'INTEGRATED HEALTH PLAN': 'Integrated Health Plan',
    'INTEGRATED HEALTHPLAN': 'Integrated Health Plan',
    'INTER VALLEY HEALTH PLAN': 'Inter Valley Health Plan',
    'INTERPLAN': 'Interplan',
    'INTERPLAN B-2': 'Interplan',
    'INTERPLAN B-4': 'Interplan',
    'INTERPLAN CORPORATION': 'Interplan',
    'INTERPLAN HEALTH GROUP': 'Interplan',

    'JOHN MUIR': 'John Muir Health',
    'JOHN MUIR HLTH NTWK UNITED HEALTHCARE': 'John Muir Health',
    'JOHN MUIR MG BLUE CROSS': 'John Muir Health',
    'JOHN MUIR MG BLUE SHIELD': 'John Muir Health',
    'JOHN MUIR MG CANOPY HEALTH UHC': 'John Muir Health',
    'JOHN MUIR MG HEALTH NET': 'John Muir Health',

    'LA CARE': 'L.A. Care Health Plan',
    'LA CARE HEALTH': 'L.A. Care Health Plan',
    'LA CARE HEALTH PLAN': 'L.A. Care Health Plan',
    'LUMINARE HEALTH': 'Luminare Health',
    'MVP HEALTHCARE': 'MVP Healthcare',
    'MAIL HANDLERS BENEFIT PLAN': 'Mail Handlers Benefit Plan',
    'MHBP': 'Mail Handlers Benefit Plan',
    'MANAGED HEALTH NETWORK': 'Managed Health Network',
    'MHN': 'Managed Health Network',
    'MARIN CANCER CARE': 'Marin Cancer Care',
    'MASS ADVANTAGE': 'Mass Advantage',
    'MASS GENERAL BRIGHAM': 'Mass General Brigham',
    'MASS GENERAL BRIGHAM EXCH': 'Mass General Brigham',
    'MASS GENERAL BRIGHAM MCD': 'Mass General Brigham',
    'MEDCORE': 'Medcore',
    'MEDCORE MG ALIGNMENT MCARE ADV': 'Medcore',
    'MEDICARE RAILROAD': 'Medicare Railroad',
    'MEDIGAP': 'Medigap',
    'MERCY PHYS MED GRP UHC': 'Mercy Physicians Medical Group',
    'MERITAGE': 'Meritage Medical Network',
    'MERITAGE HEALTH PLAN': 'Meritage Medical Network',
    'MERITAGE MEDICAL NETWORK': 'Meritage Medical Network',
    'MERITAGE MEDICAL NETWORK BS TRIO': 'Meritage Medical Network',
    'MERITAGE MEDICAL NETWORK HUMANA': 'Meritage Medical Network',
    'MERITAGE MEDICAL NETWORK UHC COMM': 'Meritage Medical Network',
    'MERITAGE MG BLUE SHIELD': 'Meritage Medical Network',

    'MERITAIN': 'Meritain Health',
    'MERITAIN HEALTH': 'Meritain Health',
    'MCLAREN HEALTH PLAN': 'McLaren Health Plan',
    'MCLAREN HEALTH PLAN INC': 'McLaren Health Plan',
    'MCLAREN HEALTHPLAN': 'McLaren Health Plan',
    'MODA': 'Moda Health',
    'MODA HEALTH': 'Moda Health',
    'MUTUAL OF OMAHA': 'Mutual of Omaha',
    'MUTUAL OF OMAHA MEDICARE SUPPLEMENT': 'Mutual of Omaha',
    'NAPHCARE': 'NaphCare',
    'NATIONAL PROVIDER NETWORK/PLANCARE AMERICA': 'National Provider Network',
    'NETCARE': 'Netcare',
    'NET HEALTH COMMONWEALTH C': 'Network Health',
    'NETWORK HEALTH': 'Network Health',
    'NETWORK HEALTH BELOW 65 D': 'Network Health',
    'NETWORK HEALTH EXCHANGE': 'Network Health',

    'NETWORK BY DESIGN': 'Networks By Design',
    'NETWORK BY DESIGN WORKERS COMP': 'Networks By Design',
    'NETWORKS BY DESIGN': 'Networks By Design',
    'NIVANO PHYS MED GRP BLUE CROSS': 'Nivano Physicians Medical Group',
    'NIVANO PHYSICIANS MED GRP BLUE CROSS': 'Nivano Physicians Medical Group',
    'NORTHBAY HEALTHCARE GROUP': 'NorthBay Healthcare',
    'ALT NORTHEAST MEDICAL SERVICES MCAL': 'Northeast Medical Services',
    'NEMS': 'Northeast Medical Services',
    'NEMS PACE': 'Northeast Medical Services',
    'NORTHEAST MEDICAL SERVICES': 'Northeast Medical Services',

    'ODS HEALTH PLAN': 'ODS Health Plan',
    'ON LOK': 'On Lok',
    'ON LOK SENIOR HEALTH': 'On Lok',
    'ON LOK SENIOR HEALTH SERVICES': 'On Lok',
    'ONLOK': 'On Lok',
    'ONLOK LIFEWAYS': 'On Lok',

    'ONE LEGACY': 'OneLegacy',
    'ONELEGACY': 'OneLegacy',
    'OPTIMA EXCHANGE': 'Optima Health',
    'OPTIMA FAMILY CARE': 'Optima Health',
    'OPTIMA SENTARA DUAL': 'Optima Health',
    'OPTIMA/SENTARA': 'Optima Health',

    'PPO NEXT': 'PPONext',
    'PPONEXT': 'PPONext',
    'PACIFIC SOURCE': 'PacificSource',
    'PACIFICSOURCE': 'PacificSource',
    'PACIFIC FOUNDATION': 'Pacific Foundation',
    'PACIFIC FOUNDATION FOR MEDICAL CARE': 'Pacific Foundation',
    'PACIFIC HEALTH ALLIANCE': 'Pacific Health Alliance',
    'PHA PACIFIC HEALTH ALLIANCE': 'Pacific Health Alliance',
    'ALT PALO ALTO MED GROUP FFS': 'Palo Alto Medical Foundation',
    'ALT PAMF COMMERCIAL': 'Palo Alto Medical Foundation',
    'ALT PAMF RHMO': 'Palo Alto Medical Foundation',
    'ALT PAMF RHMO COMM': 'Palo Alto Medical Foundation',
    'PALO ALTO MEDICAL': 'Palo Alto Medical Foundation',

    'PARADIGM MANAGEMENT': 'Paradigm',
    'PARADIGM WORK COMP': 'Paradigm',
    'PARTNERSHIP': 'Partnership Health Plan',
    'PARTNERSHIP HEALTH PLAN': 'Partnership Health Plan',
    'PARTNERSHIP HEALTH PLAN ADULT EXPANSION': 'Partnership Health Plan',
    'PERSONIFY HEALTH': 'Personify Health',
    'PREMIER HEALTH': 'Premier Health',
    'PREMIER HEALTH MEDICAID': 'Premier Health',
    'PREMIER HEALTH PLAN': 'Premier Health',
    'PREMIER IPA': 'Premier Health',

    'PRESBYTERIAN HEALTH PLAN': 'Presbyterian Health Plan',
    'PRIME CARE MEDICAL GROUP': 'Prime Health Services',
    'PRIME HEALTH SERVICES': 'Prime Health Services',
    'PRIMECARE': 'PrimeCare Medical Network',
    'PRIMECARE MEDICAL NETWORK': 'PrimeCare Medical Network',
    'PROCURA WC': 'Procura',
    'PROGYNY': 'Progyny',
    'AMVI/PROSPECT MEDICAL GROUP': 'Prospect Medical Group',
    'PROSPECT HEALTH': 'Prospect Medical Group',
    'PROSPECT HEALTH PLAN, INC.': 'Prospect Medical Group',
    'PROSPECT MEDICAL GROUP': 'Prospect Medical Group',

    'PROVIDENCE HEALTH NETWORK': 'Providence Health',
    'PROVIDER NETWORK OF AMERCIA': 'Provider Network of America',
    'PROVIDER NETWORK OF AMERICA': 'Provider Network of America',
    'PROVIDER NETWORK OF AMERICA WRAP TPA': 'Provider Network of America',
    'PROVIDERS NETWORK OF AMERICA': 'Provider Network of America',

    'PRUDENTIAL': 'Prudential',
    'REGAL MEDICAL GROUP': 'Regal Medical Group',
    'QUIK TRIP': 'QuikTrip',
    'QUIKTRIP': 'QuikTrip',
    'QUIKTRIP CORPORATION': 'QuikTrip',
    'SCAN': 'SCAN Health Plan',
    'SCAN HEALTH PLAN': 'SCAN Health Plan',
    'SCAN MEDICARE': 'SCAN Health Plan',
    'JADE MED GRP SAN FRANCISCO HEALTH PLAN': 'San Francisco Health Plan',
    'SAN FRANCISCO HEALTH PLAN': 'San Francisco Health Plan',
    'SAN FRANCISCO HEALTH PLAN - NON-CHN': 'San Francisco Health Plan',
    'HEALTH PLAN OF SANTA CLARA': 'Santa Clara Family Health Plan',
    'SANTA CLARA FAMILY HEALTH PLAN': 'Santa Clara Family Health Plan',
    'SANTA CLARA FAMILY HEALTH PLAN DUAL CONN': 'Santa Clara Family Health Plan',
    'SANTA CLARA IPA': 'Santa Clara IPA',
    'SANTA CLARA IPA COMM': 'Santa Clara IPA',
    'SANTA CLARA IPA MCR': 'Santa Clara IPA',
    'SANTA CLARA MG ALIGNMENT MCARE ADV': 'Santa Clara IPA',

    'SEASIDE HEALTH PLAN': 'Seaside Health Plan',
    'SELECT HEALTH': 'Select Health',
    'SENIOR WHOLE HEALTH': 'Senior Whole Health',
    'SEQUOIA': 'Sequoia',
    'SEVEN CORNERS': 'Seven Corners',
    'SIDECAR HEALTH': 'Sidecar Health',
    'SIERRA HEALTH AND': 'Sierra Health',
    'SIERRA PACE': 'Sierra Health',
    'STANDFORD HEALTH SERVICES': 'Stanford Health',
    'STANFORD HEALTH SERVICES': 'Stanford Health',
    'STANISLAUS COUNTY PARTNERS': 'Stanislaus County Partners in Health',
    'STANISLAUS COUNTY PARTNERS IN HEALTH': 'Stanislaus County Partners in Health',
    'STANISLAUS FOUNDATION': 'Stanislaus County Partners in Health',
    'SUPERIOR': 'Superior Health Plan',
    'SUPERIOR HEALTH PLAN': 'Superior Health Plan',
    'SUPERIOR HEALTHPLAN': 'Superior Health Plan',
    'ALT SEBMF COMMERCIAL': 'Sutter East Bay Medical Foundation',
    'ALT SEBMF RHMO': 'Sutter East Bay Medical Foundation',
    'ALT SEBMF RHMO COMM': 'Sutter East Bay Medical Foundation',
    'ALT SEBRH NON SUTTER HMO COMM': 'Sutter East Bay Medical Foundation',
    'SEBMF ALT': 'Sutter East Bay Medical Foundation',

    'ALT SGMF RHMO': 'Sutter Gould Medical Foundation',
    'ALT SGMF RHMO COMM': 'Sutter Gould Medical Foundation',
    'ALT SUTTER ADVANTAGE ALIGNMENT MCARE AHP': 'Sutter Health Plan',
    'SUTTER': 'Sutter Health Plan',
    'SUTTER HEALTH': 'Sutter Health Plan',
    'SUTTER HEALTH PLAN': 'Sutter Health Plan',
    'SUTTER HEALTH PLUS': 'Sutter Health Plan',
    'SUTTER HEALTH PLUS BTMG': 'Sutter Health Plan',
    'SUTTER HEALTH SEBMF': 'Sutter Health Plan',
    'SUTTER PREFERRED HEALTH PLAN': 'Sutter Health Plan',
    'SUTTER SELECT': 'Sutter Health Plan',
    'SUTTER SELECT (UMR)': 'Sutter Health Plan',
    'SUTTER VALLEY HOSP': 'Sutter Health Plan',

    'ALT SMF COMMERCIAL': 'Sutter Medical Foundation',
    'ALT SMF RHMO': 'Sutter Medical Foundation',
    'ALT SMF RHMO COMM': 'Sutter Medical Foundation',
    'ALT SPMF': 'Sutter Pacific Medical Foundation',
    'ALT SPMF COMMERCIAL': 'Sutter Pacific Medical Foundation',
    'ALT SPMF MEDICAL': 'Sutter Pacific Medical Foundation',
    'ALT SPMF RHMO': 'Sutter Pacific Medical Foundation',
    'ALT SPMF RHMO COMM': 'Sutter Pacific Medical Foundation',

    'TENET RECIPROCITY': 'Tenet Health',
    'THREE RIVERS': 'Three Rivers Provider Network',
    'THREE RIVERS AUTO': 'Three Rivers Provider Network',
    'THREE RIVERS COMMERCIAL': 'Three Rivers Provider Network',
    'THREE RIVERS NETWORK': 'Three Rivers Provider Network',
    'THREE RIVERS PROVIDER NETWORK': 'Three Rivers Provider Network',
    'THREE RIVERS PROVIDER NETWORK AUTO': 'Three Rivers Provider Network',

    'TRANSAMERICA LIFE INSURANCE CO': 'Transamerica',
    'TRISTAR': 'TriStar',
    'TRUSTMARK': 'Trustmark',
    'TRUSTMARK INSURANCE': 'Trustmark',
    'TUGO': 'TuGo',
    'TUGO INSURANCE': 'TuGo',
    'TUFTS': 'Tufts Health Plan',
    'TUFTS SECURE HORIZONS': 'Tufts Health Plan',
    'REGENTS UC DAVIS': 'UC Davis Health',
    'REGENTS UC DAVIS MR': 'UC Davis Health',
    'UC DAVIS SPECIALTY BLUE/GOLD HEALTH NET': 'UC Davis Health',
    'UC DAVIS SPECIALTY HEALTHNET': 'UC Davis Health',

    'UMR': 'UMR',
    'UMR NON-CONTRACTED': 'UMR',
    'USAA LIFE INSURANCE': 'USAA',
    'UNIVERSAL CARE': 'Universal Care',
    'VALLEY HEALTH PLAN': 'Valley Health Plan',
    'VALLEY HEALTH PLAN (MEDI-CAL)': 'Valley Health Plan',
    'VALLEY HEALTH PLAN COMMERCIAL': 'Valley Health Plan',
    'VALUE OPTIONS': 'Value Options',
    'VITALITY': 'Vitality Health Plan',
    'VITALITY HEALTH PLAN': 'Vitality Health Plan',
    'VITAS HEALTHCARE': 'Vitas Healthcare',
    'VITAS HEALTHCARE HOSPICE': 'Vitas Healthcare',
    'VIVANT HEALTH ALIGNMENT': 'Vivant Health',
    'WEB/TPA': 'WebTPA',
    'WEB-TPA': 'WebTPA',
    'WEB TPA': 'WebTPA',
    'WEBTPA': 'WebTPA',
    'ALT WELLCARE': 'WellCare',
    'WELLCARE': 'WellCare',
    'WELLCARE HEALTH INSURANCE OF ARIZONA': 'WellCare',
    'WELLCOMP': 'WellComp',
    'WELLFLEET': 'Wellfleet',
    'WELLPATH': 'Wellpath',
    'WELLPATH LOA': 'Wellpath',
    'WESTERN HEALTH ADVANTAGE': 'Western Health Advantage',

    # ---- AARP variants -> United Healthcare ----
    'AARP': 'United Healthcare',
    'AARP BY UHC': 'United Healthcare',
    'AARP DEICARE COMPLETE UHC': 'United Healthcare',  # typo: Dedicare
    'AARP MCR': 'United Healthcare',
    'AARP MC WELLMED': 'United Healthcare',
    'AARP MEDICARE REPLACEMENT': 'United Healthcare',
    'AARP SUPPLEMENT': 'United Healthcare',

    # ---- Typo corrections ----
    'AENTA': 'Aetna',
    '4MOST HEALTH NEWORK': '4Most Health Network',
    'BC HEALTH OPITONS': 'Blue Cross Blue Shield',
    'GREENVBRIER SPORTING CLUB': 'Greenbrier Sporting Club',
    'MANHATTENLIFE ASSURANCE COMPANY': 'Manhattan Life Assurance Company',
    'MUTUTAL OF OMAHA': 'Mutual of Omaha',
    'HUMANNA OK MEDICAID': 'Humana',
    'MULITPLAN': 'MultiPlan',
    'PACIFISOURCE': 'PacificSource',
    'SELCT HEALTH': 'Select Health',
    'SCOTT ADN WHITE': 'Scott and White',
    'UNIVERSAL HEALTH NETOWRK': 'Universal Health Network',
    'MEFCHANTS BENEFIT ADMIN': 'Merchants Benefit Administration',
    'UNITEDHEATHCARE INSURANCE COMPANY': 'United Healthcare',
    'UNITEDHEATHCARE': 'United Healthcare',
    'VERITY HEATLHNET': 'Health Net',
    'MOLINDA IOWA MCD': 'Molina Healthcare',
    'IAMOLINA': 'Molina Healthcare',
    'CITYOFFTWORTH': 'City of Fort Worth',
    "MARTYIN'S POINT": "Martin's Point",
    'UHEALTH MILTARY AND VETS': 'UHealth',
    'POLKIN HEALTH': 'Plotkin Health',
    'BMC WELLCARE SCO AND ONE CAREL': 'WellCare',
    'WELMED': 'WellMed',

    # ---- Near-duplicate consolidation ----
    'ADVA NET': 'Adva-Net',
    'ADVA-NET': 'Adva-Net',
    'AV MED': 'AvMed',
    'AVMED': 'AvMed',
    'HEALTH PARTNERS': 'HealthPartners',
    'HEALTHPARTNERS': 'HealthPartners',
    'HEALTH SCOPE': 'Healthscope',
    'HEALTHSCOPE': 'Healthscope',
    'SELECTHEALTH': 'Select Health',
    'FIRSTCARE': 'FirstCare',
    'FIRST CARE': 'FirstCare',
    'FIRSTHEALTH': 'First Health Network',
    'HEALTH CHOICE': 'HealthChoice',
    'HEALTHCHOICE': 'HealthChoice',
    'HEALTH SMART': 'HealthSmart',
    'CORESOURCE': 'CoreSource',
    'CORE SOURCE': 'CoreSource',
    'CHOICECARE': 'ChoiceCare',
    'CHOICE CARE': 'ChoiceCare',
    'CAREPLUS': 'CarePlus',
    'CARE PLUS': 'CarePlus',
    'CAREOREGON': 'CareOregon',
    'CARE OREGON': 'CareOregon',
    'HEALTHFIRST': 'Healthfirst',
    'HEALTH FIRST': 'Healthfirst',
    'MDSAVE': 'MDsave',
    'MD SAVE': 'MDsave',

    # ---- Node-prefix target payers ----
    # (After NODE strip, these need explicit mapping)
    'BCBS AL': 'Blue Cross Blue Shield of Alabama',
    'BCBS FL': 'Florida Blue',
    'BCFL': 'Florida Blue',
    'MEDICARE NON PAR': 'Medicare',
    'MEDICARE TRADITIONAL': 'Medicare',
    'NON PAR HOSPICE AGREE': 'Hospice',
    'UNITED OPTUM VA CCN': 'United Healthcare',
    'US DEPT OF LABOR': 'US Department of Labor',
    'VA': 'Department of Veterans Affairs',
    'SIMPLY': 'Simply Healthcare',
    'CIGNA HEALTHSPRING': 'Cigna',
    'CLOVER HEALTH': 'Clover Health',
    'CORRECTIONAL RISK SERVICES': 'Correctional Risk Services',
    'DEVOTED HEALTH': 'Devoted Health',
    'FREEDOM HEALTH': 'Freedom Health',
    'PRIME HEALTH': 'Prime Health',
    'WINDSOR': 'Windsor Health',
    'WELLCARE AMBETTER MARKETPLACE': 'Ambetter',

    # ---- BCBS state-specific variants (fixed) ----
    'BC OF ARKANSAS': 'Blue Cross Blue Shield of Arkansas',
    'BC OF AZ': 'Blue Cross Blue Shield of Arizona',
    'BC OF CA': 'Anthem Blue Cross',  # Blue Cross of California IS Anthem
    'BC OF CO': 'Blue Cross Blue Shield of Colorado',
    'BC OF IDAHO': 'Blue Cross of Idaho',
    'BC OF ILLINOIS': 'Blue Cross Blue Shield of Illinois',
    'BC OF KANSAS': 'Blue Cross Blue Shield of Kansas',
    'BC OF KY': 'Blue Cross Blue Shield of Kentucky',  # Anthem BCBS KY
    'BC OF MISSOURI': 'Blue Cross Blue Shield of Missouri',
    'BC OF NH': 'Blue Cross Blue Shield of New Hampshire',
    'BC OF NM': 'Blue Cross Blue Shield of New Mexico',
    'BC OF NV': 'Blue Cross Blue Shield of Nevada',
    'BC OF OHIO': 'Blue Cross Blue Shield of Ohio',
    'BC OF TN': 'Blue Cross Blue Shield of Tennessee',
    'BC OF TX': 'Blue Cross Blue Shield of Texas',
    'BC OF WV': 'Blue Cross Blue Shield of West Virginia',  # Highmark BCBS WV
    'BCBS-AL': 'Blue Cross Blue Shield of Alabama',
    'BCBS-FL': 'Florida Blue',
    'BC/BS OF FL': 'Florida Blue',
    'BCTN': 'Blue Cross Blue Shield of Tennessee',
    'BC FEP': 'Blue Cross Blue Shield',  # Federal Employee Program
    'BC HEALTH OPTIONS': 'Blue Cross Blue Shield',

    # ---- Special / residual payer names ----
    'ALL OTHER': 'Non-Contracted Payers',
    'NON-CONTRACTED PAYERS': 'Non-Contracted Payers',
    'ALEXIAN BROTHERS SCHOOL PACKAGE': 'Alexian Brothers School Package',

    # ---- BCBS licensee consolidation ----
    # Highmark (PA/WV/DE/WNY)
    'HIGHMARK': 'Highmark Blue Cross Blue Shield',
    'HIGHMARK BCBS': 'Highmark Blue Cross Blue Shield',
    'HIGHMARK BLUE CROSS': 'Highmark Blue Cross Blue Shield',
    'HIGHMARK BLUE CROSS BLUE SHIELD': 'Highmark Blue Cross Blue Shield',
    'HIGHMARK BLUE CROSS BLUE SHIELD OF WESTERN NY': 'Highmark Blue Cross Blue Shield',
    'HIGHMARK BLUE SHIELD': 'Highmark Blue Cross Blue Shield',
    'HIGHMARK BCBS COMMERCIAL': 'Highmark Blue Cross Blue Shield',
    'HIGHMARK BCBS DE': 'Highmark Blue Cross Blue Shield',
    'HIGHMARK BCBS MEDICARE': 'Highmark Blue Cross Blue Shield',
    'HIGHMARK BCBS MEDICAID': 'Highmark Blue Cross Blue Shield',
    'HIGHMARK BCBS WNY': 'Highmark Blue Cross Blue Shield',
    'HIGHMARK BLUE SHIELD COMMERCIAL': 'Highmark Blue Cross Blue Shield',
    'HIGHMARK CHOICE': 'Highmark Blue Cross Blue Shield',
    'HIGHMARK COMMUNITY BLUE': 'Highmark Blue Cross Blue Shield',
    'HIGHMARK FREEDOM BLUE': 'Highmark Blue Cross Blue Shield',
    'HIGHMARK SECURITY BLUE': 'Highmark Blue Cross Blue Shield',
    'HIGHMARK WHOLECARE': 'Highmark Blue Cross Blue Shield',
    'HIGHMARK DIRECT': 'Highmark Blue Cross Blue Shield',
    'HIGHMARK SELECT': 'Highmark Blue Cross Blue Shield',
    'HIGHMARK INC': 'Highmark Blue Cross Blue Shield',

    # CareFirst (MD/DC/VA)
    'CAREFIRST': 'CareFirst Blue Cross Blue Shield',
    'CAREFIRST BCBS': 'CareFirst Blue Cross Blue Shield',
    'CAREFIRST BLUE CROSS': 'CareFirst Blue Cross Blue Shield',
    'CAREFIRST BLUE CROSS BLUE SHIELD': 'CareFirst Blue Cross Blue Shield',
    'CAREFIRST BLUECHOICE': 'CareFirst Blue Cross Blue Shield',
    'CAREFIRST COMMERCIAL': 'CareFirst Blue Cross Blue Shield',
    'CAREFIRST MEDICARE': 'CareFirst Blue Cross Blue Shield',
    'CAREFIRST MEDICAID': 'CareFirst Blue Cross Blue Shield',
    'GROUP HOSPITALIZATION AND MEDICAL SERVICES': 'CareFirst Blue Cross Blue Shield',
    'GHMSI': 'CareFirst Blue Cross Blue Shield',

    # Florida Blue (FL BCBS licensee)
    'FLORIDA BLUE': 'Florida Blue',
    'FL BLUE': 'Florida Blue',
    'FLORIDA BLUE COMMERCIAL': 'Florida Blue',
    'FLORIDA BLUE MEDICARE': 'Florida Blue',
    'FLORIDA BLUE MEDICAID': 'Florida Blue',
    'GUIDEWELL': 'Florida Blue',

    # Premera Blue Cross (WA/AK)
    'PREMERA': 'Premera Blue Cross',
    'PREMERA BLUE CROSS': 'Premera Blue Cross',
    'PREMERA BCBS': 'Premera Blue Cross',

    # Regence (OR/UT/WA/ID)
    'REGENCE': 'Regence Blue Cross Blue Shield',
    'REGENCE BCBS': 'Regence Blue Cross Blue Shield',
    'REGENCE BLUE CROSS': 'Regence Blue Cross Blue Shield',
    'REGENCE BLUE CROSS BLUE SHIELD': 'Regence Blue Cross Blue Shield',
    'REGENCE BLUE SHIELD': 'Regence Blue Cross Blue Shield',
    'REGENCE BLUESHIELD': 'Regence Blue Cross Blue Shield',
    'REGENCE BCBS OF OREGON': 'Regence Blue Cross Blue Shield',
    'REGENCE BCBS OF UTAH': 'Regence Blue Cross Blue Shield',
    'REGENCE BLUE SHIELD OF IDAHO': 'Regence Blue Cross Blue Shield',

    # Elevance Health (Anthem parent, sometimes appears separately)
    'ELEVANCE': 'Anthem Blue Cross',
    'ELEVANCE HEALTH': 'Anthem Blue Cross',

    # Independence Blue Cross (PA)
    'INDEPENDENCE BLUE CROSS': 'Independence Blue Cross',
    'IBC': 'Independence Blue Cross',
    'INDEPENDENCE BCBS': 'Independence Blue Cross',
    'IBX': 'Independence Blue Cross',
    'KEYSTONE HEALTH PLAN': 'Independence Blue Cross',
    'KEYSTONE HEALTH PLAN EAST': 'Independence Blue Cross',

    # Horizon BCBS (NJ)
    'HORIZON': 'Horizon Blue Cross Blue Shield of New Jersey',
    'HORIZON BCBS': 'Horizon Blue Cross Blue Shield of New Jersey',
    'HORIZON BLUE CROSS': 'Horizon Blue Cross Blue Shield of New Jersey',
    'HORIZON BLUE CROSS BLUE SHIELD': 'Horizon Blue Cross Blue Shield of New Jersey',
    'HORIZON BLUE CROSS BLUE SHIELD OF NEW JERSEY': 'Horizon Blue Cross Blue Shield of New Jersey',
    'HORIZON NJ': 'Horizon Blue Cross Blue Shield of New Jersey',

    # Wellmark BCBS (IA/SD)
    'WELLMARK': 'Wellmark Blue Cross Blue Shield',
    'WELLMARK BCBS': 'Wellmark Blue Cross Blue Shield',
    'WELLMARK BLUE CROSS': 'Wellmark Blue Cross Blue Shield',
    'WELLMARK BLUE CROSS BLUE SHIELD': 'Wellmark Blue Cross Blue Shield',

    # UPMC Health Plan (PA)
    'UPMC': 'UPMC Health Plan',
    'UPMC HEALTH PLAN': 'UPMC Health Plan',
    'UPMC COMMERCIAL': 'UPMC Health Plan',
    'UPMC FOR LIFE': 'UPMC Health Plan',
    'UPMC FOR YOU': 'UPMC Health Plan',
    'UPMC MEDICARE': 'UPMC Health Plan',
    'UPMC MEDICAID': 'UPMC Health Plan',
    'UPMC FOR KIDS': 'UPMC Health Plan',
    'UPMC COMMUNITY HEALTH CHOICES': 'UPMC Health Plan',
    'UPMC INSURANCE SERVICES': 'UPMC Health Plan',
    'UPMC VISION ADVANTAGE': 'UPMC Health Plan',

    # Fidelis Care (NY Medicaid managed care)
    'FIDELIS': 'Fidelis Care',
    'FIDELIS CARE': 'Fidelis Care',
    'FIDELIS CARE NY': 'Fidelis Care',
    'FIDELIS COMMERCIAL': 'Fidelis Care',
    'FIDELIS MEDICAID': 'Fidelis Care',
    'FIDELIS MEDICARE': 'Fidelis Care',
    'FIDELIS ESSENTIAL PLAN': 'Fidelis Care',
    'FIDELIS CHILD HEALTH PLUS': 'Fidelis Care',

    # MVP Healthcare (NY/VT)
    'MVP': 'MVP Healthcare',
    'MVP HEALTH CARE': 'MVP Healthcare',
    'MVP COMMERCIAL': 'MVP Healthcare',
    'MVP MEDICARE': 'MVP Healthcare',
    'MVP MEDICAID': 'MVP Healthcare',

    # CareSource (OH/IN/GA/KY)
    'CARESOURCE': 'CareSource',
    'CARE SOURCE': 'CareSource',
    'CARESOURCE OHIO': 'CareSource',
    'CARESOURCE INDIANA': 'CareSource',
    'CARESOURCE MARKETPLACE': 'CareSource',
    'CARESOURCE MEDICAID': 'CareSource',
    'CARESOURCE MEDICARE': 'CareSource',

    # Medical Mutual of Ohio
    'MMO': 'Medical Mutual of Ohio',
    'MEDICAL MUTUAL': 'Medical Mutual of Ohio',
    'MEDICAL MUTUAL OF OHIO': 'Medical Mutual of Ohio',
    'MED MUTUAL': 'Medical Mutual of Ohio',
    'SUPERMED': 'Medical Mutual of Ohio',

    # ---- State BCBS entities (new) ----
    'BCBS OF MASSACHUSETTS': 'Blue Cross Blue Shield of Massachusetts',
    'BLUE CROSS BLUE SHIELD OF MASSACHUSETTS': 'Blue Cross Blue Shield of Massachusetts',
    'BCBS OF MA': 'Blue Cross Blue Shield of Massachusetts',
    'BLUE CROSS OF MASSACHUSETTS': 'Blue Cross Blue Shield of Massachusetts',

    'BCBS OF COLORADO': 'Blue Cross Blue Shield of Colorado',
    'BLUE CROSS BLUE SHIELD OF COLORADO': 'Blue Cross Blue Shield of Colorado',

    'BCBS OF KENTUCKY': 'Blue Cross Blue Shield of Kentucky',
    'BLUE CROSS BLUE SHIELD OF KENTUCKY': 'Blue Cross Blue Shield of Kentucky',

    'BCBS OF MISSOURI': 'Blue Cross Blue Shield of Missouri',
    'BLUE CROSS BLUE SHIELD OF MISSOURI': 'Blue Cross Blue Shield of Missouri',

    'BCBS OF NEW HAMPSHIRE': 'Blue Cross Blue Shield of New Hampshire',
    'BLUE CROSS BLUE SHIELD OF NEW HAMPSHIRE': 'Blue Cross Blue Shield of New Hampshire',

    'BCBS OF NEVADA': 'Blue Cross Blue Shield of Nevada',
    'BLUE CROSS BLUE SHIELD OF NEVADA': 'Blue Cross Blue Shield of Nevada',

    'BCBS OF OHIO': 'Blue Cross Blue Shield of Ohio',
    'BLUE CROSS BLUE SHIELD OF OHIO': 'Blue Cross Blue Shield of Ohio',

    'BCBS OF WEST VIRGINIA': 'Blue Cross Blue Shield of West Virginia',
    'BLUE CROSS BLUE SHIELD OF WEST VIRGINIA': 'Blue Cross Blue Shield of West Virginia',

    'BCBS OF MICHIGAN': 'Blue Cross Blue Shield of Michigan',
    'BLUE CROSS BLUE SHIELD OF MICHIGAN': 'Blue Cross Blue Shield of Michigan',
    'BC OF MICHIGAN': 'Blue Cross Blue Shield of Michigan',

    'BCBS OF MINNESOTA': 'Blue Cross Blue Shield of Minnesota',
    'BLUE CROSS BLUE SHIELD OF MINNESOTA': 'Blue Cross Blue Shield of Minnesota',
    'BC OF MINNESOTA': 'Blue Cross Blue Shield of Minnesota',

    'BCBS OF NORTH CAROLINA': 'Blue Cross Blue Shield of North Carolina',
    'BLUE CROSS BLUE SHIELD OF NORTH CAROLINA': 'Blue Cross Blue Shield of North Carolina',
    'BC OF NC': 'Blue Cross Blue Shield of North Carolina',

    'BCBS OF SOUTH CAROLINA': 'Blue Cross Blue Shield of South Carolina',
    'BLUE CROSS BLUE SHIELD OF SOUTH CAROLINA': 'Blue Cross Blue Shield of South Carolina',
    'BC OF SC': 'Blue Cross Blue Shield of South Carolina',

    'BCBS OF GEORGIA': 'Blue Cross Blue Shield of Georgia',
    'BLUE CROSS BLUE SHIELD OF GEORGIA': 'Blue Cross Blue Shield of Georgia',
    'BC OF GA': 'Blue Cross Blue Shield of Georgia',

    'BCBS OF LOUISIANA': 'Blue Cross Blue Shield of Louisiana',
    'BLUE CROSS BLUE SHIELD OF LOUISIANA': 'Blue Cross Blue Shield of Louisiana',
    'BC OF LA': 'Blue Cross Blue Shield of Louisiana',

    'BCBS OF NEBRASKA': 'Blue Cross Blue Shield of Nebraska',
    'BLUE CROSS BLUE SHIELD OF NEBRASKA': 'Blue Cross Blue Shield of Nebraska',
    'BC OF NE': 'Blue Cross Blue Shield of Nebraska',

    'BCBS OF WYOMING': 'Blue Cross Blue Shield of Wyoming',
    'BLUE CROSS BLUE SHIELD OF WYOMING': 'Blue Cross Blue Shield of Wyoming',
    'BC OF WY': 'Blue Cross Blue Shield of Wyoming',

    # ---- State Medicaid ----
    'MEDICAID AZ': 'Medicaid - Arizona',
    'MEDICAID ARIZONA': 'Medicaid - Arizona',
    'AZ MEDICAID': 'Medicaid - Arizona',
    'MEDICAID CA': 'Medicaid - California',
    'MEDICAID CALIFORNIA': 'Medicaid - California',
    'CA MEDICAID': 'Medicaid - California',
    'MEDICAID CO': 'Medicaid - Colorado',
    'MEDICAID COLORADO': 'Medicaid - Colorado',
    'CO MEDICAID': 'Medicaid - Colorado',
    'MEDICAID CT': 'Medicaid - Connecticut',
    'MEDICAID CONNECTICUT': 'Medicaid - Connecticut',
    'CT MEDICAID': 'Medicaid - Connecticut',
    'MEDICAID FL': 'Medicaid - Florida',
    'MEDICAID FLORIDA': 'Medicaid - Florida',
    'FL MEDICAID': 'Medicaid - Florida',
    'MEDICAID GA': 'Medicaid - Georgia',
    'MEDICAID GEORGIA': 'Medicaid - Georgia',
    'GA MEDICAID': 'Medicaid - Georgia',
    'MEDICAID IL': 'Medicaid - Illinois',
    'MEDICAID ILLINOIS': 'Medicaid - Illinois',
    'IL MEDICAID': 'Medicaid - Illinois',
    'MEDICAID IN': 'Medicaid - Indiana',
    'MEDICAID INDIANA': 'Medicaid - Indiana',
    'IN MEDICAID': 'Medicaid - Indiana',
    'MEDICAID KY': 'Medicaid - Kentucky',
    'MEDICAID KENTUCKY': 'Medicaid - Kentucky',
    'KY MEDICAID': 'Medicaid - Kentucky',
    'MEDICAID LA': 'Medicaid - Louisiana',
    'MEDICAID LOUISIANA': 'Medicaid - Louisiana',
    'LA MEDICAID': 'Medicaid - Louisiana',
    'MEDICAID MD': 'Medicaid - Maryland',
    'MEDICAID MARYLAND': 'Medicaid - Maryland',
    'MD MEDICAID': 'Medicaid - Maryland',
    'MEDICAID MI': 'Medicaid - Michigan',
    'MEDICAID MICHIGAN': 'Medicaid - Michigan',
    'MI MEDICAID': 'Medicaid - Michigan',
    'MEDICAID MN': 'Medicaid - Minnesota',
    'MEDICAID MINNESOTA': 'Medicaid - Minnesota',
    'MN MEDICAID': 'Medicaid - Minnesota',
    'MEDICAID MO': 'Medicaid - Missouri',
    'MEDICAID MISSOURI': 'Medicaid - Missouri',
    'MO MEDICAID': 'Medicaid - Missouri',
    'MEDICAID NC': 'Medicaid - North Carolina',
    'MEDICAID NORTH CAROLINA': 'Medicaid - North Carolina',
    'NC MEDICAID': 'Medicaid - North Carolina',
    'MEDICAID NJ': 'Medicaid - New Jersey',
    'MEDICAID NEW JERSEY': 'Medicaid - New Jersey',
    'NJ MEDICAID': 'Medicaid - New Jersey',
    'MEDICAID NV': 'Medicaid - Nevada',
    'MEDICAID NEVADA': 'Medicaid - Nevada',
    'NV MEDICAID': 'Medicaid - Nevada',
    'MEDICAID NY': 'Medicaid - New York',
    'MEDICAID NEW YORK': 'Medicaid - New York',
    'NY MEDICAID': 'Medicaid - New York',
    'MEDICAID OH': 'Medicaid - Ohio',
    'MEDICAID OHIO': 'Medicaid - Ohio',
    'OH MEDICAID': 'Medicaid - Ohio',
    'MEDICAID OK': 'Medicaid - Oklahoma',
    'MEDICAID OKLAHOMA': 'Medicaid - Oklahoma',
    'OK MEDICAID': 'Medicaid - Oklahoma',
    'MEDICAID OR': 'Medicaid - Oregon',
    'MEDICAID OREGON': 'Medicaid - Oregon',
    'OR MEDICAID': 'Medicaid - Oregon',
    'MEDICAID PA': 'Medicaid - Pennsylvania',
    'MEDICAID PENNSYLVANIA': 'Medicaid - Pennsylvania',
    'PA MEDICAID': 'Medicaid - Pennsylvania',
    'MEDICAID SC': 'Medicaid - South Carolina',
    'MEDICAID SOUTH CAROLINA': 'Medicaid - South Carolina',
    'SC MEDICAID': 'Medicaid - South Carolina',
    'MEDICAID TN': 'Medicaid - Tennessee',
    'MEDICAID TENNESSEE': 'Medicaid - Tennessee',
    'TN MEDICAID': 'Medicaid - Tennessee',
    'MEDICAID TX': 'Medicaid - Texas',
    'MEDICAID TEXAS': 'Medicaid - Texas',
    'TX MEDICAID': 'Medicaid - Texas',
    'MEDICAID UT': 'Medicaid - Utah',
    'MEDICAID UTAH': 'Medicaid - Utah',
    'UT MEDICAID': 'Medicaid - Utah',
    'MEDICAID WA': 'Medicaid - Washington',
    'MEDICAID WASHINGTON': 'Medicaid - Washington',
    'WA MEDICAID': 'Medicaid - Washington',
    'MEDICAID WI': 'Medicaid - Wisconsin',
    'MEDICAID WISCONSIN': 'Medicaid - Wisconsin',
    'WI MEDICAID': 'Medicaid - Wisconsin',
    'MEDICAID WV': 'Medicaid - West Virginia',
    'MEDICAID WEST VIRGINIA': 'Medicaid - West Virginia',
    'WV MEDICAID': 'Medicaid - West Virginia',
    'MEDICAID DC': 'Medicaid - District of Columbia',
    'MEDICAID DISTRICT OF COLUMBIA': 'Medicaid - District of Columbia',
    'DC MEDICAID': 'Medicaid - District of Columbia',

    # Fix existing state Medicaid entries
    'MEDICAID MA': 'Medicaid - Massachusetts',
    'MEDICAID PENDING MA': 'Medicaid - Massachusetts',
    'MEDICAID VA': 'Medicaid - Virginia',
    'MEDICAID PENDING VA': 'Medicaid - Virginia',
    'MEDICAID PENDNG GA': 'Medicaid - Georgia',

    # ---- Garbage/employer/non-insurer to meaningful names ----
    # CDM Default (Charge Description Master - not a payer)
    'CDM DEFAULT': 'Gross Charges',
    'CDM': 'Gross Charges',
    'CHARGE MASTER': 'Gross Charges',
    'CHARGEMASTER': 'Gross Charges',
    'GROSS CHARGES': 'Gross Charges',
    'GROSS CHARGES ESTIMATE': 'Gross Charges',

    # Cash/self-pay estimates
    'CASH PRICE ESTIMATE': 'Cash Price',
    'CASH PRICE': 'Cash Price',
    'SELF PAY': 'Cash Price',
    'SELF-PAY': 'Cash Price',
    'SELFPAY': 'Cash Price',
    'UNINSURED': 'Cash Price',
    'UNINSURED/SELF-PAY': 'Cash Price',

    # ---- Abbreviation expansions ----
    '90 DEGREE BENEFIT': '90 Degree Benefits',
    '90 DEGREE BENEFITS': '90 Degree Benefits',
    'PFC': 'Patient Financial Communications',
    'OHCP': 'Ohio Health Choice',
    'USAMCO': 'USA Managed Care',

    # ---- Wellpoint -> Elevance/Anthem ----
    # Wellpoint is the old name for Elevance Health (Anthem parent)
    'WELLPOINT': 'Anthem Blue Cross',
    'WELLPOINT INDEMNITY': 'Anthem Blue Cross',
    'WELLPOIONT': 'Anthem Blue Cross',
}



def normalize_payer_name(
    raw_payer: str,
    raw_plan: Optional[str] = None,
    *,
    ref: Optional[ReferenceData] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Normalize a raw payer_name from hospital MRF data.

    Returns: (canonical_payer_name, enriched_plan_name)
      - canonical_payer_name: cleaned, standardized name (or None to discard)
      - enriched_plan_name: original plan_name, enriched with product-line
        info extracted from payer_name if plan_name was empty

    Normalization steps:
      1. Strip bracket-wrapped names and contract IDs
      1c. Discard per-diem rate values ("Abbhh 14/Day") -> single payer
      1d. Handle percentage-prefixed ("95% Cigna") -> strip %, recurse
      1e. Strip embedded 8-digit dates and hospital suffixes
      1f. Strip trailing numeric IDs from billing systems
      1g. Handle Workers Comp prefix ("Wc Aetna") -> parent payer + WC plan
      2. Strip hospital prefixes: "JMPN AETNA", "NODE AETNA" -> "AETNA"
      3. Extract plan type suffix: "AETNA HMO" -> name="AETNA", plan_type="HMO"
      4. Look up canonical name: "AETNA" -> "Aetna"
         4b. Try stripping trailing "HEALTH PLAN" / "HEALTHCARE" / etc.
         4c. Try stripping ", INC." / ", LLC" / etc.
         4d. Progressive prefix fallback: strip trailing words one at a time
      5. Title-case fallback if not in map
      6. Enrich plan_name with extracted plan_type if plan was empty

    ``ref.payer_aliases`` (raw alias -> canonical name) are checked before
    the built-in map; ``ref.payer_match_index`` folds spelling variants of
    uncurated payers onto a name you already use (see
    ``_fold_canonical_via_match``).
    """
    if not raw_payer or not raw_payer.strip():
        return None, raw_plan

    aliases: Dict[str, str] = ref.payer_aliases if ref is not None else {}

    working = html.unescape(str(raw_payer)).strip()
    working = working.strip('"\'\u201c\u201d\u2018\u2019`')
    if working.startswith('<') and working.endswith('>'):
        working = working[1:-1].strip()

    if _NUMERIC_PAYER_RE.match(working):
        return None, raw_plan

    # Step 0: Unwrap bracket-enclosed names: "[Kaiser Foundation Health Plan, Inc.]"
    m = _BRACKET_WRAPPED_RE.match(working)
    if m:
        working = (m.group(1) or m.group(2)).strip()

    # Step 1: Strip contract IDs  [1001103], [1640000007], etc.
    working = _CONTRACT_ID_RE.sub('', working).strip()

    # Step 1a: Replace underscores with spaces (some MRFs use underscores
    # as word separators, e.g. "BLUE_SHIELD_OF_CALIFORNIA")
    working = working.replace('_', ' ')

    # Uppercase for matching
    upper = working.upper()

    alias_match = aliases.get(_clean_payer_key(working))
    if alias_match:
        return alias_match, raw_plan

    # Step 1b: Check junk list (after stripping contract IDs)
    if upper in _JUNK_PAYER_NAMES:
        return None, raw_plan

    # Step 1b2: Reject decimal/rate-like values ("27.07", "147.05", quoted variants)
    if _RATE_LIKE_PAYER_RE.match(working):
        return None, raw_plan

    # Step 1b3: Reject punctuation-only values ("#", "--", etc.)
    if _PUNCT_ONLY_PAYER_RE.match(working):
        return None, raw_plan

    # Step 1b4: Reject bare integer-only values ("1199" alone without alphabetic context).
    # Legitimate numeric payers always have words (e.g. "1199 SEIU", "Local 104"); those
    # pass through the title-case fallback or canonical map and are preserved.
    if _INTEGER_ONLY_PAYER_RE.match(working):
        return None, raw_plan

    # Step 1c: Discard per-diem rate values used as payer names
    # "Abbhh 14/Day", "Abbhh 22.37/Day": these are rate tiers, not payers
    if _PER_DIEM_PAYER_RE.match(upper):
        return 'Alexian Brothers School Package', raw_plan

    # Step 1d: Handle percentage-prefixed payer names
    # "95% Anthem Blue Cross" -> payer="Anthem Blue Cross", percentage stored separately
    pct_match = _PCT_PREFIX_RE.match(working)
    if pct_match:
        payer_after_pct = pct_match.group(2).strip()
        upper_after = payer_after_pct.upper()
        # "100% All Other" -> Non-Contracted Payers (regex strips "100%" prefix,
        # so we only need to check for "ALL OTHER" here)
        if upper_after == 'ALL OTHER':
            return 'Non-Contracted Payers', raw_plan
        # "30% Self Pay/Uninsured Discount" -> route to self-pay
        if any(kw in upper_after for kw in ('SELF PAY', 'SELF-PAY', 'SELFPAY',
                                             'UNINSURED', 'CASH')):
            return None, raw_plan  # discard: self-pay rates
        # Otherwise, recursively normalize the payer portion
        canonical, enriched = normalize_payer_name(payer_after_pct, raw_plan, ref=ref)
        return canonical, enriched

    # Step 1e: Strip embedded 8-digit dates and trailing hospital suffixes
    # "Cofinity/Ppom 20120101 (St Mary)" -> "Cofinity/Ppom"
    working = _EMBEDDED_DATE_RE.sub('', working).strip()
    upper = working.upper()

    # Step 1f: Strip trailing numeric IDs from hospital billing systems
    # "Fidelis 5155" -> "Fidelis", but preserve "Imagine 360", "Local 104"
    if upper not in _TRAILING_ID_PRESERVE and 'LOCAL ' not in upper:
        # Strip compound IDs first so NY-style multi-ID suffixes still match intact
        # "Highmark BCBS Medicaid 1702, Another 5143" -> "Highmark BCBS Medicaid"
        working = _COMPOUND_ID_RE.sub('', working).strip()
        working = _TRAILING_ID_RE.sub('', working).strip()
        upper = working.upper()

    # Step 1g: Handle "Workers Comp" prefix: "Wc Aetna" -> "Aetna" with WC plan
    wc_match = _WC_PREFIX_RE.match(working)
    if wc_match:
        wc_payer_part = wc_match.group(1).strip()
        # Recursively normalize the parent payer
        canonical, _ = normalize_payer_name(wc_payer_part, None, ref=ref)
        if canonical:
            # Enrich plan with Workers Comp category
            wc_plan = raw_plan if raw_plan else 'Workers Comp'
            return canonical, wc_plan
        # If parent is unrecognizable, keep as-is with Workers Comp prefix
        return f'Workers Comp - {wc_payer_part.title()}', raw_plan or 'Workers Comp'

    # Step 2: Strip hospital-specific prefixes (includes NODE)
    for prefix in _HOSPITAL_PREFIXES:
        if upper.startswith(prefix):
            working = working[len(prefix):].strip()
            upper = working.upper()
            break

    alias_match = aliases.get(_clean_payer_key(working))
    if alias_match:
        return alias_match, raw_plan

    # Step 2b: Strip contract status suffixes when the base is recognizable.
    status_stripped = _PAYER_CONTRACT_STATUS_SUFFIX_RE.sub('', working).strip()
    if status_stripped and status_stripped != working:
        status_upper = status_stripped.upper()
        status_canonical = (aliases.get(_clean_payer_key(status_stripped))
                            or _PAYER_CANONICAL_MAP.get(status_upper))
        if status_canonical:
            working = status_stripped
            upper = status_upper

    # Step 3: Extract plan type suffix (only if it doesn't consume the entire name)
    # Try multiple passes to extract compound suffixes (e.g., "ALAMEDA ALLIANCE PPO-ALL OTHER PLANS")
    extracted_plan_type = None
    for suffix in _PLAN_TYPE_SUFFIXES:
        if upper.endswith(suffix):
            remainder = working[:len(working) - len(suffix)].strip().rstrip('-').strip()
            if remainder:  # Don't extract if it would leave the name empty
                extracted_plan_type = working[len(working) - len(suffix):].strip().lstrip('-').strip()
                working = remainder
                upper = working.upper()
            break

    if (not extracted_plan_type
            and upper not in _PAYER_CANONICAL_MAP
            and upper not in aliases):
        for suffix, plan_type in _PAYER_TRAILING_PLAN_SUFFIXES:
            if upper.endswith(suffix):
                remainder = working[:len(working) - len(suffix)].strip().rstrip('-').strip()
                if remainder:
                    extracted_plan_type = plan_type
                    working = remainder
                    upper = working.upper()
                break

    if not extracted_plan_type and ' ' not in upper and len(upper) > 5:
        lower_compact = re.sub(r'[^a-z0-9]', '', working.lower())
        for payer_prefix, canonical_name in _CONCAT_PAYER_PLAN_PATTERNS:
            if not lower_compact.startswith(payer_prefix):
                continue
            suffix_part = lower_compact[len(payer_prefix):]
            for suffix, plan_type in _CONCAT_PLAN_SUFFIXES:
                if suffix_part == suffix or suffix_part.endswith(suffix):
                    canonical = canonical_name
                    enriched_plan = raw_plan or plan_type
                    return canonical, enriched_plan

    # Step 3b: Normalize whitespace (collapse multiple spaces)
    working = re.sub(r'\s+', ' ', working).strip()
    upper = working.upper()

    # Step 4: Canonical name lookup
    canonical = aliases.get(upper) or _PAYER_CANONICAL_MAP.get(upper)

    # Step 4b: If not found, try without common trailing words
    if not canonical:
        for trail in ['HEALTH PLAN', 'HEALTHCARE', 'HEALTH SERVICES', 'HEALTH']:
            if upper.endswith(' ' + trail):
                trimmed = upper[:-(len(trail) + 1)].strip()
                canonical = aliases.get(trimmed) or _PAYER_CANONICAL_MAP.get(trimmed)
                if canonical:
                    break

    # Step 4c: If not found, try stripping ", INC." / ", LLC" / ", INC" etc.
    if not canonical:
        cleaned = re.sub(r',\s*(INC\.?|LLC\.?|LP\.?|CO\.?)\s*$', '', upper).strip()
        if cleaned != upper:
            canonical = aliases.get(cleaned) or _PAYER_CANONICAL_MAP.get(cleaned)

    # Step 4d: Progressive prefix fallback - strip trailing words one at a time
    # and retry the map.  "AETNA SOME NEW VARIANT" → "AETNA SOME NEW" → "AETNA"
    # Require the candidate to have ≥1 word (we already have the full string in
    # step 4).  Stop as soon as we find a match.
    if not canonical:
        words = upper.split()
        for n in range(len(words) - 1, 0, -1):
            candidate = ' '.join(words[:n])
            canonical = aliases.get(candidate) or _PAYER_CANONICAL_MAP.get(candidate)
            if canonical:
                break

    # Step 5: Fallback - title-case the cleaned name
    if not canonical:
        canonical = working.title()
        # Fold trivial spelling variants (plural/punctuation/whitespace) of an
        # uncurated payer onto the established payer that already owns this
        # match key, so two hospitals spelling the same payer differently stop
        # producing duplicate payers.  No-op without a match index.
        canonical = _fold_canonical_via_match(canonical, ref)

    # Step 6: Enrich plan_name
    enriched_plan = raw_plan
    if extracted_plan_type and not raw_plan:
        # Title-case the plan type: "HMO" stays "HMO", "MEDICARE" -> "Medicare"
        enriched_plan = extracted_plan_type.title()
        # Normalize common plan type abbreviations
        _plan_type_normalize = {
            'Hmo': 'HMO', 'Ppo': 'PPO', 'Epo': 'EPO', 'Pos': 'POS',
            'Indemnity': 'Indemnity',
            'Mcare': 'Medicare', 'Network Mcare': 'Network Medicare',
            'Mcr Adv': 'Medicare Advantage',
            'Hmo/Ifp': 'HMO',
            'Hmo/Ppo': 'HMO/PPO',
            'Hmo/Pos': 'HMO/POS',
            'Ppo/Epo': 'PPO',
            'Ppo/EPO': 'PPO',
            'Eppo': 'PPO',
            'Ppo/Hmo': 'PPO/HMO',
            'Ppo/Ep': 'PPO',
        }
        for original, replacement in _plan_type_normalize.items():
            enriched_plan = enriched_plan.replace(original, replacement)

    return canonical, enriched_plan


def is_valid_payer_name(
    name: Optional[str],
    *,
    ref: Optional[ReferenceData] = None,
) -> bool:
    """Return True if *name* survives normalization (i.e. is not junk).

    A name is valid when normalize_payer_name() returns a non-None
    canonical form.
    """
    if name is None:
        return False
    canonical, _ = normalize_payer_name(name, ref=ref)
    return canonical is not None




# PLAN NAME NORMALIZATION
# ============================================================================
# Classifies raw plan names into a standard taxonomy.
# Each hospital system uses its own naming conventions:
#   Sutter:   "Hmo/Ppo", "Medicare Adv_ Hmo / Ppo", "Medi-Cal"
#   O'Connor: "COMMERCIAL (HMO/PPO)", "MEDICARE ADVANTAGE (PPO)", "MEDI-CAL"
#   CMS JSON: "non-HMO Other Commercial Plan", "Medicare Managed Care Plan"
#   El Camino: "United Healthcare Ppo", "Blue Shield Medicare Advantage"
#   Tenet:    "Blueshieldhix", "Molinamgdmcaid", "Americaschoiceprovidernetworkwc"
#   JohnMuir: "Mc Pending Cigna Com Pb", "Mc Ben Hum 076-356"
#   SFGH:     "Anthem Blue Cross Non-Chn Mcalmc - Hill Physicians Mg"

# Standard plan categories (canonical):
# Top-level plan categories (plan_category column).
# These are the coarse buckets - independent of network type.
PLAN_CATEGORIES = {
    'Commercial',
    'Medicare',
    'Medicaid',
    'TRICARE',
    'Behavioral Health',
    'Transplant',
    'Employee',
    'Government',
    'Workers Comp',
    'Other',
}

# Valid network types (plan_network column, NULL when not applicable).
PLAN_NETWORKS = {'HMO', 'PPO', 'POS', 'EPO', 'Indemnity', 'Managed Care'}

# plan_name → (plan_category, plan_network) lookup.
# plan_name is the full classified name that _classify_plan() returns.
_PLAN_NAME_TO_CAT_NET: dict = {
    # Commercial variants
    'Commercial':              ('Commercial', None),
    'Commercial HMO':          ('Commercial', 'HMO'),
    'Commercial PPO':          ('Commercial', 'PPO'),
    'Commercial POS':          ('Commercial', 'POS'),
    'Commercial EPO':          ('Commercial', 'EPO'),
    'Commercial Indemnity':    ('Commercial', 'Indemnity'),
    # Medicare variants
    'Medicare':                ('Medicare', None),
    'Medicare Advantage':      ('Medicare', None),
    'Medicare Advantage HMO':  ('Medicare', 'HMO'),
    'Medicare Advantage PPO':  ('Medicare', 'PPO'),
    'Medicare PACE':           ('Medicare', None),
    # Medicaid variants
    'Medicaid':                ('Medicaid', None),
    'Medi-Cal':                ('Medicaid', None),
    'Medi-Cal Managed Care':   ('Medicaid', 'Managed Care'),
    # Other top-level plans (no network type)
    'TRICARE':                 ('TRICARE', None),
    'Workers Comp':            ('Workers Comp', None),
    'Government':              ('Government', None),
    'Behavioral Health':       ('Behavioral Health', None),
    'Transplant':              ('Transplant', None),
    'Employee':                ('Employee', None),
    'Marketplace':             ('Commercial', None),
    'Covered California':      ('Commercial', None),
    'Other':                   ('Other', None),
}


def plan_name_to_cat_net(plan_name: str):
    """Return (plan_category, plan_network) for a classified plan_name.

    plan_network is None when the plan type does not have an HMO/PPO/etc. variant.
    """
    if not plan_name:
        return ('Other', None)
    entry = _PLAN_NAME_TO_CAT_NET.get(plan_name)
    if entry:
        return entry
    # Unknown plan_name (title-cased fallback from _classify_plan) -
    # try to infer network from the name itself
    up = plan_name.upper()
    if 'PPO' in up:
        return ('Commercial', 'PPO')
    if re.search(r'\bEPO\b', up):
        return ('Commercial', 'EPO')
    if re.search(r'(?<!NON-)HMO', up):
        return ('Commercial', 'HMO')
    if 'POS' in up:
        return ('Commercial', 'POS')
    if 'INDEMNITY' in up:
        return ('Commercial', 'Indemnity')
    return ('Other', None)



# Plan classification rules - checked in order, first match wins.
# Each rule: (pattern_function, canonical_plan_name)
# Patterns match against uppercase plan name.

# Short abbreviation lookup: maps 2-5 char internal codes to categories.
# Built from observed real-file data with payer context verification.
_ABBREV_PLAN_MAP = {
    # Medicare abbreviations
    'MCR': 'Medicare Advantage',
    'MCRPOS': 'Medicare Advantage',
    'MCRSNP': 'Medicare Advantage',
    'MGMCR': 'Medicare Advantage',
    'PFFS': 'Medicare Advantage',        # Private Fee-For-Service Medicare
    'DSNP': 'Medicare Advantage',        # Dual Special Needs Plan
    # Medicaid abbreviations
    'MCD': 'Medi-Cal',
    'MGMCD': 'Medi-Cal Managed Care',
    # Workers Comp abbreviations
    'WC': 'Workers Comp',
    'WCOMP': 'Workers Comp',
    # Medicaid abbreviations (CHIP = Children's Health Insurance Program)
    'CHIP': 'Medicaid',
    'SCHIP': 'Medicaid',
    # Medicare abbreviations (special plan types)
    'SNP': 'Medicare Advantage',         # Special Needs Plan
    'CSNP': 'Medicare Advantage',        # Chronic SNP
    'ISNP': 'Medicare Advantage',        # Institutional SNP
    'MMP': 'Medicare Advantage',         # Medicare-Medicaid Plan
    'MAPD': 'Medicare Advantage',        # Medicare Advantage Prescription Drug
    # Commercial abbreviations
    'COM': 'Commercial',
    'COMM': 'Commercial',
    'QHP': 'Marketplace',                # Qualified Health Plan (ACA)
    'IFP': 'Commercial',                 # Individual/Family Plan
    'ASO': 'Commercial',                 # Administrative Services Only
    # Government abbreviations
    'FEP': 'Government',                 # Federal Employee Program
    'FEHB': 'Government',               # Federal Employee Health Benefits
    'GEHA': 'Government',               # Government Employees Health Assoc.
    'CHAMPVA': 'Government',            # Civilian Health and Medical Program of VA
    # Behavioral health
    'COMMBH': 'Behavioral Health',
    'COCM': 'Commercial',               # Collaborative care management
}

# Concatenated token patterns for Tenet-style no-space plan names.
# Matched against the lowercased concatenated string.
# Order matters: more specific patterns before general ones.
_CONCAT_PATTERNS = [
    # Medi-Cal Managed Care patterns
    ('mgdmcaid', 'Medi-Cal Managed Care'),      # "Molinamgdmcaid", "Cahealthandwellnessmgdmcaid"
    ('mgdmcal', 'Medi-Cal Managed Care'),
    ('mcaid', 'Medi-Cal Managed Care'),          # e.g. "Alphacareancillarymgdmcaid"
    ('mcal', 'Medi-Cal'),                        # Broad Medi-Cal match
    # Marketplace / Exchange
    ('hix', 'Marketplace'),                      # "Blueshieldhix", "Ambetterhix", "Molinahix"
    ('exchange', 'Marketplace'),
    # Workers Comp patterns (suffix)
    ('wc', 'Workers Comp'),                      # "Bluecrosswc", "Corvelwc", "Americaschoiceprovidernetworkwc"
    # Medicare
    ('medicare', 'Medicare Advantage'),
    ('mcare', 'Medicare Advantage'),
    # Behavioral
    ('behavioral', 'Behavioral Health'),
    ('behavioralcare', 'Behavioral Health'),
    # Commercial sub-patterns (less specific, checked last)
    ('indemnity', 'Commercial Indemnity'),        # "Cignaindemnity", "Aetnaindemnity"
    ('reciprocity', 'Commercial'),               # "Blueshieldreciprocity", "Tenetreciprocity"
    ('ancillary', 'Commercial'),                 # "Hillphysiciansancillary", "Allcareipaancillary"
    ('gatekeeper', 'Commercial'),                # "Aetnagatekeeper", "Aetnanongatekeeper"
    ('nongatekeeper', 'Commercial'),
    ('select', 'Commercial'),                    # "Bluecrossselect"
    ('nonmcs', 'Commercial'),                    # "Bluecrossnonmcs"
    ('medical', 'Commercial'),                   # "Bluecrossmedical", "Centralcaalliancemedical"
]


def _classify_concatenated(lower: str) -> Optional[str]:
    """Classify a concatenated/camelCase plan name with no spaces.

    Tenet JSON MRFs produce plan names like "Blueshieldhix",
    "Molinamgdmcaid", "Americaschoiceprovidernetworkwc".
    This function detects known tokens within these strings.
    """
    # Workers comp: must check suffix specifically to avoid false positives
    # (e.g., "Healthcomp" contains "wc" but is not workers comp)
    if lower.endswith('wc'):
        return 'Workers Comp'

    for token, category in _CONCAT_PATTERNS:
        if token == 'wc':
            continue  # Already handled above as suffix-only
        if token in lower:
            return category

    return None


def _classify_plan(upper: str, raw_plan: str, payer_canonical: str) -> str:
    """Classify a plan name into a standard category.

    Standard categories (PLAN_CATEGORIES):
      Commercial, Commercial HMO/PPO/POS, Medicare Advantage (HMO/PPO),
      Medicare PACE, Medi-Cal, Medi-Cal Managed Care, Marketplace,
      TRICARE, Covered California, Behavioral Health, Transplant,
      Employee, Government, Workers Comp, Other.
    """

    # ---- Strip bracket-wrapped plan names first, then contract IDs ----
    # e.g., "[Medicaid]" -> "MEDICAID"
    m = re.match(r'^\[(.+)\]$', upper)
    if m:
        upper = m.group(1).strip()
    # Strip trailing contract IDs: "Beacon Health Strategies [100510301]"
    upper = re.sub(r'\s*\[[\w]+\]\s*$', '', upper).strip()

    # ---- Short abbreviation lookup (before keyword matching) ----
    # Handles: Mcr, Mcd, Wc, Mgmcr, Dsnp, Qhp, etc.
    abbrev_result = _ABBREV_PLAN_MAP.get(upper)
    if abbrev_result:
        return abbrev_result

    # ---- Workers Comp variants (before Medi-Cal to avoid false matches) ----
    if (re.search(r"WORKER'?S[_ ]?COMP", upper)
            or upper in ('WORKERS COMP', 'WORKERS COMPENSATION',
                         'WORKER COMP', 'WORK COMP')
            or upper.startswith('WORKERS COMP')):
        return 'Workers Comp'

    # ---- TRICARE (military) ----
    if re.search(r'\bTRICARE\b', upper) or upper == 'TRICARE':
        return 'TRICARE'
    # CHAMPVA (civilian VA medical program - classified as Government)
    if 'CHAMPVA' in upper or 'CHAMP VA' in upper:
        return 'Government'
    # VA Community Care Network (VACCN) - military/veterans
    if re.search(r'\bVACCN\b', upper) or 'VA COMMUNITY CARE' in upper:
        return 'TRICARE'

    # ---- Medicaid variants (before Medicare to avoid misclassification) ----
    if 'MEDI-CAL' in upper or 'MEDI CAL' in upper or 'MEDICAL' == upper or 'MANAGED MEDI' in upper:
        return 'Medi-Cal'
    if re.search(r'\bMEDICAID\b', upper):
        return 'Medi-Cal'

    # ---- CHIP / SCHIP (Children's Health Insurance Program → Medicaid) ----
    if re.search(r'\bCHIP\b', upper) or re.search(r'\bSCHIP\b', upper):
        return 'Medicaid'
    if 'CHILD HEALTH PLUS' in upper or 'KIDCARE' in upper:
        return 'Medicaid'

    # ---- State Medicaid programs ----
    # Texas STAR programs (STAR, STAR+PLUS, STAR Kids, STAR Health)
    if re.search(r'\bSTAR\s*\+?\s*PLUS\b', upper):
        return 'Medicaid'
    if re.search(r'\bSTAR\s*KIDS?\b', upper) or upper == 'STARKIDS':
        return 'Medicaid'
    if re.search(r'\bSTAR\s*HEALTH\b', upper):
        return 'Medicaid'
    # MassHealth (Massachusetts Medicaid)
    if 'MASSHEALTH' in upper:
        return 'Medicaid'
    # NY Essential Plans
    if 'ESSENTIAL PLAN' in upper:
        return 'Medicaid'
    if re.search(r'\bESSENTIAL\s+P[QA]\b', upper):
        return 'Medicaid'
    # ConnectorCare (Massachusetts subsidized Medicaid plans)
    if 'CONNECTORCARE' in upper or 'CONNECTOR CARE' in upper:
        return 'Medicaid'
    # Family Choice (Medicaid MCOs - MedStar Family Choice MD/DC)
    if 'FAMILY CHOICE' in upper:
        return 'Medicaid'
    # Community Plan (UHC Community Plan = Medicaid MCO)
    if 'COMMUNITY PLAN' in upper:
        return 'Medicaid'
    # WellSense / BMC HealthNet (Medicaid MCOs)
    if 'WELLSENSE' in upper:
        return 'Medicaid'
    # Better Health (Medicaid-related programs)
    if 'BETTER HEALTH' in upper and 'HEALTHY' in upper:
        return 'Medicaid'
    # Healthy Kids programs
    if 'HEALTHY KIDS' in upper or 'HEALTHY KID' in upper:
        return 'Medicaid'

    # ---- Medi-Cal Managed Care ----
    # SFGH: "Anthem Blue Cross Non-Chn Mcalmc - Hill Physicians Mg"
    # Tenet: "Molinamgdmcaid" (handled by concatenated classifier)
    if 'MCALMC' in upper:
        return 'Medi-Cal Managed Care'

    # ---- Government programs ----
    if upper == 'GOVERNMENT' or upper.startswith('GOVERNMENT'):
        return 'Government'
    # Federal Employee Health Benefits / Federal Employee Program
    if 'FEDERAL EMPLOYEE' in upper or upper == 'FEHB' or upper == 'FEP':
        return 'Government'
    if re.search(r'\bGEHA\b', upper):
        return 'Government'

    # ---- Medicare Advantage variants ----
    if 'PACE' in upper:
        return 'Medicare PACE'
    if 'MEDICARE ADV' in upper or 'MEDICARE ADVANTAGE' in upper:
        # Preserve HMO vs PPO distinction if present
        if 'PPO' in upper and 'HMO' not in upper:
            return 'Medicare Advantage PPO'
        if 'HMO' in upper and 'PPO' not in upper:
            return 'Medicare Advantage HMO'
        return 'Medicare Advantage'
    if 'MCR ADV' in upper:
        return 'Medicare Advantage'
    if 'D-SNP' in upper:
        return 'Medicare Advantage'
    if re.search(r'\bDUAL\b', upper):
        return 'Medicare Advantage'
    # SNP / MMP / Special Needs Plans (Medicare sub-types)
    if re.search(r'\bSNP\b', upper) or re.search(r'\bCSNP\b', upper) or re.search(r'\bISNP\b', upper):
        return 'Medicare Advantage'
    if re.search(r'\bMMP\b', upper):
        return 'Medicare Advantage'
    if 'DUAL ELIGIBLE' in upper or 'DUALELIGIBLE' in upper:
        return 'Medicare Advantage'
    if re.search(r'\bMAPD\b', upper):
        return 'Medicare Advantage'
    if upper.startswith('MEDICARE') or 'MEDICARE' in upper:
        # "Medicare Managed Care Plan" etc.
        return 'Medicare Advantage'

    # ---- Marketplace / Health Exchange ----
    if re.search(r'\bHIX\b', upper) or 'EXCHANGE' in upper:
        return 'Marketplace'
    if re.search(r'\bAMBETTER\b', upper):
        return 'Marketplace'
    if re.search(r'\bMARKETPLACE\b', upper):
        return 'Marketplace'
    if re.search(r'\bQHP\b', upper):
        return 'Marketplace'

    # ---- Covered California ----
    if 'COVERED CA' in upper or 'COVERED CALIFORNIA' in upper:
        return 'Covered California'

    # ---- Transplant ----
    if 'TRANSPLANT' in upper or 'DONOR NETWORK' in upper:
        return 'Transplant'

    # ---- Employee plan ----
    if upper == 'EMPLOYEE' or upper.startswith('EMPLOYEE'):
        return 'Employee'

    # ---- Behavioral health ----
    if 'BEHAVIORAL HEALTH' in upper or re.search(r'\bEAP\b', upper):
        return 'Behavioral Health'
    if 'BEHAVIORAL' in upper:
        return 'Behavioral Health'

    # ---- Commercial: explicit markers ----
    if 'COMMERCIAL' in upper:
        has_hmo = bool(re.search(r'(?<!NON-)HMO', upper))
        has_ppo = 'PPO' in upper
        has_pos = 'POS' in upper and 'HMO' not in upper
        has_epo = bool(re.search(r'\bEPO\b', upper))
        has_indemnity = 'INDEMNITY' in upper
        if has_indemnity and not has_hmo and not has_ppo and not has_epo and not has_pos:
            return 'Commercial Indemnity'
        if has_ppo and not has_hmo:
            return 'Commercial PPO'
        if has_epo and not has_hmo and not has_ppo:
            return 'Commercial PPO'
        if has_hmo and not has_ppo:
            return 'Commercial HMO'
        if has_pos:
            return 'Commercial POS'
        return 'Commercial'

    # ---- Commercial: HMO/PPO plan types ----
    # Catch "Hmo/Ppo", "Hmo / Ppo", "HMO", "PPO", "EPO", "POS", "PPO/EPO"
    # Also "All Commercial Plans", "Individual", etc.
    if upper in ('HMO/PPO', 'HMO / PPO', 'HMO/ PPO', 'HMO /PPO'):
        return 'Commercial'
    if upper == 'HMO':
        return 'Commercial HMO'
    if upper in ('PPO', 'PPO/EPO', 'EPO'):
        return 'Commercial PPO'
    if upper == 'POS':
        return 'Commercial POS'
    if upper == 'INDEMNITY':
        return 'Commercial Indemnity'
    if upper in ('INDIVIDUAL', 'ALL COMMERCIAL PLANS'):
        return 'Commercial'

    # ---- "All Products" umbrella plans → Commercial ----
    if re.search(r'\bALL\s+PRODUCTS?\b', upper):
        return 'Commercial'

    # ---- Self-funded / ASO / Fully Insured → Commercial ----
    if re.search(r'\bSELF[- ]?FUNDED\b', upper) or re.search(r'\bASO\b', upper):
        return 'Commercial'
    if 'FULLY INSURED' in upper:
        return 'Commercial'

    # ---- IFP (Individual/Family Plan) → Commercial ----
    if upper == 'IFP' or re.search(r'\bINDIVIDUAL\s*/?\s*FAMILY\b', upper):
        return 'Commercial'

    # ---- Group size labels → Commercial ----
    if re.search(r'\b(SMALL|LARGE)\s*GROUP\b', upper) or upper == 'SMALLGROUP':
        return 'Commercial'

    # ---- John Muir internal codes ----
    # "MC PENDING CIGNA COM PB" → Commercial (pending contract)
    # "MC BEN HUM 076-356" → Medicare Advantage (Humana MA benefit)
    # "MC BEN SCAN 101" → Medicare Advantage (SCAN is MA plan)
    if upper.startswith('MC PENDING ') or upper.startswith('MC BEN '):
        jm_suffix = upper[len('MC PENDING '):] if upper.startswith('MC PENDING ') else upper[len('MC BEN '):]
        jm_upper = jm_suffix.upper()
        # MA payers in John Muir context
        if any(p in jm_upper for p in ('HEALTHNET MA', 'BLUESHIELD MA',
                                        'SCAN', 'HUM', 'CAN')):
            return 'Medicare Advantage'
        # Default: commercial pending contracts
        return 'Commercial'

    # ---- Managed Care (generic) ----
    if upper == 'MANAGED CARE':
        return 'Commercial'

    # ---- Payer-specific plan names ----
    # "Blue Shield Ppo", "Cigna Ppo", "United Healthcare Select Ppo", etc.
    # Strip the payer name prefix to find the plan type
    payer_upper = payer_canonical.upper() if payer_canonical else ''
    plan_suffix = upper
    if payer_upper and upper.startswith(payer_upper):
        plan_suffix = upper[len(payer_upper):].strip()
    # Also try common short payer names
    for prefix in ('BLUE CROSS ANTHEM ', 'BLUE SHIELD ', 'ANTHEM BLUE CROSS ',
                    'ANTHEM BC ', 'ANTHEM ',
                    'UNITED HEALTHCARE ', 'UNITED HEALTH CARE ', 'UHC ',
                    'CIGNA ', 'AETNA ', 'HEALTH NET ', 'HEALTHNET ',
                    'HUMANA ', 'KAISER ', 'KAISER PERMANENTE ',
                    'ALIGNMENT HEALTH PLAN ', 'SCAN ', 'SCFHP ',
                    'MULTIPLAN ', 'BLUE CROSS '):
        if upper.startswith(prefix):
            plan_suffix = upper[len(prefix):].strip()
            break

    if plan_suffix:
        ps = plan_suffix
        if 'MEDICARE' in ps or 'MCARE' in ps or 'MCR ADV' in ps:
            return 'Medicare Advantage'
        if 'MEDI-CAL' in ps or 'MANAGED MEDI' in ps:
            return 'Medi-Cal'
        if ps in ('PATHWAY', 'PATHWAYS', 'PATHWAY X', 'BLUE CONNECTION',
                  'PREFERRED', 'TRADITIONAL', 'SHORT TERM LIMITED DURATION',
                  'LOCALPLUS'):
            return 'Commercial'
        if ps in ('PPO', 'SELECT PPO', 'OUT OF STATE PPO', "DOCTOR'S PLAN", 'EPO', 'PPO/EPO'):
            return 'Commercial PPO'
        if ps in ('HMO', 'SR HMO'):
            return 'Commercial HMO'
        if ps in ('HMO/POS', 'HMO / POS', 'POS'):
            return 'Commercial POS'
        if ps == 'INDEMNITY':
            return 'Commercial Indemnity'

    # ---- Catch-all patterns ----
    if 'OTHER COMMERCIAL' in upper or 'NON-HMO' in upper:
        return 'Commercial'
    if 'PPO' in upper or re.search(r'\bEPO\b', upper):
        return 'Commercial PPO'
    if re.search(r'(?<!NON-)HMO', upper):
        return 'Commercial HMO'
    if re.search(r'\bINDEMNITY\b', upper):
        return 'Commercial Indemnity'

    # ---- Specific known plans ----
    if upper == 'SFHSS':
        return 'Commercial'  # San Francisco Health Service System
    if 'SELECT/NAVIGATE' in upper or 'OAP' in upper:
        return 'Commercial'

    # ---- TRICARE short forms ----
    if upper in ('TRI', 'TRIM', 'TRICARE EAST', 'TRICARE WEST'):
        return 'TRICARE'

    # ---- Veterans / VA ----
    if upper in ('VETERANS', 'VETERANS AFFAIRS') or 'VETERANS ADMIN' in upper:
        return 'Government'

    # ---- Generic catch-alls that indicate commercial plans ----
    # "All Other Plans", "All Plans", "Comm - All Other Plans" etc.
    if re.search(r'ALL\s*(OTHER\s*)?PLANS', upper):
        return 'Commercial'
    if upper.endswith('COMM') or 'COMM ' in upper:
        return 'Commercial'
    if 'HEALTHY FAMILY' in upper or 'HEALTHY FAMILIES' in upper:
        return 'Medicaid'

    # ---- Plan name is just a payer/network name (no plan type info) ----
    # These should default to "Commercial" since they indicate a payer relationship
    # without specifying a government program
    _PAYER_AS_PLAN_NAMES = {
        'AETNA', 'ANTHEM', 'ANTHEM BLUE CROSS', 'BLUE CROSS', 'BLUE SHIELD',
        'CIGNA', 'HEALTHNET', 'HEALTH NET', 'HUMANA', 'KAISER',
        'KAISER PERMANENTE', 'UNITED', 'UNITED HEALTHCARE',
        'UNITED HEALTH CARE', 'UNITEDHEALTHCARE', 'UHC', 'UMR',
        'MULTIPLAN', 'BEECH STREET', 'INTERPLAN', 'HEALTH PLAN OF SAN MATEO',
        'SUTTER HEALTH', 'SUTTER', 'OPTUM',
        'BEACON HEALTH STRATEGIES', 'CLARITEV',
        'COVENTRY HEALTHCARE', 'CAREMORE HEALTH PLAN',
        'AETNA LIFE', 'HUMANA-CHOICE CARE',
    }
    if upper in _PAYER_AS_PLAN_NAMES:
        return 'Commercial'

    # Also check if the plan name matches the payer's canonical name (case-insensitive)
    if payer_upper and upper == payer_upper:
        return 'Commercial'

    # ---- Concatenated/CamelCase names (Tenet JSON) ----
    # No spaces → try splitting by known tokens
    # Only attempt if the string has no spaces and is reasonably long
    if ' ' not in upper and len(upper) > 6:
        concat_result = _classify_concatenated(upper.lower())
        if concat_result:
            return concat_result

    # ---- Fallback: return cleaned title-case of the raw plan ----
    return raw_plan.strip().title() if raw_plan else 'Other'


_PLAN_LEADING_CONTRACT_RE = re.compile(r'^\s*\d{2,6}[_\s-]+')
_PLAN_TRAILING_CONTRACT_RE = re.compile(r'\s+-\s+\d{5,8}\s*$')
_PLAN_TRAILING_DATE_RE = re.compile(r'(?:^|\s+)\d{8}\s*$')
_PLAN_BRACKETED_ID_RE = re.compile(r'\s*\[[\w-]*\d[\w-]*\]')
_PLAN_SITE_CODES = (
    'AB', 'BAOK', 'BO', 'BOGI', 'BOSU', 'DEKALB', 'FNWI', 'GO', 'HN',
    'JPOK', 'LG', 'MCOK', 'MEWI', 'MIL', 'MTTN', 'MW', 'MWWI', 'NHOK',
    'OHOK', 'RHTN', 'RPTN', 'SA', 'SDTN', 'SEWI', 'SFWI', 'SJWI', 'SPOK',
    'STTN', 'THTN', 'VEIN', 'WVN',
)
_PLAN_SITE_CODE_PATTERN = '|'.join(re.escape(code) for code in _PLAN_SITE_CODES)
_PLAN_SITE_PREFIX_RE = re.compile(
    rf'^(?:(?:{_PLAN_SITE_CODE_PATTERN})(?:,\s*|\s+))+',
    re.IGNORECASE,
)
_PLAN_SITE_PAREN_RE = re.compile(
    rf'\s*\((?:{_PLAN_SITE_CODE_PATTERN})'
    rf'(?:,\s*(?:{_PLAN_SITE_CODE_PATTERN}))*\)\s*',
    re.IGNORECASE,
)
_PLAN_SITE_SUFFIX_RE = re.compile(
    rf'(?:\s+(?:{_PLAN_SITE_CODE_PATTERN}))+\s*$',
    re.IGNORECASE,
)


def _clean_plan_display_artifacts(raw_plan: str) -> str:
    """Strip source-system IDs/dates from plan display names."""
    cleaned = raw_plan.strip()

    had_contract_prefix = bool(_PLAN_LEADING_CONTRACT_RE.match(cleaned))
    had_bracketed_id = bool(_PLAN_BRACKETED_ID_RE.search(cleaned))
    had_trailing_contract = bool(_PLAN_TRAILING_CONTRACT_RE.search(cleaned))
    had_trailing_date = bool(_PLAN_TRAILING_DATE_RE.search(cleaned))
    artifact_context = (
        had_contract_prefix or had_bracketed_id
        or had_trailing_contract or had_trailing_date
    )

    cleaned = _PLAN_LEADING_CONTRACT_RE.sub('', cleaned).strip()
    cleaned = _PLAN_BRACKETED_ID_RE.sub('', cleaned).strip()
    cleaned = _PLAN_TRAILING_CONTRACT_RE.sub('', cleaned).strip()
    cleaned = _PLAN_TRAILING_DATE_RE.sub('', cleaned).strip()

    if artifact_context:
        cleaned = _PLAN_SITE_PAREN_RE.sub(' ', cleaned).strip()
        cleaned = _PLAN_SITE_PREFIX_RE.sub('', cleaned).strip()
        cleaned = _PLAN_SITE_SUFFIX_RE.sub('', cleaned).strip()
        cleaned = _PLAN_TRAILING_DATE_RE.sub('', cleaned).strip()
        cleaned = _PLAN_SITE_SUFFIX_RE.sub('', cleaned).strip()

    cleaned = re.sub(r'\s*,\s*,+', ', ', cleaned)
    cleaned = re.sub(r'\s{2,}', ' ', cleaned)
    return cleaned.strip(' ,-_')


def normalize_plan_name(
    raw_plan: Optional[str],
    payer_canonical: str,
) -> tuple:
    """
    Normalize a raw plan name into category, network type, and canonical name.

    Top-level categories (plan_category):
      Commercial, Medicare, Medicaid, TRICARE, Behavioral Health,
      Transplant, Employee, Government, Workers Comp, Other.

    Network types (plan_network, None when not applicable):
      HMO, PPO, POS, EPO, Managed Care.

    plan_name is the full classified canonical name (e.g. 'Commercial PPO',
    'Medicare Advantage HMO', 'Medi-Cal Managed Care').

    Args:
        raw_plan: Raw plan name from source data
        payer_canonical: Already-normalized payer name (for stripping payer prefix)

    Returns: Tuple of (plan_category, plan_network, plan_name).
        plan_category is the top-level bucket ('Commercial', 'Medicare', etc.)
        plan_network is the network type ('HMO', 'PPO', etc.) or None.
        plan_name is the full classified name ('Commercial PPO', etc.)
    """
    if not raw_plan or not raw_plan.strip():
        return ('Other', None, 'Other')

    cleaned = _clean_plan_display_artifacts(raw_plan)
    # Strip bracket-wrapped contract IDs: "Anthem Blue Connection Epo [100210404]"
    cleaned = re.sub(r'\s*\[[\w]+\]\s*$', '', cleaned).strip()
    # Unwrap fully bracket-wrapped names: "[Medicaid]" -> "Medicaid"
    m = re.match(r'^\[(.+)\]$', cleaned)
    if m:
        cleaned = m.group(1).strip()

    upper = cleaned.upper()
    # Normalize underscores used as spaces (Sutter: "Medicare Adv_ Hmo / Ppo")
    upper = upper.replace('_', ' ').strip()
    upper = re.sub(r'\s+', ' ', upper)

    plan_name = _classify_plan(upper, cleaned, payer_canonical)
    plan_category, plan_network = plan_name_to_cat_net(plan_name)
    return (plan_category, plan_network, plan_name)

