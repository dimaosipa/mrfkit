"""Map MRF column headers to canonical fields.

Hospitals name the same column a hundred ways ("Gross Charge", "gross_price",
"IVPRICE1"). Every header is normalized (lowercase, underscores, no
punctuation) and looked up in ``SYNONYM_MAP``. Wide CMS files encode the payer
and plan inside the header (``standard_charge|Aetna|PPO|negotiated_dollar``);
``parse_wide_payer_header`` takes those apart. A few hospital families use
their own wide layouts (Hawaii, Atrium), described by the tables below.
"""

from __future__ import annotations

import re
from typing import Dict, Optional, Tuple

CANONICAL_FIELDS = {
    'hospital_name',
    'hospital_location',
    'hospital_ein',
    'cms_certification_number',
    'code',
    'code_type',
    'description',
    'modifiers',
    'gross_charge',
    'discounted_cash_price',
    'min_negotiated_rate',
    'max_negotiated_rate',
    'payer_name',
    'plan_name',
    'negotiated_rate',
    'negotiated_percentage',
    'negotiated_algorithm',
    'methodology',
    'estimated_amount',
    'billing_class',
    'setting',
    'additional_notes',
    'footnote',
    'median_amount',
    'pct_10',
    'pct_90',
    'claim_count',
    'drug_unit_of_measurement',
    'drug_type_of_measurement',
    'additional_generic_notes',
}

# Fields that belong to payer-specific rates (not the item's standard charges)
PAYER_FIELDS = {
    'payer_name', 'plan_name', 'negotiated_rate', 'negotiated_percentage',
    'negotiated_algorithm', 'methodology', 'estimated_amount',
    'additional_notes', 'footnote', 'median_amount', 'pct_10', 'pct_90', 'claim_count',
}

# Fields stored on the charge item (also passed through to each payer rate)
ITEM_LEVEL_FIELDS = {
    'billing_class', 'setting',
}


