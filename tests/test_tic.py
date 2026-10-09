"""Insurer Transparency in Coverage readers: in-network rates and tables of contents.

The in-network fixture is a cut of a real Blue Shield of California network
file with poison rows added, and with every item name and description
replaced by neutral text (the originals are descriptor text this project
does not ship). Its numbers match the original parser exactly; a
differential run checked every record when the reader was ported.
"""

import copy
import dataclasses
import gzip
import io
import json
import zipfile
from pathlib import Path

import ijson
import pytest

import mrfkit
from mrfkit import tic
from mrfkit.cli import main
from mrfkit.records import TicFileMetadata, TicIndexEntry, TicProviderGroup, TicRate
from mrfkit.reference import ParseStats
from mrfkit.tic import group_tic_networks, iter_tic_in_network, iter_tic_index, tic_network_key

FIXTURES = Path(__file__).parent / "fixtures"
IN_NETWORK = FIXTURES / "tic" / "bsca-1056_in-network-rates.json.gz"
INDEX = FIXTURES / "tic" / "example_index.json"
HOSPITAL_JSON = FIXTURES / "mrf" / "cms-v3_standardcharges.json"

PRICES = "$.in_network[].negotiated_rates[].negotiated_prices[]"
GROUPS = "$.provider_references[].provider_groups[]"

# 164 provider references: 161 from the real file plus 3 poison groups
# (non-numeric NPI, out-of-range NPI, missing TIN). 168 groups across them;
# one has an empty NPI list and the 3 poison groups are dropped, leaving 164.
# Two of those repeat an earlier (reference, TIN, NPIs) row (one reference is
# listed twice, another lists its group twice), so 162 group records, and
# 161 distinct (TIN, NPIs) groups: one reference shares another's group.
EXPECTED_GROUP_RECORDS = 162
EXPECTED_DISTINCT_GROUPS = 161
# 9 real items + 2 poison items: one with no billing code, one (77777) with
# 4 poison prices.
EXPECTED_PRICES_READ = 41
# 36 real prices (one more has a bogus negotiated_type) + the 77777 price
# with an unparseable expiration date, which is reported, then kept.
EXPECTED_RATES = 37
EXPECTED_GROUP_SETS = 14
# 3 poison groups + the code-less item (1) + 3 poison prices (missing rate,
# rate too big, percentage too big) + the bogus negotiated_type price.
EXPECTED_ROWS_SKIPPED = 8


def _read(path, **kwargs):
    stats = ParseStats()
    return list(iter_tic_in_network(path, stats=stats, **kwargs)), stats


def _of(records, cls):
    return [r for r in records if isinstance(r, cls)]


def _fixture_dict():
    with gzip.open(IN_NETWORK, "rt") as f:
        return json.load(f)


def _write(tmp_path, name, data=None, text=None):
    path = tmp_path / name
    with gzip.open(path, "wt") as f:
        f.write(text if text is not None else json.dumps(data))
    return path


def _members(records):
    """group_id -> set of (tin, npis) it stands for."""
    out = {}
    for g in _of(records, TicProviderGroup):
        out.setdefault(g.group_id, set()).add((g.tin, g.npis))
    return out


def _quirk_variant(tmp_path):
    """A hyphenated EIN, an NPI-typed TIN, a comma-packed modifier string, and
    two provider groups priced identically under one code."""
    data = _fixture_dict()
    data["provider_references"] = [
        {"provider_group_id": 90001, "network_name": ["Quirks"],
         "provider_groups": [{"npi": [1194728220],
                              "tin": {"type": "ein", "value": "30-0318970"}}]},
        {"provider_group_id": 90002, "network_name": ["Quirks"],
         "provider_groups": [{"npi": [1114438157],
                              "tin": {"type": "npi", "value": "1114438157"}}]},
    ] + data["provider_references"]
    price = {"negotiated_type": "negotiated", "negotiated_rate": 123.45,
             "expiration_date": "9999-12-31", "service_code": ["11"],
             "billing_class": "professional", "setting": "outpatient",
             "billing_code_modifier": ["52,53"]}
    data["in_network"].append({
        "negotiation_arrangement": "ffs", "billing_code_type": "CPT",
        "billing_code_type_version": "2026", "billing_code": "99999",
        "severity_of_illness": "",
        "negotiated_rates": [
            {"provider_references": [90001], "negotiated_prices": [copy.deepcopy(price)]},
            {"provider_references": [90002], "negotiated_prices": [copy.deepcopy(price)]},
        ],
    })
    return _write(tmp_path, "quirks.json.gz", data)


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------

