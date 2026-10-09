"""Baked-modifier splitting and the description-consistency guard."""

from __future__ import annotations

import pytest

from mrfkit.codes import (
    _BAKED_MODIFIER_DIGIT_KEYS,
    _BAKED_MODIFIER_LETTER_KEYS,
    _BAKED_MODIFIER_SPLIT_KEYS,
    _description_matches,
    _split_attached_modifier_code,
    apply_baked_modifier_split,
    merge_modifier_into_field,
    normalize_code,
)
from mrfkit.reference import ReferenceData

# The known-modifier set passed to the pure helper. A focused subset that
# covers every guard branch.
KNOWN = frozenset({
    "TC",  # technical component (CPT numeric host)
    "SG",  # ASC facility (CPT numeric host)
    "QW",  # CLIA-waived (CPT numeric host)
    "LT",  # laterality (CPT numeric host)
    "RT",  # laterality
    "JW",  # discarded drug (HCPCS Level II host)
    "JZ",  # zero-discarded drug
    "26",  # numeric modifier (rare baked form but possible)
    "50",  # bilateral procedure
})


# ---------------------------------------------------------------------------
# _split_attached_modifier_code: the pure helper
# ---------------------------------------------------------------------------


class TestCptFiveDigitHostSplits:
    def test_73721tc_classic_baked_cpt_modifier(self):
        assert _split_attached_modifier_code("73721TC", "CDM", KNOWN) == ("73721", "CPT", "TC")

    def test_87077qw_clia_waived(self):
        assert _split_attached_modifier_code("87077QW", "LOCAL", KNOWN) == ("87077", "CPT", "QW")

    def test_36415sg_asc_facility(self):
        assert _split_attached_modifier_code("36415SG", "CDM", KNOWN) == ("36415", "CPT", "SG")

    def test_82274lt_left_side_modifier(self):
        assert _split_attached_modifier_code("82274LT", "CDM", KNOWN) == ("82274", "CPT", "LT")

    def test_two_digit_numeric_modifier_baked_in(self):
        """`50` and `26` are the common 2-digit atomic modifiers. The suffix
        shape allows them and the known-modifier guard confirms them."""
        assert _split_attached_modifier_code("7372150", "CDM", KNOWN) == ("73721", "CPT", "50")


class TestHcpcsLevelIiHostSplits:
    def test_j0591jw_discarded_drug(self):
        assert _split_attached_modifier_code("J0591JW", "CDM", KNOWN) == ("J0591", "HCPCS", "JW")

    def test_a4253jz_zero_discarded(self):
        """Any prefix letter in [A-CE-V] is valid, not just J/V."""
        assert _split_attached_modifier_code("A4253JZ", "CDM", KNOWN) == ("A4253", "HCPCS", "JZ")


class TestSourceCodeTypeGuard:
    """Only CDM, LOCAL or None are in scope: never cells already classified
    under a real coding system."""

    def test_already_cpt_is_left_alone(self):
        assert _split_attached_modifier_code("73721TC", "CPT", KNOWN) == ("73721TC", "CPT", None)

    def test_already_hcpcs_is_left_alone(self):
        assert _split_attached_modifier_code("J0591JW", "HCPCS", KNOWN) == ("J0591JW", "HCPCS", None)

    def test_already_ms_drg_is_left_alone(self):
        assert _split_attached_modifier_code("47010TC", "MS-DRG", KNOWN) == ("47010TC", "MS-DRG", None)

    def test_already_cdt_is_left_alone(self):
        assert _split_attached_modifier_code("D7510LT", "CDT", KNOWN) == ("D7510LT", "CDT", None)


class TestIcd10PcsGuard:
    """ICD-10-PCS codes (`02573ZZ`) end in letters that are part of the code."""

    def test_seven_char_icd_pcs_not_touched_even_if_zz_in_known(self):
        known_with_zz = KNOWN | {"ZZ"}
        assert _split_attached_modifier_code("02573ZZ", "ICD", known_with_zz) == ("02573ZZ", "ICD", None)

    def test_seven_char_icd_pcs_filed_as_cdm_still_not_touched(self):
        """Shape-wise `02573ZZ` could match, but ZZ is not a modifier."""
        assert _split_attached_modifier_code("02573ZZ", "CDM", KNOWN) == ("02573ZZ", "CDM", None)