# Synonym dictionary: normalized_header -> canonical_field
SYNONYM_MAP = {
    # code
    'billing_code': 'code',
    'code': 'code',
    'cpt': 'code',
    'hcpcs': 'code',             # Paris (Cornerstone Regional) format
    'hcpcs_code': 'code',
    'hcpcscpt_code': 'code',
    'procedure_code': 'code',
    'procedurecode': 'code',     # Hawaii hospitals (CamelCase → no underscores)
    'service_code': 'code',
    'code_value': 'code',             # Ascension hospitals (CMS 3.0 variant)
    'codes': 'code',                   # Atrium anesthesia/BH sections (Codes, Code(s))
    'procedure_external_id': 'code',   # Atrium Prof Discounted Cash Price section
    'cpthcpcs_code': 'code',           # Middlesex Hospital (CPT/HCPCS Code)
    'ivcptcd': 'code',                 # Bear Lake Memorial chargemaster (IV-prefixed legacy headers)

    # code_type
    'billing_code_type': 'code_type',
    'code_type': 'code_type',
    'coding_type': 'code_type',
    
    # description
    'description': 'description',
    'item_description': 'description',
    'procedure_description': 'description',
    'service_description': 'description',
    'customer_description': 'description',  # Ascension hospitals
    'code_descriptions': 'description',     # Ascension hospitals (CMS 3.0 variant)
    'charge_description': 'description',    # William Bee Ririe Hospital
    'procedure_external_id_record_name': 'description',  # Atrium Prof section
    'billing_code_description': 'description',  # Middlesex Hospital
    'ivdesc': 'description',                     # Bear Lake Memorial chargemaster

    # gross_charge
    'gross_charge': 'gross_charge',
    'grosscharge': 'gross_charge',
    'standard_charge_gross': 'gross_charge',
    'gross': 'gross_charge',
    'charge_gross': 'gross_charge',
    'gross_price': 'gross_charge',
    'standard_charge': 'gross_charge',  # generic standard_charge maps to gross
    'price': 'gross_charge',  # Atrium Prof Discounted Cash Price section
    'gross_charge_per_cdm': 'gross_charge',  # Middlesex Hospital
    'ivprice1': 'gross_charge',              # Bear Lake Memorial chargemaster
    'gross_charges_estimate': 'gross_charge',  # Ascension estimate files
    'gross_outpatient': 'gross_charge',        # Troy Regional CMS wide variant

    # discounted_cash_price
    'discounted_cash_price': 'discounted_cash_price',
    'cash_price': 'discounted_cash_price',
    'self_pay': 'discounted_cash_price',
    'selfpay': 'discounted_cash_price',
    'cash': 'discounted_cash_price',
    'discounted_price': 'discounted_cash_price',
    'standard_charge_discounted_cash': 'discounted_cash_price',
    'discounted_cash': 'discounted_cash_price',
    'discounted_cash_price_gross_charges': 'discounted_cash_price',
    'estimated_discounted_cash': 'discounted_cash_price',
    'standard_charge_estimated_discounted_cash': 'discounted_cash_price',
    'cash_price_estimate': 'discounted_cash_price',  # Ascension estimate files
    
    # min_negotiated_rate
    'min_negotiated_rate': 'min_negotiated_rate',
    'minimum_negotiated_rate': 'min_negotiated_rate',
    'minimumnegotiatedcharge': 'min_negotiated_rate',  # Hawaii hospitals (CamelCase)
    'min_rate': 'min_negotiated_rate',
    'minimum': 'min_negotiated_rate',
    'standard_charge_min': 'min_negotiated_rate',
    'min': 'min_negotiated_rate',
    'min_ip_reimb': 'min_negotiated_rate',  # Partners Healthcare pipe-delimited
    'min_negotiated_charge': 'min_negotiated_rate',   # Atrium anesthesia section
    'minimum_reimbursement': 'min_negotiated_rate',   # Atrium BH section
    
    # max_negotiated_rate
    'max_negotiated_rate': 'max_negotiated_rate',
    'maximum_negotiated_rate': 'max_negotiated_rate',
    'maximumnegotiatedcharge': 'max_negotiated_rate',  # Hawaii hospitals (CamelCase)
    'max_rate': 'max_negotiated_rate',
    'maximum': 'max_negotiated_rate',
    'standard_charge_max': 'max_negotiated_rate',
    'max': 'max_negotiated_rate',
    'max_ip_reimb': 'max_negotiated_rate',  # Partners Healthcare pipe-delimited
    'max_negotiated_charge': 'max_negotiated_rate',   # Atrium anesthesia section
    'maximum_reimbursement': 'max_negotiated_rate',   # Atrium BH section
    
    # hospital_name
    'hospital_name': 'hospital_name',
    'facility_name': 'hospital_name',
    'facility': 'hospital_name',
    
    # hospital_location
    'hospital_location': 'hospital_location',
    'facility_location': 'hospital_location',
    'address': 'hospital_location',
    
    # hospital_ein
    'ein': 'hospital_ein',
    'hospital_ein': 'hospital_ein',
    'tax_id': 'hospital_ein',
    
    # cms_certification_number
    'cc_number': 'cms_certification_number',
    'cms_certification_number': 'cms_certification_number',
    'cms_id': 'cms_certification_number',
    'medicare_provider_number': 'cms_certification_number',

    # payer_name
    'payer_name': 'payer_name',
    'payer': 'payer_name',
    'insurance_name': 'payer_name',
    'insurance_carrier': 'payer_name',  # Ascension hospitals

    # plan_name
    'plan_name': 'plan_name',
    'plan': 'plan_name',
    'plans': 'plan_name',           # Plan(s) header → plans after normalization
    'insurance_plan': 'plan_name',
    'product': 'plan_name',         # Atrium Prof section uses Product for plan

    # negotiated_rate (dollar amount)
    'standard_charge_negotiated_dollar': 'negotiated_rate',
    'negotiated_dollar': 'negotiated_rate',
    'negotiated_rate': 'negotiated_rate',
    'ip_price': 'negotiated_rate',  # Partners Healthcare pipe-delimited format

    # negotiated_percentage (CVHFullCDMPriceTransparency ships the singular
    # 'percent' form with spaces around the pipe; both variants must route
    # to the same canonical field).
    'standard_charge_negotiated_percentage': 'negotiated_percentage',
    'standard_charge_negotiated_percent': 'negotiated_percentage',
    'negotiated_percentage': 'negotiated_percentage',
    'negotiated_percent': 'negotiated_percentage',

    # negotiated_algorithm
    'standard_charge_negotiated_algorithm': 'negotiated_algorithm',
    'negotiated_algorithm': 'negotiated_algorithm',

    # methodology
    'standard_charge_methodology': 'methodology',
    'methodology': 'methodology',
    'rate_methodology': 'methodology',  # Middlesex Hospital

    # estimated_amount
    'estimated_amount': 'estimated_amount',
    'estimate_amount': 'estimated_amount',
    'insurance_estimate': 'estimated_amount',
    'ip_expected_reimbursement': 'estimated_amount',  # Partners Healthcare pipe-delimited

    # billing_class
    'billing_class': 'billing_class',

    # setting
    'setting': 'setting',

    # modifiers
    'modifiers': 'modifiers',
    'modifier': 'modifiers',
    'modifier_code': 'modifiers',
    'procedure_modifier': 'modifiers',  # Atrium Prof Discounted Cash Price

    # drug fields
    'drug_unit_of_measurement': 'drug_unit_of_measurement',
    'drug_unit': 'drug_unit_of_measurement',
    'drug_type_of_measurement': 'drug_type_of_measurement',
    'drug_type': 'drug_type_of_measurement',

    # additional_notes (payer-level notes)
    'additional_notes': 'additional_notes',
    'additional_payer_notes': 'additional_notes',  # University Hospitals

    # additional_generic_notes (item-level notes, distinct from payer-level)
    'additional_generic_notes': 'additional_generic_notes',
    'additional_generic_notes_with_lpp': 'additional_generic_notes',

    # footnote (pricing logic: bundling rules, lesser-of provisions, etc.)
    'footnote': 'footnote',
    'pricing_footnote': 'footnote',
    'rate_footnote': 'footnote',

    # statistical fields (bare / tall-format variants)
    'median_amount': 'median_amount',
    'median_allowed_amount': 'median_amount',             # Valley Medical Center
    '10th_percentile': 'pct_10',
    'tenth_percentile_allowed_amount': 'pct_10',          # Valley Medical Center
    '90th_percentile': 'pct_90',
    'ninetieth_percentile_allowed_amount': 'pct_90',      # Valley Medical Center
    'count': 'claim_count',
    'allowed_amount_count': 'claim_count',                # Valley Medical Center
    'count_of_compared_rates': 'claim_count',             # Cleveland Clinic + others
}


