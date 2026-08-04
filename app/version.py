"""Project version utilities.

Version scheme:
- 1.00 initial
- 1.01 bug fix
- 1.10 feature
- 2.00 major
"""

VERSION = "2.00"


def parse_version(version: str) -> tuple[int, int, int]:
    major_s, rest = version.split(".")
    if len(rest) != 2:
        raise ValueError("Version must be M.mm format")
    return int(major_s), int(rest[0]), int(rest[1])


def bump_bugfix(version: str) -> str:
    """1.98 -> 1.99, and 1.99 -> 2.00 (never 1.100).

    The scheme is strictly M.mm: the minor part is TWO digits. Overflowing it
    without carrying into the major produced "1.100", which parse_version
    rejects, which makes is_newer() return False forever, which would have
    silently frozen OTA for every deployed unit.
    """
    major, feature, bugfix = parse_version(version)
    bugfix += 1
    if bugfix > 9:
        bugfix = 0
        feature += 1
    if feature > 9:
        return bump_major(version)
    return f"{major}.{feature}{bugfix}"


def bump_feature(version: str) -> str:
    """1.90 -> 1.a0 is impossible, so 1.9x features carry to the next major."""
    major, feature, _ = parse_version(version)
    feature += 1
    if feature > 9:
        return bump_major(version)
    return f"{major}.{feature}0"


def bump_major(version: str) -> str:
    major, _, _ = parse_version(version)
    return f"{major + 1}.00"
