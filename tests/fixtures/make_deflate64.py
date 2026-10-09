"""Rebuild deflate64.zip. Needs 7-Zip (`7zz`): the stdlib cannot write Deflate64.

    python tests/fixtures/make_deflate64.py
"""

import pathlib
import subprocess
import tempfile

HERE = pathlib.Path(__file__).parent


def charges_csv() -> bytes:
    """Deterministic CSV big enough to span several inflate chunks."""
    rows = ["description,code,payer_name,standard_charge|negotiated_dollar"]
    rows += [f"Item {i},{10000 + i * 7 % 90000},Payer {i % 7},{i * 1.25:.2f}" for i in range(20000)]
    return ("\n".join(rows) + "\n").encode()


if __name__ == "__main__":
    out = HERE / "deflate64.zip"
    out.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        # A .txt inner name forces the content sniff through the Deflate64 path too.
        (pathlib.Path(tmp) / "charges.txt").write_bytes(charges_csv())
        subprocess.run(["7zz", "a", "-tzip", "-mm=Deflate64", str(out), "charges.txt"],
                       cwd=tmp, check=True, capture_output=True)
    print(out, out.stat().st_size, "bytes")
