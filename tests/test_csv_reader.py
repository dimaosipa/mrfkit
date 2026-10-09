"""Tests for mrfkit's CSV reader API (iter_csv) beyond the ported staging tests."""

import gzip
import io

from mrfkit.csv_reader import iter_csv
from mrfkit.records import (
    ChargeItem, FileMetadata, HeaderMapping, PayerRate, StandardCharge, UnmappedCell,
)
from mrfkit.reference import ParseStats
from mrfkit.tabular import UNMAPPED_CELLS_PER_COLUMN_CAP

CMS_WIDE = (
    "hospital_name,last_updated_on,version,license_number|OR\n"
    "Test Hospital,2026-01-01,3.0.0,14-1472\n"
    "description,code|1,code|1|type,setting,standard_charge|gross,"
    "standard_charge|Aetna|PPO|negotiated_dollar,mystery column\n"
    "Office Visit Level 3,99213,CPT,outpatient,150.00,90.00,abc\n"
    "Office Visit Level 3,99213,CPT,outpatient,150.00,90.00,abc\n"
)


def _of(records, cls):
    return [r for r in records if isinstance(r, cls)]


def test_record_order_and_types():
    records = list(iter_csv(io.StringIO(CMS_WIDE)))
    assert isinstance(records[0], FileMetadata)
    n_headers = 7
    assert all(isinstance(r, HeaderMapping) for r in records[1:1 + n_headers])
    meta = records[0]
    assert (meta.hospital_name, meta.version, meta.license_state) == ("Test Hospital", "3.0.0", "OR")


def test_charge_items_are_deduplicated_but_rates_are_not():
    records = list(iter_csv(io.StringIO(CMS_WIDE)))
    assert len(_of(records, ChargeItem)) == 1
    assert len(_of(records, StandardCharge)) == 2
    assert len(_of(records, PayerRate)) == 2


def test_header_mappings_name_the_layout():
    mapped = {r.source_header: r.mapped_to
              for r in iter_csv(io.StringIO(CMS_WIDE)) if isinstance(r, HeaderMapping)}
    assert mapped["standard_charge|Aetna|PPO|negotiated_dollar"] == "wide_payer:negotiated_rate"
    assert mapped["code|1"] == "code"
    assert mapped["mystery column"] is None


def test_unmapped_cells_keep_the_value():
    cells = _of(iter_csv(io.StringIO(CMS_WIDE)), UnmappedCell)
    assert {(c.source_column, c.value, c.code) for c in cells} == {("mystery column", "abc", "99213")}


def test_wide_rate_uses_the_inferred_code_type():
    # 'A19325' with no type is inferred as CPT 19325. The production parser
    # keyed the wide payer rate with the raw (empty) type, so the rate did
    # not join back to its item.
    csv_text = (
        "description,code|1,code|1|type,setting,standard_charge|gross,"
        "standard_charge|Aetna|PPO|negotiated_dollar\n"
        "Breast lesion excision,A19325,,outpatient,1000.00,600.00\n"
    )
    records = list(iter_csv(io.StringIO(csv_text)))
    (item,) = _of(records, ChargeItem)
    (rate,) = _of(records, PayerRate)
    assert (item.code, item.code_type) == ("19325", "CPT")
    assert (rate.code, rate.code_type) == (item.code, item.code_type)


def test_unmapped_cells_capped_per_column():
    rows = "".join(f"Item {i},{10000 + i},CPT,{i}.00,note {i}\n"
                   for i in range(UNMAPPED_CELLS_PER_COLUMN_CAP + 50))
    csv_text = "description,code,code_type,gross_charge,free text\n" + rows
    cells = _of(iter_csv(io.StringIO(csv_text)), UnmappedCell)
    assert len(cells) == UNMAPPED_CELLS_PER_COLUMN_CAP


def test_stats_collect_rows_and_rejections():
    csv_text = (
        "description,code,code_type,gross_charge\n"
        "Good,99213,CPT,10.00\n"
        "Bad type,99214,12345,10.00\n"  # numeric code_type is rejected
    )
    stats = ParseStats()
    records = list(iter_csv(io.StringIO(csv_text), stats=stats))
    assert stats.rows_read == 2
    assert stats.rejected_codes == 1
    assert len(_of(records, ChargeItem)) == 1


def test_reads_gzip_path(tmp_path):
    path = tmp_path / "charges.csv.gz"
    path.write_bytes(gzip.compress(CMS_WIDE.encode()))
    assert len(_of(iter_csv(path), PayerRate)) == 2


def test_header_overrides_and_extra_synonyms():
    csv_text = "description,code,Type,Charge Amt\nVisit,99213,CPT,10.00\n"
    records = list(iter_csv(io.StringIO(csv_text), header_overrides={"Type": "code_type"},
                            extra_synonyms={"charge_amt": "gross_charge"}))
    (std,) = _of(records, StandardCharge)
    assert (std.code_type, std.gross_charge) == ("CPT", 10.0)