class TestHelpers:

    def test_normalize_tin(self):
        assert tic._normalize_tin("30-0318970") == "300318970"
        assert tic._normalize_tin(" 12 345-6789 ") == "123456789"
        assert tic._normalize_tin(1114438157) == "1114438157"
        for empty in (None, "", "   ", "EIN"):
            assert tic._normalize_tin(empty) is None

    def test_split_modifiers(self):
        assert tic._split_modifiers(["52,53"]) == ["52", "53"]
        assert tic._split_modifiers([" 26 ", "26", "TC"]) == ["26", "TC"]
        assert tic._split_modifiers(["", ",", None]) == []
        assert tic._split_modifiers(None) == []

    def test_valid_npi(self):
        assert tic._valid_npi(1234567893) == 1234567893
        assert tic._valid_npi("1234567893") == 1234567893
        for bad in (123, 10_000_000_000, "not-a-number", None, -1234567893):
            assert tic._valid_npi(bad) is None

    def test_parse_expiration(self):
        stats = ParseStats()
        assert tic._parse_expiration("2026-12-31", stats) == "2026-12-31"
        assert tic._parse_expiration("", stats) == "9999-12-31"
        assert tic._parse_expiration(None, stats) == "9999-12-31"
        assert stats.unmapped_paths == {}  # missing is normal, not reported
        assert tic._parse_expiration("12/31/2026", stats) == "9999-12-31"
        assert stats.unmapped_paths == {f"{PRICES}.expiration_date=unparseable": 1}

    def test_repairs_bad_escapes_and_keeps_valid_ones(self):
        out, n = tic._repair_escapes(
            b'{"a": "\\WAKE", "b": "x\\\\y", "c": "tab\\t", "d": "\\u0041"}')
        assert out == b'{"a": "\\\\WAKE", "b": "x\\\\y", "c": "tab\\t", "d": "\\u0041"}'
        assert n == 1
        json.loads(out)

    def test_backslash_split_across_reads_is_carried(self):
        raw = b'{"a": "\\WAKE"}'
        for size in (1, 2, 3, 5, 7):
            stream = tic._EscapeRepairingStream(io.BytesIO(raw))
            got = b""
            while True:
                chunk = stream.read(size)
                if not chunk:
                    break
                got += chunk
            assert got == b'{"a": "\\\\WAKE"}', size
            assert stream.repairs == 1

    def test_bounded_array_events_stop_at_the_closing_bracket(self):
        # Everything after provider_references is broken: it must never be read.
        fh = io.BytesIO(b'{"provider_references": [{"provider_group_id": 1}], '
                        b'"in_network": [ this is not json')
        events = tic._bounded_array_events(fh, "provider_references")
        assert list(ijson.items(events, "provider_references.item")) == [{"provider_group_id": 1}]

    def test_metadata_reads_the_root_and_one_reference_only(self, tmp_path):
        path = tmp_path / "head.json"
        path.write_text(
            '{"reporting_entity_name": "Example Plan", "reporting_entity_type": "issuer", '
            '"last_updated_on": "2026-09-01", "version": "2.0.0", "x_root_note": "x", '
            '"provider_references": [{"provider_group_id": 1, "network_name": ["NET A"], '
            '"provider_groups": []}, this is never read')
        stats = ParseStats()
        meta = tic._read_metadata(path, None, stats)
        assert meta == TicFileMetadata("Example Plan", "issuer", "2026-09-01", "2.0.0", "NET A")
        assert stats.unmapped_paths == {"$.x_root_note": 1}

    def test_metadata_stops_at_in_network(self, tmp_path):
        path = tmp_path / "head.json"
        path.write_text('{"version": "1.0.0", "in_network": [ this is never read')
        assert tic._read_metadata(path, None, ParseStats()).version == "1.0.0"


