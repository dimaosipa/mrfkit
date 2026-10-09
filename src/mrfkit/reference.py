"""Optional reference data and parse counters for the normalizers.

Every normalizer works with no reference data at all. When you have lookup
tables (published code lists, payer aliases you curate, payers you already
know), put them in a ``ReferenceData`` and pass it as ``ref=``. When you want
to know how many rows were rejected or repaired, pass a ``ParseStats`` as
``stats=``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Mapping, Optional

from mrfkit.payers import _clean_payer_key


@dataclass(frozen=True)
class ReferenceData:
    """Lookup tables that sharpen normalization. Every field is optional.

    code_prefixes
        ``{'CPT': frozenset(codes), 'HCPCS': frozenset(codes)}`` of published
        codes. When set, a modifier baked into a code (``73721TC``) is split
        only if the remaining code is in this list. ``None`` skips the check;
        an empty set for a type rejects every split of that type.
    modifier_validity
        CPT code -> frozenset of digit modifiers CMS marks valid for it
        (``{'73721': frozenset({'50'})}``). Digit modifiers baked into a code
        split only when this confirms the pair. ``None`` means they never
        split.
    code_descriptions
        CPT code -> a short description you hold the rights to. When set, a
        digit modifier split also requires the row description to resemble
        it.
    payer_aliases
        Raw payer name -> canonical payer name, checked before the built-in
        map. Keys are cleaned (quotes, brackets, underscores, spacing) and
        uppercased for you.
    payer_match_index
        ``payer_match_key(name)`` -> canonical payer name of payers you
        already know. Spelling variants of an otherwise unknown payer fold
        onto the name here. When the index is not empty, new payers are added
        to it as they are seen, so later variants in the same run fold onto
        the first spelling.
    methodology_aliases
        Raw methodology text -> methodology type, checked before the built-in
        map. Keys are uppercased for you.
    """

    code_prefixes: Optional[Mapping[str, FrozenSet[str]]] = None
    modifier_validity: Optional[Mapping[str, FrozenSet[str]]] = None
    code_descriptions: Optional[Mapping[str, str]] = None
    payer_aliases: Dict[str, str] = field(default_factory=dict)
    payer_match_index: Dict[str, str] = field(default_factory=dict)
    methodology_aliases: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        aliases = {}
        for alias, canonical in self.payer_aliases.items():
            key = _clean_payer_key(alias)
            if key and canonical:
                aliases[key] = canonical
        object.__setattr__(self, 'payer_aliases', aliases)
        object.__setattr__(self, 'payer_match_index', dict(self.payer_match_index))
        object.__setattr__(
            self, 'methodology_aliases',
            {k.upper(): v for k, v in self.methodology_aliases.items()},
        )


@dataclass
class ParseStats:
    """Counts of rows the normalizers rejected or repaired.

    Pass one instance per file as ``stats=`` to ``is_rejected_code`` and
    ``apply_baked_modifier_split``. Sample lists keep at most
    ``sample_limit`` entries, with each value cut short, so a file with
    millions of bad rows cannot blow up memory.
    """

    sample_limit: int = 20
    rows_read: int = 0
    rows_skipped: int = 0
    warnings: List[str] = field(default_factory=list)
    rejected_codes: int = 0
    rejected_code_samples: List[Dict[str, str]] = field(default_factory=list)
    baked_modifier_splits: int = 0
    baked_modifiers: Dict[str, int] = field(default_factory=dict)
    baked_modifier_samples: List[Dict[str, str]] = field(default_factory=list)

    def warn(self, message: str) -> None:
        """Keep *message* for the caller and log it."""
        self.warnings.append(message)
        logging.getLogger("mrfkit").warning(message)

    def record_rejected_code(
        self, code: Optional[str], code_type: Optional[str],
    ) -> None:
        self.rejected_codes += 1
        if len(self.rejected_code_samples) < self.sample_limit:
            self.rejected_code_samples.append({
                'code': (code or '')[:32],
                'code_type': (code_type or '')[:32],
            })

    def record_baked_modifier_split(
        self, baked_modifier: Optional[str], source_code_type: Optional[str],
    ) -> None:
        if not baked_modifier:
            return
        self.baked_modifier_splits += 1
        self.baked_modifiers[baked_modifier] = (
            self.baked_modifiers.get(baked_modifier, 0) + 1
        )
        if len(self.baked_modifier_samples) < self.sample_limit:
            self.baked_modifier_samples.append({
                'baked_modifier': baked_modifier[:8],
                'source_code_type': (source_code_type or '')[:16],
            })