class TestNdcGuard:
    """NDCs are 9-11+ digits: far longer than the 7-char anchored shape."""

    def test_eleven_digit_ndc_not_touched(self):
        assert _split_attached_modifier_code("00071015868", "CDM", KNOWN) == ("00071015868", "CDM", None)

    def test_nine_digit_ndc_not_touched(self):
        assert _split_attached_modifier_code("000710158", "CDM", KNOWN) == ("000710158", "CDM", None)


class TestUnknownSuffixGuard:
    def test_unknown_two_letter_suffix_rejected(self):
        assert _split_attached_modifier_code("12345AB", "CDM", KNOWN) == ("12345AB", "CDM", None)

    def test_unknown_alphanumeric_suffix_rejected(self):
        assert _split_attached_modifier_code("73721X9", "CDM", KNOWN) == ("73721X9", "CDM", None)

    def test_empty_known_set_means_never_split(self):
        assert _split_attached_modifier_code("73721TC", "CDM", frozenset()) == ("73721TC", "CDM", None)


class TestStructuralShape:
    def test_bare_cpt5_not_a_composite(self):
        assert _split_attached_modifier_code("73721", "CDM", KNOWN) == ("73721", "CDM", None)

    def test_six_digit_numeric_not_touched(self):
        assert _split_attached_modifier_code("123456", "CDM", KNOWN) == ("123456", "CDM", None)

    def test_eight_char_alphanumeric_not_touched(self):
        assert _split_attached_modifier_code("73721TCC", "CDM", KNOWN) == ("73721TCC", "CDM", None)

    def test_letter_prefixed_d_code_not_hcpcs(self):
        """D-prefix codes are CDT (dental), not HCPCS Level II."""
        assert _split_attached_modifier_code("D7510LT", "CDM", KNOWN) == ("D7510LT", "CDM", None)

    def test_letter_prefixed_w_x_y_z_not_hcpcs(self):
        for prefix in ("W", "X", "Y", "Z"):
            code = f"{prefix}1234LT"
            assert _split_attached_modifier_code(code, "CDM", KNOWN) == (code, "CDM", None), prefix


class TestEdgeCases:
    def test_empty_code_returns_unchanged(self):
        assert _split_attached_modifier_code("", "CDM", KNOWN) == ("", "CDM", None)

    def test_none_code_returns_unchanged(self):
        assert _split_attached_modifier_code(None, "CDM", KNOWN) == (None, "CDM", None)

    def test_whitespace_padded_code_is_trimmed_before_match(self):
        assert _split_attached_modifier_code("  73721TC  ", "CDM", KNOWN) == ("73721", "CPT", "TC")

    def test_lowercase_input_not_split(self):
        """The shapes are uppercase only: the splitter fails closed."""
        assert _split_attached_modifier_code("73721tc", "CDM", KNOWN) == ("73721tc", "CDM", None)

    def test_none_code_type_treated_as_in_scope(self):
        assert _split_attached_modifier_code("73721TC", None, KNOWN) == ("73721", "CPT", "TC")


