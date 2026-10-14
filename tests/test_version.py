"""Version bumping: every bump must round-trip through parse_version."""

import pytest

from app.version import bump_bugfix, bump_feature, bump_major, parse_version


def test_parse_version():
    assert parse_version("1.99") == (1, 9, 9)
    assert parse_version("2.00") == (2, 0, 0)
    with pytest.raises(ValueError):
        parse_version("1.100")


def test_bugfix_bump_normal():
    assert bump_bugfix("1.40") == "1.41"
    assert bump_bugfix("1.49") == "1.50"


def test_feature_bump_normal():
    assert bump_feature("1.40") == "1.50"
    assert bump_feature("1.45") == "1.50"


def test_bugfix_bump_at_1_99_rolls_into_next_major():
    # Regression: 1.99 used to become "1.100", which parse_version rejects
    # and which OTA then compares as OLDER than 1.99 (tag regex grabs "1.10").
    assert bump_bugfix("1.99") == "2.00"
    assert parse_version(bump_bugfix("1.99")) == (2, 0, 0)


def test_feature_bump_at_1_9x_rolls_into_next_major():
    assert bump_feature("1.90") == "2.00"
    assert bump_feature("1.99") == "2.00"
    assert bump_feature("3.95") == "4.00"


def test_major_bump():
    assert bump_major("1.99") == "2.00"
    assert bump_major("2.37") == "3.00"


@pytest.mark.parametrize("start", ["0.00", "1.09", "1.89", "1.98", "1.99", "9.99"])
def test_every_bump_round_trips(start):
    for fn in (bump_bugfix, bump_feature, bump_major):
        out = fn(start)
        assert parse_version(out) > parse_version(start), (fn.__name__, start, out)
