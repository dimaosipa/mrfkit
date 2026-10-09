"""Tests for mrfkit's JSON reader API (iter_json, records_from_items)."""

import gzip
import json
import socket

import pytest

from mrfkit.files import detect_file_format
from mrfkit.json_reader import iter_json, records_from_items
from mrfkit.records import (
    ChargeItem, FileMetadata, HeaderMapping, ModifierInfo, PayerRate, StandardCharge,
)
from mrfkit.reference import ParseStats

CMS_V3 = {
    "hospital_name": "Test Hospital",
    "last_updated_on": "2026-01-01",
    "version": "3.0.0",
    "location_name": ["Main Campus", "North Clinic"],
    "hospital_address": ["1 Main St, Springfield, OR 97477"],
    "license_information": {"license_number": "14-1472", "state": "Oregon"},
    "type_2_npi": ["1234567893"],
    "attestation": {"attestation": "I attest", "attester_name": "Jane Doe",
                    "confirm_attestation": True},
    "standard_charge_information": [
        {
            "description": "Office Visit Level 3",
            "code_information": [{"code": "99213", "type": "CPT"}, {"code": "0510", "type": "RC"}],
            "standard_charges": [{
                "setting": "outpatient",
                "billing_class": "both",
                "gross_charge": 200,
                "discounted_cash": 150,
                "minimum": 80,
                "maximum": 120,
                "modifier_code": ["25"],
                "payers_information": [
                    {"payer_name": "Aetna", "plan_name": "PPO", "standard_charge_dollar": 100,
                     "methodology": "fee schedule", "median_amount": 98, "count": "1 through 10"},
                    {"payer_name": "Self Pay", "plan_name": "Cash", "standard_charge_dollar": 140},
                ],
            }],
        },
    ],
    "modifier_information": [
        {"code": "25", "description": "Separate E/M service", "setting": "outpatient",
         "modifier_payer_information": [
             {"payer_name": "Aetna", "plan_name": "PPO", "description": "Paid at 100%"}]},
    ],
}


def _of(records, cls):
    return [r for r in records if isinstance(r, cls)]


def _write(tmp_path, doc, name="mrf.json", raw=None):
    path = tmp_path / name
    path.write_bytes(raw if raw is not None else json.dumps(doc).encode())
    return path


def test_cms_v3_end_to_end(tmp_path):
    records = list(iter_json(_write(tmp_path, CMS_V3)))
    (meta,) = _of(records, FileMetadata)
    assert meta.hospital_location == "Main Campus|North Clinic"
    assert (meta.license_number, meta.license_state) == ("14-1472", "OR")
    assert meta.type_2_npi == "1234567893"
    assert (meta.attester_name, meta.confirm_attestation) == ("Jane Doe", True)

    mods = _of(records, ModifierInfo)
    assert [(m.code, m.payer_name) for m in mods] == [("25", None), ("25", "Aetna")]

    assert any(isinstance(r, HeaderMapping) for r in records)
    items = _of(records, ChargeItem)
    # billing_class "both" becomes a facility item and a professional item.
    assert sorted(i.billing_class for i in items) == ["facility", "professional"]
    assert {(i.code, i.code_type, i.modifiers) for i in items} == {("99213", "CPT", "25")}

    std = _of(records, StandardCharge)
    # Self Pay is the cash price when it is lower than discounted_cash.
    assert {s.discounted_cash_price for s in std} == {140.0}
    rates = _of(records, PayerRate)
    assert {(r.payer_name, r.negotiated_rate, r.claim_count) for r in rates} == {("Aetna", 100.0, 10)}


def test_gzip_detected(tmp_path):
    path = _write(tmp_path, None, "mrf.json.gz", gzip.compress(json.dumps(CMS_V3).encode()))
    assert detect_file_format(path) == ("json", "gz")
    assert len(_of(iter_json(path), PayerRate)) == 2


def test_flat_json_tall_payer_rates_are_kept(tmp_path):
    # The original parser mapped payer columns in flat JSON but never wrote
    # the rates. Flat items now read exactly like CSV rows.
    doc = [
        {"description": "MRI Brain", "code": "70551", "code_type": "CPT", "gross_charge": "900",
         "payer_name": "Cigna", "plan_name": "HMO", "negotiated_rate": "450"},
        {"description": "MRI Brain", "code": "70551", "code_type": "CPT", "gross_charge": "900",
         "payer_name": "Aetna", "plan_name": "PPO", "negotiated_rate": "500"},
    ]
    records = list(iter_json(_write(tmp_path, doc)))
    assert len(_of(records, ChargeItem)) == 1
    assert {(r.payer_name, r.negotiated_rate) for r in _of(records, PayerRate)} == {
        ("Cigna", 450.0), ("Aetna", 500.0)}


def test_repair_retry_skips_items_already_read(tmp_path):
    items = [{"description": f"Item {i}", "code": str(10000 + i), "code_type": "CPT",
              "gross_charge": "10"} for i in range(5)]
    body = json.dumps({"data": items})
    damaged = body.replace('}, {"description": "Item 3"', '},, {"description": "Item 3"')
    assert damaged != body
    stats = ParseStats()
    records = list(iter_json(_write(tmp_path, None, raw=damaged.encode()), stats=stats))
    assert [i.code for i in _of(records, ChargeItem)] == [str(10000 + i) for i in range(5)]
    assert any("retrying with repair" in w for w in stats.warnings)


def test_unknown_array_name_found(tmp_path):
    doc = {"hospital_name": "X", "chargemaster_rows": [
        {"description": "Visit", "code": "99213", "code_type": "CPT", "gross_charge": "10"}]}
    assert [i.code for i in _of(iter_json(_write(tmp_path, doc)), ChargeItem)] == ["99213"]


def test_html_page_is_rejected(tmp_path):
    path = _write(tmp_path, None, "download.aspx.json", b"<!DOCTYPE html><html>Not found</html>")
    with pytest.raises(ValueError, match="HTML"):
        list(iter_json(path))


def test_no_network(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("mrfkit must not open network connections")
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    assert len(list(iter_json(_write(tmp_path, CMS_V3)))) > 0


def test_records_from_items_skips_non_dicts():
    items = ["junk", 3, {"description": "Visit", "code": "99213", "code_type": "CPT",
                         "gross_charge": "10"}]
    assert len(_of(records_from_items(items), ChargeItem)) == 1