class TestCanonicalPrefixGuardOptional:
    def test_none_skips_prefix_check(self):
        assert _split_attached_modifier_code("12345TC", "CDM", KNOWN) == ("12345", "CPT", "TC")

    def test_populated_map_accepts_known_prefix(self):
        assert _split_attached_modifier_code(
            "73721TC", "CDM", KNOWN,
            canonical_prefixes_by_type={"CPT": frozenset({"73721"}), "HCPCS": frozenset()},
        ) == ("73721", "CPT", "TC")

    def test_populated_map_rejects_unknown_prefix(self):
        """A 7-char string that factors as `<5-digit><modifier>` but whose
        prefix isn't a published CPT must NOT split."""
        assert _split_attached_modifier_code(
            "12345TC", "CDM", KNOWN,
            canonical_prefixes_by_type={"CPT": frozenset({"73721"}), "HCPCS": frozenset()},
        ) == ("12345TC", "CDM", None)

    def test_hcpcs_prefix_check_uses_hcpcs_bucket(self):
        """The bucket lookup keys on the RESOLVED target type."""
        assert _split_attached_modifier_code(
            "J0591JW", "CDM", KNOWN,
            canonical_prefixes_by_type={"CPT": frozenset(), "HCPCS": frozenset({"J0591"})},
        ) == ("J0591", "HCPCS", "JW")
        assert _split_attached_modifier_code(
            "J0591JW", "CDM", KNOWN,
            canonical_prefixes_by_type={"CPT": frozenset({"J0591"}), "HCPCS": frozenset()},
        ) == ("J0591JW", "CDM", None)

    def test_empty_buckets_reject_all(self):
        assert _split_attached_modifier_code(
            "73721TC", "CDM", KNOWN,
            canonical_prefixes_by_type={"CPT": frozenset(), "HCPCS": frozenset()},
        ) == ("73721TC", "CDM", None)

    def test_missing_bucket_treated_as_empty(self):
        assert _split_attached_modifier_code(
            "J0591JW", "CDM", KNOWN,
            canonical_prefixes_by_type={"CPT": frozenset({"J0591"})},
        ) == ("J0591JW", "CDM", None)


class TestDigitGatePureHelper:
    """Digit suffixes passing modifier_validity_by_code directly."""

    CPT_ONLY = {"CPT": frozenset({"73721"}), "HCPCS": frozenset()}

    def _split(self, code, **kw):
        return _split_attached_modifier_code(
            code, "CDM", _BAKED_MODIFIER_SPLIT_KEYS,
            digit_modifiers=_BAKED_MODIFIER_DIGIT_KEYS, **kw,
        )

    def test_authorized_pair_splits(self):
        assert self._split(
            "7372150", canonical_prefixes_by_type=self.CPT_ONLY,
            modifier_validity_by_code={"73721": frozenset({"50"})},
        ) == ("73721", "CPT", "50")

    def test_unauthorized_pair_rejected(self):
        code, _, mod = self._split(
            "7372150", canonical_prefixes_by_type=self.CPT_ONLY,
            modifier_validity_by_code={"73721": frozenset({"26"})},
        )
        assert mod is None
        assert code == "7372150"

    def test_none_validity_map_defaults_deny(self):
        _, _, mod = self._split(
            "7372150", canonical_prefixes_by_type=self.CPT_ONLY,
            modifier_validity_by_code=None,
        )
        assert mod is None

    def test_prefix_absent_from_validity_map_rejected(self):
        _, _, mod = self._split(
            "7372150", canonical_prefixes_by_type=self.CPT_ONLY,
            modifier_validity_by_code={"99213": frozenset({"25"})},
        )
        assert mod is None

    def test_letter_suffix_unaffected_by_digit_gate(self):
        assert self._split(
            "73721TC", canonical_prefixes_by_type=None, modifier_validity_by_code={},
        ) == ("73721", "CPT", "TC")

    def test_letter_suffix_bypasses_digit_gate(self):
        assert self._split(
            "73721TC", canonical_prefixes_by_type=None, modifier_validity_by_code=None,
        ) == ("73721", "CPT", "TC")


# ---------------------------------------------------------------------------
# The curated key sets
# ---------------------------------------------------------------------------


class TestBakedModifierKeySets:
    def test_split_set_is_frozenset(self):
        assert isinstance(_BAKED_MODIFIER_SPLIT_KEYS, frozenset)

    def test_partition_is_disjoint(self):
        assert _BAKED_MODIFIER_LETTER_KEYS & _BAKED_MODIFIER_DIGIT_KEYS == frozenset()

    def test_partition_covers_full_split_set(self):
        assert _BAKED_MODIFIER_LETTER_KEYS | _BAKED_MODIFIER_DIGIT_KEYS == _BAKED_MODIFIER_SPLIT_KEYS

    def test_letter_set_contains_no_pure_digit_tokens(self):
        assert {t for t in _BAKED_MODIFIER_LETTER_KEYS if t.isdigit()} == set()

    def test_digit_set_contains_only_pure_digit_tokens(self):
        assert {t for t in _BAKED_MODIFIER_DIGIT_KEYS if not t.isdigit()} == set()