# ---------------------------------------------------------------------------
# In-network rates: the fixture
# ---------------------------------------------------------------------------

class TestInNetworkFixture:

    def test_counts(self):
        records, stats = _read(IN_NETWORK)
        groups = _of(records, TicProviderGroup)
        rates = _of(records, TicRate)
        assert stats.rows_read == EXPECTED_PRICES_READ
        assert len(rates) == EXPECTED_RATES
        assert len(groups) == EXPECTED_GROUP_RECORDS
        assert len({(g.tin, g.npis) for g in groups}) == EXPECTED_DISTINCT_GROUPS

        # A reference that shares another's TIN and NPIs adds no new group.
        assert len({g.npis for g in groups if g.tin == "237428302"}) == 1
        # The empty-NPI group is never emitted.
        assert not [g for g in groups if g.tin == "000000000"]

        members = _members(records)
        sets = {frozenset().union(*(members[i] for i in r.provider_group_ids.split("|")))
                for r in rates}
        assert len(sets) == EXPECTED_GROUP_SETS

    def test_metadata_comes_first(self):
        records, _ = _read(IN_NETWORK)
        assert records[0] == TicFileMetadata(
            reporting_entity_name="Blue Shield of California",
            reporting_entity_type="Health Insurance Issuer",
            last_updated_on="2026-09-01", version="2.0.0", network_name="1056_IFP EPO THO")
        assert not _of(records[1:], TicFileMetadata)

    def test_every_group_id_on_a_rate_has_a_group(self):
        records, _ = _read(IN_NETWORK)
        members = _members(records)
        for rate in _of(records, TicRate):
            ids = rate.provider_group_ids.split("|")
            assert ids and all(i in members for i in ids)
            assert ids == sorted(ids, key=int)

    def test_poison_rows_skipped_and_recorded(self):
        records, stats = _read(IN_NETWORK)
        assert stats.rows_skipped == EXPECTED_ROWS_SKIPPED
        assert not [g for g in _of(records, TicProviderGroup)
                    if g.tin in ("111111111", "222222222", "")]
        rates = _of(records, TicRate)
        assert not [r for r in rates if not r.billing_code]
        # 77777: only the unparseable-expiration price survives, defaulted.
        poison = [r for r in rates if r.billing_code == "77777"]
        assert len(poison) == 1 and poison[0].expiration_date == "9999-12-31"

        paths = stats.unmapped_paths
        assert paths[f"{GROUPS}.npi=non_numeric"] == 1
        assert paths[f"{GROUPS}.npi=out_of_range"] == 1
        assert paths[f"{GROUPS}.tin.value=missing"] == 1
        assert paths["$.in_network[].billing_code=missing"] == 1
        assert paths[f"{PRICES}.negotiated_rate=missing"] == 1
        # Rate-too-big and percentage-too-big share the raw field's path.
        assert paths[f"{PRICES}.negotiated_rate=out_of_range"] == 2
        assert paths[f"{PRICES}.expiration_date=unparseable"] == 1

    def test_unmapped_capture(self):
        _, stats = _read(IN_NETWORK)
        for path in (
            "$.x_unknown_root_key",
            "$.in_network[].x_payer_custom_flag",
            f"{PRICES}.x_unknown_price_key",
            "$.provider_references[].x_unknown_ref_key",
            f"{GROUPS}.tin.type=npi",
            f"{PRICES}.negotiated_type=bogus_type",
            "$.in_network[].negotiated_rates[].provider_references=unknown",
            f"{PRICES}.service_code=CSTM-00",
        ):
            assert stats.unmapped_paths.get(path, 0) >= 1, path
        assert all(isinstance(n, int) for n in stats.unmapped_paths.values())
        for path in stats.unmapped_paths:
            assert "name=" not in path and "description=" not in path

    def test_percentage_and_per_diem_rows(self):
        rates = _of(_read(IN_NETWORK)[0], TicRate)
        pct = [r for r in rates if r.billing_code == "0359" and r.negotiated_type == "percentage"]
        assert len(pct) == 1
        assert pct[0].negotiated_rate is None and pct[0].negotiated_percentage == 59.60
        per_diem = [r for r in rates if r.negotiated_type == "per diem"]
        assert per_diem
        assert all(r.negotiated_rate is not None and r.negotiated_percentage is None
                   for r in per_diem)


