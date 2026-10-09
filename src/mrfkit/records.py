"""The rows mrfkit produces.

Every record carries its charge item's key (code, code_type, billing_class,
setting, modifiers), so standard charges, payer rates and unmapped cells join
back to their item without a database. Values are what the file says after
normalization: mrfkit never truncates text or clamps numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Optional


@dataclass(slots=True)
class ChargeItem:
    """One billable item. Emitted once per distinct item key in a file."""

    TABLE: ClassVar[str] = "charge_items"

    code: Optional[str]
    code_type: Optional[str]
    description: Optional[str]
    billing_class: str = ""
    setting: str = ""
    modifiers: Optional[str] = None
    drug_unit_of_measurement: Optional[str] = None
    drug_type_of_measurement: Optional[str] = None
    additional_generic_notes: Optional[str] = None


@dataclass(slots=True)
class StandardCharge:
    """The payer-independent prices for an item."""

    TABLE: ClassVar[str] = "standard_charges"

    code: Optional[str]
    code_type: Optional[str]
    description: Optional[str]
    billing_class: str = ""
    setting: str = ""
    modifiers: Optional[str] = None
    gross_charge: Optional[float] = None
    discounted_cash_price: Optional[float] = None
    min_negotiated_rate: Optional[float] = None
    max_negotiated_rate: Optional[float] = None


@dataclass(slots=True)
class PayerRate:
    """One negotiated rate for an item under one payer and plan.

    ``billing_class``, ``setting`` and ``modifiers`` are the item's key.
    ``rate_billing_class`` and ``rate_setting`` are what the rate itself says,
    which can differ from the item's.
    """

    TABLE: ClassVar[str] = "payer_rates"

    code: Optional[str]
    code_type: Optional[str]
    description: Optional[str]
    billing_class: str = ""
    setting: str = ""
    modifiers: Optional[str] = None
    payer_name: Optional[str] = None       # normalized
    raw_payer_name: Optional[str] = None   # as written in the file
    plan_name: Optional[str] = None
    plan_category: str = "Other"
    plan_network: Optional[str] = None
    negotiated_rate: Optional[float] = None
    negotiated_percentage: Optional[float] = None
    negotiated_algorithm: Optional[str] = None
    methodology: Optional[str] = None       # as written, minus audit notes and URLs
    methodology_type: Optional[str] = None  # normalized category
    estimated_amount: Optional[float] = None
    rate_billing_class: str = ""
    rate_setting: str = ""
    additional_notes: Optional[str] = None
    footnote: Optional[str] = None
    median_amount: Optional[float] = None
    pct_10: Optional[float] = None
    pct_90: Optional[float] = None
    claim_count: Optional[int] = None


@dataclass(slots=True)
class UnmappedCell:
    """A value from a column mrfkit could not map to a field.

    Kept so nothing in the file is silently lost, and so new header synonyms
    can be found. Capped per column by the readers.
    """

    TABLE: ClassVar[str] = "unmapped_cells"

    code: Optional[str]
    code_type: Optional[str]
    description: Optional[str]
    billing_class: str = ""
    setting: str = ""
    modifiers: Optional[str] = None
    source_column: str = ""
    value: Optional[str] = None


@dataclass(slots=True)
class FileMetadata:
    """What the file says about itself: hospital, license, dates, attestation.

    Several locations or addresses are joined with ``|``, as the CMS CSV
    template writes them.
    """

    TABLE: ClassVar[str] = "file_metadata"

    hospital_name: Optional[str] = None
    hospital_location: Optional[str] = None
    hospital_address: Optional[str] = None
    cms_certification_number: Optional[str] = None
    license_number: Optional[str] = None
    license_state: Optional[str] = None
    type_2_npi: Optional[str] = None
    ein: Optional[str] = None
    last_updated_on: Optional[str] = None
    version: Optional[str] = None
    attestation: Optional[str] = None
    attester_name: Optional[str] = None
    confirm_attestation: Optional[bool] = None


@dataclass(slots=True)
class HeaderMapping:
    """How one source column (or JSON key) was read.

    ``mapped_to`` is a canonical field name, a layout tag such as
    ``wide_payer:negotiated_rate``, or None for an unmapped column.
    """

    TABLE: ClassVar[str] = "header_mappings"

    source_header: str
    normalized: str
    mapped_to: Optional[str] = None


@dataclass(slots=True)
class ModifierInfo:
    """A modifier the file defines (CMS 3.0 ``modifier_information``).

    One general record per modifier code, then one per payer with its own
    description. ``payer_name`` is normalized; ``raw_payer_name`` is as written.
    """

    TABLE: ClassVar[str] = "modifiers"

    code: str
    description: Optional[str] = None
    setting: Optional[str] = None
    payer_name: Optional[str] = None
    raw_payer_name: Optional[str] = None
    plan_name: Optional[str] = None
    payer_description: Optional[str] = None