def normalize_header(header: str) -> str:
    """
    Tier 1 normalization:
    - lowercase
    - trim whitespace
    - strip surrounding quote characters (single, double, smart quotes)
      that some publishers leak into header rows
    - replace spaces and dashes with underscores
    - remove punctuation except underscores
    - collapse multiple underscores
    """
    h = header.strip().lower()
    # Strip surrounding quotes (straight + smart). A few MRF publishers emit
    # CSVs with literal quotes baked into header names, e.g.
    # `"standard_charge|gross"` instead of `standard_charge|gross`. Without
    # this strip, the pipe-pattern matcher in map_header() sees parts[0] as
    # `"standard_charge` and fails to match.
    h = h.strip('"\'\u201c\u201d\u2018\u2019')
    h = re.sub(r'[\s\-]+', '_', h)
    h = re.sub(r'[^\w_]', '', h)
    h = re.sub(r'_+', '_', h)
    h = h.strip('_')
    return h



def map_header(
    source_header: str,
    *,
    extra_synonyms: Optional[Dict[str, str]] = None,
) -> Tuple[str, Optional[str]]:
    """
    Map source_header to canonical field via Tier 1 synonym mapping.
    Handles piped patterns like 'code|1', 'code|1|type', 'standard_charge|gross'.
    Returns: (normalized_header, mapped_field or None)

    ``extra_synonyms`` maps normalized headers to canonical fields. It is
    consulted only when the built-in table finds nothing, and a target that
    is not in ``CANONICAL_FIELDS`` is ignored.
    """
    normalized = normalize_header(source_header)

    # First try exact match
    mapped_field = SYNONYM_MAP.get(normalized)

    # Strip surrounding quotes for the pipe-pattern matcher below - some MRFs
    # ship headers like `"standard_charge|gross"` (literal quotes included).
    # normalize_header() already strips them; do the same here so split()
    # produces clean parts.
    raw_for_pipes = source_header.strip().strip('"\'\u201c\u201d\u2018\u2019')

    # If no match and contains pipes, try pattern matching
    if not mapped_field and '|' in raw_for_pipes:
        parts = [p.strip() for p in raw_for_pipes.split('|')]
        
        # Pattern 1: code|1|type -> code_type
        if len(parts) >= 3 and 'type' in parts[-1].lower() and parts[0].lower() == 'code':
            combined = f"{parts[0]}_{parts[-1]}"
            combined_normalized = normalize_header(combined)
            mapped_field = SYNONYM_MAP.get(combined_normalized)
        
        # Pattern 2: standard_charge|gross, standard_charge|discounted_cash, etc.
        # Try combining first + last for 2-part patterns only
        elif not mapped_field and len(parts) == 2 and parts[0].lower() == 'standard_charge':
            # Try last part alone first
            last_normalized = normalize_header(parts[-1])
            mapped_field = SYNONYM_MAP.get(last_normalized)
            
            # If not found, try combining
            if not mapped_field:
                combined = f"{parts[0]}_{parts[-1]}"
                combined_normalized = normalize_header(combined)
                mapped_field = SYNONYM_MAP.get(combined_normalized)
        
        # Pattern 3: code|1 -> code (base pattern)
        elif not mapped_field and parts[0].lower() in ('code', ):
            base_normalized = normalize_header(parts[0])
            mapped_field = SYNONYM_MAP.get(base_normalized)

    if not mapped_field and extra_synonyms:
        target = extra_synonyms.get(normalized)
        if target in CANONICAL_FIELDS:
            mapped_field = target

    return normalized, mapped_field