# ---------------------------------------------------------------------------
# Licensed text: descriptor text never reaches a record
# ---------------------------------------------------------------------------

def _descriptor_strings(data):
    """Every item name/description/covered_services string and bundled-code description."""
    found = set()

    def strings(value):
        if isinstance(value, str):
            found.add(value)
        elif isinstance(value, dict):
            for v in value.values():
                strings(v)
        elif isinstance(value, list):
            for v in value:
                strings(v)

    for item in data["in_network"]:
        for key in tic.IGNORED_ITEM_KEYS:
            strings(item.get(key))
        for bc in item.get("bundled_codes") or []:
            strings(bc.get("description"))
    return found


def _everything_emitted(records, stats):
    return json.dumps([dataclasses.asdict(r) for r in records]) + json.dumps(
        [stats.unmapped_paths, stats.warnings])


class TestLicensedText:

    def test_no_descriptor_text_in_any_record(self):
        descriptors = _descriptor_strings(_fixture_dict())
        assert len(descriptors) >= 10
        records, stats = _read(IN_NETWORK)
        emitted = _everything_emitted(records, stats)
        for text in descriptors:
            assert text not in emitted, text

    def test_unknown_key_values_are_never_emitted(self, tmp_path):
        """A payer's own free-text key is reported by path and count only."""
        secret = "Sentinel free text that must not appear"
        # Root keys are read up to the first big array, as payers write them.
        data = {"x_long_description": secret, **_fixture_dict()}
        ref = data["provider_references"][0]
        ref["x_long_description"] = secret
        ref["provider_groups"][0]["x_long_description"] = secret
        ref["provider_groups"][0]["tin"]["x_long_description"] = secret
        item = data["in_network"][0]
        item["x_long_description"] = secret
        item["covered_services"] = {"service_description": secret}
        item["negotiated_rates"][0]["x_long_description"] = secret
        item["negotiated_rates"][0]["negotiated_prices"][0]["x_long_description"] = secret
        item["negotiated_rates"][0]["negotiated_prices"][0]["additional_information"] = secret
        knee = next(i for i in data["in_network"] if i["billing_code"] == "CSTM-KNEE")
        knee["bundled_codes"][0]["x_long_description"] = secret

        stats = ParseStats()
        records = list(mrfkit.iter_records(_write(tmp_path, "secret.json.gz", data), stats=stats))
        assert secret not in _everything_emitted(records, stats)
        for path in ("$", "$.provider_references[]", GROUPS, f"{GROUPS}.tin", "$.in_network[]",
                     "$.in_network[].negotiated_rates[]", PRICES,
                     "$.in_network[].bundled_codes[]"):
            assert stats.unmapped_paths[f"{path}.x_long_description"] == 1, path
        assert not [p for p in stats.unmapped_paths if "covered_services" in p]


# ---------------------------------------------------------------------------
# In-network rates: payer shapes
# ---------------------------------------------------------------------------

