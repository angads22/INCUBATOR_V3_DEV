"""Project version utilities.

Version scheme:
- 1.00 initial
- 1.01 bug fix
- 1.10 feature
- 2.00 major
"""

VERSION = "1.50"


def parse_version(version: str) -> tuple[int, int, int]:
    major_s, rest = version.split(".")
    if len(rest) != 2:
        raise ValueError("Version must be M.mm format")
    return int(major_s), int(rest[0]), int(rest[1])


def bump_bugfix(version: str) -> str:
    major, feature, bugfix = parse_version(version)
    bugfix += 1
    if bugfix > 9:
        bugfix = 0
        feature += 1
    return f"{major}.{feature}{bugfix}"


def bump_feature(version: str) -> str:
    major, feature, _ = parse_version(version)
    feature += 1
    return f"{major}.{feature}0"


def bump_major(version: str) -> str:
    major, _, _ = parse_version(version)
    return f"{major + 1}.00"
