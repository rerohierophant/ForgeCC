"""Compatibility entry point for the installed ``cca`` command."""

from __future__ import annotations

import sys

from .__main__ import main as _main


def main(argv: list[str] | None = None) -> int:
    old_argv = sys.argv
    if argv is not None:
        sys.argv = [old_argv[0], *argv]
    try:
        _main()
    except SystemExit as exc:
        return int(exc.code or 0)
    finally:
        sys.argv = old_argv
    return 0


if __name__ == "__main__":
    sys.exit(main())