class TestPayerShapes:

    def test_quirky_file_reads_into_normalized_records(self, tmp_path):
        records, stats = _read(_quirk_variant(tmp_path))
        groups = _of(records, TicProviderGroup)
        assert [g.tin for g in groups if g.group_id == "90001"] == ["300318970"]
        assert not [g for g in groups if "-" in g.tin]
        assert [g.tin_type for g in groups if g.group_id == "90002"] == ["npi"]

        # Both groups priced 99999 identically: one record holding both.
        rates = [r for r in _of(records, TicRate) if r.billing_code == "99999"]
        assert len(rates) == 1
        assert rates[0].provider_group_ids == "90001|90002"
        assert rates[0].modifiers == "52|53"  # "52,53" is two modifiers
        assert rates[0].negotiated_rate == 123.45
        assert not [p for p in stats.unmapped_paths if "severity" in p]

    def test_merging_survives_a_mid_item_flush(self, tmp_path, monkeypatch):
        """Holding fewer distinct prices than one item has emits mid-item. The
        price then appears twice, one group each, and every group keeps the
        price it was given."""
        monkeypatch.setattr(tic, "MAX_HELD_RATES", 1)
        records, _ = _read(_quirk_variant(tmp_path))
        rates = [r for r in _of(records, TicRate) if r.billing_code == "99999"]
        assert sorted(r.provider_group_ids for r in rates) == ["90001", "90002"]

    def test_duplicate_negotiated_rate_entry_merges(self, tmp_path):
        data = _fixture_dict()
        knee = next(i for i in data["in_network"] if i["billing_code"] == "CSTM-KNEE")
        knee["negotiated_rates"].append(copy.deepcopy(knee["negotiated_rates"][0]))
        records, stats = _read(_write(tmp_path, "dup.json.gz", data))
        assert stats.rows_read == EXPECTED_PRICES_READ + 1
        assert len([r for r in _of(records, TicRate) if r.billing_code == "CSTM-KNEE"]) == 1

    def test_remote_provider_references_are_reported(self, tmp_path):
        data = _fixture_dict()
        data["provider_references"].append(
            {"provider_group_id": 95001, "network_name": ["Remote"],
             "location": "https://payer.example.test/provider-refs.json"})
        records, stats = _read(_write(tmp_path, "remote.json.gz", data))
        assert stats.unmapped_paths["$.provider_references[].location=unsupported"] == 1
        assert not [g for g in _of(records, TicProviderGroup) if g.group_id == "95001"]

    def test_inline_provider_groups_get_synthetic_ids(self, tmp_path):
        data = _fixture_dict()
        inline = {"npi": [1194728220], "tin": {"type": "ein", "value": "300318970"}}
        price = {"negotiated_type": "negotiated", "negotiated_rate": 10.0,
                 "expiration_date": "9999-12-31", "service_code": ["11"],
                 "billing_class": "professional", "setting": "outpatient"}
        for code in ("88888", "88889"):
            data["in_network"].append({
                "negotiation_arrangement": "ffs", "billing_code_type": "CPT",
                "billing_code_type_version": "2026", "billing_code": code,
                "negotiated_rates": [{"provider_groups": [copy.deepcopy(inline)],
                                      "negotiated_prices": [copy.deepcopy(price)]}],
            })
        records, _ = _read(_write(tmp_path, "inline.json.gz", data))
        inline_groups = [g for g in _of(records, TicProviderGroup) if g.group_id.startswith("inline")]
        assert inline_groups == [TicProviderGroup("inline-1", "ein", "300318970", "1194728220")]
        rates = [r for r in _of(records, TicRate) if r.billing_code in ("88888", "88889")]
        assert [r.provider_group_ids for r in rates] == ["inline-1", "inline-1"]
        # The group is emitted before the first rate that uses it.
        assert records.index(inline_groups[0]) < records.index(rates[0])

    def test_rates_pointing_at_unknown_references_are_not_invented(self, tmp_path):
        data = _fixture_dict()
        for item in data["in_network"]:
            for nr in item.get("negotiated_rates") or []:
                nr["provider_references"] = [99999999]
        records, stats = _read(_write(tmp_path, "nothing.json.gz", data))
        assert not _of(records, TicRate)
        assert stats.unmapped_paths[
            "$.in_network[].negotiated_rates[].provider_references=unknown"] >= 1

    def test_billing_class_both_is_kept(self, tmp_path):
        data = _fixture_dict()
        for nr in data["in_network"][0]["negotiated_rates"]:
            for price in nr["negotiated_prices"]:
                price["billing_class"] = "both"
        records, stats = _read(_write(tmp_path, "both.json.gz", data))
        assert [r for r in _of(records, TicRate) if r.billing_class == "both"]
        assert not [p for p in stats.unmapped_paths if p.endswith("billing_class=both")]

    def test_bare_backslash_in_a_name_is_repaired(self, tmp_path):
        data = _fixture_dict()
        data["provider_references"][0]["provider_groups"][0]["tin"]["business_name"] = (
            "\\WAKE SPECIALTY PHYSICIANS LLC")
        path = _write(tmp_path, "escape.json.gz",
                      text=json.dumps(data).replace("\\\\WAKE", "\\WAKE"))
        with gzip.open(path, "rb") as f:
            assert b'"\\WAKE' in f.read()
        records, stats = _read(path)
        assert stats.rows_read == EXPECTED_PRICES_READ
        assert _of(records, TicProviderGroup)[0].business_name == "\\WAKE SPECIALTY PHYSICIANS LLC"
        assert any("1 invalid JSON escape" in w for w in stats.warnings)

    def test_zip_reads_like_gz(self, tmp_path):
        path = tmp_path / "rates.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("in-network-rates.json", json.dumps(_fixture_dict()))
        assert _read(path)[0] == _read(IN_NETWORK)[0]