# ---------------------------------------------------------------------------
# apply_baked_modifier_split: the wrapper with the curated set
# ---------------------------------------------------------------------------


class TestApplyBakedModifierSplit:
    def test_cpt_modifier_split_recognized(self):
        assert apply_baked_modifier_split("73721TC", "CDM") == ("73721", "CPT", "TC")

    def test_hcpcs_modifier_split_recognized(self):
        assert apply_baked_modifier_split("J0591JW", "LOCAL") == ("J0591", "HCPCS", "JW")

    def test_code_already_cpt_not_split(self):
        assert apply_baked_modifier_split("73721TC", "CPT") == ("73721TC", "CPT", None)

    def test_unknown_suffix_not_split(self):
        assert apply_baked_modifier_split("73721XX", "CDM") == ("73721XX", "CDM", None)

    def test_none_code_safe(self):
        assert apply_baked_modifier_split(None, "CDM") == (None, "CDM", None)

    def test_empty_code_safe(self):
        assert apply_baked_modifier_split("", "CDM") == ("", "CDM", None)

    def test_local_code_type_in_scope(self):
        _, code_type, baked = apply_baked_modifier_split("87077QW", "LOCAL")
        assert code_type in ("HCPCS", "CPT")
        assert baked == "QW"

    def test_end_to_end_with_normalize_and_merge(self):
        """normalize_code cannot split; the wrapper does, and the modifier
        merges into the row's existing modifiers."""
        code, code_type = normalize_code("73721TC", "CDM")
        code, code_type, baked = apply_baked_modifier_split(code, code_type)
        assert (code, code_type, baked) == ("73721", "CPT", "TC")
        assert merge_modifier_into_field("LT", baked) == "LT,TC"

    def test_87077qw(self):
        assert apply_baked_modifier_split("87077QW", "CDM") == ("87077", "CPT", "QW")

    def test_icd10pcs_shape_not_split(self):
        assert apply_baked_modifier_split("02573ZZ", "CDM") == ("02573ZZ", "CDM", None)

    def test_helper_returns_three_tuple(self):
        result = _split_attached_modifier_code("73721TC", "CDM", _BAKED_MODIFIER_SPLIT_KEYS)
        assert isinstance(result, tuple) and len(result) == 3

    def test_helper_rejects_cpt_input(self):
        assert _split_attached_modifier_code("73721TC", "CPT", _BAKED_MODIFIER_SPLIT_KEYS)[2] is None


PREFIXES = ReferenceData(code_prefixes={
    "CPT": frozenset({"73721", "87077", "36415", "82274"}),
    "HCPCS": frozenset({"J0591", "A4253"}),
})


class TestDigitTokensNeedValidity:
    """Even with a published prefix, digit tokens do not split without a
    validity matrix."""

    def test_73721_50_does_not_split(self):
        assert apply_baked_modifier_split("7372150", "CDM", ref=PREFIXES) == ("7372150", "CDM", None)

    def test_87077_26_does_not_split(self):
        assert apply_baked_modifier_split("8707726", "CDM", ref=PREFIXES) == ("8707726", "CDM", None)


class TestCoincidentalCdmIdsRejected:
    """Hospital CDM item ids that happen to factor as <5-digit><2-digit>. Each
    one's description was unrelated to the would-be CPT. None may split."""

    SAMPLES = [
        "5423177", "8982079", "8513158", "3115979", "2501254",
        "4400654", "4474526", "2770790", "4440625", "3775478",
        "4689226", "3072726", "4964750", "3773122", "4000059",
        "1242354", "1167891", "3333358", "1103455", "4954052",
    ]

    @pytest.mark.parametrize("code", SAMPLES)
    def test_not_split_without_reference(self, code):
        assert apply_baked_modifier_split(code, "CDM") == (code, "CDM", None)

    @pytest.mark.parametrize("code", SAMPLES)
    def test_not_split_with_permissive_prefixes(self, code):
        ref = ReferenceData(code_prefixes={
            "CPT": frozenset(c[:5] for c in self.SAMPLES), "HCPCS": frozenset(),
        })
        assert apply_baked_modifier_split(code, "CDM", ref=ref)[2] is None


