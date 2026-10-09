import csv
import dataclasses

import pytest

from mrfkit.records import ChargeItem, PayerRate, StandardCharge, UnmappedCell
from mrfkit.sinks import CsvSink, ParquetSink, open_sink

RECORDS = [
    ChargeItem("99213", "CPT", "Office visit", "professional", "outpatient"),
    StandardCharge("99213", "CPT", "Office visit", gross_charge=250.0, discounted_cash_price=None),
    PayerRate("99213", "CPT", "Office visit", payer_name="Aetna", raw_payer_name="AETNA INC",
              plan_name="PPO", negotiated_rate=112.5, claim_count=11),
    PayerRate("99214", "CPT", None, payer_name="Cigna", negotiated_percentage=60.0),
    UnmappedCell("99213", "CPT", "Office visit", source_column="weird col", value="x"),
]


def _by_table(records):
    out = {}
    for r in records:
        out.setdefault(r.TABLE, []).append(r)
    return out


def test_csv_round_trip(tmp_path):
    with CsvSink(tmp_path) as sink:
        for r in RECORDS:
            sink.write(r)
    for table, records in _by_table(RECORDS).items():
        with open(tmp_path / f"{table}.csv", newline="") as fh:
            rows = list(csv.DictReader(fh))
        expected = [{k: "" if v is None else str(v) for k, v in dataclasses.asdict(r).items()}
                    for r in records]
        assert rows == expected


def test_csv_only_writes_tables_it_saw(tmp_path):
    with CsvSink(tmp_path) as sink:
        sink.write(RECORDS[0])
    assert [p.name for p in tmp_path.iterdir()] == ["charge_items.csv"]


def test_parquet_round_trip_across_row_groups(tmp_path):
    pq = pytest.importorskip("pyarrow.parquet")
    rates = [PayerRate(str(i), "CPT", None, negotiated_rate=float(i), claim_count=i) for i in range(7)]
    with ParquetSink(tmp_path, batch_size=3) as sink:
        for r in RECORDS + rates:
            sink.write(r)
    for table, records in _by_table(RECORDS + rates).items():
        pf = pq.ParquetFile(tmp_path / f"{table}.parquet")
        assert pf.read().to_pylist() == [dataclasses.asdict(r) for r in records]
    assert pq.ParquetFile(tmp_path / "payer_rates.parquet").num_row_groups == 3  # 9 rows / 3


def test_parquet_column_types(tmp_path):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    with ParquetSink(tmp_path) as sink:
        sink.write(RECORDS[2])
    schema = pq.read_schema(tmp_path / "payer_rates.parquet")
    assert schema.field("negotiated_rate").type == pa.float64()
    assert schema.field("claim_count").type == pa.int64()
    assert schema.field("payer_name").type == pa.string()


def test_open_sink_rejects_unknown_format(tmp_path):
    with pytest.raises(ValueError, match="parquet"):
        open_sink(tmp_path, "xlsx")