# ---------------------------------------------------------------------------
# Tables of contents
# ---------------------------------------------------------------------------

def _toc_doc():
    shared = "https://cms.example.com/files/2026-09-01_2001-in-network-rates_0.json.gz"
    return {
        "reporting_entity_name": "Test TPA",
        "reporting_entity_type": "third_party_administrator",
        "reporting_structure": [
            {
                "reporting_plans": [
                    {"plan_name": "PPO Group A", "plan_id_type": "EIN",
                     "plan_id": "111111111", "plan_market_type": "group"},
                    {"plan_name": "PPO Group B", "plan_id_type": "EIN",
                     "plan_id": "111111112", "plan_market_type": "group"},
                ],
                "in_network_files": [
                    {"description": "PPO in-network", "location": shared},
                    {"description": "PPO cap rates",
                     "location": "https://cms.example.com/files/2026-09-01_2001-cap-rates_0.json.gz"},
                    {"description": "PPO allowed amounts",
                     "location": "https://cms.example.com/files/"
                                 "2026-09-01_2001-allowed-amounts_0.json.gz"},
                ],
            },
            {
                "reporting_plans": [
                    {"plan_name": "EPO Individual", "plan_id_type": "EIN",
                     "plan_id": "222222222", "plan_market_type": "individual"},
                ],
                "in_network_files": [
                    {"description": "PPO in-network (shared)", "location": shared},
                    {"description": "Custom network, no date prefix",
                     "location": "https://cms.example.com/files/custom-network-rates.json.gz"},
                ],
                "allowed_amount_file": {
                    "location": "https://cms.example.com/files/allowed-2.json.gz"},
            },
        ],
    }


def _groups(tmp_path, doc, name="toc.json", stats=None):
    path = tmp_path / name
    path.write_text(json.dumps(doc))
    return group_tic_networks(iter_tic_index(path, stats=stats))