class TestCanonicalPrefixGuardOnLetterPath:
    def test_real_cpt_letter_modifier_splits(self):
        assert apply_baked_modifier_split("73721TC", "CDM", ref=PREFIXES) == ("73721", "CPT", "TC")

    def test_fake_cpt_letter_modifier_rejected(self):
        assert apply_baked_modifier_split("99999TC", "CDM", ref=PREFIXES) == ("99999TC", "CDM", None)

    def test_real_hcpcs_letter_modifier_splits(self):
        assert apply_baked_modifier_split("J0591JW", "CDM", ref=PREFIXES) == ("J0591", "HCPCS", "JW")

    def test_fake_hcpcs_letter_modifier_rejected(self):
        assert apply_baked_modifier_split("V9999JW", "CDM", ref=PREFIXES)[2] is None

    def test_no_reference_skips_prefix_guard(self):
        assert apply_baked_modifier_split("73721TC", "CDM") == ("73721", "CPT", "TC")


VALIDITY = ReferenceData(
    code_prefixes={"CPT": frozenset({"73721", "87077", "36415", "82274"}), "HCPCS": frozenset({"J0591"})},
    modifier_validity={"73721": frozenset({"50"}), "87077": frozenset({"26"})},
)


class TestDigitTokensGatedByValidity:
    def test_7372150_splits_when_bilateral_valid(self):
        assert apply_baked_modifier_split("7372150", "CDM", ref=VALIDITY) == ("73721", "CPT", "50")

    def test_7372126_rejected_when_26_not_in_matrix(self):
        assert apply_baked_modifier_split("7372126", "CDM", ref=VALIDITY) == ("7372126", "CDM", None)

    def test_8707726_splits_when_pctc_valid(self):
        assert apply_baked_modifier_split("8707726", "CDM", ref=VALIDITY) == ("87077", "CPT", "26")

    def test_8707750_rejected_when_not_bilateral(self):
        assert apply_baked_modifier_split("8707750", "CDM", ref=VALIDITY)[2] is None

    def test_hcpcs_digit_composite_rejected(self):
        """The validity matrix is CPT-only, so HCPCS digit composites never split."""
        assert apply_baked_modifier_split("J059150", "CDM", ref=VALIDITY) == ("J059150", "CDM", None)

    @pytest.mark.parametrize("code", ["5423177", "8982079", "4954052", "1167891", "4000059"])
    def test_coincidental_ids_still_rejected(self, code):
        assert apply_baked_modifier_split(code, "CDM", ref=VALIDITY) == (code, "CDM", None)

    def test_none_validity_keeps_digits_dormant(self):
        ref = ReferenceData(code_prefixes={"CPT": frozenset({"73721", "87077"}), "HCPCS": frozenset()})
        assert apply_baked_modifier_split("7372150", "CDM", ref=ref) == ("7372150", "CDM", None)
        assert apply_baked_modifier_split("8707726", "CDM", ref=ref) == ("8707726", "CDM", None)


# ---------------------------------------------------------------------------
# merge_modifier_into_field
# ---------------------------------------------------------------------------


class TestMergeModifierIntoField:
    def test_baked_appended_to_existing(self):
        assert merge_modifier_into_field("LT", "TC") == "LT,TC"

    def test_empty_existing_returns_baked(self):
        assert merge_modifier_into_field(None, "TC") == "TC"
        assert merge_modifier_into_field("", "TC") == "TC"
        assert merge_modifier_into_field("   ", "TC") == "TC"

    def test_empty_baked_returns_existing(self):
        assert merge_modifier_into_field("LT", None) == "LT"
        assert merge_modifier_into_field("LT", "") == "LT"
        assert merge_modifier_into_field("LT", "   ") == "LT"

    def test_both_empty(self):
        assert merge_modifier_into_field(None, None) is None
        assert merge_modifier_into_field("", "") == ""

    def test_dedups_when_baked_already_present(self):
        assert merge_modifier_into_field("TC,LT", "TC") == "TC,LT"
        assert merge_modifier_into_field("LT,TC", "TC") == "LT,TC"

    def test_tokenization_delimiters(self):
        assert merge_modifier_into_field("LT|RT", "RT") == "LT,RT"
        assert merge_modifier_into_field("LT;RT", "TC") == "LT,RT,TC"
        assert merge_modifier_into_field("LT, RT", "TC") == "LT,RT,TC"

    def test_output_is_canonical_comma_join(self):
        out = merge_modifier_into_field("LT|RT", "TC")
        assert "," in out and " " not in out


