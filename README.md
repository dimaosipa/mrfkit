# mrfkit

mrfkit reads US price transparency files and turns them into clean, normalized rows.

Hospitals and insurers must publish their prices as machine-readable files (MRFs). The files are huge, and the formats vary a lot. mrfkit handles the mess so you get the same rows from any file.

## Status

Pre-alpha. The parser is being moved here from a production system that ingested thousands of hospital files. Nothing is usable yet.

Planned for v0.1:

- Hospital files in the CMS CSV (tall and wide) and JSON formats, template versions 2 and 3
- Real-world quirks: stacked CSV sections, glued header rows, Deflate64 zips, odd encodings
- Code cleanup for CPT, HCPCS, NDC and APR-DRG
- Output as CSV or Parquet, with no database needed

Planned for v0.2: insurer Transparency in Coverage files (in-network rates and table of contents).

## Install

```bash
pip install mrfkit
```

Add Parquet output with `pip install "mrfkit[parquet]"`.

## Disclaimer

mrfkit reports what the files say. Hospitals and insurers publish errors, and mrfkit does not fix them. The output is for information only. It is not medical, legal or financial advice.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