class TestIndex:

    def test_one_entry_per_plan_and_file(self):
        stats = ParseStats()
        entries = list(iter_tic_index(INDEX, stats=stats))
        # 2 plans x 2 files, 1 plan x 2 files (a third has no location), 1 file with no plans.
        assert len(entries) == 7
        assert stats.rows_read == 5 and stats.rows_skipped == 1
        assert {e.reporting_entity_name for e in entries} == {"Example Health Plan"}
        first = entries[0]
        assert first == TicIndexEntry(
            reporting_entity_name="Example Health Plan",
            reporting_entity_type="health insurance issuer",
            plan_name="PPO Group A", plan_id="111111111", plan_id_type="EIN",
            plan_market_type="group",
            in_network_location="https://payer.example.com/mrf/"
                                "2026-09-01_2001-in-network-rates_0.json.gz?sig=a1",
            in_network_description="PPO in-network",
            allowed_amount_location="https://payer.example.com/mrf/"
                                    "2026-09-01_allowed-amounts.json.gz")
        no_plan = entries[-1]
        assert no_plan.plan_name is None and no_plan.allowed_amount_location is None

    def test_groups_by_full_basename_shared_across_structures(self, tmp_path):
        stats = ParseStats()
        groups = _groups(tmp_path, _toc_doc(), stats=stats)
        assert stats.rows_skipped == 0
        key = "2001-in-network-rates_0"
        # One file referenced by both structures: file_count counts distinct
        # files, not references.
        assert groups[key]["file_count"] == 1
        assert groups[key]["location"] == (
            "https://cms.example.com/files/2026-09-01_2001-in-network-rates_0.json.gz")

    def test_signed_urls_and_dates_give_one_stable_key(self, tmp_path):
        # Signed differently per reference and dated by month: one file is one
        # network, and next month's copy keeps the key.
        base = "https://x.example.com/2026-09_182_25B0_in-network-rates_3_of_3.json.gz"
        doc = {"reporting_structure": [
            {"reporting_plans": [{"plan_market_type": "group"}],
             "in_network_files": [{"location": base + "?Signature=a"}]},
            {"reporting_plans": [{"plan_market_type": "group"}],
             "in_network_files": [{"location": base + "?Signature=b"}]},
        ]}
        groups = _groups(tmp_path, doc)
        assert list(groups) == ["182_25B0_in-network-rates_3_of_3"]
        g = groups["182_25B0_in-network-rates_3_of_3"]
        assert g["file_count"] == 1
        assert g["file_name"] == "2026-09_182_25B0_in-network-rates_3_of_3.json.gz"
        assert g["location"] == base + "?Signature=a"
        assert tic_network_key("2026-10-01_182_25B0_in-network-rates_3_of_3.json.gz") == (
            "182_25B0_in-network-rates_3_of_3")

    def test_never_groups_by_a_leading_numeric_prefix(self, tmp_path):
        # Two unrelated files sharing a leading number are two networks.
        doc = {"reporting_structure": [{"reporting_plans": [], "in_network_files": [
            {"location": "https://cms.example.com/files/2026-09-01_2001-network-a.json"},
            {"location": "https://cms.example.com/files/2026-09-01_2001-network-b.json"},
        ]}]}
        assert set(_groups(tmp_path, doc)) == {"2001-network-a", "2001-network-b"}

    def test_modal_market_type_across_structures(self, tmp_path):
        # 2 "group" plans vs 1 "individual" plan reference the shared file.
        assert _groups(tmp_path, _toc_doc())["2001-in-network-rates_0"]["market_type"] == "group"

    def test_cap_rates_and_allowed_files_are_dropped(self, tmp_path):
        groups = _groups(tmp_path, _toc_doc())
        locations = {g["location"] for g in groups.values()}
        assert not any("cap-rates" in p or "allowed" in p for p in locations)
        assert len(groups) == 2

    def test_groups_by_full_basename_without_extension(self, tmp_path):
        groups = _groups(tmp_path, _toc_doc())
        assert "custom-network-rates" in groups
        assert groups["custom-network-rates"]["file_count"] == 1
        assert groups["custom-network-rates"]["market_type"] == "individual"

    def test_missing_location_counts_as_skipped(self, tmp_path):
        stats = ParseStats()
        doc = {"reporting_structure": [
            {"reporting_plans": [], "in_network_files": [{"description": "no location"}]}]}
        assert _groups(tmp_path, doc, stats=stats) == {}
        assert stats.rows_skipped == 1

    def test_empty_structure_list(self, tmp_path):
        assert _groups(tmp_path, {"reporting_structure": []}) == {}

    def test_list_root_with_an_object_is_accepted(self, tmp_path):
        entries = list(iter_tic_index(self._write(tmp_path, [_toc_doc()])))
        assert {e.reporting_entity_name for e in entries} == {"Test TPA"}
        assert "2001-in-network-rates_0" in group_tic_networks(entries)

    def test_non_object_root_raises_value_error(self, tmp_path):
        with pytest.raises(ValueError):
            list(iter_tic_index(self._write(tmp_path, "just a string")))

    def test_large_gzipped_index_streams(self, tmp_path):
        doc = {"reporting_structure": [
            {"reporting_plans": [{"plan_market_type": "group"}],
             "in_network_files": [{"location": f"https://example.com/files/2026-09-01_network-{i}.json"}]}
            for i in range(300)
        ]}
        path = tmp_path / "toc.json.gz"
        path.write_bytes(gzip.compress(json.dumps(doc).encode()))
        assert len(group_tic_networks(iter_tic_index(path))) == 300

    @staticmethod
    def _write(tmp_path, doc):
        path = tmp_path / "toc.json"
        path.write_text(json.dumps(doc))
        return path