# Rate-type suffixes in CMS wide CSV compound headers
_WIDE_RATE_SUFFIXES = {
    'negotiated_dollar': 'negotiated_rate',
    'negotiated_rate_dollar': 'negotiated_rate',
    'negotiated_percentage': 'negotiated_percentage',
    # Singular variant some publishers ship (e.g. compound forms of
    # `standard_charge|PAYER|negotiated_percent`).  Routes to the same
    # canonical as `negotiated_percentage` so wide-payer CSVs adopting the
    # singular spelling don't silently drop their per-payer rate.
    'negotiated_percent': 'negotiated_percentage',
    'negotiated_rate_percentage': 'negotiated_percentage',
    'negotiated_rate_percent': 'negotiated_percentage',
    'negotiated_algorithm': 'negotiated_algorithm',
    'negotiated_rate_algorithm': 'negotiated_algorithm',
    'methodology': 'methodology',
}

# Prefix variants accepted by parse_wide_payer_header(); keep in sync with its
# docstring and golden tests in tests/golden/parse_wide_payer_header.json.
_WIDE_STANDARD_CHARGE_PREFIXES = {
    'standard_charge',
    'standard_charges',
    'standard_charged',
}

_WIDE_ESTIMATED_AMOUNT_PREFIXES = {
    'estimated_amount',
    'estimate_amount',
}


def _wide_payer_plan(parts, min_len: int = 2):
    if len(parts) < min_len or not parts[1].strip():
        return None
    payer = parts[1].strip()
    plan = ' - '.join(p.strip() for p in parts[2:] if p.strip())
    return payer, plan