# ---------------------------------------------------------------------------
# _description_matches (reference descriptions below are synthetic)
# ---------------------------------------------------------------------------


class TestDescriptionMatch:
    def test_xr_abbreviation_and_bilat(self):
        assert _description_matches("XR FEMUR 2VWS BILAT", "Femur X-ray study")

    def test_hyphenated_shorthand(self):
        assert _description_matches("SERUM PROTEIN E-PHORESIS", "Protein electrophoresis, serum")

    def test_stopwords_do_not_dilute_coverage(self):
        # {BIOPSY, SKIN, NAIL} after stop-words go: 2 of 3 covered. Counting
        # OF/THE/AND would leave 2 of 6, below the threshold.
        assert _description_matches("SKIN BIOPSY PUNCH", "Biopsy of the skin and nail")
        # Stop-words alone must never create a match.
        assert not _description_matches("WITH AND OF THE", "Foreign object extraction")

    def test_x_rays_plural(self):
        assert _description_matches("ARTERY XRAYS ADRENAL GLAND PC", "Adrenal artery X-rays")

    def test_drug_vs_procedure(self):
        assert not _description_matches("GUAIFENESIN 100 MG/5ML PO LIQD", "Umbilical repair surgery")

    def test_generic_other_outpatient(self):
        assert not _description_matches("Other Outpatient", "Mastoid X-ray study")

    def test_device_vs_procedure(self):
        assert not _description_matches("SCREWDRIVER OSTEODRIVER 2", "Burn wound dressing")

    @pytest.mark.parametrize("row,ref", [("", "anything"), (None, "anything"), ("anything", ""), ("anything", None)])
    def test_empty_inputs_deny(self, row, ref):
        assert not _description_matches(row, ref)

    def test_generic_misc(self):
        assert not _description_matches("MISC", "Femur X-ray study")

    def test_generic_outpatient(self):
        assert not _description_matches("OUTPATIENT", "Femur X-ray study")


DESCRIBED = ReferenceData(
    code_prefixes={"CPT": frozenset({"73721", "87077"}), "HCPCS": frozenset()},
    modifier_validity={"73721": frozenset({"50"}), "87077": frozenset({"26"})},
    code_descriptions={"73721": "Knee X-ray study", "87077": "Bacteria culture isolate"},
)


class TestDigitSplitDescriptionGuard:
    def test_matching_description_splits(self):
        assert apply_baked_modifier_split(
            "7372150", "CDM", description="XR KNEE 3 VIEWS BILATERAL", ref=DESCRIBED,
        ) == ("73721", "CPT", "50")

    def test_mismatched_description_rejects(self):
        assert apply_baked_modifier_split(
            "7372150", "CDM", description="GUAIFENESIN 100MG TABLET", ref=DESCRIBED,
        ) == ("7372150", "CDM", None)

    def test_none_row_description_rejects(self):
        assert apply_baked_modifier_split(
            "7372150", "CDM", description=None, ref=DESCRIBED,
        ) == ("7372150", "CDM", None)

    def test_no_descriptions_skips_guard(self):
        assert apply_baked_modifier_split("7372150", "CDM", ref=VALIDITY) == ("73721", "CPT", "50")

    def test_letter_suffix_unaffected_by_description_guard(self):
        assert apply_baked_modifier_split(
            "73721TC", "CDM", description="GUAIFENESIN TABLET UNRELATED DESCRIPTION", ref=DESCRIBED,
        ) == ("73721", "CPT", "TC")

    def test_empty_descriptions_reject_digit_split(self):
        ref = ReferenceData(
            code_prefixes=DESCRIBED.code_prefixes,
            modifier_validity=DESCRIBED.modifier_validity,
            code_descriptions={},
        )
        assert apply_baked_modifier_split(
            "7372150", "CDM", description="XR KNEE BILATERAL", ref=ref,
        ) == ("7372150", "CDM", None)