# ---------------------------------------------------------------------------
# Dispatch and command line
# ---------------------------------------------------------------------------

class TestDispatch:

    def test_detect_tic_file(self, tmp_path):
        assert tic.detect_tic_file(IN_NETWORK) == "in_network"
        assert tic.detect_tic_file(INDEX) == "index"
        assert tic.detect_tic_file(HOSPITAL_JSON) is None
        wrapped = tmp_path / "wrapped.json"
        wrapped.write_text(json.dumps([_toc_doc()]))
        assert tic.detect_tic_file(wrapped) == "index"

    def test_iter_records_dispatches(self):
        assert _of(mrfkit.iter_records(IN_NETWORK), TicRate)
        assert _of(mrfkit.iter_records(INDEX), TicIndexEntry)
        assert _of(mrfkit.iter_records(HOSPITAL_JSON), mrfkit.ModifierInfo)

    def test_hospital_options_are_ignored_for_tic(self):
        stats = ParseStats()
        records = list(mrfkit.iter_records(IN_NETWORK, stats=stats, extra_synonyms={"a": "b"}))
        assert len(_of(records, TicRate)) == EXPECTED_RATES
        assert stats.rows_read == EXPECTED_PRICES_READ

    def test_cli_writes_tic_tables(self, tmp_path, capsys):
        assert main([str(IN_NETWORK), "-o", str(tmp_path)]) == 0
        lines = (tmp_path / "tic_rates.csv").read_text().splitlines()
        assert len(lines) == EXPECTED_RATES + 1
        assert (tmp_path / "tic_provider_groups.csv").exists()
        assert (tmp_path / "tic_file_metadata.csv").exists()
        err = capsys.readouterr().err
        assert f"{EXPECTED_RATES} tic_rates" in err
        assert "unmapped JSON paths (-v lists them)" in err

    def test_cli_lists_unmapped_paths_when_verbose(self, tmp_path, capsys):
        assert main([str(IN_NETWORK), "-o", str(tmp_path), "-v"]) == 0
        assert "$.x_unknown_root_key: 1" in capsys.readouterr().err

    def test_parquet(self, tmp_path):
        pytest.importorskip("pyarrow")
        assert main([str(IN_NETWORK), str(INDEX), "-o", str(tmp_path), "-f", "parquet"]) == 0
        assert (tmp_path / "bsca-1056_in-network-rates" / "tic_rates.parquet").exists()
        assert (tmp_path / "example_index" / "tic_index.parquet").exists()