def parse_wide_payer_header(source_header: str):
    """
    Parse a CMS "wide" CSV compound header into (payer, plan, rate_field).

    Patterns handled (N = number of pipe-separated parts):

      standard_charge|PAYER|rate_suffix                (3 parts, plan is empty string)
      standard_charge|PAYER|PLAN|rate_suffix           (4 parts)
      standard_charge|PAYER|PLAN|PRODUCT|rate_suffix   (5 parts, plan becomes "PLAN - PRODUCT")

      estimated_amount|PAYER|PLAN                      (3 parts)
      estimated_amount|PAYER|PLAN|PRODUCT              (4 parts, plan becomes "PLAN - PRODUCT")

      additional_payer_notes|PAYER|PLAN                (3 parts)
      additional_payer_notes|PAYER|PLAN|PRODUCT        (4 parts, plan becomes "PLAN - PRODUCT")

      metric|PAYER|PLAN          (3 parts, metric = median_amount/10th_percentile/90th_percentile/count)
      metric|PAYER|PLAN|PRODUCT  (4 parts, plan becomes "PLAN - PRODUCT")

    When an extra PRODUCT segment is present (e.g. "MEDICARE"), it is appended
    to the plan name with " - " separator so that payer rates are grouped
    correctly without losing the product-line information.

    Returns (payer, plan, canonical_field) or None if not a compound payer header.
    """
    if '|' not in source_header:
        return None

    # Strip surrounding quote characters that some publishers leave embedded
    # in CSV headers (e.g. `"standard_charge|PAYER|gross"`). Without this the
    # split below produces a leading `"standard_charge` part that no prefix
    # check matches.
    cleaned = source_header.strip().strip('"\'\u201c\u201d\u2018\u2019')
    parts = [p.strip() for p in cleaned.split('|')]

    # Summit BHC West Virginia uses a leading empty segment after the prefix:
    #   standard_charge||PAYER|PLAN|PRODUCT|suffix   (6 parts)
    #   additional_payer_notes||PAYER|PLAN           (4 parts)
    #   estimated_amount||PAYER|PLAN|PRODUCT         (5 parts)
    # Collapse the leading empty slot so the rest of this function sees the
    # canonical 3/4/5-part shape.
    if len(parts) >= 3 and parts[1] == '':
        parts = [parts[0]] + parts[2:]

    prefix = normalize_header(parts[0])

    # standard_charge|PAYER|PLAN|rate_suffix (4 parts)
    # standard_charge|PAYER|PLAN|PRODUCT|rate_suffix (5 parts)
    # standard_charge|PAYER|rate_suffix (3 parts, no plan - e.g. Triwest)
    # Some publishers use standard_charges/standard_charged or split payer/plan
    # names across extra pipe segments; preserve the first segment as payer and
    # fold the remaining middle segments into the plan name.
    if prefix in _WIDE_STANDARD_CHARGE_PREFIXES and len(parts) >= 3:
        suffix = normalize_header(parts[-1])
        canonical = _WIDE_RATE_SUFFIXES.get(suffix)
        if canonical:
            parsed = _wide_payer_plan(parts[:-1])
            if parsed:
                payer, plan = parsed
                return (payer, plan, canonical)

    # estimated_amount|PAYER (2 parts, no plan - e.g. Triwest)
    # estimated_amount|PAYER|PLAN (3 parts)
    # estimated_amount|PAYER|PLAN|PRODUCT (4 parts)
    if prefix in _WIDE_ESTIMATED_AMOUNT_PREFIXES and len(parts) >= 2:
        parsed = _wide_payer_plan(parts)
        if parsed:
            payer, plan = parsed
            return (payer, plan, 'estimated_amount')

    # additional_payer_notes|PAYER (2 parts, no plan - e.g. Triwest)
    # additional_payer_notes|PAYER|PLAN (3 parts)
    # additional_payer_notes|PAYER|PLAN|PRODUCT (4 parts)
    if prefix == 'additional_payer_notes' and len(parts) >= 2:
        parsed = _wide_payer_plan(parts)
        if parsed:
            payer, plan = parsed
            return (payer, plan, 'additional_notes')

    # CMS statistical fields: metric|PAYER (2 parts, no plan - e.g. Triwest)
    # CMS statistical fields: metric|PAYER|PLAN (3 parts)
    # CMS statistical fields: metric|PAYER|PLAN|PRODUCT (4 parts)
    _WIDE_STAT_PREFIXES = {
        'median_amount': 'median_amount',
        '10th_percentile': 'pct_10',
        '90th_percentile': 'pct_90',
        'count': 'claim_count',
    }
    if prefix in _WIDE_STAT_PREFIXES and len(parts) >= 2:
        parsed = _wide_payer_plan(parts)
        if parsed:
            payer, plan = parsed
            return (payer, plan, _WIDE_STAT_PREFIXES[prefix])

    return None


