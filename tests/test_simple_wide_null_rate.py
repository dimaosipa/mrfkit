#!/usr/bin/env python3
"""Simple wide-format payer detection must treat ``N/A`` as a missing rate.

Regression test for the unmapped_cell doubling.

A single wide-format file (Texas Institute for Surgery, ingest run 11814)
doubled ``unmapped_cell`` to 2.2M rows.  Root cause: the simple wide-format
payer auto-detector (`_column_looks_like_payer_rates`, formerly inline in the
CSV ingest path) sampled each unmapped column's first rows and required
>= 50% of *non-empty* values to look like dollar amounts.  Textual ``N/A``
placeholders were counted as non-empty but not dollar-like, so payer columns
that were ~67% ``N/A`` / ~33% real dollars scored ~0.33 and fell below the
threshold.  Every such column stayed unmapped, so every data row's cell for it
was written to ``unmapped_cell`` (~34 payer columns x ~43K rows = ~1.5M rows) ,
and, worse, the ~138K real negotiated rates in those columns were never
ingested as ``charge_payer_rate``.

The fix treats ``N/A`` (and friends) as *missing*, the column is judged only
on the dollar-likeness of its meaningful values, and additionally drops
null-rate placeholders at the ``stage_unmapped`` choke point so no undetected
column can ever flood ``unmapped_cell`` with ``N/A`` again.
"""

import pytest

import mrfkit.values as fi
from mrfkit.records import UnmappedCell
from mrfkit.tabular import TableLayout


# --- Texas Institute for Surgery, run 11814: measured value distribution ----
# 289,962 N/A ; 138,645 dollar-like ; 223 zero across the sampled payer columns
# -> a real dollar-likeness of ~0.323 among non-empty cells.
def _texas_payer_column(n_na=67, n_dollar=32, n_zero=1):
    return ['N/A'] * n_na + ['10045.74'] * n_dollar + ['0'] * n_zero


class TestNullRateToken:
    @pytest.mark.parametrize('value', [
        'N/A', 'n/a', ' N/A ', 'NA', '#N/A', 'null', 'NULL', 'None',
        'nil', '-', '--', '\u2014',
    ])
    def test_recognized(self, value):
        assert fi._is_null_rate_token(value)

    @pytest.mark.parametrize('value', [
        '0', '0.00', '10045.74', '$1,234.50', 'Aetna', 'Packaged', 'CDM',
    ])
    def test_not_a_null_token(self, value):
        assert not fi._is_null_rate_token(value)


class TestLooksLikeDollarContractUnchanged:
    """The dollar-value predicate itself is untouched: N/A is still NOT a
    dollar value.  The detector handles N/A by excluding it, not by pretending
    it is a dollar (which would let all-N/A text columns masquerade as payers).
    """

    @pytest.mark.parametrize('value', ['0', '0.00', '123.45', '$10,002', '$-', 'Packaged'])
    def test_dollar_like(self, value):
        assert fi._looks_like_dollar_value(value)

    @pytest.mark.parametrize('value', ['N/A', 'CDM', 'IP', 'Aetna', ''])
    def test_not_dollar_like(self, value):
        assert not fi._looks_like_dollar_value(value)


class TestColumnLooksLikePayerRates:
    def test_na_heavy_payer_column_is_detected(self):
        """The exact failure: 67% N/A + 33% dollars must be seen as a payer."""
        assert fi._column_looks_like_payer_rates(_texas_payer_column())

    def test_old_behavior_would_have_missed_it(self):
        """Document the regression: counting N/A in the denominator yields a
        sub-threshold ratio, which is what dropped the rates and bloated the
        table before the fix."""
        col = _texas_payer_column()
        old_ratio = sum(fi._looks_like_dollar_value(v) for v in col) / len(col)
        assert old_ratio < fi._SIMPLE_WIDE_THRESHOLD

    def test_clean_payer_column_still_detected(self):
        assert fi._column_looks_like_payer_rates(['1234.50', '0', '$99.00'] * 10)

    @pytest.mark.parametrize('values', [
        ['CDM', 'DRG'] * 25,          # Line Type
        ['IP', 'OP'] * 25,            # Patient Type
        ['7/10/2023'] * 50,           # As of Date
        ['Charge Description ' + str(i) for i in range(50)],
    ])
    def test_metadata_columns_not_detected(self, values):
        assert not fi._column_looks_like_payer_rates(values)

    def test_all_null_column_not_detected(self):
        """No meaningful values -> undetectable (and stages no unmapped cells)."""
        assert not fi._column_looks_like_payer_rates(['N/A'] * 50)
        assert not fi._column_looks_like_payer_rates([None, '', '  '] * 10)

    def test_mostly_na_with_a_few_dollars_detected(self):
        assert fi._column_looks_like_payer_rates(['N/A'] * 49 + ['500.00'])


class TestUnmappedCellsSkipNullRates:
    """Defense in depth: a null-rate placeholder never becomes an unmapped cell."""

    def _cells(self, value):
        layout = TableLayout(['code', 'code_type', 'description', 'Some Unrecognized Payer'])
        row = {'code': '99213', 'code_type': 'CPT', 'description': 'desc',
               'Some Unrecognized Payer': value}
        return [r for r in layout.row_to_records(row) if isinstance(r, UnmappedCell)]

    @pytest.mark.parametrize('value', ['N/A', 'n/a', 'null', '-', '  N/A  '])
    def test_null_rate_value_not_kept(self, value):
        assert self._cells(value) == []

    @pytest.mark.parametrize('value', ['12345', 'Real text value', '0'])
    def test_real_value_is_kept(self, value):
        cells = self._cells(value)
        assert len(cells) == 1
        assert cells[0].value == value


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
