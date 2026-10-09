import pytest

from mrfkit.values import _safe_count


@pytest.mark.parametrize("raw, expected", [
    ("12", 12),
    ("12.0", 12),
    ("-3", -3),
    ("1 through 10", 10),
    ("1 to 10", 10),
    # CMS privacy ranges written with a dash used to raise ValueError,
    # because "1-10" passes the digit check before the range parse.
    ("1-10", 10),
    ("4-1461843", 1461843),
    ("n/a", None),
    ("", None),
    (None, None),
])
def test_safe_count(raw, expected):
    assert _safe_count(raw) == expected