# ---- Hawaii wide format: setting-specific columns ----
# Maps normalized column names to (canonical_field, setting) pairs.
# Each row becomes up to 3 charge items (one per setting).
HAWAII_SETTING_COLUMNS: Dict[str, Tuple[str, str]] = {
    'inpatientgrosscharge':            ('gross_charge', 'inpatient'),
    'outpatientgrosscharge':           ('gross_charge', 'outpatient'),
    'emergencyroomgrosscharge':        ('gross_charge', 'emergency'),
    'discountedcashpriceinpatient':    ('discounted_cash_price', 'inpatient'),
    'discountedcashpriceoutpatient':   ('discounted_cash_price', 'outpatient'),
    'discountedcashpriceemergencyroom': ('discounted_cash_price', 'emergency'),
}

# The three setting-specific gross charge columns that signal the Hawaii format
_HAWAII_DETECT_COLUMNS = frozenset({
    'inpatientgrosscharge',
    'outpatientgrosscharge',
    'emergencyroomgrosscharge',
})

# Setting tokens used in Hawaii payer column headers
_HAWAII_SETTINGS = {
    'Inpatient': 'inpatient',
    'Outpatient': 'outpatient',
    'EmergencyRoom': 'emergency',
}

# Methodology tokens used in Hawaii payer column headers
_HAWAII_METHODOLOGIES = {
    'PercentofCharges': 'percent_of_charge',
    'PerDiem': 'per_diem',
    'FeeSchedule': 'fee_schedule',
    'CaseRate': 'case_rate',
}


def parse_hawaii_payer_header(
    header: str,
) -> Optional[Tuple[str, str, str]]:
    """
    Parse a Hawaii-format payer column header into (payer_name, setting, methodology).

    Hawaii hospitals encode payer/setting/methodology as a single CamelCase
    column header with underscores separating the three parts:

        {PayerPlan}_{Setting}_{Methodology}
        {PayerPlan}_{Setting}_{Methodology}_{N}

    Examples:
        QuestIntegrationHMSA-ABDPlans_Outpatient_PercentofCharges
          → ("QuestIntegrationHMSA-ABDPlans", "outpatient", "percent_of_charge")

        OhanaCCSSMI-BehavioralHealthPlans_EmergencyRoom_FeeSchedule
          → ("OhanaCCSSMI-BehavioralHealthPlans", "emergency", "fee_schedule")

        QuestIntegrationAlohacare-ABD&NONABDPlansPlans_Inpatient_PerDiem_3
          → ("QuestIntegrationAlohacare-ABD&NONABDPlansPlans", "inpatient", "per_diem")

    Returns (payer_name, setting, methodology) or None if not a match.
    """
    parts = header.split('_')
    if len(parts) < 3:
        return None

    # Payer names may contain underscores too (rare), but after an optional
    # trailing numeric suffix we assume the final token is the methodology
    # and the token immediately before it is the setting. Everything to the
    # left is treated as the payer name.

    # Strip trailing numeric suffix (e.g. _3) used for duplicate columns
    # before identifying methodology/setting tokens.
    if parts[-1].isdigit():
        parts = parts[:-1]
    if len(parts) < 3:
        return None

    methodology_token = parts[-1]
    methodology = _HAWAII_METHODOLOGIES.get(methodology_token)
    if not methodology:
        return None

    setting_token = parts[-2]
    setting = _HAWAII_SETTINGS.get(setting_token)
    if not setting:
        return None

    payer = '_'.join(parts[:-2])
    if not payer:
        return None

    return (payer, setting, methodology)


# ---- Atrium wide format: setting-specific columns with underscore-separated names ----
# Atrium Health hospitals use a flat JSON/CSV format where each row has
# setting-specific charge columns:
#   " Inpatient Gross Charge " / " Outpatient Gross Charge "
#   " Inpatient Negotiated Charge " / " Outpatient Negotiated Charge "
#   " Inpatient Discounted Charge " / " Outpatient Discounted Charge "
#   " Gross Charge - Facility " / " Gross Charge - Non-Facility "
#   " Negotiated Charge - Facility " / " Negotiated Charge - Non-Facility "
# A "Min /Max" column indicates whether negotiated charges are min or max.
# Payer/Plan are in separate tall-format columns per row.
# Each row becomes up to 2 charge items (one per setting).
#
# This differs from Hawaii because:
# 1. Column names normalize with underscores (inpatient_gross_charge)
# 2. Payer/plan are in row-level columns (tall format), not in column headers
# 3. A Min/Max column determines which rate field gets the negotiated charge
# 4. Professional sections use Facility/Non-Facility instead of Inpatient/Outpatient

