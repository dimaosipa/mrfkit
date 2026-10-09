"""ReferenceData reaches the normalizers; ParseStats counts what they did."""

from __future__ import annotations

from mrfkit.codes import apply_baked_modifier_split, is_rejected_code
from mrfkit.payers import is_valid_payer_name, normalize_payer_name
from mrfkit.reference import ParseStats, ReferenceData
from mrfkit.values import normalize_methodology

# ---------------------------------------------------------------------------
# ReferenceData
# ---------------------------------------------------------------------------


def test_empty_reference_matches_no_reference():
    ref = ReferenceData()
    for raw in ("AETNA HMO [1001103]", "Brand New Health Plans", "95% Cigna"):
        assert normalize_payer_name(raw, ref=ref) == normalize_payer_name(raw)
    assert normalize_methodology("Fee Schedule", ref=ref) == "fee_schedule"
    assert apply_baked_modifier_split("7372150", "CDM", ref=ref) == ("7372150", "CDM", None)


def test_payer_alias_is_honoured():
    assert normalize_payer_name("Acme Regional Plan")[0] == "Acme Regional Plan"
    ref = ReferenceData(payer_aliases={"acme_regional  plan": "Acme Health"})
    assert normalize_payer_name("Acme Regional Plan", ref=ref) == ("Acme Health", None)
    # Bracket and contract-id noise is stripped before the alias lookup.
    assert normalize_payer_name("[Acme Regional Plan]", "PPO", ref=ref) == ("Acme Health", "PPO")


def test_payer_alias_beats_the_built_in_map():
    ref = ReferenceData(payer_aliases={"AETNA": "Aetna Inc"})
    assert normalize_payer_name("Aetna", ref=ref)[0] == "Aetna Inc"


def test_payer_alias_reaches_recursive_branches():
    ref = ReferenceData(payer_aliases={"Acme Regional Plan": "Acme Health"})
    assert normalize_payer_name("95% Acme Regional Plan", ref=ref)[0] == "Acme Health"
    assert normalize_payer_name("Wc Acme Regional Plan", ref=ref) == ("Acme Health", "Workers Comp")


def test_is_valid_payer_name_uses_reference():
    ref = ReferenceData(payer_aliases={"Acme": "Acme Health"})
    assert is_valid_payer_name("Acme", ref=ref) is True


def test_methodology_alias_is_honoured():
    ref = ReferenceData(methodology_aliases={"Custom Methodology": "per_diem"})
    assert normalize_methodology("Custom Methodology") == "other"
    assert normalize_methodology("Custom Methodology", ref=ref) == "per_diem"


def test_methodology_alias_beats_built_in_map_and_numbers():
    ref = ReferenceData(methodology_aliases={"FEE SCHEDULE": "case_rate", "150": "fee_schedule"})
    assert normalize_methodology("Fee Schedule", ref=ref) == "case_rate"
    assert normalize_methodology("150", ref=ref) == "fee_schedule"


def test_code_prefixes_are_honoured():
    ref = ReferenceData(code_prefixes={"CPT": frozenset({"73721"}), "HCPCS": frozenset()})
    assert apply_baked_modifier_split("12345TC", "CDM") == ("12345", "CPT", "TC")
    assert apply_baked_modifier_split("12345TC", "CDM", ref=ref) == ("12345TC", "CDM", None)
    assert apply_baked_modifier_split("73721TC", "CDM", ref=ref) == ("73721", "CPT", "TC")


def test_payer_match_index_is_copied_per_reference():
    seed = {"SEED": "Seed"}
    ref = ReferenceData(payer_match_index=seed)
    normalize_payer_name("Brand New Health Plans", ref=ref)
    assert seed == {"SEED": "Seed"}
    assert "BRAND NEW HEALTH PLAN" in ref.payer_match_index


# ---------------------------------------------------------------------------
# ParseStats
# ---------------------------------------------------------------------------


def test_rejections_are_counted():
    stats = ParseStats()
    assert is_rejected_code("99213", "1", stats=stats) == "numeric_code_type"
    assert is_rejected_code("0.1234", "MS-DRG", stats=stats) == "ms_drg_decimal"
    assert is_rejected_code("99213", "CPT", stats=stats) is None
    assert stats.rejected_codes == 2
    assert stats.rejected_code_samples == [
        {"code": "99213", "code_type": "1"},
        {"code": "0.1234", "code_type": "MS-DRG"},
    ]


def test_baked_modifier_splits_are_counted():
    stats = ParseStats()
    apply_baked_modifier_split("73721TC", "CDM", stats=stats)
    apply_baked_modifier_split("73721TC", "LOCAL", stats=stats)
    apply_baked_modifier_split("87077QW", "CDM", stats=stats)
    apply_baked_modifier_split("73721XX", "CDM", stats=stats)  # no split
    assert stats.baked_modifier_splits == 3
    assert stats.baked_modifiers == {"TC": 2, "QW": 1}
    assert stats.baked_modifier_samples[0] == {"baked_modifier": "TC", "source_code_type": "CDM"}
    assert stats.baked_modifier_samples[1]["source_code_type"] == "LOCAL"


def test_empty_modifier_does_not_record():
    stats = ParseStats()
    stats.record_baked_modifier_split("", "CDM")
    stats.record_baked_modifier_split(None, "CDM")
    assert stats.baked_modifier_splits == 0
    assert stats.baked_modifier_samples == []


def test_samples_are_bounded():
    stats = ParseStats()
    for _ in range(50):
        stats.record_baked_modifier_split("TC", "CDM")
        stats.record_rejected_code("X", "1")
    assert stats.baked_modifier_splits == 50
    assert stats.rejected_codes == 50
    assert len(stats.baked_modifier_samples) == 20
    assert len(stats.rejected_code_samples) == 20
    assert stats.baked_modifiers["TC"] == 50


def test_sample_limit_is_configurable():
    stats = ParseStats(sample_limit=3)
    for _ in range(10):
        stats.record_baked_modifier_split("TC", "CDM")
    assert stats.baked_modifier_splits == 10
    assert len(stats.baked_modifier_samples) == 3


def test_long_values_are_cut_in_samples():
    stats = ParseStats()
    stats.record_baked_modifier_split("TC" * 100, "CDM" * 100)
    stats.record_rejected_code("9" * 100, "X" * 100)
    assert len(stats.baked_modifier_samples[0]["baked_modifier"]) <= 8
    assert len(stats.baked_modifier_samples[0]["source_code_type"]) <= 16
    assert len(stats.rejected_code_samples[0]["code"]) <= 32
    assert len(stats.rejected_code_samples[0]["code_type"]) <= 32
