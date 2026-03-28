#!/usr/bin/env python3
from pathlib import Path
import re
import sys

from app.version import VERSION, bump_bugfix, bump_feature, bump_major


if len(sys.argv) != 2 or sys.argv[1] not in {"bugfix", "feature", "major"}:
    print("Usage: scripts/bump_version.py [bugfix|feature|major]")
    raise SystemExit(1)

mode = sys.argv[1]
if mode == "bugfix":
    new_version = bump_bugfix(VERSION)
elif mode == "feature":
    new_version = bump_feature(VERSION)
else:
    new_version = bump_major(VERSION)

version_file = Path("app/version.py")
text = version_file.read_text()
text = re.sub(r'VERSION = "[0-9]+\.[0-9]{2}"', f'VERSION = "{new_version}"', text)
version_file.write_text(text)

pyproject = Path("pyproject.toml")
ptxt = pyproject.read_text()
ptxt = re.sub(r'version = "[0-9]+\.[0-9]{2}"', f'version = "{new_version}"', ptxt)
pyproject.write_text(ptxt)

print(f"Updated version: {VERSION} -> {new_version}")
