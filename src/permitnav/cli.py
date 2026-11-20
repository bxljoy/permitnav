"""Command line: `permitnav <command>`. Commands are added as phase 1 progresses."""

from __future__ import annotations

import argparse
import sys

from permitnav import db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="permitnav")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply database migrations")
    args = parser.parse_args(argv)

    if args.command == "migrate":
        with db.connect() as conn:
            applied = db.migrate(conn)
        print("\n".join(f"applied {name}" for name in applied) or "database is up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
