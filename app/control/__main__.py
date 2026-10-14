"""``python -m app.control`` — entry point for incubator-control.service.

``--check`` imports the daemon module and exits 0 without touching GPIO or the
database, so an installer / image build can prove the entry point works.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if "--check" in args:
        from . import loop  # noqa: F401 — import is the check

        print("app.control: ok")
        return 0
    from .loop import main as run

    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
