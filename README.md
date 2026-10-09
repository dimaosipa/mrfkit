# mrfkit

mrfkit reads US hospital price transparency files and turns them into clean, normalized tables.

Every US hospital must publish its prices in a machine-readable file (MRF). The files are huge, and no two look alike. Columns get renamed, payers sit in rows or in columns, codes come glued to modifiers, and plenty of JSON files are not valid JSON. mrfkit handles that mess, so you get the same tables from any file.

It was extracted from a production system that parsed files from about 3,000 hospitals. Its parsing tests, and the fixes behind them, came along.

## Install

```bash
pip install mrfkit
```

For Parquet output:

```bash
pip install "mrfkit[parquet]"
```

Python 3.11 or newer.

## Command line

```bash
mrfkit hospital_standardcharges.csv -o out/
```

This writes one CSV per table into `out/`. Use `-f parquet` for Parquet. Give several files and each one gets its own subdirectory. Plain, gzip and zip files all work, and the format is detected from the content.

## Python

```python
import mrfkit

stats = mrfkit.ParseStats()
for record in mrfkit.iter_records("hospital_standardcharges.json", stats=stats):
    if isinstance(record, mrfkit.PayerRate):
        print(record.code, record.payer_name, record.plan_name, record.negotiated_rate)

print(stats.rows_read, stats.rejected_codes, stats.warnings)
```

Records stream one at a time, so memory stays flat for multi-gigabyte files. To write them to disk:

```python
with mrfkit.open_sink("out/", "parquet") as sink:
    for record in mrfkit.iter_records("hospital_standardcharges.csv"):
        sink.write(record)
```

## Output

| Table | One row per | Main columns |
|---|---|---|
| `charge_items` | billable item (code, type, billing class, setting, modifiers) | `code`, `code_type`, `description`, `billing_class`, `setting`, `modifiers`, drug units |
| `standard_charges` | item price row | `gross_charge`, `discounted_cash_price`, `min_negotiated_rate`, `max_negotiated_rate` |
| `payer_rates` | negotiated rate for one payer and plan | `payer_name`, `raw_payer_name`, `plan_name`, `plan_category`, `negotiated_rate`, `negotiated_percentage`, `methodology_type`, `estimated_amount`, `median_amount` |
| `unmapped_cells` | value in a column mrfkit could not map | `source_column`, `value` |
| `file_metadata` | file | hospital name, locations, license, NPI, last updated, version, attestation |
| `header_mappings` | source column | what the column was read as |
| `modifiers` | modifier definition (CMS 3.0 JSON) | `code`, `description`, payer-specific notes |

Every data row carries its item key (`code`, `code_type`, `billing_class`, `setting`, `modifiers`), so the tables join without a database.

## What it handles

- CMS templates, versions 2 and 3, as CSV (tall and wide) and JSON
- Older and vendor layouts: one column per payer, payer and setting packed into column names, separate inpatient and outpatient price columns
- More than 130 header spellings, plus your own (`extra_synonyms`)
- Metadata rows with the data header glued on, `Key: Value` preambles, several tables stacked in one CSV
- Pipe- and tab-delimited files, stray quotes that would fuse rows
- Deflate64 zips, gzip, UTF-16, Windows-1252 bytes, byte-order marks
- Broken JSON: double and trailing commas, control characters
- Codes: CPT, HCPCS, NDC, revenue codes, MS-DRG, APR-DRG; modifiers baked into codes (`73721TC`); corrupt codes are dropped and counted
- Payer and plan names: about 1,200 known payer spellings, normalized to a canonical payer, a plan category and a network
- Self-pay rows: read as the cash price, not as a payer

## What it does not do

- Download files. Point it at files you already have.
- Fix hospital errors. mrfkit reports what the file says.
- Insurer Transparency in Coverage files. They are planned for v0.2.

## Disclaimer

The output is for information only. It is not medical, legal or financial advice. Hospitals publish mistakes, and a price in a file is not a quote.

CPT codes are numbers published by hospitals under the federal price transparency rule. mrfkit ships no CPT descriptions.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
