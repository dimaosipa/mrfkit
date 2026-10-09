import csv
import json
from pathlib import Path

import pytest

import mrfkit
from mrfkit.cli import main

FIXTURES = Path(__file__).parent / "fixtures" / "mrf"
CSV_MRF = FIXTURES / "test-hospital_standardcharges.csv"
JSON_MRF = FIXTURES / "cms-v3_standardcharges.json"


def _rows(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def test_single_file_to_csv(tmp_path, capsys):
    assert main([str(CSV_MRF), "-o", str(tmp_path)]) == 0
    assert len(_rows(tmp_path / "payer_rates.csv")) == 30
    assert "30 payer_rates" in capsys.readouterr().err


def test_several_files_get_subdirectories(tmp_path):
    assert main([str(CSV_MRF), str(JSON_MRF), "-o", str(tmp_path)]) == 0
    assert (tmp_path / "test-hospital_standardcharges" / "charge_items.csv").exists()
    assert (tmp_path / "cms-v3_standardcharges" / "modifiers.csv").exists()


def test_parquet(tmp_path):
    pytest.importorskip("pyarrow")
    assert main([str(JSON_MRF), "-o", str(tmp_path), "-f", "parquet"]) == 0
    assert (tmp_path / "payer_rates.parquet").exists()


def test_synonyms_file(tmp_path):
    mrf = tmp_path / "custom.csv"
    mrf.write_text("description,code,code_type,Charge Amt\nVisit,99213,CPT,10.00\n")
    synonyms = tmp_path / "syn.json"
    synonyms.write_text(json.dumps({"charge_amt": "gross_charge"}))
    assert main([str(mrf), "-o", str(tmp_path / "out"), "--synonyms", str(synonyms)]) == 0
    assert _rows(tmp_path / "out" / "standard_charges.csv")[0]["gross_charge"] == "10.0"


def test_bad_file_fails_but_others_still_run(tmp_path, capsys):
    html = tmp_path / "download.aspx.csv"
    html.write_bytes(b"<!DOCTYPE html><html>Not found</html>")
    assert main([str(html), str(CSV_MRF), "-o", str(tmp_path / "out")]) == 1
    err = capsys.readouterr().err
    assert "HTML" in err
    assert (tmp_path / "out" / "test-hospital_standardcharges" / "payer_rates.csv").exists()


def test_iter_records_dispatches_on_format():
    assert any(isinstance(r, mrfkit.ModifierInfo) for r in mrfkit.iter_records(JSON_MRF))
    assert any(isinstance(r, mrfkit.PayerRate) for r in mrfkit.iter_records(CSV_MRF))


def test_parquet_without_pyarrow_is_a_clean_error(tmp_path, monkeypatch, capsys):
    import builtins
    real_import = builtins.__import__

    def no_pyarrow(name, *args, **kwargs):
        if name == "pyarrow":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pyarrow)
    with pytest.raises(SystemExit) as exc:
        main([str(JSON_MRF), "-o", str(tmp_path), "-f", "parquet"])
    assert exc.value.code == 2
    assert "mrfkit[parquet]" in capsys.readouterr().err