# Maps normalized column name → (canonical_field, setting)
ATRIUM_SETTING_COLUMNS: Dict[str, Tuple[str, str]] = {
    # Hospital sections: Inpatient/Outpatient
    'inpatient_gross_charge':       ('gross_charge', 'inpatient'),
    'outpatient_gross_charge':      ('gross_charge', 'outpatient'),
    'inpatient_negotiated_charge':  ('negotiated_charge', 'inpatient'),
    'outpatient_negotiated_charge': ('negotiated_charge', 'outpatient'),
    'inpatient_discounted_charge':  ('discounted_cash_price', 'inpatient'),
    'outpatient_discounted_charge': ('discounted_cash_price', 'outpatient'),
    # Professional sections: Facility/Non-Facility
    # CMS MPFS defines facility fees as rendered in a hospital/ASC (≈inpatient)
    # and non-facility fees as rendered in an office (≈outpatient), so they
    # map onto the standard inpatient/outpatient settings.
    'gross_charge_facility':          ('gross_charge', 'inpatient'),
    'gross_charge_non_facility':      ('gross_charge', 'outpatient'),
    'negotiated_charge_facility':     ('negotiated_charge', 'inpatient'),
    'negotiated_charge_non_facility': ('negotiated_charge', 'outpatient'),
}

# Minimum columns that signal the Atrium format - the two hospital gross
# charge columns are always present in Atrium files.
_ATRIUM_DETECT_COLUMNS = frozenset({
    'inpatient_gross_charge',
    'outpatient_gross_charge',
})


