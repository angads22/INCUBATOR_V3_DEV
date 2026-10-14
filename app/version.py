"""Project version utilities.

Version scheme (``M.mm``: major, feature digit, bugfix digit):
- 1.00 initial
- 1.01 bug fix
- 1.10 feature
- 2.00 major

The minor part is exactly two digits, so a bump that would overflow it
(1.99 -> bugfix, 1.9x -> feature) rolls over into the next major (2.00)
instead of producing a string ``parse_version`` rejects.
"""

VERSION = "1.99"


def parse_version(version: str) -> tuple[int, int, int]:
    major_s, rest = version.split(".")
    if len(rest) != 2:
        raise ValueError("Version must be M.mm format")
    return int(major_s), int(rest[0]), int(rest[1])


def _format(major: int, feature: int, bugfix: int) -> str:
    return f"{major}.{feature}{bugfix}"


def bump_bugfix(version: str) -> str:
    major, feature, bugfix = parse_version(version)
    bugfix += 1
    if bugfix > 9:
        bugfix = 0
        feature += 1
    if feature > 9:
        # 1.99 -> 2.00: the minor field cannot hold a tenth feature digit.
        return bump_major(_format(major, 0, 0))
    return _format(major, feature, bugfix)


def bump_feature(version: str) -> str:
    major, feature, _ = parse_version(version)
    feature += 1
    if feature > 9:
        return bump_major(_format(major, 0, 0))
    return _format(major, feature, 0)


def bump_major(version: str) -> str:
    major, _, _ = parse_version(version)
    return _format(major + 1, 0, 0)