# ---------------------------------------------------------------------------
# Low-value unmapped header suppression (shared between CSV and JSON paths)
# ---------------------------------------------------------------------------
# Headers that appear in source files but have no canonical target and
# produce large volumes of unmapped cells without analytical value.
# Keeping these in one place ensures CSV and JSON readers stay in sync.
_SUPPRESS_UNMAPPED_HEADERS = {
    # outpatient pricing handled by fallback columns
    'op_price', 'min_op_reimb', 'max_op_reimb', 'op_expected_reimbursement',
    # pricing detail / cross-reference blobs
    'ip_pricing_detail', 'op_pricing_detail', 'ip_xr_detail', 'op_xr_detail',
    # Partners-specific row metadata
    'facility_id', 'contract', 'procedure', 'quantity',
    # code identifiers stored elsewhere or not yet canonical
    'rev_code', 'rev', 'ndc',
    # internal hospital IDs / mnemonic codes (no analytical value)
    'cdm', 'procedure_id', 'mnemonic', 'neumonic',
    # department / cost-center labels (no canonical field)
    'department',
    # garbage / separator columns (including headers that normalize to empty)
    'd', '',
    # section / category labels (no analytical value)
    'tabname',
    # KU Great Bend billing-specific min/max columns (nearly all NULL,
    # duplicates data already captured in standard_charge|min/max)
    'hospitalbilling_inpatient_min_price',
    'hospitalbilling_inpatient_max_price',
    'hospitalbilling_outpatient_min_price',
    'hospitalbilling_outpatient_max_price',
    'professionalbilling_inpatient_min_price',
    'professionalbilling_inpatient_max_price',
    'professionalbilling_outpatient_min_price',
    'professionalbilling_outpatient_max_price',
    # Middlesex internal classification / fiscal columns
    'revenue_code_description', 'fscrptcat3name', 'fscname',
    'fsccategory', 'script_type', 'service_area',
    # count / statistics metadata (mapped to claim_count in SYNONYM_MAP,
    # kept here as safety net for edge cases)
    'count_of_compared_rates',
    # Kennedy Krieger numbered notes (internal charge IDs / dept names)
    'additional_generic_notes1', 'additional_generic_notes2',
    # Alameda / Philip Health internal pricing/metadata columns
    # (kept individually for clarity; *_internal suffix regex below also
    # catches these and future variants)
    'lpp_id_internal', 'algorithm_factors_internal', 'contract_internal',
    'count_internal', 'location_internal', 'pricing_detail_internal',
    'xr_detail_internal',
    # revenue_code variant (rev_code already suppressed above)
    'revenue_code',
    # CMS JSON metadata fields (allowed_amounts is a nested structure
    # with no single canonical target; code type version is a string)
    'allowed_amounts', 'billing_code_type_version',
    # timestamp / audit metadata (no canonical field, no analytical value)
    'last_updated',
    # Secondary code columns. A charge item carries one code; a second code
    # (typically a rev code paired with a CPT) is duplicated in the row's
    # primary code/code_type columns or appears on a sibling row. Suppress
    # to avoid millions of unmapped cells from Ascension files.
    'code_2', 'code_2_type', 'code_3', 'code_3_type',
    'code_4', 'code_4_type',
    # Hospital-specific bare-token header columns that publishers leave in
    # CSVs (e.g. SURGCTR, WESTLAKE - surgery-center / facility tags). They
    # have no canonical target and produce huge volumes of unmapped rows.
    'surgctr', 'westlake',
    # Per-payer free-text note columns shipped by publishers as a single
    # rolled-up column. There is no per-payer notes field, and mapping to
    # additional_notes would conflict with the canonical generic-notes
    # column. Suppress.
    'additional_payer_specific_notes',
    # Source-local identifiers and ancillary timing fields without a canonical
    # price target.
    'charge_number', 'ivnum', 'mins_per_unit', 'financial_aid_policy',
    # Internal item-id columns published as `Item#`, `Item #`,
    # `Item No`, `Item No.` - they all normalize to `item` / `item_no`
    # (Prowers Medical Center ships ~27K cells per ingest).
    'item', 'item_no',
    # Rolled-up per-payer text notes column.  Same family as
    # `additional_payer_specific_notes` already suppressed above; some
    # publishers spell the suffix `payor_reimbursement` instead.
    'additional_generic_notes_payor_reimbursement',
    # Internal item-id / department metadata columns with no canonical target.
    # `Item ID` (normalizes to `item_id`, a sibling of `item`/`item_no` above),
    # `Dept #` (`dept`) and `Dept Name` (`dept_name`) - department cost-center
    # labels, same family as `department` suppressed above.  Observed shipping
    # tens of thousands of unmapped cells each (Item ID ~35K, Dept #/Dept Name
    # ~24K each).  Value/price columns from the same files (Average Charge Per
    # Unit, Amount, possible_amount) are deliberately NOT suppressed here - they
    # carry real prices and are tracked as synonym candidates instead.
    'item_id', 'dept', 'dept_name',
}

# Regex patterns for low-value columns that match a family of header names.
# Used in addition to the exact-match set above.  Each pattern is matched
# against the normalized header (lowercase, underscores only).
_SUPPRESS_UNMAPPED_PATTERNS = [
    # Alameda / Philip Health internal columns: anything_internal
    re.compile(r'^[a-z0-9_]+_internal$'),
    # Generic unnamed CSV columns: column1 .. column999 (Watsonville, etc.)
    re.compile(r'^column\d+$'),
    # Bare numeric headers - data row mis-read as header (WakeMed Cary,
    # Evanston, Swedish Covenant).  Suppressing avoids millions of
    # unmapped cells per file.
    re.compile(r'^\d+$'),
    # Bracket-form secondary code columns.  `code[2]` etc
    # normalize to `code2` (the brackets are stripped, NO underscore is
    # inserted), so the existing exact-match entries for `code_2..code_4`
    # / `code_2_type..code_4_type` miss the bracket variants.  Cover
    # N >= 2 only - `code[1]` / `code1` could be a primary code column
    # and a publisher labelling it that way would be a real mapping
    # problem we want to see in the unmapped cells, not silence here.
    re.compile(r'^code(?:[2-9]|[1-9]\d)(type)?$'),
]


def _is_unmapped_header_suppressed(normalized: str) -> bool:
    """Return True if a normalized header should be excluded from
    unmapped-cell tracking.  Used by both CSV and JSON readers.
    """
    if normalized in _SUPPRESS_UNMAPPED_HEADERS:
        return True
    return any(p.match(normalized) for p in _SUPPRESS_UNMAPPED_PATTERNS)

